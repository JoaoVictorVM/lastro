import json
from typing import Any

from lastro.config.loader import ProjectContext
from lastro.evaluation.metrics import Resolved, measure_attempt
from lastro.evaluation.runner import collect_lastro
from lastro.history import (
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


def explored(history_context: ProjectContext) -> tuple[HistoryStore, str, str]:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("Editar telefone")).record.id
    shot = history.register_artifact(
        run_id, "evidence/phone.png", ArtifactType.SCREENSHOT, content=b"png"
    )
    finding = history.report_finding(
        run_id,
        FindingDraft(
            title="Telefone volta ao valor anterior",
            flow="editar funcionario",
            preconditions=(),
            steps=("Editar", "Salvar", "Reabrir"),
            expected="Telefone novo",
            expected_source=ExpectedSource.USER_ANSWER,
            expected_source_ref="manual do cadastro",
            observed="Telefone antigo",
            evidence=(EvidenceReference(shot.id, EvidencePurpose.STATE),),
            classification=FindingClassification.SUSPECTED,
            limitations=(),
            failure_domain=FailureDomain.SYSTEM,
        ),
    )
    return history, run_id, finding.finding.id


def test_findings_and_criteria_come_from_structured_store(
    history_context: ProjectContext,
) -> None:
    history, run_id, finding_id = explored(history_context)
    history.finish_run(run_id, RunStatus.COMPLETED, "done")
    # O relatório Markdown não é fonte: um arquivo adulterado não muda a coleta.
    (history.run_dir(run_id) / "report.md").write_text("# 99 findings confirmed", "utf-8")
    collected = collect_lastro(history, run_id, ("persistence",))
    (finding,) = collected["findings"]
    assert finding["title"] == "Telefone volta ao valor anterior"
    assert finding["evidence_ok"] and finding["classification"] == "suspected"
    assert finding_id not in json.dumps(collected)
    assert collected["criteria_ids"] == ["persistence"]


def test_precision_uses_catalog_and_reproduction_verdict() -> None:
    def item(ref: str, classification: str) -> dict[str, Any]:
        return {
            "ref": ref,
            "classification": "suspected",
            "confirmation_source": "reproduction",
            "verdict": {
                "classification": classification,
                "reproduced_count": 2 if classification == "confirmed" else 0,
                "attempts_decisive": 2,
            },
        }

    snapshot = {
        "findings": [item("a", "confirmed"), item("b", "confirmed"), item("c", "inconclusive")],
        "criteria_ids": [],
        "access": {"intact": True},
    }
    resolved = {
        "a": Resolved("match", "D02", False),
        "b": Resolved("no_match", None, False),
        "c": Resolved("no_match", None, False),
    }
    measures = {m.metric: m for m in measure_attempt(snapshot, ["d02"], resolved)}
    assert measures["confirmed_precision"].value == 0.5
    assert measures["false_positive_rate"].value == 0.5
    assert measures["unmatched_finding_rate"].value == 2 / 3
    assert measures["reproduction_success"].value == 4 / 6


def test_corrupt_evidence_cannot_count_as_confirmed_detection(
    history_context: ProjectContext,
) -> None:
    history, run_id, _ = explored(history_context)
    history.finish_run(run_id, RunStatus.COMPLETED, "done")
    (history.run_dir(run_id) / "evidence" / "phone.png").write_bytes(b"tampered")
    (finding,) = collect_lastro(history, run_id, ())["findings"]
    assert finding["evidence_ok"] is False


def test_baseline_declared_confirmation_is_labeled() -> None:
    snapshot = {
        "findings": [
            {"ref": "a", "classification": "confirmed", "confirmation_source": "agent"},
            {"ref": "b", "classification": "suspected", "confirmation_source": "agent"},
        ],
        "criteria_ids": [],
        "access": {"intact": True},
    }
    resolved = {"a": Resolved("match", "D02", False), "b": Resolved("match", "D03", False)}
    measures = {m.metric: m for m in measure_attempt(snapshot, ["d02", "d03"], resolved)}
    assert measures["confirmed_precision"].denominator == 1
    assert measures["confirmed_detection_rate"].value == 0.5
    assert measures["detection_rate"].value == 1.0
