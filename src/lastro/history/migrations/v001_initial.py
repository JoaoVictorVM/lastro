"""Schema inicial: tabelas centrais do registro e contratos mínimos das tabelas reservadas.

Publicada e imutável. As features donas das tabelas reservadas ampliam o schema por migrations
novas, sem reinterpretar os campos mínimos definidos aqui.
"""

VERSION = 1
NAME = "initial"


def _hex64(column: str) -> str:
    return f"(length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*')"


def _json(column: str) -> str:
    return f"json_valid({column})"


_TERMINAL = "('completed', 'blocked', 'interrupted', 'cancelled', 'limit_reached')"
_STATUSES = (
    "('running', 'awaiting_approval', 'awaiting_clarification', 'completed', 'blocked', "
    "'interrupted', 'cancelled', 'limit_reached')"
)
_EVENT_TYPES = (
    "('run_started', 'tool_call', 'policy_decision', 'authentication', 'approval_requested', "
    "'approval_decided', 'clarification_requested', 'clarification_answered', 'oracle_signal', "
    "'finding_submitted', 'record_created', 'knowledge_proposed', 'limit_hit', 'run_ended')"
)
_RUN_ID = (
    "'run_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]_"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]'"
)

STATEMENTS = (
    # issue_snapshots vem antes de runs porque runs referencia a tabela.
    f"""
    CREATE TABLE issue_snapshots (
        id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        repository TEXT NOT NULL CHECK (length(trim(repository)) > 0),
        issue_number INTEGER NOT NULL CHECK (issue_number > 0),
        content_sha256 TEXT NOT NULL CHECK {_hex64("content_sha256")},
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK ({_json("payload_json")}),
        captured_at TEXT NOT NULL,
        UNIQUE (project_id, repository, issue_number, content_sha256)
    )
    """,
    "CREATE INDEX idx_issue_snapshots_issue "
    "ON issue_snapshots (project_id, repository, issue_number, captured_at)",
    "CREATE INDEX idx_issue_snapshots_content ON issue_snapshots (content_sha256)",
    f"""
    CREATE TABLE runs (
        id TEXT NOT NULL PRIMARY KEY CHECK (id GLOB {_RUN_ID}),
        project_id TEXT NOT NULL,
        profile TEXT NOT NULL,
        environment_url TEXT NOT NULL,
        tenant TEXT NOT NULL,
        mission_source TEXT NOT NULL CHECK (mission_source IN ('text', 'file', 'issue')),
        mission_text TEXT NULL,
        mission_snapshot_path TEXT NULL,
        mission_file_sha256 TEXT NULL
            CHECK (mission_file_sha256 IS NULL OR {_hex64("mission_file_sha256")}),
        issue_snapshot_id TEXT NULL REFERENCES issue_snapshots (id),
        started_at TEXT NOT NULL,
        ended_at TEXT NULL,
        status TEXT NOT NULL CHECK (status IN {_STATUSES}),
        status_reason TEXT NULL,
        owner_pid INTEGER NOT NULL,
        executor_name TEXT NOT NULL,
        executor_version TEXT NULL,
        lastro_version TEXT NOT NULL,
        skill_bundle_version TEXT NULL,
        config_snapshot_json TEXT NOT NULL CHECK ({_json("config_snapshot_json")}),
        analyzed_code_version TEXT NULL,
        executed_system_version TEXT NULL,
        CHECK (
            (mission_source = 'text' AND mission_text IS NOT NULL
                AND mission_snapshot_path IS NULL AND mission_file_sha256 IS NULL)
            OR (mission_source = 'file' AND mission_text IS NULL
                AND mission_snapshot_path IS NOT NULL AND mission_file_sha256 IS NOT NULL)
            OR (mission_source = 'issue' AND issue_snapshot_id IS NOT NULL)
        ),
        CHECK ((status IN {_TERMINAL}) = (ended_at IS NOT NULL))
    )
    """,
    "CREATE INDEX idx_runs_started_at ON runs (started_at)",
    "CREATE INDEX idx_runs_status_started_at ON runs (status, started_at)",
    "CREATE INDEX idx_runs_profile_started_at ON runs (profile, started_at)",
    "CREATE INDEX idx_runs_issue_snapshot ON runs (issue_snapshot_id)",
    f"""
    CREATE TRIGGER runs_terminal_is_immutable
    BEFORE UPDATE ON runs
    WHEN OLD.status IN {_TERMINAL}
    BEGIN
        SELECT RAISE(ABORT, 'terminal run is immutable');
    END
    """,
    f"""
    CREATE TABLE events (
        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs (id),
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        timestamp TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN {_EVENT_TYPES}),
        tool_name TEXT NULL,
        arguments_sha256 TEXT NULL
            CHECK (arguments_sha256 IS NULL OR {_hex64("arguments_sha256")}),
        arguments_preview TEXT NULL
            CHECK (arguments_preview IS NULL OR length(arguments_preview) <= 200),
        policy_decision TEXT NULL
            CHECK (policy_decision IS NULL
                OR policy_decision IN ('allow', 'deny', 'require_approval')),
        correlation_id TEXT NULL,
        idempotency_key TEXT NULL,
        payload_json TEXT NULL CHECK (payload_json IS NULL OR {_json("payload_json")}),
        result_json TEXT NULL CHECK (result_json IS NULL OR {_json("result_json")}),
        CHECK (
            event_type <> 'authentication' OR (
                payload_json IS NOT NULL
                AND json_type(payload_json, '$.subtype') = 'text'
                AND length(trim(json_extract(payload_json, '$.subtype'))) > 0
            )
        )
    )
    """,
    "CREATE UNIQUE INDEX idx_events_run_sequence ON events (run_id, sequence)",
    "CREATE INDEX idx_events_run_type ON events (run_id, event_type)",
    "CREATE INDEX idx_events_correlation ON events (correlation_id)",
    "CREATE INDEX idx_events_timestamp ON events (timestamp)",
    "CREATE UNIQUE INDEX idx_events_idempotency ON events (run_id, event_type, idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
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
    f"""
    CREATE TABLE artifacts (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        relative_path TEXT NOT NULL CHECK (
            length(relative_path) > 0
            AND substr(relative_path, 1, 1) <> '/'
            AND instr(relative_path, char(92)) = 0
            AND instr(relative_path, ':') = 0
            AND relative_path NOT GLOB '*//*'
            AND relative_path <> '..'
            AND relative_path NOT GLOB '../*'
            AND relative_path NOT GLOB '*/../*'
            AND relative_path NOT GLOB '*/..'
            AND relative_path NOT GLOB './*'
            AND relative_path NOT GLOB '*/./*'
            AND relative_path NOT GLOB '*/'
        ),
        artifact_type TEXT NOT NULL
            CHECK (artifact_type IN ('screenshot', 'video', 'report', 'snapshot')),
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        sha256 TEXT NOT NULL CHECK {_hex64("sha256")},
        created_at TEXT NOT NULL,
        UNIQUE (run_id, relative_path)
    )
    """,
    """
    CREATE TRIGGER artifacts_are_immutable_update
    BEFORE UPDATE ON artifacts
    BEGIN
        SELECT RAISE(ABORT, 'artifacts are immutable');
    END
    """,
    f"""
    CREATE TABLE criteria (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        outcome TEXT NULL
            CHECK (outcome IS NULL OR outcome IN ('met', 'not_met', 'blocked', 'inconclusive')),
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK ({_json("payload_json")}),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_criteria_run_outcome ON criteria (run_id, outcome)",
    "CREATE INDEX idx_criteria_run ON criteria (run_id)",
    f"""
    CREATE TABLE findings (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        classification TEXT NULL CHECK (
            classification IS NULL
            OR classification IN ('suspected', 'confirmed', 'inconclusive')
        ),
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK ({_json("payload_json")}),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_findings_run_classification ON findings (run_id, classification)",
    "CREATE INDEX idx_findings_run ON findings (run_id)",
    f"""
    CREATE TABLE approvals (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        action_fingerprint TEXT NOT NULL CHECK {_hex64("action_fingerprint")},
        decision TEXT NULL
            CHECK (decision IS NULL OR decision IN ('approve', 'skip', 'end_run')),
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK ({_json("payload_json")}),
        requested_at TEXT NOT NULL,
        decided_at TEXT NULL,
        CHECK ((decision IS NULL) = (decided_at IS NULL))
    )
    """,
    "CREATE INDEX idx_approvals_run_decision ON approvals (run_id, decision)",
    "CREATE INDEX idx_approvals_fingerprint ON approvals (action_fingerprint)",
    "CREATE INDEX idx_approvals_pending ON approvals (run_id, requested_at) WHERE decision IS NULL",
    f"""
    CREATE TABLE created_records (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        entity_type TEXT NOT NULL CHECK (length(trim(entity_type)) > 0),
        system_identifier TEXT NOT NULL CHECK (length(trim(system_identifier)) > 0),
        tenant TEXT NOT NULL CHECK (length(trim(tenant)) > 0),
        cleanup_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (cleanup_status IN ('pending', 'cleaned_manually', 'not_required')),
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK ({_json("payload_json")}),
        created_at TEXT NOT NULL,
        cleaned_at TEXT NULL,
        CHECK (cleanup_status <> 'pending' OR cleaned_at IS NULL)
    )
    """,
    "CREATE INDEX idx_created_records_run_cleanup ON created_records (run_id, cleanup_status)",
    "CREATE INDEX idx_created_records_cleanup_created "
    "ON created_records (cleanup_status, created_at)",
    "CREATE INDEX idx_created_records_tenant_entity ON created_records (tenant, entity_type)",
    f"""
    CREATE TABLE knowledge (
        id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        run_id TEXT NULL REFERENCES runs (id),
        validation_state TEXT NULL CHECK (
            validation_state IS NULL
            OR validation_state IN ('observed', 'user_provided', 'contested')
        ),
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK ({_json("payload_json")}),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_knowledge_project_state ON knowledge (project_id, validation_state)",
    "CREATE INDEX idx_knowledge_run ON knowledge (run_id)",
    "CREATE INDEX idx_knowledge_updated ON knowledge (updated_at)",
    f"""
    CREATE TABLE knowledge_history (
        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        knowledge_id TEXT NOT NULL REFERENCES knowledge (id),
        run_id TEXT NULL REFERENCES runs (id),
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK ({_json("payload_json")}),
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_knowledge_history_entry ON knowledge_history (knowledge_id, created_at)",
    "CREATE INDEX idx_knowledge_history_run ON knowledge_history (run_id)",
    f"""
    CREATE TABLE eval_results (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NULL REFERENCES runs (id),
        suite_id TEXT NOT NULL CHECK (length(trim(suite_id)) > 0),
        condition TEXT NOT NULL CHECK (length(trim(condition)) > 0),
        executor_name TEXT NOT NULL CHECK (length(trim(executor_name)) > 0),
        executor_version TEXT NULL,
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK ({_json("payload_json")}),
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_eval_results_suite ON eval_results (suite_id, condition, created_at)",
    "CREATE INDEX idx_eval_results_run ON eval_results (run_id)",
    "CREATE INDEX idx_eval_results_executor ON eval_results (executor_name, executor_version)",
)
