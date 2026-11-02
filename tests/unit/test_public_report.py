import re
from typing import Any

import pytest

from lastro.browser.events import record_call
from lastro.browser.policy import Decision
from lastro.config.loader import ProjectContext
from lastro.history import (
    ArtifactType,
    CreatedRecordInput,
    EventType,
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
from lastro.oracles.models import OracleOccurrence
from lastro.reporting import ReportBuilder
from lastro.reporting.public import export_public, redact_public

UUID = "3f2b8c1e-9a4d-4e7f-8b6a-1c2d3e4f5a6b"
RECORD = "emp-90210"


def sentinel_run(context: ProjectContext) -> tuple[HistoryStore, str, dict[str, str]]:
    """Run com valores sentinela em todos os campos textuais que chegam ao relatório."""
    store = open_history(context)
    tenant = context.profile.tenant_id
    url = context.profile.target_url
    sentinels = f"{tenant} {url}/employees/{RECORD} LASTRO-ab12-x {UUID} ![shot](evidence/x.png)"
    run_id = store.create_run(context, TextMission(f"Explore {sentinels}")).record.id
    shot = store.register_artifact(
        run_id, "evidence/screen.png", ArtifactType.SCREENSHOT, content=b"png"
    )
    record_call(store, run_id, "create-1", "submit", {}, Decision("allow", "", ""))
    created = store.record_created(
        CreatedRecordInput(
            run_id=run_id,
            action_id="create-1",
            entity="employee",
            system_identifier=RECORD,
            tenant=tenant,
            expected_tenant=tenant,
            detail_path=f"/employees/{RECORD}",
            cleanup_instructions=f"Remover {RECORD} em {url}",
        )
    )
    action = store.append_event(
        run_id, EventType.TOOL_CALL, tool_name="submit", correlation_id="save-1"
    )
    store.record_oracle_signal(
        run_id,
        OracleOccurrence.model_validate(
            {
                "occurrence_id": "o1",
                "signal_type": "http_5xx",
                "url": f"{url}/api/employees/90210",
                "url_path": "/api/employees/90210",
                "http_method": "PUT",
                "http_status": 500,
                "message": f"Failed for {tenant} at {url} id {UUID}",
                "timestamp": "2026-11-01T15:00:04.000000Z",
                "action_id": "save-1",
                "action_sequence": action.sequence,
            }
        ),
    )
    finding: dict[str, Any] = {
        "title": f"Erro em {tenant} {RECORD}",
        "flow": f"Cadastro {url}",
        "preconditions": (sentinels,),
        "steps": (f"Abrir {url}/employees/{RECORD}", sentinels),
        "expected": sentinels,
        "expected_source": ExpectedSource.DOCUMENTATION,
        "expected_source_ref": f"{url}/docs",
        "observed": f"{sentinels} [link]({url}/x)",
        "evidence": (EvidenceReference(shot.id, EvidencePurpose.STATE),),
        "classification": FindingClassification.SUSPECTED,
        "limitations": (sentinels,),
        "failure_domain": FailureDomain.SYSTEM,
    }
    store.report_finding(run_id, FindingDraft(**finding))
    store.report_finding(
        run_id, FindingDraft(**{**finding, "failure_domain": FailureDomain.AGENT, "title": "A"})
    )
    store.finish_run(run_id, RunStatus.COMPLETED, f"done for {tenant}")
    ReportBuilder(store).build_final(run_id)
    values = {
        "tenant": tenant,
        "url": url,
        "host": url.split("://")[1],
        "record": RECORD,
        "tracking": created.id,
        "artifact": shot.id,
        "uuid": UUID,
    }
    return store, run_id, values


def test_private_projection_removes_all_sentinels(history_context: ProjectContext) -> None:
    store, run_id, values = sentinel_run(history_context)

    exported = export_public(store, run_id)

    text = exported.path.read_text(encoding="utf-8")
    assert not exported.public_profile
    lowered = text.lower()
    for name, value in values.items():
        assert value.lower() not in lowered, name
    assert "http://" not in text and "https://" not in text
    assert not re.search(r"(?i)lastro-", text)
    assert "![" not in text and "](" not in text
    assert "evidence/" not in text
    assert "90210" not in text
    for title in ("## Summary", "## Oracle signals", "## Agent and tool failures"):
        assert title in text
    assert "1 record(s) created" in text
    registered = {a.relative_path: a for a in store.list_artifacts(run_id)}
    assert registered["report.public.md"].artifact_type == ArtifactType.REPORT


def test_public_export_respects_profile_visibility(history_context: ProjectContext) -> None:
    history_context.profile.public = True
    store, run_id, values = sentinel_run(history_context)

    exported = export_public(store, run_id)

    assert exported.public_profile
    original = (store.run_dir(run_id) / "report.md").read_text(encoding="utf-8")
    assert exported.path.read_text(encoding="utf-8") == original
    assert values["tenant"] in original


def test_public_export_is_idempotent_and_requires_report(history_context: ProjectContext) -> None:
    store, run_id, _ = sentinel_run(history_context)
    first = export_public(store, run_id)
    second = export_public(store, run_id)
    assert first.snapshot == second.snapshot

    fresh = store.create_run(history_context, TextMission("Sem relatório")).record.id
    store.finish_run(fresh, RunStatus.COMPLETED)
    with pytest.raises(ReportingError) as info:
        export_public(store, fresh)
    assert info.value.code == "report_not_ready"


def test_public_export_refuses_tampered_report(history_context: ProjectContext) -> None:
    store, run_id, _ = sentinel_run(history_context)
    report = store.run_dir(run_id) / "report.md"
    report.write_text(report.read_text(encoding="utf-8") + "edited", encoding="utf-8")

    with pytest.raises(ReportingError) as info:
        export_public(store, run_id)
    assert info.value.code == "report_changed"


def test_redaction_rules() -> None:
    text = redact_public(
        "see ![a](x.png) and [doc](https://d.example/p) www.site.test LASTRO-77 "
        f"{UUID} art_{'a' * 32} created_{'b' * 32} evidence/x.png Acme",
        ("acme",),
    )
    assert text == (
        "see [image removed] and doc [url removed] [redacted] [redacted] [redacted] "
        "[redacted] [evidence removed] [redacted]"
    )
