"""Orquestração da sessão autenticada: reuso, login, refresh coalescido, gatilhos e eventos.

Somente uma sessão cujo tenant foi verificado expõe `verified_context`. Qualquer gatilho
descarta o contexto antes de nova verificação, e a falha ao registrar o evento no histórico
impede a entrega da sessão.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from playwright.async_api import Browser, BrowserContext, Page, StorageState, async_playwright

from lastro.auth.credentials import LoginCredentials, resolve_credentials
from lastro.auth.login import perform_login
from lastro.auth.models import (
    AuthenticatedSessionRef,
    AuthEventSubtype,
    AuthTrigger,
    LoginFailedError,
    MissingCredentialError,
    SessionState,
    TenantVerificationResult,
    VerificationStatus,
    VerifiedTenantContext,
)
from lastro.auth.storage import SessionStateStore
from lastro.auth.tenant import SESSION_EXPIRED_REASONS, JwksOriginPolicy, TenantVerifier
from lastro.config.loader import ProjectContext
from lastro.config.schema import Profile, is_same_origin
from lastro.history import EventType, HistoryStore
from lastro.timeutil import utc_now

type Clock = Callable[[], datetime]
type LoginFunction = Callable[[BrowserContext, Profile, LoginCredentials], Awaitable[Page]]

_STATE_BY_STATUS = {
    VerificationStatus.VERIFIED: SessionState.VERIFIED,
    VerificationStatus.MISMATCH: SessionState.MISMATCH,
    VerificationStatus.UNVERIFIABLE: SessionState.UNVERIFIABLE,
}
_SUBTYPE_BY_STATUS = {
    VerificationStatus.VERIFIED: AuthEventSubtype.TENANT_VERIFIED,
    VerificationStatus.MISMATCH: AuthEventSubtype.TENANT_MISMATCH,
    VerificationStatus.UNVERIFIABLE: AuthEventSubtype.TENANT_UNVERIFIABLE,
}


@dataclass(frozen=True)
class SessionOutcome:
    """Resultado de início ou refresh; `ref` só existe com estado publicado e reutilizável."""

    ref: AuthenticatedSessionRef | None
    result: TenantVerificationResult
    reused: bool = False
    refreshed: bool = False


class AuthSessionManager:
    def __init__(
        self,
        project: ProjectContext,
        *,
        history: HistoryStore | None = None,
        run_id: str | None = None,
        clock: Clock = utc_now,
        environ: Mapping[str, str] | None = None,
        jwks_origin_policy: JwksOriginPolicy | None = None,
        store: SessionStateStore | None = None,
        verifier: TenantVerifier | None = None,
        login: LoginFunction = perform_login,
    ) -> None:
        if (history is None) != (run_id is None):
            raise ValueError("history e run_id são informados juntos ou omitidos juntos")
        self.profile = project.profile
        self._history = history
        self._run_id = run_id
        self._environ = environ if environ is not None else os.environ
        self._store = store or SessionStateStore(project.project_data_dir, clock=clock)
        self._verifier = verifier or TenantVerifier(
            self.profile, clock=clock, jwks_origin_policy=jwks_origin_policy
        )
        self._login = login
        self._state = SessionState.UNAUTHENTICATED
        self._verified: VerifiedTenantContext | None = None
        self._ref: AuthenticatedSessionRef | None = None
        self._refresh_task: asyncio.Task[SessionOutcome] | None = None

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def verified_context(self) -> VerifiedTenantContext | None:
        """Contexto apto para avaliação de escrita; None em qualquer outro estado."""
        return self._verified if self._state is SessionState.VERIFIED else None

    @property
    def session_ref(self) -> AuthenticatedSessionRef | None:
        return self._ref

    async def start(
        self, browser: Browser, trigger: AuthTrigger = AuthTrigger.SESSION_START
    ) -> SessionOutcome:
        """Reutiliza estado com menos de 8 h ou autentica, e verifica o tenant."""
        self.invalidate()
        credentials = self._credentials(trigger)
        loaded = self._store.load(self.profile.name)
        if loaded is not None:
            context = await browser.new_context(storage_state=cast(StorageState, loaded.state))
            try:
                result = await self._verifier.verify(context)
            finally:
                await context.close()
            if result.reason_code not in SESSION_EXPIRED_REASONS:
                if result.is_verified:
                    self._record(
                        AuthEventSubtype.SESSION_REUSED, trigger, **self._result_fields(result)
                    )
                else:
                    self._record_result(result, trigger)
                self._ref = loaded.ref
                self._apply(result)
                return SessionOutcome(ref=loaded.ref, result=result, reused=True)
            self._store.delete(self.profile.name)
        return await self._authenticate(browser, credentials, trigger, refreshed=False)

    async def refresh(self, browser: Browser, trigger: AuthTrigger) -> SessionOutcome:
        """Renova em contexto limpo; chamadas simultâneas compartilham a mesma renovação."""
        self.invalidate()
        running = self._refresh_task
        if running is not None and not running.done():
            return await asyncio.shield(running)
        task = asyncio.ensure_future(self._refresh(browser, trigger))
        self._refresh_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._refresh_task is task and task.done():
                self._refresh_task = None

    async def _refresh(self, browser: Browser, trigger: AuthTrigger) -> SessionOutcome:
        credentials = self._credentials(trigger)
        return await self._authenticate(browser, credentials, trigger, refreshed=True)

    async def handle_unauthorized(self, browser: Browser) -> SessionOutcome:
        """Resposta 401/403: uma renovação coalescida; a request original nunca é repetida."""
        return await self.refresh(browser, AuthTrigger.UNAUTHORIZED)

    async def handle_login_redirect(self, browser: Browser) -> SessionOutcome:
        return await self.refresh(browser, AuthTrigger.LOGIN_REDIRECT)

    async def handle_browser_restart(self, browser: Browser) -> SessionOutcome:
        return await self.start(browser, AuthTrigger.BROWSER_RESTART)

    def handle_navigation(self, url: str) -> bool:
        """Descarta o contexto ao sair da origem do target; True quando é preciso revalidar."""
        if not is_same_origin(url, self.profile.target_url):
            self.invalidate()
            return True
        return self._state is not SessionState.VERIFIED

    async def verify(
        self,
        context: BrowserContext,
        page: Page | None = None,
        trigger: AuthTrigger = AuthTrigger.PRE_WRITE,
    ) -> TenantVerificationResult:
        """Verifica o tenant no contexto corrente; o contexto anterior é descartado antes."""
        self.invalidate()
        result = await self._verifier.verify(context, page)
        self._record_result(result, trigger)
        self._apply(result)
        return result

    async def before_write(
        self, context: BrowserContext, page: Page | None = None
    ) -> TenantVerificationResult:
        return await self.verify(context, page, AuthTrigger.PRE_WRITE)

    async def open_context(self, browser: Browser) -> BrowserContext:
        """Abre um contexto com o estado publicado, sem expor o conteúdo ao chamador."""
        if self._ref is None:
            raise LoginFailedError(self.profile.name, "no_session")
        state = self._store.read_for_context(self._ref, self.profile.name)
        return await browser.new_context(storage_state=cast(StorageState, state))

    def invalidate(self) -> None:
        self._verified = None
        if self._state is not SessionState.UNAUTHENTICATED:
            self._state = SessionState.INVALIDATED

    def _credentials(self, trigger: AuthTrigger) -> LoginCredentials:
        try:
            return resolve_credentials(self.profile, self._environ)
        except MissingCredentialError as exc:
            self._record(AuthEventSubtype.LOGIN_FAILED, trigger, reason_code=exc.reason_code)
            raise

    async def _authenticate(
        self,
        browser: Browser,
        credentials: LoginCredentials,
        trigger: AuthTrigger,
        *,
        refreshed: bool,
    ) -> SessionOutcome:
        context = await browser.new_context()
        try:
            try:
                page = await self._login(context, self.profile, credentials)
            except LoginFailedError as exc:
                self._record(AuthEventSubtype.LOGIN_FAILED, trigger, reason_code=exc.reason_code)
                raise
            self._record(AuthEventSubtype.LOGIN_SUCCEEDED, trigger)
            result = await self._verifier.verify(context, page)
            state: StorageState | None = (
                await context.storage_state() if result.is_verified else None
            )
        finally:
            await context.close()
        if state is None:
            self._record_result(result, trigger)
            self._apply(result)
            return SessionOutcome(ref=None, result=result, refreshed=refreshed)
        ref = self._store.publish(self.profile.name, cast(dict[str, Any], state))
        if refreshed:
            self._record(AuthEventSubtype.SESSION_REFRESHED, trigger, **self._result_fields(result))
        else:
            self._record_result(result, trigger)
        self._ref = ref
        self._apply(result)
        return SessionOutcome(ref=ref, result=result, refreshed=refreshed)

    def _apply(self, result: TenantVerificationResult) -> None:
        self._state = _STATE_BY_STATUS[result.status]
        self._verified = result.verified_context()

    def _result_fields(self, result: TenantVerificationResult) -> dict[str, str | None]:
        if result.status is VerificationStatus.UNVERIFIABLE:
            return {"verification_method": result.method_label, "reason_code": result.reason_code}
        return {
            "verification_method": result.method_label,
            "tenant_expected": result.tenant_expected,
            "tenant_observed": result.tenant_observed,
        }

    def _record_result(self, result: TenantVerificationResult, trigger: AuthTrigger) -> None:
        self._record(_SUBTYPE_BY_STATUS[result.status], trigger, **self._result_fields(result))

    def _record(
        self, subtype: AuthEventSubtype, trigger: AuthTrigger, **fields: str | None
    ) -> None:
        if self._history is None or self._run_id is None:
            return
        payload: dict[str, str | None] = {
            "subtype": subtype.value,
            "profile": self.profile.name,
            "trigger": trigger.value,
            **fields,
        }
        self._history.append_event(self._run_id, EventType.AUTHENTICATION, payload=payload)


async def run_auth_check(
    project: ProjectContext, *, environ: Mapping[str, str] | None = None
) -> SessionOutcome:
    """Diagnóstico sem run: mesmo login e verificação, sem histórico e sem eventos."""
    manager = AuthSessionManager(project, environ=environ)
    resolve_credentials(project.profile, environ)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            return await manager.start(browser, AuthTrigger.DIAGNOSTIC)
        finally:
            await browser.close()
