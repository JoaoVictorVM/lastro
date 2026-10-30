"""Contratos consumidos pelo registro de runs, pela autenticação e pelo servidor de ferramentas."""

import dataclasses
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from lastro import history
from lastro.config.loader import ProjectContext, load_project
from lastro.history import (
    ArtifactType,
    EventType,
    PolicyDecision,
    RunStatus,
    TextMission,
    open_history,
)
from lastro.history.database import Database
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


def test_run_record_stores_active_profile_and_resolves_folder_under_private_root(
    history_context_for: Callable[[str], ProjectContext],
) -> None:
    context = history_context_for("local")
    store = open_history(context)

    created = store.create_run(context, TextMission("Explore"))

    assert created.record.profile == "local"
    assert created.record.project_id == context.require_project_id()
    assert created.paths.run_dir == (
        context.private_data_root / context.require_project_id() / "runs" / created.record.id
    )
    assert created.paths.run_dir.is_relative_to(context.private_data_root)


def test_consumer_facade_covers_active_run_events_artifacts_and_limits(
    history_context: ProjectContext,
) -> None:
    store = open_history(history_context)
    assert store.get_active_run() is None
    run_id = store.create_run(history_context, TextMission("Explore o cadastro")).record.id

    active = store.get_active_run()
    assert active is not None and active.id == run_id
    assert active.mission_text == "Explore o cadastro"
    assert active.config_snapshot["run_limits"] == {"max_actions": 40, "max_minutes": 30}
    call = store.append_event(
        run_id,
        EventType.TOOL_CALL,
        tool_name="click",
        arguments={"role": "button", "name": "Salvar"},
        correlation_id="c1",
        idempotency_key="c1",
    )
    decision = store.append_event(
        run_id,
        EventType.POLICY_DECISION,
        tool_name="click",
        arguments={"role": "button", "name": "Salvar"},
        policy_decision=PolicyDecision.REQUIRE_APPROVAL,
        correlation_id="c1",
        idempotency_key="c1",
    )
    artifact = store.register_artifact(
        run_id, "evidence/salvar.png", ArtifactType.SCREENSHOT, content=b"png"
    )

    events = store.list_events(run_id)
    assert [event.sequence for event in events] == [1, 2, 3]
    assert (call.correlation_id, decision.correlation_id) == ("c1", "c1")
    assert store.list_artifacts(run_id) == [artifact]
    path = store.run_paths(run_id).run_dir / artifact.relative_path
    assert artifact.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    finished = store.finish_run(run_id, RunStatus.COMPLETED)
    assert finished.ended_at is not None and store.get_active_run() is None


def _public_fields(value: object) -> set[str]:
    if dataclasses.is_dataclass(value):
        return {field.name for field in dataclasses.fields(value)}
    return set()


def test_public_api_exposes_no_sqlite_connection_or_secret_fields(
    history_context: ProjectContext,
) -> None:
    for name in history.__all__:
        exported = getattr(history, name)
        assert exported not in (sqlite3.Connection, sqlite3.Cursor, sqlite3.Row), name
        assert not isinstance(exported, sqlite3.Connection | sqlite3.Cursor), name
        assert "Database" not in name and "Connection" not in name
    store = open_history(history_context)
    for attribute in dir(store):
        if attribute.startswith("_"):
            continue
        value = getattr(store, attribute)
        assert not isinstance(value, sqlite3.Connection | sqlite3.Cursor | Database), attribute
    created = store.create_run(history_context, TextMission("Explore"))
    forbidden = ("password", "secret", "token", "credential", "connection", "cursor")
    for dto in (created, created.record, created.paths, *store.list_runs()):
        for field in _public_fields(dto):
            assert not any(word in field for word in forbidden), (type(dto).__name__, field)
    snapshot = json.dumps(created.record.config_snapshot)
    assert "credentials" not in snapshot
    assert "PASSWORD" not in snapshot
