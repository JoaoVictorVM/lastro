"""Contratos consumidos pelo registro de runs, pela autenticação e pelo servidor de ferramentas."""

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from lastro.config.loader import load_project
from lastro.timeutil import utc_now_iso


def _write_project(
    write_config: Callable[..., Path], profile_toml: Callable[..., str], root: Path
) -> None:
    extra = (
        'version_endpoint = "/api/version"\n'
        'denied_methods = ["DELETE", "PATCH"]\n'
        'denied_endpoint_patterns = ["/admin/*", "/api/payroll/**"]\n'
        'editable_fields = { employee = ["phone", "email"] }'
    )
    write_config(
        f"private_data_root = '{root.as_posix()}'\n"
        + profile_toml("homolog", extra=extra)
        + profile_toml("local")
    )


def test_profile_name_and_private_root_resolve_for_run_registry(
    write_config: Callable[..., Path], profile_toml: Callable[..., str], workdir: Path
) -> None:
    root = workdir.parent / "dados-privados"
    _write_project(write_config, profile_toml, root)

    project = load_project(profile="local", cwd=workdir)

    assert project.profile_name == "local"
    assert project.private_data_root == root
    assert project.profile_dir == root / "profiles" / "local"
    assert project.profile_dir.is_relative_to(project.private_data_root)
    assert datetime.fromisoformat(utc_now_iso()).utcoffset() is not None
    assert project.timezone == "America/Sao_Paulo"


def test_login_fields_available_for_auth(
    write_config: Callable[..., Path],
    profile_toml: Callable[..., str],
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(write_config, profile_toml, workdir.parent / "root")
    monkeypatch.delenv("HOMOLOG_USERNAME", raising=False)
    monkeypatch.delenv("HOMOLOG_PASSWORD", raising=False)
    (workdir / ".env").write_text(
        "HOMOLOG_USERNAME=qa-user\nHOMOLOG_PASSWORD=senha-de-teste\n", encoding="utf-8"
    )

    project = load_project(profile="homolog", cwd=workdir)

    profile = project.profile
    assert profile.login_url == "https://homolog.example.test/login"
    assert profile.tenant_id == "acme"
    assert profile.tenant_display_name == "acme"
    username_var, password_var = profile.credential_variable_names()
    assert (username_var, password_var) == ("HOMOLOG_USERNAME", "HOMOLOG_PASSWORD")
    assert os.environ[username_var] == "qa-user"
    assert os.environ[password_var] == "senha-de-teste"


def test_policy_fields_available_for_tool_server(
    write_config: Callable[..., Path], profile_toml: Callable[..., str], workdir: Path
) -> None:
    _write_project(write_config, profile_toml, workdir.parent / "root")

    profile = load_project(profile="homolog", cwd=workdir).profile

    assert profile.target_url == "https://homolog.example.test"
    assert profile.allowed_entities == ["employee"]
    assert profile.editable_fields == {"employee": ["phone", "email"]}
    assert profile.denied_methods == ["DELETE", "PATCH"]
    assert profile.denied_endpoint_patterns == ["/admin/*", "/api/payroll/**"]
    assert profile.version_endpoint == "/api/version"
