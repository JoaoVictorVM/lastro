"""Modo autônomo: agendas, fila por perfil com lease, comparação de achados e outbox.

`events` e `report_snapshots` são reconstruídas dentro da transação com as FKs adiadas, como na
v011, para ampliar os CHECKs de tipo de evento e de kind de relatório sem perder ids, sequências,
índices nem triggers. As FKs nunca são desligadas. Nenhuma agenda, item ou entrega é criada por
backfill: runs anteriores continuam sem comparação autônoma.
"""

VERSION = 15
NAME = "autonomous_mode"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"
_JSON_ARRAY = "(json_valid({0}) AND json_type({0}) = 'array')"
_JSON_OBJECT = "(json_valid({0}) AND json_type({0}) = 'object')"

_EVENT_TYPES = (
    "('run_started', 'tool_call', 'policy_decision', 'authentication', 'approval_requested', "
    "'approval_decided', 'clarification_requested', 'clarification_answered', 'oracle_signal', "
    "'finding_submitted', 'record_created', 'knowledge_proposed', 'limit_hit', 'run_ended', "
    "'notification_degraded', 'native_started', 'model_call_started', 'model_call_completed', "
    "'model_call_failed', 'agent_failure', 'change_context_collected', 'security_event', "
    "'version_observed', 'execution_warning', 'cancellation_requested', 'write_reconciled', "
    "'run_resumed', 'reproduction_started', 'reproduction_attempt_finished', "
    "'reproduction_verdict', 'autonomous_triggered', 'autonomous_dequeued', "
    "'autonomous_compared', 'autonomous_notification')"
)

_SAFE_RELATIVE = (
    "(length({0}) BETWEEN 1 AND 500 AND substr({0}, 1, 1) <> '/' AND instr({0}, '\\') = 0 "
    "AND instr({0}, ':') = 0 AND '/' || {0} || '/' NOT LIKE '%/../%' "
    "AND '/' || {0} || '/' NOT LIKE '%/./%')"
)

STATEMENTS = (
    "PRAGMA defer_foreign_keys = ON",
    # events: amplia o CHECK de tipos -----------------------------------------------------------
    "CREATE TABLE events_v15_copy AS SELECT * FROM events",
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
    "result_json FROM events_v15_copy",
    "DROP TABLE events_v15_copy",
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
    # report_snapshots: aceita o kind autonomous -------------------------------------------------
    "CREATE TABLE report_snapshots_v15_copy AS SELECT * FROM report_snapshots",
    "DROP TABLE report_snapshots",
    f"""
    CREATE TABLE report_snapshots (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        kind TEXT NOT NULL CHECK (kind IN ('final', 'partial', 'public', 'autonomous')),
        input_sha256 TEXT NOT NULL CHECK {_HEX64.format("input_sha256")},
        status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'fallback', 'failed')),
        report_artifact_id TEXT NULL REFERENCES artifacts(id),
        fallback_artifact_id TEXT NULL REFERENCES artifacts(id),
        issue_artifact_ids_json TEXT NOT NULL DEFAULT '[]'
            CHECK {_JSON_ARRAY.format("issue_artifact_ids_json")},
        integrity_valid INTEGER NULL CHECK (integrity_valid IS NULL OR integrity_valid IN (0, 1)),
        error_text TEXT NULL,
        termination_reason TEXT NULL,
        duration_ms INTEGER NULL CHECK (duration_ms IS NULL OR duration_ms >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (run_id, kind, input_sha256),
        CHECK ((status = 'ready') = (report_artifact_id IS NOT NULL)),
        CHECK (status NOT IN ('fallback', 'failed') OR error_text IS NOT NULL)
    )
    """,
    "INSERT INTO report_snapshots (id, run_id, kind, input_sha256, status, report_artifact_id, "
    "fallback_artifact_id, issue_artifact_ids_json, integrity_valid, error_text, "
    "termination_reason, duration_ms, created_at, updated_at) "
    "SELECT id, run_id, kind, input_sha256, status, report_artifact_id, fallback_artifact_id, "
    "issue_artifact_ids_json, integrity_valid, error_text, termination_reason, duration_ms, "
    "created_at, updated_at FROM report_snapshots_v15_copy",
    "DROP TABLE report_snapshots_v15_copy",
    "CREATE INDEX idx_report_snapshots_run_status ON report_snapshots(run_id, status)",
    # Agendas e ocorrências ----------------------------------------------------------------------
    f"""
    CREATE TABLE autonomous_schedules (
        id TEXT NOT NULL PRIMARY KEY CHECK (id GLOB 'asch_*'),
        project_id TEXT NOT NULL CHECK (length(project_id) > 0),
        name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
        profile_name TEXT NOT NULL CHECK (length(profile_name) > 0),
        mission_path TEXT NOT NULL CHECK {_SAFE_RELATIVE.format("mission_path")},
        daily_time TEXT NOT NULL
            CHECK (daily_time GLOB '[0-2][0-9]:[0-5][0-9]' AND substr(daily_time, 1, 2) < '24'),
        timezone TEXT NOT NULL CHECK (length(timezone) > 0),
        executor TEXT NOT NULL CHECK (length(executor) > 0),
        settings_json TEXT NOT NULL CHECK {_JSON_OBJECT.format("settings_json")},
        fingerprint_sha256 TEXT NOT NULL CHECK {_HEX64.format("fingerprint_sha256")},
        backend TEXT NOT NULL CHECK (length(backend) > 0),
        external_id TEXT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'drifted', 'disabled', 'removed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        removed_at TEXT NULL,
        UNIQUE (project_id, name),
        CHECK ((state = 'removed') = (removed_at IS NOT NULL))
    )
    """,
    "CREATE INDEX idx_autonomous_schedules_profile ON autonomous_schedules "
    "(project_id, profile_name, state)",
    """
    CREATE TABLE autonomous_occurrences (
        id TEXT NOT NULL PRIMARY KEY CHECK (id GLOB 'aocc_*'),
        schedule_id TEXT NOT NULL REFERENCES autonomous_schedules (id),
        local_date TEXT NOT NULL
            CHECK (local_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
        due_at TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued', 'duplicate', 'missed')),
        item_id TEXT NULL REFERENCES autonomous_queue_items (id),
        reason TEXT NULL,
        UNIQUE (schedule_id, local_date),
        CHECK (status <> 'missed' OR (reason IS NOT NULL AND item_id IS NULL)),
        CHECK (status <> 'queued' OR item_id IS NOT NULL)
    )
    """,
    # Fila e lease -------------------------------------------------------------------------------
    f"""
    CREATE TABLE autonomous_queue_items (
        id TEXT NOT NULL PRIMARY KEY CHECK (id GLOB 'aq_*'),
        project_id TEXT NOT NULL CHECK (length(project_id) > 0),
        profile_name TEXT NOT NULL CHECK (length(profile_name) > 0),
        schedule_id TEXT NULL REFERENCES autonomous_schedules (id),
        trigger_kind TEXT NOT NULL
            CHECK (trigger_kind IN ('schedule', 'manual', 'workflow_run', 'deployment_status')),
        trigger_key TEXT NOT NULL CHECK (length(trigger_key) BETWEEN 1 AND 200),
        trigger_sha256 TEXT NOT NULL CHECK {_HEX64.format("trigger_sha256")},
        mission_snapshot_json TEXT NOT NULL CHECK {_JSON_OBJECT.format("mission_snapshot_json")},
        mission_sha256 TEXT NOT NULL CHECK {_HEX64.format("mission_sha256")},
        change_context_ref TEXT NULL,
        trigger_snapshot_json TEXT NOT NULL CHECK {_JSON_OBJECT.format("trigger_snapshot_json")},
        status TEXT NOT NULL CHECK (status IN
            ('queued', 'claimed', 'running', 'completed', 'blocked', 'failed', 'dropped')),
        reason TEXT NULL,
        queued_at TEXT NOT NULL,
        claimed_at TEXT NULL,
        started_at TEXT NULL,
        ended_at TEXT NULL,
        run_id TEXT NULL REFERENCES runs (id),
        report_snapshot_id TEXT NULL REFERENCES report_snapshots (id),
        UNIQUE (project_id, profile_name, trigger_kind, trigger_key),
        CHECK (status <> 'dropped' OR reason IS NOT NULL),
        CHECK (trigger_kind <> 'schedule' OR schedule_id IS NOT NULL)
    )
    """,
    "CREATE INDEX idx_autonomous_queue_profile ON autonomous_queue_items "
    "(project_id, profile_name, status, queued_at, id)",
    "CREATE UNIQUE INDEX idx_autonomous_queue_run ON autonomous_queue_items (run_id) "
    "WHERE run_id IS NOT NULL",
    "CREATE UNIQUE INDEX idx_autonomous_queue_one_active ON autonomous_queue_items "
    "(project_id, profile_name) WHERE status IN ('claimed', 'running')",
    """
    CREATE TRIGGER autonomous_queue_run_is_stable
    BEFORE UPDATE OF run_id ON autonomous_queue_items
    WHEN OLD.run_id IS NOT NULL AND (NEW.run_id IS NULL OR NEW.run_id <> OLD.run_id)
    BEGIN
        SELECT RAISE(ABORT, 'autonomous item run link is immutable');
    END
    """,
    """
    CREATE TABLE autonomous_profile_leases (
        project_id TEXT NOT NULL,
        profile_name TEXT NOT NULL,
        token TEXT NULL,
        generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
        owner_json TEXT NULL CHECK (owner_json IS NULL OR json_valid(owner_json)),
        expires_at TEXT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (project_id, profile_name),
        CHECK ((token IS NULL) = (owner_json IS NULL) AND (token IS NULL) = (expires_at IS NULL))
    )
    """,
    """
    CREATE TRIGGER autonomous_lease_generation_monotonic
    BEFORE UPDATE OF generation ON autonomous_profile_leases
    WHEN NEW.generation < OLD.generation
    BEGIN
        SELECT RAISE(ABORT, 'lease generation cannot decrease');
    END
    """,
    # Comparação ---------------------------------------------------------------------------------
    f"""
    CREATE TABLE autonomous_comparisons (
        id TEXT NOT NULL PRIMARY KEY CHECK (id GLOB 'acmp_*'),
        queue_item_id TEXT NOT NULL UNIQUE REFERENCES autonomous_queue_items (id),
        run_id TEXT NOT NULL UNIQUE REFERENCES runs (id),
        baseline_run_id TEXT NULL REFERENCES runs (id),
        status TEXT NOT NULL
            CHECK (status IN ('compared', 'no_baseline', 'not_comparable', 'failed')),
        algorithm TEXT NOT NULL CHECK (length(algorithm) > 0),
        algorithm_sha256 TEXT NOT NULL CHECK {_HEX64.format("algorithm_sha256")},
        coverage_json TEXT NOT NULL CHECK {_JSON_OBJECT.format("coverage_json")},
        counts_json TEXT NOT NULL CHECK {_JSON_OBJECT.format("counts_json")},
        limitations_json TEXT NOT NULL CHECK {_JSON_ARRAY.format("limitations_json")},
        created_at TEXT NOT NULL,
        CHECK (baseline_run_id IS NULL OR baseline_run_id <> run_id),
        CHECK (status <> 'no_baseline' OR baseline_run_id IS NULL)
    )
    """,
    f"""
    CREATE TABLE autonomous_finding_matches (
        id TEXT NOT NULL PRIMARY KEY CHECK (id GLOB 'amat_*'),
        comparison_id TEXT NOT NULL REFERENCES autonomous_comparisons (id),
        flow TEXT NOT NULL,
        label TEXT NOT NULL
            CHECK (label IN ('new', 'recurring', 'resolved', 'regressed', 'not_comparable')),
        current_finding_id TEXT NULL REFERENCES findings (id),
        previous_finding_id TEXT NULL REFERENCES findings (id),
        signature_sha256 TEXT NOT NULL CHECK {_HEX64.format("signature_sha256")},
        score REAL NULL CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
        match_rule TEXT NOT NULL CHECK (length(match_rule) > 0),
        detail_json TEXT NOT NULL CHECK {_JSON_OBJECT.format("detail_json")},
        CHECK (label <> 'new' OR (current_finding_id IS NOT NULL AND previous_finding_id IS NULL)),
        CHECK (label <> 'resolved'
            OR (current_finding_id IS NULL AND previous_finding_id IS NOT NULL)),
        CHECK (label NOT IN ('recurring', 'regressed')
            OR (current_finding_id IS NOT NULL AND previous_finding_id IS NOT NULL)),
        CHECK (label <> 'not_comparable'
            OR (current_finding_id IS NULL) <> (previous_finding_id IS NULL))
    )
    """,
    "CREATE UNIQUE INDEX idx_autonomous_matches_current ON autonomous_finding_matches "
    "(comparison_id, current_finding_id) WHERE current_finding_id IS NOT NULL",
    "CREATE UNIQUE INDEX idx_autonomous_matches_previous ON autonomous_finding_matches "
    "(comparison_id, previous_finding_id) WHERE previous_finding_id IS NOT NULL",
    # Outbox de notificação ----------------------------------------------------------------------
    f"""
    CREATE TABLE autonomous_notifications (
        id TEXT NOT NULL PRIMARY KEY CHECK (id GLOB 'anot_*'),
        queue_item_id TEXT NOT NULL REFERENCES autonomous_queue_items (id),
        channel_name TEXT NOT NULL CHECK (length(channel_name) > 0),
        channel_type TEXT NOT NULL CHECK (channel_type IN ('slack', 'teams', 'smtp')),
        message_json TEXT NOT NULL CHECK {_JSON_OBJECT.format("message_json")},
        message_sha256 TEXT NOT NULL CHECK {_HEX64.format("message_sha256")},
        destination_label TEXT NOT NULL CHECK (length(destination_label) > 0),
        status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'notification_failed')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
        next_attempt_at TEXT NULL,
        delivered_at TEXT NULL,
        last_error_code TEXT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (queue_item_id, channel_name, message_sha256),
        CHECK ((status = 'pending') = (next_attempt_at IS NOT NULL)),
        CHECK ((status = 'delivered') = (delivered_at IS NOT NULL))
    )
    """,
    "CREATE INDEX idx_autonomous_notifications_due ON autonomous_notifications "
    "(status, next_attempt_at)",
    """
    CREATE TABLE autonomous_notification_attempts (
        id TEXT NOT NULL PRIMARY KEY CHECK (id GLOB 'anat_*'),
        notification_id TEXT NOT NULL REFERENCES autonomous_notifications (id),
        ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 3),
        requested_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('delivered', 'retryable', 'permanent_failure')),
        response_code INTEGER NULL,
        latency_ms INTEGER NULL CHECK (latency_ms IS NULL OR latency_ms >= 0),
        error_code TEXT NULL,
        UNIQUE (notification_id, ordinal)
    )
    """,
    """
    CREATE TRIGGER autonomous_attempts_append_only_update
    BEFORE UPDATE ON autonomous_notification_attempts
    BEGIN
        SELECT RAISE(ABORT, 'notification attempts are append-only');
    END
    """,
    """
    CREATE TRIGGER autonomous_attempts_append_only_delete
    BEFORE DELETE ON autonomous_notification_attempts
    BEGIN
        SELECT RAISE(ABORT, 'notification attempts are append-only');
    END
    """,
)
