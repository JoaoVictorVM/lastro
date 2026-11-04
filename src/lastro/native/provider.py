"""Chamadas ao modelo: SDK Anthropic, contagem prévia, reserva, retry, usage e timeout.

Toda tentativa passa por `ModelGateway.call`: reserva durável antes do envio, `sent` antes da
rede e liquidação ou retenção depois. Os retries internos do SDK ficam desligados para que
nenhuma tentativa aconteça fora do ledger.
"""

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from lastro.native.budget import BudgetLedger
from lastro.native.pricing import Usage, reservation_nusd

API_KEY_MESSAGE = "ANTHROPIC_API_KEY is missing or invalid; the native executor cannot start."
API_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
MAX_JITTER_SECONDS = 0.25

type ErrorKind = Literal["auth", "transient", "rejected"]


class ProviderError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        code: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        not_charged: bool = False,
        request_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.kind, self.code, self.status = kind, code, status
        self.retry_after = retry_after
        self.not_charged = not_charged
        self.request_id = request_id


class ApiAuthError(Exception):
    def __init__(self) -> None:
        super().__init__(API_KEY_MESSAGE)


class ContextLimitError(Exception):
    """Histórico acima do teto: nada é truncado nem resumido em silêncio."""

    def __init__(self, counted: int, limit: int) -> None:
        super().__init__(f"{counted} input tokens exceed the context limit of {limit}")
        self.counted, self.limit = counted, limit


class ApiUnavailableError(Exception):
    """Retries esgotados ou erro definitivo do provedor: a run termina como api_error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ModelRequest:
    model: str
    system: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    max_tokens: int

    def payload(self) -> dict[str, Any]:
        """Exatamente o que é contado e enviado; a contagem recebe o mesmo conteúdo."""
        return {
            "model": self.model,
            "system": self.system,
            "messages": self.messages,
            "tools": self.tools,
            "max_tokens": self.max_tokens,
        }

    def sha256(self) -> str:
        text = json.dumps(self.payload(), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModelResponse:
    content: list[dict[str, Any]]
    stop_reason: str | None
    usage: Usage | None
    request_id: str | None = None

    def tool_uses(self) -> list[dict[str, Any]]:
        return [block for block in self.content if block.get("type") == "tool_use"]


class Provider(Protocol):
    async def count_tokens(self, request: ModelRequest) -> int: ...

    async def create(self, request: ModelRequest) -> ModelResponse: ...


def _retry_after(headers: Any) -> float | None:
    try:
        value = headers.get("retry-after") if headers is not None else None
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def translate_error(error: Exception) -> ProviderError:
    """Só resposta HTTP de recusa prova que não houve geração; o resto fica incerto."""
    import anthropic

    if isinstance(error, anthropic.APITimeoutError):
        return ProviderError("transient", "timeout")
    if isinstance(error, anthropic.APIConnectionError):
        return ProviderError("transient", "connection_error")
    if isinstance(error, anthropic.APIStatusError):
        status = error.status_code
        request_id = getattr(error, "request_id", None)
        retry_after = _retry_after(error.response.headers)
        if status in (401, 403):
            return ProviderError("auth", "api_auth", status=status, not_charged=True)
        if status == 429:
            return ProviderError(
                "transient",
                "rate_limited",
                status=status,
                retry_after=retry_after,
                not_charged=True,
                request_id=request_id,
            )
        if status == 529:
            return ProviderError(
                "transient",
                "overloaded",
                status=status,
                retry_after=retry_after,
                not_charged=True,
                request_id=request_id,
            )
        if status >= 500:
            return ProviderError(
                "transient",
                "server_error",
                status=status,
                retry_after=retry_after,
                request_id=request_id,
            )
        return ProviderError(
            "rejected", f"http_{status}", status=status, not_charged=True, request_id=request_id
        )
    return ProviderError("transient", "unexpected_error")


class AnthropicProvider:
    """API direta, endpoint oficial, serviço padrão, sem streaming nem server tools."""

    def __init__(self, api_key: str, *, timeout: float = API_TIMEOUT_SECONDS) -> None:
        import anthropic

        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout)

    async def count_tokens(self, request: ModelRequest) -> int:
        try:
            result = await self.client.messages.count_tokens(
                model=request.model,
                system=cast(Any, request.system),
                messages=cast(Any, request.messages),
                tools=cast(Any, request.tools),
            )
        except Exception as error:
            raise translate_error(error) from None
        return int(result.input_tokens)

    async def create(self, request: ModelRequest) -> ModelResponse:
        try:
            message = await self.client.messages.create(
                model=request.model,
                system=cast(Any, request.system),
                messages=cast(Any, request.messages),
                tools=cast(Any, request.tools),
                max_tokens=request.max_tokens,
            )
        except Exception as error:
            raise translate_error(error) from None
        usage = message.usage
        return ModelResponse(
            content=[block.model_dump(mode="json", exclude_none=True) for block in message.content],
            stop_reason=message.stop_reason,
            usage=Usage(
                input_tokens=usage.input_tokens,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
                output_tokens=usage.output_tokens,
            ),
            request_id=getattr(message, "_request_id", None),
        )


def backoff(
    attempt: int, retry_after: float | None, jitter: Callable[[float, float], float]
) -> float:
    base = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
    return max(base + jitter(0.0, MAX_JITTER_SECONDS), retry_after or 0.0)


async def count_tokens(
    provider: Provider,
    request: ModelRequest,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> int:
    """Contagem não gera nem cobra; repete com a mesma espera, sem reserva."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await provider.count_tokens(request)
        except ProviderError as error:
            if error.kind == "auth":
                raise ApiAuthError() from None
            if error.kind == "rejected" or attempt == MAX_ATTEMPTS:
                raise ApiUnavailableError(error.code) from None
            await sleep(backoff(attempt, error.retry_after, jitter))
    raise ApiUnavailableError("count_failed")


@dataclass
class GatewayStats:
    attempts: int = 0
    sent: int = 0
    retries: int = 0
    calls: list[str] = field(default_factory=list[str])


class ModelGateway:
    def __init__(
        self,
        provider: Provider,
        ledger: BudgetLedger,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.ledger = ledger
        self.sleep, self.jitter, self.clock = sleep, jitter, clock
        self.stats = GatewayStats()

    def backoff(self, attempt: int, retry_after: float | None) -> float:
        return backoff(attempt, retry_after, self.jitter)

    async def count(self, request: ModelRequest) -> int:
        return await count_tokens(self.provider, request, sleep=self.sleep, jitter=self.jitter)

    async def call(
        self,
        node: str,
        step_index: int,
        request: ModelRequest,
        *,
        context_limit: int | None = None,
    ) -> ModelResponse:
        counted = await self.count(request)
        if context_limit is not None and counted > context_limit:
            raise ContextLimitError(counted, context_limit)
        reserved = reservation_nusd(request.model, counted, request.max_tokens)
        digest = request.sha256()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            call = self.ledger.reserve(
                step_index=step_index,
                attempt=attempt,
                node=node,
                model=request.model,
                payload_sha256=digest,
                max_output_tokens=request.max_tokens,
                reserved_nusd=reserved,
            )
            self.stats.attempts += 1
            self.stats.calls.append(call.id)
            self.ledger.mark_sent(call.id)
            self.stats.sent += 1
            started = self.clock()
            try:
                response = await self.provider.create(request)
            except ProviderError as error:
                latency = int((self.clock() - started) * 1000)
                if error.not_charged:
                    self.ledger.release(call.id, error.code, latency_ms=latency)
                else:
                    self.ledger.uncertain(call.id, error.code, latency_ms=latency)
                if error.kind == "auth":
                    raise ApiAuthError() from None
                if error.kind == "rejected" or attempt == MAX_ATTEMPTS:
                    raise ApiUnavailableError(error.code) from None
                self.stats.retries += 1
                await self.sleep(self.backoff(attempt, error.retry_after))
                continue
            except BaseException:
                # Cancelamento ou falha local com a requisição possivelmente em voo.
                self.ledger.uncertain(call.id, "cancelled")
                raise
            latency = int((self.clock() - started) * 1000)
            if response.usage is None:
                self.ledger.uncertain(call.id, "usage_missing", latency_ms=latency)
                return response
            tools = tuple(
                (str(block.get("id", "")), str(block.get("name", "")))
                for block in response.tool_uses()
            )
            self.ledger.settle(
                call,
                response.usage,
                request_id=response.request_id,
                latency_ms=latency,
                tool_calls=tools,
            )
            return response
        raise ApiUnavailableError("retries_exhausted")
