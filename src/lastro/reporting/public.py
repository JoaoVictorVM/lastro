"""Exportação pública: projeção allowlist do relatório e redação de valores privados conhecidos."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from lastro.change_context.versions import is_full_oid
from lastro.history import (
    HistoryStore,
    ReportConflictError,
    ReportingError,
    ReportKind,
    ReportSnapshot,
    RunRecord,
    SnapshotStatus,
)
from lastro.reporting.builder import (
    REPORT_PATHS,
    ReportBuilder,
    data_sha256,
    project,
    sha256_text,
)
from lastro.reporting.integrity import verify_artifacts
from lastro.reporting.models import (
    BlockView,
    CreatedRecordView,
    CriterionView,
    FindingView,
    ReportData,
    SignalView,
)

REPORT_NOT_READY = "report_not_ready"
REPORT_CHANGED = "report_changed"
REDACTED = "[redacted]"

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s)>\]|`]+")
_LASTRO_PREFIX = re.compile(r"(?i)\bLASTRO-[\w-]*")
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_INTERNAL_ID = re.compile(r"\b(?:art|created|video)_[0-9a-f]{12,}\b")
_EVIDENCE_PATH = re.compile(r"\bevidence/[^\s)`|]+")
_NUMERIC_SEGMENT = re.compile(r"(?<=/)\d+(?=/|$|\?)")


TRACE_HEADING = "\n## Trace\n"


def known_values(run: RunRecord, record_values: Iterable[str]) -> tuple[str, ...]:
    """Valores privados da run que precisam sumir também do texto livre."""
    environment = run.config_snapshot.get("environment")
    profile = environment if isinstance(environment, dict) else {}
    values = {run.tenant, run.environment_url, run.environment_url.rstrip("/")}
    for key in ("tenant_id", "tenant_display_name", "target_url", "login_url"):
        value = profile.get(key)
        if isinstance(value, str):
            values.add(value)
    host = re.sub(r"(?i)^https?://", "", run.environment_url).split("/")[0]
    values.add(host)
    values.update(record_values)
    return tuple(sorted((v for v in values if v.strip()), key=len, reverse=True))


def redact_public(text: str, known: Iterable[str]) -> str:
    text = _IMAGE.sub("[image removed]", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("[url removed]", text)
    for value in known:
        text = re.sub(re.escape(value), REDACTED, text, flags=re.IGNORECASE)
    text = _LASTRO_PREFIX.sub(REDACTED, text)
    text = _UUID.sub(REDACTED, text)
    text = _INTERNAL_ID.sub(REDACTED, text)
    return _EVIDENCE_PATH.sub("[evidence removed]", text)


def is_public(run: RunRecord) -> bool:
    """Visibilidade gravada no snapshot de configuração da run, não a configuração atual."""
    environment = run.config_snapshot.get("environment")
    return isinstance(environment, dict) and (environment.get("public") is True)


def public_projection(data: ReportData, known: tuple[str, ...]) -> ReportData:
    """Somente campos permitidos: sem tenant, URLs, ids de registros nem evidências visuais."""

    def clean(text: str) -> str:
        return redact_public(text, known)

    def finding(item: FindingView) -> FindingView:
        return replace(
            item,
            title=clean(item.title),
            flow=clean(item.flow),
            environment_url="",
            tenant="",
            system_version=clean(item.system_version) if item.system_version else None,
            preconditions=tuple(clean(text) for text in item.preconditions),
            steps=tuple(clean(text) for text in item.steps),
            expected=clean(item.expected),
            expected_source_ref=None,
            observed=clean(item.observed),
            evidence=(),
            limitations=tuple(clean(text) for text in item.limitations),
            reproduction_attempts=tuple(
                replace(attempt, child_run_id="", executor=clean(attempt.executor))
                for attempt in item.reproduction_attempts
            ),
            # Executores e frequência ficam; notas internas e pré-condição ausente não.
            verdict=replace(item.verdict, notes=(), missing_precondition=None)
            if item.verdict is not None
            else None,
        )

    def criterion(item: CriterionView) -> CriterionView:
        return replace(
            item, text=clean(item.text), reason=clean(item.reason), note=clean(item.note)
        )

    def signal(item: SignalView) -> SignalView:
        return replace(
            item,
            url_path=_NUMERIC_SEGMENT.sub("{id}", clean(item.url_path)),
            message=clean(item.message),
            action_id=None,
        )

    def record(item: CreatedRecordView) -> CreatedRecordView:
        return CreatedRecordView(
            tracking_id="",
            entity=clean(item.entity),
            identifier=REDACTED,
            tenant="",
            cleanup_status=item.cleanup_status,
            action_id=None,
            detail_url=None,
            cleanup_instructions=None,
            warnings=(),
            created_at=item.created_at,
        )

    return replace(
        data,
        kind=ReportKind.PUBLIC.value,
        summary=replace(
            data.summary,
            status_reason=clean(data.summary.status_reason) if data.summary.status_reason else None,
        ),
        mission=replace(
            data.mission,
            text=None,
            snapshot_path=None,
            objectives=tuple(clean(text) for text in data.mission.objectives),
            scope=tuple(clean(text) for text in data.mission.scope),
            gaps=tuple(clean(text) for text in data.mission.gaps),
            out_of_scope=tuple(clean(text) for text in data.mission.out_of_scope),
            charter_statement=clean(data.mission.charter_statement)
            if data.mission.charter_statement
            else None,
        ),
        # Contexto de mudança é privado: corpo da issue, código, diff, caminhos e commits.
        change_context=None,
        trace=None,
        environment=replace(
            data.environment,
            environment_url="",
            tenant="",
            analyzed_code_version=None,
            executed_system_version=clean(data.environment.executed_system_version)
            if data.environment.executed_system_version
            and not is_full_oid(data.environment.executed_system_version)
            else None,
        ),
        criteria=tuple(criterion(item) for item in data.criteria),
        confirmed=tuple(finding(item) for item in data.confirmed),
        suspected=tuple(finding(item) for item in data.suspected),
        oracle_signals=tuple(signal(item) for item in data.oracle_signals),
        harness_failures=tuple(finding(item) for item in data.harness_failures),
        blocks=tuple(BlockView(item.kind, clean(item.detail)) for item in data.blocks),
        created_records=tuple(record(item) for item in data.created_records),
        created_records_warning=None,
        limitations=tuple(clean(text) for text in data.limitations),
        missing_evidence=(),
    )


@dataclass(frozen=True)
class PublicExport:
    snapshot: ReportSnapshot
    path: Path
    public_profile: bool


def _ready_source(history: HistoryStore, run_id: str) -> ReportSnapshot:
    for kind in (ReportKind.FINAL, ReportKind.PARTIAL):
        ready = [
            item
            for item in history.list_report_snapshots(run_id, kind)
            if item.status == SnapshotStatus.READY
        ]
        if ready:
            return ready[-1]
    raise ReportingError(
        REPORT_NOT_READY, f"Run {run_id} has no ready report; generate it before exporting."
    )


def strip_trace(text: str) -> str:
    """Remove a seção da trace: mesmo em perfil público ela pode apontar para metadados privados."""
    start = text.find(TRACE_HEADING)
    if start < 0:
        return text
    following = text.find("\n## ", start + len(TRACE_HEADING))
    return text[:start] + (text[following + 1 :] if following >= 0 else "")


def export_public(history: HistoryStore, run_id: str) -> PublicExport:
    """Gera report.public.md uma única vez; repetir com o mesmo relatório reaproveita o arquivo."""
    source = _ready_source(history, run_id)
    run = history.get_run(run_id)
    run_dir = history.run_dir(run_id)
    artifacts = {item.id: item for item in history.list_artifacts(run_id)}
    original = artifacts[source.report_artifact_id or ""]
    if not verify_artifacts(run_dir, [original]).valid:
        raise ReportingError(
            REPORT_CHANGED, f"Report {original.relative_path} changed after it was generated."
        )
    public_profile = is_public(run)
    builder = ReportBuilder(history)
    if public_profile:
        text = strip_trace((run_dir / original.relative_path).read_text(encoding="utf-8"))
    else:
        inputs = history.report_inputs(run_id)
        data = project(inputs, run_dir, source.kind, source.termination_reason)
        if data_sha256(data) != source.input_sha256:
            raise ReportingError(
                REPORT_CHANGED,
                f"Run {run_id} changed after its report was generated; the export would diverge.",
            )
        records = [
            value for item in inputs.created_records for value in (item.id, item.system_identifier)
        ]
        known = known_values(run, records)
        text = redact_public(builder.render_report(public_projection(data, known)), known)
    digest = sha256_text(text)
    for existing in history.list_report_snapshots(run_id, ReportKind.PUBLIC):
        if existing.status == SnapshotStatus.READY and existing.input_sha256 != digest:
            raise ReportConflictError(
                f"A public export already exists for run {run_id} with different content."
            )
    snapshot = history.open_report_snapshot(run_id, ReportKind.PUBLIC, digest)
    if snapshot.status != SnapshotStatus.READY:
        artifact = builder.store_report(run_id, REPORT_PATHS[ReportKind.PUBLIC], text)
        snapshot = history.close_report_snapshot(
            snapshot.id,
            status=SnapshotStatus.READY,
            report_artifact_id=artifact.id,
            integrity_valid=source.integrity_valid,
        )
    return PublicExport(
        snapshot=snapshot,
        path=run_dir / REPORT_PATHS[ReportKind.PUBLIC],
        public_profile=public_profile,
    )
