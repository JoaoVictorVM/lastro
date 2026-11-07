import asyncio
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from lastro.browser.demo import FIELDS, demo_policy
from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.config.policy import Preauthorized
from lastro.execution.models import WriteIntentInput
from lastro.execution.resume import (
    RESUME_CONTEXT_MISMATCH,
    RESUME_NOT_ELIGIBLE,
    RESUME_SOURCE_ACTIVE,
    ResumeError,
    create_resumed_run,
    preflight,
)
from lastro.history import EventType, RunStatus, TextMission, open_history
from lastro.history.store import HistoryStore

EMAIL = "resume-applied@example.com"


def _writable(context: ProjectContext, *, preauthorized: bool = True) -> ProjectContext:
    context.profile.policy = demo_policy()
    if preauthorized:
        context.profile.policy.preauthorized_records = [
            Preauthorized(tenant="aurora", entity="employee", id=str(i)) for i in range(1, 101)
        ]
    context.profile.editable_fields = {"employee": list(FIELDS)}
    return context


def _posts(demo_server: Any, path: str = "/employees/1") -> int:
    return sum(1 for method, item in demo_server.access_log if (method, item) == ("POST", path))


async def _edit(runtime: BrowserTools, base: str, record: str, value: str) -> dict[str, Any]:
    await runtime.call("navigate", {"url": f"{base}/employees/{record}/edit"})
    filled = await runtime.call("fill", {"target": {"label": "E-mail"}, "value": value})
    assert filled.get("status") == "ok" or filled.get("decision"), filled
    return await runtime.call("submit", {"target": {"role": "button", "name": "Salvar"}})


def _interrupt_after_send(history: HistoryStore, run_id: str) -> str:
    """Simula a queda entre o envio e o registro da resposta: o efeito fica desconhecido."""
    (intent,) = history.list_write_intents(run_id)
    with sqlite3.connect(history.database_path) as conn:
        conn.execute(
            "UPDATE write_intents SET state = 'response_unknown', response_status = NULL "
            "WHERE id = ?",
            (intent.id,),
        )
    history.finish_run(run_id, RunStatus.INTERRUPTED, "process_lost")
    return intent.id


def _resume(history: HistoryStore, context: ProjectContext, parent: str) -> BrowserTools:
    source = preflight(history, context, parent)
    created = create_resumed_run(history, context, source)
    return BrowserTools(context, history, created.record.id)


def test_edit_applied_before_interrupt_is_not_resent(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    context = _writable(make_auth_project().context())
    history = open_history(context)
    first = BrowserTools(
        context, history, history.create_run(context, TextMission("Edit")).record.id
    )

    async def original() -> None:
        try:
            result = await _edit(first, context.profile.target_url, "1", EMAIL)
            assert result["status"] == "ok", result
        finally:
            await first.session.close()

    asyncio.run(original())
    assert _posts(demo_server) == 1
    intent_id = _interrupt_after_send(history, first.run_id)
    second = _resume(history, context, first.run_id)

    async def resumed() -> dict[str, Any]:
        try:
            return await _edit(second, context.profile.target_url, "1", EMAIL)
        finally:
            await second.session.close()

    result = asyncio.run(resumed())
    assert result["resume_check"]["state"] == "already_applied", result
    assert result["resume_check"]["write_repeated"] is False
    assert result["resume_check"]["source_write_id"] == intent_id
    # Verificado no log de acesso do demo: nenhum segundo POST.
    assert _posts(demo_server) == 1
    (reconciliation,) = history.list_reconciliations(second.run_id)
    assert reconciliation.state.value == "already_applied"
    assert reconciliation.observation_artifact_id is not None
    assert history.parent_run(second.run_id) == first.run_id
    events = [e.event_type for e in history.list_events(second.run_id)]
    assert EventType.RUN_RESUMED in events and EventType.WRITE_RECONCILED in events


def test_unknown_create_requires_manual_review(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    parent = history.create_run(history_context, TextMission("Create")).record.id
    history.reserve_action(parent, "c1", "submit")
    intent = history.prepare_write(
        WriteIntentInput(
            run_id=parent,
            action_id="c1",
            binding_id="employee-create",
            operation="create",
            entity="employee",
            record_id=None,
            tenant="t1",
            detail_path=None,
            before=None,
            intended={"full_name": "Ana"},
            comparable=False,
        )
    )
    history.mark_write_sending(intent.id)
    history.mark_unknown_writes(parent)
    history.finish_run(parent, RunStatus.INTERRUPTED, "process_lost")
    source = preflight(history, history_context, parent)
    child = create_resumed_run(history, history_context, source).record.id
    (reconciliation,) = history.list_reconciliations(child)
    assert reconciliation.state.value == "inconclusive"
    pending = history.pending_items(child)
    assert ("reconciliation", "inconclusive") in {(item.kind, item.state) for item in pending}
    assert all(item.requires_manual_review for item in pending if item.kind == "reconciliation")


def test_new_approval_required_on_resumed_edit(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    context = _writable(make_auth_project().context())
    history = open_history(context)
    first = BrowserTools(
        context, history, history.create_run(context, TextMission("Edit")).record.id
    )
    history.reserve_action(first.run_id, "w1", "submit")
    history.prepare_write(
        WriteIntentInput(
            run_id=first.run_id,
            action_id="w1",
            binding_id="employee-update",
            operation="update",
            entity="employee",
            record_id="2",
            tenant="aurora",
            detail_path="/employees/2/edit",
            before=None,
            intended={"email": "never-sent@example.com"},
            comparable=False,
        )
    )
    with sqlite3.connect(history.database_path) as conn:
        conn.execute("UPDATE write_intents SET state = 'response_unknown'")
    history.finish_run(first.run_id, RunStatus.CANCELLED, "user_cancelled")
    # Política atual sem pré-autorização: a run nova precisa da sua própria aprovação.
    current = _writable(make_auth_project().context(), preauthorized=False)
    history = open_history(current)
    second = _resume(history, current, first.run_id)

    async def resumed() -> dict[str, Any]:
        try:
            await second.call("navigate", {"url": current.profile.target_url + "/employees/3/edit"})
            await second.call("fill", {"target": {"label": "E-mail"}, "value": "fresh@example.com"})
            return await second.call("submit", {"target": {"role": "button", "name": "Salvar"}})
        finally:
            await second.session.close()

    result = asyncio.run(resumed())
    assert result["decision"] == "awaiting_approval", result
    assert _posts(demo_server, "/employees/3") == 0


def test_changed_tool_cannot_bypass_resume_barrier(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    context = _writable(make_auth_project().context())
    history = open_history(context)
    first = BrowserTools(
        context, history, history.create_run(context, TextMission("Edit")).record.id
    )

    async def original() -> None:
        try:
            await _edit(first, context.profile.target_url, "1", "first@example.com")
        finally:
            await first.session.close()

    asyncio.run(original())
    _interrupt_after_send(history, first.run_id)
    second = _resume(history, context, first.run_id)

    async def resumed() -> list[dict[str, Any]]:
        try:
            await second.call("navigate", {"url": context.profile.target_url + "/employees/1/edit"})
            await second.call("fill", {"target": {"label": "E-mail"}, "value": "other@example.com"})
            # click no mesmo botão, com outro valor, passa pela mesma barreira que submit.
            via_click = await second.call("click", {"target": {"role": "button", "name": "Salvar"}})
            return [via_click]
        finally:
            await second.session.close()

    results = asyncio.run(resumed())
    # Valor atual já é o pretendido pela intenção original: aplicada, nada reenviado.
    assert all(item["resume_check"]["state"] == "already_applied" for item in results), results
    assert _posts(demo_server) == 1


def test_resume_chain_preserves_original_pending_intent(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    root = history.create_run(history_context, TextMission("Edit")).record.id
    history.reserve_action(root, "w1", "submit")
    intent = history.prepare_write(
        WriteIntentInput(
            run_id=root,
            action_id="w1",
            binding_id="employee-update",
            operation="update",
            entity="employee",
            record_id="5",
            tenant="t1",
            detail_path="/employees/5/edit",
            before={"email": "a@example.com"},
            intended={"email": "b@example.com"},
            comparable=True,
        )
    )
    history.mark_write_sending(intent.id)
    history.mark_unknown_writes(root)
    history.finish_run(root, RunStatus.INTERRUPTED, "process_lost")
    middle = create_resumed_run(
        history, history_context, preflight(history, history_context, root)
    ).record.id
    history.finish_run(middle, RunStatus.CANCELLED, "user_cancelled")
    leaf = create_resumed_run(
        history, history_context, preflight(history, history_context, middle)
    ).record.id
    assert [item.id for item in history.unresolved_writes(leaf)] == [intent.id]
    writes = [item for item in history.pending_items(leaf) if item.kind == "write"]
    assert [(item.reference, item.run_id) for item in writes] == [(intent.id, root)]


def test_legacy_run_never_replays_unverifiable_write(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    legacy = history.create_run(history_context, TextMission("Edit")).record.id
    # Run anterior ao journal: só o evento da chamada, sem intenção nem valores verificáveis.
    history.append_event(legacy, EventType.TOOL_CALL, tool_name="submit", correlation_id="old1")
    history.finish_run(legacy, RunStatus.INTERRUPTED, "process_lost")
    child = create_resumed_run(
        history, history_context, preflight(history, history_context, legacy)
    ).record.id
    pending = [item for item in history.pending_items(child) if item.kind == "write"]
    assert [(item.reference, item.state, item.run_id) for item in pending] == [
        ("old1", "unverifiable", legacy)
    ]
    assert all(item.requires_manual_review for item in pending)
    assert history.unresolved_writes(child) == []


def test_active_parent_and_context_mismatch_are_rejected(
    history_context_for: Callable[[str], ProjectContext],
) -> None:
    homolog = history_context_for("homolog")
    history = open_history(homolog)
    active = history.create_run(homolog, TextMission("Explore")).record.id
    with pytest.raises(ResumeError) as busy:
        preflight(history, homolog, active)
    assert busy.value.code == RESUME_SOURCE_ACTIVE
    history.finish_run(active, RunStatus.INTERRUPTED, "process_lost")
    local = history_context_for("local")
    with pytest.raises(ResumeError) as other:
        preflight(open_history(local), local, active)
    assert other.value.code == RESUME_CONTEXT_MISMATCH
    done = history.create_run(homolog, TextMission("Explore")).record.id
    history.finish_run(done, RunStatus.COMPLETED)
    with pytest.raises(ResumeError) as finished:
        preflight(history, homolog, done)
    assert finished.value.code == RESUME_NOT_ELIGIBLE
    runs = [summary.record.id for summary in history.list_runs()]
    assert len(runs) == 2


def test_missing_record_on_resume_is_inconclusive(
    make_auth_project: Callable[..., Any], demo_server: Any
) -> None:
    context = _writable(make_auth_project().context())
    history = open_history(context)
    parent = history.create_run(context, TextMission("Edit")).record.id
    history.reserve_action(parent, "w1", "submit")
    history.prepare_write(
        WriteIntentInput(
            run_id=parent,
            action_id="w1",
            binding_id="employee-update",
            operation="update",
            entity="employee",
            record_id="9999",
            tenant="aurora",
            detail_path="/employees/9999/edit",
            before={"email": "a@example.com"},
            intended={"email": "b@example.com"},
            comparable=True,
        )
    )
    with sqlite3.connect(history.database_path) as conn:
        conn.execute("UPDATE write_intents SET state = 'response_unknown'")
    history.finish_run(parent, RunStatus.INTERRUPTED, "process_lost")
    runtime = _resume(history, context, parent)

    async def resumed() -> dict[str, Any]:
        try:
            return await runtime.call(
                "navigate", {"url": context.profile.target_url + "/employees/9999/edit"}
            )
        finally:
            await runtime.session.close()

    result = asyncio.run(resumed())
    assert result["resume_check"]["message"] == (
        "Record 9999 not found on resume; no write repeated."
    )
    assert result["resume_check"]["state"] == "inconclusive"
    assert _posts(demo_server, "/employees/9999") == 0


def test_no_cross_project_resume(
    history_context: ProjectContext, make_auth_project: Callable[..., Any]
) -> None:
    from lastro.history import RunNotFoundError

    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Explore")).record.id
    history.finish_run(run_id, RunStatus.INTERRUPTED, "process_lost")
    other = make_auth_project().context()
    with pytest.raises(RunNotFoundError):
        preflight(open_history(other), other, run_id)
