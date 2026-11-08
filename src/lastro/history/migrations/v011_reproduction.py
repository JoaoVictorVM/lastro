"""Reprodução independente: rodadas, runs filhas, relatos do reprodutor e vereditos imutáveis.

`events` é reconstruída dentro da transação com as FKs adiadas, como na v010, para ampliar o
CHECK de tipos sem perder ids, sequências, índices nem triggers. As FKs nunca são desligadas.
"""

VERSION = 11
NAME = "reproduction"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"

_EVENT_TYPES = (
    "('run_started', 'tool_call', 'policy_decision', 'authentication', 'approval_requested', "
    "'approval_decided', 'clarification_requested', 'clarification_answered', 'oracle_signal', "
    "'finding_submitted', 'record_created', 'knowledge_proposed', 'limit_hit', 'run_ended', "
    "'notification_degraded', 'native_started', 'model_call_started', 'model_call_completed', "
    "'model_call_failed', 'agent_failure', 'change_context_collected', 'security_event', "
    "'version_observed', 'execution_warning', 'cancellation_requested', 'write_reconciled', "
    "'run_resumed', 'reproduction_started', 'reproduction_attempt_finished', "
    "'reproduction_verdict')"
)

_ATTEMPT_REASONS = "('blocked', 'budget', 'preconditions', 'executor', 'diverged', 'evidence')"
_VERDICT_REASONS = (
    "('not_reproduced', 'blocked', 'preconditions', 'budget', 'executor', 'diverged', 'evidence')"
)

STATEMENTS = (
    "PRAGMA defer_foreign_keys = ON",
    "CREATE TABLE events_v11_copy AS SELECT * FROM events",
    "DROP TABLE events",
    f"""
    CREATE TABLE events (
        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs (id),
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        timestamp TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN {_EVENT_TYPES}),
        tool_name TEXT NULL,
        arguments_sha256 TEXT NULL
            CHECK (arguments_sha256 IS NULL OR {_HEX64.format("arguments_sha256")}),
        arguments_preview TEXT NULL
            CHECK (arguments_preview IS NULL OR length(arguments_preview) <= 200),
        policy_decision TEXT NULL
            CHECK (policy_decision IS NULL
                OR policy_decision IN ('allow', 'deny', 'require_approval')),
        correlation_id TEXT NULL,
        idempotency_key TEXT NULL,
        payload_json TEXT NULL CHECK (payload_json IS NULL OR json_valid(payload_json)),
        result_json TEXT NULL CHECK (result_json IS NULL OR json_valid(result_json)),
        CHECK (
            event_type <> 'authentication' OR (
                payload_json IS NOT NULL
                AND json_type(payload_json, '$.subtype') = 'text'
                AND length(trim(json_extract(payload_json, '$.subtype'))) > 0
            )
        )
    )
    """,
    "INSERT INTO events (id, run_id, sequence, timestamp, event_type, tool_name, "
    "arguments_sha256, arguments_preview, policy_decision, correlation_id, idempotency_key, "
    "payload_json, result_json) "
    "SELECT id, run_id, sequence, timestamp, event_type, tool_name, arguments_sha256, "
    "arguments_preview, policy_decision, correlation_id, idempotency_key, payload_json, "
    "result_json FROM events_v11_copy",
    "DROP TABLE events_v11_copy",
    "CREATE UNIQUE INDEX idx_events_run_sequence ON events (run_id, sequence)",
    "CREATE INDEX idx_events_run_type ON events (run_id, event_type)",
    "CREATE INDEX idx_events_correlation ON events (correlation_id)",
    "CREATE INDEX idx_events_timestamp ON events (timestamp)",
    "CREATE UNIQUE INDEX idx_events_idempotency ON events (run_id, event_type, idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    "CREATE UNIQUE INDEX idx_events_oracle_occurrence "
    "ON events(run_id, json_extract(payload_json, '$.occurrence_id')) "
    "WHERE event_type = 'oracle_signal' "
    "AND json_extract(payload_json, '$.occurrence_id') IS NOT NULL",
    """
    CREATE TRIGGER events_are_append_only_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events are append-only');
    END
    """,
    """
    CREATE TRIGGER events_are_append_only_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events are append-only');
    END
    """,
    "ALTER TABLE finding_reproduction_attempts "
    "ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 1 CHECK (ordinal > 0)",
    "ALTER TABLE finding_reproduction_attempts "
    f"ADD COLUMN reason TEXT NULL CHECK (reason IS NULL OR reason IN {_ATTEMPT_REASONS})",
    "ALTER TABLE finding_reproduction_attempts "
    "ADD COLUMN note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000)",
    "ALTER TABLE finding_reproduction_attempts "
    "ADD COLUMN precondition_index INTEGER NULL "
    "CHECK (precondition_index IS NULL OR precondition_index >= 0)",
    "ALTER TABLE finding_reproduction_attempts "
    "ADD COLUMN verifier_executor TEXT NOT NULL DEFAULT ''",
    """
    CREATE TABLE reproduction_rounds (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        mode TEXT NOT NULL CHECK (mode IN ('same', 'cross')),
        explorer_executor TEXT NOT NULL CHECK (length(explorer_executor) > 0),
        verifier_executor TEXT NOT NULL CHECK (length(verifier_executor) > 0),
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'completed', 'abandoned')),
        owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
        created_at TEXT NOT NULL,
        closed_at TEXT NULL,
        CHECK ((status = 'open') = (closed_at IS NULL))
    )
    """,
    "CREATE UNIQUE INDEX ux_reproduction_rounds_open ON reproduction_rounds (run_id) "
    "WHERE status = 'open'",
    "CREATE INDEX ix_reproduction_rounds_run ON reproduction_rounds (run_id, status)",
    """
    CREATE TRIGGER reproduction_rounds_closed_are_final
    BEFORE UPDATE ON reproduction_rounds
    WHEN OLD.status <> 'open'
    BEGIN
        SELECT RAISE(ABORT, 'a closed reproduction round is immutable');
    END
    """,
    """
    CREATE TABLE reproduction_runs (
        child_run_id TEXT NOT NULL PRIMARY KEY REFERENCES runs (id),
        round_id TEXT NOT NULL REFERENCES reproduction_rounds (id),
        finding_id TEXT NOT NULL REFERENCES findings (id),
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        created_at TEXT NOT NULL,
        UNIQUE (finding_id, ordinal)
    )
    """,
    "CREATE INDEX ix_reproduction_runs_finding ON reproduction_runs (finding_id, ordinal)",
    """
    CREATE TRIGGER reproduction_runs_are_immutable_update
    BEFORE UPDATE ON reproduction_runs
    BEGIN
        SELECT RAISE(ABORT, 'reproduction runs are immutable');
    END
    """,
    """
    CREATE TRIGGER reproduction_runs_are_immutable_delete
    BEFORE DELETE ON reproduction_runs
    BEGIN
        SELECT RAISE(ABORT, 'reproduction runs are immutable');
    END
    """,
    """
    CREATE TABLE reproduction_reports (
        child_run_id TEXT NOT NULL PRIMARY KEY REFERENCES reproduction_runs (child_run_id),
        result TEXT NOT NULL
            CHECK (result IN ('reproduced', 'not_reproduced', 'diverged', 'precondition_missing')),
        evidence_json TEXT NOT NULL DEFAULT '[]'
            CHECK (json_valid(evidence_json) AND json_type(evidence_json) = 'array'),
        precondition_index INTEGER NULL
            CHECK (precondition_index IS NULL OR precondition_index >= 0),
        note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
        created_at TEXT NOT NULL,
        CHECK ((result = 'precondition_missing') = (precondition_index IS NOT NULL)),
        CHECK (result <> 'reproduced' OR json_array_length(evidence_json) > 0)
    )
    """,
    """
    CREATE TRIGGER reproduction_reports_are_immutable_update
    BEFORE UPDATE ON reproduction_reports
    BEGIN
        SELECT RAISE(ABORT, 'reproduction reports are immutable');
    END
    """,
    f"""
    CREATE TABLE finding_reproduction_verdicts (
        finding_id TEXT NOT NULL PRIMARY KEY REFERENCES findings (id),
        round_id TEXT NOT NULL REFERENCES reproduction_rounds (id),
        attempts_planned INTEGER NOT NULL CHECK (attempts_planned > 0),
        attempts_decisive INTEGER NOT NULL
            CHECK (attempts_decisive BETWEEN 0 AND attempts_planned),
        reproduced_count INTEGER NOT NULL
            CHECK (reproduced_count BETWEEN 0 AND attempts_decisive),
        classification TEXT NOT NULL CHECK (classification IN ('confirmed', 'inconclusive')),
        reason TEXT NULL CHECK (reason IS NULL OR reason IN {_VERDICT_REASONS}),
        missing_precondition TEXT NULL,
        intermittent INTEGER NOT NULL CHECK (intermittent IN (0, 1)),
        cross_executor INTEGER NOT NULL CHECK (cross_executor IN (0, 1)),
        explorer_executor TEXT NOT NULL CHECK (length(explorer_executor) > 0),
        verifier_executor TEXT NOT NULL CHECK (length(verifier_executor) > 0),
        notes_json TEXT NOT NULL DEFAULT '[]'
            CHECK (json_valid(notes_json) AND json_type(notes_json) = 'array'),
        decided_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK {_HEX64.format("content_sha256")},
        CHECK ((classification = 'confirmed') = (reason IS NULL)),
        CHECK (classification <> 'confirmed' OR reproduced_count > 0),
        CHECK (classification <> 'inconclusive' OR reproduced_count = 0)
    )
    """,
    "CREATE INDEX ix_verdicts_round ON finding_reproduction_verdicts (round_id)",
    """
    CREATE TRIGGER finding_reproduction_verdicts_no_update
    BEFORE UPDATE ON finding_reproduction_verdicts
    BEGIN
        SELECT RAISE(ABORT, 'reproduction verdicts are immutable');
    END
    """,
    """
    CREATE TRIGGER finding_reproduction_verdicts_no_delete
    BEFORE DELETE ON finding_reproduction_verdicts
    BEGIN
        SELECT RAISE(ABORT, 'reproduction verdicts are immutable');
    END
    """,
)
