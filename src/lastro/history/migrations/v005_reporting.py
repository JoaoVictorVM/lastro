"""Achados estruturados, evidências, reproduções, resultados de critérios e snapshots de relatório.

`findings` já existe desde a v001 com o contrato mínimo; aqui ela só ganha colunas consultáveis.
Os campos textuais do achado ficam no payload validado pela fachada.
"""

VERSION = 5
NAME = "reporting"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"
_JSON_ARRAY = "(json_valid({0}) AND json_type({0}) = 'array')"

STATEMENTS = (
    "ALTER TABLE findings ADD COLUMN failure_domain TEXT NULL "
    "CHECK (failure_domain IS NULL OR failure_domain IN ('system', 'agent', 'tool'))",
    "ALTER TABLE findings ADD COLUMN origin TEXT NULL "
    "CHECK (origin IS NULL OR origin IN ('agent', 'oracle'))",
    "ALTER TABLE findings ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)",
    "CREATE INDEX idx_findings_run_domain ON findings(run_id, classification, failure_domain)",
    "CREATE UNIQUE INDEX idx_findings_run_content "
    "ON findings(run_id, json_extract(payload_json, '$.content_sha256')) "
    "WHERE json_type(payload_json, '$.content_sha256') = 'text'",
    """
    CREATE TABLE finding_steps (
        finding_id TEXT NOT NULL REFERENCES findings(id),
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        instruction TEXT NOT NULL CHECK (length(trim(instruction)) > 0),
        PRIMARY KEY (finding_id, ordinal)
    )
    """,
    """
    CREATE TABLE finding_evidence (
        finding_id TEXT NOT NULL REFERENCES findings(id),
        artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        purpose TEXT NOT NULL
            CHECK (purpose IN ('state', 'before', 'after', 'error', 'timing')),
        PRIMARY KEY (finding_id, artifact_id),
        UNIQUE (finding_id, ordinal)
    )
    """,
    "CREATE INDEX idx_finding_evidence_artifact ON finding_evidence(artifact_id)",
    f"""
    CREATE TABLE finding_reproduction_attempts (
        id TEXT NOT NULL PRIMARY KEY,
        finding_id TEXT NOT NULL REFERENCES findings(id),
        child_run_id TEXT NOT NULL REFERENCES runs(id),
        outcome TEXT NOT NULL
            CHECK (outcome IN ('reproduced', 'not_reproduced', 'inconclusive')),
        evidence_json TEXT NOT NULL DEFAULT '[]' CHECK {_JSON_ARRAY.format("evidence_json")},
        executor TEXT NOT NULL CHECK (length(trim(executor)) > 0),
        attempted_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_reproduction_attempts_finding "
    "ON finding_reproduction_attempts(finding_id, attempted_at)",
    f"""
    CREATE TABLE criterion_results (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        criterion_id TEXT NOT NULL REFERENCES criteria(id),
        revision INTEGER NOT NULL CHECK (revision > 0),
        outcome TEXT NOT NULL CHECK (outcome IN ('met', 'not_met', 'blocked', 'inconclusive')),
        evidence_json TEXT NOT NULL DEFAULT '[]' CHECK {_JSON_ARRAY.format("evidence_json")},
        note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 4000),
        source TEXT NOT NULL CHECK (source IN ('agent', 'system')),
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0 AND length(reason) <= 200),
        content_sha256 TEXT NOT NULL CHECK {_HEX64.format("content_sha256")},
        created_at TEXT NOT NULL,
        UNIQUE (criterion_id, revision),
        CHECK (outcome <> 'met' OR json_array_length(evidence_json) > 0)
    )
    """,
    "CREATE INDEX idx_criterion_results_current "
    "ON criterion_results(run_id, criterion_id, revision DESC)",
    """
    CREATE TRIGGER criterion_results_are_immutable
    BEFORE UPDATE ON criterion_results
    BEGIN
        SELECT RAISE(ABORT, 'criterion results are immutable');
    END
    """,
    f"""
    CREATE TABLE report_snapshots (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        kind TEXT NOT NULL CHECK (kind IN ('final', 'partial', 'public')),
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
    "CREATE INDEX idx_report_snapshots_run_status ON report_snapshots(run_id, status)",
)
