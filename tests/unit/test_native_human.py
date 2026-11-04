import asyncio
from typing import Any

from lastro.config.loader import ProjectContext
from lastro.history import (
    ApprovalChoice,
    ApprovalStatus,
    DecidedBy,
    JsonObject,
    TextMission,
    open_history,
)
from lastro.native.human import HumanDecisionPort, parse_choice

FINGERPRINT = "d" * 64


def payload(run_id: str) -> JsonObject:
    return {
        "summary": "edit employee #57 (phone) in Clínica Aurora",
        "run_id": run_id,
        "environment": {"profile": "homolog", "url": "https://homolog.example.test"},
        "tenant": {"id": "acme", "display": "Clínica Aurora"},
        "record": {"entity": "employee", "id": "57"},
        "action": {"operation": "update", "method": "POST", "route": "/employees/57/edit"},
        "changes": [{"field": "phone", "old": "1", "new": "2"}],
        "reason": "Validar persistência",
        "known_effects": ["Sends POST /employees/57"],
        "components": {},
    }


def pending(context: ProjectContext) -> tuple[Any, str, str]:
    history = open_history(context)
    run_id = history.create_run(context, TextMission("Editar")).record.id
    history.request_approval(run_id, "act57", FINGERPRINT, payload(run_id))
    return history, run_id, "act57"


def port(history: Any, run_id: str, lines: list[str | None], interactive: bool = True) -> Any:
    output: list[str] = []
    answers = iter(lines)

    async def sleep(_: float) -> None:
        await asyncio.sleep(0.01)

    human = HumanDecisionPort(
        history=history,
        run_id=run_id,
        write=output.append,
        interactive=interactive,
        timezone="America/Sao_Paulo",
        read_line=lambda: next(answers, None),
        sleep=sleep,
        poll_seconds=0.0,
    )
    return human, output


def test_human_prompt_shows_persisted_fingerprint(history_context: ProjectContext) -> None:
    history, run_id, action = pending(history_context)
    human, output = port(history, run_id, ["approve"])
    record = asyncio.run(human.decide(action))
    assert record.status == ApprovalStatus.APPROVED
    assert record.decided_by == DecidedBy.CLI
    assert f"Fingerprint: {FINGERPRINT}" in output
    assert any("Options: approve | skip | end run" in line for line in output)


def test_eof_never_approves(history_context: ProjectContext) -> None:
    history, run_id, action = pending(history_context)
    human, output = port(history, run_id, [None])

    async def scenario() -> Any:
        task = asyncio.create_task(human.decide(action))
        await asyncio.sleep(0.3)
        assert not task.done()
        assert history.get_approval(action).status == ApprovalStatus.PENDING
        history.decide_approval(action, ApprovalChoice.SKIP, DecidedBy.CLI)
        return await asyncio.wait_for(task, 5)

    record = asyncio.run(scenario())
    assert record.status == ApprovalStatus.SKIPPED
    assert any("Nothing was approved" in line for line in output)


def test_no_tty_waits_without_model_calls(history_context: ProjectContext) -> None:
    history, run_id, action = pending(history_context)
    human, output = port(history, run_id, [], interactive=False)
    calls: list[str] = []

    async def scenario() -> Any:
        task = asyncio.create_task(human.decide(action))
        await asyncio.sleep(0.2)
        assert not task.done()
        history.decide_approval(action, ApprovalChoice.APPROVE, DecidedBy.CLI)
        return await asyncio.wait_for(task, 5)

    record = asyncio.run(scenario())
    assert record.status == ApprovalStatus.APPROVED
    assert any("lastro approve act57" in line for line in output)
    assert calls == []


def test_clarification_is_not_approval(history_context: ProjectContext) -> None:
    for text in ("yes", "ok", "sim", "approve it", "", "  ", "the field must be required"):
        assert parse_choice(text) is None
    assert parse_choice(None) is None
    history, run_id, action = pending(history_context)
    human, output = port(history, run_id, ["yes please", "skip"])
    record = asyncio.run(human.decide(action))
    assert record.status == ApprovalStatus.SKIPPED
    assert "Answer approve, skip or end." in output
