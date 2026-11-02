"""Revisões imutáveis do plano da missão, pedidos de esclarecimento e critérios estáveis."""

VERSION = 4
NAME = "mission_plans"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"

STATEMENTS = (
    f"""
    CREATE TABLE mission_plans (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        content_sha256 TEXT NOT NULL CHECK {_HEX64.format("content_sha256")},
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        revision_reason TEXT NOT NULL DEFAULT 'initial'
            CHECK (revision_reason IN ('initial', 'clarification')),
        clarification_id TEXT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (run_id, revision),
        UNIQUE (run_id, content_sha256),
        CHECK ((revision_reason = 'clarification') = (clarification_id IS NOT NULL))
    )
    """,
    "CREATE INDEX idx_mission_plans_run_revision ON mission_plans(run_id, revision DESC)",
    """
    CREATE TABLE clarifications (
        id TEXT NOT NULL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        mission_plan_id TEXT NOT NULL REFERENCES mission_plans(id),
        gap_id TEXT NOT NULL CHECK (length(gap_id) > 0),
        question TEXT NOT NULL CHECK (length(question) BETWEEN 1 AND 1000),
        why_it_matters TEXT NOT NULL CHECK (length(why_it_matters) BETWEEN 1 AND 1000),
        options_json TEXT NOT NULL DEFAULT '[]'
            CHECK (json_valid(options_json) AND json_type(options_json) = 'array'),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'answered', 'skipped')),
        answer_json TEXT NULL CHECK (answer_json IS NULL OR json_valid(answer_json)),
        requested_at TEXT NOT NULL,
        answered_at TEXT NULL,
        CHECK ((status = 'pending') = (answered_at IS NULL)),
        CHECK ((status = 'answered') = (answer_json IS NOT NULL))
    )
    """,
    "CREATE UNIQUE INDEX idx_clarifications_pending_gap "
    "ON clarifications(run_id, gap_id) WHERE status = 'pending'",
    "CREATE INDEX idx_clarifications_run_status ON clarifications(run_id, status)",
    "CREATE INDEX idx_clarifications_plan ON clarifications(mission_plan_id)",
    "CREATE UNIQUE INDEX idx_criteria_run_normalized "
    "ON criteria(run_id, json_extract(payload_json, '$.normalized_sha256')) "
    "WHERE json_type(payload_json, '$.normalized_sha256') = 'text'",
)
