import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import (
    LOCK_RETRY_DELAYS,
    LOCK_TIMEOUT_SECONDS,
    EventType,
    HistoryStore,
    HistoryUnavailableError,
    InvalidEventError,
    PolicyDecision,
    RetryPolicy,
    RunNotActiveError,
    RunNotFoundError,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.history.database import Database
from lastro.history.errors import HISTORY_UNAVAILABLE_MESSAGE
from lastro.history.layout import HistoryLayout

PASSWORD = "senha-super-secreta-123"
BEARER = "eyJhbGciOiJIUzI1NiJ9.tokenTeste.assinatura"
COOKIE = "sessao-cookie-valor-987"


@pytest.fixture
def store(history_context: ProjectContext) -> HistoryStore:
    return open_history(history_context)


@pytest.fixture
def run_id(store: HistoryStore, history_context: ProjectContext) -> str:
    return store.create_run(history_context, TextMission("Explore o cadastro")).record.id


def test_authentication_event_accepts_subtypes_in_payload(store: HistoryStore, run_id: str) -> None:
    store.append_event(
        run_id,
        EventType.AUTHENTICATION,
        payload={
            "subtype": "login",
            "method": "scripted",
            "result": "success",
            "password": PASSWORD,
        },
    )
    store.append_event(
        run_id,
        "authentication",
        payload={"subtype": "session_refreshed", "result": "success", "tenant_id": "aurora"},
    )
    store.append_event(
        run_id,
        EventType.AUTHENTICATION,
        payload={"subtype": "tenant_verified", "tenant_id": "aurora", "reason": "api_me"},
    )

    events = [e for e in store.list_events(run_id) if e.event_type == EventType.AUTHENTICATION]
    assert [event.payload["subtype"] for event in events] == [  # type: ignore[index]
        "login",
        "session_refreshed",
        "tenant_verified",
    ]
    assert events[0].payload["password"] == "[REDACTED]"  # type: ignore[index]
    assert events[1].payload == {
        "subtype": "session_refreshed",
        "result": "success",
        "tenant_id": "aurora",
    }
    for payload in (None, {}, {"subtype": ""}, {"subtype": 3}):
        with pytest.raises(InvalidEventError):
            store.append_event(run_id, EventType.AUTHENTICATION, payload=payload)


def test_event_is_visible_after_each_append(store: HistoryStore, run_id: str) -> None:
    for index in range(1, 4):
        store.append_event(run_id, EventType.ORACLE_SIGNAL, payload={"status": 500, "n": index})

        conn = sqlite3.connect(store.database_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,))
            assert count.fetchone()[0] == index + 1
        finally:
            conn.close()


def test_event_sequence_is_unique_under_concurrency(
    history_context: ProjectContext, store: HistoryStore, run_id: str
) -> None:
    writers, per_writer = 4, 15
    stores = [open_history(history_context) for _ in range(writers)]
    errors: list[BaseException] = []
    start = threading.Barrier(writers)

    def write(writer: int) -> None:
        try:
            start.wait()
            for index in range(per_writer):
                stores[writer].append_event(
                    run_id, EventType.ORACLE_SIGNAL, payload={"writer": writer, "index": index}
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(writer,)) for writer in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    events = store.list_events(run_id)
    assert [event.sequence for event in events] == list(range(1, writers * per_writer + 2))
    written = {
        (event.payload["writer"], event.payload["index"])  # type: ignore[index]
        for event in events[1:]
    }
    assert written == {(w, i) for w in range(writers) for i in range(per_writer)}


def test_tool_and_policy_events_store_hash_preview_and_correlation(
    store: HistoryStore, run_id: str
) -> None:
    arguments = {"selector": "#email", "value": "ana@example.test", "password": PASSWORD}

    call = store.append_event(
        run_id,
        EventType.TOOL_CALL,
        tool_name="fill",
        arguments=arguments,
        correlation_id="corr-1",
        idempotency_key="call-1",
        result={"ok": True, "headers": {"Set-Cookie": f"sid={COOKIE}"}},
    )
    decision = store.append_event(
        run_id,
        EventType.POLICY_DECISION,
        tool_name="fill",
        arguments=arguments,
        policy_decision=PolicyDecision.ALLOW,
        correlation_id="corr-1",
        idempotency_key="call-1",
    )

    assert call.arguments_sha256 == decision.arguments_sha256
    assert call.arguments_sha256 is not None and len(call.arguments_sha256) == 64
    assert call.arguments_preview is not None and PASSWORD not in call.arguments_preview
    assert "[REDACTED]" in call.arguments_preview
    assert call.result == {"ok": True, "headers": {"Set-Cookie": "[REDACTED]"}}
    assert decision.policy_decision == PolicyDecision.ALLOW
    assert (call.correlation_id, decision.correlation_id) == ("corr-1", "corr-1")


def test_idempotency_key_returns_existing_event(store: HistoryStore, run_id: str) -> None:
    first = store.append_event(
        run_id, EventType.TOOL_CALL, tool_name="click", arguments={"x": 1}, idempotency_key="k1"
    )
    again = store.append_event(
        run_id, EventType.TOOL_CALL, tool_name="click", arguments={"x": 1}, idempotency_key="k1"
    )
    other = store.append_event(
        run_id, EventType.TOOL_CALL, tool_name="click", arguments={"x": 1}, idempotency_key="k2"
    )

    assert again == first
    assert other.sequence == first.sequence + 1
    assert len(store.list_events(run_id)) == 3


def test_event_contracts_are_enforced(store: HistoryStore, run_id: str) -> None:
    invalid: list[dict[str, Any]] = [
        {"event_type": "inventado"},
        {"event_type": EventType.RUN_STARTED},
        {"event_type": EventType.RUN_ENDED},
        {"event_type": EventType.POLICY_DECISION},
        {"event_type": EventType.POLICY_DECISION, "policy_decision": "talvez"},
        {"event_type": EventType.TOOL_CALL, "policy_decision": "allow"},
        {"event_type": EventType.ORACLE_SIGNAL, "idempotency_key": "k"},
        {"event_type": EventType.ORACLE_SIGNAL, "payload": {"x": object()}},
    ]
    for kwargs in invalid:
        event_type = kwargs.pop("event_type")
        with pytest.raises(InvalidEventError):
            store.append_event(run_id, event_type, **kwargs)
    with pytest.raises(RunNotFoundError):
        store.append_event("run_20000101_000000_ffff", EventType.LIMIT_HIT, payload={})
    store.finish_run(run_id, RunStatus.COMPLETED)
    with pytest.raises(RunNotActiveError):
        store.append_event(run_id, EventType.LIMIT_HIT, payload={"limit": "max_actions"})


def test_events_are_append_only(store: HistoryStore, run_id: str) -> None:
    conn = sqlite3.connect(store.database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE events SET event_type = 'limit_hit' WHERE run_id = ?", (run_id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
    finally:
        conn.close()


def test_cursor_returns_events_after_sequence(store: HistoryStore, run_id: str) -> None:
    for index in range(5):
        store.append_event(run_id, EventType.ORACLE_SIGNAL, payload={"index": index})

    assert [event.sequence for event in store.list_events(run_id, after_sequence=3)] == [4, 5, 6]
    assert [event.sequence for event in store.list_events(run_id, 1, limit=2)] == [2, 3]


class LockedConnector:
    """Simula banco travado e registra cada tentativa de conexão."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def __call__(self, *args: object, **kwargs: Any) -> sqlite3.Connection:
        self.timeouts.append(kwargs["timeout"])
        raise sqlite3.OperationalError("database is locked")


def test_lock_exhaustion_uses_initial_plus_three_retries(tmp_path: Path) -> None:
    sleeps: list[float] = []
    connector = LockedConnector()
    database = Database(
        tmp_path / "history.sqlite", RetryPolicy(sleep=sleeps.append), connector=connector
    )

    with pytest.raises(HistoryUnavailableError):
        database.write(lambda conn: None)

    assert LOCK_TIMEOUT_SECONDS == 5.0
    assert LOCK_RETRY_DELAYS == (0.25, 0.5, 1.0)
    assert connector.timeouts == [5.0, 5.0, 5.0, 5.0]
    assert sleeps == [0.25, 0.5, 1.0]


def test_connections_use_five_second_busy_timeout(store: HistoryStore) -> None:
    database = Database(store.database_path)

    with database.connection() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_non_lock_errors_are_not_retried(tmp_path: Path) -> None:
    sleeps: list[float] = []
    database = Database(tmp_path / "history.sqlite", RetryPolicy(sleep=sleeps.append))

    with pytest.raises(sqlite3.OperationalError):
        database.write(lambda conn: conn.execute("SELECT * FROM tabela_inexistente"))

    assert sleeps == []


def _locked_store(store: HistoryStore, connector: LockedConnector) -> HistoryStore:
    layout = HistoryLayout(store.project_dir)
    policy = RetryPolicy(sleep=lambda seconds: None)
    return HistoryStore(
        layout,
        Database(layout.database, policy, connector=connector),
        project_id=store.project_id,
        timezone=store.timezone,
    )


def test_unavailable_history_blocks_run_writes_fallback_and_fails_closed(
    store: HistoryStore, run_id: str
) -> None:
    connector = LockedConnector()
    locked = _locked_store(store, connector)

    with pytest.raises(HistoryUnavailableError) as info:
        locked.append_event(
            run_id, EventType.TOOL_CALL, tool_name="fill", arguments={"password": PASSWORD}
        )

    assert str(info.value) == HISTORY_UNAVAILABLE_MESSAGE
    assert len(connector.timeouts) == 4
    fallback = store.run_paths(run_id).run_dir / "fallback-events.jsonl"
    (line,) = fallback.read_text(encoding="utf-8").splitlines()
    entry = json.loads(line)
    assert entry["event_type"] == "tool_call"
    assert entry["error"] == "history_unavailable"
    assert PASSWORD not in line
    record = store.get_run(run_id)
    assert record.status == RunStatus.BLOCKED
    assert record.status_reason == HISTORY_UNAVAILABLE_MESSAGE
    with pytest.raises(HistoryUnavailableError):
        locked.append_event(run_id, EventType.ORACLE_SIGNAL, payload={})
    assert len(connector.timeouts) == 4


def _history_files(root: Path) -> Iterator[Path]:
    return (path for path in root.rglob("*") if path.is_file())


def test_sensitive_values_absent_from_all_history_files(store: HistoryStore, run_id: str) -> None:
    keeper = sqlite3.connect(store.database_path)
    try:
        keeper.execute("SELECT COUNT(*) FROM runs").fetchone()
        store.append_event(
            run_id,
            EventType.TOOL_CALL,
            tool_name="request",
            arguments={
                "password": PASSWORD,
                "headers": {"Authorization": f"Bearer {BEARER}", "Cookie": f"sid={COOKIE}"},
                "url": f"https://alvo.test/login?token={BEARER}",
                "note": f"Authorization: Bearer {BEARER}",
            },
            result={"set-cookie": f"sid={COOKIE}", "echo": f"password={PASSWORD}"},
            idempotency_key="secret-call",
        )
        store.append_event(
            run_id,
            EventType.AUTHENTICATION,
            payload={"subtype": "login", "credential": PASSWORD, "detail": f"Bearer {BEARER}"},
        )
        with pytest.raises(HistoryUnavailableError):
            _locked_store(store, LockedConnector()).append_event(
                run_id,
                EventType.ORACLE_SIGNAL,
                payload={"cookie": COOKIE, "message": f"falhou com password={PASSWORD}"},
            )

        files = list(_history_files(store.project_dir))
        names = {path.name for path in files}
        assert "history.sqlite" in names
        assert "history.sqlite-wal" in names
        assert "fallback-events.jsonl" in names
        for path in files:
            data = path.read_bytes()
            for secret in (PASSWORD, BEARER, COOKIE):
                assert secret.encode() not in data, (path.name, secret)
    finally:
        keeper.close()
