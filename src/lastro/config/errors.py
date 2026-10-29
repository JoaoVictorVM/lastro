"""Tradução de erros de configuração para `arquivo › caminho: esperado, got recebido`."""

import re
import tomllib
from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import Path

from pydantic import ValidationError
from pydantic_core import ErrorDetails

PATH_SEPARATOR = "›"
NOT_EXECUTED_MESSAGE = "Configuração inválida: nenhum comando foi executado."

# Descrições por campo têm prioridade sobre a descrição genérica do tipo de erro.
_FIELD_EXPECTATIONS: tuple[tuple[str, str], ...] = (
    ("profiles.*.editable_fields.*.*", "field name (text)"),
    ("profiles.*.editable_fields.*", "list of field names"),
    ("profiles.*.editable_fields", "table of entity name to list of field names"),
    ("profiles.*.allowed_entities.*", "entity name (text)"),
    ("profiles.*.allowed_entities", "list of entity names"),
    ("profiles.*.denied_methods.*", "HTTP method (text)"),
    ("profiles.*.denied_methods", "list of HTTP methods"),
    ("profiles.*.denied_endpoint_patterns.*", "endpoint pattern (text)"),
    ("profiles.*.denied_endpoint_patterns", "list of endpoint patterns"),
    ("profiles.*.credentials.*", "environment variable name (text)"),
    ("profiles.*.credentials", "table with username_env and password_env"),
    ("profiles.*.run_limits", "table of run limits"),
    ("profiles.*.target_url", "http or https URL with host"),
    ("profiles.*.login_url", "http or https URL with host"),
    ("profiles.*.version_endpoint", "path starting with / or http(s) URL"),
    ("profiles.*", "profile table"),
    ("profiles", "table of profiles"),
    ("demo.port", "integer between 1024 and 65535"),
    ("demo", "table"),
    ("timezone", "IANA timezone name (e.g. America/Sao_Paulo)"),
    ("private_data_root", "directory path (text)"),
)

_GENERIC_EXPECTATIONS: dict[str, str] = {
    "string_type": "text",
    "int_type": "integer",
    "int_parsing": "integer",
    "float_type": "number",
    "bool_type": "boolean",
    "list_type": "list",
    "dict_type": "table",
    "model_type": "table",
    "model_attributes_type": "table",
}

_RANGE_ERRORS = frozenset({"greater_than", "greater_than_equal", "less_than", "less_than_equal"})

_TOML_LOCATION = re.compile(r"\s*\(at line (\d+), column (\d+)\)\s*$")


class ConfigError(Exception):
    """Configuração ausente ou inválida; cada linha é uma mensagem pronta para o terminal."""

    def __init__(self, lines: Sequence[str]) -> None:
        super().__init__("\n".join(lines))
        self.lines = list(lines)


def display_path(path: Path, cwd: Path | None = None) -> str:
    base = (cwd or Path.cwd()).resolve()
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(base))
    except ValueError:
        return str(resolved)


def describe_received(value: object) -> str:
    """Descreve só o tipo recebido: valores podem ser segredos e nunca são exibidos."""
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list | tuple):
        return "list"
    if isinstance(value, dict):
        count = len(value)  # pyright: ignore[reportUnknownArgumentType]
        return f"table with {count} {'entry' if count == 1 else 'entries'}"
    return type(value).__name__


def _field_path(loc: Sequence[int | str]) -> str:
    return ".".join(str(part) for part in loc)


def _field_expectation(path: str) -> str | None:
    for pattern, expectation in _FIELD_EXPECTATIONS:
        if fnmatchcase(path, pattern) and pattern.count(".") == path.count("."):
            return expectation
    return None


def _expected_text(error: ErrorDetails, path: str) -> str:
    error_type = error["type"]
    if error_type == "missing":
        return "expected a value (required field)"
    if error_type == "extra_forbidden":
        return "expected no field with this name (unknown field)"
    if error_type.startswith("lastro_"):
        return error["msg"]
    field_expectation = _field_expectation(path)
    if error_type in _RANGE_ERRORS and field_expectation is None:
        return f"expected {error['msg'].lower()}"
    if field_expectation is not None:
        return f"expected {field_expectation}"
    generic = _GENERIC_EXPECTATIONS.get(error_type)
    if generic is not None:
        return f"expected {generic}"
    return f"expected valid value ({error['msg']})"


def format_validation_error(exc: ValidationError, file_label: str) -> list[str]:
    lines: list[str] = []
    for error in exc.errors(include_url=False, include_input=True):
        path = _field_path(error["loc"])
        received = "nothing" if error["type"] == "missing" else describe_received(error["input"])
        location = f"{file_label} {PATH_SEPARATOR} {path}" if path else file_label
        lines.append(f"{location}: {_expected_text(error, path)}, got {received}")
    return lines


def format_toml_error(exc: tomllib.TOMLDecodeError, file_label: str) -> str:
    message = str(exc)
    match = _TOML_LOCATION.search(message)
    if match is None:
        return f"{file_label}: expected valid TOML, got syntax error ({message})"
    line, column = match.group(1), match.group(2)
    reason = message[: match.start()]
    return (
        f"{file_label} {PATH_SEPARATOR} line {line}, column {column}: "
        f"expected valid TOML, got syntax error ({reason})"
    )
