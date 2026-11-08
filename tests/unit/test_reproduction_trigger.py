import shutil
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.config.reproduction import ReproductionSettings
from lastro.history import (
    REPRODUCTION_CHILD_BLOCK,
    ArtifactType,
    EvidencePurpose,
    EvidenceReference,
    ExpectedSource,
    FailureDomain,
    FindingClassification,
    FindingDraft,
    HistoryStore,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.reproduction import trigger
from lastro.reproduction.coordinator import ReproductionCoordinator
from lastro.reproduction.launchers import ClaudeReproducer, ExecutorUnavailable


def suspected(store: HistoryStore, context: ProjectContext, status: RunStatus) -> str:
    run_id = store.create_run(context, TextMission("Explore")).record.id
    artifact = store.register_artifact(
        run_id, "evidence/a.png", ArtifactType.SCREENSHOT, content=b"a"
    )
    store.report_finding(
        run_id,
        FindingDraft(
            title="CPF duplicado",
            flow="Cadastro",
            preconditions=(),
            steps=("Salvar",),
            expected="Recusa",
            expected_source=ExpectedSource.HEURISTIC,
            expected_source_ref=None,
            observed="Aceito",
            evidence=(EvidenceReference(artifact.id, EvidencePurpose.STATE),),
            classification=FindingClassification.SUSPECTED,
            limitations=(),
            failure_domain=FailureDomain.SYSTEM,
        ),
    )
    store.finish_run(run_id, status)
    return run_id


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.COMPLETED, True),
        (RunStatus.CANCELLED, False),
        (RunStatus.INTERRUPTED, False),
        (RunStatus.LIMIT_REACHED, False),
    ],
)
def test_only_completed_runs_trigger(
    history_context: ProjectContext, status: RunStatus, expected: bool
) -> None:
    store = open_history(history_context)
    run_id = suspected(store, history_context, status)
    assert trigger.should_reproduce(history_context, store.get_run(run_id)) is expected


def test_auto_false_and_child_runs_never_trigger(history_context: ProjectContext) -> None:
    history_context.profile.reproduction = ReproductionSettings(auto=False)
    store = open_history(history_context)
    run_id = suspected(store, history_context, RunStatus.COMPLETED)
    assert not trigger.should_reproduce(history_context, store.get_run(run_id))
    record = store.get_run(run_id)
    child = type(record)(
        **{**record.__dict__, "config_snapshot": {REPRODUCTION_CHILD_BLOCK: {}, "reproduction": {}}}
    )
    assert not trigger.should_reproduce(history_context, child)


def test_auto_reproduce_runs_the_round(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = suspected(store, history_context, RunStatus.COMPLETED)
    lines: list[str] = []
    coordinator = ReproductionCoordinator(
        history_context, store, launchers={}, write=lines.append, build_report=False
    )
    outcome = trigger.auto_reproduce(
        history_context, store, run_id, write=lines.append, coordinator=coordinator
    )
    assert outcome is not None and len(outcome.findings) == 1
    assert lines[0] == "Reproducing 1 suspected finding…"


def test_failure_to_start_keeps_findings_and_points_to_command(
    history_context: ProjectContext,
) -> None:
    store = open_history(history_context)
    run_id = suspected(store, history_context, RunStatus.COMPLETED)
    blocker = store.create_run(history_context, TextMission("Outra")).record.id
    lines: list[str] = []
    assert trigger.auto_reproduce(history_context, store, run_id, write=lines.append) is None
    assert lines[-1] == f"Reproduction pending: run 'lastro run reproduce {run_id}'."
    assert store.list_findings(run_id)[0].classification == FindingClassification.SUSPECTED
    assert blocker


def test_owner_detaches_only_for_claude_code(
    history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(trigger, "SPAWN", lambda project, run_id: calls.append(run_id))
    store = open_history(history_context)
    run_id = suspected(store, history_context, RunStatus.COMPLETED)
    record = store.get_run(run_id)
    assert trigger.detach_after_owner(history_context, record)
    native = type(record)(**{**record.__dict__, "executor_name": "native"})
    assert not trigger.detach_after_owner(history_context, native)
    assert calls == [run_id]


def test_claude_adapter_requires_binary(
    history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ExecutorUnavailable, match="claude-code unavailable"):
        ClaudeReproducer(lambda line: None).prepare(history_context)


def test_claude_adapter_runs_headless_with_project_mcp(
    history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = history_context.loaded.project_dir
    (project_dir / ".mcp.json").write_text("{}", encoding="utf-8")
    (project_dir / ".claude").mkdir(exist_ok=True)
    (project_dir / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/claude")
    seen: dict[str, Any] = {}
    store = open_history(history_context)

    def fake_run(arguments: list[str], **options: Any) -> Any:
        seen.update(arguments=arguments, **options)
        store.finish_run(run_id, RunStatus.COMPLETED)
        return type("Completed", (), {"returncode": 0})()

    adapter = ClaudeReproducer(lambda line: None, run=fake_run)
    prepared = adapter.prepare(history_context)
    created = store.create_run(history_context, TextMission("Brief"))
    run_id = created.record.id
    outcome = adapter.launch(history_context, store, created, prepared)

    assert outcome.status == RunStatus.COMPLETED and outcome.available
    arguments = seen["arguments"]
    assert arguments[:2] == ["/bin/claude", "-p"]
    assert run_id in arguments[2]
    assert arguments[3:] == [
        "--output-format",
        "json",
        "--mcp-config",
        ".mcp.json",
        "--strict-mcp-config",
    ]
    assert seen["cwd"] == project_dir
    assert seen["timeout"] == history_context.profile.run_limits.max_minutes * 60 + 120
