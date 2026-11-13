"""Relatório final ou parcial gerado só a partir do banco e do registro de artefatos.

O relatório é uma projeção dos dados duráveis: as mesmas entradas produzem os mesmos bytes, e um
arquivo já registrado nunca é sobrescrito.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from jinja2 import BaseLoader, Environment, PackageLoader, StrictUndefined

from lastro.change_context.versions import version_match
from lastro.history import (
    NOT_EVALUATED_REASON,
    REPRODUCTION_CHILD_BLOCK,
    ArtifactRecord,
    ArtifactType,
    ClarificationStatus,
    CleanupStatus,
    CriterionOutcome,
    EventType,
    FailureDomain,
    Finding,
    FindingClassification,
    HistoryStore,
    PolicyDecision,
    ReportConflictError,
    ReportingError,
    ReportInputs,
    ReportKind,
    ReportSnapshot,
    ReproductionVerdict,
    RunRecord,
    RunStatus,
    SnapshotStatus,
    unidentified_warning,
)
from lastro.history.artifacts import resolve_inside
from lastro.history.errors import UnsafeArtifactPathError
from lastro.reporting.models import (
    BlockView,
    ChangeContextView,
    CreatedRecordView,
    CriterionView,
    EnvironmentView,
    EvidenceView,
    ExecutionView,
    FindingView,
    MissionView,
    ModelUsageView,
    PendingView,
    ReconciliationView,
    ReportData,
    ReproductionView,
    SignalView,
    SummaryView,
    TraceView,
    VerdictView,
)

REPORT_PATHS = {
    ReportKind.FINAL: "report.md",
    ReportKind.PARTIAL: "report.partial.md",
    ReportKind.PUBLIC: "report.public.md",
}
FALLBACK_PATHS = {ReportKind.FINAL: "report.json", ReportKind.PARTIAL: "report.partial.json"}
ISSUES_DIR = "issues"
RUN_NOT_TERMINAL = "run_not_terminal"
MEMORY_LEGACY = "not recorded: this run predates persistent memory."
MISSING_EVIDENCE = "⚠ evidence missing: {path}"

_INLINE_SPECIAL = re.compile(r"([\\`*_\[\]<>|#])")
_SPACES = re.compile(r"\s+")


def inline(value: object) -> str:
    """Texto não confiável numa linha de Markdown, sem formatação, links ou HTML ativos."""
    text = _SPACES.sub(" ", str(value)).strip()
    return _INLINE_SPECIAL.sub(r"\\\1", text)


def code(value: object) -> str:
    text = _SPACES.sub(" ", str(value)).strip().replace("`", "'")
    return f"`{text}`"


def quote(value: object) -> str:
    """Bloco multilinha como citação, para que nenhuma linha vire título ou lista."""
    lines = str(value).strip().splitlines() or [""]
    return "\n".join("> " + inline(line) if line.strip() else ">" for line in lines)


def environment(loader: BaseLoader | None = None) -> Environment:
    env = Environment(
        loader=loader or PackageLoader("lastro.reporting", "templates"),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    filters = cast(
        dict[str, Callable[[object], str]],
        env.filters,  # pyright: ignore[reportUnknownMemberType]
    )
    filters.update(inline=inline, code=code, quote=quote)
    return env


def canonical(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def report_path(kind: ReportKind, memory_revision: int, verdicts: int = 0) -> str:
    """Recuperação da memória ou novos vereditos geram um relatório novo; nada é sobrescrito."""
    base = REPORT_PATHS[kind]
    stem = base.removesuffix(".md")
    if memory_revision > 1:
        stem = f"{stem}.memory-r{memory_revision}"
    if verdicts > 0:
        stem = f"{stem}.reproduction-v{verdicts}"
    return f"{stem}.md"


def _strip_finding_defaults(item: dict[str, Any]) -> None:
    """Achados sem veredito nem tag mantêm o digest anterior à reprodução."""
    if item.get("verdict") is None:
        item.pop("verdict", None)
    if item.get("intermittent") is False:
        item.pop("intermittent", None)
    for attempt in cast(list[dict[str, Any]], item.get("reproduction_attempts") or []):
        if attempt.get("ordinal") == 1 and attempt.get("reason") is None:
            attempt.pop("ordinal", None)
            attempt.pop("reason", None)


def data_sha256(data: ReportData) -> str:
    content = asdict(data)
    # Runs sem executor nativo mantêm o digest anterior; o campo só existe quando há consumo.
    if content.get("model_usage") is None:
        content.pop("model_usage", None)
    if content.get("change_context") is None:
        content.pop("change_context", None)
    if content.get("execution") is None:
        content.pop("execution", None)
    if content.get("trace") is None:
        content.pop("trace", None)
    for group in ("confirmed", "suspected", "harness_failures"):
        for item in cast(list[dict[str, Any]], content.get(group) or []):
            _strip_finding_defaults(item)
    return sha256_text(canonical(content))


def _execution(inputs: ReportInputs) -> ExecutionView | None:
    state = inputs.execution
    if state is None:
        return None
    return ExecutionView(
        stop_status=state.stop_status,
        stop_reason=state.stop_reason,
        actions_used=state.actions_reserved,
        max_actions=state.max_actions,
        max_minutes=state.max_minutes,
        max_attempts=state.max_attempts,
        max_no_progress_actions=state.max_no_progress_actions,
        parent_run_id=inputs.parent_run_id,
        pending=tuple(
            PendingView(
                kind=item.kind,
                reference=item.reference,
                state=item.state,
                requires_manual_review=item.requires_manual_review,
                detail=item.detail,
                run_id=item.run_id,
            )
            for item in inputs.pending_items
        ),
        reconciliations=tuple(
            ReconciliationView(
                source_write_id=item.source_write_id,
                state=item.state.value,
                reason=item.reason,
                evidence_artifact_id=item.observation_artifact_id,
            )
            for item in inputs.reconciliations
        ),
    )


def _model_usage(inputs: ReportInputs) -> ModelUsageView | None:
    usage = inputs.model_usage
    if usage is None:
        return None
    from lastro.native.pricing import format_usd

    return ModelUsageView(
        calls=usage.calls,
        uncertain_calls=usage.uncertain_calls,
        input_tokens=usage.total_input_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        confirmed_usd=format_usd(usage.confirmed_nusd),
        committed_usd=format_usd(usage.committed_nusd),
        cap_usd=format_usd(usage.cap_nusd),
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _change_context(inputs: ReportInputs) -> ChangeContextView | None:
    """Snapshot, modo, frescor, exclusões e versões; correspondência só com prova confiável."""
    context, snapshot, run = inputs.change_context, inputs.issue_snapshot, inputs.run
    if context is None and snapshot is None:
        return None
    payload = context.payload if context is not None else {}
    observation = inputs.version_observations[-1] if inputs.version_observations else {}
    analyzed = run.analyzed_code_version
    executed = run.executed_system_version
    match = (
        _text(observation.get("version_match"))
        or version_match(analyzed, executed, bool(payload.get("dirty"))).value
    )
    issue_source = None
    if snapshot is not None:
        issue_source = (
            f"{snapshot.repository}#{snapshot.issue_number}"
            if snapshot.repository and snapshot.issue_number
            else snapshot.source_kind
        )
    data = snapshot.payload if snapshot is not None else {}
    exclusions = cast(list[dict[str, object]], payload.get("exclusions") or [])
    files = cast(list[object], payload.get("files") or [])
    sources = cast(list[object], payload.get("sources") or [])
    base_ref, base_commit = _text(payload.get("base_ref")), _text(payload.get("base_commit"))
    return ChangeContextView(
        issue_source=issue_source,
        issue_sha256=snapshot.content_sha256 if snapshot is not None else None,
        issue_comments=f"{data.get('comments_included', 0)}/{data.get('comments_total', 0)}"
        if snapshot is not None
        else None,
        issue_scan=_text(data.get("scan_level")),
        source_state=context.source_state if context is not None else "disabled",
        comparison=_text(payload.get("comparison")),
        branch=_text(payload.get("branch")),
        head=_text(payload.get("head")),
        base=f"{base_ref or '-'} ({base_commit or 'unavailable'})"
        if base_ref or base_commit
        else None,
        merge_base=_text(payload.get("merge_base")),
        dirty=payload.get("dirty") is True,
        remote_freshness=_text(payload.get("remote_freshness")) or "not_checked",
        changed=len(files),
        sources=len(sources),
        exclusions=tuple(f"{item.get('path')}: {item.get('reason')}" for item in exclusions),
        truncated=payload.get("truncated") is True or payload.get("diff_truncated") is True,
        warnings=tuple(str(item) for item in cast(list[object], payload.get("warnings") or [])),
        analyzed_version=analyzed or "unknown",
        executed_version=executed or "unknown",
        version_match=match,
        version_method=_text(observation.get("method")) or "not observed",
    )


def _file_present(run_dir: Path, relative: str) -> bool:
    try:
        return resolve_inside(run_dir, relative).is_file()
    except UnsafeArtifactPathError:
        return False


def _evidence(
    artifact: ArtifactRecord | None, artifact_id: str, purpose: str, present: dict[str, bool]
) -> EvidenceView:
    if artifact is None:
        return EvidenceView(artifact_id, "unknown", "unknown", "", purpose, True)
    return EvidenceView(
        artifact_id=artifact.id,
        relative_path=artifact.relative_path,
        artifact_type=artifact.artifact_type.value,
        sha256=artifact.sha256,
        purpose=purpose,
        missing=not present.get(artifact.id, False),
    )


def _verdict_view(verdict: ReproductionVerdict | None) -> VerdictView | None:
    if verdict is None:
        return None
    return VerdictView(
        label=verdict.label,
        classification=verdict.classification.value,
        reason=verdict.reason.value if verdict.reason else None,
        missing_precondition=verdict.missing_precondition,
        attempts_planned=verdict.attempts_planned,
        attempts_decisive=verdict.attempts_decisive,
        reproduced_count=verdict.reproduced_count,
        intermittent=verdict.intermittent,
        cross_executor=verdict.cross_executor,
        explorer_executor=verdict.explorer_executor,
        verifier_executor=verdict.verifier_executor,
        notes=verdict.notes,
        decided_at=verdict.decided_at,
    )


def _finding_view(
    finding: Finding,
    artifacts: dict[str, ArtifactRecord],
    present: dict[str, bool],
    verdict: ReproductionVerdict | None = None,
) -> FindingView:
    return FindingView(
        id=finding.id,
        title=finding.title,
        flow=finding.flow,
        classification=finding.classification.value,
        failure_domain=finding.failure_domain.value,
        origin=finding.origin.value,
        severity_suggestion=finding.severity_suggestion.value
        if finding.severity_suggestion
        else None,
        environment_url=finding.environment_url,
        tenant=finding.tenant,
        system_version=finding.system_version,
        preconditions=finding.preconditions,
        steps=finding.steps,
        expected=finding.expected,
        expected_source=finding.expected_source.value,
        expected_source_ref=finding.expected_source_ref,
        observed=finding.observed,
        evidence=tuple(
            _evidence(artifacts.get(item.artifact_id), item.artifact_id, item.purpose, present)
            for item in finding.evidence
        ),
        limitations=finding.limitations,
        reproduction_attempts=tuple(
            ReproductionView(
                item.child_run_id,
                item.outcome.value,
                item.executor,
                item.attempted_at,
                item.ordinal,
                item.reason.value if item.reason else None,
            )
            for item in finding.reproduction_attempts
        ),
        fingerprint=finding.fingerprint,
        created_at=finding.created_at,
        intermittent=finding.intermittent,
        verdict=_verdict_view(verdict),
    )


def _mission(inputs: ReportInputs) -> MissionView:
    run, plan = inputs.run, inputs.plan
    charter = plan.charter if plan is not None else {}
    timebox = charter.get("timebox_minutes")
    statement = charter.get("statement")
    heuristics = charter.get("heuristics")
    return MissionView(
        source=run.mission_source.value,
        text=run.mission_text,
        snapshot_path=run.mission_snapshot_path,
        content_sha256=run.mission_file_sha256,
        plan_revision=plan.revision if plan is not None else None,
        objectives=plan.objectives if plan is not None else (),
        scope=plan.scope if plan is not None else (),
        gaps=tuple(gap.description + (" (blocking)" if gap.blocking else "") for gap in plan.gaps)
        if plan is not None
        else (),
        out_of_scope=plan.out_of_scope if plan is not None else (),
        charter_statement=statement if isinstance(statement, str) else None,
        timebox_minutes=timebox if isinstance(timebox, int) else None,
        heuristics=tuple(str(item) for item in cast(list[object], heuristics))
        if isinstance(heuristics, list)
        else (),
    )


def _blocks(inputs: ReportInputs, criteria: tuple[CriterionView, ...]) -> tuple[BlockView, ...]:
    run = inputs.run
    blocks: list[BlockView] = []
    if run.status not in (RunStatus.COMPLETED, RunStatus.RUNNING):
        blocks.append(BlockView("run", f"{run.status.value}: {run.status_reason or 'no reason'}"))
    for item in inputs.clarifications:
        if item.status != ClarificationStatus.ANSWERED:
            blocks.append(
                BlockView("clarification", f"{item.gap_id}: {item.question} ({item.status.value})")
            )
    if inputs.plan is not None:
        blocks += [BlockView("gap", gap.description) for gap in inputs.plan.blocking_gaps]
    blocks += [
        BlockView("criterion", f"{item.id}: {item.reason}")
        for item in criteria
        if item.outcome == CriterionOutcome.BLOCKED.value
    ]
    denials: dict[tuple[str, str], int] = {}
    for event in inputs.events:
        if (
            event.event_type != EventType.POLICY_DECISION
            or event.policy_decision != PolicyDecision.DENY
        ):
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        source = event.tool_name or str(payload.get("source") or "policy")
        key = (source, str(payload.get("reason") or "denied"))
        denials[key] = denials.get(key, 0) + 1
    blocks += [
        BlockView("policy", f"{tool}: {reason} (x{count})")
        for (tool, reason), count in denials.items()
    ]
    pending = sum(1 for item in inputs.created_records if item.cleanup_status.value == "pending")
    if pending:
        blocks.append(BlockView("cleanup", f"{pending} created records pending manual cleanup"))
    return tuple(blocks)


def project(
    inputs: ReportInputs,
    run_dir: Path,
    kind: ReportKind,
    termination_reason: str | None = None,
) -> ReportData:
    """Projeção determinística das entradas: ordem estável e nenhum dado fora do banco."""
    run = inputs.run
    memory = inputs.memory
    artifacts = {item.id: item for item in inputs.artifacts}
    present = {
        item.id: _file_present(run_dir, item.relative_path)
        for item in inputs.artifacts
        if item.artifact_type != ArtifactType.REPORT
    }
    verdicts = {item.finding_id: item for item in inputs.verdicts}
    findings = [
        _finding_view(item, artifacts, present, verdicts.get(item.id)) for item in inputs.findings
    ]
    system = [item for item in findings if item.failure_domain == FailureDomain.SYSTEM.value]
    results = {item.criterion_id: item for item in inputs.criterion_results}
    criteria: list[CriterionView] = []
    for criterion in inputs.plan.criteria if inputs.plan is not None else ():
        result = results.get(criterion.id)
        criteria.append(
            CriterionView(
                id=criterion.id,
                text=criterion.text,
                source=criterion.source,
                outcome=result.outcome.value if result else CriterionOutcome.INCONCLUSIVE.value,
                reason=result.reason if result else NOT_EVALUATED_REASON,
                note=result.note if result else "",
                revision=result.revision if result else 0,
                result_source=result.source.value if result else "none",
                evidence=tuple(
                    _evidence(artifacts.get(artifact_id), artifact_id, "positive", present)
                    for artifact_id in (result.evidence_artifact_ids if result else ())
                ),
            )
        )
    tools = {
        event.correlation_id: event.tool_name
        for event in inputs.events
        if event.event_type == EventType.TOOL_CALL and event.correlation_id
    }
    signals = tuple(
        SignalView(
            id=signal.id,
            signal_type=signal.signal_type,
            http_method=signal.last_occurrence.http_method,
            http_status=signal.last_occurrence.http_status,
            url_path=signal.url_path,
            message=signal.message,
            occurrence_count=signal.occurrence_count,
            first_seen_at=signal.first_seen_at,
            last_seen_at=signal.last_seen_at,
            action_id=signal.first_occurrence.action_id,
            action_sequence=signal.first_occurrence.action_sequence,
            action_tool=tools.get(signal.first_occurrence.action_id or ""),
        )
        for signal in inputs.oracle_signals
    )
    records = tuple(
        CreatedRecordView(
            tracking_id=item.id,
            entity=item.entity_type,
            identifier=item.system_identifier,
            tenant=item.tenant,
            cleanup_status=item.cleanup_status.value,
            action_id=item.action_id,
            detail_url=item.detail_url,
            cleanup_instructions=item.cleanup_instructions,
            warnings=item.warnings,
            created_at=item.created_at,
        )
        for item in inputs.created_records
    )
    missing = tuple(
        artifacts[artifact_id].relative_path
        for artifact_id, available in present.items()
        if not available
    )
    limitations: list[str] = []
    if kind == ReportKind.PARTIAL:
        limitations.append(
            f"Partial report: the run ended early ({termination_reason or run.status.value})."
        )
    limitations += [
        f"Criterion {item.id} was not evaluated."
        for item in criteria
        if item.reason == NOT_EVALUATED_REASON
    ]
    limitations += [MISSING_EVIDENCE.format(path=path) for path in missing]
    limitations += [f"{item.id}: {text}" for item in findings for text in item.limitations]
    limitations += _reproduction_limitations(run, system)
    summary = SummaryView(
        status=run.status.value,
        status_reason=run.status_reason,
        criteria_met=sum(1 for item in criteria if item.outcome == CriterionOutcome.MET.value),
        criteria_total=len(criteria),
        confirmed=sum(1 for item in system if item.classification == "confirmed"),
        suspected=sum(1 for item in system if item.classification == "suspected"),
        inconclusive=sum(1 for item in system if item.classification == "inconclusive"),
        harness_failures=len(findings) - len(system),
        pending_cleanup=sum(
            1 for item in inputs.created_records if item.cleanup_status == CleanupStatus.PENDING
        ),
        integrity_valid=not missing,
    )
    return ReportData(
        kind=kind.value,
        run_id=run.id,
        termination_reason=termination_reason,
        summary=summary,
        mission=_mission(inputs),
        environment=EnvironmentView(
            run_id=run.id,
            profile=run.profile,
            environment_url=run.environment_url,
            tenant=run.tenant,
            executor_name=run.executor_name,
            executor_version=run.executor_version,
            lastro_version=run.lastro_version,
            skill_bundle_version=run.skill_bundle_version,
            analyzed_code_version=run.analyzed_code_version,
            executed_system_version=run.executed_system_version,
            started_at=run.started_at,
            ended_at=run.ended_at,
        ),
        criteria=tuple(criteria),
        confirmed=tuple(
            item for item in system if item.classification == FindingClassification.CONFIRMED
        ),
        suspected=tuple(
            item for item in system if item.classification != FindingClassification.CONFIRMED
        ),
        oracle_signals=signals,
        harness_failures=tuple(item for item in findings if item not in system),
        blocks=_blocks(inputs, tuple(criteria)),
        created_records=records,
        created_records_warning=unidentified_warning(run.id, list(inputs.created_records)),
        memory_outcome=memory.outcome.value if memory else MEMORY_LEGACY,
        memory_revision=memory.revision if memory else 0,
        memory_changed=memory.changed_count if memory else 0,
        memory_pending=memory.pending_count if memory else 0,
        limitations=tuple(limitations),
        missing_evidence=missing,
        model_usage=_model_usage(inputs),
        change_context=_change_context(inputs),
        execution=_execution(inputs),
        trace=(
            TraceView(trace_id=inputs.trace.trace_id, trace_url=inputs.trace.trace_url)
            if inputs.trace is not None
            else None
        ),
    )


def _reproduction_limitations(run: RunRecord, system: list[FindingView]) -> list[str]:
    """Pendência explícita: achados suspected sem veredito ainda não passaram pela reprodução."""
    if REPRODUCTION_CHILD_BLOCK in run.config_snapshot or not run.status.is_terminal:
        return []
    pending = [
        item for item in system if item.classification == "suspected" and item.verdict is None
    ]
    lines: list[str] = []
    if pending:
        lines.append(
            f"Reproduction pending for {len(pending)} suspected finding(s): "
            f"run `lastro run reproduce {run.id}`."
        )
    same = [
        item.id
        for item in system
        if item.verdict is not None
        and item.verdict.classification == "confirmed"
        and not item.verdict.cross_executor
    ]
    if same:
        lines.append(
            "Confirmed findings were reproduced by the same executor as the explorer; "
            "the verification is not executor-independent."
        )
    return lines


@dataclass(frozen=True)
class ReportOutcome:
    """Resultado operacional da geração; nunca altera o estado da run."""

    snapshot: ReportSnapshot
    data: ReportData
    report_path: Path | None
    fallback_path: Path | None
    issue_paths: tuple[Path, ...]

    @property
    def status(self) -> SnapshotStatus:
        return self.snapshot.status

    @property
    def summary_lines(self) -> tuple[str, ...]:
        return self.data.summary.lines()


class ReportBuilder:
    def __init__(
        self,
        history: HistoryStore,
        *,
        loader: BaseLoader | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.history = history
        self.env = environment(loader)
        self.clock = clock

    def build_final(self, run_id: str) -> ReportOutcome:
        """Depois de run_ended, para que o relatório inclua o estado terminal."""
        if not self.history.get_run(run_id).status.is_terminal:
            raise ReportingError(RUN_NOT_TERMINAL, "The final report needs a finished run.")
        return self._build(run_id, ReportKind.FINAL, None)

    def build_partial(self, run_id: str, termination_reason: str) -> ReportOutcome:
        """Para runs terminalizadas por limites ou cancelamento, e para diagnóstico explícito."""
        return self._build(run_id, ReportKind.PARTIAL, termination_reason)

    def render_report(self, data: ReportData) -> str:
        return self.env.get_template("report.md.j2").render(report=data)

    def render_issue(self, data: ReportData, finding: FindingView) -> str:
        return self.env.get_template("issue.md.j2").render(
            finding=finding, environment=data.environment
        )

    def _build(self, run_id: str, kind: ReportKind, reason: str | None) -> ReportOutcome:
        started = self.clock()
        run_dir = self.history.run_dir(run_id)
        data = project(self.history.report_inputs(run_id), run_dir, kind, reason)
        digest = data_sha256(data)
        verdicts = sum(
            1
            for group in (data.confirmed, data.suspected, data.harness_failures)
            for item in group
            if item.verdict is not None
        )
        target = report_path(kind, data.memory_revision, verdicts)
        paths = {item.id: item.relative_path for item in self.history.list_artifacts(run_id)}
        for existing in self.history.list_report_snapshots(run_id, kind):
            if existing.status == SnapshotStatus.READY:
                if existing.input_sha256 == digest:
                    return self._outcome(existing, data)
                if paths.get(existing.report_artifact_id or "") != target:
                    continue
                raise ReportConflictError(
                    f"A {kind.value} report already exists for run {run_id} with different "
                    "inputs; existing reports are never overwritten."
                )
        snapshot = self.history.open_report_snapshot(run_id, kind, digest, reason)
        try:
            rendered = self.render_report(data)
            issues = [(item, self.render_issue(data, item)) for item in data.confirmed]
        except Exception as error:
            return self._fallback(snapshot, data, kind, error, started)
        try:
            report = self.store_report(run_id, target, rendered)
            issue_ids = tuple(
                self.store_report(run_id, f"{ISSUES_DIR}/{item.id}.md", text).id
                for item, text in issues
            )
        except Exception as error:
            self.history.close_report_snapshot(
                snapshot.id,
                status=SnapshotStatus.FAILED,
                error_text=f"{type(error).__name__}: {error}",
                integrity_valid=data.summary.integrity_valid,
                duration_ms=self._elapsed(started),
            )
            raise
        closed = self.history.close_report_snapshot(
            snapshot.id,
            status=SnapshotStatus.READY,
            report_artifact_id=report.id,
            issue_artifact_ids=issue_ids,
            integrity_valid=data.summary.integrity_valid,
            duration_ms=self._elapsed(started),
        )
        return self._outcome(closed, data)

    def _fallback(
        self,
        snapshot: ReportSnapshot,
        data: ReportData,
        kind: ReportKind,
        error: Exception,
        started: float,
    ) -> ReportOutcome:
        """Falha de template: os dados brutos vão para JSON e o erro fica no snapshot."""
        detail = f"{type(error).__name__}: {error}"
        raw: dict[str, Any] = {"error": detail, "report": asdict(data)}
        fallback_id: str | None = None
        try:
            artifact = self.store_report(
                data.run_id,
                FALLBACK_PATHS.get(kind, "report.fallback.json"),
                json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            fallback_id = artifact.id
            status = SnapshotStatus.FALLBACK
        except Exception as store_error:
            detail += f"; fallback failed: {type(store_error).__name__}: {store_error}"
            status = SnapshotStatus.FAILED
        closed = self.history.close_report_snapshot(
            snapshot.id,
            status=status,
            fallback_artifact_id=fallback_id,
            integrity_valid=data.summary.integrity_valid,
            error_text=detail,
            duration_ms=self._elapsed(started),
        )
        return self._outcome(closed, data)

    def store_report(self, run_id: str, relative: str, text: str) -> ArtifactRecord:
        """Registra o arquivo uma única vez; conteúdo igual reaproveita, diferente é erro."""
        content = text.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        for artifact in self.history.list_artifacts(run_id):
            if artifact.relative_path == relative:
                if artifact.sha256 != digest:
                    raise ReportConflictError(
                        f"Artifact {relative} already exists with different content."
                    )
                return artifact
        return self.history.register_artifact(
            run_id, relative, ArtifactType.REPORT, content=content
        )

    def _outcome(self, snapshot: ReportSnapshot, data: ReportData) -> ReportOutcome:
        run_dir = self.history.run_dir(snapshot.run_id)
        paths = {
            item.id: run_dir / item.relative_path
            for item in self.history.list_artifacts(snapshot.run_id)
        }
        return ReportOutcome(
            snapshot=snapshot,
            data=data,
            report_path=paths.get(snapshot.report_artifact_id or ""),
            fallback_path=paths.get(snapshot.fallback_artifact_id or ""),
            issue_paths=tuple(paths[item] for item in snapshot.issue_artifact_ids if item in paths),
        )

    def _elapsed(self, started: float) -> int:
        return max(0, int((self.clock() - started) * 1000))
