"""Idempotência por ação autorizada e ordem determinística dos registros criados."""

VERSION = 3
NAME = "created_record_actions"
STATEMENTS = (
    "CREATE UNIQUE INDEX idx_created_records_run_action "
    "ON created_records(run_id, json_extract(payload_json, '$.action_id')) "
    "WHERE json_type(payload_json, '$.action_id') = 'text'",
    "CREATE INDEX idx_created_records_run_created ON created_records(run_id, created_at, id)",
)
