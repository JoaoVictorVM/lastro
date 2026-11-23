from typing import Any

import pytest
from pydantic import ValidationError

from lastro.autonomous.models import (
    MAX_SUMMARY_LINES,
    AutonomousError,
    MissionSnapshot,
    NotificationMessage,
    TriggerInput,
    TriggerKind,
)
from lastro.config.autonomous import AutonomousSettings


def _settings(**data: Any) -> AutonomousSettings:
    return AutonomousSettings.model_validate(data)


def test_strict_config_rejects_secret_values() -> None:
    with pytest.raises(ValidationError, match="environment variable name"):
        _settings(
            notifications=[
                {"name": "s", "type": "slack", "webhook_env": "https://hooks.slack.com/x"}
            ]
        )
    with pytest.raises(ValidationError):
        _settings(
            notifications=[
                {"name": "s", "type": "slack", "webhook_url": "https://hooks.slack.com/x"}
            ]
        )
    with pytest.raises(ValidationError, match="TLS"):
        _settings(
            notifications=[
                {
                    "name": "mail",
                    "type": "smtp",
                    "host": "smtp.example.com",
                    "starttls": False,
                    "from_address": "a@example.com",
                    "to": ["b@example.com"],
                }
            ]
        )
    with pytest.raises(ValidationError, match="unique"):
        _settings(
            notifications=[
                {"name": "dup", "type": "slack", "webhook_env": "A"},
                {"name": "dup", "type": "teams", "webhook_env": "B"},
            ]
        )
    settings = _settings(
        notifications=[
            {"name": "qa-slack", "type": "slack", "webhook_env": "LASTRO_SLACK_WEBHOOK"},
            {
                "name": "qa-email",
                "type": "smtp",
                "host": "smtp.example.com",
                "username_env": "LASTRO_SMTP_USER",
                "password_env": "LASTRO_SMTP_PASSWORD",
                "from_address": "lastro@example.com",
                "to": ["qa@example.com"],
            },
        ]
    )
    snapshot = settings.snapshot()
    assert snapshot["max_minutes"] == 30
    assert settings.native_cap_nusd == 2_000_000_000
    assert [item["name"] for item in snapshot["channels"]] == ["qa-slack", "qa-email"]
    assert "LASTRO_SMTP_PASSWORD" not in str(snapshot)
    assert "qa@example.com" not in str(snapshot)


def _trigger(**overrides: Any) -> TriggerInput:
    data: dict[str, Any] = {
        "kind": TriggerKind.MANUAL,
        "key": "manual:1",
        "profile": "homolog",
        "mission": MissionSnapshot("file", "missions/a.md", "Explore employees"),
        "payload": {"b": 1, "a": 2},
    }
    data.update(overrides)
    return TriggerInput(**data)


def test_trigger_fingerprint_is_canonical() -> None:
    first = _trigger()
    reordered = _trigger(payload={"a": 2, "b": 1})
    assert first.fingerprint == reordered.fingerprint
    assert first.fingerprint != _trigger(payload={"a": 3, "b": 1}).fingerprint
    changed = _trigger(mission=MissionSnapshot("file", "missions/a.md", "Other text"))
    assert first.fingerprint != changed.fingerprint
    with pytest.raises(AutonomousError) as empty:
        MissionSnapshot("text", None, "   ")
    assert empty.value.code == "mission_snapshot_invalid"
    with pytest.raises(AutonomousError):
        _trigger(kind=TriggerKind.SCHEDULE)
    restored = MissionSnapshot.from_json(first.mission.as_json())
    assert restored == first.mission
    tampered = {**first.mission.as_json(), "sha256": "0" * 64}
    with pytest.raises(AutonomousError):
        MissionSnapshot.from_json(tampered)


def test_notification_has_at_most_20_lines() -> None:
    message = NotificationMessage(
        profile="homolog",
        status="completed",
        termination_reason=None,
        counts={"new": 2, "regressed": 1, "recurring": 3, "resolved": 4},
        pending_approvals=1,
        clarification_gaps=2,
        other_pending=3,
        report="runs/run_current/report.autonomous.md",
        comparison="compared with run_previous",
    )
    lines = message.lines()
    assert len(lines) <= MAX_SUMMARY_LINES
    assert lines[0] == "Lastro · homolog · 2 new findings, 1 regressed, 3 recurring"
    assert "1 action skipped awaiting approval" in lines
    assert lines[-1] == "report: runs/run_current/report.autonomous.md"
    assert NotificationMessage.from_json(message.as_json()) == message
    data = message.as_json()
    assert data["summary"] == {"new": 2, "regressed": 1, "recurring": 3, "resolved": 4}
    assert data["title"] == "Lastro · homolog"
