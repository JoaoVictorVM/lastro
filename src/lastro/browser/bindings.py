"""Vínculo entre rota, formulário e a requisição única autorizada."""

import json
from dataclasses import dataclass
from typing import cast
from urllib.parse import parse_qsl, urlsplit

from lastro.config.policy import Binding, safe_path


def path_parameters(pattern: str, path: str) -> dict[str, str] | None:
    try:
        safe_path(path)
    except ValueError:
        return None
    left, right = pattern.split("/"), path.split("/")
    if len(left) != len(right):
        return None
    result: dict[str, str] = {}
    for expected, actual in zip(left, right, strict=True):
        if expected.startswith("{") and expected.endswith("}"):
            if not actual:
                return None
            result[expected[1:-1]] = actual
        elif expected != actual:
            return None
    return result


def request_values(body: str | None, encoding: str) -> dict[str, str]:
    if body is None:
        return {}
    if len(body) > 65536:
        raise ValueError("body_too_large")
    if encoding == "form":
        pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
        if len(dict(pairs)) != len(pairs):
            raise ValueError("duplicate_fields")
        return dict(pairs)

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        if len(dict(pairs)) != len(pairs):
            raise ValueError("duplicate_fields")
        return dict(pairs)

    value: object = json.loads(body, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("expected_object")
    result: dict[str, str] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError("expected_string_fields")
        result[key] = item
    return result


@dataclass
class ExpectedRequest:
    binding: Binding
    url: str
    values: dict[str, str]
    action_id: str
    used: bool = False

    def consume(self, method: str, url: str, body: str | None) -> bool:
        request = self.binding.request
        if self.used or request is None or method != request.method or url != self.url:
            return False
        try:
            actual = request_values(body, request.encoding)
        except (ValueError, TypeError):
            return False
        if actual != self.values:
            return False
        self.used = True
        return True


def denied_endpoint(pattern: str, url: str) -> bool:
    path = urlsplit(url).path
    if pattern.endswith("/*") and not pattern.endswith("/**"):
        pattern += "*"
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return path == pattern
