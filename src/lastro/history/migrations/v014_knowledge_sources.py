"""Fontes documentais: catálogo, gerações, documentos, chunks, vetores, citações e disputas.

Migration aditiva: nenhuma tabela publicada muda e nada exige fastembed ou sqlite-vec. Vetores
são BLOBs float32 em tabela ordinária; a distância só é calculada na conexão de recuperação.
A referência circular catálogo/geração usa FK simples: o catálogo nasce sem geração ativa.
"""

VERSION = 14
NAME = "knowledge_sources"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"
_JSON = "(json_valid({0}))"
_RELATIVE = """(
    length({0}) > 0
    AND substr({0}, 1, 1) <> '/'
    AND instr({0}, char(92)) = 0
    AND instr({0}, ':') = 0
    AND instr({0}, char(0)) = 0
    AND {0} NOT GLOB '*//*'
    AND {0} <> '..'
    AND {0} NOT GLOB '../*'
    AND {0} NOT GLOB '*/../*'
    AND {0} NOT GLOB '*/..'
)"""

STATEMENTS = (
    f"""
    CREATE TABLE source_generations (
        id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        status TEXT NOT NULL CHECK (status IN ('staging', 'active', 'retired')),
        fingerprint_json TEXT NOT NULL CHECK {_JSON.format("fingerprint_json")},
        fingerprint_sha256 TEXT NOT NULL CHECK {_HEX64.format("fingerprint_sha256")},
        lease_generation INTEGER NOT NULL CHECK (lease_generation > 0),
        created_at TEXT NOT NULL,
        published_at TEXT NULL,
        CHECK (status = 'staging' OR published_at IS NOT NULL),
        UNIQUE (project_id, ordinal),
        UNIQUE (id, project_id)
    )
    """,
    "CREATE INDEX idx_source_generations_project_status ON source_generations (project_id, status)",
    "CREATE UNIQUE INDEX uq_source_generations_active "
    "ON source_generations (project_id) WHERE status = 'active'",
    """
    CREATE TABLE source_catalogs (
        project_id TEXT NOT NULL PRIMARY KEY,
        active_generation_id TEXT NULL,
        revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
        lease_token TEXT NULL,
        lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
        lease_expires_at TEXT NULL,
        updated_at TEXT NOT NULL,
        CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL)),
        FOREIGN KEY (active_generation_id, project_id)
            REFERENCES source_generations (id, project_id)
    )
    """,
    f"""
    CREATE TABLE document_sources (
        id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        canonical_root TEXT NOT NULL CHECK (length(canonical_root) > 0),
        root_key TEXT NOT NULL CHECK {_HEX64.format("root_key")},
        display_label TEXT NOT NULL CHECK (length(display_label) BETWEEN 1 AND 200),
        created_at TEXT NOT NULL,
        last_sync_at TEXT NULL,
        UNIQUE (project_id, root_key),
        UNIQUE (project_id, display_label),
        UNIQUE (id, project_id)
    )
    """,
    f"""
    CREATE TABLE source_documents (
        id TEXT NOT NULL PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES document_sources (id),
        relative_path TEXT NOT NULL CHECK {_RELATIVE.format("relative_path")},
        format TEXT NOT NULL CHECK (format IN ('markdown', 'text', 'html', 'pdf')),
        file_sha256 TEXT NOT NULL CHECK {_HEX64.format("file_sha256")},
        text_sha256 TEXT NOT NULL CHECK {_HEX64.format("text_sha256")},
        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
        extracted_characters INTEGER NOT NULL CHECK (extracted_characters >= 0),
        normalized_text TEXT NOT NULL,
        extraction_fingerprint TEXT NOT NULL CHECK {_HEX64.format("extraction_fingerprint")},
        declarations_json TEXT NOT NULL DEFAULT '[]' CHECK (
            {_JSON.format("declarations_json")} AND json_type(declarations_json) = 'array'
        ),
        created_at TEXT NOT NULL,
        UNIQUE (source_id, relative_path, file_sha256, extraction_fingerprint)
    )
    """,
    f"""
    CREATE TABLE source_generation_files (
        generation_id TEXT NOT NULL REFERENCES source_generations (id),
        source_id TEXT NOT NULL REFERENCES document_sources (id),
        relative_path TEXT NOT NULL CHECK {_RELATIVE.format("relative_path")},
        document_id TEXT NULL REFERENCES source_documents (id),
        modified_at TEXT NULL,
        status TEXT NOT NULL CHECK (status IN ('ready', 'excluded', 'failed')),
        reason TEXT NULL,
        PRIMARY KEY (generation_id, source_id, relative_path),
        CHECK (
            (status = 'ready' AND document_id IS NOT NULL AND reason IS NULL)
            OR (status <> 'ready' AND document_id IS NULL AND reason IS NOT NULL)
        )
    )
    """,
    "CREATE INDEX idx_source_files_document "
    "ON source_generation_files (document_id, generation_id)",
    f"""
    CREATE TABLE source_chunks (
        id TEXT NOT NULL PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents (id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        heading TEXT NOT NULL,
        locator_json TEXT NOT NULL CHECK {_JSON.format("locator_json")},
        text TEXT NOT NULL CHECK (length(text) > 0),
        token_count INTEGER NOT NULL CHECK (token_count BETWEEN 1 AND 800),
        content_sha256 TEXT NOT NULL CHECK {_HEX64.format("content_sha256")},
        chunking_fingerprint TEXT NOT NULL CHECK {_HEX64.format("chunking_fingerprint")},
        UNIQUE (document_id, chunking_fingerprint, ordinal)
    )
    """,
    "CREATE INDEX idx_source_chunks_document ON source_chunks (document_id, ordinal)",
    """
    CREATE TABLE source_embeddings (
        chunk_id TEXT NOT NULL REFERENCES source_chunks (id),
        model_fingerprint TEXT NOT NULL CHECK (length(model_fingerprint) = 64),
        window_ordinal INTEGER NOT NULL CHECK (window_ordinal >= 0),
        token_start INTEGER NOT NULL CHECK (token_start >= 0),
        token_end INTEGER NOT NULL,
        dimension INTEGER NOT NULL CHECK (dimension = 1024),
        vector BLOB NOT NULL CHECK (typeof(vector) = 'blob' AND length(vector) = 4096),
        PRIMARY KEY (chunk_id, model_fingerprint, window_ordinal),
        CHECK (token_end > token_start)
    )
    """,
    "CREATE VIRTUAL TABLE source_chunks_fts USING fts5("
    "chunk_id UNINDEXED, heading, text, tokenize = 'unicode61')",
    f"""
    CREATE TABLE source_queries (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NULL REFERENCES runs (id),
        project_id TEXT NOT NULL,
        flow TEXT NOT NULL CHECK (length(flow) BETWEEN 1 AND 80),
        generation_id TEXT NOT NULL,
        query_text TEXT NOT NULL,
        model_fingerprint TEXT NOT NULL CHECK {_HEX64.format("model_fingerprint")},
        created_at TEXT NOT NULL,
        truncated INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1)),
        omitted_count INTEGER NOT NULL DEFAULT 0 CHECK (omitted_count >= 0)
    )
    """,
    "CREATE INDEX idx_source_queries_run ON source_queries (run_id, created_at)",
    f"""
    CREATE TABLE source_run_refs (
        id TEXT NOT NULL PRIMARY KEY,
        query_id TEXT NOT NULL REFERENCES source_queries (id),
        run_id TEXT NULL REFERENCES runs (id),
        chunk_identity TEXT NOT NULL CHECK (length(chunk_identity) > 0),
        rank INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 5),
        snapshot_json TEXT NOT NULL CHECK {_JSON.format("snapshot_json")},
        snapshot_sha256 TEXT NOT NULL CHECK {_HEX64.format("snapshot_sha256")},
        created_at TEXT NOT NULL,
        UNIQUE (query_id, rank)
    )
    """,
    "CREATE INDEX idx_source_run_refs_run ON source_run_refs (run_id, id)",
    """
    CREATE TRIGGER source_run_refs_are_immutable
    BEFORE UPDATE ON source_run_refs
    BEGIN
        SELECT RAISE(ABORT, 'source references are immutable');
    END
    """,
    f"""
    CREATE TABLE source_knowledge_refs (
        id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        knowledge_id TEXT NOT NULL REFERENCES knowledge (id),
        source_document_identity TEXT NOT NULL CHECK (length(source_document_identity) > 0),
        chunk_identity TEXT NOT NULL CHECK (length(chunk_identity) > 0),
        source_type TEXT NOT NULL CHECK (source_type = 'documentation'),
        snapshot_json TEXT NOT NULL CHECK {_JSON.format("snapshot_json")},
        snapshot_sha256 TEXT NOT NULL CHECK {_HEX64.format("snapshot_sha256")},
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL,
        UNIQUE (knowledge_id, chunk_identity)
    )
    """,
    "CREATE INDEX idx_source_knowledge_refs_entry "
    "ON source_knowledge_refs (project_id, knowledge_id, active)",
    f"""
    CREATE TABLE source_conflicts (
        id TEXT NOT NULL PRIMARY KEY,
        project_id TEXT NOT NULL,
        run_id TEXT NULL REFERENCES runs (id),
        flow TEXT NOT NULL CHECK (length(flow) BETWEEN 1 AND 80),
        left_snapshot_json TEXT NOT NULL CHECK {_JSON.format("left_snapshot_json")},
        right_snapshot_json TEXT NOT NULL CHECK {_JSON.format("right_snapshot_json")},
        pair_sha256 TEXT NOT NULL CHECK {_HEX64.format("pair_sha256")},
        assessment TEXT NOT NULL CHECK (assessment IN ('structured', 'reported')),
        explanation TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
        created_at TEXT NOT NULL,
        resolved_at TEXT NULL,
        resolution_ref TEXT NULL,
        CHECK (
            (status = 'open' AND resolved_at IS NULL AND resolution_ref IS NULL)
            OR (status = 'resolved' AND resolved_at IS NOT NULL AND resolution_ref IS NOT NULL)
        ),
        UNIQUE (project_id, flow, pair_sha256)
    )
    """,
    "CREATE INDEX idx_source_conflicts_run ON source_conflicts (run_id, created_at)",
    """
    CREATE TABLE source_conflict_entries (
        conflict_id TEXT NOT NULL REFERENCES source_conflicts (id),
        knowledge_id TEXT NOT NULL REFERENCES knowledge (id),
        PRIMARY KEY (conflict_id, knowledge_id)
    )
    """,
    "CREATE INDEX idx_source_conflict_entries_entry ON source_conflict_entries (knowledge_id)",
    f"""
    CREATE TABLE source_conflict_requests (
        run_id TEXT NOT NULL REFERENCES runs (id),
        idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 100),
        request_sha256 TEXT NOT NULL CHECK {_HEX64.format("request_sha256")},
        conflict_id TEXT NOT NULL REFERENCES source_conflicts (id),
        PRIMARY KEY (run_id, idempotency_key)
    )
    """,
    f"""
    CREATE TABLE retrieval_samples (
        id TEXT NOT NULL PRIMARY KEY,
        suite_id TEXT NOT NULL REFERENCES eval_suites (id),
        trial_id TEXT NOT NULL REFERENCES eval_trials (id),
        attempt_id TEXT NOT NULL REFERENCES eval_attempts (id),
        question_id TEXT NOT NULL CHECK (length(question_id) BETWEEN 1 AND 100),
        repetition INTEGER NOT NULL CHECK (repetition > 0),
        input_sha256 TEXT NOT NULL CHECK {_HEX64.format("input_sha256")},
        snapshot_json TEXT NOT NULL CHECK {_JSON.format("snapshot_json")},
        metrics_json TEXT NOT NULL CHECK {_JSON.format("metrics_json")},
        created_at TEXT NOT NULL,
        UNIQUE (attempt_id, question_id, repetition)
    )
    """,
    "CREATE INDEX idx_retrieval_samples_suite ON retrieval_samples (suite_id, question_id)",
    "CREATE INDEX idx_retrieval_samples_trial ON retrieval_samples (trial_id)",
)
