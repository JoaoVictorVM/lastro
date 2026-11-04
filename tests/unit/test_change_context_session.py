import asyncio
from typing import Any

from lastro.browser.session import BrowserSession


class FakeResponse:
    def __init__(self, ok: bool, body: str) -> None:
        self.ok, self.body = ok, body

    async def text(self) -> str:
        return self.body


class FakeRequest:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.urls.append(url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeContext:
    def __init__(self, request: FakeRequest) -> None:
        self.request = request


class FakeHistory:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, str]] = []

    def observe_executed_version(
        self, run_id: str, executed: str | None, *, method: str, origin: str
    ) -> None:
        self.calls.append((executed, method, origin))


class FakeProfile:
    def __init__(self, endpoint: str | None) -> None:
        self.version_endpoint = endpoint
        self.target_url = "https://app.example.test"


class FakeProject:
    def __init__(self, endpoint: str | None) -> None:
        self.profile = FakeProfile(endpoint)


def _session(endpoint: str | None, response: FakeResponse | Exception) -> Any:
    session: Any = object.__new__(BrowserSession)
    session.project = FakeProject(endpoint)
    session.history = FakeHistory()
    session.run_id = "run_x"
    session.version_observed = False
    session.context = FakeContext(FakeRequest(response))
    return session


def test_version_is_read_once_from_trusted_endpoint() -> None:
    session = _session("/version", FakeResponse(True, '{"commit": "' + "a" * 40 + '"}'))
    asyncio.run(BrowserSession.observe_version(session))
    asyncio.run(BrowserSession.observe_version(session))
    assert session.history.calls == [("a" * 40, "profile_version_endpoint", "/version")]
    assert session.context.request.urls == ["https://app.example.test/version"]


def test_cross_origin_or_failed_endpoint_stays_unknown() -> None:
    cross = _session("https://evil.example/version", FakeResponse(True, "a" * 40))
    asyncio.run(BrowserSession.observe_version(cross))
    assert cross.history.calls == [(None, "cross_origin_refused", "none")]
    assert cross.context.request.urls == []
    absent = _session(None, FakeResponse(True, "x"))
    asyncio.run(BrowserSession.observe_version(absent))
    assert absent.history.calls == [(None, "not_configured", "none")]
    failing = _session("/version", RuntimeError("boom"))
    asyncio.run(BrowserSession.observe_version(failing))
    assert failing.history.calls == [(None, "profile_version_endpoint", "/version")]
