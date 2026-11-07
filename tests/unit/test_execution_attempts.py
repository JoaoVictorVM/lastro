from lastro.browser.tools import failed_result
from lastro.config.loader import ProjectContext
from lastro.execution.models import WriteIntentInput
from lastro.execution.progress import operation_key
from lastro.history import TextMission, open_history


def test_third_failed_attempt_stops(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Explore")).record.id
    key = operation_key("click", {"target": {"label": "Salvar"}}, "t1", "/employees/1/edit")
    outcomes = []
    for index in range(3):
        reservation = history.reserve_action(run_id, f"a{index}", "click", key)
        assert reservation.attempt_no == index + 1
        outcomes.append(
            history.complete_action(run_id, f"a{index}", failed=True, result_code="timeout")
        )
    assert [item.stop_reason for item in outcomes] == [None, None, "attempt_limit"]
    state = history.execution_state(run_id)
    assert (state.stop_status, state.stop_reason) == ("limit_reached", "attempt_limit")


def test_approval_wait_is_not_failure() -> None:
    assert not failed_result({"decision": "awaiting_approval", "approval_id": "x"})
    assert not failed_result({"decision": "deny", "reason": "approval_pending"})
    assert not failed_result({"decision": "not_executed", "reason": "approval_skipped"})
    assert failed_result({"decision": "deny", "reason": "field_not_editable"})
    assert failed_result({"error": "Element 'x' not found"})
    assert not failed_result({"status": "ok"})


def test_unknown_write_is_not_retried(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Explore")).record.id
    history.reserve_action(run_id, "a1", "submit")
    intent = history.prepare_write(
        WriteIntentInput(
            run_id=run_id,
            action_id="a1",
            binding_id="employee-update",
            operation="update",
            entity="employee",
            record_id="1",
            tenant="t1",
            detail_path="/employees/1/edit",
            before={"email": "a@example.com"},
            intended={"email": "b@example.com"},
            comparable=True,
        )
    )
    history.mark_write_sending(intent.id)
    history.mark_unknown_writes(run_id)
    # Nenhuma intenção nova nasce da recuperação: o reenvio nunca é automático.
    assert [item.id for item in history.list_write_intents(run_id)] == [intent.id]
    assert history.list_write_intents(run_id)[0].state.value == "response_unknown"


def test_agent_cannot_choose_operation_key() -> None:
    base = {"target": {"label": "Salvar"}}
    key = operation_key("click", base, "t1", "/employees/1/edit")
    # Prova, id de aprovação, motivo e timeout não mudam a identidade da operação.
    noisy = {**base, "approval_id": "x", "approval_proof": "p", "reason": "r", "timeout_ms": 9}
    assert operation_key("click", noisy, "t1", "/employees/1/edit") == key
    # Tenant e rota observados pelo servidor fazem parte da identidade.
    assert operation_key("click", base, "t2", "/employees/1/edit") != key
    assert operation_key("click", base, "t1", "/employees/2/edit") != key
    assert operation_key("observe", {}, "t1", "/") is None
