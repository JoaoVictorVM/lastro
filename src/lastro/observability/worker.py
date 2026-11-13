"""Worker de exportação: polling do histórico, lease por run, backoff e drenagem limitada.

O worker nunca escreve em `events` nem em stdout (reservado ao protocolo MCP). Falhas de
telemetria ficam no estado local da fila e em stderr sanitizado; o resultado da run e a
política não dependem delas. Cada ciclo relê o perfil: desligado ou com destino diferente do
congelado, nenhum envio novo acontece.
"""

import contextlib
import os
import secrets
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from lastro.config.observability import ObservabilitySettings
from lastro.history import HistoryError, HistoryStore
from lastro.observability.models import ExportOutcome, ExportStatus, SpanDescriptor
from lastro.observability.projection import ProjectionError

POLL_SECONDS = 1.0
DRAIN_SECONDS = 5.0
SEND_BUDGET_SECONDS = 5.0


class Sender(Protocol):
    def send(self, descriptors: list[SpanDescriptor]) -> ExportOutcome: ...


type SenderFactory = Callable[[ObservabilitySettings, str, str, float], Sender]
type SettingsSource = Callable[[], ObservabilitySettings]


def default_sender(
    settings: ObservabilitySettings, public_key: str, secret_key: str, timeout: float
) -> Sender:
    from lastro.observability.exporter import LangfuseSpanExporter

    destination = settings.destination()
    assert destination is not None
    return LangfuseSpanExporter(destination, public_key, secret_key, timeout=timeout)


@dataclass
class DrainResult:
    sent: int = 0
    acknowledged: int = 0
    last_code: str | None = None


class ExportWorker:
    """Exportador de uma run; seguro para vários processos graças ao lease com fencing."""

    def __init__(
        self,
        history: HistoryStore,
        run_id: str,
        settings_source: SettingsSource,
        *,
        environ: Mapping[str, str] | None = None,
        sender_factory: SenderFactory = default_sender,
        extra_available: Callable[[], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        from lastro.observability import extra_available as detect

        self.history = history
        self.run_id = run_id
        self._settings_source = settings_source
        self._environ = environ if environ is not None else os.environ
        self._sender_factory = sender_factory
        self._extra_available = extra_available or detect
        self._monotonic = monotonic
        self._poll = poll_seconds
        self.token = secrets.token_hex(16)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _suspend(self, code: str) -> None:
        self.history.set_observability_status(self.run_id, "suspended", code)
        self.history.release_observability_lease(self.run_id, self.token)

    def _ready(self) -> tuple[ObservabilitySettings, str, str] | None:
        """Perfil atual habilitado, mesmo destino congelado, extra e chaves presentes."""
        context = self.history.observability_context(self.run_id)
        if context is None:
            return None
        try:
            settings = self._settings_source()
        except Exception:
            self._suspend("obs_suspended")
            return None
        destination = settings.destination()
        if destination is None:
            self._suspend("obs_suspended")
            return None
        if destination.sha256 != context.destination_sha256:
            self._suspend("obs_destination_mismatch")
            return None
        if not self._extra_available():
            self._suspend("obs_extra_missing")
            return None
        public_key = self._environ.get(settings.public_key_env, "")
        secret_key = self._environ.get(settings.secret_key_env, "")
        if not public_key.strip() or not secret_key.strip():
            self._suspend("obs_credentials_missing")
            return None
        if context.export_status == "suspended":
            self.history.set_observability_status(self.run_id, "pending", None)
        return settings, public_key, secret_key

    def project(self) -> int:
        total = 0
        while (count := self.history.project_observability(self.run_id)) > 0:
            total += count
        return total

    def cycle(self, budget: float = SEND_BUDGET_SECONDS, *, force: bool = False) -> DrainResult:
        """Projeta, toma o lease e envia no máximo um lote; nunca levanta exceção."""
        result = DrainResult()
        try:
            self._cycle(result, budget, force=force)
        except ProjectionError:
            self._safe_status("blocked", "obs_projection_failed")
            result.last_code = "obs_projection_failed"
        except (HistoryError, sqlite3.Error, OSError):
            result.last_code = "obs_storage_unavailable"
        return result

    def _safe_status(self, status: ExportStatus, code: str) -> None:
        with contextlib.suppress(HistoryError, sqlite3.Error, OSError):
            self.history.set_observability_status(self.run_id, status, code)

    def _cycle(self, result: DrainResult, budget: float, *, force: bool) -> None:
        self.project()
        ready = self._ready()
        if ready is None:
            return
        settings, public_key, secret_key = ready
        context = self.history.observability_context(self.run_id)
        if context is None or (context.export_status == "blocked" and not force):
            return
        if not self.history.claim_observability_lease(self.run_id, self.token):
            result.last_code = "obs_lease_busy"
            return
        batch = self.history.take_observability_batch(self.run_id, self.token)
        if not batch:
            return
        ids = [item.event_id for item in batch]
        sender = self._sender_factory(settings, public_key, secret_key, budget)
        outcome = sender.send([item.descriptor for item in batch])
        result.sent += len(batch)
        if outcome.kind == "ok":
            result.acknowledged += self.history.acknowledge_observability(
                self.run_id, self.token, ids
            )
            return
        result.last_code = outcome.code
        self.history.fail_observability(
            self.run_id,
            self.token,
            ids,
            outcome.kind,
            outcome.code or "export_rejected",
            outcome.retry_after_seconds,
        )

    def drain(self, budget: float = DRAIN_SECONDS, *, force: bool = False) -> DrainResult:
        """Envia lotes até não haver pendência elegível ou o orçamento acabar."""
        total = DrainResult()
        deadline = self._monotonic() + budget
        while (remaining := deadline - self._monotonic()) > 0:
            step = self.cycle(min(remaining, SEND_BUDGET_SECONDS), force=force)
            total.sent += step.sent
            total.acknowledged += step.acknowledged
            total.last_code = step.last_code or total.last_code
            if step.sent == 0 or step.acknowledged < step.sent:
                break
        with contextlib.suppress(HistoryError, sqlite3.Error, OSError):
            self.history.release_observability_lease(self.run_id, self.token)
            # Lotes em backoff de ciclos anteriores também significam entrega incompleta.
            status = self.history.observability_status(self.run_id)
            if total.last_code is None and status is not None:
                incomplete = status.pending_spans > status.waiting_end or status.rejected_spans
                if incomplete or status.export_status in ("blocked", "suspended"):
                    total.last_code = status.last_error_code or "export_unavailable"
        return total

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="lastro-obs", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.cycle()
            self._stop.wait(self._poll)

    def stop(self, drain_budget: float = DRAIN_SECONDS) -> DrainResult:
        """Encerra o polling e drena o que já está confirmado, dentro do orçamento."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(SEND_BUDGET_SECONDS + 1)
            self._thread = None
        result = self.drain(drain_budget)
        if result.last_code is not None:
            warn(
                f"observability export incomplete ({result.last_code}); "
                f"run 'lastro obs export {self.run_id}' to retry"
            )
        return result


def warn(message: str) -> None:
    print(f"lastro: warning: {message}", file=sys.stderr)
