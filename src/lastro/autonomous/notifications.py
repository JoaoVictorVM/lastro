"""Notificação de fim de run autônoma: mensagem canônica, outbox por canal e adapters.

Todos os canais habilitados recebem a mesma projeção de até 20 linhas, sem evidência nem
segredo. Cada canal tem entrega independente, com no máximo três tentativas totais nos instantes
0, 60 e 300 segundos. Credenciais são resolvidas só no instante da tentativa; corpo de resposta
nunca é lido nem persistido. Falha de entrega não altera o resultado da run.
"""

import contextlib
import os
import smtplib
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from lastro.autonomous.models import (
    AttemptStatus,
    ComparisonResult,
    DeliveryResult,
    DeliveryStatus,
    NotificationMessage,
    NotificationRecord,
    QueueItem,
)
from lastro.config.autonomous import (
    AutonomousSettings,
    SlackChannel,
    SmtpChannel,
    TeamsChannel,
)
from lastro.history import EventType, HistoryStore, RunRecord
from lastro.history.database import db_timestamp
from lastro.timeutil import utc_now

HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RETRYABLE_HTTP = frozenset({429})

type Channel = SlackChannel | TeamsChannel | SmtpChannel
type Environ = Mapping[str, str]


def build_message(
    history: HistoryStore,
    item: QueueItem,
    run: RunRecord | None,
    comparison: ComparisonResult | None,
    report_path: Path | None,
) -> NotificationMessage:
    """Projeção a partir de dados confiáveis: estado, razão, pendências e contagens."""
    pending = history.pending_items(run.id) if run is not None else []
    skipped = sum(1 for p in pending if p.kind == "approval" and p.state == "skipped_unattended")
    gaps = sum(1 for p in pending if p.kind == "clarification_gap")
    report: str | None = None
    if report_path is not None:
        try:
            report = report_path.relative_to(history.project_dir).as_posix()
        except ValueError:
            report = str(report_path)
    if comparison is None:
        summary = "comparison not available"
    elif comparison.baseline_run_id is None:
        summary = "no previous run to compare; all findings shown as new"
    else:
        summary = f"compared with {comparison.baseline_run_id}"
    return NotificationMessage(
        profile=item.profile,
        status=run.status.value if run is not None else item.status.value,
        termination_reason=(run.status_reason if run is not None else item.reason),
        counts=comparison.counts if comparison is not None else {},
        pending_approvals=skipped,
        clarification_gaps=gaps,
        other_pending=len(pending) - skipped - gaps,
        report=report,
        comparison=summary,
    )


class ChannelAdapter(Protocol):
    def send(
        self, message: NotificationMessage, channel: Any, environ: Environ
    ) -> DeliveryResult: ...


def _elapsed(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _http_result(status: int, started: float) -> DeliveryResult:
    if 200 <= status < 300:
        return DeliveryResult(AttemptStatus.DELIVERED, status, None, _elapsed(started))
    if 300 <= status < 400:
        # Redirect nunca é seguido: o destino configurado é o único autorizado.
        return DeliveryResult(
            AttemptStatus.PERMANENT, status, "redirect_refused", _elapsed(started)
        )
    if status in RETRYABLE_HTTP or status >= 500:
        return DeliveryResult(AttemptStatus.RETRYABLE, status, f"http_{status}", _elapsed(started))
    return DeliveryResult(AttemptStatus.PERMANENT, status, f"http_{status}", _elapsed(started))


@dataclass
class WebhookAdapter:
    """POST JSON com timeouts explícitos; o corpo da resposta é descartado."""

    payload: Callable[[NotificationMessage], dict[str, Any]]
    client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(
        timeout=HTTP_TIMEOUT, follow_redirects=False
    )

    def send(self, message: NotificationMessage, channel: Any, environ: Environ) -> DeliveryResult:
        url = environ.get(channel.webhook_env, "").strip()
        if not url:
            return DeliveryResult(AttemptStatus.PERMANENT, None, "webhook_env_missing")
        if urlsplit(url).scheme != "https":
            return DeliveryResult(AttemptStatus.PERMANENT, None, "webhook_not_https")
        started = time.monotonic()
        try:
            with self.client_factory() as client:
                response = client.post(url, json=self.payload(message))
                status = response.status_code
                response.close()
        except httpx.TimeoutException:
            return DeliveryResult(AttemptStatus.RETRYABLE, None, "timeout", _elapsed(started))
        except httpx.HTTPError:
            return DeliveryResult(AttemptStatus.RETRYABLE, None, "network_error", _elapsed(started))
        return _http_result(status, started)


def slack_payload(message: NotificationMessage) -> dict[str, Any]:
    """Bloco textual simples, sem mrkdwn: o texto chega exatamente como projetado."""
    return {
        "text": message.lines()[0],
        "blocks": [
            {"type": "section", "text": {"type": "plain_text", "text": message.text}},
        ],
    }


def teams_payload(message: NotificationMessage) -> dict[str, Any]:
    """Adaptive Card mínimo aceito por webhooks de Workflows e conectores do Teams."""
    lines = message.lines()
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": lines[0], "weight": "Bolder", "wrap": True}
    ]
    body += [
        {"type": "TextBlock", "text": line, "wrap": True, "spacing": "None"} for line in lines[1:]
    ]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                },
            }
        ],
    }


type SmtpFactory = Callable[[SmtpChannel, ssl.SSLContext], smtplib.SMTP]


def _smtp_factory(channel: SmtpChannel, context: ssl.SSLContext) -> smtplib.SMTP:
    if channel.ssl:
        return smtplib.SMTP_SSL(
            channel.host, channel.port, timeout=channel.timeout_seconds, context=context
        )
    return smtplib.SMTP(channel.host, channel.port, timeout=channel.timeout_seconds)


@dataclass
class SmtpAdapter:
    """text/plain UTF-8 com TLS obrigatório; nunca rebaixa para texto claro."""

    factory: SmtpFactory = _smtp_factory
    context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context

    def send(
        self, message: NotificationMessage, channel: SmtpChannel, environ: Environ
    ) -> DeliveryResult:
        username = password = None
        if channel.username_env is not None and channel.password_env is not None:
            username = environ.get(channel.username_env)
            password = environ.get(channel.password_env)
            if not username or not password:
                return DeliveryResult(AttemptStatus.PERMANENT, None, "smtp_credentials_missing")
        email = EmailMessage()
        email["Subject"] = f"{message.title} · {message.status}"
        email["From"] = channel.from_address
        email["To"] = ", ".join(channel.to)
        email.set_content(message.text, subtype="plain", charset="utf-8")
        started = time.monotonic()
        context = self.context_factory()
        try:
            with self.factory(channel, context) as server:
                if channel.starttls:
                    server.starttls(context=context)
                if username is not None and password is not None:
                    server.login(username, password)
                server.send_message(email)
        except smtplib.SMTPNotSupportedError:
            return DeliveryResult(
                AttemptStatus.PERMANENT, None, "starttls_unsupported", _elapsed(started)
            )
        except smtplib.SMTPAuthenticationError as exc:
            return DeliveryResult(
                AttemptStatus.PERMANENT, exc.smtp_code, "smtp_auth", _elapsed(started)
            )
        except smtplib.SMTPResponseException as exc:
            status = (
                AttemptStatus.RETRYABLE if 400 <= exc.smtp_code < 500 else AttemptStatus.PERMANENT
            )
            return DeliveryResult(status, exc.smtp_code, f"smtp_{exc.smtp_code}", _elapsed(started))
        except smtplib.SMTPRecipientsRefused:
            return DeliveryResult(
                AttemptStatus.PERMANENT, None, "smtp_recipients_refused", _elapsed(started)
            )
        except ssl.SSLError:
            return DeliveryResult(AttemptStatus.PERMANENT, None, "tls_failed", _elapsed(started))
        except (smtplib.SMTPException, OSError):
            return DeliveryResult(
                AttemptStatus.RETRYABLE, None, "smtp_unavailable", _elapsed(started)
            )
        return DeliveryResult(AttemptStatus.DELIVERED, 250, None, _elapsed(started))


def default_adapters() -> dict[str, ChannelAdapter]:
    return {
        "slack": WebhookAdapter(slack_payload),
        "teams": WebhookAdapter(teams_payload),
        "smtp": SmtpAdapter(),
    }


@dataclass
class Outbox:
    history: HistoryStore
    settings: AutonomousSettings
    adapters: dict[str, ChannelAdapter] = field(default_factory=default_adapters)
    environ: Environ = field(default_factory=lambda: os.environ)
    clock: Callable[[], Any] = utc_now

    def schedule(self, item: QueueItem, message: NotificationMessage) -> list[NotificationRecord]:
        channels = [(channel.name, channel.type) for channel in self.settings.notifications]
        if not channels:
            return []
        return self.history.autonomous.create_notifications(item.id, channels, message)

    def deliver_due(self, item_id: str | None = None) -> list[NotificationRecord]:
        """Uma tentativa por entrega vencida; canais são independentes entre si."""
        delivered: list[NotificationRecord] = []
        for record in self.history.autonomous.due_notifications(item_id):
            requested = db_timestamp(self.clock())
            channel = self.settings.channel(record.channel_name)
            if channel is None or channel.type != record.channel_type:
                result = DeliveryResult(AttemptStatus.PERMANENT, None, "channel_not_configured")
            else:
                adapter = self.adapters.get(channel.type)
                if adapter is None:
                    result = DeliveryResult(AttemptStatus.PERMANENT, None, "adapter_unavailable")
                else:
                    try:
                        result = adapter.send(record.message, channel, self.environ)
                    except Exception as exc:
                        result = DeliveryResult(
                            AttemptStatus.RETRYABLE, None, f"adapter_{type(exc).__name__}"
                        )
            updated = self.history.autonomous.record_attempt(record.id, requested, result)
            delivered.append(updated)
            if updated.status != DeliveryStatus.PENDING:
                self._audit(updated)
        return delivered

    def _audit(self, record: NotificationRecord) -> None:
        with contextlib.suppress(Exception):
            item = self.history.autonomous.get_item(record.queue_item_id)
            if item.run_id is None:
                return
            self.history.autonomous.append_run_event(
                item.run_id,
                EventType.AUTONOMOUS_NOTIFICATION,
                {
                    "channel": record.channel_name,
                    "type": record.channel_type,
                    "status": record.status.value,
                    "attempts": record.attempts,
                    "error_code": record.last_error_code,
                },
                f"{record.id}:final",
            )
