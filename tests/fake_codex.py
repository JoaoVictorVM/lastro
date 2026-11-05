"""Codex falso para os testes: mesma interface de processo, comportamento em behavior.json."""

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(os.environ.get("FAKE_CODEX_DIR") or sys.argv[0]).parent
BEHAVIOR = json.loads((HERE / "behavior.json").read_text(encoding="utf-8"))
ARGS = sys.argv[1:]
HELP = "--no-daemon --config --json --ignore-user-config --ignore-rules --permission-profile"


def overrides() -> list[str]:
    return [ARGS[i + 1] for i, value in enumerate(ARGS) if value == "--config"]


def pinned_args() -> list[str]:
    for item in overrides():
        if item.startswith("mcp_servers.lastro="):
            return json.loads(re.search(r"args = (\[.*?\])", item)[1])  # type: ignore[index]
    return []


def record(mode: str) -> None:
    (HERE / f"{mode}-call.json").write_text(
        json.dumps({"argv": ARGS, "env": dict(os.environ), "pid": os.getpid()}), encoding="utf-8"
    )


def end_run_through_history() -> None:
    from lastro.config.loader import load_project
    from lastro.history import RunStatus, open_history

    args = pinned_args()
    run_id = args[args.index("--run-id") + 1]
    config = Path(args[args.index("--config") + 1])
    history = open_history(load_project(config, args[args.index("--profile") + 1]))
    history.finish_run(run_id, RunStatus(BEHAVIOR["finish_status"]), "mission_completed")


def main() -> int:
    command = next((value for value in ARGS if not value.startswith("-")), "")
    if ARGS[:1] == ["--version"]:
        print(f"codex-cli {BEHAVIOR.get('version', '0.166.0')}")
        return 0
    if "--help" in ARGS:
        print(HELP)
        return 0
    if ARGS[:2] == ["login", "status"]:
        print(BEHAVIOR.get("login", "Logged in using ChatGPT"))
        return BEHAVIOR.get("login_rc", 0)
    if ARGS[:2] == ["mcp", "list"]:
        servers = [
            {"name": "lastro", "enabled": True, "transport": {"args": pinned_args()}},
            *[
                {
                    "name": name,
                    "enabled": BEHAVIOR.get("foreign_stuck", False)
                    or not any(
                        item.startswith(f"mcp_servers.{name}.enabled=") for item in overrides()
                    ),
                    "transport": {},
                }
                for name in BEHAVIOR.get("foreign", [])
            ],
        ]
        print(json.dumps(servers))
        return 0
    if ARGS[:2] == ["debug", "prompt-input"]:
        tools = [
            {"type": "namespace", "tools": [{"name": n} for n in BEHAVIOR.get("tools", ["exec"])]}
        ]
        print(json.dumps([{"type": "additional_tools", "tools": tools}]))
        return 0
    if command == "sandbox":
        return BEHAVIOR.get("sandbox_rc", 0)
    if command == "exec":
        record("exec")
        for line in BEHAVIOR.get("raw_lines", []):
            sys.stdout.write(line)
        for event in BEHAVIOR.get("events", []):
            print(json.dumps(event), flush=True)
        sys.stdout.flush()
    else:
        record("interactive")
    if BEHAVIOR.get("finish_status"):
        end_run_through_history()
    if BEHAVIOR.get("hang"):
        (HERE / "hanging").write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(120)
    sys.stderr.write(BEHAVIOR.get("stderr", ""))
    return BEHAVIOR.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
