import asyncio
from datetime import UTC, date, datetime

from lastro import testdata
from lastro.browser.tools import NO_RUN, BrowserTools
from lastro.config.loader import ProjectContext
from lastro.history import EventType, TextMission, open_history
from lastro_demo.validation import is_valid_cpf

CLOCK = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)


def test_generate_employee_without_opening_browser(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("generate")).record.id
    runtime = BrowserTools(history_context, history, run_id, clock=lambda: CLOCK)

    result = asyncio.run(runtime.call("generate_test_data", {"entity": "employee"}))

    assert result["status"] == "ok"
    assert result["generation_index"] == 1
    assert result["reference_date"] == "2026-11-01"
    assert result["values"]["name"].startswith("LASTRO-" + run_id[-4:] + "-")
    assert result["values"]["email"] == f"lastro+{run_id}@example.com"
    assert is_valid_cpf(result["values"]["cpf"])
    assert runtime.session.page is None
    assert runtime.session.browser is None
    events = history.list_events(run_id)
    call = next(e for e in events if e.event_type == EventType.TOOL_CALL)
    decision = next(e for e in events if e.event_type == EventType.POLICY_DECISION)
    assert call.tool_name == "generate_test_data"
    assert decision.correlation_id == call.correlation_id
    assert decision.policy_decision is not None and decision.policy_decision.value == "allow"

    rejected = asyncio.run(runtime.call("generate_test_data", {"type": "passport"}))
    assert "Unsupported test data type 'passport'" in rejected["error"]
    denied = [e for e in history.list_events(run_id) if e.event_type == EventType.POLICY_DECISION]
    assert denied[-1].policy_decision is not None and denied[-1].policy_decision.value == "deny"

    history.finish_run(run_id, "completed")
    assert asyncio.run(runtime.call("generate_test_data", {"entity": "employee"})) == {
        "error": NO_RUN
    }


def test_tool_call_ordinal_reproduces_after_reopen(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("generate")).record.id
    first_runtime = BrowserTools(history_context, history, run_id, clock=lambda: CLOCK)
    employee = asyncio.run(first_runtime.call("generate_test_data", {"entity": "employee"}))
    cpf = asyncio.run(first_runtime.call("generate_test_data", {"type": "cpf"}))
    assert (employee["generation_index"], cpf["generation_index"]) == (1, 2)
    assert employee["values"]["cpf"] != cpf["value"]

    reopened = BrowserTools(
        history_context, open_history(history_context), run_id, clock=lambda: CLOCK
    )
    later = asyncio.run(reopened.call("generate_test_data", {"entity": "employee"}))

    assert later["generation_index"] == 3
    assert later["values"] != employee["values"]
    replay = testdata.generate(
        testdata.parse_request({"entity": "employee"}),
        run_id=run_id,
        generation_index=1,
        reference_date=date(2026, 11, 1),
    )
    assert replay.model_dump() == {k: v for k, v in employee.items() if k != "action_id"}
