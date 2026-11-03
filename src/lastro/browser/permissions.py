"""Hook do executor: catálogo Lastro, prompt nativo de aprovação e notificação de permissão.

O hook nunca aprova sozinho. Quando há pedido pendente para o retry, ele devolve `ask` com o
pedido completo e um input atualizado com a prova; o executor só aplica esse input depois que o
humano aceita o prompt. Sem pedido, sem canal ou com erro, o retry segue sem prova e o servidor
mantém a espera pela CLI.
"""

import json
import sys
from pathlib import Path
from typing import Any, cast

from lastro.approvals.gate import PromptAdapter
from lastro.browser.tools import EFFECT_TOOLS, TOOLS

PREFIX = "mcp__lastro__"
CATALOG = frozenset(PREFIX + tool for tool in TOOLS)
PROOF_FIELD = "approval_proof"
RESERVED_REASON = "approval_proof is reserved to the executor adapter."


def _output(permission: str, reason: str, **extra: object) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": permission,
            "permissionDecisionReason": reason,
            **extra,
        }
    }


def decision(name: str) -> dict[str, object]:
    return _output(
        "allow" if name in CATALOG else "deny",
        "Mission tools are restricted to the Lastro catalog.",
    )


def approval_prompt(tool_input: dict[str, Any], cwd: Path) -> tuple[str, str] | None:
    """Texto completo do pedido e prova curta, somente para o pedido pendente da run ativa."""
    from lastro.approvals import request_lines
    from lastro.approvals.proof import issue
    from lastro.browser.control import owner_secret
    from lastro.config.loader import load_config_file
    from lastro.history import open_history
    from lastro.timeutil import to_display_zone, utc_now

    approval_id = tool_input.get("approval_id")
    if not isinstance(approval_id, str):
        return None
    loaded = load_config_file(None, cwd)
    history = open_history(loaded)
    run = history.get_active_run()
    if run is None:
        return None
    pending = history.pending_approval(run.id)
    if pending is None or pending.action_id != approval_id:
        return None
    secret = owner_secret(loaded.project_data_dir, run.id)
    token = issue(secret, pending.action_id, pending.fingerprint, utc_now())
    expires = to_display_zone(pending.expires_at, loaded.config.timezone).strftime("%H:%M")
    return "\n".join(request_lines(pending, f"{expires} ({loaded.config.timezone})")), token


class ClaudePromptAdapter:
    """Adapter do Claude Code: `ask` com o pedido e input atualizado com a prova."""

    name = "claude-code"

    def prompt(self, tool_input: dict[str, Any], cwd: Path) -> tuple[str, str] | None:
        return approval_prompt(tool_input, cwd)


def pre_tool_use(data: dict[str, Any], adapter: PromptAdapter | None = None) -> dict[str, object]:
    name = data.get("tool_name", "")
    if not isinstance(name, str) or name not in CATALOG:
        return decision("")
    result = decision(name)
    raw_input = data.get("tool_input")
    tool_input = cast(dict[str, Any], raw_input) if isinstance(raw_input, dict) else {}
    if PROOF_FIELD in tool_input:
        return _output("deny", RESERVED_REASON)
    tool = name.removeprefix(PREFIX)
    if tool in EFFECT_TOOLS and "approval_id" in tool_input:
        cwd = data.get("cwd")
        try:
            prompt = (adapter or ClaudePromptAdapter()).prompt(
                tool_input, Path(cwd) if isinstance(cwd, str) else Path.cwd()
            )
        except Exception:
            prompt = None
        if prompt is not None:
            text, token = prompt
            return _output("ask", text, updatedInput={**tool_input, PROOF_FIELD: token})
    return result


def notification(data: dict[str, Any]) -> dict[str, object]:
    """Prompt de permissão do executor: avisa no desktop quando há pedido pendente na run."""
    import asyncio
    import uuid

    from lastro.approvals.notifications import DesktopNotifier, NotificationRouter
    from lastro.config.loader import load_config_file
    from lastro.history import NotificationChannel, open_history

    cwd = data.get("cwd")
    try:
        loaded = load_config_file(None, Path(cwd) if isinstance(cwd, str) else Path.cwd())
        history = open_history(loaded)
        run = history.get_active_run()
        pending = history.pending_approval(run.id) if run is not None else None
        if pending is None:
            return {}
        desktop = DesktopNotifier()
        desktop.channel = NotificationChannel.EXECUTOR_HOOK
        router = NotificationRouter(history, [desktop], attempt_key="hook-" + uuid.uuid4().hex)
        asyncio.run(router.notify(pending))
    except Exception:
        return {}
    return {}


def handle(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        return decision("")
    event = cast(dict[str, Any], data)
    if event.get("hook_event_name") == "Notification":
        return notification(event)
    return pre_tool_use(event)


def main() -> None:
    try:
        result = handle(json.loads(sys.stdin.read(65537)))
    except (ValueError, TypeError):
        result = decision("")
    print(json.dumps(result))
