"""Saída de terminal: ícones de status com fallback ASCII e mensagens de erro em stderr."""

import sys
from enum import Enum
from typing import TextIO

import typer


class Status(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


UNICODE_ICONS = {Status.OK: "✓", Status.WARN: "⚠", Status.FAIL: "✗"}
ASCII_ICONS = {Status.OK: "[OK]", Status.WARN: "[WARN]", Status.FAIL: "[FAIL]"}
UNICODE_PATH_SEPARATOR = "›"
ASCII_PATH_SEPARATOR = ">"

_PROBE = "".join(UNICODE_ICONS.values()) + UNICODE_PATH_SEPARATOR


def supports_unicode(stream: TextIO | None = None) -> bool:
    target = stream if stream is not None else sys.stdout
    encoding = getattr(target, "encoding", None) or "ascii"
    try:
        _PROBE.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def icon(status: Status, unicode: bool | None = None) -> str:
    use_unicode = supports_unicode() if unicode is None else unicode
    return (UNICODE_ICONS if use_unicode else ASCII_ICONS)[status]


def to_console(text: str, stream: TextIO | None = None) -> str:
    """Troca os caracteres não ASCII usados pela CLI quando o console não os suporta."""
    if supports_unicode(stream):
        return text
    for status in Status:
        text = text.replace(UNICODE_ICONS[status], ASCII_ICONS[status])
    return text.replace(UNICODE_PATH_SEPARATOR, ASCII_PATH_SEPARATOR)


def info(message: str) -> None:
    typer.echo(to_console(message))


def error(message: str) -> None:
    typer.echo(to_console(message, sys.stderr), err=True)
