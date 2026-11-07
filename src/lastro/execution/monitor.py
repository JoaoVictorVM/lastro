"""Monitor de limites no browser owner: prazo contínuo, cancelamento durável e latch único.

O monitor nunca espera o lock da sessão para travar escritas: o lock de política entra primeiro
e só depois o encerramento disputa o lock para fechar recursos, sob o mesmo deadline absoluto.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from lastro.execution.models import (
    DRAIN_SECONDS,
    DURATION_LIMIT,
    FINALIZE_SECONDS,
    Notice,
)
from lastro.history import RunStatus
from lastro.timeutil import parse_timestamp, utc_now

if TYPE_CHECKING:
    from lastro.browser.bindings import ExpectedRequest
    from lastro.browser.tools import BrowserTools

POLL_SECONDS = 0.1
EXECUTION_LOCK = "execution_limit"
# Ferramentas que podem enviar a escrita vinculada; as demais travam antes do dispatch.
WRITE_TOOLS = frozenset({"click", "submit"})

type Monotonic = Callable[[], float]
type WallClock = Callable[[], datetime]


class ExecutionMonitor:
    """Autoridade dos limites no processo confiável; o agente apenas recebe avisos."""

    def __init__(
        self,
        runtime: "BrowserTools",
        *,
        monotonic: Monotonic = time.monotonic,
        wall_clock: WallClock = utc_now,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.runtime = runtime
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.poll_seconds = poll_seconds
        self.deadline: float | None = None
        self.grant: str | None = None
        self.notices: list[Notice] = []
        self.task: asyncio.Task[None] | None = None
        self.stopping: asyncio.Task[dict[str, Any]] | None = None
        self.result: dict[str, Any] | None = None
        self.finished = asyncio.Event()

    @property
    def history(self) -> Any:
        return self.runtime.history

    @property
    def run_id(self) -> str:
        return self.runtime.run_id

    def arm(self) -> None:
        """Converte o prazo UTC persistido em monotônico uma única vez por processo.

        Saltos posteriores do relógio de parede não ampliam o orçamento já calculado.
        """
        if self.deadline is not None:
            return
        state = self.history.execution_state(self.run_id)
        remaining = (parse_timestamp(state.deadline_at) - self.wall_clock()).total_seconds()
        self.deadline = self.monotonic() + remaining

    async def start(self) -> None:
        self.arm()
        self.task = asyncio.create_task(self.watch())

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                self.tick()
            except Exception:
                # Falha de consulta nega escritas; o histórico decide o restante na recuperação.
                self.engage()
            if self.stopping is not None:
                return

    def tick(self) -> None:
        """Uma volta do monitor: término externo, pedido durável, prazo e aviso de 80%."""
        if self.stopping is not None:
            return
        self.arm()
        record = self.history.get_run(self.run_id)
        state = self.history.execution_state(self.run_id)
        if state.stopped:
            self.stop(state.stop_status, state.stop_reason)
            return
        if record.status.is_terminal:
            self.engage()
            return
        if self.expired():
            self.trigger(RunStatus.LIMIT_REACHED.value, DURATION_LIMIT)
            return
        if not state.duration_warned:
            notice = self.history.warn_duration(self.run_id)
            if notice is not None:
                self.notices.append(notice)

    def expired(self) -> bool:
        return self.deadline is not None and self.monotonic() >= self.deadline

    def engage(self) -> None:
        """Bloqueio imediato; a concessão da última ação deixa de valer."""
        self.runtime.session.policy.locks.add(EXECUTION_LOCK)
        self.grant = None

    def trigger(self, status: str, reason: str) -> None:
        """Gatilho de tempo ou limite: trava antes de qualquer espera e grava o latch."""
        self.engage()
        latch = self.history.latch_stop(self.run_id, status, reason)
        self.stop(latch.state.stop_status, latch.state.stop_reason)

    def stop(self, status: str | None, reason: str | None) -> None:
        self.engage()
        if self.stopping is not None or status is None or reason is None:
            return
        self.stopping = asyncio.get_running_loop().create_task(self.shutdown(status, reason))

    # Admissão e rede ---------------------------------------------------------------------------

    def admitted(self, name: str, action_id: str, last: bool) -> None:
        """Última unidade: escrita vinculada recebe concessão única; o resto trava já."""
        if not last:
            return
        if name in WRITE_TOOLS and EXECUTION_LOCK not in self.runtime.session.policy.locks:
            self.grant = action_id
        else:
            self.engage()

    def check_write(self, expected: "ExpectedRequest | None") -> str | None:
        """Revalida prazo e estado durável imediatamente antes de encaminhar uma escrita."""
        if self.expired():
            with contextlib.suppress(Exception):
                self.trigger(RunStatus.LIMIT_REACHED.value, DURATION_LIMIT)
            self.engage()
            return "write_lock"
        try:
            state = self.history.execution_state(self.run_id)
        except Exception:
            self.engage()
            return "write_lock"
        if state.stopped:
            self.stop(state.stop_status, state.stop_reason)
            return "write_lock"
        closed = state.actions_reserved >= state.max_actions
        if closed and (expected is None or self.grant != expected.action_id):
            return "write_lock"
        return None

    def write_sent(self, expected: "ExpectedRequest") -> None:
        """Consome a concessão na mesma seção crítica: nenhuma segunda escrita sai."""
        if self.grant is not None and self.grant == expected.action_id:
            self.engage()

    def take_notice(self) -> Notice | None:
        return self.notices.pop(0) if self.notices else None

    # Encerramento ------------------------------------------------------------------------------

    async def shutdown(self, status: str, reason: str) -> dict[str, Any]:
        """Drena por até 10 s a ação corrente e finaliza dentro do deadline de 15 s."""
        from lastro.browser.lifecycle import finalize

        started = self.monotonic()
        drain_until = started + DRAIN_SECONDS
        final_until = started + FINALIZE_SECONDS
        lock = self.runtime.session.lock
        acquired = False
        try:
            await asyncio.wait_for(lock.acquire(), timeout=max(0.0, drain_until - started))
            acquired = True
        except TimeoutError:
            # A ação corrente não terminou na drenagem: o contexto é fechado por baixo dela.
            acquired = False
        try:
            self.result = await finalize(
                self.runtime,
                RunStatus(status),
                reason,
                skip_reason=reason,
                drain_until=drain_until,
                final_until=final_until,
                monotonic=self.monotonic,
            )
        except Exception as error:
            self.result = {"status": status, "report_status": "failed", "report_error": str(error)}
        finally:
            if acquired:
                lock.release()
            self.finished.set()
        return self.result

    async def wait(self, timeout: float | None = None) -> dict[str, Any] | None:
        if self.stopping is None:
            return None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.finished.wait(), timeout)
        return self.result

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.stopping is not None:
            with contextlib.suppress(Exception):
                await self.stopping
