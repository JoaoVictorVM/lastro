import json
import time
from dataclasses import replace
from typing import Any

import pytest
from jinja2 import DictLoader

from lastro.config.loader import ProjectContext
from lastro.history import (
    ArtifactType,
    CriterionOutcome,
    CriterionResultDraft,
    EventType,
    EvidencePurpose,
    EvidenceReference,
    ExpectedSource,
    FailureDomain,
    FindingClassification,
    FindingDraft,
    HistoryStore,
    ReportConflictError,
    ReportKind,
    ReproductionOutcome,
    RunStatus,
    SnapshotStatus,
    TextMission,
    open_history,
)
from lastro.mission import MissionPlanInput
from lastro.reporting import ReportBuilder

SECTIONS = (
    "## Summary",
    "## Mission and charter",
    "## Environment and versions",
    "## Criteria results",
    "## Confirmed findings",
    "## Suspected and inconclusive findings",
    "## Oracle signals",
    "## Agent and tool failures",
    "## Blocks and pending items",
    "## Created records and cleanup",
    "## Memory outcome",
    "## Limitations",
)
PLAN = {
    "objectives": ["Validar o cadastro"],
    "scope": ["Editar funcionário"],
    "criteria": [
        {"text": "O telefone permanece após recarregar", "source": "mission_explicit"},
        {"text": "E-mail inválido é rejeitado", "source": "mission_explicit"},
    ],
    "gaps": [{"description": "Formato de CPF não informado", "blocking": False}],
    "out_of_scope": ["Excluir"],
    "charter": {
        "target": "employee registration",
        "resources": ["demo application"],
        "information": "persistence defects",
        "heuristics": ["persistence_after_reload"],
    },
}


def draft(title: str, evidence: tuple[str, ...] = (), **overrides: Any) -> FindingDraft:
    data: dict[str, Any] = {
        "title": title,
        "flow": "Cadastro de funcionário",
        "preconditions": ("Usuário autenticado",),
        "steps": ("Abrir funcionário", "Salvar", "Recarregar"),
        "expected": "O telefone permanece",
        "expected_source": ExpectedSource.HEURISTIC,
        "expected_source_ref": None,
        "observed": "O campo fica vazio",
        "evidence": tuple(EvidenceReference(item, EvidencePurpose.STATE) for item in evidence),
        "classification": FindingClassification.SUSPECTED,
        "limitations": (),
        "failure_domain": FailureDomain.SYSTEM,
    }
    data.update(overrides)
    return FindingDraft(**data)


def finished_run(context: ProjectContext) -> tuple[HistoryStore, str, dict[str, str]]:
    """Run encerrada com plano, critério met, achados de cada domínio e um confirmado."""
    store = open_history(context)
    run_id = store.create_run(context, TextMission("Explore o cadastro")).record.id
    criteria = store.submit_mission_plan(run_id, MissionPlanInput.model_validate(PLAN)).plan
    shot = store.register_artifact(
        run_id, "evidence/after.png", ArtifactType.SCREENSHOT, content=b"png-after"
    ).id
    store.record_criterion_result(
        run_id,
        CriterionResultDraft(
            criteria.criteria[0].id,
            CriterionOutcome.MET,
            (shot,),
            "O valor persistiu após recarregar.",
            "positive_behavior_observed",
        ),
    )
    confirmed = store.report_finding(run_id, draft("Telefone some", (shot,))).finding.id
    suspected = store.report_finding(run_id, draft("E-mail aceito sem domínio")).finding.id
    harness = store.report_finding(
        run_id,
        draft("Seletor ambíguo no formulário", failure_domain=FailureDomain.TOOL),
    ).finding.id
    store.seal_criterion_results(run_id)
    store.finish_run(run_id, RunStatus.COMPLETED, "done")
    child = store.create_run(context, TextMission("Reproduzir")).record.id
    store.record_reproduction_attempt(confirmed, child, ReproductionOutcome.REPRODUCED, (), "codex")
    store.promote_finding(confirmed)
    store.finish_run(child, RunStatus.COMPLETED)
    return (
        store,
        run_id,
        {"confirmed": confirmed, "suspected": suspected, "harness": harness, "shot": shot},
    )


def section(text: str, title: str) -> str:
    start = text.index(title)
    following = [text.index(item) for item in SECTIONS if text.index(item) > start]
    return text[start : min(following, default=len(text))]


def test_report_renders_every_section_in_stable_order(history_context: ProjectContext) -> None:
    store, run_id, ids = finished_run(history_context)

    outcome = ReportBuilder(store).build_final(run_id)

    assert outcome.status == SnapshotStatus.READY
    assert outcome.report_path == store.run_dir(run_id) / "report.md"
    assert outcome.report_path is not None
    text = outcome.report_path.read_text(encoding="utf-8")
    positions = [text.index(title) for title in SECTIONS]
    assert positions == sorted(positions)
    assert ids["confirmed"] in section(text, "## Confirmed findings")
    assert "evidence/after.png" in section(text, "## Confirmed findings")
    assert ids["suspected"] in section(text, "## Suspected and inconclusive findings")
    assert ids["harness"] in section(text, "## Agent and tool failures")
    for title in ("## Confirmed findings", "## Suspected and inconclusive findings"):
        assert ids["harness"] not in section(text, title)
    assert "not\\_evaluated" in section(text, "## Criteria results")
    assert "No oracle signals." in text
    assert "No records were created." in text
    assert "Outcome: `nothing_new`" in section(text, "## Memory outcome")
    assert outcome.summary_lines == (
        "Status: completed (done)",
        "Criteria met: 1/2",
        "Confirmed findings: 1",
        "Suspected findings: 1",
        "Created records pending cleanup: 0",
    )
    report = next(a for a in store.list_artifacts(run_id) if a.relative_path == "report.md")
    assert report.artifact_type == ArtifactType.REPORT


def test_confirmed_finding_produces_issue_draft(history_context: ProjectContext) -> None:
    store, run_id, ids = finished_run(history_context)

    outcome = ReportBuilder(store).build_final(run_id)

    assert outcome.issue_paths == (store.run_dir(run_id) / "issues" / f"{ids['confirmed']}.md",)
    issue = outcome.issue_paths[0].read_text(encoding="utf-8")
    for heading in ("## Environment", "## Steps to reproduce", "## Expected", "## Observed"):
        assert heading in issue
    assert "1. Abrir funcionário" in issue
    assert "`evidence/after.png`" in issue
    assert "never published automatically" in issue
    registered = {a.relative_path: a for a in store.list_artifacts(run_id)}
    assert f"issues/{ids['confirmed']}.md" in registered
    assert not any(path.startswith("issues/") and ids["suspected"] in path for path in registered)


def test_builder_is_idempotent(history_context: ProjectContext) -> None:
    store, run_id, ids = finished_run(history_context)
    first = ReportBuilder(store).build_final(run_id)
    second = ReportBuilder(store).build_final(run_id)

    assert second.snapshot == first.snapshot
    assert len([a for a in store.list_artifacts(run_id) if a.relative_path == "report.md"]) == 1
    child = store.create_run(history_context, TextMission("De novo")).record.id
    store.record_reproduction_attempt(
        ids["suspected"], child, ReproductionOutcome.REPRODUCED, (), "x"
    )
    before = first.report_path.read_bytes() if first.report_path else b""

    with pytest.raises(ReportConflictError):
        ReportBuilder(store).build_final(run_id)
    assert first.report_path is not None and first.report_path.read_bytes() == before


def test_final_report_requires_finished_run(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    with pytest.raises(Exception, match="finished run"):
        ReportBuilder(store).build_final(run_id)

    partial = ReportBuilder(store).build_partial(run_id, "diagnostic")
    assert partial.report_path == store.run_dir(run_id) / "report.partial.md"
    assert partial.report_path is not None
    assert "Partial report" in partial.report_path.read_text(encoding="utf-8")
    assert store.get_run(run_id).status == RunStatus.RUNNING


def test_template_failure_writes_json_fallback(history_context: ProjectContext) -> None:
    store, run_id, ids = finished_run(history_context)
    broken = DictLoader({"report.md.j2": "{{ report.unknown_field }}", "issue.md.j2": ""})
    events_before = store.list_events(run_id)

    outcome = ReportBuilder(store, loader=broken).build_final(run_id)

    assert outcome.status == SnapshotStatus.FALLBACK
    assert outcome.report_path is None
    assert outcome.fallback_path == store.run_dir(run_id) / "report.json"
    assert outcome.fallback_path is not None
    raw = json.loads(outcome.fallback_path.read_text(encoding="utf-8"))
    assert "UndefinedError" in raw["error"]
    assert raw["report"]["confirmed"][0]["id"] == ids["confirmed"]
    assert outcome.snapshot.error_text is not None
    assert store.get_run(run_id).status == RunStatus.COMPLETED
    assert store.list_events(run_id) == events_before
    assert not (store.run_dir(run_id) / "report.md").exists()

    recovered = ReportBuilder(store).build_final(run_id)
    assert recovered.status == SnapshotStatus.READY
    assert recovered.snapshot.id == outcome.snapshot.id


def test_missing_evidence_marks_report_invalid(history_context: ProjectContext) -> None:
    store, run_id, _ = finished_run(history_context)
    (store.run_dir(run_id) / "evidence" / "after.png").unlink()

    outcome = ReportBuilder(store).build_final(run_id)

    assert outcome.snapshot.integrity_valid is False
    assert outcome.report_path is not None
    text = outcome.report_path.read_text(encoding="utf-8")
    assert "⚠ evidence missing: evidence/after.png" in text
    assert "Integrity: invalid" in text


def test_untrusted_text_cannot_inject_markdown(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    store.report_finding(
        run_id,
        draft(
            "![x](http://evil.example/p.png) | col",
            observed="## Fake section\n- [link](http://evil.example)",
        ),
    )
    store.finish_run(run_id, RunStatus.COMPLETED)

    outcome = ReportBuilder(store).build_final(run_id)
    assert outcome.report_path is not None
    text = outcome.report_path.read_text(encoding="utf-8")
    assert "![x](" not in text
    assert "\n## Fake section" not in text
    assert "> \\#\\# Fake section" in text


def test_report_is_generated_under_ten_seconds(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Carga")).record.id
    artifacts = [
        store.register_artifact(
            run_id, f"evidence/shot-{index}.png", ArtifactType.SCREENSHOT, content=b"x" * index
        ).id
        for index in range(200)
    ]
    for index in range(1000 - 100 - 1):
        store.append_event(
            run_id, EventType.TOOL_CALL, tool_name="observe", correlation_id=f"action-{index}"
        )
    for index in range(100):
        store.report_finding(run_id, draft(f"Achado {index}", (artifacts[index],)))
    store.finish_run(run_id, RunStatus.COMPLETED)
    assert len(store.list_events(run_id)) >= 1000

    started = time.monotonic()
    outcome = ReportBuilder(store).build_final(run_id)
    elapsed = time.monotonic() - started

    assert outcome.status == SnapshotStatus.READY
    assert elapsed < 10
    assert outcome.snapshot.duration_ms is not None and outcome.snapshot.duration_ms < 10_000


def test_partial_and_final_are_independent_snapshots(history_context: ProjectContext) -> None:
    store, run_id, _ = finished_run(history_context)
    final = ReportBuilder(store).build_final(run_id)
    partial = ReportBuilder(store).build_partial(run_id, "limit_reached")

    assert final.snapshot.kind == ReportKind.FINAL
    assert partial.snapshot.kind == ReportKind.PARTIAL
    assert replace(partial.data, kind="final", termination_reason=None, limitations=()) != (
        partial.data
    )
    assert len(store.list_report_snapshots(run_id)) == 2
