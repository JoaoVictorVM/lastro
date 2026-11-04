"""Executor nativo: ledger de chamadas de modelo, orçamentos e cinco tipos de evento novos.

O CHECK de `events.event_type` fixa os tipos aceitos, então `events` é reconstruída como na v006.
As filhas (`oracle_signals`, `knowledge_observations` e a neta `knowledge_evidence`) são copiadas
e removidas antes da pai: dropar a pai com filhos presentes deixaria a violação de FK pendente até
o COMMIT. Ids, sequências, índices, triggers append-only e CHECKs anteriores são preservados.
"""

VERSION = 8
NAME = "native_executor"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"

_EVENT_TYPES = (
    "('run_started', 'tool_call', 'policy_decision', 'authentication', 'approval_requested', "
    "'approval_decided', 'clarification_requested', 'clarification_answered', 'oracle_signal', "
    "'finding_submitted', 'record_created', 'knowledge_proposed', 'limit_hit', 'run_ended', "
    "'notification_degraded', 'native_started', 'model_call_started', 'model_call_completed', "
    "'model_call_failed', 'agent_failure')"
)
_NODES = (
    "('prepare_mission', 'consult_memory', 'observe', 'decide', 'act', 'verify', 'record', "
    "'wrap_up')"
)

STATEMENTS = (
    "CREATE TABLE knowledge_evidence_v8_copy AS SELECT * FROM knowledge_evidence",
    "CREATE TABLE knowledge_observations_v8_copy AS SELECT * FROM knowledge_observations",
    "CREATE TABLE oracle_signals_v8_copy AS SELECT * FROM oracle_signals",
    "DROP TABLE knowledge_evidence",
    "DROP TABLE knowledge_observations",
    "DROP TABLE oracle_signals",
    f"""
    CREATE TABLE events_v8 (
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
    "INSERT INTO events_v8 (id, run_id, sequence, timestamp, event_type, tool_name, "
    "arguments_sha256, arguments_preview, policy_decision, correlation_id, idempotency_key, "
    "payload_json, result_json) "
    "SELECT id, run_id, sequence, timestamp, event_type, tool_name, arguments_sha256, "
    "arguments_preview, policy_decision, correlation_id, idempotency_key, payload_json, "
    "result_json FROM events",
    "DROP TABLE events",
    "ALTER TABLE events_v8 RENAME TO events",
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
    "INSERT INTO oracle_signals SELECT * FROM oracle_signals_v8_copy",
    "CREATE UNIQUE INDEX idx_oracle_signals_run_dedupe ON oracle_signals(run_id, dedupe_sha256)",
    "CREATE INDEX idx_oracle_signals_run_latest ON oracle_signals(run_id, last_event_id DESC)",
    f"""
    CREATE TABLE knowledge_observations (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        event_id INTEGER NOT NULL REFERENCES events (id),
        route TEXT NOT NULL CHECK (length(route) > 0),
        screen_title TEXT NOT NULL,
        landmarks_json TEXT NOT NULL CHECK (
            json_valid(landmarks_json) AND json_type(landmarks_json) = 'array'
        ),
        scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
        projection_json TEXT NOT NULL CHECK (json_valid(projection_json)),
        content_sha256 TEXT NOT NULL CHECK {_HEX64.format("content_sha256")},
        observed_at TEXT NOT NULL,
        UNIQUE (run_id, event_id)
    )
    """,
    "INSERT INTO knowledge_observations SELECT * FROM knowledge_observations_v8_copy",
    "CREATE INDEX idx_knowledge_observations_run ON knowledge_observations (run_id, observed_at)",
    """
    CREATE TABLE knowledge_evidence (
        id TEXT NOT NULL PRIMARY KEY,
        knowledge_id TEXT NOT NULL REFERENCES knowledge (id),
        run_id TEXT NOT NULL REFERENCES runs (id),
        observation_id TEXT NULL REFERENCES knowledge_observations (id),
        artifact_id TEXT NULL REFERENCES artifacts (id),
        clarification_id TEXT NULL REFERENCES clarifications (id),
        created_at TEXT NOT NULL,
        CHECK (
            (observation_id IS NOT NULL) + (artifact_id IS NOT NULL)
            + (clarification_id IS NOT NULL) = 1
        )
    )
    """,
    "INSERT INTO knowledge_evidence SELECT * FROM knowledge_evidence_v8_copy",
    "CREATE INDEX idx_knowledge_evidence_entry ON knowledge_evidence (knowledge_id)",
    "CREATE INDEX idx_knowledge_evidence_run ON knowledge_evidence (run_id)",
    "CREATE UNIQUE INDEX uq_knowledge_evidence_observation "
    "ON knowledge_evidence (knowledge_id, observation_id) WHERE observation_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_knowledge_evidence_artifact "
    "ON knowledge_evidence (knowledge_id, artifact_id) WHERE artifact_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_knowledge_evidence_clarification "
    "ON knowledge_evidence (knowledge_id, clarification_id) WHERE clarification_id IS NOT NULL",
    "DROP TABLE knowledge_evidence_v8_copy",
    "DROP TABLE knowledge_observations_v8_copy",
    "DROP TABLE oracle_signals_v8_copy",
    """
    CREATE TABLE native_budgets (
        id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        scope TEXT NOT NULL CHECK (scope IN ('run', 'suite')),
        run_id TEXT NULL REFERENCES runs (id),
        cap_nusd INTEGER NOT NULL CHECK (cap_nusd > 0),
        created_at TEXT NOT NULL,
        CHECK (
            (scope = 'run' AND run_id IS NOT NULL AND id = 'run:' || run_id)
            OR (scope = 'suite' AND run_id IS NULL AND id GLOB 'suite:*')
        )
    )
    """,
    "CREATE INDEX idx_native_budgets_project_scope ON native_budgets (project_id, scope)",
    "CREATE UNIQUE INDEX uq_native_budgets_run ON native_budgets (run_id) WHERE run_id IS NOT NULL",
    """
    CREATE TRIGGER native_budgets_are_immutable
    BEFORE UPDATE ON native_budgets
    BEGIN
        SELECT RAISE(ABORT, 'native budgets are immutable');
    END
    """,
    f"""
    CREATE TABLE native_run_contexts (
        run_id TEXT NOT NULL PRIMARY KEY REFERENCES runs (id),
        run_budget_id TEXT NOT NULL UNIQUE REFERENCES native_budgets (id),
        suite_budget_id TEXT NULL REFERENCES native_budgets (id),
        catalog_sha256 TEXT NOT NULL CHECK {_HEX64.format("catalog_sha256")},
        catalog_json TEXT NOT NULL CHECK (json_valid(catalog_json)),
        pricing_json TEXT NOT NULL CHECK (json_valid(pricing_json)),
        pricing_sha256 TEXT NOT NULL CHECK {_HEX64.format("pricing_sha256")},
        runtime_versions_json TEXT NOT NULL CHECK (json_valid(runtime_versions_json))
    )
    """,
    "CREATE INDEX idx_native_contexts_suite ON native_run_contexts (suite_budget_id, run_id)",
    f"""
    CREATE TABLE native_model_calls (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES native_run_contexts (run_id),
        step_index INTEGER NOT NULL CHECK (step_index >= 1),
        attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 4),
        node TEXT NOT NULL CHECK (node IN {_NODES}),
        model TEXT NOT NULL CHECK (length(model) > 0),
        status TEXT NOT NULL
            CHECK (status IN ('reserved', 'sent', 'settled', 'released', 'uncertain')),
        payload_sha256 TEXT NOT NULL CHECK {_HEX64.format("payload_sha256")},
        reserved_nusd INTEGER NOT NULL CHECK (reserved_nusd >= 0),
        actual_nusd INTEGER NULL CHECK (actual_nusd IS NULL OR actual_nusd >= 0),
        input_tokens INTEGER NULL CHECK (input_tokens IS NULL OR input_tokens >= 0),
        cache_read_tokens INTEGER NULL
            CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
        cache_write_tokens INTEGER NULL
            CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
        output_tokens INTEGER NULL CHECK (output_tokens IS NULL OR output_tokens >= 0),
        max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens > 0),
        request_id TEXT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NULL,
        latency_ms INTEGER NULL CHECK (latency_ms IS NULL OR latency_ms >= 0),
        tool_calls_json TEXT NOT NULL DEFAULT '[]'
            CHECK (json_valid(tool_calls_json) AND json_type(tool_calls_json) = 'array'),
        error_code TEXT NULL,
        completion_event_id INTEGER NULL UNIQUE REFERENCES events (id),
        UNIQUE (run_id, step_index, attempt),
        CHECK (
            status <> 'settled' OR (
                actual_nusd IS NOT NULL AND input_tokens IS NOT NULL
                AND cache_read_tokens IS NOT NULL AND cache_write_tokens IS NOT NULL
                AND output_tokens IS NOT NULL
            )
        ),
        CHECK (status <> 'uncertain' OR actual_nusd IS NULL)
    )
    """,
    "CREATE INDEX idx_native_calls_run_status ON native_model_calls (run_id, status)",
    "CREATE INDEX idx_native_calls_run_step ON native_model_calls (run_id, step_index, attempt)",
)
