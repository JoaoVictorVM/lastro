from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.integration.sources_support import CADASTRO, make_service, write_docs

from lastro.config.loader import ProjectContext
from lastro.history import (
    ArtifactType,
    EvidencePurpose,
    EvidenceReference,
    ExpectedSource,
    FailureDomain,
    FindingClassification,
    FindingDraft,
    HistoryStore,
    ReportingError,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.reporting.builder import ReportBuilder
from lastro.reporting.models import FindingInput
from lastro.reporting.public import export_public
from lastro.reproduction.brief import build_brief
from lastro.reproduction.coordinator import ReproductionCoordinator

pytest.importorskip("sqlite_vec")
pytest.importorskip("markdown_it")

FLOW = "employee.create"


def draft(
    source_ref: str, evidence: str, source: ExpectedSource = ExpectedSource.DOCUMENTATION
) -> FindingDraft:
    return FindingDraft(
        title="Email vazio é recusado",
        flow="Cadastro de funcionário",
        preconditions=(),
        steps=("Abrir novo funcionário", "Deixar email vazio", "Salvar"),
        expected="O cadastro deveria aceitar email vazio conforme documentação.",
        expected_source=source,
        expected_source_ref=source_ref,
        observed="A interface exige o email.",
        evidence=(EvidenceReference(evidence, EvidencePurpose.STATE),),
        classification=FindingClassification.SUSPECTED,
        limitations=(),
        failure_domain=FailureDomain.SYSTEM,
    )


def setup(
    history_context: ProjectContext, workdir: Path
) -> tuple[HistoryStore, str, str, str, Path]:
    history = open_history(history_context)
    docs = write_docs(workdir / "docs", {"cadastro.md": CADASTRO})
    make_service(history).add(docs, "work")
    run_id = history.create_run(history_context, TextMission("Explore o cadastro")).record.id
    outcome = make_service(history).query(run_id=run_id, flow=FLOW, text="email opcional", limit=1)
    shot = history.register_artifact(
        run_id, "evidence/email.png", ArtifactType.SCREENSHOT, content=b"png"
    )
    return history, run_id, outcome.passages[0].reference_id, shot.id, docs


def test_finding_requires_delivered_citation(
    history_context: ProjectContext, workdir: Path
) -> None:
    history, run_id, reference, shot, _ = setup(history_context, workdir)
    base: dict[str, Any] = {
        "title": "Email vazio é recusado",
        "flow": "Cadastro",
        "steps": ["Salvar"],
        "expected": {"text": "Aceitar email vazio", "source": "documentation"},
        "observed": "Recusado",
        "classification": "suspected",
        "failure_domain": "system",
    }
    with pytest.raises(ValidationError):
        FindingInput.model_validate(base)
    with pytest.raises(ValidationError):
        FindingInput.model_validate(
            {**base, "expected": {**base["expected"], "source_ref": "[docs/cadastro.md § X]"}}
        )

    with pytest.raises(ReportingError) as invented:
        history.report_finding(run_id, draft("sref_" + "0" * 32, shot))
    accepted = history.report_finding(run_id, draft(reference, shot))

    assert invented.value.code == "invalid_reference"
    assert accepted.finding.expected_source_ref == reference


def test_report_survives_sync_and_deletion(history_context: ProjectContext, workdir: Path) -> None:
    history, run_id, reference, shot, docs = setup(history_context, workdir)
    history.report_finding(run_id, draft(reference, shot))
    (docs / "cadastro.md").unlink()
    write_docs(docs, {"outro.md": "# Outro\n\nnada"})
    make_service(history).sync()
    history.finish_run(run_id, RunStatus.COMPLETED)

    outcome = ReportBuilder(history).build_final(run_id)

    assert outcome.report_path is not None
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "**Sources used**" in report
    # Markdown escapado: renderiza como [docs/cadastro.md § Campos obrigatórios].
    assert r"\[docs/cadastro.md § Campos obrigatórios\]" in report
    assert "O email é opcional no cadastro de funcionário." in report
    (finding,) = outcome.data.suspected
    assert finding.sources[0].file_sha256 and not finding.sources[0].legacy
    issue = ReportBuilder(history).render_issue(outcome.data, finding)
    assert r"Cited documentation: \[docs/cadastro.md § Campos obrigatórios\]" in issue


def test_reproduction_inherits_only_authorized_refs(
    history_context: ProjectContext, workdir: Path
) -> None:
    history, run_id, reference, shot, _ = setup(history_context, workdir)
    cited = history.report_finding(run_id, draft(reference, shot)).finding
    legacy = history.report_finding(
        run_id, draft("[docs/x.md § Y]", shot, ExpectedSource.USER_ANSWER)
    ).finding
    coordinator = ReproductionCoordinator.__new__(ReproductionCoordinator)
    coordinator.history = history

    inherited = coordinator._documentation(cited)  # pyright: ignore[reportPrivateUsage]
    nothing = coordinator._documentation(legacy)  # pyright: ignore[reportPrivateUsage]
    brief = build_brief(cited, None, inherited)

    assert inherited == (
        (
            "[docs/cadastro.md § Campos obrigatórios]",
            inherited[0][1],
        ),
    )
    assert "O email é opcional" in inherited[0][1]
    assert nothing == ()
    assert "declared expectation, not evidence" in brief.mission_text()
    assert "A interface exige o email" not in brief.mission_text()


def test_public_export_omits_private_paths(history_context: ProjectContext, workdir: Path) -> None:
    history, run_id, reference, shot, docs = setup(history_context, workdir)
    history.report_finding(run_id, draft(reference, shot))
    history.finish_run(run_id, RunStatus.COMPLETED)
    ReportBuilder(history).build_final(run_id)

    exported = export_public(history, run_id).path.read_text(encoding="utf-8")

    assert "Declared documentation passage" in exported
    for private in (
        "cadastro.md",
        "Campos obrigatórios",
        "O email é opcional",
        reference,
        str(docs),
    ):
        assert private not in exported


def test_legacy_document_reference_remains_readable(
    history_context: ProjectContext, workdir: Path
) -> None:
    history, run_id, _, shot, _ = setup(history_context, workdir)
    created = history.report_finding(
        run_id, draft("[docs/x.md § Y]", shot, ExpectedSource.USER_ANSWER)
    )
    # Achado gravado antes desta entrega: documentação com referência livre.
    history._database.write(  # pyright: ignore[reportPrivateUsage]
        lambda conn: conn.execute(
            "UPDATE findings SET payload_json = json_set(payload_json, '$.expected_source', "
            "'documentation') WHERE id = ?",
            (created.finding.id,),
        )
    )
    history.finish_run(run_id, RunStatus.COMPLETED)

    outcome = ReportBuilder(history).build_final(run_id)

    assert outcome.report_path is not None
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "legacy documentation reference, not verified" in report
    assert any("legacy or unknown reference" in item for item in outcome.data.limitations)
