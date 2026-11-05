import json

import pytest

from lastro.codex.events import (
    MAX_DISPLAY_CHARS,
    MAX_LINE_BYTES,
    REASON_ERROR,
    REASON_LIMIT,
    REASON_NETWORK,
    REASON_RATE_LIMIT,
    CodexProtocolError,
    Failure,
    classify_exec_event,
    classify_rollout_record,
    display_text,
    parse_line,
)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "turn.failed", "error": {"codex_error_info": "usage_limit_exceeded"}},
        {"type": "turn.failed", "error": {"message": "You\u2019ve hit your usage limit. Upgrade."}},
        {"type": "error", "codex_error_info": "usage_limit_exceeded", "message": "x"},
    ],
)
def test_usage_limit_classification(event: dict[str, object]) -> None:
    assert classify_exec_event(event) == Failure(REASON_LIMIT)


def test_rollout_limit_comes_only_from_cli_events() -> None:
    reached = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {"rate_limit_reached_type": "usage_limit_reached"},
        },
    }
    assert classify_rollout_record(reached) == Failure(REASON_LIMIT)
    calm = {
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": {"rate_limit_reached_type": None}},
    }
    assert classify_rollout_record(calm) is None
    spoken = {
        "type": "response_item",
        "payload": {"type": "message", "text": "You\u2019ve hit your usage limit."},
    }
    assert classify_rollout_record(spoken) is None
    said = {"type": "event_msg", "payload": {"type": "agent_message", "message": "usage limit"}}
    assert classify_rollout_record(said) is None


def test_agent_text_never_means_limit() -> None:
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": "usage limit"}}
    assert classify_exec_event(event) is None


@pytest.mark.parametrize("code", ["rate_limit_exceeded", "server_overloaded"])
def test_rate_limit_is_not_quota(code: str) -> None:
    event = {"type": "turn.failed", "error": {"codex_error_info": code}}
    assert classify_exec_event(event) == Failure(REASON_RATE_LIMIT)


def test_network_failure_has_its_own_reason() -> None:
    event = {
        "type": "turn.failed",
        "error": {"codex_error_info": {"http_connection_failed": {"status": 502}}},
    }
    assert classify_exec_event(event) == Failure(REASON_NETWORK)


def test_unknown_error_is_interrupted() -> None:
    assert classify_exec_event({"type": "turn.failed", "error": {"message": "boom"}}) == Failure(
        REASON_ERROR
    )
    assert classify_exec_event({"type": "turn.failed"}) == Failure(REASON_ERROR)


def test_reconnect_notice_does_not_end_the_session() -> None:
    assert classify_exec_event({"type": "error", "message": "Reconnecting... 1/5"}) is None


@pytest.mark.parametrize("line", [b"not json\n", b"[1]\n", b'{"type":"turn'])
def test_invalid_jsonl(line: bytes) -> None:
    with pytest.raises(CodexProtocolError):
        parse_line(line)


def test_oversized_line() -> None:
    event = json.dumps({"type": "x", "text": "a" * MAX_LINE_BYTES}).encode()
    with pytest.raises(CodexProtocolError, match="size limit"):
        parse_line(event)


def test_output_redacted() -> None:
    event = {
        "type": "item.completed",
        "item": {
            "type": "agent_message",
            "text": "token: abc Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz " + "x" * 900,
        },
    }
    shown = display_text(event)
    assert shown is not None
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in shown
    assert len(shown) <= MAX_DISPLAY_CHARS + 3
    assert display_text({"type": "item.completed", "item": {"type": "reasoning"}}) is None
