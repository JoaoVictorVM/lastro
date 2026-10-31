import asyncio
import os
import socket
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.async_api import Browser, async_playwright
from starlette.responses import JSONResponse

from lastro.config.loader import ProjectContext, load_project
from lastro.doctor.checks import playwright_browsers_dir
from lastro_demo.app import create_app
from lastro_demo.config import DemoConfig, load_seed_users
from lastro_demo.flags import FlagState

# Capturado antes do isolamento de HOME e LOCALAPPDATA feito pelo conftest raiz.
BROWSERS_DIR = playwright_browsers_dir()
AUTH_PROJECT_ID = "5b6c7d8e-9f0a-4b1c-8d2e-3f4a5b6c7d8e"
BROKEN_ME_PATH = "/__test/me-broken"


def chromium_available() -> bool:
    if BROWSERS_DIR is None or not BROWSERS_DIR.is_dir():
        return False
    return any(child.name.startswith("chromium") for child in BROWSERS_DIR.iterdir())


@pytest.fixture
def chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exige o Chromium do Playwright; no CI a ausência é falha, localmente é skip."""
    if not chromium_available():
        if os.environ.get("CI"):
            pytest.fail("Chromium do Playwright não instalado no CI")
        pytest.skip("Chromium do Playwright não instalado (playwright install chromium)")
    assert BROWSERS_DIR is not None
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS_DIR))


@dataclass(frozen=True)
class DemoServer:
    url: str
    state_dir: Path

    def user(self, key: str) -> tuple[str, str]:
        user = next(item for item in load_seed_users({}) if item.key == key)
        return user.email, user.password


def _with_test_routes(app: FastAPI) -> FastAPI:
    @app.get(BROKEN_ME_PATH)
    def broken_me() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse({"error": "boom"}, status_code=500)

    return app


@pytest.fixture
def demo_server(tmp_path: Path, chromium: None) -> Iterator[DemoServer]:
    state_dir = tmp_path / "demo-state"
    app = _with_test_routes(create_app(DemoConfig(state_dir=state_dir), FlagState.only()))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("demo não iniciou")
        time.sleep(0.05)
    try:
        yield DemoServer(url=f"http://127.0.0.1:{port}", state_dir=state_dir)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()


@dataclass(frozen=True)
class AuthProject:
    config_path: Path
    private_root: Path

    def context(self, profile: str = "demo") -> ProjectContext:
        return load_project(self.config_path, profile, cwd=self.config_path.parent)

    @property
    def project_dir(self) -> Path:
        return self.private_root / AUTH_PROJECT_ID


def demo_profile(
    name: str,
    url: str,
    *,
    tenant: str = "aurora",
    display: str = "Clínica Aurora",
    user_env: str = "DEMO_AURORA_USER",
    password_env: str = "DEMO_AURORA_PASSWORD",
    auth: str = "",
) -> str:
    return f"""
[profiles.{name}]
target_url = "{url}"
login_url = "{url}/login"
tenant_id = "{tenant}"
tenant_display_name = "{display}"
allowed_entities = ["employee"]

[profiles.{name}.credentials]
username_env = "{user_env}"
password_env = "{password_env}"

[profiles.{name}.auth]
timeout_seconds = 5
{auth}
"""


@pytest.fixture
def make_auth_project(
    workdir: Path, demo_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., AuthProject]:
    """Projeto apontado para o demo, com credenciais do seed no ambiente."""
    for key in ("aurora", "boreal", "multi"):
        email, password = demo_server.user(key)
        monkeypatch.setenv(f"DEMO_{key.upper()}_USER", email)
        monkeypatch.setenv(f"DEMO_{key.upper()}_PASSWORD", password)

    def make(*profiles: str, project_id: str | None = AUTH_PROJECT_ID) -> AuthProject:
        root = workdir.parent / "private-root"
        header = f'project_id = "{project_id}"\n' if project_id else ""
        body = "".join(profiles) or demo_profile("demo", demo_server.url)
        path = workdir / "lastro.toml"
        path.write_text(
            header + f"private_data_root = '{root.as_posix()}'\n" + body, encoding="utf-8"
        )
        return AuthProject(config_path=path, private_root=root)

    return make


@pytest.fixture
def profile_for() -> Callable[..., str]:
    return demo_profile


type BrowserScenario[T] = Callable[[Browser], Coroutine[Any, Any, T]]


@pytest.fixture
def with_browser(chromium: None) -> Callable[[BrowserScenario[Any]], Any]:
    """Executa o cenário assíncrono com um Chromium headless real."""

    def execute(scenario: BrowserScenario[Any]) -> Any:
        async def main() -> Any:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    return await scenario(browser)
                finally:
                    await browser.close()

        return asyncio.run(main())

    return execute
