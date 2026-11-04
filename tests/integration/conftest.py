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
from fastapi import FastAPI, Request
from playwright.async_api import Browser, async_playwright
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

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
    access_log: list[tuple[str, str]]

    def user(self, key: str) -> tuple[str, str]:
        user = next(item for item in load_seed_users({}) if item.key == key)
        return user.email, user.password


def _with_test_routes(app: FastAPI) -> FastAPI:
    @app.get(BROKEN_ME_PATH)
    def broken_me() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse({"error": "boom"}, status_code=500)

    return app


@pytest.fixture
def demo_flags() -> FlagState:
    return FlagState.only()


@pytest.fixture
def demo_server(tmp_path: Path, chromium: None, demo_flags: FlagState) -> Iterator[DemoServer]:
    state_dir = tmp_path / "demo-state"
    app = _with_test_routes(create_app(DemoConfig(state_dir=state_dir), demo_flags))
    access_log: list[tuple[str, str]] = []

    @app.middleware("http")
    async def record_access(request: Request, call_next: RequestResponseEndpoint) -> Response:
        access_log.append((request.method, request.url.path))
        return await call_next(request)

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
        yield DemoServer(url=f"http://127.0.0.1:{port}", state_dir=state_dir, access_log=access_log)
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


@dataclass
class KnowledgeHarness:
    """Runs e observações reais no histórico, sem browser, para os testes da memória."""

    context: Callable[[str], ProjectContext]
    clock: Callable[[], Any] | None = None
    process_checker: Callable[[int], bool] | None = None
    owner_pid: int | None = None

    def history(self, profile: str = "homolog") -> Any:
        from lastro.history import open_history

        kwargs: dict[str, Any] = {}
        if self.clock is not None:
            kwargs["clock"] = self.clock
        if self.process_checker is not None:
            kwargs["process_checker"] = self.process_checker
        if self.owner_pid is not None:
            kwargs["owner_pid"] = self.owner_pid
        return open_history(self.context(profile), **kwargs)

    def start(self, profile: str = "homolog", version: str | None = "v1") -> tuple[Any, str]:
        from lastro.history import TextMission

        history = self.history(profile)
        run = history.create_run(
            self.context(profile), TextMission("Explore employees"), executed_system_version=version
        )
        return history, run.record.id

    def observe(
        self,
        history: Any,
        run_id: str,
        route: str = "/employees/new",
        *,
        fields: list[dict[str, Any]] | None = None,
        landmarks: list[dict[str, Any]] | None = None,
        incomplete: bool = False,
    ) -> str:
        import uuid

        from lastro.history import EventType

        action_id = uuid.uuid4().hex
        history.append_event(
            run_id, EventType.TOOL_CALL, tool_name="observe", correlation_id=action_id
        )
        projection: dict[str, Any] = {
            "route": route,
            "title": "Novo funcionário",
            "landmarks": landmarks
            if landmarks is not None
            else [{"role": "heading", "label": "Novo funcionário"}],
            "fields": fields
            if fields is not None
            else [{"name": "cpf", "label": "CPF", "required": True, "visible": True}],
            "incomplete": incomplete,
        }
        return history.record_observation(run_id, action_id, projection).id

    def propose(
        self,
        history: Any,
        run_id: str,
        observation_id: str,
        *,
        value: Any = True,
        attribute: str = "required",
        key: str | None = None,
        flow: str = "employee",
    ) -> Any:
        from lastro.knowledge import ProposalInput

        proposal = ProposalInput.model_validate(
            {
                "flow": flow,
                "category": "screen_component",
                "fact": {
                    "type": "field_property",
                    "subject": "employee",
                    "field": "cpf",
                    "attribute": attribute,
                    "value": value,
                },
                "observation_ids": [observation_id],
                "idempotency_key": key or f"{attribute}-{value}-{observation_id}",
            }
        )
        return history.propose_knowledge(run_id, proposal)


@pytest.fixture
def knowledge(history_context_for: Callable[[str], ProjectContext]) -> KnowledgeHarness:
    return KnowledgeHarness(history_context_for)


@dataclass
class NativeHarness:
    """Runs nativas com contexto e orçamento reais no histórico, sem browser nem provedor."""

    context: Callable[[str], ProjectContext]

    def history(self, **kwargs: Any) -> Any:
        from lastro.history import open_history

        return open_history(self.context("homolog").with_executor("native"), **kwargs)

    def start(
        self, history: Any, *, cap_nusd: int = 2_000_000_000, suite: str | None = None
    ) -> str:
        from lastro.history import NativeContextInput, TextMission

        context = self.context("homolog").with_executor("native")
        run = history.create_run(context, TextMission("Explore employees"))
        history.start_native_run(
            NativeContextInput(
                run_id=run.record.id,
                cap_nusd=cap_nusd,
                suite_budget_id=suite,
                catalog=[{"name": "observe", "description": "d", "input_schema": {}}],
                catalog_sha256="a" * 64,
                pricing={"models": {}},
                pricing_sha256="b" * 64,
                runtime_versions={"lastro": "0"},
            )
        )
        return run.record.id

    def reservation(self, run_id: str, amount: int, step: int = 1, attempt: int = 1) -> Any:
        from lastro.history import ReservationInput

        return ReservationInput(
            run_id=run_id,
            step_index=step,
            attempt=attempt,
            node="decide",
            model="claude-sonnet-5-5",
            payload_sha256="c" * 64,
            max_output_tokens=2048,
            reserved_nusd=amount,
        )


@pytest.fixture
def native_harness(history_context_for: Callable[[str], ProjectContext]) -> NativeHarness:
    return NativeHarness(history_context_for)


class ScriptedModel:
    """Provedor falso: cada geração aplica o próximo passo do roteiro sobre a requisição."""

    def __init__(
        self,
        steps: list[Callable[[Any], list[dict[str, Any]]]],
        *,
        usage: Any = None,
        counted: int = 1500,
    ) -> None:
        from lastro.native.pricing import Usage

        self.steps = list(steps)
        self.usage = usage if usage is not None else Usage(1000, 400, 100, 50)
        self.counted = counted
        self.requests: list[Any] = []
        self.counts = 0

    async def count_tokens(self, request: Any) -> int:
        self.counts += 1
        return self.counted

    async def create(self, request: Any) -> Any:
        from lastro.native.provider import ModelResponse

        self.requests.append(request)
        if not self.steps:
            raise AssertionError("script exhausted")
        step = self.steps.pop(0)
        content = step(request)
        stop = "tool_use" if any(block["type"] == "tool_use" for block in content) else "end_turn"
        return ModelResponse(content, stop, self.usage, f"req_{len(self.requests)}")


def tool_use(name: str, arguments: dict[str, Any], index: int = 0) -> list[dict[str, Any]]:
    import uuid

    return [
        {
            "type": "tool_use",
            "id": f"toolu_{index}_{uuid.uuid4().hex[:8]}",
            "name": name,
            "input": arguments,
        }
    ]


def tool_results(request: Any) -> list[dict[str, Any]]:
    """Resultados JSON de todas as ferramentas já devolvidas ao modelo, em ordem."""
    import json

    found: list[dict[str, Any]] = []
    for message in request.messages:
        if message["role"] != "user" or isinstance(message["content"], str):
            continue
        for block in message["content"]:
            if block.get("type") == "tool_result":
                try:
                    found.append(json.loads(block["content"]))
                except ValueError:
                    found.append({"raw": block["content"]})
    return found


@pytest.fixture
def scripted() -> Any:
    return type(
        "Scripted",
        (),
        {
            "Model": ScriptedModel,
            "tool_use": staticmethod(tool_use),
            "results": staticmethod(tool_results),
        },
    )


UPDATE_POLICY = """
[profiles.demo.policy]
version = 1

[[profiles.demo.policy.bindings]]
id = "employee-update"
entity = "employee"
operation = "update"
route = "/employees/{id}/edit"
target = {role = "button", name = "Salvar"}
form = "employee-form"
field_values = {status = ["active"]}

[profiles.demo.policy.bindings.fields]
full_name = "full_name"
cpf = "cpf"
email = "email"
phone = "phone"
role = "role"
hire_date = "hire_date"
status = "status"

[profiles.demo.policy.bindings.request]
method = "POST"
path = "/employees/{id}"
success_status = 200
"""
EDITABLE = (
    'allowed_entities = ["employee"]\neditable_fields = { employee = '
    '["full_name", "cpf", "email", "phone", "role", "hire_date", "status"] }'
)


@pytest.fixture
def make_native_demo(
    make_auth_project: Callable[..., AuthProject],
    demo_server: DemoServer,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., AuthProject]:
    """Projeto demo nativo com bundle materializado e chave fictícia no ambiente."""
    from lastro.mission import bundle_files

    def make(*, policy: bool = False, native: str = "") -> AuthProject:
        profile = demo_profile("demo", demo_server.url)
        if policy:
            profile = profile.replace('allowed_entities = ["employee"]', EDITABLE) + UPDATE_POLICY
        if native:
            profile += "\n[profiles.demo.native]\n" + native + "\n"
        project = make_auth_project(profile)
        text = project.config_path.read_text(encoding="utf-8")
        project.config_path.write_text('executor = "native"\n' + text, encoding="utf-8")
        for relative, content in bundle_files().items():
            target = project.config_path.parent / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0000000000000000")
        monkeypatch.setenv("LASTRO_DESKTOP_NOTIFICATIONS", "off")
        return project

    return make


@pytest.fixture
def native_demo(make_native_demo: Callable[..., AuthProject]) -> AuthProject:
    return make_native_demo()


@dataclass
class NativeRun:
    outcome: Any
    history: Any
    run_id: str
    output: list[str]
    runner: Any


@pytest.fixture
def run_native() -> Callable[..., NativeRun]:
    """Executa o runner nativo real (servidor MCP por stdio) com o modelo roteirizado."""

    def execute(
        project: AuthProject,
        model: Any,
        lines: tuple[str | None, ...] = (),
        *,
        cancel_after: float | None = None,
    ) -> NativeRun:
        from lastro.history import TextMission, open_history
        from lastro.native.human import HumanDecisionPort
        from lastro.native.runner import NativeRunner, preflight

        context = project.context()
        prepared, provider = asyncio.run(preflight(context, lambda key: model))
        history = open_history(context)
        run_id = history.create_run(
            context, TextMission("Explore employees"), skill_bundle_version=prepared.bundle.sha256
        ).record.id
        output: list[str] = []
        answers = iter(lines)
        human = HumanDecisionPort(
            history=history,
            run_id=run_id,
            write=output.append,
            interactive=True,
            timezone=context.timezone,
            read_line=lambda: next(answers, None),
            poll_seconds=0.1,
        )
        runner = NativeRunner(
            context,
            history,
            run_id,
            prepared,
            provider,
            write=output.append,
            interactive=True,
            human=human,
        )

        async def main() -> Any:
            if cancel_after is None:
                return await runner.run()
            task = asyncio.create_task(runner.run())
            await asyncio.sleep(cancel_after)
            task.cancel()
            try:
                return await task
            except (KeyboardInterrupt, asyncio.CancelledError):
                return runner.outcome

        outcome = asyncio.run(main())
        return NativeRun(outcome, history, run_id, output, runner)

    return execute


def make_issue_capture(
    *,
    title: str = "Permitir email opcional",
    body: str = "Critério: salvar cadastro sem email.",
    repository: str | None = "org/repo",
    number: int | None = 123,
    kind: str = "github",
    comments: tuple[str, ...] = (),
) -> Any:
    from lastro.change_context import IssueCapture, IssueComment, IssueContent, ScanLevel

    content = IssueContent.model_validate(
        {
            "source_kind": kind,
            "repository": repository,
            "number": number,
            "url": f"https://github.com/{repository}/issues/{number}" if kind == "github" else None,
            "title": title,
            "body": body,
            "labels": ("bug",),
            "comments": tuple(
                IssueComment(source_id=str(index), body=text, created_at="2026-11-04T12:00:00Z")
                for index, text in enumerate(comments, start=1)
            ),
            "comments_total": len(comments),
            "comments_included": len(comments),
            "source_updated_at": "2026-11-04T12:00:00Z",
        }
    )
    return IssueCapture(
        content=content,
        capture_started_at="2026-11-04T12:00:01Z",
        capture_finished_at="2026-11-04T12:00:02Z",
        scan_level=ScanLevel.REDACTION_ONLY,
    )


@dataclass(frozen=True)
class GitRepo:
    root: Path

    def git(self, *args: str) -> str:
        import subprocess

        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Lastro Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.autocrlf=false",
                *args,
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def commit(self, message: str, *paths: str) -> str:
        self.git("add", "--", *(paths or (".",)))
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def git_repo(workdir: Path) -> GitRepo:
    """Repositório Git real com main e uma branch de feature, fora do projeto Lastro."""
    root = workdir.parent / "source-repo"
    root.mkdir()
    repo = GitRepo(root)
    repo.git("init", "-q", "-b", "main")
    repo.write("src/form.py", "def save(email):\n    return email\n")
    repo.write("README.md", "# demo\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "feature/email")
    return repo
