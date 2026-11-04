"""Planos da missão, critérios e esclarecimentos, só dentro das transações da fachada."""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import cast
from uuid import uuid4

from lastro.change_context.models import IssueContent, issue_requirements
from lastro.history.change_context import (
    check_snapshot_integrity,
    select_context,
    select_snapshot,
)
from lastro.history.errors import MissionPlanError
from lastro.history.events import insert_event, prepare_event
from lastro.history.models import (
    MAX_MISSION_TEXT_LENGTH,
    ActionReference,
    Clarification,
    ClarificationStatus,
    EventType,
    JsonObject,
    JsonValue,
    MissionPlan,
    MissionSource,
    PlanCriterion,
    PlanGap,
    PlanSubmission,
    RunRecord,
    RunStatus,
)
from lastro.history.redaction import redact_to_json
from lastro.history.runs import apply_transition, select_run
from lastro.mission.charter import Charter
from lastro.mission.models import (
    BROWSER_ACTIONS,
    EXPLORATION_TOOLS,
    PLAN_SCHEMA_VERSION,
    ClarificationAnswer,
    ClarificationInput,
    CriterionSource,
    MissionPlanInput,
    RevisionReason,
)

INVALID_PLAN = "invalid_plan"
INVALID_REFERENCE = "invalid_reference"
REVISION_REQUIRES_CLARIFICATION = "revision_requires_clarification"
MISSION_INTEGRITY = "mission_integrity"
RUN_NOT_RUNNING = "run_not_running"
INVALID_CURSOR = "invalid_cursor"
CONTEXT_NOT_FULLY_READ = "context_not_fully_read"
PAGE_CHARACTERS = MAX_MISSION_TEXT_LENGTH
_ADDITIVE = ("supporting_information", "hypotheses", "affected_flows")
_CAPTURE_KEYS = ("capture_started_at", "capture_finished_at", "scan_level")


def _sha256(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def mission_content(run: RunRecord, snapshot: Path | None) -> tuple[str, str]:
    """Conteúdo e hash da missão; a de arquivo vem do snapshot privado conferido pelo hash."""
    if run.mission_source == MissionSource.FILE:
        if snapshot is None or run.mission_file_sha256 is None:
            raise MissionPlanError(MISSION_INTEGRITY, "mission snapshot missing")
        try:
            data = snapshot.read_bytes()
        except OSError:
            raise MissionPlanError(MISSION_INTEGRITY, "mission snapshot missing") from None
        digest = _sha256(data)
        if digest != run.mission_file_sha256:
            raise MissionPlanError(MISSION_INTEGRITY, "mission snapshot hash mismatch")
        try:
            return data.decode("utf-8-sig"), digest
        except UnicodeDecodeError:
            raise MissionPlanError(MISSION_INTEGRITY, "mission snapshot is not UTF-8") from None
    text = run.mission_text or ""
    return text, _sha256(text)


def issue_content(conn: sqlite3.Connection, run: RunRecord) -> tuple[IssueContent, str]:
    """Snapshot congelado da run, conferido pelo hash; a origem nunca é relida."""
    if run.issue_snapshot_id is None:
        raise MissionPlanError(MISSION_INTEGRITY, "issue snapshot missing")
    snapshot = select_snapshot(conn, run.issue_snapshot_id, run.project_id)
    check_snapshot_integrity(snapshot)
    payload = {key: value for key, value in snapshot.payload.items() if key not in _CAPTURE_KEYS}
    return IssueContent.model_validate(payload), snapshot.id


def requirements_text(conn: sqlite3.Connection, run: RunRecord, snapshot: Path | None) -> str:
    if run.mission_source == MissionSource.ISSUE:
        return issue_requirements(issue_content(conn, run)[0])
    return mission_content(run, snapshot)[0]


def paginate(text: str, size: int = PAGE_CHARACTERS) -> list[str]:
    """Páginas estáveis de até `size` caracteres, cortando em quebra de linha quando possível."""
    pages: list[str] = []
    rest = text
    while len(rest) > size:
        cut = rest.rfind("\n", size // 2, size)
        cut = cut + 1 if cut >= 0 else size
        pages.append(rest[:cut])
        rest = rest[cut:]
    pages.append(rest)
    return pages


def _cursor_digest(run_id: str, digest: str, section: str, index: int) -> str:
    return _sha256(f"{run_id}:{digest}:{section}:{index}")[:16]


def make_cursor(run_id: str, digest: str, section: str, index: int) -> str:
    return f"{section}.{index}.{_cursor_digest(run_id, digest, section, index)}"


def parse_cursor(cursor: str, run_id: str, digest: str, section: str, total: int) -> int:
    """Cursor opaco vinculado à run e ao conteúdo congelado; qualquer outro é inválido."""
    parts = cursor.split(".")
    if len(parts) != 3 or parts[0] != section or not parts[1].isdigit():
        raise MissionPlanError(INVALID_CURSOR, "cursor does not belong to this run and section")
    index = int(parts[1])
    if index >= total or parts[2] != _cursor_digest(run_id, digest, section, index):
        raise MissionPlanError(INVALID_CURSOR, "cursor does not belong to this run and section")
    return index


def read_pages(conn: sqlite3.Connection, run_id: str, section: str) -> set[int]:
    rows = conn.execute(
        "SELECT json_extract(payload_json, '$.page.index') AS page FROM events "
        "WHERE run_id = ? AND event_type = 'policy_decision' AND tool_name = 'mission_context' "
        "AND policy_decision = 'allow' AND json_extract(payload_json, '$.page.section') = ?",
        (run_id, section),
    ).fetchall()
    return {int(row["page"]) for row in rows if row["page"] is not None}


def _check_requirements_read(conn: sqlite3.Connection, run: RunRecord) -> None:
    if run.mission_source != MissionSource.ISSUE:
        return
    total = len(paginate(issue_requirements(issue_content(conn, run)[0])))
    if total > 1 and read_pages(conn, run.id, "issue") != set(range(total)):
        raise MissionPlanError(
            CONTEXT_NOT_FULLY_READ,
            f"read all {total} requirement pages with mission_context before submitting",
        )


def allowed_references(conn: sqlite3.Connection, run: RunRecord) -> tuple[set[str], set[str]]:
    """(todas as referências aprovadas da run, referências da issue)."""
    issue_refs: set[str] = set()
    if run.mission_source == MissionSource.ISSUE:
        content, snapshot_id = issue_content(conn, run)
        issue_refs = {snapshot_id, "issue"}
        issue_refs |= {f"comment_{index}" for index in range(1, len(content.comments) + 1)}
    refs = set(issue_refs)
    context = select_context(conn, run.id)
    if context is not None:
        refs |= {context.id, "changes"}
        refs |= {source.id for source in context.sources}
    return refs, issue_refs


def _check_references(conn: sqlite3.Connection, run: RunRecord, plan: MissionPlanInput) -> None:
    refs, issue_refs = allowed_references(conn, run)
    unknown = plan.references() - refs
    if unknown:
        raise MissionPlanError(
            INVALID_REFERENCE, "source references must be approved items of this run"
        )
    if run.mission_source != MissionSource.ISSUE:
        return
    for criterion in plan.criteria:
        if criterion.source == CriterionSource.MISSION_EXPLICIT and (
            criterion.source_ref not in issue_refs
        ):
            raise MissionPlanError(
                INVALID_REFERENCE,
                "explicit criteria of an imported issue must cite the issue snapshot",
            )


def _items(value: JsonValue) -> tuple[str, ...]:
    return tuple(str(item) for item in cast(list[JsonValue], value))


def _objects(value: JsonValue | None) -> tuple[JsonObject, ...]:
    return tuple(cast(list[JsonObject], value or []))


def _plan_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> MissionPlan:
    payload = cast(JsonObject, json.loads(row["payload_json"]))
    ids = _items(payload["criterion_ids"])
    criteria: list[PlanCriterion] = []
    for criterion_id in ids:
        stored = conn.execute(
            "SELECT payload_json FROM criteria WHERE id = ?", (criterion_id,)
        ).fetchone()
        data = cast(JsonObject, json.loads(stored["payload_json"]))
        ref = data.get("source_ref")
        criteria.append(
            PlanCriterion(
                criterion_id,
                str(data["text"]),
                str(data["source"]),
                ref if isinstance(ref, str) else None,
            )
        )
    gaps = [
        PlanGap(str(gap["id"]), str(gap["description"]), gap["blocking"] is True)
        for gap in cast(list[JsonObject], payload["gaps"])
    ]
    return MissionPlan(
        id=row["id"],
        run_id=row["run_id"],
        revision=row["revision"],
        content_sha256=row["content_sha256"],
        objectives=_items(payload["objectives"]),
        scope=_items(payload["scope"]),
        criteria=tuple(criteria),
        gaps=tuple(gaps),
        out_of_scope=_items(payload["out_of_scope"]),
        charter=cast(JsonObject, payload["charter"]),
        revision_reason=row["revision_reason"],
        clarification_id=row["clarification_id"],
        created_at=row["created_at"],
        supporting_information=_objects(payload.get("supporting_information")),
        hypotheses=_objects(payload.get("hypotheses")),
        affected_flows=_objects(payload.get("affected_flows")),
    )


def select_plans(conn: sqlite3.Connection, run_id: str) -> list[MissionPlan]:
    select_run(conn, run_id)
    rows = conn.execute(
        "SELECT * FROM mission_plans WHERE run_id = ? ORDER BY revision", (run_id,)
    ).fetchall()
    return [_plan_from_row(conn, row) for row in rows]


def select_active_plan(conn: sqlite3.Connection, run_id: str) -> MissionPlan | None:
    """O plano ativo é a maior revisão confirmada; não há flag mutável."""
    row = conn.execute(
        "SELECT * FROM mission_plans WHERE run_id = ? ORDER BY revision DESC LIMIT 1", (run_id,)
    ).fetchone()
    return _plan_from_row(conn, row) if row is not None else None


def exploration_started(conn: sqlite3.Connection, run_id: str) -> bool:
    marks = ", ".join("?" for _ in EXPLORATION_TOOLS)
    row = conn.execute(
        "SELECT 1 FROM events WHERE run_id = ? AND event_type = 'policy_decision' "
        f"AND policy_decision = 'allow' AND tool_name IN ({marks}) LIMIT 1",
        (run_id, *sorted(EXPLORATION_TOOLS)),
    ).fetchone()
    return row is not None


def _gap_id(description: str) -> str:
    return "gap_" + _sha256(description)[:12]


def _criterion_key(text: str, source: str, ref: str | None = None) -> str:
    # Sem referência, a chave é a mesma de antes: critérios antigos continuam deduplicados.
    return _sha256(source + "\n" + text + (f"\n{ref}" if ref is not None else ""))


def _content(plan: MissionPlanInput, charter: Charter) -> JsonObject:
    criteria: list[JsonValue] = []
    for item in plan.criteria:
        criterion: JsonObject = {"text": item.text, "source": item.source.value}
        if item.source_ref is not None:
            criterion["source_ref"] = item.source_ref
        criteria.append(criterion)
    gaps: list[JsonValue] = [
        {"description": item.description, "blocking": item.blocking} for item in plan.gaps
    ]
    return {
        "objectives": list[JsonValue](plan.objectives),
        "scope": list[JsonValue](plan.scope),
        "criteria": criteria,
        "gaps": gaps,
        "out_of_scope": list[JsonValue](plan.out_of_scope),
        "charter": cast(JsonObject, charter.as_json()),
        **_additive(plan),
    }


def _additive(plan: MissionPlanInput) -> JsonObject:
    """Campos novos só entram no payload quando presentes, preservando hashes antigos."""
    data: JsonObject = {}
    if plan.supporting_information:
        data["supporting_information"] = [
            {"text": item.text, "source_ref": item.source_ref}
            for item in plan.supporting_information
        ]
    if plan.hypotheses:
        data["hypotheses"] = [
            {"text": item.text, "source_ref": item.source_ref} for item in plan.hypotheses
        ]
    if plan.affected_flows:
        data["affected_flows"] = [
            {
                "flow": item.flow,
                "source_refs": list[JsonValue](item.source_refs),
                "priority": item.priority,
            }
            for item in plan.affected_flows
        ]
    return data


def _check_revision(
    conn: sqlite3.Connection, run_id: str, plan: MissionPlanInput, has_plan: bool
) -> RevisionReason:
    if not has_plan:
        if plan.revision_reason not in (None, RevisionReason.INITIAL):
            raise MissionPlanError(INVALID_PLAN, "the first plan must be an initial revision")
        return RevisionReason.INITIAL
    if plan.revision_reason is None:
        raise MissionPlanError(INVALID_PLAN, "a new revision requires revision_reason")
    if plan.revision_reason == RevisionReason.INITIAL:
        if exploration_started(conn, run_id):
            raise MissionPlanError(
                REVISION_REQUIRES_CLARIFICATION,
                "after exploration starts, revisions must reference an answered clarification",
            )
        return RevisionReason.INITIAL
    row = conn.execute(
        "SELECT status FROM clarifications WHERE id = ? AND run_id = ?",
        (plan.clarification_id, run_id),
    ).fetchone()
    if row is None or row["status"] != ClarificationStatus.ANSWERED.value:
        raise MissionPlanError(
            REVISION_REQUIRES_CLARIFICATION, "clarification_id must be answered in this run"
        )
    return RevisionReason.CLARIFICATION


def _criterion_ids(
    conn: sqlite3.Connection, run_id: str, criteria: list[JsonValue], plan_id: str, timestamp: str
) -> list[JsonValue]:
    ids: list[JsonValue] = []
    for item in cast(list[JsonObject], criteria):
        text, source = str(item["text"]), str(item["source"])
        ref_value = item.get("source_ref")
        ref = ref_value if isinstance(ref_value, str) else None
        key = _criterion_key(text, source, ref)
        row = conn.execute(
            "SELECT id, payload_json FROM criteria WHERE run_id = ? "
            "AND json_extract(payload_json, '$.normalized_sha256') = ?",
            (run_id, key),
        ).fetchone()
        if row is not None:
            stored = cast(JsonObject, json.loads(row["payload_json"]))
            if (
                stored.get("text") != text
                or stored.get("source") != source
                or stored.get("source_ref") != ref
            ):
                raise MissionPlanError(MISSION_INTEGRITY, "criterion hash collision")
            ids.append(row["id"])
            continue
        criterion_id = "criterion_" + uuid4().hex[:16]
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "text": text,
            "source": source,
            "normalized_sha256": key,
            "first_plan_id": plan_id,
            **({"source_ref": ref} if ref is not None else {}),
        }
        conn.execute(
            "INSERT INTO criteria (id, run_id, outcome, payload_json, created_at, updated_at) "
            "VALUES (?, ?, NULL, ?, ?, ?)",
            (criterion_id, run_id, json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
        )
        ids.append(criterion_id)
    return ids


def submit_plan(
    conn: sqlite3.Connection,
    run_id: str,
    plan: MissionPlanInput,
    charter: Charter,
    timestamp: str,
) -> PlanSubmission:
    """Valida a revisão e grava critérios e plano na mesma transação IMMEDIATE."""
    run = select_run(conn, run_id)
    if run.status != RunStatus.RUNNING:
        raise MissionPlanError(RUN_NOT_RUNNING, f"run is {run.status.value}")
    _check_references(conn, run, plan)
    _check_requirements_read(conn, run)
    redacted = redact_to_json(_content(plan, charter))
    content = cast(JsonObject, redacted.value)
    existing = conn.execute(
        "SELECT id FROM mission_plans WHERE run_id = ? AND content_sha256 = ?",
        (run_id, redacted.sha256),
    ).fetchone()
    active = select_active_plan(conn, run_id)
    if existing is not None:
        if active is not None and active.id == existing["id"]:
            return PlanSubmission(plan=active, created=False)
        raise MissionPlanError(INVALID_PLAN, "plan is identical to an earlier revision")
    reason = _check_revision(conn, run_id, plan, active is not None)
    plan_id = "plan_" + uuid4().hex[:16]
    criterion_ids = _criterion_ids(
        conn, run_id, cast(list[JsonValue], content["criteria"]), plan_id, timestamp
    )
    gaps: list[JsonValue] = [
        {
            "id": _gap_id(str(gap["description"])),
            "description": gap["description"],
            "blocking": gap["blocking"],
        }
        for gap in cast(list[JsonObject], content["gaps"])
    ]
    payload: JsonObject = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "objectives": content["objectives"],
        "scope": content["scope"],
        "criterion_ids": criterion_ids,
        "gaps": gaps,
        "out_of_scope": content["out_of_scope"],
        "charter": content["charter"],
        **{key: content[key] for key in _ADDITIVE if key in content},
    }
    conn.execute(
        "INSERT INTO mission_plans (id, run_id, revision, content_sha256, payload_json, "
        "revision_reason, clarification_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            run_id,
            (active.revision + 1) if active is not None else 1,
            redacted.sha256,
            json.dumps(payload, ensure_ascii=False),
            reason.value,
            plan.clarification_id,
            timestamp,
        ),
    )
    stored = select_active_plan(conn, run_id)
    assert stored is not None
    return PlanSubmission(plan=stored, created=True)


def _clarification_from_row(row: sqlite3.Row) -> Clarification:
    answer = row["answer_json"]
    return Clarification(
        id=row["id"],
        run_id=row["run_id"],
        mission_plan_id=row["mission_plan_id"],
        gap_id=row["gap_id"],
        question=row["question"],
        why_it_matters=row["why_it_matters"],
        options=_items(json.loads(row["options_json"])),
        status=ClarificationStatus(row["status"]),
        answer=cast(JsonObject, json.loads(answer)) if answer is not None else None,
        requested_at=row["requested_at"],
        answered_at=row["answered_at"],
    )


def select_clarifications(conn: sqlite3.Connection, run_id: str) -> list[Clarification]:
    select_run(conn, run_id)
    rows = conn.execute(
        "SELECT * FROM clarifications WHERE run_id = ? ORDER BY requested_at, id", (run_id,)
    ).fetchall()
    return [_clarification_from_row(row) for row in rows]


def blocking_gap(conn: sqlite3.Connection, run_id: str, gap_id: str) -> MissionPlan:
    """Plano ativo que contém o gap bloqueante; qualquer outra referência é rejeitada."""
    active = select_active_plan(conn, run_id)
    if active is None or not any(gap.id == gap_id for gap in active.blocking_gaps):
        raise MissionPlanError(
            INVALID_REFERENCE, "gap_id must be a blocking gap of the active plan"
        )
    return active


def request_clarification(
    conn: sqlite3.Connection, run_id: str, request: ClarificationInput, timestamp: str
) -> tuple[Clarification, bool]:
    """Pergunta, evento e espera confirmados juntos; uma pergunta pendente por gap."""
    run = select_run(conn, run_id)
    active = blocking_gap(conn, run_id, request.gap_id)
    pending = conn.execute(
        "SELECT * FROM clarifications WHERE run_id = ? AND gap_id = ? AND status = 'pending'",
        (run_id, request.gap_id),
    ).fetchone()
    if pending is not None:
        return _clarification_from_row(pending), False
    if run.status != RunStatus.RUNNING:
        raise MissionPlanError(RUN_NOT_RUNNING, f"run is {run.status.value}")
    fields = cast(
        JsonObject,
        redact_to_json(
            {
                "question": request.question,
                "why_it_matters": request.why_it_matters,
                "options": list[JsonValue](request.options),
            }
        ).value,
    )
    clarification_id = "clarification_" + uuid4().hex[:16]
    conn.execute(
        "INSERT INTO clarifications (id, run_id, mission_plan_id, gap_id, question, "
        "why_it_matters, options_json, status, requested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (
            clarification_id,
            run_id,
            active.id,
            request.gap_id,
            fields["question"],
            fields["why_it_matters"],
            json.dumps(fields["options"], ensure_ascii=False),
            timestamp,
        ),
    )
    event = prepare_event(
        EventType.CLARIFICATION_REQUESTED,
        payload={
            "clarification_id": clarification_id,
            "mission_plan_id": active.id,
            "gap_id": request.gap_id,
            **fields,
        },
    )
    insert_event(conn, run_id, event, timestamp)
    apply_transition(conn, run_id, RunStatus.AWAITING_CLARIFICATION, "clarification", timestamp)
    row = conn.execute("SELECT * FROM clarifications WHERE id = ?", (clarification_id,)).fetchone()
    return _clarification_from_row(row), True


def answer_clarification(
    conn: sqlite3.Connection,
    clarification_id: str,
    answer: ClarificationAnswer,
    timestamp: str,
) -> Clarification:
    """Registra a resposta humana e volta a run para running; nada aqui concede aprovação."""
    row = conn.execute("SELECT * FROM clarifications WHERE id = ?", (clarification_id,)).fetchone()
    if row is None:
        raise MissionPlanError(INVALID_REFERENCE, "unknown clarification_id")
    if row["status"] != ClarificationStatus.PENDING.value:
        raise MissionPlanError(INVALID_REFERENCE, "clarification is not pending")
    stored = cast(
        JsonObject,
        redact_to_json(
            {"answer": answer.answer, "scope": answer.scope, "source": "user_provided"}
        ).value,
    )
    conn.execute(
        "UPDATE clarifications SET status = 'answered', answer_json = ?, answered_at = ? "
        "WHERE id = ?",
        (json.dumps(stored, ensure_ascii=False), timestamp, clarification_id),
    )
    event = prepare_event(
        EventType.CLARIFICATION_ANSWERED,
        payload={"clarification_id": clarification_id, "gap_id": row["gap_id"], **stored},
    )
    insert_event(conn, row["run_id"], event, timestamp)
    apply_transition(conn, row["run_id"], RunStatus.RUNNING, None, timestamp)
    updated = conn.execute(
        "SELECT * FROM clarifications WHERE id = ?", (clarification_id,)
    ).fetchone()
    return _clarification_from_row(updated)


def resolve_actions(
    conn: sqlite3.Connection, run_id: str, action_ids: list[str]
) -> tuple[ActionReference, ...]:
    """Cada id aponta para uma chamada permitida desta run, com ao menos uma ação de browser."""
    references: list[ActionReference] = []
    for action_id in action_ids:
        row = conn.execute(
            "SELECT call.tool_name, call.sequence, call.arguments_sha256 FROM events call "
            "JOIN events decision ON decision.run_id = call.run_id "
            "AND decision.correlation_id = call.correlation_id "
            "AND decision.event_type = 'policy_decision' "
            "WHERE call.run_id = ? AND call.event_type = 'tool_call' AND call.correlation_id = ? "
            "AND decision.policy_decision = 'allow'",
            (run_id, action_id),
        ).fetchone()
        if row is None:
            raise MissionPlanError(
                INVALID_REFERENCE, "action ids must be allowed tool calls of this run"
            )
        references.append(
            ActionReference(action_id, row["tool_name"], row["sequence"], row["arguments_sha256"])
        )
    if not any(reference.tool_name in BROWSER_ACTIONS for reference in references):
        raise MissionPlanError(
            INVALID_REFERENCE, "a check needs at least one browser action, not only metadata"
        )
    return tuple(references)
