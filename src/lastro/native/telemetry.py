"""Telemetria por tentativa de chamada de modelo, lida do ledger durável.

É o contrato entregue à observabilidade: cada tentativa traz modelo, nó, tokens, custo, reserva,
latência, request id, tool calls e o evento que a encerrou. Valores desconhecidos ficam None;
nada aqui exporta spans nem depende de um collector.
"""

from dataclasses import dataclass
from typing import Literal

from lastro.history import HistoryStore, ModelCall
from lastro.native.pricing import format_usd, parse_usd

type UsageStatus = Literal["reported", "uncertain", "not_charged", "pending"]
STATUS: dict[str, UsageStatus] = {
    "settled": "reported",
    "uncertain": "uncertain",
    "released": "not_charged",
    "reserved": "pending",
    "sent": "pending",
}


@dataclass(frozen=True)
class ModelCallTelemetry:
    call_id: str
    run_id: str
    event_id: int | None
    node: str
    step_index: int
    attempt: int
    model: str
    input_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    total_input_tokens: int | None
    output_tokens: int | None
    cost_usd: str | None
    reserved_cost_usd: str
    latency_ms: int | None
    request_id: str | None
    tool_calls: tuple[tuple[str, str], ...]
    usage_status: UsageStatus
    error_code: str | None


@dataclass(frozen=True)
class TelemetryTotals:
    calls: int
    reported_calls: int
    uncertain_calls: int
    total_input_tokens: int
    output_tokens: int
    cost_usd: str


def from_call(call: ModelCall) -> ModelCallTelemetry:
    known = call.status == "settled"
    total = (
        (call.input_tokens or 0) + (call.cache_read_tokens or 0) + (call.cache_write_tokens or 0)
        if known
        else None
    )
    return ModelCallTelemetry(
        call_id=call.id,
        run_id=call.run_id,
        event_id=call.completion_event_id,
        node=call.node,
        step_index=call.step_index,
        attempt=call.attempt,
        model=call.model,
        input_tokens=call.input_tokens,
        cache_read_input_tokens=call.cache_read_tokens,
        cache_creation_input_tokens=call.cache_write_tokens,
        total_input_tokens=total,
        output_tokens=call.output_tokens,
        cost_usd=format_usd(call.actual_nusd) if call.actual_nusd is not None else None,
        reserved_cost_usd=format_usd(call.reserved_nusd),
        latency_ms=call.latency_ms,
        request_id=call.request_id,
        tool_calls=call.tool_calls,
        usage_status=STATUS[call.status],
        error_code=call.error_code,
    )


def model_calls(history: HistoryStore, run_id: str) -> list[ModelCallTelemetry]:
    return [from_call(call) for call in history.list_model_calls(run_id)]


def totals(calls: list[ModelCallTelemetry]) -> TelemetryTotals:
    """Soma só usage reportado; tentativas incertas são contadas à parte, nunca como zero."""
    reported = [call for call in calls if call.usage_status == "reported"]
    nusd = sum(parse_usd(call.cost_usd) for call in reported if call.cost_usd is not None)
    return TelemetryTotals(
        calls=len(calls),
        reported_calls=len(reported),
        uncertain_calls=sum(1 for call in calls if call.usage_status in ("uncertain", "pending")),
        total_input_tokens=sum(call.total_input_tokens or 0 for call in reported),
        output_tokens=sum(call.output_tokens or 0 for call in reported),
        cost_usd=format_usd(nusd),
    )
