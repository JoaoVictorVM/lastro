"""Contexto de mudança: snapshots offline, captura congelada por run, fontes e três eventos.

`issue_snapshots` e `events` são reconstruídas dentro da transação com as FKs adiadas: a remoção
da tabela pai deixa os filhos órfãos só até a reinserção das mesmas linhas, e o COMMIT falha (e a
versão inteira é revertida) se alguma referência continuar sem pai. As FKs nunca são desligadas.
Ids, sequências, índices, triggers e CHECKs anteriores são preservados.
"""

VERSION = 9
NAME = "change_context"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"

_EVENT_TYPES = (
    "('run_started', 'tool_call', 'policy_decision', 'authentication', 'approval_requested', "
    "'approval_decided', 'clarification_requested', 'clarification_answered', 'oracle_signal', "
    "'finding_submitted', 'record_created', 'knowledge_proposed', 'limit_hit', 'run_ended', "
    "'notification_degraded', 'native_started', 'model_call_started', 'model_call_completed', "
    "'model_call_failed', 'agent_failure', 'change_context_collected', 'security_event', "
    "'version_observed')"
)

_RELATIVE_PATH = """(
    length(relative_path) > 0
    AND substr(relative_path, 1, 1) <> '/'
    AND instr(relative_path, char(92)) = 0
    AND instr(relative_path, ':') = 0
    AND instr(relative_path, char(0)) = 0
    AND relative_path NOT GLOB '*//*'
    AND relative_path <> '..'
    AND relative_path NOT GLOB '../*'
    AND relative_path NOT GLOB '*/../*'
    AND relative_path NOT GLOB '*/..'
)"""


def _immutable(table: str, message: str) -> tuple[str, str]:
    return (
        f"""
    CREATE TRIGGER {table}_are_immutable_update
    BEFORE UPDATE ON {table}
    BEGIN
        SELECT RAISE(ABORT, '{message}');
    END
    """,
        f"""
    CREATE TRIGGER {table}_are_immutable_delete
    BEFORE DELETE ON {table}
    BEGIN
        SELECT RAISE(ABORT, '{message}');
    END
    """,
    )


STATEMENTS = (
    "PRAGMA defer_foreign_keys = ON",
    "CREATE TABLE issue_snapshots_v9_copy AS SELECT * FROM issue_snapshots",
    "DROP TABLE issue_snapshots",
    f"""
    CREATE TABLE issue_snapshots (
        id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        repository TEXT NULL CHECK (repository IS NULL OR length(trim(repository)) > 0),
        issue_number INTEGER NULL CHECK (issue_number IS NULL OR issue_number > 0),
        source_kind TEXT NOT NULL DEFAULT 'github'
            CHECK (source_kind IN ('github', 'markdown', 'text')),
        source_identity TEXT NOT NULL CHECK (length(source_identity) > 0),
        content_sha256 TEXT NOT NULL CHECK {_HEX64.format("content_sha256")},
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(payload_json)),
        captured_at TEXT NOT NULL,
        CHECK (
            source_kind <> 'github'
            OR (repository IS NOT NULL AND issue_number IS NOT NULL)
        ),
        UNIQUE (project_id, source_kind, source_identity, content_sha256)
    )
    """,
    "INSERT INTO issue_snapshots (id, project_id, repository, issue_number, source_kind, "
    "source_identity, content_sha256, payload_json, captured_at) "
    "SELECT id, project_id, repository, issue_number, 'github', "
    "'github:' || repository || '#' || issue_number, content_sha256, payload_json, captured_at "
    "FROM issue_snapshots_v9_copy",
    "DROP TABLE issue_snapshots_v9_copy",
    "CREATE INDEX idx_issue_snapshots_issue "
    "ON issue_snapshots (project_id, repository, issue_number, captured_at)",
    "CREATE INDEX idx_issue_snapshots_content ON issue_snapshots (content_sha256)",
    *_immutable("issue_snapshots", "issue snapshots are immutable"),
    "CREATE TABLE events_v9_copy AS SELECT * FROM events",
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
    "result_json FROM events_v9_copy",
    "DROP TABLE events_v9_copy",
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
    f"""
    CREATE TABLE change_contexts (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL UNIQUE REFERENCES runs (id),
        issue_snapshot_id TEXT NULL REFERENCES issue_snapshots (id),
        source_state TEXT NOT NULL
            CHECK (source_state IN ('disabled', 'available', 'skipped', 'unavailable', 'partial')),
        flow_key TEXT NULL CHECK (flow_key IS NULL OR length(flow_key) BETWEEN 1 AND 200),
        captured_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK {_HEX64.format("content_sha256")},
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        manifest_artifact_id TEXT NULL REFERENCES artifacts (id)
    )
    """,
    "CREATE INDEX idx_change_contexts_flow ON change_contexts (flow_key, run_id)",
    """
    CREATE TRIGGER change_contexts_match_run
    BEFORE INSERT ON change_contexts
    WHEN NEW.issue_snapshot_id IS NOT (SELECT issue_snapshot_id FROM runs WHERE id = NEW.run_id)
        OR (NEW.manifest_artifact_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM artifacts a WHERE a.id = NEW.manifest_artifact_id
            AND a.run_id = NEW.run_id AND a.artifact_type = 'snapshot'))
    BEGIN
        SELECT RAISE(ABORT, 'change context must match its run');
    END
    """,
    *_immutable("change_contexts", "change contexts are immutable"),
    f"""
    CREATE TABLE source_files (
        id TEXT NOT NULL PRIMARY KEY,
        context_id TEXT NOT NULL REFERENCES change_contexts (id),
        relative_path TEXT NOT NULL CHECK {_RELATIVE_PATH},
        origin TEXT NOT NULL CHECK (origin IN ('commit', 'index', 'worktree', 'base')),
        revision_oid TEXT NULL,
        content_sha256 TEXT NOT NULL CHECK {_HEX64.format("content_sha256")},
        size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 0 AND 1000000),
        artifact_id TEXT NOT NULL REFERENCES artifacts (id),
        UNIQUE (context_id, relative_path, origin)
    )
    """,
    "CREATE INDEX idx_source_files_context ON source_files (context_id, id)",
    """
    CREATE TRIGGER source_files_match_run
    BEFORE INSERT ON source_files
    WHEN NOT EXISTS (
        SELECT 1 FROM artifacts a JOIN change_contexts c ON c.run_id = a.run_id
        WHERE a.id = NEW.artifact_id AND c.id = NEW.context_id AND a.artifact_type = 'snapshot'
    )
    BEGIN
        SELECT RAISE(ABORT, 'source file artifact must belong to the context run');
    END
    """,
    *_immutable("source_files", "source files are immutable"),
)
