"""Contexto da run com interceptação instalada antes de criar páginas."""

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Request,
    Response,
    Route,
    async_playwright,
)

from lastro.auth import AuthSessionManager
from lastro.browser.bindings import ExpectedRequest, denied_endpoint, path_parameters
from lastro.browser.network import AuxiliaryTransport
from lastro.browser.policy import PolicyEngine
from lastro.browser.provenance import Provenance
from lastro.config.loader import ProjectContext
from lastro.config.policy import safe_path
from lastro.config.schema import is_same_origin
from lastro.history import EventType, HistoryStore, RunStatus


class BrowserSession:
    def __init__(
        self,
        project: ProjectContext,
        history: HistoryStore,
        run_id: str,
        active: Callable[[], None],
    ) -> None:
        self.project, self.history, self.run_id = project, history, run_id
        self.active = active
        self.policy = PolicyEngine(project.profile)
        self.auth = AuthSessionManager(project, history=history, run_id=run_id)
        self.auxiliary = AuxiliaryTransport(project.profile.policy.auxiliary_origins)
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.playwright: Any = None
        self.expected: ExpectedRequest | None = None
        self.crashes: list[float] = []
        self.crashed = False
        self.needs_refresh = False
        self.network_denial: str | None = None
        self.request_actions: dict[Request, ExpectedRequest] = {}
        self.provenance = Provenance(history, run_id, project.profile)
        self.lock = asyncio.Lock()
        self.secrets: set[str] = {
            value
            for key in project.profile.credential_variable_names()
            if (value := os.environ.get(key))
        }

    def sanitize(self, text: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    async def start(self) -> Page:
        self.active()
        if self.crashed:
            await self.recover()
            raise ValueError("Browser restarted; re-observe the page before acting.")
        if self.needs_refresh and self.browser is not None:
            self.expected = None
            outcome = await self.auth.handle_unauthorized(self.browser)
            if self.context is not None:
                await self.context.close()
            await self.open_context()
            self.needs_refresh = False
            if not outcome.result.is_verified:
                self.policy.locks.add("tenant")
            raise ValueError("Session refreshed; re-observe the page before acting.")
        if self.page is not None:
            return self.page
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch()
        assert self.browser is not None
        self.browser.on("disconnected", self.on_crash)
        await self.auth.start(self.browser)
        self.active()
        await self.open_context()
        assert self.page is not None
        return self.page

    async def open_context(self) -> None:
        assert self.browser is not None
        self.context = await self.auth.open_context(self.browser)
        self.secrets.update(
            cookie.get("value", "")
            for cookie in await self.context.cookies()
            if cookie.get("value")
        )
        await self.context.route("**/*", self.route)
        await self.context.route_web_socket("**/*", lambda ws: ws.close())
        self.context.on("response", self.response)
        self.context.on("page", self.new_page)
        self.page = await self.context.new_page()
        if self.auth.verified_context is None:
            self.policy.locks.add("tenant")

    def new_page(self, page: Page) -> None:
        page.on("crash", self.on_crash)
        page.on("download", lambda download: download.cancel())
        page.on("console", lambda message: self.record_signal("console", message.text))
        page.on("pageerror", lambda error: self.record_signal("pageerror", str(error)))

    def record_signal(self, kind: str, text: str) -> None:
        try:
            self.active()
            self.history.append_event(
                self.run_id,
                EventType.POLICY_DECISION,
                policy_decision="allow",
                payload={"source": kind, "text": self.sanitize(text[:8000])},
            )
        except Exception:
            self.policy.locks.add("history")

    def on_crash(self, *_: object) -> None:
        self.crashed = True
        self.expected = None
        self.auth.invalidate()
        self.policy.locks.add("browser")

    async def recover(self) -> None:
        now = time.monotonic()
        self.crashes = [moment for moment in self.crashes if now - moment < 300]
        if self.crashes:
            self.history.finish_run(self.run_id, RunStatus.BLOCKED, "browser_crash")
            await self.close()
            raise ValueError("Browser repeatedly crashed; run blocked.")
        self.crashes.append(now)
        if self.context is not None:
            await self.context.close()
        assert self.browser is not None
        if not self.browser.is_connected():
            self.browser = await self.playwright.chromium.launch()
            assert self.browser is not None
            self.browser.on("disconnected", self.on_crash)
        await self.auth.handle_browser_restart(self.browser)
        await self.open_context()
        self.crashed = False
        self.policy.locks.discard("browser")

    async def verify(self, *, recovery: bool = False) -> bool:
        assert self.context is not None
        self.expected = None
        result = await self.auth.before_write(self.context, self.page)
        self.active()
        if not result.is_verified:
            self.policy.locks.add("tenant")
        elif recovery:
            self.policy.locks.discard("tenant")
        return result.is_verified

    async def route(self, route: Route) -> None:
        request = route.request
        try:
            self.active()
            profile = self.project.profile
            url = request.url
            parts = urlsplit(url)
            safe_path(parts.path or "/")
            reason: str | None = None
            if parts.scheme not in {"http", "https"} or parts.username or parts.password:
                reason = "invalid_url"
            elif request.method in profile.denied_methods:
                reason = "denied_method"
            elif denied_endpoint("/__demo/**", url) or any(
                denied_endpoint(pattern, url) for pattern in profile.denied_endpoint_patterns
            ):
                reason = "denied_endpoint"
            elif not is_same_origin(url, profile.target_url):
                if request.is_navigation_request() or request.method != "GET":
                    reason = "outside_target"
                elif self.auxiliary.authorize_jwks_origin(url):
                    status, headers, body = await asyncio.to_thread(self.auxiliary.fetch, url)
                    self.active()
                    await route.fulfill(
                        status=status,
                        body=body,
                        headers={
                            "content-type": headers.get("content-type", "application/octet-stream")
                        },
                    )
                    return
                else:
                    reason = "outside_target"
            elif request.method not in {"GET", "HEAD", "OPTIONS"} or any(
                binding.operation != "read"
                and binding.request is not None
                and binding.request.method == request.method
                and path_parameters(binding.request.path, parts.path) is not None
                for binding in profile.policy.bindings
            ):
                if self.policy.locks or self.auth.verified_context is None:
                    reason = "write_lock"
                elif self.expected is None or not self.expected.consume(
                    request.method, url, request.post_data
                ):
                    reason = "unbound_write"
                else:
                    self.request_actions[request] = self.expected
            if reason:
                self.network_denial = reason
                self.history.append_event(
                    self.run_id,
                    EventType.POLICY_DECISION,
                    policy_decision="deny",
                    payload={
                        "source": "network",
                        "method": request.method,
                        "path": parts.path,
                        "reason": reason,
                    },
                )
                await route.abort("blockedbyclient")
                return
            self.active()
            await route.continue_()
        except Exception:
            self.policy.locks.add("history")
            await route.abort("blockedbyclient")

    async def response(self, response: Response) -> None:
        request: Request = response.request
        expected = self.request_actions.pop(request, None)
        action_id = expected.action_id if expected else None
        if response.status in {401, 403} or (
            request.is_navigation_request()
            and urlsplit(response.url).path == urlsplit(self.project.profile.login_url).path
        ):
            self.auth.invalidate()
            self.needs_refresh = True
            self.policy.locks.add("tenant")
            self.expected = None
        try:
            self.active()
            if expected is not None:
                binding = expected.binding
                contract = binding.request
                if (
                    binding.operation == "create"
                    and contract is not None
                    and response.status == contract.success_status
                ):
                    record: object = None
                    if contract.location_id:
                        from urllib.parse import urljoin

                        location = urljoin(response.url, response.headers.get("location", ""))
                        if is_same_origin(location, self.project.profile.target_url):
                            record = urlsplit(location).path.rstrip("/").rsplit("/", 1)[-1]
                    elif contract.response_id:
                        data: Any = await response.json()
                        for key in contract.response_id.split("."):
                            data = data[key]
                        record = data
                    if isinstance(record, str | int) and not isinstance(record, bool):
                        self.provenance.record(binding.entity, str(record), expected.action_id)
            self.history.append_event(
                self.run_id,
                EventType.POLICY_DECISION,
                policy_decision="allow",
                payload={
                    "source": "response",
                    "status": response.status,
                    "path": urlsplit(response.url).path,
                    "method": request.method,
                    "action_id": action_id,
                },
            )
        except Exception:
            self.policy.locks.add("history")

    async def close(self) -> None:
        self.expected = None
        if self.context is not None:
            await self.context.close()
        if self.browser is not None:
            await self.browser.close()
        if self.playwright is not None:
            await self.playwright.stop()
        self.context = self.page = self.browser = None
