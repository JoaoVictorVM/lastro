"""Agregados de sinais e idempotência de ocorrências reais."""

VERSION = 2
NAME = "oracle_signals"
STATEMENTS = (
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
    "CREATE UNIQUE INDEX idx_oracle_signals_run_dedupe ON oracle_signals(run_id, dedupe_sha256)",
    "CREATE INDEX idx_oracle_signals_run_latest ON oracle_signals(run_id, last_event_id DESC)",
    "CREATE UNIQUE INDEX idx_events_oracle_occurrence "
    "ON events(run_id, json_extract(payload_json, '$.occurrence_id')) "
    "WHERE event_type = 'oracle_signal' "
    "AND json_extract(payload_json, '$.occurrence_id') IS NOT NULL",
)
