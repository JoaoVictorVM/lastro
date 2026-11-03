"""Gates de aprovação: estados do pedido, decisão auditável e tentativas de notificação.

`approvals` existe desde a v001 com o contrato mínimo e nunca recebeu escrita; aqui ela é
reconstruída com os estados projetados. Pedidos antigos sem decisão viram `expired`, porque
silêncio nunca aprova.

O tipo `notification_degraded` exige reconstruir `events`, já que o CHECK da v001 fixa os tipos.
`oracle_signals` referencia `events` e é recriada na mesma transação: dropar a tabela pai com
filhos presentes deixaria a violação de FK pendente até o COMMIT.
"""

VERSION = 6
NAME = "approval_gates"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"

_EVENT_TYPES = (
    "('run_started', 'tool_call', 'policy_decision', 'authentication', 'approval_requested', "
    "'approval_decided', 'clarification_requested', 'clarification_answered', 'oracle_signal', "
    "'finding_submitted', 'record_created', 'knowledge_proposed', 'limit_hit', 'run_ended', "
    "'notification_degraded')"
)

_STATUSES = (
    "('pending', 'approved', 'skipped', 'ended', 'expired', 'invalidated', 'consumed', "
    "'execution_failed')"
)

STATEMENTS = (
    "CREATE TABLE oracle_signals_v6_copy AS SELECT * FROM oracle_signals",
    "DROP TABLE oracle_signals",
    f"""
    CREATE TABLE events_v6 (
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
    "INSERT INTO events_v6 (id, run_id, sequence, timestamp, event_type, tool_name, "
    "arguments_sha256, arguments_preview, policy_decision, correlation_id, idempotency_key, "
    "payload_json, result_json) "
    "SELECT id, run_id, sequence, timestamp, event_type, tool_name, arguments_sha256, "
    "arguments_preview, policy_decision, correlation_id, idempotency_key, payload_json, "
    "result_json FROM events",
    "DROP TABLE events",
    "ALTER TABLE events_v6 RENAME TO events",
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
    CREATE TABLE oracle_signals (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        signal_type TEXT NOT NULL
            CHECK (signal_type IN ('js_error', 'http_4xx', 'http_5xx', 'request_failed')),
        url_path TEXT NOT NULL,
        message TEXT NOT NULL CHECK (length(message) <= 8000),
        dedupe_sha256 TEXT NOT NULL CHECK (
            length(dedupe_sha256) = 64 AND dedupe_sha256 NOT GLOB '*[^0-9a-f]*'),
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
        first_occurrence_json TEXT NOT NULL CHECK (json_valid(first_occurrence_json)),
        last_occurrence_json TEXT NOT NULL CHECK (json_valid(last_occurrence_json)),
        last_event_id INTEGER NOT NULL REFERENCES events(id)
    )
    """,
    "INSERT INTO oracle_signals (id, run_id, signal_type, url_path, message, dedupe_sha256, "
    "first_seen_at, last_seen_at, occurrence_count, first_occurrence_json, "
    "last_occurrence_json, last_event_id) "
    "SELECT id, run_id, signal_type, url_path, message, dedupe_sha256, first_seen_at, "
    "last_seen_at, occurrence_count, first_occurrence_json, last_occurrence_json, "
    "last_event_id FROM oracle_signals_v6_copy",
    "DROP TABLE oracle_signals_v6_copy",
    "CREATE UNIQUE INDEX idx_oracle_signals_run_dedupe ON oracle_signals(run_id, dedupe_sha256)",
    "CREATE INDEX idx_oracle_signals_run_latest ON oracle_signals(run_id, last_event_id DESC)",
    f"""
    CREATE TABLE approvals_v6 (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        action_id TEXT NOT NULL CHECK (length(trim(action_id)) > 0),
        action_fingerprint TEXT NOT NULL CHECK {_HEX64.format("action_fingerprint")},
        status TEXT NOT NULL CHECK (status IN {_STATUSES}),
        decision TEXT NULL
            CHECK (decision IS NULL OR decision IN ('approve', 'skip', 'end_run')),
        decided_by TEXT NULL CHECK (
            decided_by IS NULL
            OR decided_by IN ('cli', 'executor_prompt', 'system', 'scripted')
        ),
        decision_reason TEXT NULL
            CHECK (decision_reason IS NULL OR length(decision_reason) <= 2000),
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(payload_json)),
        requested_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT NULL,
        invalidated_at TEXT NULL,
        consumed_at TEXT NULL,
        execution_correlation_id TEXT NULL,
        proof_nonce_sha256 TEXT NULL
            CHECK (proof_nonce_sha256 IS NULL OR {_HEX64.format("proof_nonce_sha256")}),
        CHECK ((decision IS NULL) = (decided_at IS NULL)),
        CHECK ((decision IS NULL) = (decided_by IS NULL)),
        CHECK ((status = 'pending') = (decision IS NULL)),
        CHECK (status NOT IN ('approved', 'invalidated', 'consumed', 'execution_failed')
            OR decision = 'approve'),
        CHECK (status NOT IN ('skipped', 'expired') OR decision = 'skip'),
        CHECK (status <> 'ended' OR decision = 'end_run'),
        CHECK ((status = 'invalidated') = (invalidated_at IS NOT NULL)),
        CHECK ((status IN ('consumed', 'execution_failed')) = (consumed_at IS NOT NULL))
    )
    """,
    # Pedidos antigos nunca tiveram canal de decisão: os pendentes expiram como skip do sistema.
    "INSERT INTO approvals_v6 (id, run_id, action_id, action_fingerprint, status, decision, "
    "decided_by, decision_reason, payload_json, requested_at, expires_at, decided_at) "
    "SELECT id, run_id, id, action_fingerprint, "
    "CASE decision WHEN 'approve' THEN 'approved' WHEN 'skip' THEN 'skipped' "
    "WHEN 'end_run' THEN 'ended' ELSE 'expired' END, "
    "COALESCE(decision, 'skip'), CASE WHEN decision IS NULL THEN 'system' ELSE 'cli' END, "
    "CASE WHEN decision IS NULL THEN 'no_response' ELSE NULL END, "
    "payload_json, requested_at, requested_at, COALESCE(decided_at, requested_at) "
    "FROM approvals",
    "DROP TABLE approvals",
    "ALTER TABLE approvals_v6 RENAME TO approvals",
    "CREATE UNIQUE INDEX idx_approvals_run_action ON approvals (run_id, action_id)",
    "CREATE UNIQUE INDEX idx_approvals_one_pending ON approvals (run_id) WHERE status = 'pending'",
    "CREATE INDEX idx_approvals_run_status ON approvals (run_id, status, expires_at)",
    "CREATE INDEX idx_approvals_fingerprint ON approvals (action_fingerprint, status)",
    "CREATE INDEX idx_approvals_action ON approvals (action_id)",
    "CREATE UNIQUE INDEX idx_approvals_proof_nonce ON approvals (proof_nonce_sha256) "
    "WHERE proof_nonce_sha256 IS NOT NULL",
    """
    CREATE TABLE approval_notifications (
        id TEXT NOT NULL PRIMARY KEY,
        approval_id TEXT NOT NULL REFERENCES approvals (id),
        channel TEXT NOT NULL CHECK (channel IN ('executor_hook', 'desktop', 'tail')),
        status TEXT NOT NULL CHECK (status IN ('delivered', 'degraded', 'unsupported')),
        attempt_key TEXT NOT NULL CHECK (length(trim(attempt_key)) > 0),
        requested_at TEXT NOT NULL,
        completed_at TEXT NULL,
        latency_ms INTEGER NULL CHECK (latency_ms IS NULL OR latency_ms >= 0),
        error_code TEXT NULL CHECK (error_code IS NULL OR length(error_code) <= 200),
        UNIQUE (approval_id, channel, attempt_key)
    )
    """,
    "CREATE INDEX idx_approval_notifications_channel "
    "ON approval_notifications (approval_id, channel)",
)
