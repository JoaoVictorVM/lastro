import asyncio
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from lastro.approvals import ApprovalAction, FieldChange
from lastro.approvals.gate import ApprovalGate
from lastro.browser.policy import Action, fingerprint_components
from lastro.config.loader import ProjectContext
from lastro.history import (
    ApprovalRecord,
    ApprovalStatus,
    HistoryStore,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.history.redaction import canonical_json, sha256_hex


def view(run_id: str, value: str = "new@example.com", page_state: str = "p") -> ApprovalAction:
    action = Action(
        "update",
        "employee",
        "57",
        {"email": value},
        "POST",
        run_id=run_id,
        tool="submit",
        route="/employees/57/edit",
        changes=(("email", "old@example.com", value),),
        page_state=page_state,
    )
    components = fingerprint_components(action, "acme")
    return ApprovalAction(
        tool="submit",
        run_id=run_id,
        profile="homolog",
        environment_url="https://homolog.example.test",
        tenant="acme",
        tenant_display="Acme",
        entity="employee",
        record_id="57",
        operation="update",
        method="POST",
        route="/employees/57/edit",
        target={},
        origin="unknown",
        changes=(FieldChange("email", "old@example.com", value),),
        components=components,
        fingerprint=sha256_hex(canonical_json(components)),
    )


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def notify(self, record: ApprovalRecord) -> None:
        self.sent.append(record.action_id)
        if self.fail:
            raise RuntimeError("notifier down")


def started(
    context: ProjectContext, clock: Callable[[], datetime], notifier: Recorder | None = None
) -> tuple[HistoryStore, str, ApprovalGate]:
    history = open_history(context, clock=clock)
    run_id = history.create_run(context, TextMission("gate")).record.id
    return history, run_id, ApprovalGate(history, run_id, notifier=notifier, clock=clock)


def test_request_notifies_and_failed_notifier_keeps_request(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    notifier = Recorder(fail=True)
    history, run_id, gate = started(history_context, fake_clock, notifier)

    response = asyncio.run(gate.request(view(run_id), "act1"))

    assert response["decision"] == "awaiting_approval"
    assert notifier.sent == ["act1"]
    assert gate.pending() is not None
    assert history.get_run(run_id).status == RunStatus.AWAITING_APPROVAL


def test_retry_states_and_single_consumption(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id, gate = started(history_context, fake_clock)
    action = view(run_id)
    asyncio.run(gate.request(action, "act1"))

    assert gate.retry("missing", action, "r0").reason == "approval_not_found"
    assert gate.retry("act1", action, "r0").kind == "pending"
    history.decide_approval("act1", "approve", "cli")
    first = gate.retry("act1", action, "r1")
    second = gate.retry("act1", action, "r2")

    assert first.kind == "execute"
    assert first.record is not None and first.record.status == ApprovalStatus.CONSUMED
    assert (second.kind, second.reason) == ("rejected", "approval_consumed")
    gate.execution_failed("act1", "network_timeout")
    record = history.get_approval("act1", run_id)
    assert record is not None and record.status == ApprovalStatus.EXECUTION_FAILED


def test_changed_fingerprint_reports_first_field(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id, gate = started(history_context, fake_clock)
    asyncio.run(gate.request(view(run_id), "act1"))
    history.decide_approval("act1", "approve", "cli")

    outcome = gate.retry("act1", view(run_id, page_state="other"), "r1")

    assert (outcome.kind, outcome.changed_field) == ("changed", "page_state")
    assert outcome.record is not None and outcome.record.status == ApprovalStatus.INVALIDATED
    renewed = asyncio.run(gate.renew(outcome, view(run_id, page_state="other"), "act2"))
    assert renewed["message"] == (
        "The action changed after approval (page_state); approval required again."
    )
    assert renewed["approval_id"] == "act2"


def test_status_waits_for_decision_without_deciding(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id, gate = started(history_context, fake_clock)
    asyncio.run(gate.request(view(run_id), "act1"))
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            history.decide_approval("act1", "skip", "cli")

    gate.sleep = fake_sleep
    record = asyncio.run(gate.status("act1", 30))

    assert record is not None and record.status == ApprovalStatus.SKIPPED
    assert sleeps == [0.25, 0.25]
    immediate = asyncio.run(gate.status("act1", 0))
    assert immediate is not None and immediate.status == ApprovalStatus.SKIPPED


def test_timer_expires_request_and_close_cancels(
    history_context: ProjectContext, clock_now: list[datetime], fake_clock: Callable[[], datetime]
) -> None:
    history, run_id, gate = started(history_context, fake_clock)

    async def scenario() -> None:
        await gate.request(view(run_id), "act1")
        assert len(gate.timers) == 1
        clock_now[0] += timedelta(seconds=900)
        gate.schedule(gate.pending())  # type: ignore[arg-type]
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    record = history.get_approval("act1", run_id)
    assert record is not None and record.status == ApprovalStatus.EXPIRED
    assert history.get_run(run_id).status == RunStatus.RUNNING
    gate.close()
    assert gate.timers == set()


def test_concurrent_retries_execute_once(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    history, run_id, gate = started(history_context, fake_clock)
    action = view(run_id)
    asyncio.run(gate.request(action, "act1"))
    history.decide_approval("act1", "approve", "cli")
    barrier = threading.Barrier(5)
    kinds: list[str] = []

    def worker(index: int) -> None:
        store = open_history(history_context, clock=fake_clock)
        other = ApprovalGate(store, run_id, clock=fake_clock)
        barrier.wait()
        kinds.append(other.retry("act1", action, f"r{index}").kind)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(kinds) == ["execute", "rejected", "rejected", "rejected", "rejected"]


def test_fingerprint_covers_context_and_ignores_retry_fields() -> None:
    from lastro.browser.policy import PolicyEngine
    from lastro.config.schema import Profile

    profile = Profile.model_validate(
        {
            "target_url": "https://example.com",
            "login_url": "https://example.com/login",
            "tenant_id": "acme",
            "allowed_entities": ["employee"],
            "editable_fields": {"employee": ["email"]},
            "credentials": {"username_env": "USER", "password_env": "PASS"},
            "auth": {
                "verification": [
                    {"type": "api", "endpoint": "/api/me", "tenant_json_path": "tenant.id"}
                ]
            },
        }
    )
    engine = PolicyEngine(profile)
    base = Action("update", "employee", "57", {"email": "a@example.com"}, run_id="run_a")

    def fingerprint(**changes: object) -> str:
        fields = {**base.__dict__, **changes}
        return engine.decide(Action(**fields), True).fingerprint  # type: ignore[arg-type]

    reference = fingerprint()
    assert fingerprint() == reference
    for change in (
        {"run_id": "run_b"},
        {"record_id": "58"},
        {"values": {"email": "b@example.com"}},
        {"page_state": "changed"},
        {"route": "/employees/58/edit"},
        {"target": (("label", "E-mail"),)},
        {"changes": (("email", "x", "a@example.com"),)},
        {"tool": "fill"},
    ):
        assert fingerprint(**change) != reference, change
    decision = engine.decide(base, True)
    assert decision.components is not None
    assert list(decision.components) == [
        "tenant",
        "target",
        "record",
        "operation",
        "fields",
        "values",
        "page_state",
    ]
