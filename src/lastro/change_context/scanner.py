"""Detecção de segredos com gitleaks em modo stdin, com regras do Lastro e falha fechada.

O repositório analisado não fornece configuração, baseline, ignore file nem allow inline, e
variáveis GITLEAKS_* são removidas do ambiente. Diagnósticos do scanner nunca são repassados:
só contagens e posições de linha saem daqui.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from importlib.resources import as_file, files
from typing import cast

from lastro.change_context.models import ScanLevel
from lastro.history.redaction import REDACTED, redact_text

SCAN_TIMEOUT_SECONDS = 60.0
LEAKS_EXIT_CODE = 2
SCAN_FAILED = "gitleaks failed while scanning; no code was delivered."
# Separação entre buffers do lote: regras multilinha não atravessam arquivos distintos.
_SEPARATOR_LINES = 3


class ScannerError(Exception):
    """Scanner ausente, incompatível ou com falha; nenhum conteúdo de código é entregue."""


def _rules() -> bytes:
    return files("lastro.change_context").joinpath("gitleaks.toml").read_bytes()


def rules_sha256() -> str:
    return hashlib.sha256(_rules()).hexdigest()


def _canary() -> str:
    # Montado em tempo de execução para não acionar o scanner do próprio repositório.
    return 'aws_access_key_id = "' + "AKIA" + "IOSFODNN7" + "EXAMPLQ" + '"\n'


def _env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if "GITLEAKS" not in key.upper()}


class Gitleaks:
    """Uma instância por coleta; `ensure` precisa passar antes de qualquer leitura de código."""

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable
        self.version: str | None = None

    @property
    def executable(self) -> str:
        found = self._executable or shutil.which("gitleaks")
        if found is None:
            raise ScannerError(
                "gitleaks not found; --source requires the secret scanner. "
                "Install gitleaks or run without --source."
            )
        return found

    def ensure(self) -> dict[str, object]:
        """Versão e autoteste com um segredo sintético: sem detecção, o scanner é incompatível."""
        try:
            completed = subprocess.run(
                [self.executable, "version"],
                capture_output=True,
                timeout=SCAN_TIMEOUT_SECONDS,
                env=_env(),
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ScannerError("gitleaks could not be started; run without --source.") from None
        if completed.returncode != 0:
            raise ScannerError("gitleaks version check failed; run without --source.")
        version = completed.stdout.decode("utf-8", "replace").strip().splitlines()
        self.version = redact_text(version[0][:80]) if version else "unknown"
        if not self.findings([_canary()])[0]:
            raise ScannerError(
                "gitleaks is incompatible with the trusted rules (self-test failed)."
            )
        return self.metadata()

    def metadata(self) -> dict[str, object]:
        return {"tool": "gitleaks", "version": self.version, "rules_sha256": rules_sha256()}

    def findings(self, texts: Sequence[str]) -> list[set[int]]:
        """Linhas (1-based) com possível segredo em cada texto, numa única execução."""
        if not texts:
            return []
        lines: list[str] = []
        owners: list[tuple[int, int]] = []
        for index, text in enumerate(texts):
            for number, line in enumerate(text.split("\n"), start=1):
                lines.append(line)
                owners.append((index, number))
            for _ in range(_SEPARATOR_LINES):
                lines.append("")
                owners.append((-1, 0))
        payload = "\n".join(lines).encode("utf-8")
        result: list[set[int]] = [set() for _ in texts]
        for start, end in self._scan(payload):
            for line in range(start, end + 1):
                if 1 <= line <= len(owners):
                    owner, number = owners[line - 1]
                    if owner >= 0:
                        result[owner].add(number)
        return result

    def _scan(self, payload: bytes) -> list[tuple[int, int]]:
        with (
            as_file(files("lastro.change_context").joinpath("gitleaks.toml")) as config,
            tempfile.TemporaryDirectory(prefix="lastro-scan-") as workdir,
        ):
            command = [
                self.executable,
                "stdin",
                "--no-banner",
                "--no-color",
                "--log-level",
                "error",
                "--config",
                str(config),
                "--gitleaks-ignore-path",
                workdir,
                "--ignore-gitleaks-allow",
                "--redact",
                "--max-decode-depth",
                "5",
                "--report-format",
                "json",
                "--report-path",
                "-",
                "--exit-code",
                str(LEAKS_EXIT_CODE),
            ]
            try:
                completed = subprocess.run(
                    command,
                    input=payload,
                    capture_output=True,
                    timeout=SCAN_TIMEOUT_SECONDS,
                    env=_env(),
                    cwd=workdir,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ScannerError(
                    "gitleaks failed while scanning; no code was delivered."
                ) from None
        if completed.returncode not in (0, LEAKS_EXIT_CODE):
            raise ScannerError(SCAN_FAILED)
        if completed.returncode == 0:
            return []
        try:
            report = json.loads(completed.stdout.decode("utf-8") or "[]")
        except (UnicodeDecodeError, ValueError):
            raise ScannerError("gitleaks returned an unreadable report.") from None
        spans: list[tuple[int, int]] = []
        for item in cast(list[object], report if isinstance(report, list) else []):
            if not isinstance(item, dict):
                continue
            entry = cast(dict[str, object], item)
            start, end = entry.get("StartLine"), entry.get("EndLine")
            if isinstance(start, int) and isinstance(end, int):
                spans.append((start, max(start, end)))
        if not spans:
            # Código de vazamento sem posição legível: tratar o lote inteiro como suspeito.
            spans.append((1, payload.count(b"\n") + 1))
        return spans


def redact_lines(text: str, lines: set[int]) -> str:
    if not lines:
        return text
    return "\n".join(
        REDACTED if number in lines else line
        for number, line in enumerate(text.split("\n"), start=1)
    )


class IssueSanitizer:
    """Redação central e, quando o scanner está disponível, linhas suspeitas inteiras."""

    def __init__(self, scanner: Gitleaks | None) -> None:
        self.scanner = scanner

    def __call__(self, texts: list[str]) -> tuple[list[str], ScanLevel]:
        redacted = [redact_text(text) for text in texts]
        if self.scanner is None:
            return redacted, ScanLevel.REDACTION_ONLY
        try:
            found = self.scanner.findings(texts)
        except ScannerError:
            return redacted, ScanLevel.REDACTION_ONLY
        return [
            redact_lines(text, lines) for text, lines in zip(redacted, found, strict=True)
        ], ScanLevel.GITLEAKS


def available_scanner() -> Gitleaks | None:
    """Scanner pronto ou None; usado só onde a varredura é complementar (issues)."""
    scanner = Gitleaks()
    try:
        scanner.ensure()
    except ScannerError:
        return None
    return scanner
