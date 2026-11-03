import asyncio
import hashlib
import json
from collections.abc import Callable, Coroutine
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from lastro.browser.control import HumanControl
from lastro.browser.permissions import handle
from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.history import (
    ApprovalStatus,
    DecidedBy,
    HistoryStore,
    JsonObject,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.scaffold.files import (
    PREVIOUS_SETTINGS_SHA256,
    InitAnswers,
    missing_hooks,
    write_scaffold,
)

FINGERPRINT = "c" * 64
SUBMIT = "mcp__lastro__submit"
NOTIFICATION_BLOCK = (
    '    ],\n    "Notification": [\n      {\n        "hooks": [\n          {\n'
    '            "type": "command",\n            "command": "lastro-permission-hook"\n'
    "          }\n        ]\n      }\n    ]\n  }\n}"
)


def payload(run_id: str) -> JsonObject:
    return {
        "summary": "edit employee #57 (phone, email) in Clínica Aurora",
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


def with_owner(
    context: ProjectContext,
    scenario: Callable[[HistoryStore, str, HumanControl], Coroutine[Any, Any, None]],
    *,
    start_owner: bool = True,
) -> None:
    history = open_history(context)
    run_id = history.create_run(context, TextMission("Editar")).record.id
    control = HumanControl(BrowserTools(context, history, run_id))

    async def main() -> None:
        if start_owner:
            await control.start()
        try:
            await scenario(history, run_id, control)
        finally:
            await control.close()

    asyncio.run(main())


def hook(tool_input: dict[str, Any], cwd: Path, tool: str = SUBMIT) -> dict[str, Any]:
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "cwd": str(cwd),
    }
    return cast(dict[str, Any], handle(event))["hookSpecificOutput"]


def test_native_prompt_shows_full_request_and_proof_is_single_use(
    history_context: ProjectContext, workdir: Path
) -> None:
    async def scenario(history: HistoryStore, run_id: str, control: HumanControl) -> None:
        history.request_approval(run_id, "act57", FINGERPRINT, payload(run_id))

        target = {"role": "button", "name": "Salvar"}
        output = hook({"target": target, "approval_id": "act57"}, workdir)

        assert output["permissionDecision"] == "ask"
        reason = output["permissionDecisionReason"]
        for expected in (
            "Lastro needs approval: edit employee #57 (phone, email) in Clínica Aurora",
            f"Run: {run_id}",
            "Tenant: Clínica Aurora (acme)",
            "  - phone: 1 -> 2",
            "Reason: Validar persistência",
            "Options: approve | skip | end run",
        ):
            assert expected in reason
        token = output["updatedInput"]["approval_proof"]
        assert output["updatedInput"]["approval_id"] == "act57"
        assert control.secret not in json.dumps(output)

        assert control.accept_proof("act57", token)
        record = history.get_approval("act57", run_id)
        assert record is not None
        assert (record.status, record.decided_by) == (
            ApprovalStatus.APPROVED,
            DecidedBy.EXECUTOR_PROMPT,
        )
        assert history.get_run(run_id).status == RunStatus.RUNNING
        assert not control.accept_proof("act57", token)

    with_owner(history_context, scenario)


def test_forged_or_model_supplied_proof_never_approves(
    history_context: ProjectContext, workdir: Path
) -> None:
    async def scenario(history: HistoryStore, run_id: str, control: HumanControl) -> None:
        history.request_approval(run_id, "act57", FINGERPRINT, payload(run_id))
        token = hook({"approval_id": "act57"}, workdir)["updatedInput"]["approval_proof"]

        supplied = hook({"approval_id": "act57", "approval_proof": token}, workdir)

        assert supplied["permissionDecision"] == "deny"
        assert "reserved to the executor adapter" in supplied["permissionDecisionReason"]
        for forged in ("opaque.v1.redacted", token[:-2] + "00", "approve"):
            assert not control.accept_proof("act57", forged)
        assert not control.accept_proof("other", token)
        record = history.get_approval("act57", run_id)
        assert record is not None and record.status == ApprovalStatus.PENDING

    with_owner(history_context, scenario)


def test_unsupported_hook_never_approves(history_context: ProjectContext, workdir: Path) -> None:
    async def scenario(history: HistoryStore, run_id: str, control: HumanControl) -> None:
        no_request = hook({"approval_id": "act57"}, workdir)
        history.request_approval(run_id, "act57", FINGERPRINT, payload(run_id))
        other_id = hook({"approval_id": "different"}, workdir)
        without_owner = hook({"approval_id": "act57"}, workdir)
        observe = hook({"approval_id": "act57"}, workdir, tool="mcp__lastro__observe")
        foreign = hook({}, workdir, tool="mcp__browser__click")

        for output in (no_request, other_id, without_owner, observe):
            assert output["permissionDecision"] == "allow"
            assert "updatedInput" not in output
        assert foreign["permissionDecision"] == "deny"
        record = history.get_approval("act57", run_id)
        assert record is not None and record.status == ApprovalStatus.PENDING
        assert history.get_run(run_id).status == RunStatus.AWAITING_APPROVAL

    with_owner(history_context, scenario, start_owner=False)


def test_scaffold_updates_published_settings_and_diagnoses_local_edits(tmp_path: Path) -> None:
    template = (
        files("lastro.scaffold")
        .joinpath("templates", "claude_settings.json.tmpl")
        .read_text(encoding="utf-8")
    )
    previous = template.replace(NOTIFICATION_BLOCK, "    ]\n  }\n}")
    assert previous != template
    assert hashlib.sha256(previous.encode()).hexdigest() in PREVIOUS_SETTINGS_SHA256
    assert missing_hooks(json.loads(template)) == []
    assert missing_hooks(json.loads(previous)) == ["Notification"]
    answers = InitAnswers("homolog", "https://x.test", "https://x.test/login", "acme")

    published = tmp_path / "published"
    (published / ".claude").mkdir(parents=True)
    (published / ".claude" / "settings.json").write_text(previous, encoding="utf-8")
    updated = write_scaffold(published, answers)
    assert Path(".claude") / "settings.json" in updated.updated
    assert (published / ".claude" / "settings.json").read_text(encoding="utf-8") == template

    custom = tmp_path / "custom"
    (custom / ".claude").mkdir(parents=True)
    (custom / ".claude" / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    kept = write_scaffold(custom, answers)
    assert (custom / ".claude" / "settings.json").read_text(encoding="utf-8") == '{"hooks": {}}'
    assert any("PreToolUse, Notification" in line for line in kept.diagnostics)
