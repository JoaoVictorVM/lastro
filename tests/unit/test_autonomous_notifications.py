import smtplib
import sqlite3
import ssl
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from lastro.autonomous.models import (
    AttemptStatus,
    DeliveryResult,
    DeliveryStatus,
    MissionSnapshot,
    NotificationMessage,
    TriggerInput,
    TriggerKind,
)
from lastro.autonomous.notifications import (
    Outbox,
    SmtpAdapter,
    WebhookAdapter,
    slack_payload,
    teams_payload,
)
from lastro.config.autonomous import AutonomousSettings, SmtpChannel, TeamsChannel
from lastro.config.loader import ProjectContext
from lastro.history import HistoryStore, open_history

MESSAGE = NotificationMessage(
    profile="homolog",
    status="completed",
    termination_reason=None,
    counts={"new": 2, "regressed": 1, "recurring": 3, "resolved": 4},
    pending_approvals=1,
    clarification_gaps=0,
    other_pending=0,
    report="runs/run_x/report.autonomous.md",
    comparison="compared with run_y",
)
SETTINGS = AutonomousSettings.model_validate(
    {
        "notifications": [
            {"name": "qa-slack", "type": "slack", "webhook_env": "SLACK_HOOK"},
            {"name": "qa-teams", "type": "teams", "webhook_env": "TEAMS_HOOK"},
            {
                "name": "qa-email",
                "type": "smtp",
                "host": "smtp.example.test",
                "from_address": "lastro@example.test",
                "to": ["qa@example.test"],
            },
        ]
    }
)


class Scripted:
    def __init__(self, *results: DeliveryResult) -> None:
        self.results = list(results)
        self.calls = 0

    def send(self, message: NotificationMessage, channel: Any, environ: Any) -> DeliveryResult:
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        return DeliveryResult(AttemptStatus.RETRYABLE, 503, "http_503")


@pytest.fixture
def store(history_context: ProjectContext, fake_clock: Callable[[], datetime]) -> HistoryStore:
    return open_history(history_context, clock=fake_clock)


def _item(store: HistoryStore) -> str:
    trigger = TriggerInput(
        kind=TriggerKind.MANUAL,
        key="manual:1",
        profile="homolog",
        mission=MissionSnapshot("text", None, "Explore"),
    )
    return store.autonomous.enqueue(trigger).item.id


def _outbox(
    store: HistoryStore, clock_now: list[datetime], adapters: dict[str, Any]
) -> tuple[Outbox, str]:
    item_id = _item(store)
    outbox = Outbox(store, SETTINGS, adapters, environ={}, clock=lambda: clock_now[0])
    outbox.schedule(store.autonomous.get_item(item_id), MESSAGE)
    return outbox, item_id


def test_fanout_uses_all_channels(store: HistoryStore, clock_now: list[datetime]) -> None:
    adapters = {
        "slack": Scripted(DeliveryResult(AttemptStatus.DELIVERED, 200)),
        "teams": Scripted(DeliveryResult(AttemptStatus.DELIVERED, 202)),
        "smtp": Scripted(DeliveryResult(AttemptStatus.DELIVERED, 250)),
    }
    outbox, item_id = _outbox(store, clock_now, adapters)
    outbox.schedule(store.autonomous.get_item(item_id), MESSAGE)
    records = outbox.deliver_due(item_id)
    assert sorted(record.channel_name for record in records) == [
        "qa-email",
        "qa-slack",
        "qa-teams",
    ]
    assert {record.status for record in records} == {DeliveryStatus.DELIVERED}
    assert len({record.message_sha256 for record in records}) == 1
    assert all(adapter.calls == 1 for adapter in adapters.values())
    slack = slack_payload(MESSAGE)
    assert slack["blocks"][0]["text"]["type"] == "plain_text"
    teams = teams_payload(MESSAGE)
    card = teams["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard" and len(card["body"]) == len(MESSAGE.lines())


def test_retry_schedule_is_zero_sixty_three_hundred(
    store: HistoryStore, clock_now: list[datetime]
) -> None:
    failing = Scripted()
    adapters = {"slack": failing, "teams": failing, "smtp": failing}
    outbox, item_id = _outbox(store, clock_now, adapters)
    created = clock_now[0]
    first = {record.channel_name: record for record in outbox.deliver_due(item_id)}
    assert first["qa-slack"].attempts == 1
    assert first["qa-slack"].next_attempt_at == _ts(created + timedelta(seconds=60))
    clock_now[0] = created + timedelta(seconds=59)
    assert outbox.deliver_due(item_id) == []
    clock_now[0] = created + timedelta(seconds=60)
    second = {record.channel_name: record for record in outbox.deliver_due(item_id)}
    assert second["qa-slack"].next_attempt_at == _ts(created + timedelta(seconds=300))
    clock_now[0] = created + timedelta(seconds=300)
    third = {record.channel_name: record for record in outbox.deliver_due(item_id)}
    assert third["qa-slack"].status == DeliveryStatus.FAILED
    assert third["qa-slack"].attempts == 3
    clock_now[0] = created + timedelta(days=1)
    assert outbox.deliver_due(item_id) == []
    assert failing.calls == 9


def _ts(moment: datetime) -> str:
    from lastro.history.database import db_timestamp

    return db_timestamp(moment)


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[WebhookAdapter, list[str]]:
    seen: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    adapter = WebhookAdapter(
        slack_payload,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(wrapped), follow_redirects=False
        ),
    )
    return adapter, seen


CHANNEL = TeamsChannel(name="qa-teams", type="teams", webhook_env="TEAMS_HOOK")
ENV = {"TEAMS_HOOK": "https://hooks.example.test/abc"}


def test_http_redirect_is_not_followed() -> None:
    adapter, seen = _adapter(
        lambda request: httpx.Response(302, headers={"Location": "https://evil.example/x"})
    )
    result = adapter.send(MESSAGE, CHANNEL, ENV)
    assert (result.status, result.error_code) == (AttemptStatus.PERMANENT, "redirect_refused")
    assert seen == ["https://hooks.example.test/abc"]
    insecure = adapter.send(MESSAGE, CHANNEL, {"TEAMS_HOOK": "http://hooks.example.test/abc"})
    assert insecure.error_code == "webhook_not_https"
    missing = adapter.send(MESSAGE, CHANNEL, {})
    assert missing.error_code == "webhook_env_missing"


def test_permanent_4xx_is_not_retried() -> None:
    statuses = {
        404: AttemptStatus.PERMANENT,
        429: AttemptStatus.RETRYABLE,
        503: AttemptStatus.RETRYABLE,
        204: AttemptStatus.DELIVERED,
    }
    for code, expected in statuses.items():
        adapter, _ = _adapter(lambda request, code=code: httpx.Response(code))
        assert adapter.send(MESSAGE, CHANNEL, ENV).status == expected

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    adapter, _ = _adapter(timeout)
    assert adapter.send(MESSAGE, CHANNEL, ENV).status == AttemptStatus.RETRYABLE


class FakeSmtp:
    def __init__(self, *, starttls_error: Exception | None = None) -> None:
        self.starttls_error = starttls_error
        self.sent: list[Any] = []
        self.tls = False

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, context: ssl.SSLContext) -> None:
        if self.starttls_error is not None:
            raise self.starttls_error
        assert context.verify_mode == ssl.CERT_REQUIRED
        self.tls = True

    def login(self, username: str, password: str) -> None:
        assert self.tls

    def send_message(self, message: Any) -> None:
        assert self.tls
        self.sent.append(message)


def test_smtp_requires_tls() -> None:
    with pytest.raises(ValidationError, match="TLS"):
        SmtpChannel.model_validate(
            {
                "name": "mail",
                "type": "smtp",
                "host": "smtp.example.test",
                "starttls": False,
                "from_address": "a@example.test",
                "to": ["b@example.test"],
            }
        )
    channel = SmtpChannel.model_validate(
        {
            "name": "mail",
            "type": "smtp",
            "host": "smtp.example.test",
            "from_address": "a@example.test",
            "to": ["b@example.test"],
        }
    )
    refusing = FakeSmtp(starttls_error=smtplib.SMTPNotSupportedError("no STARTTLS"))
    result = SmtpAdapter(factory=lambda c, ctx: refusing).send(MESSAGE, channel, {})  # type: ignore[arg-type, return-value]
    assert (result.status, result.error_code) == (AttemptStatus.PERMANENT, "starttls_unsupported")
    assert refusing.sent == []
    server = FakeSmtp()
    ok = SmtpAdapter(factory=lambda c, ctx: server).send(MESSAGE, channel, {})  # type: ignore[arg-type, return-value]
    assert ok.status == AttemptStatus.DELIVERED
    (email,) = server.sent
    assert email.get_content_type() == "text/plain"
    assert email.get_content().strip().splitlines() == list(MESSAGE.lines())


def test_errors_never_store_response_body(store: HistoryStore, clock_now: list[datetime]) -> None:
    secret = "token=leaked-response-secret"
    adapter, _ = _adapter(lambda request: httpx.Response(500, text=secret))
    outbox, item_id = _outbox(
        store, clock_now, {"slack": adapter, "teams": adapter, "smtp": Scripted()}
    )
    outbox.environ = {"SLACK_HOOK": ENV["TEAMS_HOOK"], "TEAMS_HOOK": ENV["TEAMS_HOOK"]}
    records = {record.channel_name: record for record in outbox.deliver_due(item_id)}
    assert records["qa-slack"].last_error_code == "http_500"
    conn = sqlite3.connect(store.database_path)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert "leaked-response-secret" not in dump
    assert "hooks.example.test" not in dump
