import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from playwright.async_api import Browser, BrowserContext, Page

from lastro.auth import (
    SESSION_REFRESHED_MESSAGE,
    AuthSessionManager,
    AuthTrigger,
    LoginCredentials,
    LoginFailedError,
    MissingCredentialError,
    SessionState,
    SessionStateStore,
    TenantVerificationResult,
    TenantVerifier,
    VerificationMethod,
    VerificationStatus,
)
from lastro.config.loader import ProjectContext
from lastro.config.schema import Profile
from lastro.history import EventType, HistoryStore, HistoryUnavailableError

PASSWORD = "senha-sentinela-sessao-31"
ENVIRON = {"HOMOLOG_USERNAME": "usuario@example.test", "HOMOLOG_PASSWORD": PASSWORD}
STATE = {"cookies": [{"name": "sid", "value": "cookie-sentinela-55", "domain": "x"}], "origins": []}


@dataclass
class FakeContext:
    state: dict[str, Any]
    storage_seed: object = None
    closed: bool = False

    async def storage_state(self) -> dict[str, Any]:
        return self.state

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeBrowser:
    contexts: list[FakeContext] = field(default_factory=list[FakeContext])

    async def new_context(self, **kwargs: Any) -> FakeContext:
        context = FakeContext(state=STATE, storage_seed=kwargs.get("storage_state"))
        self.contexts.append(context)
        return context


@dataclass
class ScriptedVerifier:
    """Devolve resultados na ordem; o último se repete. Registra o estado do gerenciador."""

    statuses: list[tuple[VerificationStatus, str]]
    manager: AuthSessionManager | None = None
    calls: list[tuple[object, object]] = field(default_factory=list[tuple[object, object]])
    context_during_call: list[object] = field(default_factory=list[object])
    now: datetime = datetime(2026, 10, 30, 17, 32, 5)

    async def verify(self, context: object, page: object = None) -> TenantVerificationResult:
        if self.manager is not None:
            self.context_during_call.append(self.manager.verified_context)
        self.calls.append((context, page))
        index = min(len(self.calls) - 1, len(self.statuses) - 1)
        status, reason = self.statuses[index]
        verified = status is VerificationStatus.VERIFIED
        return TenantVerificationResult(
            status=status,
            tenant_expected="acme",
            tenant_display_name="Acme",
            tenant_observed="acme" if verified else ("outra" if reason == "mismatch" else None),
            method=VerificationMethod.API,
            verified_at=self.now if verified else None,
            reason_code=reason,
            detail="HTTP 500" if status is VerificationStatus.UNVERIFIABLE else "ok",
        )


VERIFIED = (VerificationStatus.VERIFIED, "match")
MISMATCH = (VerificationStatus.MISMATCH, "mismatch")
UNVERIFIABLE = (VerificationStatus.UNVERIFIABLE, "http_error")
EXPIRED = (VerificationStatus.UNVERIFIABLE, "not_authenticated")


@dataclass
class FakeLogin:
    fail: bool = False
    delay: float = 0
    calls: int = 0
    seen_passwords: list[str] = field(default_factory=list[str])

    async def __call__(
        self, context: BrowserContext, profile: Profile, credentials: LoginCredentials
    ) -> Page:
        self.calls += 1
        self.seen_passwords.append(credentials.password)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise LoginFailedError(profile.name, "still_on_login_page")
        return cast(Page, object())


@dataclass
class FakeHistory:
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fail: bool = False

    def append_event(self, run_id: str, event_type: EventType, *, payload: object) -> None:
        if self.fail:
            raise HistoryUnavailableError()
        assert event_type is EventType.AUTHENTICATION
        self.events.append({"run_id": run_id, **cast(dict[str, Any], payload)})


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


@dataclass
class Harness:
    manager: AuthSessionManager
    verifier: ScriptedVerifier
    login: FakeLogin
    browser: FakeBrowser
    store: SessionStateStore
    history: FakeHistory | None

    @property
    def subtypes(self) -> list[str]:
        assert self.history is not None
        return [event["subtype"] for event in self.history.events]


@pytest.fixture
def make_harness(
    history_context: ProjectContext, fake_clock: Callable[[], datetime]
) -> Callable[..., Harness]:
    def make(
        *statuses: tuple[VerificationStatus, str],
        history: bool = True,
        environ: dict[str, str] | None = None,
        login: FakeLogin | None = None,
    ) -> Harness:
        verifier = ScriptedVerifier(list(statuses) or [VERIFIED])
        fake_login = login or FakeLogin()
        store = SessionStateStore(history_context.project_data_dir, clock=fake_clock)
        fake_history = FakeHistory() if history else None
        manager = AuthSessionManager(
            history_context,
            history=cast(HistoryStore, fake_history) if fake_history else None,
            run_id="run-1" if fake_history else None,
            clock=fake_clock,
            environ=ENVIRON if environ is None else environ,
            store=store,
            verifier=cast(TenantVerifier, verifier),
            login=fake_login,
        )
        verifier.manager = manager
        return Harness(manager, verifier, fake_login, FakeBrowser(), store, fake_history)

    return make


def browser(harness: Harness) -> Browser:
    return cast(Browser, harness.browser)


def test_start_logs_in_verifies_and_publishes(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(VERIFIED)

    outcome = run(harness.manager.start(browser(harness)))

    assert outcome.ref is not None
    assert outcome.result.is_verified
    assert harness.manager.state is SessionState.VERIFIED
    assert harness.manager.verified_context is not None
    assert harness.login.calls == 1
    assert harness.store.load("homolog") is not None
    assert harness.subtypes == ["login_succeeded", "tenant_verified"]
    assert all(context.closed for context in harness.browser.contexts)
    assert harness.history is not None
    assert harness.history.events[1] == {
        "run_id": "run-1",
        "subtype": "tenant_verified",
        "profile": "homolog",
        "trigger": "session_start",
        "verification_method": "api",
        "tenant_expected": "acme",
        "tenant_observed": "acme",
    }


def test_valid_state_is_reused_without_login(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(VERIFIED)
    harness.store.publish("homolog", STATE)

    outcome = run(harness.manager.start(browser(harness)))

    assert outcome.reused
    assert harness.login.calls == 0
    assert harness.browser.contexts[0].storage_seed == STATE
    assert harness.subtypes == ["session_reused"]


def test_expired_state_is_renewed_before_use(
    make_harness: Callable[..., Harness], clock_now: list[datetime]
) -> None:
    harness = make_harness(VERIFIED)
    harness.store.publish("homolog", STATE)
    clock_now[0] = clock_now[0] + timedelta(hours=8)

    outcome = run(harness.manager.start(browser(harness)))

    assert not outcome.reused
    assert harness.login.calls == 1
    assert harness.subtypes == ["login_succeeded", "tenant_verified"]


def test_reused_state_rejected_by_target_triggers_single_login(
    make_harness: Callable[..., Harness],
) -> None:
    harness = make_harness(EXPIRED, VERIFIED)
    harness.store.publish("homolog", STATE)

    outcome = run(harness.manager.start(browser(harness)))

    assert outcome.result.is_verified
    assert harness.login.calls == 1
    assert len(harness.verifier.calls) == 2


def test_corrupt_state_is_removed_without_disclosure(
    make_harness: Callable[..., Harness], capsys: pytest.CaptureFixture[str]
) -> None:
    harness = make_harness(VERIFIED)
    path = harness.store.path_for("homolog")
    path.parent.mkdir(parents=True)
    path.write_text('{"cookies": [ "conteudo-sentinela-corrompido', encoding="utf-8")

    outcome = run(harness.manager.start(browser(harness)))

    assert harness.login.calls == 1
    assert outcome.result.is_verified
    captured = capsys.readouterr()
    assert "conteudo-sentinela-corrompido" not in captured.out + captured.err
    assert harness.history is not None
    assert "conteudo-sentinela-corrompido" not in repr(harness.history.events)
    assert "conteudo-sentinela-corrompido" not in path.read_text(encoding="utf-8")


def test_fresh_login_with_mismatch_is_not_published(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(MISMATCH)

    outcome = run(harness.manager.start(browser(harness)))

    assert outcome.ref is None
    assert outcome.result.status is VerificationStatus.MISMATCH
    assert harness.manager.state is SessionState.MISMATCH
    assert harness.manager.verified_context is None
    assert not harness.store.path_for("homolog").exists()
    assert harness.subtypes == ["login_succeeded", "tenant_mismatch"]


def test_unverifiable_records_reason(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(UNVERIFIABLE)

    run(harness.manager.start(browser(harness)))

    assert harness.manager.verified_context is None
    assert harness.history is not None
    assert harness.history.events[-1] == {
        "run_id": "run-1",
        "subtype": "tenant_unverifiable",
        "profile": "homolog",
        "trigger": "session_start",
        "verification_method": "api",
        "reason_code": "http_error",
    }


def test_pre_write_always_reverifies(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(VERIFIED)
    run(harness.manager.start(browser(harness)))
    context = cast(BrowserContext, object())
    decisions: list[bool] = []

    for _ in range(2):
        result = run(harness.manager.before_write(context))
        decisions.append(harness.manager.verified_context is not None and result.allows_writes)

    assert decisions == [True, True]
    assert len(harness.verifier.calls) == 3
    assert harness.verifier.context_during_call[1:] == [None, None]
    assert harness.subtypes[-2:] == ["tenant_verified", "tenant_verified"]
    assert harness.history is not None
    assert harness.history.events[-1]["trigger"] == "pre_write"


def test_401_refreshes_once_without_replaying_write(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(VERIFIED, login=FakeLogin(delay=0.05))
    run(harness.manager.start(browser(harness)))
    writes: list[str] = []

    async def scenario() -> list[object]:
        writes.append("POST /employees/1")
        # Duas respostas 401 simultâneas compartilham a mesma renovação.
        return list(
            await asyncio.gather(
                harness.manager.handle_unauthorized(browser(harness)),
                harness.manager.handle_unauthorized(browser(harness)),
            )
        )

    outcomes = run(scenario())

    assert writes == ["POST /employees/1"]
    assert harness.login.calls == 2
    assert outcomes[0] is outcomes[1]
    assert SESSION_REFRESHED_MESSAGE == "Session refreshed"
    assert harness.subtypes[-2:] == ["login_succeeded", "session_refreshed"]
    assert harness.history is not None
    assert harness.history.events[-1]["trigger"] == "unauthorized"

    result = run(harness.manager.before_write(cast(BrowserContext, object())))
    assert result.allows_writes
    assert harness.subtypes[-1] == "tenant_verified"


def test_origin_change_invalidates_until_reverified(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(VERIFIED)
    run(harness.manager.start(browser(harness)))

    assert not harness.manager.handle_navigation("https://homolog.example.test/employees")
    assert harness.manager.verified_context is not None

    assert harness.manager.handle_navigation("https://evil.example.test/")
    assert harness.manager.verified_context is None
    assert harness.manager.state is SessionState.INVALIDATED

    assert harness.manager.handle_navigation("https://homolog.example.test/employees")
    run(harness.manager.verify(cast(BrowserContext, object()), trigger=AuthTrigger.ORIGIN_CHANGE))
    assert harness.manager.verified_context is not None


def test_login_redirect_and_browser_restart_renew_session(
    make_harness: Callable[..., Harness],
) -> None:
    harness = make_harness(VERIFIED)
    run(harness.manager.start(browser(harness)))

    refreshed = run(harness.manager.handle_login_redirect(browser(harness)))
    restarted = run(harness.manager.handle_browser_restart(browser(harness)))

    assert refreshed.refreshed
    assert restarted.reused
    assert harness.history is not None
    assert harness.history.events[-1]["trigger"] == "browser_restart"
    assert harness.history.events[-1]["subtype"] == "session_reused"


def test_missing_credential_stops_before_browser(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(environ={"HOMOLOG_USERNAME": "usuario"})

    with pytest.raises(MissingCredentialError) as caught:
        run(harness.manager.start(browser(harness)))

    assert caught.value.variable == "HOMOLOG_PASSWORD"
    assert harness.browser.contexts == []
    assert harness.subtypes == ["login_failed"]


def test_login_failure_is_recorded_and_nothing_published(
    make_harness: Callable[..., Harness],
) -> None:
    harness = make_harness(login=FakeLogin(fail=True))

    with pytest.raises(LoginFailedError) as caught:
        run(harness.manager.start(browser(harness)))

    assert caught.value.message == (
        "Login failed for profile 'homolog'. "
        "Check credentials and login selectors; no actions were executed."
    )
    assert harness.subtypes == ["login_failed"]
    assert harness.manager.verified_context is None
    assert not harness.store.path_for("homolog").exists()


def test_history_failure_blocks_session_delivery(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(VERIFIED)
    assert harness.history is not None
    harness.history.fail = True

    with pytest.raises(HistoryUnavailableError):
        run(harness.manager.start(browser(harness)))

    assert harness.manager.verified_context is None
    assert harness.manager.session_ref is None


def test_password_never_reaches_events_or_results(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(VERIFIED)

    outcome = run(harness.manager.start(browser(harness)))

    assert harness.login.seen_passwords == [PASSWORD]
    assert PASSWORD not in repr(outcome)
    assert harness.history is not None
    assert PASSWORD not in repr(harness.history.events)


def test_manager_requires_history_and_run_together(history_context: ProjectContext) -> None:
    with pytest.raises(ValueError, match="juntos"):
        AuthSessionManager(history_context, run_id="run-1")


def test_open_context_uses_published_state(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(VERIFIED)
    run(harness.manager.start(browser(harness)))

    run(harness.manager.open_context(browser(harness)))

    assert harness.browser.contexts[-1].storage_seed == STATE
