"""Observabilidade: contexto congelado por run, fila de spans derivada do histórico e lease.

Nenhuma tabela publicada é alterada. Ausência de linha em `observability_runs` significa run
desligada ou anterior à feature; não há backfill que habilite runs antigas.
"""

VERSION = 12
NAME = "observability"

_HEX = "(length({0}) = {1} AND {0} NOT GLOB '*[^0-9a-f]*' AND {0} <> '{2}')"
_HEX32 = _HEX.format("{0}", 32, "0" * 32)
_HEX16 = _HEX.format("{0}", 16, "0" * 16)
_HEX64 = "(length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*')"
_ERROR_CODES = (
    "('obs_config_invalid', 'obs_extra_missing', 'obs_credentials_missing', 'obs_disabled', "
    "'obs_destination_mismatch', 'export_timeout', 'export_unavailable', 'export_rate_limited', "
    "'export_unauthorized', 'export_rejected', 'export_partial', 'obs_suspended', "
    "'obs_lease_busy', 'obs_projection_failed', 'obs_storage_unavailable')"
)

STATEMENTS = (
    f"""
    CREATE TABLE observability_runs (
        run_id TEXT NOT NULL PRIMARY KEY REFERENCES runs (id),
        trace_id TEXT NOT NULL UNIQUE CHECK ({_HEX32.format("trace_id")}),
        root_event_id INTEGER NOT NULL UNIQUE REFERENCES events (id),
        root_span_id TEXT NOT NULL CHECK ({_HEX16.format("root_span_id")}),
        mapping_version INTEGER NOT NULL CHECK (mapping_version = 1),
        endpoint TEXT NOT NULL CHECK (length(endpoint) > 0),
        ui_url TEXT NOT NULL CHECK (length(ui_url) > 0),
        langfuse_project_id TEXT NOT NULL
            CHECK (length(langfuse_project_id) BETWEEN 1 AND 128),
        destination_sha256 TEXT NOT NULL CHECK ({_HEX64.format("destination_sha256")}),
        suite_id TEXT NULL,
        condition_name TEXT NULL,
        projected_sequence INTEGER NOT NULL DEFAULT 0 CHECK (projected_sequence >= 0),
        export_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (export_status IN ('pending', 'idle', 'suspended', 'blocked')),
        lease_token TEXT NULL,
        lease_expires_at TEXT NULL,
        last_error_code TEXT NULL
            CHECK (last_error_code IS NULL OR last_error_code IN {_ERROR_CODES}),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK ((suite_id IS NULL) = (condition_name IS NULL)),
        CHECK (suite_id IS NULL OR length(suite_id) BETWEEN 1 AND 128),
        CHECK (condition_name IS NULL OR length(condition_name) BETWEEN 1 AND 128),
        CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))
    )
    """,
    f"""
    CREATE TABLE observability_spans (
        event_id INTEGER NOT NULL PRIMARY KEY REFERENCES events (id),
        run_id TEXT NOT NULL REFERENCES observability_runs (run_id),
        event_sequence INTEGER NOT NULL CHECK (event_sequence > 0),
        span_id TEXT NOT NULL CHECK ({_HEX16.format("span_id")}),
        descriptor_json TEXT NULL
            CHECK (descriptor_json IS NULL OR (
                json_valid(descriptor_json) AND length(CAST(descriptor_json AS BLOB)) <= 32768
            )),
        descriptor_sha256 TEXT NULL
            CHECK (descriptor_sha256 IS NULL OR {_HEX64.format("descriptor_sha256")}),
        state TEXT NOT NULL
            CHECK (state IN ('waiting_end', 'pending', 'inflight', 'acknowledged', 'rejected')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        next_attempt_at TEXT NULL,
        inflight_token TEXT NULL,
        acknowledged_at TEXT NULL,
        UNIQUE (run_id, event_sequence),
        UNIQUE (run_id, span_id),
        CHECK ((descriptor_json IS NULL) = (descriptor_sha256 IS NULL)),
        CHECK ((state = 'waiting_end') = (descriptor_json IS NULL)),
        CHECK ((state = 'inflight') = (inflight_token IS NOT NULL)),
        CHECK ((state = 'acknowledged') = (acknowledged_at IS NOT NULL))
    )
    """,
    "CREATE INDEX idx_obs_runs_lease ON observability_runs (export_status, lease_expires_at)",
    "CREATE INDEX idx_obs_spans_pending "
    "ON observability_spans (run_id, state, next_attempt_at, event_sequence)",
    """
    CREATE TRIGGER observability_runs_identity_immutable
    BEFORE UPDATE OF run_id, trace_id, root_event_id, root_span_id, mapping_version, endpoint,
        ui_url, langfuse_project_id, destination_sha256, suite_id, condition_name, created_at
    ON observability_runs
    BEGIN
        SELECT RAISE(ABORT, 'observability context is immutable');
    END
    """,
    """
    CREATE TRIGGER observability_runs_no_delete
    BEFORE DELETE ON observability_runs
    BEGIN
        SELECT RAISE(ABORT, 'observability context is immutable');
    END
    """,
    """
    CREATE TRIGGER observability_spans_identity_immutable
    BEFORE UPDATE OF event_id, run_id, event_sequence, span_id ON observability_spans
    BEGIN
        SELECT RAISE(ABORT, 'observability span identity is immutable');
    END
    """,
    """
    CREATE TRIGGER observability_spans_descriptor_immutable
    BEFORE UPDATE OF descriptor_json, descriptor_sha256 ON observability_spans
    WHEN OLD.descriptor_json IS NOT NULL
        AND (NEW.descriptor_json IS NOT OLD.descriptor_json
            OR NEW.descriptor_sha256 IS NOT OLD.descriptor_sha256)
    BEGIN
        SELECT RAISE(ABORT, 'observability descriptor is immutable');
    END
    """,
    """
    CREATE TRIGGER observability_spans_rejected_final
    BEFORE UPDATE OF state ON observability_spans
    WHEN OLD.state IN ('rejected', 'acknowledged') AND NEW.state IS NOT OLD.state
    BEGIN
        SELECT RAISE(ABORT, 'observability span state is final');
    END
    """,
    """
    CREATE TRIGGER observability_spans_no_delete
    BEFORE DELETE ON observability_spans
    BEGIN
        SELECT RAISE(ABORT, 'observability spans are append-only');
    END
    """,
    """
    CREATE TRIGGER observability_spans_same_run
    BEFORE INSERT ON observability_spans
    WHEN NOT EXISTS (
        SELECT 1 FROM events
        WHERE id = NEW.event_id AND run_id = NEW.run_id AND sequence = NEW.event_sequence
    )
    BEGIN
        SELECT RAISE(ABORT, 'observability span must reference an event of the same run');
    END
    """,
)
