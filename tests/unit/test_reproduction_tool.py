import asyncio
from typing import Any

from lastro.browser.tools import TOOLS, BrowserTools, catalog
from lastro.config.loader import ProjectContext
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
    ReproducerResult,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.native.mcp import expected_catalog
from lastro.reproduction import REPRODUCTION_TOOL


def call(runtime: BrowserTools, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(runtime.call(name, arguments))


def explored(context: ProjectContext) -> tuple[HistoryStore, str, str]:
    store = open_history(context)
    run_id = store.create_run(context, TextMission("Explore")).record.id
    evidence = store.register_artifact(
        run_id, "evidence/a.png", ArtifactType.SCREENSHOT, content=b"a"
    ).id
    finding = store.report_finding(
        run_id,
        FindingDraft(
            title="CPF duplicado aceito",
            flow="Cadastro",
            preconditions=("Funcionário com CPF 123 existe",),
            steps=("Abrir cadastro", "Salvar"),
            expected="Recusa",
            expected_source=ExpectedSource.HEURISTIC,
            expected_source_ref=None,
            observed="Aceito",
            evidence=(EvidenceReference(evidence, EvidencePurpose.STATE),),
            classification=FindingClassification.SUSPECTED,
            limitations=(),
            failure_domain=FailureDomain.SYSTEM,
        ),
    ).finding
    store.finish_run(run_id, RunStatus.COMPLETED)
    return store, run_id, finding.id


def child_runtime(context: ProjectContext) -> tuple[HistoryStore, str, str, BrowserTools]:
    store, run_id, finding_id = explored(context)
    round_id = store.start_reproduction_round(
        run_id, mode="same", explorer="claude-code", verifier="claude-code", finding_ids=[]
    ).id
    child = store.create_run(
        context.for_reproduction(),
        TextMission("Brief"),
        executor_snapshot={REPRODUCTION_CHILD_BLOCK: {"brief_sha256": "0" * 64}},
        reproduction_of=(round_id, finding_id, 1),
    ).record.id
    return store, run_id, child, BrowserTools(context, store, child)


def test_tool_absent_outside_reproduction_run(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explore")).record.id
    runtime = BrowserTools(history_context, store, run_id)
    assert not runtime.reproduction
    assert REPRODUCTION_TOOL not in TOOLS and REPRODUCTION_TOOL in catalog(reproduction=True)
    assert REPRODUCTION_TOOL not in [tool.name for tool in expected_catalog()]
    result = call(runtime, REPRODUCTION_TOOL, {"result": "not_reproduced"})
    assert result["error"] == "not_a_reproduction_run"


def test_already_reported_and_contract(history_context: ProjectContext) -> None:
    store, run_id, child, runtime = child_runtime(history_context)
    assert runtime.reproduction
    foreign = store.register_artifact(
        run_id, "evidence/b.png", ArtifactType.SCREENSHOT, content=b"b"
    ).id
    rejected = call(
        runtime, REPRODUCTION_TOOL, {"result": "reproduced", "evidence_artifact_ids": [foreign]}
    )
    assert rejected["error"] == "invalid_evidence"
    index = call(
        runtime, REPRODUCTION_TOOL, {"result": "precondition_missing", "precondition_index": 4}
    )
    assert index["error"] == "invalid_precondition_index"
    unknown = call(runtime, REPRODUCTION_TOOL, {"result": "maybe"})
    assert "error" in unknown

    accepted = call(runtime, REPRODUCTION_TOOL, {"result": "not_reproduced", "note": "ok"})
    assert accepted == {"recorded": True, "attempt_ordinal": 1, "action_id": accepted["action_id"]}
    assert set(accepted) == {"recorded", "attempt_ordinal", "action_id"}
    again = call(runtime, REPRODUCTION_TOOL, {"result": "reproduced"})
    assert again["error"] == "already_reported"
    report = store.reproducer_report(child)
    assert report is not None and report.result == ReproducerResult.NOT_REPRODUCED


def test_child_run_has_reduced_limits_and_no_memory(history_context: ProjectContext) -> None:
    store, _, child, runtime = child_runtime(history_context)
    limits = store.get_run(child).config_snapshot["run_limits"]
    assert isinstance(limits, dict)
    assert (limits["max_minutes"], limits["max_actions"]) == (10, 40)
    runtime.active()
    memory = call(runtime, "knowledge_query", {"flow_key": "cadastro"})
    assert memory["error"] == "knowledge_unavailable"
    reproduction_catalog = [tool.name for tool in expected_catalog(reproduction=True)]
    assert reproduction_catalog[-1] == REPRODUCTION_TOOL
