import asyncio
import json
import sqlite3
from typing import Any

import pytest

from lastro.browser.tools import BrowserTools
from lastro.history import (
    HistoryUnavailableError,
    InvalidTransitionError,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.history.knowledge_recovery import FALLBACK_NAME
from lastro.knowledge import RevalidateInput


def rows(history: Any, query: str, *params: object) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(history.database_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.COMPLETED,
        RunStatus.BLOCKED,
        RunStatus.LIMIT_REACHED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    ],
)
def test_terminal_run_has_one_memory_outcome(knowledge: Any, status: RunStatus) -> None:
    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))

    history.finish_run(run_id, status, "motivo")

    (row,) = rows(
        history, "SELECT outcome, revision FROM run_memory_outcomes WHERE run_id = ?", run_id
    )
    promoted = status not in {RunStatus.CANCELLED, RunStatus.INTERRUPTED}
    assert row == ("updated" if promoted else "consolidation_pending", 1)
    assert len(history.list_knowledge()) == (1 if promoted else 0)


def test_empty_run_nothing_new(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    runtime = BrowserTools(knowledge.context("homolog"), history, run_id)

    ended = asyncio.run(runtime.call("end_run", {"outcome": "completed"}))

    assert ended["memory_outcome"] == "nothing_new" and ended["memory_pending"] == 0
    outcome = history.memory_outcome(run_id)
    assert outcome is not None and outcome.changed_count == 0


def test_revalidation_only_run_updated(knowledge: Any) -> None:
    history, first = knowledge.start()
    knowledge.propose(history, first, knowledge.observe(history, first))
    history.finish_run(first, RunStatus.COMPLETED)
    (entry,) = history.list_knowledge()

    history, second = knowledge.start()
    current = knowledge.observe(history, second)
    result = history.revalidate_knowledge(
        second,
        RevalidateInput(
            entry_id=entry.id,
            expected_revision=entry.revision,
            observation_id=current,
            idempotency_key="v",
        ),
    )
    history.finish_run(second, RunStatus.COMPLETED)

    assert result.status == "verified"
    outcome = history.memory_outcome(second)
    assert outcome is not None and outcome.outcome == "updated" and outcome.changed_count == 1


def test_logical_error_keeps_pending(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    conn = sqlite3.connect(history.database_path)
    try:
        conn.execute(
            "UPDATE knowledge_pending SET payload_json = json_set(payload_json, '$.fact.type', "
            "'unknown') WHERE run_id = ?",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()

    record = history.finish_run(run_id, RunStatus.COMPLETED)

    assert record.status == RunStatus.COMPLETED
    outcome = history.memory_outcome(run_id)
    assert outcome is not None and outcome.outcome == "consolidation_pending"
    (attempt,) = history.list_consolidation_attempts(run_id)
    assert attempt.error_code == "ValidationError"
    assert rows(history, "SELECT status FROM knowledge_pending") == [("pending",)]
    assert history.list_knowledge() == []


class FailingWrites:
    """Banco que ainda lê, mas recusa a transação terminal."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.path = inner.path

    def read(self, operation: Any) -> Any:
        return self.inner.read(operation)

    def write(self, operation: Any) -> Any:
        raise HistoryUnavailableError()


def fail_terminal_write(knowledge: Any) -> tuple[Any, str, str]:
    history, run_id = knowledge.start()
    proposal = knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    real = history._database
    history._database = FailingWrites(real)
    with pytest.raises(HistoryUnavailableError):
        history.finish_run(run_id, RunStatus.COMPLETED)
    history._database = real
    return history, run_id, proposal.proposal_id


def test_db_error_exports_pending_json(knowledge: Any) -> None:
    history, run_id, proposal_id = fail_terminal_write(knowledge)

    path = history.run_dir(run_id) / FALLBACK_NAME
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["outcome"] == "persistence_failed"
    assert document["run_id"] == run_id and document["project_id"] == history.project_id
    assert [item["id"] for item in document["proposals"]] == [proposal_id]
    assert history.memory_outcome(run_id) is None


def test_recovery_imports_fallback_once(knowledge: Any) -> None:
    history, run_id, _ = fail_terminal_write(knowledge)
    # Outro processo, como depois de reiniciar, vê o dono original como morto.
    knowledge.process_checker = lambda pid: False
    knowledge.owner_pid = 999_999

    recovered = knowledge.history()
    again = knowledge.history()

    attempts = recovered.list_consolidation_attempts(run_id)
    imported = [item for item in attempts if item.attempt_key.startswith("fallback:")]
    assert len(imported) == 1
    assert imported[0].previous_outcome == "persistence_failed"
    assert imported[0].outcome == "updated"
    assert recovered.get_run(run_id).status == RunStatus.INTERRUPTED
    assert len(again.list_consolidation_attempts(run_id)) == len(attempts)
    assert len(again.list_knowledge()) == 1
    assert not (history.run_dir(run_id) / FALLBACK_NAME).exists()
    (row,) = rows(again, "SELECT COUNT(*) FROM run_memory_outcomes WHERE run_id = ?", run_id)
    assert row == (1,)


def test_owner_death_does_not_promote_pending(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    # Outro processo, como depois de reiniciar, vê o dono original como morto.
    knowledge.process_checker = lambda pid: False
    knowledge.owner_pid = 999_999

    recovered = knowledge.history()

    assert recovered.get_run(run_id).status == RunStatus.INTERRUPTED
    outcome = recovered.memory_outcome(run_id)
    assert outcome is not None and outcome.outcome == "consolidation_pending"
    assert recovered.list_knowledge() == []


def test_double_end_does_not_double_consolidate(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    history.finish_run(run_id, RunStatus.COMPLETED)

    with pytest.raises(InvalidTransitionError):
        history.finish_run(run_id, RunStatus.COMPLETED)

    assert len(history.list_consolidation_attempts(run_id)) == 1
    ended = [e for e in history.list_events(run_id) if e.event_type == "run_ended"]
    assert len(ended) == 1 and len(history.list_knowledge()) == 1


def test_retry_updates_single_outcome_with_attempt_history(knowledge: Any) -> None:
    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    history.finish_run(run_id, RunStatus.CANCELLED, "cancelada")

    retried = history.consolidate_memory(run_id)
    repeated = history.consolidate_memory(run_id)

    assert retried.outcome == "updated" and retried.revision == 2
    assert repeated == retried
    attempts = history.list_consolidation_attempts(run_id)
    assert [(a.previous_outcome, a.outcome) for a in attempts] == [
        (None, "consolidation_pending"),
        ("consolidation_pending", "updated"),
    ]
    assert len(history.list_knowledge()) == 1


def test_consolidate_requires_terminal_run(knowledge: Any) -> None:
    from lastro.history import KnowledgeError

    history, run_id = knowledge.start()
    with pytest.raises(KnowledgeError) as info:
        history.consolidate_memory(run_id)
    assert info.value.code == "run_not_terminal"


def test_legacy_runs_have_no_invented_outcome(history_context: Any) -> None:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("m")).record.id
    history.finish_run(run_id, RunStatus.COMPLETED)
    conn = sqlite3.connect(history.database_path)
    try:
        conn.execute("DELETE FROM knowledge_consolidation_attempts")
        conn.execute("DELETE FROM run_memory_outcomes")
        conn.commit()
    finally:
        conn.close()

    assert history.memory_outcome(run_id) is None
    assert history.consolidate_memory(run_id).outcome == "nothing_new"


def test_report_hash_changes_after_memory_recovery(knowledge: Any) -> None:
    from lastro.reporting import ReportBuilder

    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    history.finish_run(run_id, RunStatus.CANCELLED, "cancelada")
    builder = ReportBuilder(history)

    before = builder.build_final(run_id)
    history.consolidate_memory(run_id)
    after = builder.build_final(run_id)

    assert before.report_path is not None and after.report_path is not None
    assert before.snapshot.input_sha256 != after.snapshot.input_sha256
    assert before.report_path.name == "report.md"
    assert after.report_path.name == "report.memory-r2.md"
    old = before.report_path.read_text(encoding="utf-8")
    new = after.report_path.read_text(encoding="utf-8")
    assert "Outcome: `consolidation_pending`" in old
    assert "lastro knowledge consolidate" in old
    assert "Outcome: `updated`" in new and "Outcome revision: 2" in new
    assert builder.build_final(run_id).snapshot.id == after.snapshot.id
