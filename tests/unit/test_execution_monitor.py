import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

from lastro.browser.tools import BrowserTools
from lastro.config.loader import ProjectContext
from lastro.execution.monitor import EXECUTION_LOCK, ExecutionMonitor
from lastro.history import RunStatus, TextMission, open_history


class Ticker:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _runtime(context: ProjectContext, clock: Callable[[], datetime]) -> tuple[BrowserTools, str]:
    history = open_history(context, clock=clock)
    run = history.create_run(context, TextMission("Explore employees"))
    return BrowserTools(context, history, run.record.id, clock=clock), run.record.id


def test_warning_once_at_eighty_percent(
    history_context: ProjectContext,
    clock_now: list[datetime],
    fake_clock: Callable[[], datetime],
) -> None:
    runtime, run_id = _runtime(history_context, fake_clock)
    ticker = Ticker()
    monitor = ExecutionMonitor(runtime, monotonic=ticker, wall_clock=fake_clock)

    async def scenario() -> None:
        clock_now[0] += timedelta(minutes=24)
        monitor.tick()
        monitor.tick()

    asyncio.run(scenario())
    notice = monitor.take_notice()
    assert notice is not None and notice.dimension == "duration"
    assert monitor.take_notice() is None
    assert runtime.history.execution_state(run_id).duration_warned


def test_deadline_runs_without_tool_calls(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> None:
    runtime, run_id = _runtime(history_context, fake_clock)
    ticker = Ticker()
    monitor = ExecutionMonitor(runtime, monotonic=ticker, wall_clock=fake_clock, poll_seconds=0.01)
    runtime.monitor = monitor

    async def scenario() -> dict[str, object] | None:
        await monitor.start()
        ticker.now += 30 * 60
        # Nenhuma tool é chamada: o monitor sozinho detecta o prazo.
        for _ in range(100):
            if monitor.stopping is not None:
                break
            await asyncio.sleep(0.01)
        result = await monitor.wait(15)
        await monitor.close()
        return result

    result = asyncio.run(scenario())
    assert EXECUTION_LOCK in runtime.session.policy.locks
    record = runtime.history.get_run(run_id)
    assert (record.status, record.status_reason) == (RunStatus.LIMIT_REACHED, "duration_limit")
    assert result is not None and result["report_kind"] == "partial"


def test_waiting_time_consumes_duration(
    history_context: ProjectContext,
    clock_now: list[datetime],
    fake_clock: Callable[[], datetime],
) -> None:
    runtime, _ = _runtime(history_context, fake_clock)
    # O owner só abre 29 minutos depois do início: a espera já consumiu o orçamento.
    clock_now[0] += timedelta(minutes=29)
    ticker = Ticker()
    monitor = ExecutionMonitor(runtime, monotonic=ticker, wall_clock=fake_clock)
    monitor.arm()
    ticker.now += 59
    assert not monitor.expired()
    ticker.now += 1
    assert monitor.expired()


def test_wall_clock_jump_does_not_extend_budget(
    history_context: ProjectContext,
    clock_now: list[datetime],
    fake_clock: Callable[[], datetime],
) -> None:
    runtime, _ = _runtime(history_context, fake_clock)
    ticker = Ticker()
    monitor = ExecutionMonitor(runtime, monotonic=ticker, wall_clock=fake_clock)
    monitor.arm()
    clock_now[0] -= timedelta(hours=2)
    monitor.arm()
    ticker.now += 30 * 60
    assert monitor.expired()
