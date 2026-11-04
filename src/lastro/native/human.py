"""Decisões humanas fora do modelo: prompt no terminal ou espera por 'lastro approve'.

O modelo nunca chega a esta interface. Silêncio, EOF e texto fora das opções não aprovam.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from lastro.approvals import request_lines
from lastro.approvals.models import approve_command
from lastro.history import (
    ApprovalChoice,
    ApprovalError,
    ApprovalRecord,
    ApprovalStatus,
    DecidedBy,
    HistoryError,
    HistoryStore,
)
from lastro.timeutil import to_display_zone

CHOICES = {
    "a": ApprovalChoice.APPROVE,
    "approve": ApprovalChoice.APPROVE,
    "s": ApprovalChoice.SKIP,
    "skip": ApprovalChoice.SKIP,
    "e": ApprovalChoice.END_RUN,
    "end": ApprovalChoice.END_RUN,
    "end run": ApprovalChoice.END_RUN,
}
PROMPT = "Decision [approve/skip/end]: "
POLL_SECONDS = 1.0

type LineReader = Callable[[], str | None]
type Writer = Callable[[str], None]


def parse_choice(text: str | None) -> ApprovalChoice | None:
    """Somente as opções exatas; qualquer outra resposta não decide nada."""
    if text is None:
        return None
    return CHOICES.get(text.strip().lower())


def read_stdin_line() -> str | None:
    try:
        return input(PROMPT)
    except EOFError:
        return None


@dataclass
class HumanDecisionPort:
    history: HistoryStore
    run_id: str
    write: Writer
    interactive: bool
    timezone: str
    read_line: LineReader = read_stdin_line
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    poll_seconds: float = POLL_SECONDS

    def show(self, record: ApprovalRecord) -> None:
        expires = to_display_zone(record.expires_at, self.timezone).strftime("%H:%M")
        for line in request_lines(record, expires):
            self.write(line)

    def current(self, approval_id: str) -> ApprovalRecord | None:
        self.history.expire_approvals(self.run_id)
        return self.history.get_approval(approval_id, run_id=self.run_id)

    async def decide(self, approval_id: str) -> ApprovalRecord:
        """Espera a decisão durável: deste terminal, de outro terminal ou a expiração."""
        record = self.current(approval_id)
        if record is None:
            raise HistoryError(f"approval {approval_id} not found")
        if record.status != ApprovalStatus.PENDING:
            return record
        self.show(record)
        answer: asyncio.Future[str | None] | None = None
        if self.interactive:
            answer = self._read_async()
        else:
            self.write(f"No interactive terminal: decide with '{approve_command(approval_id)}'.")
        while True:
            if answer is not None and answer.done():
                choice = parse_choice(answer.result())
                if choice is not None:
                    try:
                        return self.history.decide_approval(
                            approval_id, choice, DecidedBy.CLI, run_id=self.run_id
                        )
                    except ApprovalError:
                        pass
                    answer = None
                elif answer.result() is None:
                    self.write(
                        f"Input closed; waiting for '{approve_command(approval_id)}' "
                        "or expiration. Nothing was approved."
                    )
                    answer = None
                else:
                    self.write("Answer approve, skip or end.")
                    answer = self._read_async()
            record = self.current(approval_id)
            if record is None or record.status != ApprovalStatus.PENDING:
                assert record is not None
                return record
            await self.sleep(self.poll_seconds)

    def _read_async(self) -> asyncio.Future[str | None]:
        """Leitura em thread daemon: uma decisão de outro terminal não fica presa ao input."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()

        def read() -> None:
            try:
                line = self.read_line()
            except Exception:
                line = None
            if not loop.is_closed():
                loop.call_soon_threadsafe(_resolve, future, line)

        threading.Thread(target=read, daemon=True).start()
        return future


def _resolve(future: "asyncio.Future[str | None]", value: str | None) -> None:
    if not future.done():
        future.set_result(value)
