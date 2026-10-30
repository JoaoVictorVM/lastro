import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import Browser, BrowserContext, Page
from typer.testing import CliRunner

from lastro.auth import (
    AuthSessionManager,
    LoginCredentials,
    SessionStateStore,
    TenantVerificationResult,
    TenantVerifier,
    VerificationMethod,
    VerificationStatus,
    failure_message,
)
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.config.schema import Profile
from lastro.history import (
    EventType,
    HistoryStore,
    HistoryUnavailableError,
    RetryPolicy,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.history.database import Database
from lastro.history.layout import HistoryLayout

SECRET = "sentinela-token-redacao-8812"


@dataclass
class OfflineContext:
    async def storage_state(self) -> dict[str, Any]:
        return {"cookies": [], "origins": []}

    async def close(self) -> None:
        return None


@dataclass
class OfflineBrowser:
    async def new_context(self, **kwargs: Any) -> OfflineContext:
        return OfflineContext()


async def offline_login(
    context: BrowserContext, profile: Profile, credentials: LoginCredentials
) -> Page:
    return cast(Page, object())


@dataclass
class LeakyVerifier:
    """Tenant observado com padrão de segredo: só a redação central do histórico o remove."""

    calls: list[object] = field(default_factory=list[object])

    async def verify(self, context: object, page: object = None) -> TenantVerificationResult:
        self.calls.append(context)
        return TenantVerificationResult(
            status=VerificationStatus.MISMATCH,
            tenant_expected="acme",
            tenant_display_name="Acme",
            tenant_observed=f"session_token={SECRET}",
            method=VerificationMethod.API,
            verified_at=None,
            reason_code="tenant_mismatch",
            detail="active tenant differs from authorized tenant",
        )


def offline_manager(
    context: ProjectContext, store: HistoryStore, run_id: str, verifier: object = None
) -> AuthSessionManager:
    return AuthSessionManager(
        context,
        history=store,
        run_id=run_id,
        environ={"HOMOLOG_USERNAME": "usuario", "HOMOLOG_PASSWORD": "senha-offline"},
        verifier=cast(TenantVerifier, verifier or LeakyVerifier()),
        login=offline_login,
    )


def _all_bytes(project_dir: Path) -> bytes:
    return b"".join(path.read_bytes() for path in sorted(project_dir.rglob("*")) if path.is_file())


def test_active_run_auth_uses_public_history_api(
    make_auth_project: Callable[..., Any], with_browser: Callable[..., Any]
) -> None:
    project = make_auth_project()
    context = project.context()
    store = open_history(context)
    run_id = store.create_run(context, TextMission("Explorar cadastro")).record.id

    async def scenario(browser: Browser) -> Any:
        manager = AuthSessionManager(context, history=store, run_id=run_id)
        outcome = await manager.start(browser)
        run_context = await manager.open_context(browser)
        await manager.before_write(run_context)
        await run_context.close()
        return outcome

    outcome = with_browser(scenario)

    assert outcome.result.is_verified
    events = [
        event for event in store.list_events(run_id) if event.event_type is EventType.AUTHENTICATION
    ]
    payloads = [cast(dict[str, Any], event.payload) for event in events]
    assert [payload["subtype"] for payload in payloads] == [
        "login_succeeded",
        "tenant_verified",
        "tenant_verified",
    ]
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences)
    assert all(event.correlation_id is None and event.idempotency_key is None for event in events)
    assert all(event.result is None for event in events)
    assert payloads[1] == {
        "subtype": "tenant_verified",
        "profile": "demo",
        "trigger": "session_start",
        "verification_method": "api",
        "tenant_expected": "aurora",
        "tenant_observed": "aurora",
    }
    assert payloads[2]["trigger"] == "pre_write"


def test_auth_event_uses_central_redaction(history_context: ProjectContext) -> None:
    store = open_history(history_context)
    run_id = store.create_run(history_context, TextMission("Explorar")).record.id
    manager = offline_manager(history_context, store, run_id)

    outcome = asyncio.run(manager.start(cast(Browser, OfflineBrowser())))

    assert outcome.result.status is VerificationStatus.MISMATCH
    events = store.list_events(run_id)
    mismatch = next(
        event
        for event in events
        if cast(dict[str, Any], event.payload or {}).get("subtype") == "tenant_mismatch"
    )
    assert mismatch.payload is not None
    assert SECRET not in json.dumps(mismatch.payload)
    assert "[REDACTED]" in json.dumps(mismatch.payload)
    assert SECRET.encode() not in _all_bytes(store.project_dir)
    assert b"senha-offline" not in _all_bytes(store.project_dir)


def test_history_failure_prevents_session_delivery(history_context: ProjectContext) -> None:
    opened = open_history(history_context)
    run_id = opened.create_run(history_context, TextMission("Explorar")).record.id
    layout = HistoryLayout(opened.project_dir)

    def locked(*args: object, **kwargs: Any) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    locked_store = HistoryStore(
        layout,
        Database(layout.database, RetryPolicy(sleep=lambda seconds: None), connector=locked),
        project_id=opened.project_id,
        timezone=opened.timezone,
    )
    verifier = LeakyVerifier()
    manager = offline_manager(history_context, locked_store, run_id, verifier)

    with pytest.raises(HistoryUnavailableError):
        asyncio.run(manager.start(cast(Browser, OfflineBrowser())))

    assert manager.verified_context is None
    assert manager.session_ref is None
    assert verifier.calls == []
    assert opened.get_run(run_id).status is RunStatus.BLOCKED
    fallback = layout.fallback(run_id)
    assert fallback.is_file()
    assert "senha-offline" not in fallback.read_text(encoding="utf-8")
    assert SessionStateStore(history_context.project_data_dir).load("homolog") is None


def test_password_absent_from_all_observable_outputs(
    make_auth_project: Callable[..., Any],
    with_browser: Callable[..., Any],
    demo_server: Any,
    capfd: pytest.CaptureFixture[str],
    runner: CliRunner,
) -> None:
    project = make_auth_project()
    context = project.context()
    _, password = demo_server.user("aurora")
    store = open_history(context)
    run_id = store.create_run(context, TextMission("Explorar cadastro")).record.id

    async def scenario(browser: Browser) -> list[str]:
        manager = AuthSessionManager(context, history=store, run_id=run_id)
        outcome = await manager.start(browser)
        run_context = await manager.open_context(browser)
        write = await manager.before_write(run_context)
        await run_context.close()
        tool_results = [repr(outcome), repr(write), repr(manager.verified_context)]
        if not write.is_verified:
            tool_results.append(failure_message(write))
        return tool_results

    tool_results = with_browser(scenario)
    cli = runner.invoke(app, ["--config", str(project.config_path), "auth", "check"])
    events = [
        {"type": event.event_type.value, "payload": event.payload}
        for event in store.list_events(run_id)
    ]
    report = json.dumps({"run": run_id, "events": events}, ensure_ascii=False)
    transcript = capfd.readouterr()

    assert cli.exit_code == 0, cli.output
    observable = {
        "tool results": "\n".join(tool_results),
        "events": json.dumps(events, ensure_ascii=False),
        "report": report,
        "cli": cli.output,
        "transcript": transcript.out + transcript.err,
    }
    for name, text in observable.items():
        assert password not in text, name
    assert password.encode() not in _all_bytes(project.project_dir)
