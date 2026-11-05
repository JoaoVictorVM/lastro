import copy
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from lastro.browser.tools import TOOLS
from lastro.codex import config as cc


def identity(tmp_path: Path, profile: str = "local") -> cc.SessionIdentity:
    return cc.SessionIdentity(
        project_dir=tmp_path / "project with space",
        config_path=tmp_path / "project with space" / "lastro.toml",
        profile=profile,
        run_id="run_20261104_150000_ab12",
        private_root=tmp_path / "private",
        codex_home=tmp_path / "home",
        instructions='Bundle "quoted"\nsecond line',
    )


def parsed_overrides(overrides: list[str]) -> dict[str, str]:
    assert overrides[0::2] == ["--config"] * (len(overrides) // 2)
    return dict(item.split("=", 1) for item in overrides[1::2])


def test_effective_security_policy() -> None:
    policy = cc.expected_policy()

    assert policy["forced_login_method"] == "chatgpt"
    assert policy["model_provider"] == "openai"
    assert policy["web_search"] == "disabled"
    assert policy["features"]["shell_tool"] is False
    assert policy["features"]["plugins"] is False
    assert policy["features"]["hooks"] is False
    assert policy["permissions"]["lastro"]["network"]["enabled"] is False
    assert policy["mcp_servers"]["lastro"]["required"] is True
    assert policy["mcp_servers"]["lastro"]["enabled_tools"] == sorted(TOOLS)
    assert cc.policy_problems(policy) == []


def test_private_paths_resolved(tmp_path: Path) -> None:
    session = identity(tmp_path)
    values = parsed_overrides(cc.session_overrides(cc.expected_policy(), session, []))

    filesystem = tomllib.loads("x=" + values["permissions.lastro.filesystem"])["x"]

    assert filesystem[session.private_root.resolve().as_posix()] == "deny"
    assert filesystem[(session.codex_home / "auth.json").as_posix()] == "deny"
    roots = filesystem[":workspace_roots"]
    for pattern in (".env", ".env.*", "**/.env", "**/.env.*", ".auth", "evals/catalog"):
        assert roots[pattern] == "deny"


def test_overrides_are_valid_toml_and_pinned(tmp_path: Path) -> None:
    session = identity(tmp_path)
    values = parsed_overrides(cc.session_overrides(cc.expected_policy(), session, ["node_repl"]))

    for value in values.values():
        tomllib.loads("x=" + value)
    server = tomllib.loads("x=" + values["mcp_servers.lastro"])["x"]
    assert server["command"] == sys.executable
    assert server["args"][-2:] == ["--run-id", session.run_id]
    assert server["args"][server["args"].index("--profile") + 1] == "local"
    assert tomllib.loads("x=" + values["developer_instructions"])["x"] == session.instructions
    assert values["mcp_servers.node_repl.enabled"] == "false"
    assert values["features.shell_tool"] == "false"
    assert values["skills.bundled.enabled"] == "false"


def test_foreign_mcp_rejected() -> None:
    policy = copy.deepcopy(cc.expected_policy())
    policy["mcp_servers"]["playwright"] = {"command": "npx"}
    assert cc.policy_problems(policy) == ["foreign MCP server playwright"]


def test_provider_override_rejected() -> None:
    policy = copy.deepcopy(cc.expected_policy())
    policy["model_provider"] = "other"
    assert cc.policy_problems(policy) == ["unexpected value for model_provider"]


def test_unknown_security_field_rejected() -> None:
    policy = copy.deepcopy(cc.expected_policy())
    policy["openai_base_url"] = "https://other.example"
    policy["features"]["shell_tool"] = True
    problems = cc.policy_problems(policy)
    assert "unknown security field openai_base_url" in problems
    assert "unexpected value for features.shell_tool" in problems
    assert not any("other.example" in problem for problem in problems)


def test_tunable_model_is_allowed() -> None:
    assert cc.policy_problems({**cc.expected_policy(), "model": "any"}) == []


def test_load_policy_messages(tmp_path: Path) -> None:
    with pytest.raises(cc.SandboxRulesError) as missing:
        cc.load_policy(tmp_path)
    assert str(missing.value) == (
        "Codex sandbox rules not found; run 'lastro init --executor codex'."
    )
    target = tmp_path / cc.CONFIG_FILE
    target.parent.mkdir()
    target.write_text("web_search = 'live'\n", encoding="utf-8")
    with pytest.raises(cc.SandboxRulesError, match="Codex sandbox rules are invalid"):
        cc.load_policy(tmp_path)


def _write_generated(tmp_path: Path) -> None:
    config = cc.render_config().encode()
    (tmp_path / cc.CONFIG_FILE).parent.mkdir()
    (tmp_path / cc.CONFIG_FILE).write_bytes(config)
    (tmp_path / cc.INSTRUCTIONS_FILE).write_bytes(b"agents\n")
    (tmp_path / cc.MANIFEST_FILE).write_text(
        cc.render_manifest(config, b"agents\n"), encoding="utf-8"
    )


def test_manifest_detects_edits(tmp_path: Path) -> None:
    _write_generated(tmp_path)
    assert len(cc.verify_manifest(tmp_path)) == 64

    (tmp_path / cc.INSTRUCTIONS_FILE).write_bytes(b"edited\n")

    with pytest.raises(cc.SandboxRulesError, match="differs"):
        cc.verify_manifest(tmp_path)


def test_manifest_rejects_extra_fields(tmp_path: Path) -> None:
    _write_generated(tmp_path)
    data = json.loads((tmp_path / cc.MANIFEST_FILE).read_text(encoding="utf-8"))
    data["token"] = "x"
    (tmp_path / cc.MANIFEST_FILE).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(cc.SandboxRulesError, match="expected contract"):
        cc.verify_manifest(tmp_path)


def test_user_config_that_cannot_be_neutralized(tmp_path: Path) -> None:
    assert cc.user_config_problems(tmp_path) == []
    (tmp_path / "config.toml").write_text(
        'model = "x"\nopenai_base_url = "https://o"\n', encoding="utf-8"
    )
    assert cc.user_config_problems(tmp_path) == [
        "user configuration sets openai_base_url, which cannot be neutralized"
    ]


def _listing(*servers: dict[str, Any]) -> Any:
    def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, json.dumps(list(servers)), "")

    return run


def test_foreign_server_that_stays_enabled_is_refused(tmp_path: Path) -> None:
    session = identity(tmp_path)
    policy = cc.expected_policy()
    pinned = {"transport": {"args": cc.pinned_server(policy, session)["args"]}}
    with pytest.raises(cc.SandboxRulesError, match="other than lastro"):
        cc.resolve_foreign_servers(
            ("codex",),
            policy,
            session,
            _listing({"name": "lastro", **pinned}, {"name": "node_repl", "enabled": True}),
        )
    names = cc.resolve_foreign_servers(
        ("codex",),
        policy,
        session,
        _listing({"name": "lastro", **pinned}, {"name": "node_repl", "enabled": False}),
    )
    assert names == ["node_repl"]


def test_unpinned_server_is_refused(tmp_path: Path) -> None:
    with pytest.raises(cc.SandboxRulesError, match="not pinned"):
        cc.resolve_foreign_servers(
            ("codex",),
            cc.expected_policy(),
            identity(tmp_path),
            _listing({"name": "lastro", "transport": {"args": []}}),
        )


def _prompt_input(*tools: dict[str, Any]) -> Any:
    def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        body = [{"type": "additional_tools", "tools": [{"tools": list(tools)}]}]
        return subprocess.CompletedProcess(arguments, 0, json.dumps(body), "")

    return run


def test_effective_tools_stay_inside_the_allowlist() -> None:
    allowed = {"name": "exec", "description": "### `apply_patch`\n### `clock__curr_time`\n"}
    cc.verify_effective_tools(("codex",), [], _prompt_input(allowed))


@pytest.mark.parametrize(
    "tool",
    [
        {"name": "exec_command"},
        {"name": "web_search"},
        {"name": "exec", "description": "### `mcp__node_repl__js`\n"},
    ],
)
def test_alternative_tools_are_refused(tool: dict[str, Any]) -> None:
    with pytest.raises(cc.SandboxRulesError, match="outside the Lastro catalog"):
        cc.verify_effective_tools(("codex",), [], _prompt_input(tool))


def test_self_test_requires_denial(tmp_path: Path) -> None:
    session = identity(tmp_path)
    session.project_dir.mkdir()
    seen: list[list[str]] = []

    def denied(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    cc.sandbox_self_test(("codex",), [], session, denied)
    assert "--permission-profile" in seen[0]
    assert not (session.private_root / "codex-sandbox-probe").exists()

    def readable(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 3, "", "")

    with pytest.raises(cc.SandboxRulesError, match="did not deny"):
        cc.sandbox_self_test(("codex",), [], session, readable)


def test_self_test_retries_transient_sandbox_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc, "SELF_TEST_RETRY_SECONDS", 0)
    session = identity(tmp_path)
    session.project_dir.mkdir()
    codes = [1, 1, 0]

    def flaky(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, codes.pop(0), "", "")

    cc.sandbox_self_test(("codex",), [], session, flaky)
    assert codes == []

    def broken(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 1, "", "")

    with pytest.raises(cc.SandboxRulesError, match="self-test exit 1"):
        cc.sandbox_self_test(("codex",), [], session, broken)


def test_self_test_verdict_is_not_retried(tmp_path: Path) -> None:
    session = identity(tmp_path)
    session.project_dir.mkdir()
    calls: list[int] = []

    def readable(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess(arguments, 3, "", "")

    with pytest.raises(cc.SandboxRulesError):
        cc.sandbox_self_test(("codex",), [], session, readable)
    assert len(calls) == 1


def test_self_test_script_detects_exposed_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("x", encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-I", "-c", cc.SELF_TEST_SCRIPT, str(tmp_path / "w"), str(secret)],
        check=False,
    )
    assert done.returncode == 3
