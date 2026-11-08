import re

import pytest
from typer.testing import CliRunner

from lastro.cli import tail_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import (
    EventRecord,
    EventType,
    HistoryStore,
    JsonValue,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.reproduction.coordinator import ReproductionCoordinator

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def verdict_event(label: str, **extra: str) -> EventRecord:
    return EventRecord(
        id=1,
        run_id="run",
        sequence=9,
        timestamp="2026-11-07T12:00:00.000000Z",
        event_type=EventType.REPRODUCTION_VERDICT,
        tool_name=None,
        arguments_sha256=None,
        arguments_preview=None,
        policy_decision=None,
        correlation_id=None,
        idempotency_key=None,
        payload=dict[str, JsonValue]({"label": label, "title": "", **extra}),
        result=None,
    )


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("confirmed (2/2)", "✓ confirmed (2/2)"),
        ("confirmed (1/3, intermittent)", "◐ confirmed (1/3, intermittent)"),
        ("not reproduced", "✗ not reproduced"),
        ("inconclusive (blocked)", "? inconclusive (blocked)"),
        ("inconclusive (budget)", "? inconclusive (budget)"),
        ("inconclusive (executor)", "? inconclusive (executor)"),
    ],
)
def test_verdict_lines_use_the_literal_forms(label: str, line: str) -> None:
    assert tail_cmd.reproduction_line(verdict_event(label)) == line


def test_preconditions_line_names_the_item() -> None:
    event = verdict_event("inconclusive (preconditions)", missing_precondition="CPF 123 existe")
    assert tail_cmd.reproduction_line(event) == "? inconclusive (preconditions): CPF 123 existe"


def test_started_line() -> None:
    event = verdict_event("")
    started = EventRecord(**{**event.__dict__, "event_type": EventType.REPRODUCTION_STARTED})
    started = EventRecord(**{**started.__dict__, "payload": {"count": 3}})
    assert tail_cmd.reproduction_line(started) == "Reproducing 3 suspected findings…"


def _suspected_run(store: HistoryStore, context: ProjectContext) -> str:
    from lastro.history import (
        ArtifactType,
        EvidencePurpose,
        EvidenceReference,
        ExpectedSource,
        FailureDomain,
        FindingClassification,
        FindingDraft,
    )

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
    store.finish_run(run_id, RunStatus.COMPLETED)
    return run_id


def test_tail_waits_for_the_round_to_finish(
    runner: CliRunner, history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_history(history_context)
    run_id = _suspected_run(store, history_context)
    polls: list[float] = []

    def round_progress(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 2:
            # A rodada automática começa depois do fim da run e grava no log da run explorada.
            ReproductionCoordinator(history_context, store, launchers={}, build_report=False).run(
                run_id
            )

    monkeypatch.setattr(tail_cmd, "sleep", round_progress)

    result = runner.invoke(app, ["tail", run_id])

    assert result.exit_code == 0, result.output
    text = ANSI.sub("", result.stdout)
    assert "Reproducing 1 suspected finding…" in text
    assert "? inconclusive (executor)  CPF duplicado" in text
    assert text.strip().splitlines()[-1].endswith("CPF duplicado")


def test_tail_does_not_wait_without_reproduction(
    runner: CliRunner, history_context: ProjectContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    store.finish_run(run_id, RunStatus.COMPLETED)
    polls: list[float] = []
    monkeypatch.setattr(tail_cmd, "sleep", polls.append)
    result = runner.invoke(app, ["tail", run_id])
    assert result.exit_code == 0, result.output
    assert polls == []
