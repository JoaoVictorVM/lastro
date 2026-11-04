"""Reserva e liquidação de cada tentativa de modelo, sempre pela fachada do histórico."""

from lastro.history import BudgetError, HistoryStore, ModelCall, ReservationInput, Settlement
from lastro.native.pricing import Usage, usage_cost_nusd

ACCOUNTING_VIOLATION = "budget_accounting_violation"


class BudgetAccountingViolation(BudgetError):
    """Usage acima da reserva: falha de contabilidade, nunca prova de teto de fatura."""

    def __init__(self, call: ModelCall) -> None:
        super().__init__(
            ACCOUNTING_VIOLATION,
            f"model call {call.id} cost {call.actual_nusd} nUSD above its reservation "
            f"of {call.reserved_nusd} nUSD",
        )
        self.call = call


class BudgetLedger:
    def __init__(self, history: HistoryStore, run_id: str, pricing_id: str = "") -> None:
        self.history = history
        self.run_id = run_id
        self.pricing_id = pricing_id
        self.violated = False

    def reserve(
        self,
        *,
        step_index: int,
        attempt: int,
        node: str,
        model: str,
        payload_sha256: str,
        max_output_tokens: int,
        reserved_nusd: int,
    ) -> ModelCall:
        if self.violated:
            raise BudgetError(
                ACCOUNTING_VIOLATION, "no new model calls after an accounting failure"
            )
        return self.history.reserve_model_call(
            ReservationInput(
                run_id=self.run_id,
                step_index=step_index,
                attempt=attempt,
                node=node,
                model=model,
                payload_sha256=payload_sha256,
                max_output_tokens=max_output_tokens,
                reserved_nusd=reserved_nusd,
            )
        )

    def mark_sent(self, call_id: str) -> ModelCall:
        return self.history.mark_model_call_sent(call_id)

    def settle(
        self,
        call: ModelCall,
        usage: Usage,
        *,
        request_id: str | None,
        latency_ms: int,
        tool_calls: tuple[tuple[str, str], ...],
    ) -> ModelCall:
        """Persiste o valor real mesmo acima da reserva e só então sinaliza a violação."""
        actual = usage_cost_nusd(call.model, usage)
        settled = self.history.settle_model_call(
            call.id,
            Settlement(
                input_tokens=usage.input_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                output_tokens=usage.output_tokens,
                actual_nusd=actual,
                request_id=request_id,
                latency_ms=latency_ms,
                tool_calls=tool_calls,
                pricing_id=self.pricing_id,
            ),
        )
        if actual > call.reserved_nusd:
            self.violated = True
            raise BudgetAccountingViolation(settled)
        return settled

    def release(self, call_id: str, error_code: str, *, latency_ms: int | None = None) -> None:
        self.history.fail_model_call(
            call_id, released=True, error_code=error_code, latency_ms=latency_ms
        )

    def uncertain(
        self, call_id: str, error_code: str, *, latency_ms: int | None = None
    ) -> ModelCall:
        return self.history.fail_model_call(
            call_id, released=False, error_code=error_code, latency_ms=latency_ms
        )
