import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DEMO_TEXT = (WORKFLOWS / "autonomous-demo.yml").read_text(encoding="utf-8")
EXAMPLE_TEXT = (WORKFLOWS / "autonomous-post-deploy.example.yml").read_text(encoding="utf-8")
DEMO: dict[Any, Any] = yaml.safe_load(DEMO_TEXT)
EXAMPLE: dict[Any, Any] = yaml.safe_load(EXAMPLE_TEXT)
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML follows YAML 1.1, where the bare key `on` is read as the boolean True.
    return workflow["on"] if "on" in workflow else workflow[True]


def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def test_demo_runs_on_main_push() -> None:
    assert triggers(DEMO) == {"push": {"branches": ["main"]}}
    commands = "\n".join(step["run"] for step in steps(DEMO) if "run" in step)
    assert "uv sync --locked --extra native" in commands
    assert "tests/integration/test_autonomous_demo.py" in commands
    assert "uv lock" not in commands and "--upgrade" not in commands


def test_permissions_are_read_only() -> None:
    for workflow in (DEMO, EXAMPLE):
        assert workflow["permissions"] == {"contents": "read"}
        for job in workflow["jobs"].values():
            assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}
    for text in (DEMO_TEXT, EXAMPLE_TEXT):
        assert "write" not in re.sub(r"#.*", "", text).replace("persist-credentials", "")


def test_actions_are_sha_pinned() -> None:
    for workflow in (DEMO, EXAMPLE):
        for step in steps(workflow):
            if "uses" in step:
                assert PINNED_ACTION.match(step["uses"]), step["uses"]
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False


def test_report_is_uploaded() -> None:
    (upload,) = [s for s in steps(DEMO) if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"
    (run,) = [s for s in steps(DEMO) if "test_autonomous_demo.py" in s.get("run", "")]
    assert run["env"]["LASTRO_AUTONOMOUS_ARTIFACT_DIR"] == upload["with"]["path"]


def test_demo_uses_no_real_secrets() -> None:
    assert "secrets." not in DEMO_TEXT
    assert "${{ secrets" not in DEMO_TEXT
    for step in steps(DEMO):
        for name in step.get("env", {}):
            assert not re.search(r"KEY|TOKEN|PASSWORD|SECRET|WEBHOOK", name), name


def test_workflow_run_filters_success() -> None:
    events = triggers(EXAMPLE)
    assert events["workflow_run"]["types"] == ["completed"]
    assert events["workflow_run"]["workflows"]
    (job,) = EXAMPLE["jobs"].values()
    condition = job["if"]
    assert "github.event.workflow_run.conclusion == 'success'" in condition
    assert "github.event.deployment_status.state == 'success'" in condition
    commands = "\n".join(step["run"] for step in job["steps"] if "run" in step)
    assert '--event-file "$GITHUB_EVENT_PATH"' in commands
    assert "--mission-file missions/" in commands
    assert "download-artifact" not in EXAMPLE_TEXT
    assert "workflow_run.head_sha" not in EXAMPLE_TEXT
