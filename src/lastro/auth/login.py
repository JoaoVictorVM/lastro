"""Login determinístico por perfil: preenche, submete, escolhe o tenant e detecta sucesso.

Os valores de credencial só passam pelo `fill` do Playwright. Exceções do browser são
convertidas em `LoginFailedError` sem repassar a mensagem original.
"""

import asyncio
import contextlib
import time
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Locator, Page

from lastro.auth.credentials import LoginCredentials
from lastro.auth.models import LoginFailedError
from lastro.config.schema import Profile

POLL_INTERVAL_SECONDS = 0.2


async def _is_visible(page: Page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible()
    except Exception:
        return False


async def _submit_control(page: Page, field_selector: str, submit_selector: str) -> Locator:
    """Botão de envio do mesmo formulário do campo, quando houver um.

    O seletor padrão de submit também casa com outros botões da página (como o de sair no
    cabeçalho do demo depois do login); restringir ao formulário evita clicar no errado.
    """
    scoped = page.locator("form", has=page.locator(field_selector)).locator(submit_selector)
    if await scoped.count() > 0:
        return scoped.first
    return page.locator(submit_selector).first


def _on_login_page(page: Page, login_url: str) -> bool:
    current = urlsplit(page.url)
    expected = urlsplit(login_url)
    return (current.hostname, current.path.rstrip("/")) == (
        expected.hostname,
        expected.path.rstrip("/"),
    )


async def perform_login(
    context: BrowserContext, profile: Profile, credentials: LoginCredentials
) -> Page:
    """Autentica no contexto informado e devolve a página já fora do login.

    Continuar na página de login até o prazo do perfil é falha de login.
    """
    settings = profile.auth
    timeout_ms = settings.timeout_seconds * 1000
    deadline = time.monotonic() + settings.timeout_seconds
    page = await context.new_page()
    try:
        await page.goto(profile.login_url, timeout=timeout_ms)
        await page.locator(settings.username_selector).first.fill(
            credentials.username, timeout=timeout_ms
        )
        await page.locator(settings.password_selector).first.fill(
            credentials.password, timeout=timeout_ms
        )
        submit = await _submit_control(page, settings.password_selector, settings.submit_selector)
        await submit.click(timeout=timeout_ms)
    except Exception:
        await page.close()
        raise LoginFailedError(profile.name, "login_form_unavailable") from None
    try:
        await _wait_for_login(page, profile, deadline)
    except LoginFailedError:
        await page.close()
        raise
    except Exception:
        await page.close()
        raise LoginFailedError(profile.name, "login_error") from None
    return page


async def _wait_for_login(page: Page, profile: Profile, deadline: float) -> None:
    settings = profile.auth
    tenant_attempted = False
    while time.monotonic() < deadline:
        remaining_ms = max((deadline - time.monotonic()) * 1000, 1)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state(timeout=remaining_ms)
        if settings.tenant_selector and await _is_visible(page, settings.tenant_selector):
            # A escolha é feita uma única vez; se a seleção continuar visível até o prazo, falha.
            if not tenant_attempted:
                tenant_attempted = True
                await _select_tenant(page, profile, remaining_ms)
                continue
        else:
            password_visible = await _is_visible(page, settings.password_selector)
            if not password_visible and not _on_login_page(page, profile.login_url):
                return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    reason = "tenant_selection_failed" if tenant_attempted else "still_on_login_page"
    raise LoginFailedError(profile.name, reason)


async def _select_tenant(page: Page, profile: Profile, timeout_ms: float) -> None:
    settings = profile.auth
    assert settings.tenant_selector is not None
    assert settings.tenant_submit_selector is not None
    try:
        await page.locator(settings.tenant_selector).first.select_option(
            value=profile.tenant_id, timeout=timeout_ms
        )
        submit = await _submit_control(
            page, settings.tenant_selector, settings.tenant_submit_selector
        )
        await submit.click(timeout=timeout_ms)
    except Exception:
        raise LoginFailedError(profile.name, "tenant_unavailable") from None
