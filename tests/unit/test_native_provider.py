import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest

from lastro.native.pricing import Usage
from lastro.native.provider import (
    ApiAuthError,
    ApiUnavailableError,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ProviderError,
)


class FakeCall:
    def __init__(self, call_id: str, reserved: int) -> None:
        self.id = call_id
        self.reserved_nusd = reserved
        self.model = "claude-sonnet-5-5"


class FakeLedger:
    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.count = 0

    def reserve(self, **kwargs: Any) -> FakeCall:
        self.count += 1
        call = FakeCall(f"call_{self.count}", kwargs["reserved_nusd"])
        self.log.append(("reserve", call.id))
        return call

    def mark_sent(self, call_id: str) -> None:
        self.log.append(("sent", call_id))

    def settle(self, call: FakeCall, usage: Usage, **kwargs: Any) -> None:
        self.log.append(("settle", call.id))

    def release(self, call_id: str, code: str, **kwargs: Any) -> None:
        self.log.append(("release", call_id))

    def uncertain(self, call_id: str, code: str, **kwargs: Any) -> None:
        self.log.append(("uncertain", call_id))


class ScriptedProvider:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.created = 0

    async def count_tokens(self, request: ModelRequest) -> int:
        return 100

    async def create(self, request: ModelRequest) -> ModelResponse:
        self.created += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await cast(Callable[[], Awaitable[ModelResponse]], outcome)()
        return outcome


def request() -> ModelRequest:
    return ModelRequest("claude-sonnet-5-5", [{"type": "text", "text": "s"}], [], [], 256)


def ok() -> ModelResponse:
    return ModelResponse([{"type": "text", "text": "done"}], "end_turn", Usage(10, 0, 0, 5), "r")


def gateway(provider: Any, ledger: FakeLedger, sleeps: list[float]) -> ModelGateway:
    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    return ModelGateway(provider, ledger, sleep=sleep, jitter=lambda a, b: 0.0)  # type: ignore[arg-type]


def test_three_retries_after_initial_attempt() -> None:
    overloaded = [ProviderError("transient", "overloaded", not_charged=True) for _ in range(4)]
    provider, ledger, sleeps = ScriptedProvider(overloaded), FakeLedger(), []
    with pytest.raises(ApiUnavailableError) as raised:
        asyncio.run(gateway(provider, ledger, sleeps).call("decide", 1, request()))
    assert raised.value.code == "overloaded"
    assert provider.created == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert [kind for kind, _ in ledger.log].count("reserve") == 4
    assert [kind for kind, _ in ledger.log].count("release") == 4


def test_retry_succeeds_with_new_reservation() -> None:
    outcomes = [ProviderError("transient", "server_error"), ok()]
    provider, ledger, sleeps = ScriptedProvider(outcomes), FakeLedger(), []
    response = asyncio.run(gateway(provider, ledger, sleeps).call("decide", 1, request()))
    assert response.stop_reason == "end_turn"
    assert ledger.log == [
        ("reserve", "call_1"),
        ("sent", "call_1"),
        ("uncertain", "call_1"),
        ("reserve", "call_2"),
        ("sent", "call_2"),
        ("settle", "call_2"),
    ]


def test_sdk_retries_disabled() -> None:
    from lastro.native.provider import AnthropicProvider

    provider = AnthropicProvider("sk-ant-test-not-real")
    assert provider.client.max_retries == 0
    assert float(provider.client.timeout) == 60.0  # type: ignore[arg-type]


def test_retry_after_and_cancel() -> None:
    limited = ProviderError("transient", "rate_limited", retry_after=7.5, not_charged=True)
    provider, ledger, sleeps = ScriptedProvider([limited, ok()]), FakeLedger(), []
    asyncio.run(gateway(provider, ledger, sleeps).call("decide", 1, request()))
    assert sleeps == [7.5]

    async def hang() -> ModelResponse:
        await asyncio.sleep(30)
        raise AssertionError

    hanging, cancelled_ledger = ScriptedProvider([hang]), FakeLedger()

    async def scenario() -> None:
        task = asyncio.create_task(gateway(hanging, cancelled_ledger, []).call("act", 2, request()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert cancelled_ledger.log[-1] == ("uncertain", "call_1")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProviderError("auth", "api_auth", status=401, not_charged=True), ApiAuthError),
        (ProviderError("rejected", "http_400", status=400, not_charged=True), ApiUnavailableError),
    ],
)
def test_definitive_errors_are_not_retried(error: ProviderError, expected: type) -> None:
    provider, ledger, sleeps = ScriptedProvider([error]), FakeLedger(), []
    with pytest.raises(expected):
        asyncio.run(gateway(provider, ledger, sleeps).call("decide", 1, request()))
    assert provider.created == 1
    assert sleeps == []


def test_missing_usage_is_uncertain_not_zero() -> None:
    no_usage = ModelResponse([{"type": "text", "text": "x"}], "end_turn", None)
    provider, ledger, sleeps = ScriptedProvider([no_usage]), FakeLedger(), []
    asyncio.run(gateway(provider, ledger, sleeps).call("decide", 1, request()))
    assert ledger.log[-1] == ("uncertain", "call_1")


def test_error_translation_from_sdk() -> None:
    import anthropic
    import httpx

    from lastro.native.provider import translate_error

    def status_error(cls: Callable[..., Exception], code: int, headers: Any = None) -> Exception:
        response = httpx.Response(
            code, headers=headers or {}, request=httpx.Request("POST", "https://x")
        )
        return cls("boom", response=response, body=None)

    assert translate_error(status_error(anthropic.AuthenticationError, 401)).kind == "auth"
    limited = translate_error(status_error(anthropic.RateLimitError, 429, {"retry-after": "3"}))
    assert (limited.kind, limited.retry_after, limited.not_charged) == ("transient", 3.0, True)
    server = translate_error(status_error(anthropic.InternalServerError, 500))
    assert (server.kind, server.not_charged) == ("transient", False)
    timeout = translate_error(
        anthropic.APITimeoutError(request=cast(Any, httpx.Request("POST", "https://x")))
    )
    assert (timeout.code, timeout.not_charged) == ("timeout", False)
