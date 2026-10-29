"""Fábrica da aplicação: rotas, sessão, templates, estáticos e log de acesso."""

import json
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response

from lastro_demo import __version__, routes_auth, routes_control, routes_employees
from lastro_demo.config import DemoConfig
from lastro_demo.flags import FlagState
from lastro_demo.runtime import DemoRuntime
from lastro_demo.security import LoginRequired, TenantRequired

SESSION_COOKIE = "lastro_demo_session"


class AccessLog:
    """Uma linha JSON por requisição, em modo de acréscimo; o reset nunca trunca o arquivo."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, entry: dict[str, object]) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def create_app(config: DemoConfig, flags: FlagState | None = None) -> FastAPI:
    runtime = DemoRuntime(config=config, flags=flags if flags is not None else FlagState.all_on())
    runtime.initialize()
    access_log = AccessLog(config.access_log_path)

    app = FastAPI(
        title="Demo do Lastro",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = runtime
    app.state.access_log = access_log

    @app.middleware("http")
    async def log_access(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            access_log.write(
                {
                    "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
                    "client": request.client.host if request.client else None,
                    "method": request.method,
                    "path": request.url.path,
                    "query": request.url.query,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret,
        session_cookie=SESSION_COOKIE,
        same_site="lax",
        https_only=False,
    )

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        if _is_api(request):
            return JSONResponse(
                {"error": "not_authenticated", "message": "Faça login para continuar."},
                status_code=401,
            )
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(TenantRequired)
    async def tenant_required(request: Request, exc: TenantRequired) -> Response:
        if _is_api(request):
            return JSONResponse(
                {"error": "tenant_not_selected", "message": "Selecione uma clínica."},
                status_code=409,
            )
        return RedirectResponse("/select-clinic", status_code=303)

    app.mount("/static", StaticFiles(packages=[("lastro_demo", "static")]), name="static")
    app.include_router(routes_auth.router)
    app.include_router(routes_employees.router)
    app.include_router(routes_control.router)
    return app
