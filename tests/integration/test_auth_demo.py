import os
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from playwright.async_api import Browser

from lastro.auth import (
    AuthSessionManager,
    LoginFailedError,
    SessionState,
    VerificationMethod,
    VerificationStatus,
    failure_message,
)

BROKEN_ME = (
    '[[profiles.{name}.auth.verification]]\ntype = "api"\nendpoint = "/__test/me-broken"\n'
    'tenant_json_path = "tenant.id"\n'
)
WITH_USER = (
    '[[profiles.{name}.auth.verification]]\ntype = "api"\nendpoint = "/api/me"\n'
    'tenant_json_path = "tenant.id"\nuser_json_path = "user.email"\n'
)
PAGE_ONLY = '[[profiles.{name}.auth.verification]]\ntype = "page"\nselector = "#active-clinic"\n'


def test_demo_single_and_multi_tenant_login(
    make_auth_project: Callable[..., Any],
    profile_for: Callable[..., str],
    demo_server: Any,
    with_browser: Callable[..., Any],
) -> None:
    project = make_auth_project(
        profile_for("aurora", demo_server.url, auth=WITH_USER.format(name="aurora")),
        profile_for(
            "multi",
            demo_server.url,
            tenant="boreal",
            display="Clínica Boreal",
            user_env="DEMO_MULTI_USER",
            password_env="DEMO_MULTI_PASSWORD",
        ),
        profile_for(
            "pagina",
            demo_server.url,
            auth=PAGE_ONLY.format(name="pagina"),
        ),
    )

    async def scenario(browser: Browser) -> list[Any]:
        outcomes = []
        for name in ("aurora", "multi", "pagina"):
            manager = AuthSessionManager(project.context(name))
            outcomes.append(await manager.start(browser))
            context = await manager.open_context(browser)
            response = await context.request.get(f"{demo_server.url}/api/me")
            outcomes.append(await response.json())
            await context.close()
        return outcomes

    aurora, aurora_me, multi, multi_me, page, _ = with_browser(scenario)

    assert aurora.result.status is VerificationStatus.VERIFIED
    assert aurora.result.observed_user == "aurora.teste@example.com"
    assert aurora_me["tenant"]["id"] == "aurora"
    assert multi.result.status is VerificationStatus.VERIFIED
    assert multi.result.observed_user is None
    assert multi_me["tenant"] == {"id": "boreal", "name": "Clínica Boreal"}
    assert page.result.method is VerificationMethod.PAGE
    assert page.result.is_verified


def test_state_file_is_private_and_under_project_root(
    make_auth_project: Callable[..., Any], with_browser: Callable[..., Any]
) -> None:
    project = make_auth_project()

    async def scenario(browser: Browser) -> Any:
        return await AuthSessionManager(project.context()).start(browser)

    outcome = with_browser(scenario)

    path = project.project_dir / ".auth" / "demo.json"
    assert outcome.ref is not None
    assert outcome.ref.path == path.resolve()
    if sys.platform == "win32":
        from lastro.auth import winacl

        assert winacl.is_owner_only(path)
        assert winacl.is_owner_only(path.parent)
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_switched_tenant_blocks_writes_but_reads_continue(
    make_auth_project: Callable[..., Any],
    profile_for: Callable[..., str],
    demo_server: Any,
    with_browser: Callable[..., Any],
) -> None:
    project = make_auth_project(
        profile_for(
            "demo",
            demo_server.url,
            user_env="DEMO_MULTI_USER",
            password_env="DEMO_MULTI_PASSWORD",
        )
    )

    async def scenario(browser: Browser) -> tuple[Any, ...]:
        manager = AuthSessionManager(project.context())
        started = await manager.start(browser)
        context = await manager.open_context(browser)
        before = await manager.before_write(context)
        await context.request.post(f"{demo_server.url}/select-clinic", form={"tenant": "boreal"})
        writes = [await manager.before_write(context) for _ in range(2)]
        read = await context.request.get(f"{demo_server.url}/employees")
        state = manager.state
        verified = manager.verified_context
        await context.close()
        return started, before, writes, read.status, state, verified

    started, before, writes, read_status, state, verified = with_browser(scenario)

    assert started.result.is_verified
    assert before.allows_writes
    assert all(item.status is VerificationStatus.MISMATCH for item in writes)
    assert not any(item.allows_writes for item in writes)
    assert failure_message(writes[0]) == (
        "Active tenant 'boreal' does not match authorized tenant 'aurora'. "
        "Write actions are blocked for this run."
    )
    assert state is SessionState.MISMATCH
    assert verified is None
    assert read_status == 200


def test_verification_endpoint_500_blocks_writes(
    make_auth_project: Callable[..., Any],
    profile_for: Callable[..., str],
    demo_server: Any,
    with_browser: Callable[..., Any],
) -> None:
    project = make_auth_project(
        profile_for("demo", demo_server.url, auth=BROKEN_ME.format(name="demo"))
    )

    async def scenario(browser: Browser) -> Any:
        manager = AuthSessionManager(project.context())
        outcome = await manager.start(browser)
        return outcome, manager.verified_context

    outcome, verified = with_browser(scenario)

    assert outcome.result.status is VerificationStatus.UNVERIFIABLE
    assert not outcome.result.allows_writes
    assert verified is None
    assert failure_message(outcome.result) == (
        "Tenant could not be verified (api: HTTP 500). Write actions are blocked."
    )


def test_expired_session_is_refreshed_and_reverified(
    make_auth_project: Callable[..., Any],
    demo_server: Any,
    with_browser: Callable[..., Any],
) -> None:
    project = make_auth_project()
    now = [datetime.now(UTC)]

    async def scenario(browser: Browser) -> dict[str, Any]:
        manager = AuthSessionManager(project.context(), clock=lambda: now[0])
        first = await manager.start(browser)
        context = await manager.open_context(browser)
        # O reset do demo invalida todas as sessões emitidas, como uma expiração no servidor.
        await context.request.post(f"{demo_server.url}/__demo/reset", data="{}")
        expired = await manager.before_write(context)
        await context.close()
        refreshed = await manager.handle_unauthorized(browser)
        renewed_context = await manager.open_context(browser)
        after = await manager.before_write(renewed_context)
        await renewed_context.close()
        now[0] = now[0] + timedelta(hours=8)
        aged = await manager.start(browser)
        return {
            "first": first,
            "expired": expired,
            "refreshed": refreshed,
            "after": after,
            "aged": aged,
        }

    result = with_browser(scenario)

    assert result["first"].result.is_verified
    assert result["expired"].reason_code == "not_authenticated"
    assert not result["expired"].allows_writes
    assert result["refreshed"].refreshed
    assert result["refreshed"].ref != result["first"].ref
    assert result["after"].allows_writes
    assert not result["aged"].reused
    assert result["aged"].result.is_verified


def test_wrong_password_fails_login_with_profile_message(
    make_auth_project: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    with_browser: Callable[..., Any],
) -> None:
    project = make_auth_project()
    monkeypatch.setenv("DEMO_AURORA_PASSWORD", "senha-errada-sentinela")

    async def scenario(browser: Browser) -> LoginFailedError:
        with pytest.raises(LoginFailedError) as caught:
            await AuthSessionManager(project.context(), environ=dict(os.environ)).start(browser)
        return caught.value

    error = with_browser(scenario)

    assert error.message == (
        "Login failed for profile 'demo'. "
        "Check credentials and login selectors; no actions were executed."
    )
    assert not (project.project_dir / ".auth" / "demo.json").exists()
