"""Os 13 checks do lastro doctor. Cada check devolve um resultado tipado com frase de correção."""

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import httpx

from lastro.cli.output import Status
from lastro.codex.config import SandboxRulesError, load_policy, verify_manifest
from lastro.codex.models import MINIMUM_VERSION
from lastro.codex.preflight import CodexPreflightError, probe_install
from lastro.config.loader import ENV_FILENAME, LoadedConfig
from lastro.config.schema import DEFAULT_EXECUTOR, Profile
from lastro.config.source import SourceSettings
from lastro.executors import get_executor, supported_executors
from lastro.mission import BundleError, instruction_problems, verify_bundle
from lastro.native import missing_dependencies as native_missing_dependencies
from lastro.paths import ensure_writable, is_inside_git_tree

MIN_PYTHON = (3, 12)
HTTP_TIMEOUT_SECONDS = 10.0
TARGET_FAILURE_STATUS = 500
VERSION_ENDPOINT_FAILURE_STATUS = 400

NAME_PYTHON = "Versão do Python"
NAME_UV = "uv"
NAME_PLAYWRIGHT = "Chromium do Playwright"
NAME_EXECUTOR_CLI = "CLI do executor"
NAME_CONFIG = "Configuração"
NAME_CREDENTIAL_VARS = "Variáveis de credencial"
NAME_ROOT_WRITABLE = "Private data root gravável"
NAME_ROOT_OUTSIDE_GIT = "Private data root fora do Git"
NAME_ENV_IGNORED = ".env ignorado pelo Git"
NAME_TARGET = "URL alvo acessível"
NAME_VERSION_ENDPOINT = "Version endpoint acessível"
NAME_PERMISSION = "Permissão do executor"
NAME_SKILL_BUNDLE = "Bundle de skills"
NAME_SOURCE = "Contexto de código (--source)"

DEPENDS_ON_CONFIG_DETAIL = "depende de configuração válida"
DEPENDS_ON_CONFIG_FIX = "Corrija a configuração apontada no check Configuração e rode de novo."


@dataclass(frozen=True)
class CheckResult:
    number: int
    name: str
    status: Status
    detail: str
    fix: str | None = None


def _ok(number: int, name: str, detail: str) -> CheckResult:
    return CheckResult(number, name, Status.OK, detail)


def _warn(number: int, name: str, detail: str, fix: str) -> CheckResult:
    return CheckResult(number, name, Status.WARN, detail, fix)


def _fail(number: int, name: str, detail: str, fix: str) -> CheckResult:
    return CheckResult(number, name, Status.FAIL, detail, fix)


def depends_on_config(number: int, name: str) -> CheckResult:
    return _fail(number, name, DEPENDS_ON_CONFIG_DETAIL, DEPENDS_ON_CONFIG_FIX)


class HttpCheckError(Exception):
    pass


def http_status(url: str, timeout: float) -> int:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            return client.get(url).status_code
    except httpx.TimeoutException:
        raise HttpCheckError(f"tempo esgotado ({timeout:g} s)") from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise HttpCheckError(f"falha de conexão ({type(exc).__name__})") from None


def playwright_installed() -> bool:
    return importlib.util.find_spec("playwright") is not None


def playwright_browsers_dir(
    env: Mapping[str, str] | None = None, platform: str | None = None, home: Path | None = None
) -> Path | None:
    env = env if env is not None else os.environ
    platform = platform if platform is not None else sys.platform
    home = home if home is not None else Path(os.path.expanduser("~"))
    configured = env.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured == "0":
        spec = importlib.util.find_spec("playwright")
        if spec is None or spec.origin is None:
            return None
        return Path(spec.origin).parent / "driver" / "package" / ".local-browsers"
    if configured:
        return Path(configured)
    if platform.startswith("win"):
        local = env.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData" / "Local") / "ms-playwright"
    if platform == "darwin":
        return home / "Library" / "Caches" / "ms-playwright"
    cache = env.get("XDG_CACHE_HOME")
    return (Path(cache) if cache else home / ".cache") / "ms-playwright"


def chromium_installed() -> bool:
    browsers = playwright_browsers_dir()
    if browsers is None or not browsers.is_dir():
        return False
    return any(child.is_dir() and child.name.startswith("chromium") for child in browsers.iterdir())


def git_check_ignore(path: Path) -> bool | None:
    """True se o Git ignora o caminho, False se não ignora, None se não foi possível verificar."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        completed = subprocess.run(
            [git, "check-ignore", "-q", path.name],
            cwd=path.parent,
            capture_output=True,
            timeout=HTTP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    return None


def scanner_problem() -> str | None:
    """None quando o gitleaks responde e detecta o segredo sintético das regras confiáveis."""
    from lastro.change_context.scanner import Gitleaks, ScannerError

    try:
        Gitleaks().ensure()
    except ScannerError as exc:
        return str(exc)
    return None


def codex_version() -> str:
    """Versão validada do Codex; sem inferência e sem ler as credenciais da CLI."""
    return probe_install().version


def _current_python() -> tuple[int, int, int]:
    return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


@dataclass
class DoctorEnvironment:
    """Acesso ao mundo externo usado pelos checks, substituível em testes."""

    python_version: tuple[int, int, int] = field(default_factory=_current_python)
    which: Callable[[str], str | None] = shutil.which
    playwright_installed: Callable[[], bool] = playwright_installed
    chromium_installed: Callable[[], bool] = chromium_installed
    http_status: Callable[[str, float], int] = http_status
    git_check_ignore: Callable[[Path], bool | None] = git_check_ignore
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    http_timeout: float = HTTP_TIMEOUT_SECONDS
    fts5_available: Callable[[], bool] = lambda: sqlite_fts5_available()
    missing_native_modules: Callable[[], list[str]] = lambda: native_missing_dependencies()
    scanner_problem: Callable[[], str | None] = scanner_problem
    codex_version: Callable[[], str] = codex_version


def check_python(env: DoctorEnvironment) -> CheckResult:
    version = ".".join(str(part) for part in env.python_version)
    if env.python_version[:2] >= MIN_PYTHON:
        return _ok(1, NAME_PYTHON, version)
    return _fail(
        1,
        NAME_PYTHON,
        f"{version} (requer 3.12 ou superior)",
        "Instale o Python 3.12 ou superior e reinstale o lastro.",
    )


def check_uv(env: DoctorEnvironment) -> CheckResult:
    found = env.which("uv")
    if found:
        return _ok(2, NAME_UV, found)
    return _warn(
        2,
        NAME_UV,
        "uv não encontrado no PATH",
        "Instale o uv (https://docs.astral.sh/uv/) para instalar e atualizar o lastro.",
    )


def check_playwright(env: DoctorEnvironment) -> CheckResult:
    if not env.playwright_installed():
        return _fail(
            3,
            NAME_PLAYWRIGHT,
            "Playwright não instalado",
            "Instale o Playwright no ambiente do lastro e rode playwright install chromium.",
        )
    if not env.chromium_installed():
        return _fail(
            3,
            NAME_PLAYWRIGHT,
            "Chromium não encontrado",
            "Rode playwright install chromium.",
        )
    return _ok(3, NAME_PLAYWRIGHT, "instalado")


def check_executor_cli(env: DoctorEnvironment, executor: str = DEFAULT_EXECUTOR) -> CheckResult:
    spec = get_executor(executor)
    if spec is None:
        return _fail(
            4,
            NAME_EXECUTOR_CLI,
            f"executor '{executor}' não suportado nesta versão "
            f"(suportados: {supported_executors()})",
            "Ajuste a chave executor do lastro.toml para um executor suportado.",
        )
    if spec.external is None:
        return check_native_runtime(env)
    if spec.name == "codex":
        return check_codex_runtime(env)
    binary = spec.external.binary
    found = env.which(binary)
    if found:
        return _ok(4, NAME_EXECUTOR_CLI, f"{spec.name}: {found}")
    return _fail(
        4,
        NAME_EXECUTOR_CLI,
        f"{spec.name}: comando '{binary}' não encontrado no PATH",
        f"Instale o {spec.name} e confirme que o comando {binary} está no PATH.",
    )


def check_codex_runtime(env: DoctorEnvironment) -> CheckResult:
    try:
        version = env.codex_version()
    except CodexPreflightError as exc:
        return _fail(
            4,
            NAME_EXECUTOR_CLI,
            f"codex: {exc}",
            f"Instale o Codex {MINIMUM_VERSION} ou superior e rode codex login com ChatGPT.",
        )
    return _ok(4, NAME_EXECUTOR_CLI, f"codex {version}: login ChatGPT")


def check_native_runtime(env: DoctorEnvironment) -> CheckResult:
    """Extra instalado e chave presente; a validade remota só é conferida ao iniciar a run."""
    missing = env.missing_native_modules()
    if missing:
        return _fail(
            4,
            NAME_EXECUTOR_CLI,
            f"native: extra não instalado (faltam: {', '.join(missing)})",
            "Instale com uv sync --extra native (ou pip install 'lastro[native]').",
        )
    if not env.environ.get("ANTHROPIC_API_KEY", "").strip():
        return _fail(
            4,
            NAME_EXECUTOR_CLI,
            "native: ANTHROPIC_API_KEY ausente",
            "Defina ANTHROPIC_API_KEY no .env ao lado do lastro.toml ou no ambiente.",
        )
    return _ok(4, NAME_EXECUTOR_CLI, "native: extra instalado, ANTHROPIC_API_KEY presente")


def config_valid(loaded: LoadedConfig, profiles: Sequence[Profile]) -> CheckResult:
    names = ", ".join(profile.name for profile in profiles)
    return _ok(5, NAME_CONFIG, f"{loaded.path} (perfis: {names})")


def config_invalid(lines: Sequence[str]) -> CheckResult:
    return _fail(
        5,
        NAME_CONFIG,
        "inválida\n" + "\n".join(lines),
        "Corrija o lastro.toml conforme as mensagens acima.",
    )


def config_missing(lines: Sequence[str]) -> CheckResult:
    return _fail(
        5,
        NAME_CONFIG,
        "ausente\n" + "\n".join(lines),
        "Rode lastro init para criar a configuração do projeto.",
    )


def check_credential_vars(env: DoctorEnvironment, profiles: Sequence[Profile]) -> CheckResult:
    missing: list[str] = []
    total = 0
    for profile in profiles:
        for variable in profile.credential_variable_names():
            total += 1
            if not env.environ.get(variable):
                missing.append(f"{variable} ({profile.name})")
    if missing:
        return _fail(
            6,
            NAME_CREDENTIAL_VARS,
            "não definidas ou vazias: " + ", ".join(missing),
            "Defina essas variáveis no .env do projeto ou no ambiente (veja .env.example).",
        )
    return _ok(6, NAME_CREDENTIAL_VARS, f"todas definidas ({total})")


def sqlite_fts5_available() -> bool:
    """A memória exige FTS5 no SQLite efetivamente carregado pelo Python; não há fallback."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(value)")
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return True


def check_private_root_writable(
    root: Path, fts5_available: Callable[[], bool] = sqlite_fts5_available
) -> CheckResult:
    try:
        ensure_writable(root)
    except OSError as exc:
        return _fail(
            7,
            NAME_ROOT_WRITABLE,
            f"{root}: {exc.strerror or type(exc).__name__}",
            "Ajuste as permissões ou defina private_data_root para uma pasta gravável.",
        )
    if not fts5_available():
        return _fail(
            7,
            NAME_ROOT_WRITABLE,
            f"fts5_unavailable: o SQLite {sqlite3.sqlite_version} não oferece FTS5",
            "Instale um Python cujo SQLite tenha FTS5; a busca da memória não tem alternativa.",
        )
    return _ok(7, NAME_ROOT_WRITABLE, str(root))


def check_private_root_outside_git(root: Path) -> CheckResult:
    if is_inside_git_tree(root):
        return _fail(
            8,
            NAME_ROOT_OUTSIDE_GIT,
            f"{root} está dentro de um repositório Git",
            "Defina private_data_root no lastro.toml para um local fora do repositório.",
        )
    return _ok(8, NAME_ROOT_OUTSIDE_GIT, str(root))


def check_env_ignored(env: DoctorEnvironment, project_dir: Path) -> CheckResult:
    env_file = project_dir / ENV_FILENAME
    if not env_file.is_file():
        return _warn(
            9,
            NAME_ENV_IGNORED,
            f"{ENV_FILENAME} ausente em {project_dir}",
            "Copie .env.example para .env e preencha as credenciais.",
        )
    if not is_inside_git_tree(project_dir):
        return _ok(9, NAME_ENV_IGNORED, "diretório fora de repositório Git")
    ignored = env.git_check_ignore(env_file)
    if ignored is True:
        return _ok(9, NAME_ENV_IGNORED, "ignorado")
    if ignored is False:
        return _fail(
            9,
            NAME_ENV_IGNORED,
            f"{ENV_FILENAME} presente e não ignorado pelo Git",
            "Adicione .env ao .gitignore do projeto antes de qualquer commit.",
        )
    return _warn(
        9,
        NAME_ENV_IGNORED,
        "não foi possível verificar (git indisponível)",
        "Instale o Git e confirme que .env está no .gitignore do projeto.",
    )


def _labeled(name: str, profile: Profile) -> str:
    return f"{name} [{profile.name}]"


def check_target(env: DoctorEnvironment, profile: Profile) -> CheckResult:
    name = _labeled(NAME_TARGET, profile)
    fix = "Confirme que o sistema alvo está no ar e acessível desta máquina."
    try:
        status = env.http_status(profile.target_url, env.http_timeout)
    except HttpCheckError as exc:
        return _fail(10, name, f"{profile.target_url}: {exc}", fix)
    if status >= TARGET_FAILURE_STATUS:
        return _fail(10, name, f"{profile.target_url}: HTTP {status}", fix)
    return _ok(10, name, f"{profile.target_url}: HTTP {status}")


def version_endpoint_url(profile: Profile) -> str | None:
    endpoint = profile.version_endpoint
    if endpoint is None:
        return None
    if endpoint.startswith("/"):
        return urljoin(profile.target_url, endpoint)
    return endpoint


def check_version_endpoint(env: DoctorEnvironment, profile: Profile) -> CheckResult:
    name = _labeled(NAME_VERSION_ENDPOINT, profile)
    url = version_endpoint_url(profile)
    if url is None:
        return _ok(11, name, "não configurado")
    fix = "Confirme o version_endpoint do perfil e que o sistema alvo está no ar."
    try:
        status = env.http_status(url, env.http_timeout)
    except HttpCheckError as exc:
        return _fail(11, name, f"{url}: {exc}", fix)
    if status >= VERSION_ENDPOINT_FAILURE_STATUS:
        return _fail(11, name, f"{url}: HTTP {status}", fix)
    return _ok(11, name, f"{url}: HTTP {status}")


def check_source(env: DoctorEnvironment, settings: SourceSettings) -> CheckResult:
    """Só existe com [source] habilitado: Git e scanner obrigatórios, gh opcional (--issue)."""
    if env.which("git") is None:
        return _fail(14, NAME_SOURCE, "git não encontrado no PATH", "Instale o Git.")
    problem = env.scanner_problem()
    if problem is not None:
        return _fail(
            14,
            NAME_SOURCE,
            problem,
            "Instale o gitleaks (https://github.com/gitleaks/gitleaks) ou rode sem --source.",
        )
    if not env.environ.get(settings.repo_path_env):
        return _warn(
            14,
            NAME_SOURCE,
            f"{settings.repo_path_env} não definida",
            f"Defina {settings.repo_path_env} no .env com a raiz do repositório analisado.",
        )
    if env.which("gh") is None:
        return _warn(
            14,
            NAME_SOURCE,
            "git e gitleaks prontos; gh ausente (necessário só para --issue URL)",
            "Instale o GitHub CLI e rode gh auth login para importar issues por URL.",
        )
    return _ok(14, NAME_SOURCE, "git, gitleaks e gh prontos")


def check_permission_file(project_dir: Path, executor: str) -> CheckResult:
    spec = get_executor(executor)
    if spec is None:
        return _fail(
            12,
            NAME_PERMISSION,
            f"executor '{executor}' não suportado nesta versão",
            "Ajuste a chave executor do lastro.toml para um executor suportado.",
        )
    if spec.external is None:
        return _ok(12, NAME_PERMISSION, "native: políticas aplicadas pelo servidor MCP do Lastro")
    permission_file = spec.external.permission_file
    if spec.name == "codex":
        try:
            load_policy(project_dir)
            verify_manifest(project_dir)
        except SandboxRulesError as exc:
            return _fail(
                12, NAME_PERMISSION, str(exc), "Rode lastro init --executor codex no projeto."
            )
        return _ok(12, NAME_PERMISSION, permission_file.as_posix())
    path = project_dir / permission_file
    if path.is_file():
        return _ok(12, NAME_PERMISSION, permission_file.as_posix())
    return _fail(
        12,
        NAME_PERMISSION,
        f"{permission_file.as_posix()} ausente",
        "Rode lastro init no diretório do projeto para criar a permissão do executor.",
    )


def check_skill_bundle(project_dir: Path, executor: str) -> CheckResult:
    """Manifest, hashes e instruções do executor que a run exige antes de iniciar."""
    fix = "Rode lastro init no diretório do projeto; arquivos modificados são preservados."
    try:
        bundle = verify_bundle(project_dir)
    except BundleError as exc:
        return _fail(13, NAME_SKILL_BUNDLE, exc.detail, fix)
    spec = get_executor(executor)
    if spec is None or spec.external is None:
        return _ok(13, NAME_SKILL_BUNDLE, f"sha256 {bundle.sha256[:12]}")
    instruction_file = spec.external.instruction_file
    problems = instruction_problems(project_dir / instruction_file)
    if problems:
        return _fail(
            13,
            NAME_SKILL_BUNDLE,
            "; ".join(problems),
            f"Aponte {instruction_file.as_posix()} para skills/lastro/ em até 60 linhas.",
        )
    return _ok(13, NAME_SKILL_BUNDLE, f"sha256 {bundle.sha256[:12]}")
