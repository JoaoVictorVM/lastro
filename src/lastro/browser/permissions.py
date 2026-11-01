"""Hook do executor: somente o catálogo Lastro pode produzir efeitos na missão."""

import json
import sys
from typing import cast

from lastro.browser.tools import TOOLS


def decision(name: str) -> dict[str, object]:
    allowed = name in {"mcp__lastro__" + tool for tool in TOOLS}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allowed else "deny",
            "permissionDecisionReason": "Mission tools are restricted to the Lastro catalog.",
        }
    }


def main() -> None:
    try:
        data = json.loads(sys.stdin.read(65537))
        name = cast(dict[str, object], data).get("tool_name", "") if isinstance(data, dict) else ""
        result = decision(name if isinstance(name, str) else "")
    except (ValueError, TypeError):
        result = decision("")
    print(json.dumps(result))
