"""Glob deliberadamente limitado, com caixa idêntica em Windows e Linux."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class IgnoreRule(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    url_path: str | None = Field(default=None, min_length=1, max_length=256)
    message: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_pattern(self) -> "IgnoreRule":
        if len(self.model_fields_set) != 1:
            raise ValueError("expected exactly one of url_path or message")
        pattern = self.url_path if self.url_path is not None else self.message
        if pattern is None or any(char in pattern for char in "[]\\"):
            raise ValueError("expected simple glob without classes or escapes")
        if self.url_path is not None and not self.url_path.startswith("/"):
            raise ValueError("url_path must start with /")
        return self


class OracleSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    ignore: list[IgnoreRule] = Field(default_factory=list[IgnoreRule], max_length=100)

    @field_validator("ignore")
    @classmethod
    def unique_rules(cls, rules: list[IgnoreRule]) -> list[IgnoreRule]:
        if len({(rule.url_path, rule.message) for rule in rules}) != len(rules):
            raise ValueError("duplicate ignore rules")
        return rules


def glob_matches(pattern: str, text: str) -> bool:
    expression = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(expression, text, flags=re.DOTALL) is not None


def ignored(rules: list[IgnoreRule], path: str, message: str) -> bool:
    return any(
        glob_matches(rule.url_path, path)
        if rule.url_path is not None
        else glob_matches(rule.message or "", message)
        for rule in rules
    )
