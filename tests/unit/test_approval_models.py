import pytest
from pydantic import ValidationError

from lastro.approvals import (
    ApprovalAction,
    ApprovalStatusInput,
    FieldChange,
    changed_message,
    first_difference,
    request_lines,
)
from lastro.history import ApprovalRecord, ApprovalStatus, JsonObject, JsonValue


def components(**overrides: JsonValue) -> JsonObject:
    base: JsonObject = {
        "tenant": "aurora",
        "target": {"tool": "submit", "route": "/employees/57/edit"},
        "record": "57",
        "operation": "update",
        "fields": ["email", "phone"],
        "values": {"email": "new@example.com", "phone": "11 99999-0000"},
        "page_state": "f" * 64,
    }
    base.update(overrides)
    return base


def action(**overrides: object) -> ApprovalAction:
    data: dict[str, object] = {
        "tool": "submit",
        "run_id": "run_x",
        "profile": "demo",
        "environment_url": "http://127.0.0.1",
        "tenant": "aurora",
        "tenant_display": "Clínica Aurora",
        "entity": "employee",
        "record_id": "57",
        "operation": "update",
        "method": "POST",
        "route": "/employees/57/edit",
        "target": {"role": "button", "name": "Salvar"},
        "origin": "unknown",
        "changes": (
            FieldChange("phone", "11 98888-0000", "11 99999-0000"),
            FieldChange("email", "old@example.com", "new@example.com"),
        ),
        "components": components(),
        "fingerprint": "e" * 64,
        "reason": "Validate persistence",
        "known_effects": ("Sends POST /employees/57",),
    }
    data.update(overrides)
    return ApprovalAction(**data)  # type: ignore[arg-type]


def test_summary_matches_notification_text() -> None:
    assert action().summary == "edit employee #57 (phone, email) in Clínica Aurora"
    assert action(changes=()).summary == "edit employee #57 (no field changes) in Clínica Aurora"
    created = action(operation="create", record_id="")
    assert created.summary.startswith("create employee (")


def test_changed_message_is_exact() -> None:
    assert (
        changed_message("values") == "The action changed after approval (values); "
        "approval required again."
    )


def test_first_difference_follows_stable_order() -> None:
    stored = components()
    assert first_difference(stored, components()) is None
    assert first_difference(stored, components(page_state="0" * 64)) == "page_state"
    assert (
        first_difference(stored, components(values={"email": "x@example.com"}, tenant="boreal"))
        == "tenant"
    )
    assert first_difference(stored, components(values={"email": "x"})) == "values"
    assert first_difference(None, components()) == "tenant"


def test_request_lines_show_full_request() -> None:
    item = action()
    record = ApprovalRecord(
        id="approval_1",
        run_id="run_x",
        action_id="act1",
        fingerprint=item.fingerprint,
        status=ApprovalStatus.PENDING,
        decision=None,
        decided_by=None,
        decision_reason=None,
        payload=item.payload(),
        requested_at="2026-11-03T12:00:00.000000Z",
        expires_at="2026-11-03T12:15:00.000000Z",
        decided_at=None,
        invalidated_at=None,
        consumed_at=None,
        execution_correlation_id=None,
    )

    text = "\n".join(request_lines(record, "09:15"))

    for expected in (
        "Lastro needs approval: edit employee #57 (phone, email) in Clínica Aurora",
        "Run: run_x",
        "Environment: demo (http://127.0.0.1)",
        "Tenant: Clínica Aurora (aurora)",
        "Record: employee #57",
        "  - email: old@example.com -> new@example.com",
        "Reason: Validate persistence",
        "  - Sends POST /employees/57",
        "Fingerprint: " + "e" * 64,
        "Options: approve | skip | end run",
        "Command: lastro approve act1 [--skip | --end]",
    ):
        assert expected in text


def test_status_input_limits_wait() -> None:
    assert ApprovalStatusInput.model_validate({"approval_id": "a"}).wait_seconds == 30
    for invalid in ({"approval_id": "a", "wait_seconds": 31}, {"approval_id": ""}, {}):
        with pytest.raises(ValidationError):
            ApprovalStatusInput.model_validate(invalid)
