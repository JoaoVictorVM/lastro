import asyncio
from unittest.mock import AsyncMock, Mock

from lastro.browser.bindings import ExpectedRequest
from lastro.browser.session import BrowserSession
from lastro.config.loader import ProjectContext
from lastro.config.policy import Binding, RequestBinding, Target
from lastro.history import TextMission, open_history


def test_request_completion_preserves_creation_proof(history_context: ProjectContext) -> None:
    history = open_history(history_context)
    run_id = history.create_run(history_context, TextMission("create")).record.id
    session = BrowserSession(history_context, history, run_id, lambda: None)
    binding = Binding(
        id="create",
        entity="employee",
        operation="create",
        route="/new",
        target=Target(role="button", name="save"),
        fields={},
        request=RequestBinding(method="POST", path="/api/employees", response_id="id"),
    )
    request = Mock()
    request.is_navigation_request.return_value = False
    request.method = "POST"
    url = history_context.profile.target_url + "/api/employees"
    response = Mock(request=request, status=201, url=url)
    response.json = AsyncMock(return_value={"id": "new-employee"})
    session.request_actions[request] = ExpectedRequest(binding, url, {}, "create-action")

    async def scenario() -> None:
        session.response(response)
        session.request_finished(request)
        await asyncio.gather(*tuple(session.response_tasks))

    asyncio.run(scenario())
    assert session.provenance.origin("employee", "new-employee") == "created_in_run"
