"""Controle de execução: limites duráveis, ações reservadas, intenções de escrita e retomada.

`events` é reconstruída dentro da transação com as FKs adiadas, como na v009, para ampliar o
CHECK de tipos sem perder ids, sequências, índices nem triggers. As FKs nunca são desligadas.
"""

VERSION = 10
NAME = "execution_control"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"

_EVENT_TYPES = (
    "('run_started', 'tool_call', 'policy_decision', 'authentication', 'approval_requested', "
    "'approval_decided', 'clarification_requested', 'clarification_answered', 'oracle_signal', "
    "'finding_submitted', 'record_created', 'knowledge_proposed', 'limit_hit', 'run_ended', "
    "'notification_degraded', 'native_started', 'model_call_started', 'model_call_completed', "
    "'model_call_failed', 'agent_failure', 'change_context_collected', 'security_event', "
    "'version_observed', 'execution_warning', 'cancellation_requested', 'write_reconciled', "
    "'run_resumed')"
)

STATEMENTS = (
    "PRAGMA defer_foreign_keys = ON",
    "CREATE TABLE events_v10_copy AS SELECT * FROM events",
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
    "result_json FROM events_v10_copy",
    "DROP TABLE events_v10_copy",
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
    """
    CREATE TABLE execution_controls (
        run_id TEXT NOT NULL PRIMARY KEY REFERENCES runs (id),
        deadline_at TEXT NOT NULL,
        actions_reserved INTEGER NOT NULL DEFAULT 0 CHECK (actions_reserved >= 0),
        actions_completed INTEGER NOT NULL DEFAULT 0
            CHECK (actions_completed BETWEEN 0 AND actions_reserved),
        no_progress_count INTEGER NOT NULL DEFAULT 0 CHECK (no_progress_count >= 0),
        event_cursor INTEGER NOT NULL DEFAULT 0 CHECK (event_cursor >= 0),
        progress_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(progress_json)),
        duration_warned INTEGER NOT NULL DEFAULT 0 CHECK (duration_warned IN (0, 1)),
        actions_warned INTEGER NOT NULL DEFAULT 0 CHECK (actions_warned IN (0, 1)),
        stop_status TEXT NULL
            CHECK (stop_status IS NULL
                OR stop_status IN ('cancelled', 'limit_reached', 'interrupted')),
        stop_reason TEXT NULL,
        stop_requested_at TEXT NULL,
        drain_deadline_at TEXT NULL,
        finalize_deadline_at TEXT NULL,
        revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
        CHECK (
            (stop_status IS NULL AND stop_reason IS NULL AND stop_requested_at IS NULL)
            OR (stop_status IS NOT NULL AND stop_reason IS NOT NULL
                AND stop_requested_at IS NOT NULL)
        )
    )
    """,
    "CREATE INDEX ix_execution_controls_pending ON execution_controls (stop_requested_at) "
    "WHERE stop_requested_at IS NOT NULL",
    """
    CREATE TRIGGER execution_controls_first_stop_wins
    BEFORE UPDATE OF stop_status, stop_reason, stop_requested_at ON execution_controls
    WHEN OLD.stop_status IS NOT NULL AND (
        NEW.stop_status IS NOT OLD.stop_status
        OR NEW.stop_reason IS NOT OLD.stop_reason
        OR NEW.stop_requested_at IS NOT OLD.stop_requested_at
    )
    BEGIN
        SELECT RAISE(ABORT, 'the first stop reason is immutable');
    END
    """,
    """
    CREATE TABLE execution_actions (
        action_id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        tool_name TEXT NOT NULL CHECK (length(tool_name) > 0),
        operation_key TEXT NULL CHECK (operation_key IS NULL OR {hex}),
        attempt_no INTEGER NULL CHECK (attempt_no IS NULL OR attempt_no > 0),
        state TEXT NOT NULL DEFAULT 'reserved'
            CHECK (state IN ('reserved', 'completed', 'failed', 'outcome_unknown')),
        reserved_at TEXT NOT NULL,
        completed_at TEXT NULL,
        result_code TEXT NULL CHECK (result_code IS NULL OR length(result_code) <= 200),
        UNIQUE (run_id, ordinal)
    )
    """.replace("{hex}", _HEX64.format("operation_key")),
    "CREATE INDEX ix_execution_actions_operation "
    "ON execution_actions (run_id, operation_key, ordinal)",
    "CREATE INDEX ix_execution_actions_pending ON execution_actions (run_id, state)",
    """
    CREATE TABLE write_intents (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        action_id TEXT NOT NULL UNIQUE REFERENCES execution_actions (action_id),
        binding_id TEXT NOT NULL CHECK (length(binding_id) > 0),
        operation TEXT NOT NULL CHECK (operation IN ('create', 'update')),
        entity TEXT NOT NULL CHECK (length(entity) > 0),
        record_id TEXT NULL,
        tenant TEXT NOT NULL CHECK (length(tenant) > 0),
        detail_path TEXT NULL CHECK (detail_path IS NULL OR substr(detail_path, 1, 1) = '/'),
        before_json TEXT NULL CHECK (before_json IS NULL OR json_valid(before_json)),
        intended_json TEXT NULL CHECK (intended_json IS NULL OR json_valid(intended_json)),
        comparable INTEGER NOT NULL DEFAULT 0 CHECK (comparable IN (0, 1)),
        state TEXT NOT NULL DEFAULT 'prepared'
            CHECK (state IN ('prepared', 'sending', 'response_known', 'response_unknown')),
        response_status INTEGER NULL
            CHECK (response_status IS NULL OR response_status BETWEEN 100 AND 599),
        created_at TEXT NOT NULL,
        sent_at TEXT NULL,
        resolved_at TEXT NULL,
        CHECK (comparable = 0 OR (intended_json IS NOT NULL AND record_id IS NOT NULL)),
        CHECK (state <> 'response_known' OR response_status IS NOT NULL)
    )
    """,
    "CREATE INDEX ix_write_intents_run_state ON write_intents (run_id, state)",
    "CREATE INDEX ix_write_intents_record ON write_intents (tenant, entity, record_id)",
    """
    CREATE TABLE run_links (
        child_run_id TEXT NOT NULL PRIMARY KEY REFERENCES runs (id),
        parent_run_id TEXT NOT NULL REFERENCES runs (id),
        kind TEXT NOT NULL CHECK (kind = 'resume'),
        created_at TEXT NOT NULL,
        CHECK (child_run_id <> parent_run_id)
    )
    """,
    "CREATE INDEX ix_run_links_parent ON run_links (parent_run_id, kind)",
    """
    CREATE TRIGGER run_links_are_immutable_update
    BEFORE UPDATE ON run_links
    BEGIN
        SELECT RAISE(ABORT, 'run links are immutable');
    END
    """,
    """
    CREATE TABLE write_reconciliations (
        id TEXT NOT NULL PRIMARY KEY,
        resume_run_id TEXT NOT NULL REFERENCES runs (id),
        source_write_id TEXT NOT NULL REFERENCES write_intents (id),
        state TEXT NOT NULL
            CHECK (state IN ('already_applied', 'ready_for_review', 'conflict', 'inconclusive')),
        reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
        observation_artifact_id TEXT NULL REFERENCES artifacts (id),
        replacement_write_id TEXT NULL REFERENCES write_intents (id),
        created_at TEXT NOT NULL,
        UNIQUE (resume_run_id, source_write_id),
        CHECK (state <> 'already_applied' OR observation_artifact_id IS NOT NULL)
    )
    """,
    "CREATE INDEX ix_write_reconciliations_source ON write_reconciliations (source_write_id)",
)
