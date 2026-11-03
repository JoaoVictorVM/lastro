"""Memória persistente: amplia as tabelas reservadas e cria filas, evidências e resultados.

`knowledge` e `knowledge_history` existem desde a v001 sem as colunas tipadas. São reconstruídas
porque o CHECK condicional de linhas novas envolve várias colunas e não cabe em ADD COLUMN. Linhas
anteriores viram `legacy = 1` e ficam fora da busca até revisão humana; nada é inferido delas.

A filha é copiada e removida antes da pai: dropar a pai com filhos presentes deixaria a violação
de FK pendente até o COMMIT. A tabela FTS5 vem por último; sem o módulo, a versão inteira volta.
"""

VERSION = 7
NAME = "persistent_memory"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"
_JSON = "json_valid({0})"

_CATEGORIES = "('screen_component', 'flow', 'rule_clarification', 'experience')"
_SOURCES = "('observation', 'user', 'documentation', 'code', 'issue')"
_STATES = "('observed', 'user_provided', 'contested')"
_OUTCOMES = "('updated', 'nothing_new', 'consolidation_pending', 'persistence_failed')"
_OPERATIONS = (
    "('created', 'evidence_added', 'contested', 'edited', 'discarded', 'revalidated', "
    "'mutation_requested', 'mutation_rejected')"
)
_ACTORS = "('system', 'agent', 'user', 'adapter')"

STATEMENTS = (
    "CREATE TABLE knowledge_history_v7_copy AS SELECT * FROM knowledge_history",
    "CREATE TABLE knowledge_v7_copy AS SELECT * FROM knowledge",
    "DROP TABLE knowledge_history",
    "DROP TABLE knowledge",
    f"""
    CREATE TABLE knowledge (
        id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        run_id TEXT NULL REFERENCES runs (id),
        validation_state TEXT NULL CHECK (
            validation_state IS NULL OR validation_state IN {_STATES}
        ),
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK ({_JSON.format("payload_json")}),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        flow TEXT NULL,
        scope_json TEXT NULL,
        scope_key TEXT NULL,
        category TEXT NULL,
        statement TEXT NULL,
        fact_json TEXT NULL,
        fact_key TEXT NULL,
        value_sha256 TEXT NULL,
        source_type TEXT NULL,
        source_ref TEXT NULL,
        system_version TEXT NULL,
        observed_at TEXT NULL,
        last_verified_at TEXT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        discarded_at TEXT NULL,
        legacy INTEGER NOT NULL DEFAULT 1 CHECK (legacy IN (0, 1)),
        CHECK (
            legacy = 1 OR (
                validation_state IS NOT NULL
                AND flow IS NOT NULL AND length(trim(flow)) > 0
                AND scope_json IS NOT NULL AND {_JSON.format("scope_json")}
                AND scope_key IS NOT NULL AND {_HEX64.format("scope_key")}
                AND category IN {_CATEGORIES}
                AND statement IS NOT NULL AND length(statement) BETWEEN 1 AND 1000
                AND fact_json IS NOT NULL AND {_JSON.format("fact_json")}
                AND fact_key IS NOT NULL AND length(fact_key) > 0
                AND value_sha256 IS NOT NULL AND {_HEX64.format("value_sha256")}
                AND source_type IN {_SOURCES}
                AND source_ref IS NOT NULL AND length(source_ref) > 0
                AND observed_at IS NOT NULL
            )
        )
    )
    """,
    "INSERT INTO knowledge (id, project_id, run_id, validation_state, payload_json, created_at, "
    "updated_at, legacy) SELECT id, project_id, run_id, validation_state, payload_json, "
    "created_at, updated_at, 1 FROM knowledge_v7_copy",
    f"""
    CREATE TABLE knowledge_history (
        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        knowledge_id TEXT NOT NULL REFERENCES knowledge (id),
        run_id TEXT NULL REFERENCES runs (id),
        payload_json TEXT NOT NULL DEFAULT '{{}}' CHECK ({_JSON.format("payload_json")}),
        created_at TEXT NOT NULL,
        operation TEXT NULL,
        actor TEXT NULL,
        revision INTEGER NULL,
        operation_key TEXT NULL UNIQUE,
        CHECK (
            (operation IS NULL AND actor IS NULL AND revision IS NULL)
            OR (operation IN {_OPERATIONS} AND actor IN {_ACTORS} AND revision > 0)
        )
    )
    """,
    "INSERT INTO knowledge_history (id, knowledge_id, run_id, payload_json, created_at) "
    "SELECT id, knowledge_id, run_id, payload_json, created_at FROM knowledge_history_v7_copy",
    "DROP TABLE knowledge_history_v7_copy",
    "DROP TABLE knowledge_v7_copy",
    "CREATE INDEX idx_knowledge_project_state ON knowledge (project_id, validation_state)",
    "CREATE INDEX idx_knowledge_run ON knowledge (run_id)",
    "CREATE INDEX idx_knowledge_updated ON knowledge (updated_at)",
    "CREATE INDEX idx_knowledge_scope_flow "
    "ON knowledge (project_id, flow, scope_key, discarded_at)",
    "CREATE INDEX idx_knowledge_fact ON knowledge (project_id, flow, scope_key, fact_key)",
    "CREATE UNIQUE INDEX uq_knowledge_active_value "
    "ON knowledge (project_id, flow, scope_key, fact_key, value_sha256) "
    "WHERE legacy = 0 AND discarded_at IS NULL",
    "CREATE INDEX idx_knowledge_verified ON knowledge (last_verified_at)",
    "CREATE INDEX idx_knowledge_history_entry ON knowledge_history (knowledge_id, created_at)",
    "CREATE INDEX idx_knowledge_history_run ON knowledge_history (run_id)",
    f"""
    CREATE TABLE knowledge_observations (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        event_id INTEGER NOT NULL REFERENCES events (id),
        route TEXT NOT NULL CHECK (length(route) > 0),
        screen_title TEXT NOT NULL,
        landmarks_json TEXT NOT NULL CHECK (
            {_JSON.format("landmarks_json")} AND json_type(landmarks_json) = 'array'
        ),
        scope_json TEXT NOT NULL CHECK ({_JSON.format("scope_json")}),
        projection_json TEXT NOT NULL CHECK ({_JSON.format("projection_json")}),
        content_sha256 TEXT NOT NULL CHECK {_HEX64.format("content_sha256")},
        observed_at TEXT NOT NULL,
        UNIQUE (run_id, event_id)
    )
    """,
    "CREATE INDEX idx_knowledge_observations_run ON knowledge_observations (run_id, observed_at)",
    f"""
    CREATE TABLE knowledge_pending (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 100),
        payload_json TEXT NOT NULL CHECK ({_JSON.format("payload_json")}),
        content_sha256 TEXT NOT NULL CHECK {_HEX64.format("content_sha256")},
        status TEXT NOT NULL CHECK (
            status IN ('pending', 'applied', 'review_required', 'rejected')
        ),
        entry_id TEXT NULL REFERENCES knowledge (id),
        reason TEXT NULL,
        created_at TEXT NOT NULL,
        processed_at TEXT NULL,
        UNIQUE (run_id, idempotency_key)
    )
    """,
    "CREATE INDEX idx_knowledge_pending_run ON knowledge_pending (run_id, status)",
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
    "CREATE INDEX idx_knowledge_evidence_entry ON knowledge_evidence (knowledge_id)",
    "CREATE INDEX idx_knowledge_evidence_run ON knowledge_evidence (run_id)",
    "CREATE UNIQUE INDEX uq_knowledge_evidence_observation "
    "ON knowledge_evidence (knowledge_id, observation_id) WHERE observation_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_knowledge_evidence_artifact "
    "ON knowledge_evidence (knowledge_id, artifact_id) WHERE artifact_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_knowledge_evidence_clarification "
    "ON knowledge_evidence (knowledge_id, clarification_id) WHERE clarification_id IS NOT NULL",
    """
    CREATE TABLE knowledge_conflicts (
        left_id TEXT NOT NULL REFERENCES knowledge (id),
        right_id TEXT NOT NULL REFERENCES knowledge (id),
        created_at TEXT NOT NULL,
        resolved_at TEXT NULL,
        resolution_reason TEXT NULL,
        PRIMARY KEY (left_id, right_id),
        CHECK (left_id < right_id)
    )
    """,
    "CREATE INDEX idx_knowledge_conflicts_right ON knowledge_conflicts (right_id)",
    f"""
    CREATE TABLE knowledge_run_refs (
        run_id TEXT NOT NULL REFERENCES runs (id),
        knowledge_id TEXT NOT NULL REFERENCES knowledge (id),
        revision INTEGER NOT NULL CHECK (revision > 0),
        snapshot_json TEXT NOT NULL CHECK ({_JSON.format("snapshot_json")}),
        accessed_at TEXT NOT NULL,
        PRIMARY KEY (run_id, knowledge_id, revision)
    )
    """,
    "CREATE INDEX idx_knowledge_run_refs_entry ON knowledge_run_refs (knowledge_id, run_id)",
    f"""
    CREATE TABLE knowledge_mutations (
        id TEXT NOT NULL PRIMARY KEY,
        knowledge_id TEXT NOT NULL REFERENCES knowledge (id),
        expected_revision INTEGER NOT NULL CHECK (expected_revision > 0),
        operation TEXT NOT NULL CHECK (operation IN ('edit', 'discard', 'revalidate')),
        payload_json TEXT NOT NULL CHECK ({_JSON.format("payload_json")}),
        status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'conflict')),
        created_at TEXT NOT NULL,
        applied_at TEXT NULL,
        reason TEXT NULL
    )
    """,
    "CREATE INDEX idx_knowledge_mutations_entry "
    "ON knowledge_mutations (knowledge_id, status, created_at)",
    f"""
    CREATE TABLE run_memory_outcomes (
        run_id TEXT NOT NULL PRIMARY KEY REFERENCES runs (id),
        outcome TEXT NOT NULL CHECK (outcome IN {_OUTCOMES}),
        revision INTEGER NOT NULL CHECK (revision > 0),
        changed_count INTEGER NOT NULL CHECK (changed_count >= 0),
        pending_count INTEGER NOT NULL CHECK (pending_count >= 0),
        updated_at TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE knowledge_consolidation_attempts (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        attempt_key TEXT NOT NULL CHECK (length(attempt_key) > 0),
        previous_outcome TEXT NULL CHECK (
            previous_outcome IS NULL OR previous_outcome IN {_OUTCOMES}
        ),
        outcome TEXT NOT NULL CHECK (outcome IN {_OUTCOMES}),
        error_code TEXT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (run_id, attempt_key)
    )
    """,
    "CREATE INDEX idx_knowledge_attempts_run "
    "ON knowledge_consolidation_attempts (run_id, created_at)",
    "CREATE VIRTUAL TABLE knowledge_fts USING fts5("
    "entry_id UNINDEXED, statement, search_text, tokenize = 'unicode61')",
)
