import os
import stat
import sys
from pathlib import Path

import pytest

from lastro.change_context import ScanLevel
from lastro.change_context.scanner import Gitleaks, IssueSanitizer, ScannerError, redact_lines


def aws_key() -> str:
    # Montado em tempo de execução para não acionar o scanner do próprio repositório.
    return "AKIA" + "IOSFODNN7" + "EXAMPLQ"


def _fake_scanner(tmp_path: Path, exit_code: int, output: str = "") -> str:
    if sys.platform == "win32":
        script = tmp_path / "fake-gitleaks.cmd"
        script.write_text(
            f"@echo off\r\necho {output}\r\nexit /b {exit_code}\r\n", encoding="utf-8"
        )
    else:
        script = tmp_path / "fake-gitleaks"
        script.write_text(f"#!/bin/sh\necho '{output}'\nexit {exit_code}\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_missing_or_failed_scanner_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ScannerError, match="gitleaks not found"):
        Gitleaks().ensure()
    broken = Gitleaks(_fake_scanner(tmp_path, 3, "raw diagnostic with password=hunter2"))
    with pytest.raises(ScannerError) as error:
        broken.ensure()
    assert "hunter2" not in str(error.value)
    # Uma versão que não detecta o segredo sintético é incompatível com as regras confiáveis.
    blind = Gitleaks(_fake_scanner(tmp_path, 0, "v0.0.0"))
    with pytest.raises(ScannerError, match="incompatible"):
        blind.ensure()


def test_repo_config_and_inline_allow_are_not_trusted(
    gitleaks: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_all = tmp_path / "allow.toml"
    allow_all.write_text('[allowlist]\nregexes = [".*"]\n', encoding="utf-8")
    (tmp_path / ".gitleaks.toml").write_text(allow_all.read_text(), encoding="utf-8")
    (tmp_path / ".gitleaksignore").write_text("*\n", encoding="utf-8")
    monkeypatch.setenv("GITLEAKS_CONFIG", str(allow_all))
    monkeypatch.setenv("GITLEAKS_CONFIG_TOML", allow_all.read_text())
    monkeypatch.chdir(tmp_path)
    scanner = Gitleaks()
    scanner.ensure()
    text = f'aws = "{aws_key()}"  # gitleaks:allow\n'
    assert scanner.findings([text]) == [{1}]


def test_findings_map_back_to_each_buffer(gitleaks: str) -> None:
    scanner = Gitleaks()
    found = scanner.findings(["a\nb\n", f'x = 1\nkey = "{aws_key()}"\n', "clean\n"])
    assert found == [set(), {2}, set()]


def test_no_raw_diagnostics_escape(gitleaks: str) -> None:
    sanitizer = IssueSanitizer(Gitleaks())
    texts, level = sanitizer([f"Use the key {aws_key()} to log in.\nSecond line."])
    assert level == ScanLevel.GITLEAKS
    assert aws_key() not in texts[0]
    assert texts[0] == "[REDACTED]\nSecond line."
    metadata = Gitleaks().ensure()
    assert aws_key() not in repr(metadata)
    assert set(metadata) == {"tool", "version", "rules_sha256"}


def test_scan_precedes_truncation(gitleaks: str) -> None:
    # O segredo fica além do ponto em que um resumo seria cortado: ainda assim é detectado.
    text = "x\n" * 5000 + f'late = "{aws_key()}"\n'
    assert Gitleaks().findings([text]) == [{5001}]


def test_redact_lines_replaces_whole_line() -> None:
    assert redact_lines("a\nsecret\nb", {2}) == "a\n[REDACTED]\nb"
    assert redact_lines("a", set()) == "a"


def test_scanner_environment_drops_gitleaks_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    from lastro.change_context.scanner import _env  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv("GITLEAKS_CONFIG", "x")
    monkeypatch.setenv("gitleaks_config_toml", "y")
    env = _env()
    assert not any("GITLEAKS" in key.upper() for key in env)
    assert env.get("PATH") == os.environ.get("PATH")
