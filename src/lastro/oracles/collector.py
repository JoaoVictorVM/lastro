"""Captura temporal independente do lock de ferramentas, com drenagem por watermark."""

import asyncio
import time
from collections.abc import Callable
from uuid import uuid4

from lastro.history.errors import RunNotActiveError
from lastro.history.redaction import preview, redact_text
from lastro.history.store import HistoryStore
from lastro.oracles.ignore import IgnoreRule, ignored
from lastro.oracles.models import ActionContext, OracleOccurrence, SignalType
from lastro.oracles.sanitize import safe_url
from lastro.timeutil import utc_now_iso


class OracleCollector:
    def __init__(
        self,
        history: HistoryStore,
        run_id: str,
        rules: list[IgnoreRule],
        sanitize: Callable[[str], str],
        on_failure: Callable[[Exception], None],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utc: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.history, self.run_id, self.rules = history, run_id, rules
        self.sanitize, self.on_failure = sanitize, on_failure
        self.monotonic, self.utc = monotonic, utc
        self.actions: dict[object, ActionContext] = {}
        self.pending: list[OracleOccurrence] = []
        self.worker: asyncio.Task[None] | None = None
        self.accepting = True
        self.failed = False

    def mark_action(self, page: object, action_id: str, sequence: int) -> None:
        self.actions[page] = ActionContext(action_id, sequence, self.monotonic())

    def inherit(self, popup: object, parent: object) -> None:
        if popup not in self.actions and parent in self.actions:
            self.actions[popup] = self.actions[parent]

    def reset(self) -> None:
        self.actions.clear()
        self.accepting = True

    def capture(
        self,
        kind: str,
        *,
        page: object | None,
        url: str | None,
        text: str = "",
        level: str = "error",
        status: int | None = None,
        method: str | None = None,
        aborted: bool = False,
        has_response: bool = False,
    ) -> None:
        if not self.accepting or self.failed:
            return
        now, timestamp = self.monotonic(), self.utc()
        try:
            signal_type: SignalType
            if kind in {"console", "pageerror"}:
                if kind == "console" and level != "error":
                    return
                signal_type = "js_error"
            elif kind == "response":
                if status is not None and 400 <= status <= 499 and status not in {401, 403}:
                    signal_type = "http_4xx"
                elif status is not None and 500 <= status <= 599:
                    signal_type = "http_5xx"
                else:
                    return
                text = f"HTTP {status}"
            elif kind == "requestfailed" and not aborted and not has_response:
                signal_type = "request_failed"
            else:
                return
            safe, path = safe_url(self.sanitize(url) if url is not None else None)
            message = preview(redact_text(self.sanitize(text)), 8000)
            if ignored(self.rules, path, message):
                return
            action = self.actions.get(page) if page is not None else None
            if action is not None and not 0 <= now - action.started_at <= 5:
                action = None
            occurrence = OracleOccurrence(
                occurrence_id=f"occurrence_{uuid4().hex}",
                signal_type=signal_type,
                url=safe,
                url_path=path,
                http_method=method,
                http_status=status if kind == "response" else None,
                message=message,
                timestamp=timestamp,
                action_id=action.action_id if action else None,
                action_sequence=action.action_sequence if action else None,
            )
            self.pending.append(occurrence)
            if self.worker is None or self.worker.done():
                self.worker = asyncio.create_task(self._persist())
        except Exception as error:
            self.fail(error)

    def fail(self, error: Exception) -> None:
        if not self.failed:
            self.failed = True
            self.on_failure(error)

    async def _persist(self) -> None:
        batch, self.pending = self.pending, []
        for occurrence in batch:
            try:
                self.history.record_oracle_signal(self.run_id, occurrence)
            except RunNotActiveError:
                continue
            except Exception as error:
                self.fail(error)
                break

    async def drain(self) -> None:
        # Capturas posteriores ficam para a próxima chamada, sem aguardar tráfego futuro.
        worker = self.worker
        if worker is not None:
            await worker
        if self.failed:
            raise ValueError("Oracle collection unavailable; history is incomplete.")
