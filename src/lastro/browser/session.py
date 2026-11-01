"""Contexto da run com interceptação instalada antes de criar páginas."""

import asyncio
import contextlib
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Page,
    Request,
    Response,
    Route,
    WebError,
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
from lastro.oracles.collector import OracleCollector


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
        self.action_sequence = 0
        self.closing_context = False
        self.aborted: dict[Request, str] = {}
        self.blocked_console: dict[tuple[Page | None, str], tuple[int, float]] = {}
        self.auth_console: dict[tuple[Page | None, str], tuple[int, float]] = {}
        self.response_tasks: set[asyncio.Task[None]] = set()
        self.secrets: set[str] = {
            value
            for key in project.profile.credential_variable_names()
            if (value := os.environ.get(key))
        }
        self.oracles = OracleCollector(
            history, run_id, project.profile.oracles.ignore, self.sanitize, self.oracle_failure
        )

    def oracle_failure(self, error: Exception) -> None:
        self.policy.locks.add("history")
        with contextlib.suppress(Exception):
            self.history.append_event(
                self.run_id,
                EventType.POLICY_DECISION,
                policy_decision="deny",
                payload={
                    "reason": "oracle_collection_failed",
                    "diagnostic": self.sanitize(str(error)),
                },
            )
            self.history.finish_run(self.run_id, RunStatus.BLOCKED, "oracle_collection_failed")

    def mark_action(self, page: Page, action_id: str) -> None:
        self.oracles.mark_action(page, action_id, self.action_sequence)

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
                await self.close_context()
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
        self.closing_context = False
        self.oracles.reset()
        self.context = await self.auth.open_context(self.browser)
        self.secrets.update(
            cookie.get("value", "")
            for cookie in await self.context.cookies()
            if cookie.get("value")
        )
        await self.context.route("**/*", self.route)
        await self.context.route_web_socket("**/*", lambda ws: ws.close())
        self.context.on("response", self.response)
        self.context.on("console", self.console)
        self.context.on("weberror", self.web_error)
        self.context.on("requestfailed", self.request_failed)
        self.context.on("requestfinished", self.request_finished)
        self.context.on("page", self.new_page)
        self.page = await self.context.new_page()
        if self.auth.verified_context is None:
            self.policy.locks.add("tenant")

    def new_page(self, page: Page) -> None:
        page.on("crash", self.on_crash)
        page.on("download", lambda download: download.cancel())
        page.on("popup", lambda popup: self.oracles.inherit(popup, page))
        page.on("close", self.forget_page)

    def forget_page(self, page: Page) -> None:
        self.oracles.actions.pop(page, None)

    def console(self, message: ConsoleMessage) -> None:
        try:
            location = message.location.get("url") or ""
            key = (message.page, location)
            auth_response = self.auth_console.get(key)
            if (
                auth_response is not None
                and time.monotonic() - auth_response[1] <= 5
                and message.text.startswith(
                    "Failed to load resource: the server responded with a status of "
                    + str(auth_response[0])
                    + " "
                )
            ):
                self.auth_console.pop(key, None)
                return
            blocked = self.blocked_console.get(key)
            if (
                blocked is not None
                and time.monotonic() - blocked[1] <= 5
                and message.text
                in {
                    "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT",
                    "Failed to load resource: net::ERR_BLOCKED_BY_CLIENT.Inspector",
                }
            ):
                if blocked[0] <= 1:
                    self.blocked_console.pop(key, None)
                else:
                    self.blocked_console[key] = (blocked[0] - 1, blocked[1])
                return
            self.oracles.capture(
                "console",
                page=message.page,
                url=location or (message.page.url if message.page else None),
                text=message.text,
                level=message.type,
            )
        except Exception as error:
            self.oracles.fail(error)

    def web_error(self, error: WebError) -> None:
        self.oracles.capture(
            "pageerror",
            page=error.page,
            url=error.page.url if error.page else None,
            text=str(error.error),
        )

    @staticmethod
    def request_page(request: Request) -> Page | None:
        try:
            return request.frame.page
        except Exception:
            return None

    def mark_abort(self, request: Request, reason: str) -> None:
        self.aborted[request] = reason
        now = time.monotonic()
        self.blocked_console = {
            key: value for key, value in self.blocked_console.items() if now - value[1] <= 5
        }
        key = (self.request_page(request), request.url)
        count = self.blocked_console.get(key, (0, now))[0]
        self.blocked_console[key] = (count + 1, now)

    def request_finished(self, request: Request) -> None:
        self.aborted.pop(request, None)
        self.request_actions.pop(request, None)

    def request_failed(self, request: Request) -> None:
        reason = self.aborted.pop(request, None)
        self.request_actions.pop(request, None)
        self.oracles.capture(
            "requestfailed",
            page=self.request_page(request),
            url=request.url,
            text=request.failure or "Network request failed",
            method=request.method,
            aborted=reason is not None or self.closing_context,
            has_response=request.existing_response is not None,
        )

    def on_crash(self, *_: object) -> None:
        self.crashed = True
        self.expected = None
        self.auth.invalidate()
        self.policy.locks.add("browser")

    async def recover(self) -> None:
        now = time.monotonic()
        self.crashes = [moment for moment in self.crashes if now - moment < 300]
        if self.crashes:
            await self.close()
            self.history.finish_run(self.run_id, RunStatus.BLOCKED, "browser_crash")
            raise ValueError("Browser repeatedly crashed; run blocked.")
        self.crashes.append(now)
        if self.context is not None:
            await self.close_context()
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
                self.mark_abort(request, reason)
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
            self.mark_abort(request, "history")
            await route.abort("blockedbyclient")

    def response(self, response: Response) -> None:
        if response.status in {401, 403}:
            now = time.monotonic()
            self.auth_console = {
                key: value for key, value in self.auth_console.items() if now - value[1] <= 5
            }
            self.auth_console[(self.request_page(response.request), response.url)] = (
                response.status,
                now,
            )
        self.oracles.capture(
            "response",
            page=self.request_page(response.request),
            url=response.url,
            status=response.status,
            method=response.request.method,
        )
        task = asyncio.create_task(self.process_response(response))
        self.response_tasks.add(task)
        task.add_done_callback(self.response_tasks.discard)

    async def process_response(self, response: Response) -> None:
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

    async def close_context(self) -> None:
        self.closing_context = True
        self.expected = None
        self.oracles.accepting = False
        try:
            await self.oracles.drain()
            if self.response_tasks:
                await asyncio.gather(*tuple(self.response_tasks))
        finally:
            if self.context is not None:
                for event, handler in (
                    ("response", self.response),
                    ("console", self.console),
                    ("weberror", self.web_error),
                    ("requestfailed", self.request_failed),
                    ("requestfinished", self.request_finished),
                    ("page", self.new_page),
                ):
                    self.context.remove_listener(event, handler)
                await self.context.close()
            self.context = self.page = None
            self.request_actions.clear()
            self.aborted.clear()
            self.blocked_console.clear()
            self.auth_console.clear()
            self.oracles.actions.clear()

    async def close(self) -> None:
        try:
            await self.close_context()
        finally:
            if self.browser is not None:
                self.browser.remove_listener("disconnected", self.on_crash)
                await self.browser.close()
            if self.playwright is not None:
                await self.playwright.stop()
            self.context = self.page = self.browser = None
