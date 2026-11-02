import asyncio
from pathlib import Path
from typing import Any

from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.history import (
    ArtifactType,
    CriterionOutcome,
    EventType,
    HistoryStore,
    PolicyDecision,
    RunStatus,
    TextMission,
    open_history,
)

PLAN: dict[str, Any] = {
    "objectives": ["Validar o cadastro de funcionários"],
    "scope": ["Editar funcionário"],
    "criteria": [
        {"text": "O telefone permanece após recarregar", "source": "mission_explicit"},
        {"text": "E-mail inválido é rejeitado", "source": "mission_explicit"},
    ],
    "gaps": [],
    "out_of_scope": [],
    "charter": {
        "target": "employee registration",
        "resources": ["demo application"],
        "information": "persistence defects",
        "heuristics": ["persistence_after_reload"],
    },
}


def finding(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title": "Telefone não persiste após recarregar",
        "flow": "Cadastro de funcionário",
        "steps": ["Abrir funcionário", "Preencher telefone", "Salvar", "Recarregar"],
        "expected": {"text": "O telefone salvo permanece visível", "source": "heuristic"},
        "observed": "O campo aparece vazio após o reload.",
        "classification": "suspected",
        "failure_domain": "system",
    }
    data.update(overrides)
    return data


def started(context: ProjectContext) -> tuple[HistoryStore, str, BrowserTools]:
    history = open_history(context)
    run_id = history.create_run(
        context, TextMission("Explore o cadastro"), skill_bundle_version="b" * 64
    ).record.id
    runtime = BrowserTools(context, history, run_id)
    return history, run_id, runtime


def call(runtime: BrowserTools, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(runtime.call(name, arguments))


def denials(history: HistoryStore, run_id: str, tool: str) -> list[object]:
    return [
        event.payload["reason"] if isinstance(event.payload, dict) else None
        for event in history.list_events(run_id)
        if event.event_type == EventType.POLICY_DECISION
        and event.tool_name == tool
        and event.policy_decision == PolicyDecision.DENY
    ]


def test_report_finding_validation_errors_are_audited(history_context: ProjectContext) -> None:
    history, run_id, runtime = started(history_context)
    plan = call(runtime, "submit_mission_plan", PLAN)
    assert plan["exploration_allowed"]

    missing = call(runtime, "report_finding", {"title": "Só título"})
    confirmed = call(runtime, "report_finding", finding(classification="confirmed"))
    foreign = call(runtime, "report_finding", finding(evidence=[{"artifact_id": "art_other_run"}]))

    assert missing == {
        "error": "missing_finding_fields",
        "detail": "Missing finding fields: classification, expected, failure_domain, flow, "
        "observed, steps.",
    }
    assert confirmed["error"] == "classification_not_allowed"
    assert foreign == {"error": "invalid_evidence", "detail": "Evidence must belong to this run."}
    assert denials(history, run_id, "report_finding") == [
        "missing_finding_fields",
        "classification_not_allowed",
        "invalid_evidence",
    ]
    assert history.list_findings(run_id) == []


def test_report_finding_persists_structured_finding(history_context: ProjectContext) -> None:
    history, run_id, runtime = started(history_context)
    call(runtime, "submit_mission_plan", PLAN)
    shot = history.register_artifact(
        run_id, "evidence/manual.png", ArtifactType.SCREENSHOT, content=b"png"
    )

    result = call(
        runtime,
        "report_finding",
        finding(
            evidence=[{"artifact_id": shot.id, "purpose": "after"}],
            tenant="ignored",
        ),
    )
    assert result["error"] == "invalid_finding"

    result = call(runtime, "report_finding", finding(evidence=[{"artifact_id": shot.id}]))

    assert result["classification"] == "suspected"
    assert result["evidence_artifact_ids"] == [shot.id]
    assert result["created"] is True
    stored = history.get_finding(result["finding_id"])
    assert stored.tenant == history_context.profile.tenant_id
    assert stored.action_id == result["action_id"]
    submitted = [
        event
        for event in history.list_events(run_id)
        if event.event_type == EventType.FINDING_SUBMITTED
    ]
    assert len(submitted) == 1


def test_criterion_results_and_sealed_end_run(history_context: ProjectContext) -> None:
    history, run_id, runtime = started(history_context)
    criteria = call(runtime, "submit_mission_plan", PLAN)["criterion_ids"]
    shot = history.register_artifact(
        run_id, "evidence/after.png", ArtifactType.SCREENSHOT, content=b"png"
    )

    unproven = call(
        runtime,
        "record_criterion_result",
        {"criterion_id": criteria[0], "outcome": "met", "reason": "no_error_seen"},
    )
    foreign = call(
        runtime,
        "record_criterion_result",
        {"criterion_id": "criterion_x", "outcome": "not_met", "reason": "observed"},
    )
    met = call(
        runtime,
        "record_criterion_result",
        {
            "criterion_id": criteria[0],
            "outcome": "met",
            "evidence_artifact_ids": [shot.id],
            "note": "O valor persistiu após recarregar.",
            "reason": "positive_behavior_observed",
        },
    )

    assert unproven["error"] == "positive_evidence_required"
    assert foreign["error"] == "criterion_not_active"
    assert (met["revision"], met["outcome"], met["created"]) == (1, "met", True)

    ended = call(runtime, "end_run", {"outcome": "completed", "reason": "done"})
    assert ended["status"] == "completed"
    assert ended["report_status"] == "ready"
    assert ended["report_path"].endswith("report.md")
    assert ended["summary"] == [
        "Status: completed (done)",
        "Criteria met: 1/2",
        "Confirmed findings: 0",
        "Suspected findings: 0",
        "Created records pending cleanup: 0",
    ]
    report = Path(ended["report_path"]).read_text(encoding="utf-8")
    assert "Status: completed" in report
    results = {item.criterion_id: item for item in history.list_criterion_results(run_id)}
    assert results[criteria[0]].outcome == CriterionOutcome.MET
    assert (results[criteria[1]].outcome, results[criteria[1]].reason) == (
        CriterionOutcome.INCONCLUSIVE,
        "not_evaluated",
    )
    assert history.get_run(run_id).status == RunStatus.COMPLETED
    sealed_before_end = [event.event_type for event in history.list_events(run_id)][-1]
    assert sealed_before_end == EventType.RUN_ENDED


def test_video_tools_require_plan_and_active_recording(history_context: ProjectContext) -> None:
    _, _, runtime = started(history_context)
    assert call(runtime, "start_evidence_video", {"reason": "timing"})["reason"] == (
        "mission_plan_required"
    )
    call(runtime, "submit_mission_plan", PLAN)
    assert call(runtime, "stop_evidence_video", {"recording_id": "video_x"}) == {
        "error": "video_not_active"
    }
    assert runtime.session.browser is None


def test_video_evidence_records_webm_in_demo(make_auth_project: Any) -> None:
    project = make_auth_project().context()
    history = open_history(project)
    run_id = history.create_run(
        project, TextMission("Explore a lista"), skill_bundle_version="v" * 64
    ).record.id
    runtime = BrowserTools(project, history, run_id)
    base = project.profile.target_url

    async def scenario() -> dict[str, Any]:
        try:
            assert (await runtime.call("submit_mission_plan", PLAN))["exploration_allowed"]
            await runtime.call("navigate", {"url": base + "/employees"})
            started = await runtime.call("start_evidence_video", {"reason": "toast timing"})
            again = await runtime.call("start_evidence_video", {"reason": "twice"})
            assert again == {"error": "video_already_active"}
            assert (await runtime.call("observe", {}))["content"]
            stopped = await runtime.call(
                "stop_evidence_video", {"recording_id": started["recording_id"]}
            )
            after = await runtime.call("observe", {})
            assert "error" not in after
            return stopped
        finally:
            await runtime.session.close()

    stopped = asyncio.run(scenario())
    artifact = next(
        item for item in history.list_artifacts(run_id) if item.id == stopped["artifact_id"]
    )
    assert artifact.artifact_type == ArtifactType.VIDEO
    assert artifact.relative_path.endswith(".webm")
    assert stopped["stop_reason"] == "requested"
    assert 0 <= stopped["duration_seconds"] <= 60
    stored = history.run_dir(run_id) / artifact.relative_path
    assert stored.stat().st_size == artifact.size_bytes > 0
    assert not any((history.run_dir(run_id) / ".video").iterdir())
