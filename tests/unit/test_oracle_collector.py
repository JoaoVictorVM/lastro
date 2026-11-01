import asyncio

import pytest

from lastro.config.loader import ProjectContext
from lastro.history import EventType, TextMission, open_history
from lastro.oracles.collector import OracleCollector
from lastro.oracles.ignore import IgnoreRule


def test_classification_window_isolation_and_popup(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("capture")).record.id
    action = store.append_event(run_id, EventType.TOOL_CALL, correlation_id="a")
    clock = [10.0]
    failures: list[Exception] = []
    collector = OracleCollector(
        store, run_id, [], lambda text: text, failures.append, monotonic=lambda: clock[0]
    )
    page, other, popup = object(), object(), object()

    async def scenario() -> None:
        collector.mark_action(page, "a", action.sequence)
        collector.inherit(popup, page)
        for elapsed, source in [(0, page), (5, page), (5.001, page), (1, other), (2, popup)]:
            clock[0] = 10 + elapsed
            collector.capture(
                "console", page=source, url="https://example.test/x", text=str(elapsed)
            )
        for status in [200, 302, 399, 400, 401, 403, 422, 499, 500, 599, 600]:
            collector.capture("response", page=page, url="https://example.test/x", status=status)
        for level in ["info", "warning", "debug", "log"]:
            collector.capture("console", page=page, url=None, level=level)
        collector.capture("requestfailed", page=page, url=None, aborted=True)
        collector.capture("requestfailed", page=page, url=None, has_response=True)
        collector.capture("requestfailed", page=page, url=None, text="net::ERR_FAILED")
        collector.capture("pageerror", page=page, url=None, text="uncaught")
        await collector.drain()
        collector.reset()
        collector.capture("console", page=page, url=None, text="after restart")
        await collector.drain()

    asyncio.run(scenario())
    assert failures == []
    signals = store.list_oracle_signals(run_id)
    by_message = {signal.message: signal.last_occurrence for signal in signals}
    assert len(signals) == 13
    for message in ["0", "5", "2"]:
        assert by_message[message].action_id == "a"
    for message in ["5.001", "1", "after restart"]:
        assert by_message[message].action_id is None
    assert by_message["HTTP 400"].signal_type == "http_4xx"
    assert by_message["HTTP 599"].signal_type == "http_5xx"
    assert by_message["net::ERR_FAILED"].signal_type == "request_failed"
    assert by_message["uncaught"].signal_type == "js_error"


def test_secrets_redacted_before_truncation_and_ignore(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("capture")).record.id
    failures: list[Exception] = []
    collector = OracleCollector(
        store,
        run_id,
        [IgnoreRule(url_path="/noise/*")],
        lambda text: text.replace("fictional-private-value", "[REDACTED]"),
        failures.append,
    )

    async def scenario() -> None:
        collector.capture("console", page=None, url="https://x.test/noise/1", text="ignored")
        collector.capture(
            "console",
            page=None,
            url="https://user:pass@x.test/path?token=fictional#fragment",
            text="fictional-private-value" + "x" * 9000,
        )
        await collector.drain()

    asyncio.run(scenario())
    (signal,) = store.list_oracle_signals(run_id)
    assert signal.last_occurrence.url == "https://x.test/path"
    assert len(signal.message) == 8000
    assert signal.message.startswith("[REDACTED]")
    assert signal.message.endswith("...")
    assert "fictional-private-value" not in str(store.list_events(run_id))


@pytest.mark.parametrize("elapsed", [-0.1, 5.00001])
def test_background_outside_window(history_context: ProjectContext, elapsed: float) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("capture")).record.id
    clock = [10.0]
    collector = OracleCollector(
        store, run_id, [], lambda text: text, lambda error: None, monotonic=lambda: clock[0]
    )

    async def scenario() -> None:
        collector.mark_action("page", "unused", 2)
        clock[0] += elapsed
        collector.capture("pageerror", page="page", url=None, text="error")
        await collector.drain()

    asyncio.run(scenario())
    assert store.list_oracle_signals(run_id)[0].last_occurrence.action_id is None
