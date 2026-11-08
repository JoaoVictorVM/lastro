"""Gera e verifica a política do Codex e a traduz nas sobreposições de cada sessão."""

import contextlib
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any, cast

from pydantic import ValidationError

from lastro.codex.models import MINIMUM_VERSION, AdapterManifest
from lastro.codex.preflight import CodexPreflightError
from lastro.skills import normalize

CONFIG_FILE = Path(".codex") / "config.toml"
MANIFEST_FILE = Path(".codex") / "lastro.json"
INSTRUCTIONS_FILE = Path("AGENTS.md")
SERVER_NAME = "lastro"
PROFILE_NAME = "lastro"
MISSING_MESSAGE = "Codex sandbox rules not found; run 'lastro init --executor codex'."
INVALID_MESSAGE = "Codex sandbox rules are invalid"
USER_TUNABLE = frozenset({"model", "model_reasoning_effort", "model_verbosity"})
# Chaves da configuração do usuário que uma sobreposição -c não consegue retirar.
UNREMOVABLE_USER_KEYS = ("openai_base_url", "chatgpt_base_url", "profile")
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
SERVER_ENV_VARS = (
    "PLAYWRIGHT_BROWSERS_PATH",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
# Ferramentas efetivas aceitas com o MCP desligado. apply_patch vem do catálogo de modelos e
# não tem chave que o retire; o autoteste exige que a sandbox negue a escrita dele.
ALLOWED_EFFECTIVE_TOOLS = frozenset(
    {
        "apply_patch",
        "clock__curr_time",
        "exec",
        "list_mcp_resource_templates",
        "list_mcp_resources",
        "read_mcp_resource",
        "request_user_input",
        "request_user_input_async",
        "sleep",
        "wait",
    }
)
NESTED_TOOL = re.compile(r"^### `([^`]+)`", re.MULTILINE)
# 192.0.2.1 é reservado para documentação (RFC 5737): nenhum tráfego alcança terceiros. Só a
# recusa imediata da sandbox prova a negação, pois um host não roteável também falharia.
SELF_TEST_SCRIPT = """
import socket, sys
write_target, *read_targets = sys.argv[1:]
for path in read_targets:
    try:
        open(path, "rb").close()
    except PermissionError:
        continue
    except OSError:
        sys.exit(4)
    sys.exit(3)
try:
    open(write_target, "xb").close()
except PermissionError:
    pass
except OSError:
    sys.exit(4)
else:
    sys.exit(5)
try:
    socket.create_connection(("192.0.2.1", 9), 3).close()
except PermissionError:
    sys.exit(0)
except OSError:
    pass
sys.exit(6)
"""
SELF_TEST_ATTEMPTS = 4
SELF_TEST_RETRY_SECONDS = 0.5
# Códigos que o próprio script devolve; qualquer outro é falha ao abrir a sandbox.
SELF_TEST_VERDICTS = frozenset({3, 4, 5, 6})

type Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class SandboxRulesError(CodexPreflightError):
    pass


def tool_names() -> list[str]:
    from lastro.browser.tools import TOOLS

    return sorted(TOOLS)


def render_config() -> str:
    template = files("lastro.scaffold").joinpath("templates", "codex_config.toml.tmpl")
    text = template.read_text(encoding="utf-8")
    return Template(text).substitute(enabled_tools=json.dumps(tool_names()))


def expected_policy() -> dict[str, Any]:
    return tomllib.loads(render_config())


def _difference(found: object, expected: object, path: str) -> list[str]:
    if isinstance(found, dict) and isinstance(expected, dict):
        have = cast(dict[str, Any], found)
        want = cast(dict[str, Any], expected)
        problems: list[str] = []
        for key in sorted(have.keys() | want.keys()):
            label = f"{path}.{key}" if path else key
            if key not in want:
                problems.append(f"unknown security field {label}")
            elif key not in have:
                problems.append(f"missing {label}")
            else:
                problems += _difference(have[key], want[key], label)
        return problems
    return [] if found == expected else [f"unexpected value for {path}"]


def policy_problems(data: Mapping[str, Any]) -> list[str]:
    """Só nomes de campos, nunca valores: o arquivo pode ter sido editado com algo sensível."""
    effective = {key: value for key, value in data.items() if key not in USER_TUNABLE}
    servers = effective.get("mcp_servers")
    foreign: list[str] = []
    if isinstance(servers, dict):
        table = cast(dict[str, Any], servers)
        foreign = sorted(set(table) - {SERVER_NAME})
        effective["mcp_servers"] = {
            name: value for name, value in table.items() if name == SERVER_NAME
        }
    problems = [f"foreign MCP server {name}" for name in foreign]
    return problems + _difference(effective, expected_policy(), "")


def load_policy(project_dir: Path) -> dict[str, Any]:
    path = project_dir / CONFIG_FILE
    if not path.is_file():
        raise SandboxRulesError(MISSING_MESSAGE)
    if path.is_symlink():
        raise SandboxRulesError(f"{INVALID_MESSAGE}: {CONFIG_FILE.as_posix()} is a link")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SandboxRulesError(
            f"{INVALID_MESSAGE}: {CONFIG_FILE.as_posix()} could not be parsed"
        ) from None
    problems = policy_problems(data)
    if problems:
        raise SandboxRulesError(
            f"{INVALID_MESSAGE}: {'; '.join(problems[:5])}. "
            f"Restore {CONFIG_FILE.as_posix()} and run 'lastro init --executor codex'."
        )
    return data


def _digest(content: bytes) -> str:
    return hashlib.sha256(normalize(content)).hexdigest()


def render_manifest(config: bytes, instructions: bytes) -> str:
    manifest = AdapterManifest(
        schema_version=1,
        executor="codex",
        minimum_version=MINIMUM_VERSION,
        config_sha256=_digest(config),
        instructions_sha256=_digest(instructions),
    )
    return json.dumps(manifest.model_dump(), indent=2, sort_keys=True) + "\n"


def verify_manifest(project_dir: Path) -> str:
    """Devolve o hash do manifesto; a versão declarada nele nunca é tomada como verdade."""
    try:
        raw = (project_dir / MANIFEST_FILE).read_bytes()
    except OSError:
        raise SandboxRulesError(MISSING_MESSAGE) from None
    try:
        manifest = AdapterManifest.model_validate_json(raw)
        config = (project_dir / CONFIG_FILE).read_bytes()
        instructions = (project_dir / INSTRUCTIONS_FILE).read_bytes()
    except (ValidationError, OSError):
        raise SandboxRulesError(
            f"{INVALID_MESSAGE}: {MANIFEST_FILE.as_posix()} does not match the expected contract. "
            "Run 'lastro init --executor codex'."
        ) from None
    if manifest.config_sha256 != _digest(config) or manifest.instructions_sha256 != _digest(
        instructions
    ):
        raise SandboxRulesError(
            f"{INVALID_MESSAGE}: {MANIFEST_FILE.as_posix()} differs from the generated files. "
            "Run 'lastro init --executor codex'."
        )
    return _digest(raw)


def toml_key(key: str) -> str:
    return key if BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def toml_inline(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        # Strings JSON são strings básicas válidas em TOML (mesmas regras de escape).
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_inline(item) for item in cast(list[object], value)) + "]"
    if isinstance(value, dict):
        items = cast(dict[str, object], value).items()
        return "{" + ", ".join(f"{toml_key(k)} = {toml_inline(v)}" for k, v in items) + "}"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


@dataclass(frozen=True)
class SessionIdentity:
    project_dir: Path
    config_path: Path
    profile: str
    run_id: str
    private_root: Path
    codex_home: Path
    instructions: str
    # Run filha de reprodução: a sessão habilita também a tool de resultado da tentativa.
    reproduction: bool = False


def pinned_server(policy: Mapping[str, Any], identity: SessionIdentity) -> dict[str, Any]:
    """O mesmo servidor do catálogo, fixado à run; nenhuma credencial vai nos argumentos."""
    server = dict(cast(dict[str, Any], policy["mcp_servers"])[SERVER_NAME])
    server["command"] = sys.executable
    server["args"] = [
        "-m",
        "lastro.browser.server",
        "--profile",
        identity.profile,
        "--config",
        str(identity.config_path.resolve()),
        "--run-id",
        identity.run_id,
    ]
    server["cwd"] = str(identity.project_dir)
    server["env_vars"] = list(SERVER_ENV_VARS)
    if identity.reproduction:
        from lastro.reproduction.models import REPRODUCTION_TOOL

        enabled = cast(list[str], server.get("enabled_tools") or tool_names())
        server["enabled_tools"] = sorted({*enabled, REPRODUCTION_TOOL})
    return server


def _leaves(prefix: str, value: object) -> list[tuple[str, object]]:
    if not isinstance(value, dict):
        return [(prefix, value)]
    pairs: list[tuple[str, object]] = []
    for key, item in cast(dict[str, object], value).items():
        pairs += _leaves(f"{prefix}.{toml_key(key)}", item)
    return pairs


def session_overrides(
    policy: Mapping[str, Any], identity: SessionIdentity, foreign_servers: list[str]
) -> list[str]:
    """Cada valor vai por -c: a camada .codex do projeto só carrega em projeto confiável."""
    pairs: list[tuple[str, object]] = []
    for key, value in policy.items():
        if key not in {"mcp_servers", "permissions"}:
            pairs += _leaves(toml_key(key), value)
    filesystem = dict(cast(dict[str, Any], policy["permissions"])[PROFILE_NAME]["filesystem"])
    # Caminhos absolutos dependem da máquina, por isso não ficam no arquivo gerado.
    filesystem[identity.private_root.resolve().as_posix()] = "deny"
    filesystem[(identity.codex_home / "auth.json").as_posix()] = "deny"
    pairs += [
        (f"permissions.{PROFILE_NAME}.filesystem", filesystem),
        (f"permissions.{PROFILE_NAME}.network.enabled", False),
        (f"mcp_servers.{SERVER_NAME}", pinned_server(policy, identity)),
        (f"projects.{toml_key(str(identity.project_dir))}.trust_level", "untrusted"),
        ("developer_instructions", identity.instructions),
    ]
    pairs += [(f"mcp_servers.{toml_key(name)}.enabled", False) for name in foreign_servers]
    overrides: list[str] = []
    for key, value in pairs:
        overrides += ["--config", f"{key}={toml_inline(value)}"]
    return overrides


def user_config_problems(home: Path) -> list[str]:
    """Só lê config.toml do usuário; as credenciais da CLI ficam em outros arquivos."""
    try:
        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return ["the Codex user configuration could not be parsed"]
    return [
        f"user configuration sets {key}, which cannot be neutralized"
        for key in UNREMOVABLE_USER_KEYS
        if key in data
    ]


def _servers(
    command: tuple[str, ...], overrides: list[str], runner: Runner
) -> list[dict[str, Any]]:
    completed = runner([*command, "mcp", "list", "--json", *overrides])
    try:
        if completed.returncode != 0:
            raise ValueError
        data: object = json.loads(completed.stdout)
        if not isinstance(data, list):
            raise ValueError
    except ValueError:
        raise SandboxRulesError(
            f"{INVALID_MESSAGE}: Codex could not report its effective MCP servers"
        ) from None
    items = cast(list[object], data)
    return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]


def resolve_foreign_servers(
    command: tuple[str, ...],
    policy: Mapping[str, Any],
    identity: SessionIdentity,
    runner: Runner,
) -> list[str]:
    """Neutraliza MCP herdado e confere na própria CLI o que ficou ativo."""
    probe = session_overrides(policy, identity, [])
    names = [
        str(item["name"])
        for item in _servers(command, probe, runner)
        if item.get("name") != SERVER_NAME
    ]
    effective = _servers(command, session_overrides(policy, identity, names), runner)
    enabled = [item for item in effective if item.get("enabled") is not False]
    if [item.get("name") for item in enabled] != [SERVER_NAME]:
        raise SandboxRulesError(
            f"{INVALID_MESSAGE}: an MCP server other than {SERVER_NAME} stays enabled"
        )
    transport = enabled[0].get("transport")
    expected = pinned_server(policy, identity)
    args = cast(dict[str, Any], transport).get("args") if isinstance(transport, dict) else None
    if args != expected["args"]:
        raise SandboxRulesError(
            f"{INVALID_MESSAGE}: the {SERVER_NAME} MCP server is not pinned to this run"
        )
    return names


def _tool_names(tools: list[object]) -> list[str]:
    names: list[str] = []
    for raw in tools:
        if not isinstance(raw, dict):
            continue
        tool = cast(dict[str, Any], raw)
        nested = tool.get("tools")
        if isinstance(nested, list):
            names += _tool_names(cast(list[object], nested))
            continue
        name = tool.get("name")
        if isinstance(name, str):
            names.append(name)
        description = tool.get("description")
        if isinstance(description, str):
            names += NESTED_TOOL.findall(description)
    return names


def effective_tools(completed: subprocess.CompletedProcess[str]) -> list[str]:
    try:
        if completed.returncode != 0:
            raise ValueError
        items: object = json.loads(completed.stdout)
        if not isinstance(items, list):
            raise ValueError
    except ValueError:
        raise SandboxRulesError(
            f"{INVALID_MESSAGE}: Codex could not report its effective tools"
        ) from None
    names: list[str] = []
    for raw in cast(list[object], items):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, Any], raw)
        tools = item.get("tools")
        if item.get("type") == "additional_tools" and isinstance(tools, list):
            names += _tool_names(cast(list[object], tools))
    return names


def verify_effective_tools(command: tuple[str, ...], overrides: list[str], runner: Runner) -> None:
    """Catálogo que a própria CLI montaria, sem inferência; o servidor sobe depois, na sessão."""
    completed = runner(
        [
            *command,
            "debug",
            "prompt-input",
            *overrides,
            "--config",
            f"mcp_servers.{SERVER_NAME}.enabled=false",
            "tool catalog probe",
        ]
    )
    unexpected = sorted(set(effective_tools(completed)) - ALLOWED_EFFECTIVE_TOOLS)
    if unexpected:
        raise SandboxRulesError(
            f"{INVALID_MESSAGE}: Codex would expose tools outside the Lastro catalog: "
            + ", ".join(unexpected[:10])
        )


def sandbox_self_test(
    command: tuple[str, ...],
    overrides: list[str],
    identity: SessionIdentity,
    runner: Runner,
) -> None:
    """Prova na plataforma atual, sem inferência, que a sandbox nega leitura, escrita e rede."""
    scratch = identity.private_root / "codex-sandbox-probe"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=scratch, prefix="canary-", delete=False) as canary:
        canary_path = Path(canary.name)
    targets = [str(canary_path)]
    env_file = identity.project_dir / ".env"
    if env_file.is_file():
        targets.append(str(env_file))
    write_target = identity.project_dir / f".lastro-sandbox-probe-{canary_path.name}"
    arguments = [
        *command,
        "sandbox",
        *overrides,
        "--permission-profile",
        PROFILE_NAME,
        "--cd",
        str(identity.project_dir),
        "--",
        sys.executable,
        "-I",
        "-c",
        SELF_TEST_SCRIPT,
        str(write_target),
        *targets,
    ]
    try:
        completed = runner(arguments)
        # O login do usuário da sandbox no Windows falha de forma transitória (erro 267).
        for _ in range(SELF_TEST_ATTEMPTS - 1):
            if completed.returncode == 0 or completed.returncode in SELF_TEST_VERDICTS:
                break
            time.sleep(SELF_TEST_RETRY_SECONDS)
            completed = runner(arguments)
    finally:
        canary_path.unlink(missing_ok=True)
        write_target.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            scratch.rmdir()
    if completed.returncode != 0:
        raise SandboxRulesError(
            f"{INVALID_MESSAGE}: the Codex sandbox did not deny access on this platform "
            f"(self-test exit {completed.returncode})."
        )
