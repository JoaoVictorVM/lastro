"""Avaliação: suites, trials, tentativas, julgamentos, revisões, medidas, agregados e orçamento.

Migration aditiva: nenhuma tabela publicada muda. `run_id` e `finding_ref` apontam para o banco
isolado de cada tentativa, por isso não têm FK para `runs`/`findings` deste histórico.
"""

VERSION = 13
NAME = "evaluation"

_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"
_HEX64_NULL = "({0} IS NULL OR " + _HEX64 + ")"
_JSON = "(json_valid({0}))"
_JSON_NULL = "({0} IS NULL OR json_valid({0}))"
_REL_PATH = "({0} NOT LIKE '/%' AND {0} NOT LIKE '%..%' AND {0} NOT LIKE '_:%')"
_REL_PATH_NULL = "({0} IS NULL OR " + _REL_PATH + ")"
_CONDITIONS = "('baseline', 'full', '-heuristics', '-oracles', '-reproduction', '-memory')"
_DECISIONS = "('match', 'no_match', 'uncertain')"

STATEMENTS = (
    f"""
    CREATE TABLE eval_suites (
        id TEXT NOT NULL PRIMARY KEY,
        suite_name TEXT NOT NULL CHECK (length(suite_name) BETWEEN 1 AND 64),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        suite_sha256 TEXT NOT NULL CHECK {_HEX64.format("suite_sha256")},
        manifest_json TEXT NOT NULL CHECK {_JSON.format("manifest_json")},
        catalog_sha256 TEXT NOT NULL CHECK {_HEX64.format("catalog_sha256")},
        rubric_sha256 TEXT NOT NULL CHECK {_HEX64.format("rubric_sha256")},
        executor_name TEXT NOT NULL CHECK (executor_name IN ('claude-code', 'codex', 'native')),
        executor_version TEXT NOT NULL CHECK (length(executor_version) > 0),
        model_identity_json TEXT NOT NULL CHECK {_JSON.format("model_identity_json")},
        seed INTEGER NOT NULL CHECK (seed BETWEEN 0 AND 2147483647),
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
            'pending', 'running', 'paused', 'awaiting_review', 'completed', 'cancelled'
        )),
        pause_reason TEXT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        ended_at TEXT NULL,
        lease_token TEXT NULL,
        lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
        lease_expires_at TEXT NULL,
        owner_json TEXT NULL CHECK {_JSON_NULL.format("owner_json")},
        current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
        CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))
    )
    """,
    f"""
    CREATE TABLE eval_trials (
        id TEXT NOT NULL PRIMARY KEY,
        suite_id TEXT NOT NULL REFERENCES eval_suites (id) ON DELETE RESTRICT,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
        condition TEXT NOT NULL CHECK (condition IN {_CONDITIONS}),
        scenario TEXT NOT NULL CHECK (length(scenario) BETWEEN 1 AND 64),
        variant TEXT NOT NULL CHECK (length(variant) BETWEEN 1 AND 64),
        repetition INTEGER NOT NULL CHECK (repetition >= 1),
        role TEXT NOT NULL CHECK (role IN ('measured', 'warmup')),
        parent_trial_id TEXT NULL REFERENCES eval_trials (id) ON DELETE RESTRICT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
            'pending', 'preparing', 'running', 'collecting', 'valid', 'invalid', 'paused',
            'not_applicable'
        )),
        condition_sha256 TEXT NOT NULL CHECK {_HEX64.format("condition_sha256")},
        memory_sha256 TEXT NULL CHECK {_HEX64_NULL.format("memory_sha256")},
        selected_attempt_id TEXT NULL REFERENCES eval_attempts (id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        UNIQUE (suite_id, ordinal),
        UNIQUE (suite_id, condition, scenario, variant, repetition, role),
        CHECK (parent_trial_id IS NULL OR parent_trial_id <> id)
    )
    """,
    "CREATE INDEX eval_trials_resume ON eval_trials (suite_id, status, ordinal)",
    f"""
    CREATE TABLE eval_attempts (
        id TEXT NOT NULL PRIMARY KEY,
        trial_id TEXT NOT NULL REFERENCES eval_trials (id) ON DELETE RESTRICT,
        attempt INTEGER NOT NULL CHECK (attempt >= 1),
        status TEXT NOT NULL CHECK (status IN ('running', 'valid', 'invalid', 'paused')),
        reason TEXT NULL,
        run_id TEXT NULL,
        history_relative_path TEXT NOT NULL CHECK {_REL_PATH.format("history_relative_path")},
        executor_identity_json TEXT NOT NULL CHECK {_JSON.format("executor_identity_json")},
        started_at TEXT NULL,
        ended_at TEXT NULL,
        reset_json TEXT NULL CHECK {_JSON_NULL.format("reset_json")},
        version_json TEXT NULL CHECK {_JSON_NULL.format("version_json")},
        snapshot_relative_path TEXT NULL CHECK {_REL_PATH_NULL.format("snapshot_relative_path")},
        snapshot_sha256 TEXT NULL CHECK {_HEX64_NULL.format("snapshot_sha256")},
        access_start INTEGER NULL,
        access_end INTEGER NULL,
        access_sha256 TEXT NULL CHECK {_HEX64_NULL.format("access_sha256")},
        usage_json TEXT NULL CHECK {_JSON_NULL.format("usage_json")},
        observed_cost_nusd INTEGER NULL
            CHECK (observed_cost_nusd IS NULL OR observed_cost_nusd >= 0),
        logs_relative_path TEXT NULL CHECK {_REL_PATH_NULL.format("logs_relative_path")},
        UNIQUE (trial_id, attempt),
        CHECK ((access_start IS NULL) = (access_end IS NULL)),
        CHECK (access_start IS NULL OR (access_start >= 0 AND access_start <= access_end)),
        CHECK ((snapshot_relative_path IS NULL) = (snapshot_sha256 IS NULL))
    )
    """,
    "CREATE INDEX eval_attempts_trial_status ON eval_attempts (trial_id, status)",
    """
    CREATE TRIGGER eval_trials_selected_attempt_insert
    BEFORE INSERT ON eval_trials
    WHEN NEW.selected_attempt_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'selected attempt belongs to another trial');
    END
    """,
    """
    CREATE TRIGGER eval_trials_selected_attempt_update
    BEFORE UPDATE OF selected_attempt_id ON eval_trials
    WHEN NEW.selected_attempt_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM eval_attempts
        WHERE id = NEW.selected_attempt_id AND trial_id = NEW.id
    )
    BEGIN
        SELECT RAISE(ABORT, 'selected attempt belongs to another trial');
    END
    """,
    """
    CREATE TRIGGER eval_trials_parent_same_suite
    BEFORE INSERT ON eval_trials
    WHEN NEW.parent_trial_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM eval_trials
        WHERE id = NEW.parent_trial_id AND suite_id = NEW.suite_id
            AND role = 'warmup' AND ordinal < NEW.ordinal
    )
    BEGIN
        SELECT RAISE(ABORT, 'parent trial must be an earlier warmup of the same suite');
    END
    """,
    f"""
    CREATE TABLE eval_judgments (
        id TEXT NOT NULL PRIMARY KEY,
        attempt_id TEXT NOT NULL REFERENCES eval_attempts (id) ON DELETE RESTRICT,
        finding_ref TEXT NOT NULL CHECK (length(finding_ref) BETWEEN 1 AND 200),
        input_sha256 TEXT NOT NULL CHECK {_HEX64.format("input_sha256")},
        judge_identity_json TEXT NOT NULL CHECK {_JSON.format("judge_identity_json")},
        rubric_sha256 TEXT NOT NULL CHECK {_HEX64.format("rubric_sha256")},
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
            'pending', 'running', 'judged', 'manual_required', 'reviewed'
        )),
        parse_attempts INTEGER NOT NULL DEFAULT 0 CHECK (parse_attempts BETWEEN 0 AND 2),
        decision TEXT NULL CHECK (decision IS NULL OR decision IN {_DECISIONS}),
        bug_id TEXT NULL,
        reason TEXT NULL,
        output_json TEXT NULL CHECK {_JSON_NULL.format("output_json")},
        usage_json TEXT NULL CHECK {_JSON_NULL.format("usage_json")},
        observed_cost_nusd INTEGER NULL
            CHECK (observed_cost_nusd IS NULL OR observed_cost_nusd >= 0),
        created_at TEXT NOT NULL,
        completed_at TEXT NULL,
        UNIQUE (attempt_id, finding_ref, input_sha256, rubric_sha256),
        CHECK (decision IS NULL OR decision <> 'match' OR bug_id IS NOT NULL),
        CHECK (decision IS NULL OR decision <> 'no_match' OR bug_id IS NULL),
        CHECK (status <> 'judged' OR decision IS NOT NULL)
    )
    """,
    "CREATE INDEX eval_judgments_status ON eval_judgments (status, attempt_id)",
    f"""
    CREATE TABLE eval_review_items (
        id TEXT NOT NULL PRIMARY KEY,
        suite_id TEXT NOT NULL REFERENCES eval_suites (id) ON DELETE RESTRICT,
        judgment_id TEXT NOT NULL REFERENCES eval_judgments (id) ON DELETE RESTRICT,
        manifest_id TEXT NOT NULL CHECK (length(manifest_id) BETWEEN 1 AND 128),
        input_sha256 TEXT NOT NULL CHECK {_HEX64.format("input_sha256")},
        sampled INTEGER NOT NULL DEFAULT 0 CHECK (sampled IN (0, 1)),
        selection_seed INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (manifest_id, judgment_id)
    )
    """,
    "CREATE INDEX eval_review_items_sampled ON eval_review_items (suite_id, sampled)",
    f"""
    CREATE TABLE eval_reviews (
        id TEXT NOT NULL PRIMARY KEY,
        review_item_id TEXT NOT NULL REFERENCES eval_review_items (id) ON DELETE RESTRICT,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        previous_review_id TEXT NULL REFERENCES eval_reviews (id) ON DELETE RESTRICT,
        reviewer TEXT NOT NULL CHECK (length(reviewer) BETWEEN 1 AND 200),
        decision TEXT NOT NULL CHECK (decision IN {_DECISIONS}),
        bug_id TEXT NULL,
        reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
        payload_sha256 TEXT NOT NULL CHECK {_HEX64.format("payload_sha256")},
        created_at TEXT NOT NULL,
        UNIQUE (review_item_id, revision),
        UNIQUE (review_item_id, payload_sha256),
        CHECK (decision <> 'match' OR bug_id IS NOT NULL),
        CHECK (decision <> 'no_match' OR bug_id IS NULL)
    )
    """,
    """
    CREATE TRIGGER eval_reviews_append_only_update
    BEFORE UPDATE ON eval_reviews
    BEGIN
        SELECT RAISE(ABORT, 'eval_reviews is append-only');
    END
    """,
    """
    CREATE TRIGGER eval_reviews_append_only_delete
    BEFORE DELETE ON eval_reviews
    BEGIN
        SELECT RAISE(ABORT, 'eval_reviews is append-only');
    END
    """,
    f"""
    CREATE TABLE eval_measurements (
        id TEXT NOT NULL PRIMARY KEY,
        attempt_id TEXT NOT NULL REFERENCES eval_attempts (id) ON DELETE RESTRICT,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        metric TEXT NOT NULL CHECK (length(metric) BETWEEN 1 AND 128),
        unit TEXT NOT NULL CHECK (length(unit) BETWEEN 1 AND 32),
        value REAL NULL,
        numerator REAL NULL,
        denominator REAL NULL CHECK (denominator IS NULL OR denominator >= 0),
        unavailable_reason TEXT NULL,
        complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
        source_refs_json TEXT NOT NULL CHECK {_JSON.format("source_refs_json")},
        created_at TEXT NOT NULL,
        UNIQUE (attempt_id, revision, metric),
        CHECK (value IS NOT NULL OR unavailable_reason IS NOT NULL)
    )
    """,
    "CREATE INDEX eval_measurements_metric ON eval_measurements (metric, attempt_id)",
    # `eval_results` foi reservada na v001 e é estendida aqui, sem recriar a tabela nem perder
    # linhas: colunas do agregado, unicidade por índice e vínculos por trigger.
    "ALTER TABLE eval_results ADD COLUMN revision INTEGER NULL "
    "CHECK (revision IS NULL OR revision >= 1)",
    f"ALTER TABLE eval_results ADD COLUMN stratum_key TEXT NULL "
    f"CHECK (stratum_key IS NULL OR {_HEX64.format('stratum_key')})",
    "ALTER TABLE eval_results ADD COLUMN stratum_json TEXT NULL "
    "CHECK (stratum_json IS NULL OR json_valid(stratum_json))",
    "ALTER TABLE eval_results ADD COLUMN metric TEXT NULL "
    "CHECK (metric IS NULL OR length(metric) BETWEEN 1 AND 128)",
    "ALTER TABLE eval_results ADD COLUMN unit TEXT NULL "
    "CHECK (unit IS NULL OR length(unit) BETWEEN 1 AND 32)",
    "ALTER TABLE eval_results ADD COLUMN n INTEGER NULL CHECK (n IS NULL OR n >= 0)",
    "ALTER TABLE eval_results ADD COLUMN planned_n INTEGER NULL "
    "CHECK (planned_n IS NULL OR (planned_n >= 0 AND n <= planned_n))",
    "ALTER TABLE eval_results ADD COLUMN invalid_n INTEGER NULL "
    "CHECK (invalid_n IS NULL OR invalid_n >= 0)",
    "ALTER TABLE eval_results ADD COLUMN excluded_n INTEGER NULL "
    "CHECK (excluded_n IS NULL OR excluded_n >= 0)",
    "ALTER TABLE eval_results ADD COLUMN mean REAL NULL CHECK (mean IS NULL OR n >= 1)",
    "ALTER TABLE eval_results ADD COLUMN sample_stddev REAL NULL "
    "CHECK (sample_stddev IS NULL OR n >= 2)",
    "ALTER TABLE eval_results ADD COLUMN complete INTEGER NOT NULL DEFAULT 0 "
    "CHECK (complete IN (0, 1))",
    "ALTER TABLE eval_results ADD COLUMN limitations_json TEXT NOT NULL DEFAULT '[]' "
    "CHECK (json_valid(limitations_json))",
    f"ALTER TABLE eval_results ADD COLUMN inputs_sha256 TEXT NULL "
    f"CHECK (inputs_sha256 IS NULL OR {_HEX64.format('inputs_sha256')})",
    "CREATE UNIQUE INDEX eval_results_stratum_metric "
    "ON eval_results (suite_id, revision, stratum_key, metric) WHERE revision IS NOT NULL",
    "CREATE INDEX eval_results_revision ON eval_results (suite_id, revision)",
    """
    CREATE TRIGGER eval_results_aggregate_complete
    BEFORE INSERT ON eval_results
    WHEN NEW.revision IS NOT NULL AND (
        NEW.stratum_key IS NULL OR NEW.stratum_json IS NULL OR NEW.metric IS NULL
        OR NEW.unit IS NULL OR NEW.n IS NULL OR NEW.planned_n IS NULL
        OR NEW.invalid_n IS NULL OR NEW.excluded_n IS NULL OR NEW.inputs_sha256 IS NULL
        OR NOT EXISTS (SELECT 1 FROM eval_suites WHERE id = NEW.suite_id)
    )
    BEGIN
        SELECT RAISE(ABORT, 'aggregate result is incomplete or has no suite');
    END
    """,
    """
    CREATE TRIGGER eval_results_aggregate_immutable
    BEFORE UPDATE ON eval_results
    WHEN OLD.revision IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'aggregate results are immutable');
    END
    """,
    f"""
    CREATE TABLE eval_events (
        id INTEGER NOT NULL PRIMARY KEY,
        suite_id TEXT NOT NULL REFERENCES eval_suites (id) ON DELETE RESTRICT,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        trial_id TEXT NULL REFERENCES eval_trials (id) ON DELETE RESTRICT,
        event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 64),
        payload_json TEXT NOT NULL CHECK {_JSON.format("payload_json")},
        idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
        timestamp TEXT NOT NULL,
        UNIQUE (suite_id, sequence),
        UNIQUE (suite_id, idempotency_key)
    )
    """,
    "CREATE INDEX eval_events_time ON eval_events (suite_id, timestamp)",
    """
    CREATE TRIGGER eval_events_append_only_update
    BEFORE UPDATE ON eval_events
    BEGIN
        SELECT RAISE(ABORT, 'eval_events is append-only');
    END
    """,
    """
    CREATE TABLE eval_budget_entries (
        id TEXT NOT NULL PRIMARY KEY,
        suite_id TEXT NOT NULL REFERENCES eval_suites (id) ON DELETE RESTRICT,
        attempt_id TEXT NULL REFERENCES eval_attempts (id) ON DELETE RESTRICT,
        component TEXT NOT NULL CHECK (component IN (
            'exploration', 'reproduction', 'warmup', 'judge', 'retrieval'
        )),
        request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 200),
        status TEXT NOT NULL CHECK (status IN (
            'reserved', 'sent', 'settled', 'uncertain', 'released'
        )),
        reserved_nusd INTEGER NOT NULL CHECK (reserved_nusd >= 0),
        actual_nusd INTEGER NULL CHECK (actual_nusd IS NULL OR actual_nusd >= 0),
        source_ref TEXT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (suite_id, request_key)
    )
    """,
    "CREATE INDEX eval_budget_entries_status ON eval_budget_entries (suite_id, status)",
)
