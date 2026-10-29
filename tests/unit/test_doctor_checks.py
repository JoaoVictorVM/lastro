import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lastro.cli.output import Status
from lastro.config.schema import LastroConfig, Profile
from lastro.doctor import checks
from lastro.doctor.checks import CheckResult, DoctorEnvironment, http_status
from lastro.doctor.runner import run_doctor
from lastro.paths import is_inside_git_tree

SLOW_SECONDS = 1.5
FAST_TIMEOUT = 0.3


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/slow"):
            time.sleep(SLOW_SECONDS)
        code = {"/missing": 404, "/down": 503}.get(self.path, 200)
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def local_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host!s}:{port}"
    server.shutdown()
    server.server_close()


def _profile(target_url: str = "https://alvo.example.test", **extra: object) -> Profile:
    data: dict[str, object] = {
        "target_url": target_url,
        "login_url": f"{target_url}/login",
        "tenant_id": "acme",
        "allowed_entities": [],
        "credentials": {"username_env": "HOMOLOG_USERNAME", "password_env": "HOMOLOG_PASSWORD"},
    }
    data.update(extra)
    return LastroConfig.model_validate({"profiles": {"homolog": data}}).profiles["homolog"]


def _real_http_env(doctor_env: Callable[..., DoctorEnvironment]) -> DoctorEnvironment:
    return doctor_env(http_status=http_status, http_timeout=FAST_TIMEOUT)


def test_python_version_check(doctor_env: Callable[..., DoctorEnvironment]) -> None:
    assert checks.check_python(doctor_env(python_version=(3, 11, 9))).status is Status.FAIL
    assert checks.check_python(doctor_env(python_version=(3, 12, 0))).status is Status.OK
    assert checks.check_python(doctor_env(python_version=(3, 14, 0))).status is Status.OK


def test_uv_missing_is_warning(doctor_env: Callable[..., DoctorEnvironment]) -> None:
    result = checks.check_uv(doctor_env(which=lambda name: None))

    assert result.status is Status.WARN
    assert result.fix


def test_playwright_chromium_missing(doctor_env: Callable[..., DoctorEnvironment]) -> None:
    no_chromium = checks.check_playwright(doctor_env(chromium_installed=lambda: False))
    no_playwright = checks.check_playwright(doctor_env(playwright_installed=lambda: False))

    for result in (no_chromium, no_playwright):
        assert result.status is Status.FAIL
        assert result.fix and "playwright install chromium" in result.fix


def test_playwright_browsers_dir_by_platform(tmp_path: Path) -> None:
    assert checks.playwright_browsers_dir({"LOCALAPPDATA": str(tmp_path)}, "win32", tmp_path) == (
        tmp_path / "ms-playwright"
    )
    assert checks.playwright_browsers_dir({}, "linux", tmp_path) == (
        tmp_path / ".cache" / "ms-playwright"
    )
    custom = tmp_path / "browsers"
    assert checks.playwright_browsers_dir(
        {"PLAYWRIGHT_BROWSERS_PATH": str(custom)}, "linux", tmp_path
    ) == (custom)


def test_executor_cli_found_and_missing(doctor_env: Callable[..., DoctorEnvironment]) -> None:
    found = checks.check_executor_cli(doctor_env(), "claude-code")
    missing = checks.check_executor_cli(doctor_env(which=lambda name: None), "claude-code")
    unsupported = checks.check_executor_cli(doctor_env(), "codex")

    assert found.status is Status.OK
    assert missing.status is Status.FAIL
    assert "claude" in missing.detail
    assert unsupported.status is Status.FAIL
    assert "não suportado" in unsupported.detail


def test_config_invalid_reports_message(
    doctor_env: Callable[..., DoctorEnvironment],
    write_config: Callable[..., Path],
    profile_toml: Callable[..., str],
    workdir: Path,
) -> None:
    write_config(profile_toml("homolog", extra='editable_fields = { employee = "phone" }'))

    results = run_doctor(doctor_env(), cwd=workdir)

    config_result = results[4]
    assert config_result.number == 5
    assert config_result.status is Status.FAIL
    assert (
        "lastro.toml › profiles.homolog.editable_fields.employee: "
        "expected list of field names, got string"
    ) in config_result.detail


def test_unset_credential_var_names_reported(
    doctor_env: Callable[..., DoctorEnvironment],
) -> None:
    env = doctor_env(environ={"HOMOLOG_USERNAME": "usuario", "HOMOLOG_PASSWORD": ""})

    result = checks.check_credential_vars(env, [_profile()])

    assert result.status is Status.FAIL
    assert "HOMOLOG_PASSWORD" in result.detail
    assert "HOMOLOG_USERNAME" not in result.detail
    assert "usuario" not in result.detail


def test_credential_vars_all_set(doctor_env: Callable[..., DoctorEnvironment]) -> None:
    env = doctor_env(environ={"HOMOLOG_USERNAME": "u", "HOMOLOG_PASSWORD": "p"})

    assert checks.check_credential_vars(env, [_profile()]).status is Status.OK


def test_private_root_not_writable(tmp_path: Path) -> None:
    blocker = tmp_path / "arquivo"
    blocker.write_text("x", encoding="utf-8")

    assert checks.check_private_root_writable(blocker / "root").status is Status.FAIL
    assert checks.check_private_root_writable(tmp_path / "root").status is Status.OK


def test_private_root_inside_git_is_failure(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)

    result = checks.check_private_root_outside_git(tmp_path / "repo" / "dados")

    assert result.status is Status.FAIL
    assert result.fix


@pytest.mark.skipif(shutil.which("git") is None, reason="git não instalado")
def test_env_not_ignored_is_failure(
    doctor_env: Callable[..., DoctorEnvironment], tmp_path: Path
) -> None:
    project = tmp_path / "projeto"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / ".env").write_text("X=1\n", encoding="utf-8")
    env = doctor_env(git_check_ignore=checks.git_check_ignore)

    assert checks.check_env_ignored(env, project).status is Status.FAIL

    (project / ".gitignore").write_text(".env\n", encoding="utf-8")
    assert checks.check_env_ignored(env, project).status is Status.OK


def test_env_missing_is_warning(
    doctor_env: Callable[..., DoctorEnvironment], tmp_path: Path
) -> None:
    result = checks.check_env_ignored(doctor_env(), tmp_path)

    assert result.status is Status.WARN


def test_env_outside_git_passes(
    doctor_env: Callable[..., DoctorEnvironment], tmp_path: Path
) -> None:
    if is_inside_git_tree(tmp_path):
        pytest.skip("diretório temporário está dentro de um repositório Git")
    (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")

    assert checks.check_env_ignored(doctor_env(), tmp_path).status is Status.OK


def test_target_reachable_below_500(
    doctor_env: Callable[..., DoctorEnvironment], local_server: str
) -> None:
    env = _real_http_env(doctor_env)

    ok = checks.check_target(env, _profile(f"{local_server}/ok"))
    not_found = checks.check_target(env, _profile(f"{local_server}/missing"))

    assert ok.status is Status.OK and "HTTP 200" in ok.detail
    assert not_found.status is Status.OK and "HTTP 404" in not_found.detail


def test_target_5xx_and_timeout_fail(
    doctor_env: Callable[..., DoctorEnvironment], local_server: str
) -> None:
    env = _real_http_env(doctor_env)

    down = checks.check_target(env, _profile(f"{local_server}/down"))
    slow = checks.check_target(env, _profile(f"{local_server}/slow"))

    assert down.status is Status.FAIL and "HTTP 503" in down.detail
    assert slow.status is Status.FAIL and "tempo esgotado" in slow.detail


def test_target_connection_refused_fails(doctor_env: Callable[..., DoctorEnvironment]) -> None:
    result = checks.check_target(_real_http_env(doctor_env), _profile("http://127.0.0.1:9"))

    assert result.status is Status.FAIL


def test_version_endpoint_not_configured_passes(
    doctor_env: Callable[..., DoctorEnvironment],
) -> None:
    result = checks.check_version_endpoint(doctor_env(), _profile())

    assert result.status is Status.OK
    assert result.detail == "não configurado"


def test_version_endpoint_unreachable(
    doctor_env: Callable[..., DoctorEnvironment], local_server: str
) -> None:
    env = _real_http_env(doctor_env)

    reachable = checks.check_version_endpoint(
        env, _profile(local_server, version_endpoint="/version")
    )
    missing = checks.check_version_endpoint(
        env, _profile(local_server, version_endpoint="/missing")
    )
    offline = checks.check_version_endpoint(
        env, _profile(local_server, version_endpoint="http://127.0.0.1:9/version")
    )

    assert reachable.status is Status.OK
    assert missing.status is Status.FAIL
    assert offline.status is Status.FAIL


def test_permission_config_missing(tmp_path: Path) -> None:
    result = checks.check_permission_file(tmp_path, "claude-code")

    assert result.status is Status.FAIL
    assert ".claude/settings.json" in result.detail


def _by_number(results: list[CheckResult], number: int) -> list[CheckResult]:
    return [result for result in results if result.number == number]


def _break_config(project: Path) -> None:
    (project / "lastro.toml").write_text("profiles = 1\n", encoding="utf-8")


def _remove_config(project: Path) -> None:
    (project / "lastro.toml").unlink()


def _unset_credential(project: Path) -> None:
    (project / ".env").write_text("HOMOLOG_USERNAME=usuario\n", encoding="utf-8")


def _root_in_git(project: Path) -> None:
    repo = project.parent / "repo"
    (repo / ".git").mkdir(parents=True)
    config = project / "lastro.toml"
    text = config.read_text(encoding="utf-8").splitlines()
    text[0] = f"private_data_root = '{(repo / 'dados').as_posix()}'"
    config.write_text("\n".join(text) + "\n", encoding="utf-8")


def _root_not_writable(project: Path) -> None:
    blocker = project.parent / "bloqueio"
    blocker.write_text("x", encoding="utf-8")
    config = project / "lastro.toml"
    text = config.read_text(encoding="utf-8").splitlines()
    text[0] = f"private_data_root = '{(blocker / 'root').as_posix()}'"
    config.write_text("\n".join(text) + "\n", encoding="utf-8")


def _remove_env(project: Path) -> None:
    (project / ".env").unlink()


def _remove_permission(project: Path) -> None:
    (project / ".claude" / "settings.json").unlink()


def _add_version_endpoint(project: Path) -> None:
    config = project / "lastro.toml"
    text = config.read_text(encoding="utf-8").replace(
        'tenant_id = "acme"', 'tenant_id = "acme"\nversion_endpoint = "/version"'
    )
    config.write_text(text, encoding="utf-8")


def _status_for_url(failing_fragment: str) -> Callable[[str, float], int]:
    def status(url: str, timeout: float) -> int:
        if failing_fragment in url:
            raise checks.HttpCheckError("falha de conexão (ConnectError)")
        return 200

    return status


def _down_target(url: str, timeout: float) -> int:
    return 503


MISCONFIGURATIONS: dict[
    str, tuple[int, Status, Callable[[Path], None] | None, dict[str, object]]
] = {
    "python antigo": (1, Status.FAIL, None, {"python_version": (3, 11, 0)}),
    "uv ausente": (2, Status.WARN, None, {"which": lambda name: None if name == "uv" else name}),
    "playwright ausente": (3, Status.FAIL, None, {"playwright_installed": lambda: False}),
    "chromium ausente": (3, Status.FAIL, None, {"chromium_installed": lambda: False}),
    "executor ausente": (
        4,
        Status.FAIL,
        None,
        {"which": lambda name: None if name == "claude" else name},
    ),
    "config ausente": (5, Status.FAIL, _remove_config, {}),
    "config inválida": (5, Status.FAIL, _break_config, {}),
    "credencial não definida": (6, Status.FAIL, _unset_credential, {}),
    "root sem escrita": (7, Status.FAIL, _root_not_writable, {}),
    "root dentro do git": (8, Status.FAIL, _root_in_git, {}),
    ".env ausente": (9, Status.WARN, _remove_env, {}),
    ".env não ignorado": (9, Status.FAIL, None, {"git_check_ignore": lambda path: False}),
    "alvo com 5xx": (10, Status.FAIL, None, {"http_status": _down_target}),
    "version endpoint fora do ar": (
        11,
        Status.FAIL,
        _add_version_endpoint,
        {"http_status": _status_for_url("/version")},
    ),
    "permissão ausente": (12, Status.FAIL, _remove_permission, {}),
}


@pytest.mark.parametrize("scenario", list(MISCONFIGURATIONS))
def test_each_documented_misconfiguration_detected(
    scenario: str,
    healthy_project: Path,
    doctor_env: Callable[..., DoctorEnvironment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    number, expected, mutate, overrides = MISCONFIGURATIONS[scenario]
    if mutate is not None:
        mutate(healthy_project)
    # O .env do projeto só é aplicado se a variável ainda não existir no ambiente.
    monkeypatch.delenv("HOMOLOG_USERNAME", raising=False)
    monkeypatch.delenv("HOMOLOG_PASSWORD", raising=False)
    env = doctor_env(**overrides)
    if scenario == ".env não ignorado":
        (healthy_project / ".git").mkdir()

    results = run_doctor(env, cwd=healthy_project)

    statuses = {result.status for result in _by_number(results, number)}
    assert expected in statuses, results
    # Sem configuração os checks dependentes também falham; sem .env as credenciais somem.
    consequences = {5: {6, 10, 11, 12}, 9: {6}}.get(number, set())
    others = [
        r
        for r in results
        if r.number != number and r.number not in consequences and r.status is not Status.OK
    ]
    assert others == [], others
