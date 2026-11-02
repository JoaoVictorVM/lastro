"""Achados, evidências, resultados de critérios e snapshots, só dentro das transações da fachada."""

import hashlib
import json
import re
import sqlite3
from typing import cast
from uuid import uuid4

from lastro.history.artifacts import artifact_from_row
from lastro.history.errors import (
    CONFIRMED_NEEDS_EVIDENCE,
    EVIDENCE_OUTSIDE_RUN,
    ReportingError,
    RunNotActiveError,
)
from lastro.history.events import insert_event, prepare_event
from lastro.history.mission_plans import select_active_plan
from lastro.history.models import (
    NOT_EVALUATED_REASON,
    ArtifactRecord,
    ArtifactType,
    CriterionOutcome,
    CriterionResult,
    CriterionResultDraft,
    CriterionSubmission,
    EventType,
    EvidencePurpose,
    ExpectedSource,
    FailureDomain,
    Finding,
    FindingClassification,
    FindingDraft,
    FindingEvidence,
    FindingOrigin,
    FindingSubmission,
    JsonObject,
    JsonValue,
    ReportKind,
    ReportSnapshot,
    ReproductionAttempt,
    ReproductionOutcome,
    ResultSource,
    RunStatus,
    SeveritySuggestion,
    SnapshotStatus,
)
from lastro.history.redaction import redact_text, redact_to_json
from lastro.history.runs import select_run

PAYLOAD_VERSION = 1
CLASSIFICATION_NOT_ALLOWED = "classification_not_allowed"
INVALID_EVIDENCE = "invalid_evidence"
INVALID_REFERENCE = "invalid_reference"
CRITERION_NOT_ACTIVE = "criterion_not_active"
POSITIVE_EVIDENCE_REQUIRED = "positive_evidence_required"
RUN_NOT_RUNNING = "run_not_running"
FINDING_NOT_FOUND = "finding_not_found"
REPRODUCTION_REQUIRED = "reproduction_required"

_SPACES = re.compile(r"\s+")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _texts(value: JsonValue) -> tuple[str, ...]:
    return tuple(str(item) for item in cast(list[JsonValue], value or []))


def _optional(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def fingerprint(flow: str, title: str, steps: tuple[str, ...]) -> str:
    """Identidade estável do defeito, insensível a espaços e maiúsculas, para comparar runs."""
    parts = [flow, title, *steps]
    return _sha256("\n".join(_SPACES.sub(" ", part).strip().lower() for part in parts))


def require_running(conn: sqlite3.Connection, run_id: str) -> None:
    run = select_run(conn, run_id)
    if run.status.is_terminal:
        raise RunNotActiveError(run_id, run.status.value)
    if run.status != RunStatus.RUNNING:
        raise ReportingError(RUN_NOT_RUNNING, f"run is {run.status.value}")


def run_artifacts(
    conn: sqlite3.Connection, run_id: str, artifact_ids: tuple[str, ...]
) -> list[ArtifactRecord]:
    """Artefatos registrados desta run, na ordem pedida; qualquer outro id é rejeitado."""
    records: list[ArtifactRecord] = []
    for artifact_id in artifact_ids:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE id = ? AND run_id = ?", (artifact_id, run_id)
        ).fetchone()
        if row is None:
            raise ReportingError(INVALID_EVIDENCE, EVIDENCE_OUTSIDE_RUN)
        records.append(artifact_from_row(row))
    return records


def active_criterion_ids(conn: sqlite3.Connection, run_id: str) -> tuple[str, ...]:
    plan = select_active_plan(conn, run_id)
    return tuple(criterion.id for criterion in plan.criteria) if plan is not None else ()


def check_finding(conn: sqlite3.Connection, run_id: str, draft: FindingDraft) -> None:
    """Regras que dependem do banco: classificação, evidências e critério citado."""
    require_running(conn, run_id)
    if draft.classification == FindingClassification.CONFIRMED:
        raise ReportingError(
            CLASSIFICATION_NOT_ALLOWED,
            "Findings are submitted as suspected or inconclusive; "
            "confirmation requires independent reproduction.",
        )
    if len({item.artifact_id for item in draft.evidence}) != len(draft.evidence):
        raise ReportingError(INVALID_EVIDENCE, "Each evidence reference must be unique.")
    run_artifacts(conn, run_id, tuple(item.artifact_id for item in draft.evidence))
    if draft.expected_source == ExpectedSource.MISSION_CRITERION and (
        draft.expected_source_ref not in active_criterion_ids(conn, run_id)
    ):
        raise ReportingError(
            INVALID_REFERENCE, "source_ref must be a criterion id of the active plan."
        )


def _attempt_from_row(row: sqlite3.Row) -> ReproductionAttempt:
    return ReproductionAttempt(
        id=row["id"],
        finding_id=row["finding_id"],
        child_run_id=row["child_run_id"],
        outcome=ReproductionOutcome(row["outcome"]),
        evidence_artifact_ids=_texts(json.loads(row["evidence_json"])),
        executor=row["executor"],
        attempted_at=row["attempted_at"],
    )


def _finding_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Finding:
    data = cast(JsonObject, json.loads(row["payload_json"]))
    steps = conn.execute(
        "SELECT instruction FROM finding_steps WHERE finding_id = ? ORDER BY ordinal",
        (row["id"],),
    ).fetchall()
    evidence = conn.execute(
        "SELECT e.purpose, a.id, a.relative_path, a.artifact_type, a.sha256 "
        "FROM finding_evidence e JOIN artifacts a ON a.id = e.artifact_id "
        "WHERE e.finding_id = ? ORDER BY e.ordinal",
        (row["id"],),
    ).fetchall()
    attempts = conn.execute(
        "SELECT * FROM finding_reproduction_attempts WHERE finding_id = ? "
        "ORDER BY attempted_at, id",
        (row["id"],),
    ).fetchall()
    severity = _optional(data.get("severity_suggestion"))
    return Finding(
        id=row["id"],
        run_id=row["run_id"],
        title=str(data["title"]),
        flow=str(data["flow"]),
        preconditions=_texts(data["preconditions"]),
        steps=tuple(str(step["instruction"]) for step in steps),
        expected=str(data["expected"]),
        expected_source=ExpectedSource(str(data["expected_source"])),
        expected_source_ref=_optional(data.get("expected_source_ref")),
        observed=str(data["observed"]),
        evidence=tuple(
            FindingEvidence(
                artifact_id=item["id"],
                purpose=EvidencePurpose(item["purpose"]),
                relative_path=item["relative_path"],
                artifact_type=ArtifactType(item["artifact_type"]),
                sha256=item["sha256"],
            )
            for item in evidence
        ),
        classification=FindingClassification(row["classification"]),
        limitations=_texts(data["limitations"]),
        origin=FindingOrigin(row["origin"]),
        failure_domain=FailureDomain(row["failure_domain"]),
        severity_suggestion=SeveritySuggestion(severity) if severity else None,
        environment_url=str(data["environment_url"]),
        tenant=str(data["tenant"]),
        system_version=_optional(data.get("system_version")),
        fingerprint=str(data["fingerprint"]),
        action_id=_optional(data.get("action_id")),
        version=row["version"],
        reproduction_attempts=tuple(_attempt_from_row(item) for item in attempts),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def select_finding(conn: sqlite3.Connection, finding_id: str) -> Finding:
    row = conn.execute(
        "SELECT * FROM findings WHERE id = ? AND failure_domain IS NOT NULL", (finding_id,)
    ).fetchone()
    if row is None:
        raise ReportingError(FINDING_NOT_FOUND, f"unknown finding: {finding_id}")
    return _finding_from_row(conn, row)


def select_findings(conn: sqlite3.Connection, run_id: str) -> list[Finding]:
    select_run(conn, run_id)
    rows = conn.execute(
        "SELECT * FROM findings WHERE run_id = ? AND failure_domain IS NOT NULL "
        "ORDER BY created_at, id",
        (run_id,),
    ).fetchall()
    return [_finding_from_row(conn, row) for row in rows]


def insert_finding(
    conn: sqlite3.Connection, run_id: str, draft: FindingDraft, timestamp: str
) -> FindingSubmission:
    """Grava achado, passos, evidências e finding_submitted juntos; conteúdo igual é idempotente."""
    check_finding(conn, run_id, draft)
    run = select_run(conn, run_id)
    content = redact_to_json(
        {
            "title": draft.title,
            "flow": draft.flow,
            "preconditions": list[JsonValue](draft.preconditions),
            "steps": list[JsonValue](draft.steps),
            "expected": draft.expected,
            "expected_source": draft.expected_source.value,
            "expected_source_ref": draft.expected_source_ref,
            "observed": draft.observed,
            "evidence": [
                {"artifact_id": item.artifact_id, "purpose": item.purpose.value}
                for item in draft.evidence
            ],
            "classification": draft.classification.value,
            "limitations": list[JsonValue](draft.limitations),
            "failure_domain": draft.failure_domain.value,
            "severity_suggestion": draft.severity_suggestion.value
            if draft.severity_suggestion
            else None,
            "origin": draft.origin.value,
        }
    )
    existing = conn.execute(
        "SELECT id FROM findings WHERE run_id = ? "
        "AND json_extract(payload_json, '$.content_sha256') = ?",
        (run_id, content.sha256),
    ).fetchone()
    if existing is not None:
        return FindingSubmission(finding=select_finding(conn, existing["id"]), created=False)
    value = cast(JsonObject, content.value)
    steps = _texts(value["steps"])
    finding_id = "finding_" + uuid4().hex[:16]
    event = insert_event(
        conn,
        run_id,
        prepare_event(
            EventType.FINDING_SUBMITTED,
            payload={
                "finding_id": finding_id,
                "classification": draft.classification.value,
                "failure_domain": draft.failure_domain.value,
                "origin": draft.origin.value,
                "title": value["title"],
                "flow": value["flow"],
                "evidence_artifact_ids": [item.artifact_id for item in draft.evidence],
                "action_id": draft.action_id,
            },
        ),
        timestamp,
    )
    payload: JsonObject = {
        "schema_version": PAYLOAD_VERSION,
        "title": value["title"],
        "flow": value["flow"],
        "preconditions": value["preconditions"],
        "expected": value["expected"],
        "expected_source": value["expected_source"],
        "expected_source_ref": value["expected_source_ref"],
        "observed": value["observed"],
        "limitations": value["limitations"],
        "severity_suggestion": value["severity_suggestion"],
        "environment_url": run.environment_url,
        "tenant": run.tenant,
        "system_version": run.executed_system_version,
        "fingerprint": fingerprint(str(value["flow"]), str(value["title"]), steps),
        "content_sha256": content.sha256,
        "action_id": draft.action_id,
        "event_id": event.id,
    }
    conn.execute(
        "INSERT INTO findings (id, run_id, classification, payload_json, created_at, updated_at, "
        "failure_domain, origin, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            finding_id,
            run_id,
            draft.classification.value,
            json.dumps(payload, ensure_ascii=False),
            timestamp,
            timestamp,
            draft.failure_domain.value,
            draft.origin.value,
        ),
    )
    conn.executemany(
        "INSERT INTO finding_steps (finding_id, ordinal, instruction) VALUES (?, ?, ?)",
        [(finding_id, index, step) for index, step in enumerate(steps, start=1)],
    )
    conn.executemany(
        "INSERT INTO finding_evidence (finding_id, artifact_id, ordinal, purpose) "
        "VALUES (?, ?, ?, ?)",
        [
            (finding_id, item.artifact_id, index, item.purpose.value)
            for index, item in enumerate(draft.evidence, start=1)
        ],
    )
    return FindingSubmission(finding=select_finding(conn, finding_id), created=True)


def insert_reproduction_attempt(
    conn: sqlite3.Connection,
    finding_id: str,
    child_run_id: str,
    outcome: ReproductionOutcome,
    evidence_artifact_ids: tuple[str, ...],
    executor: str,
    timestamp: str,
) -> ReproductionAttempt:
    """Tentativa feita em outra run; as evidências pertencem à run de reprodução."""
    finding = select_finding(conn, finding_id)
    if child_run_id == finding.run_id:
        raise ReportingError(INVALID_REFERENCE, "A reproduction must run in a separate run.")
    select_run(conn, child_run_id)
    run_artifacts(conn, child_run_id, evidence_artifact_ids)
    if not executor.strip():
        raise ReportingError(INVALID_REFERENCE, "executor is required")
    attempt_id = "repro_" + uuid4().hex[:16]
    conn.execute(
        "INSERT INTO finding_reproduction_attempts "
        "(id, finding_id, child_run_id, outcome, evidence_json, executor, attempted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            finding_id,
            child_run_id,
            outcome.value,
            json.dumps(list(evidence_artifact_ids)),
            redact_text(executor),
            timestamp,
        ),
    )
    row = conn.execute(
        "SELECT * FROM finding_reproduction_attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    return _attempt_from_row(row)


def promote(conn: sqlite3.Connection, finding_id: str, timestamp: str) -> Finding:
    """Transição interna para confirmed: exige evidência própria e reprodução registrada."""
    finding = select_finding(conn, finding_id)
    if finding.classification == FindingClassification.CONFIRMED:
        return finding
    if not finding.evidence:
        raise ReportingError(CLASSIFICATION_NOT_ALLOWED, CONFIRMED_NEEDS_EVIDENCE)
    if not any(
        attempt.outcome == ReproductionOutcome.REPRODUCED
        for attempt in finding.reproduction_attempts
    ):
        raise ReportingError(
            REPRODUCTION_REQUIRED, "A confirmed finding needs a reproduced attempt."
        )
    conn.execute(
        "UPDATE findings SET classification = 'confirmed', version = version + 1, "
        "updated_at = ? WHERE id = ?",
        (timestamp, finding_id),
    )
    return select_finding(conn, finding_id)


def _result_from_row(row: sqlite3.Row) -> CriterionResult:
    return CriterionResult(
        id=row["id"],
        run_id=row["run_id"],
        criterion_id=row["criterion_id"],
        revision=row["revision"],
        outcome=CriterionOutcome(row["outcome"]),
        evidence_artifact_ids=_texts(json.loads(row["evidence_json"])),
        note=row["note"],
        source=ResultSource(row["source"]),
        reason=row["reason"],
        content_sha256=row["content_sha256"],
        created_at=row["created_at"],
    )


def select_criterion_results(
    conn: sqlite3.Connection, run_id: str, *, current_only: bool = True
) -> list[CriterionResult]:
    """Projeção atual (maior revisão por critério) ou todas as revisões, em ordem estável."""
    select_run(conn, run_id)
    query = "SELECT r.* FROM criterion_results r WHERE r.run_id = ?"
    if current_only:
        query += (
            " AND r.revision = (SELECT MAX(revision) FROM criterion_results x "
            "WHERE x.criterion_id = r.criterion_id)"
        )
    query += " ORDER BY r.criterion_id, r.revision"
    return [_result_from_row(row) for row in conn.execute(query, (run_id,))]


def check_criterion_result(
    conn: sqlite3.Connection, run_id: str, draft: CriterionResultDraft
) -> None:
    require_running(conn, run_id)
    if draft.criterion_id not in active_criterion_ids(conn, run_id):
        raise ReportingError(
            CRITERION_NOT_ACTIVE, "criterion_id must belong to the active plan of this run."
        )
    if draft.outcome == CriterionOutcome.MET and (
        not draft.evidence_artifact_ids or not draft.note.strip()
    ):
        raise ReportingError(
            POSITIVE_EVIDENCE_REQUIRED,
            "A met criterion needs at least 1 evidence reference of the expected behavior "
            "and a note describing what it shows.",
        )
    if len(set(draft.evidence_artifact_ids)) != len(draft.evidence_artifact_ids):
        raise ReportingError(INVALID_EVIDENCE, "Each evidence reference must be unique.")
    run_artifacts(conn, run_id, draft.evidence_artifact_ids)


def _insert_result(
    conn: sqlite3.Connection, run_id: str, draft: CriterionResultDraft, timestamp: str
) -> CriterionSubmission:
    note = redact_text(draft.note)
    reason = redact_text(draft.reason)
    content = redact_to_json(
        {
            "criterion_id": draft.criterion_id,
            "outcome": draft.outcome.value,
            "evidence_artifact_ids": list[JsonValue](draft.evidence_artifact_ids),
            "note": note,
            "reason": reason,
            "source": draft.source.value,
        }
    )
    latest = conn.execute(
        "SELECT * FROM criterion_results WHERE criterion_id = ? ORDER BY revision DESC LIMIT 1",
        (draft.criterion_id,),
    ).fetchone()
    if latest is not None and latest["content_sha256"] == content.sha256:
        return CriterionSubmission(result=_result_from_row(latest), created=False)
    result_id = "result_" + uuid4().hex[:16]
    conn.execute(
        "INSERT INTO criterion_results (id, run_id, criterion_id, revision, outcome, "
        "evidence_json, note, source, reason, content_sha256, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result_id,
            run_id,
            draft.criterion_id,
            (latest["revision"] + 1) if latest is not None else 1,
            draft.outcome.value,
            json.dumps(list(draft.evidence_artifact_ids)),
            note,
            draft.source.value,
            reason,
            content.sha256,
            timestamp,
        ),
    )
    conn.execute(
        "UPDATE criteria SET outcome = ?, updated_at = ? WHERE id = ?",
        (draft.outcome.value, timestamp, draft.criterion_id),
    )
    row = conn.execute("SELECT * FROM criterion_results WHERE id = ?", (result_id,)).fetchone()
    return CriterionSubmission(result=_result_from_row(row), created=True)


def insert_criterion_result(
    conn: sqlite3.Connection, run_id: str, draft: CriterionResultDraft, timestamp: str
) -> CriterionSubmission:
    """Nova revisão auditável; repetir o conteúdo da revisão atual não cria linha."""
    check_criterion_result(conn, run_id, draft)
    return _insert_result(conn, run_id, draft, timestamp)


def seal_results(conn: sqlite3.Connection, run_id: str, timestamp: str) -> list[CriterionResult]:
    """Critérios ativos sem resultado viram inconclusive/not_evaluated antes do fim da run."""
    select_run(conn, run_id)
    evaluated = {
        row["criterion_id"]
        for row in conn.execute(
            "SELECT DISTINCT criterion_id FROM criterion_results WHERE run_id = ?", (run_id,)
        )
    }
    sealed: list[CriterionResult] = []
    for criterion_id in active_criterion_ids(conn, run_id):
        if criterion_id in evaluated:
            continue
        draft = CriterionResultDraft(
            criterion_id=criterion_id,
            outcome=CriterionOutcome.INCONCLUSIVE,
            evidence_artifact_ids=(),
            note="",
            reason=NOT_EVALUATED_REASON,
            source=ResultSource.SYSTEM,
        )
        sealed.append(_insert_result(conn, run_id, draft, timestamp).result)
    return sealed


def _snapshot_from_row(row: sqlite3.Row) -> ReportSnapshot:
    integrity = row["integrity_valid"]
    return ReportSnapshot(
        id=row["id"],
        run_id=row["run_id"],
        kind=ReportKind(row["kind"]),
        input_sha256=row["input_sha256"],
        status=SnapshotStatus(row["status"]),
        report_artifact_id=row["report_artifact_id"],
        fallback_artifact_id=row["fallback_artifact_id"],
        issue_artifact_ids=_texts(json.loads(row["issue_artifact_ids_json"])),
        integrity_valid=None if integrity is None else bool(integrity),
        error_text=row["error_text"],
        termination_reason=row["termination_reason"],
        duration_ms=row["duration_ms"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def select_snapshots(
    conn: sqlite3.Connection, run_id: str, kind: ReportKind | None = None
) -> list[ReportSnapshot]:
    query = "SELECT * FROM report_snapshots WHERE run_id = ?"
    params: list[object] = [run_id]
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind.value)
    query += " ORDER BY created_at, id"
    return [_snapshot_from_row(row) for row in conn.execute(query, params)]


def open_snapshot(
    conn: sqlite3.Connection,
    run_id: str,
    kind: ReportKind,
    input_sha256: str,
    termination_reason: str | None,
    timestamp: str,
) -> ReportSnapshot:
    """Snapshot existente para as mesmas entradas, ou um novo pendente."""
    select_run(conn, run_id)
    row = conn.execute(
        "SELECT * FROM report_snapshots WHERE run_id = ? AND kind = ? AND input_sha256 = ?",
        (run_id, kind.value, input_sha256),
    ).fetchone()
    if row is not None:
        return _snapshot_from_row(row)
    snapshot_id = "report_" + uuid4().hex[:16]
    conn.execute(
        "INSERT INTO report_snapshots (id, run_id, kind, input_sha256, status, "
        "termination_reason, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
        (
            snapshot_id,
            run_id,
            kind.value,
            input_sha256,
            redact_text(termination_reason) if termination_reason else None,
            timestamp,
            timestamp,
        ),
    )
    created = conn.execute("SELECT * FROM report_snapshots WHERE id = ?", (snapshot_id,))
    return _snapshot_from_row(created.fetchone())


def close_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    status: SnapshotStatus,
    report_artifact_id: str | None,
    fallback_artifact_id: str | None,
    issue_artifact_ids: tuple[str, ...],
    integrity_valid: bool | None,
    error_text: str | None,
    duration_ms: int | None,
    timestamp: str,
) -> ReportSnapshot:
    conn.execute(
        "UPDATE report_snapshots SET status = ?, report_artifact_id = ?, fallback_artifact_id = ?, "
        "issue_artifact_ids_json = ?, integrity_valid = ?, error_text = ?, duration_ms = ?, "
        "updated_at = ? WHERE id = ? AND status <> 'ready'",
        (
            status.value,
            report_artifact_id,
            fallback_artifact_id,
            json.dumps(list(issue_artifact_ids)),
            None if integrity_valid is None else int(integrity_valid),
            redact_text(error_text) if error_text else None,
            duration_ms,
            timestamp,
            snapshot_id,
        ),
    )
    row = conn.execute("SELECT * FROM report_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    return _snapshot_from_row(row)
