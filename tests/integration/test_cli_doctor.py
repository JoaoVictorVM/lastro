from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lastro.cli import doctor_cmd
from lastro.cli.app import app
from lastro.cli.output import ASCII_ICONS, UNICODE_ICONS
from lastro.doctor.checks import DoctorEnvironment

UNICODE_PREFIXES = tuple(f"{icon} " for icon in UNICODE_ICONS.values())
ASCII_PREFIXES = tuple(f"{icon} " for icon in ASCII_ICONS.values())


def _use_env(monkeypatch: pytest.MonkeyPatch, env: DoctorEnvironment) -> None:
    monkeypatch.setattr(doctor_cmd, "environment_factory", lambda: env)


def _check_lines(output: str, prefixes: tuple[str, ...] = UNICODE_PREFIXES) -> list[str]:
    return [line for line in output.splitlines() if line.startswith(prefixes)]


def test_doctor_runs_all_13_checks(
    runner: CliRunner,
    healthy_project: Path,
    doctor_env: Callable[..., DoctorEnvironment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_env(monkeypatch, doctor_env())

    result = runner.invoke(app, ["doctor"])

    lines = _check_lines(result.stdout)
    assert len(lines) == 13, result.stdout


def test_exit_code_0_all_pass(
    runner: CliRunner,
    healthy_project: Path,
    doctor_env: Callable[..., DoctorEnvironment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_env(monkeypatch, doctor_env())

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.stdout
    assert all(line.startswith("✓ ") for line in _check_lines(result.stdout))


def test_exit_code_1_warnings_only(
    runner: CliRunner,
    healthy_project: Path,
    doctor_env: Callable[..., DoctorEnvironment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_env(monkeypatch, doctor_env(which=lambda name: None if name == "uv" else name))

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1, result.stdout
    assert "⚠ uv: " in result.stdout


def test_exit_code_2_any_failure(
    runner: CliRunner,
    healthy_project: Path,
    doctor_env: Callable[..., DoctorEnvironment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = doctor_env(
        which=lambda name: None if name == "uv" else name, chromium_installed=lambda: False
    )
    _use_env(monkeypatch, env)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 2, result.stdout


def test_failure_has_one_sentence_fix(
    runner: CliRunner,
    healthy_project: Path,
    doctor_env: Callable[..., DoctorEnvironment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = doctor_env(
        which=lambda name: None if name == "uv" else name, chromium_installed=lambda: False
    )
    _use_env(monkeypatch, env)

    lines = runner.invoke(app, ["doctor"]).stdout.splitlines()

    flagged = [i for i, line in enumerate(lines) if line.startswith(("⚠ ", "✗ "))]
    assert len(flagged) == 2
    for index in flagged:
        fix = lines[index + 1]
        assert fix.startswith("    Correção: ")
        assert fix.rstrip().endswith(".")


def test_ascii_fallback_icons(
    healthy_project: Path,
    doctor_env: Callable[..., DoctorEnvironment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_env(monkeypatch, doctor_env(which=lambda name: None if name == "uv" else name))

    result = CliRunner(charset="ascii").invoke(app, ["doctor"])

    assert result.exit_code == 1
    lines = _check_lines(result.stdout, ASCII_PREFIXES)
    assert len(lines) == 13
    assert "[WARN] uv: " in result.stdout
    assert not any(icon in result.stdout for icon in UNICODE_ICONS.values())


def test_invalid_config_reported_and_dependents_fail(
    runner: CliRunner,
    healthy_project: Path,
    doctor_env: Callable[..., DoctorEnvironment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (healthy_project / "lastro.toml").write_text("profiles = 1\n", encoding="utf-8")
    _use_env(monkeypatch, doctor_env())

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 2
    assert "lastro.toml › profiles: expected table of profiles, got integer" in result.stdout
    assert result.stdout.count("depende de configuração válida") == 4
    assert len(_check_lines(result.stdout)) == 13


def test_multiple_profiles_one_line_each_for_network_checks(
    runner: CliRunner,
    healthy_project: Path,
    doctor_env: Callable[..., DoctorEnvironment],
    profile_toml: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = healthy_project / "lastro.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + profile_toml("local", credentials_prefix="HOMOLOG"),
        encoding="utf-8",
    )
    _use_env(monkeypatch, doctor_env())

    result = runner.invoke(app, ["doctor"])

    lines = _check_lines(result.stdout)
    assert len(lines) == 15
    for profile in ("homolog", "local"):
        assert sum(f"URL alvo acessível [{profile}]" in line for line in lines) == 1
        assert sum(f"Version endpoint acessível [{profile}]" in line for line in lines) == 1

    selected = runner.invoke(app, ["--profile", "local", "doctor"])
    selected_lines = _check_lines(selected.stdout)
    assert len(selected_lines) == 13
    assert not any("[homolog]" in line for line in selected_lines)


def test_doctor_diagnoses_bundle_and_instructions(
    runner: CliRunner,
    healthy_project: Path,
    doctor_env: Callable[..., DoctorEnvironment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_env(monkeypatch, doctor_env())
    healthy = runner.invoke(app, ["doctor"])
    assert "✓ Bundle de skills: sha256 " in healthy.stdout

    claude = healthy_project / "CLAUDE.md"
    claude.write_text(claude.read_text(encoding="utf-8") + "linha\n" * 60, encoding="utf-8")
    long = runner.invoke(app, ["doctor"])
    assert long.exit_code == 2
    assert "CLAUDE.md excede 60 linhas" in long.stdout

    claude.write_text("# Regras locais\n", encoding="utf-8")
    (healthy_project / "skills" / "lastro" / "reproduce.md").write_text("x", encoding="utf-8")
    broken = runner.invoke(app, ["doctor"])
    assert broken.exit_code == 2
    assert "skills/lastro/reproduce.md difere do manifest" in broken.stdout
    assert "Rode lastro init" in broken.stdout
