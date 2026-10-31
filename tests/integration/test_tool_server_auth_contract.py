"""Contrato consumido pelo servidor de tools: só um tenant verificado libera escrita."""

import asyncio
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from typing import Any, Literal, cast

import pytest
from playwright.async_api import Browser, BrowserContext, Page

from lastro.auth import (
    AuthenticatedSessionRef,
    AuthSessionManager,
    LoginCredentials,
    SessionRefError,
    SessionStateStore,
    TenantVerificationResult,
    TenantVerifier,
    VerificationMethod,
    VerificationStatus,
    VerifiedTenantContext,
)
from lastro.config.loader import ProjectContext
from lastro.config.schema import Profile

type Decision = Literal["allow", "deny"]


def tool_server_decision(manager: AuthSessionManager, kind: Literal["read", "write"]) -> Decision:
    """Consumidor mínimo: escrita só com contexto verificado; leitura segue em qualquer estado."""
    if kind == "read":
        return "allow"
    return "allow" if manager.verified_context is not None else "deny"


@dataclass
class Context:
    async def storage_state(self) -> dict[str, Any]:
        return {"cookies": [], "origins": []}

    async def close(self) -> None:
        return None


@dataclass
class OfflineBrowser:
    async def new_context(self, **kwargs: Any) -> Context:
        return Context()


async def offline_login(
    context: BrowserContext, profile: Profile, credentials: LoginCredentials
) -> Page:
    return cast(Page, object())


@dataclass
class SwitchableVerifier:
    status: VerificationStatus = VerificationStatus.VERIFIED
    calls: list[object] = field(default_factory=list[object])

    async def verify(self, context: object, page: object = None) -> TenantVerificationResult:
        self.calls.append(context)
        verified = self.status is VerificationStatus.VERIFIED
        return TenantVerificationResult(
            status=self.status,
            tenant_expected="acme",
            tenant_display_name="Acme",
            tenant_observed="acme" if verified else None,
            method=VerificationMethod.API,
            verified_at=datetime(2026, 10, 30, 17, 0) if verified else None,
            reason_code="match" if verified else "http_error",
            detail="ok" if verified else "HTTP 500",
        )


def make_manager(context: ProjectContext) -> tuple[AuthSessionManager, SwitchableVerifier]:
    verifier = SwitchableVerifier()
    manager = AuthSessionManager(
        context,
        environ={"HOMOLOG_USERNAME": "usuario", "HOMOLOG_PASSWORD": "senha"},
        verifier=cast(TenantVerifier, verifier),
        login=offline_login,
    )
    return manager, verifier


def test_missing_or_mismatched_context_denies_all_writes(history_context: ProjectContext) -> None:
    manager, verifier = make_manager(history_context)
    browser = cast(Browser, OfflineBrowser())
    run_context = cast(BrowserContext, Context())

    assert tool_server_decision(manager, "write") == "deny"
    assert tool_server_decision(manager, "read") == "allow"

    asyncio.run(manager.start(browser))
    assert tool_server_decision(manager, "write") == "allow"

    for status in (VerificationStatus.MISMATCH, VerificationStatus.UNVERIFIABLE):
        verifier.status = status
        result = asyncio.run(manager.before_write(run_context))
        assert not result.allows_writes
        assert result.verified_context() is None
        assert tool_server_decision(manager, "write") == "deny"
        assert tool_server_decision(manager, "read") == "allow"

    verifier.status = VerificationStatus.VERIFIED
    asyncio.run(manager.before_write(run_context))
    assert tool_server_decision(manager, "write") == "allow"

    manager.handle_navigation("https://outra-origem.example.test/")
    assert tool_server_decision(manager, "write") == "deny"
    asyncio.run(manager.verify(run_context))
    assert tool_server_decision(manager, "write") == "allow"

    manager.invalidate()
    assert tool_server_decision(manager, "write") == "deny"


@pytest.mark.parametrize("status", [VerificationStatus.MISMATCH, VerificationStatus.UNVERIFIABLE])
def test_fresh_session_without_match_allows_reads_only(
    history_context: ProjectContext, status: VerificationStatus
) -> None:
    manager, verifier = make_manager(history_context)
    verifier.status = status
    browser = cast(Browser, OfflineBrowser())

    outcome = asyncio.run(manager.start(browser))

    assert outcome.ref is not None
    assert manager.session_ref is not None
    asyncio.run(manager.open_context(browser))
    assert tool_server_decision(manager, "read") == "allow"
    assert tool_server_decision(manager, "write") == "deny"


def test_contracts_carry_no_session_material() -> None:
    result_fields = {item.name for item in fields(TenantVerificationResult)}
    context_fields = {item.name for item in fields(VerifiedTenantContext)}
    ref_fields = {item.name for item in fields(AuthenticatedSessionRef)}

    assert result_fields == {
        "status",
        "tenant_expected",
        "tenant_display_name",
        "tenant_observed",
        "method",
        "verified_at",
        "reason_code",
        "detail",
        "observed_user",
    }
    assert context_fields == {"tenant_id", "tenant_display_name", "method", "verified_at"}
    assert ref_fields == {"profile_name", "path", "created_at", "expires_at"}


def test_session_ref_is_opaque_and_rejected_when_invalid(
    history_context: ProjectContext, fake_clock: Callable[[], datetime], clock_now: list[datetime]
) -> None:
    store = SessionStateStore(history_context.project_data_dir, clock=fake_clock)
    ref = store.publish("homolog", {"cookies": [], "origins": []})

    with pytest.raises(TypeError):
        pickle.dumps(ref)
    with pytest.raises(SessionRefError):
        store.validate_ref(ref, "local")
    clock_now[0] = clock_now[0] + timedelta(hours=8)
    with pytest.raises(SessionRefError):
        store.read_for_context(ref, "homolog")


def test_tool_server_uses_stored_session_state(
    make_auth_project: Callable[..., Any], demo_server: Any, with_browser: Callable[..., Any]
) -> None:
    project = make_auth_project()

    async def scenario(browser: Browser) -> tuple[int, Any, Any]:
        manager = AuthSessionManager(project.context())
        await manager.start(browser)
        assert manager.session_ref is not None
        restarted = AuthSessionManager(project.context())
        outcome = await restarted.handle_browser_restart(browser)
        context = await restarted.open_context(browser)
        response = await context.request.get(f"{demo_server.url}/api/me")
        body = await response.json()
        await context.close()
        return response.status, body, outcome

    status, body, outcome = with_browser(scenario)

    assert outcome.reused
    assert outcome.result.is_verified
    assert status == 200
    assert body["tenant"]["id"] == "aurora"
