"""Contratos estritos da política de browser, carregados antes de iniciar a run."""

import re
from typing import Annotated, Literal, Self
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type Operation = Literal["read", "create", "update", "delete", "deactivate", "switch_tenant"]
type Origin = Literal["created_in_run", "preauthorized", "unknown"]
type Effect = Literal["allow", "deny", "require_approval"]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


def safe_path(value: str) -> str:
    decoded = unquote(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or any(c in value for c in "?#\\")
        or decoded != value
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("expected an unambiguous absolute path")
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Target(Strict):
    role: Text | None = None
    name: Text | None = None
    label: Text | None = None

    @model_validator(mode="after")
    def shape(self) -> Self:
        if not (
            (self.label is not None and self.role is None and self.name is None)
            or (self.label is None and self.role is not None and self.name is not None)
        ):
            raise ValueError("use label or role and name")
        return self


class RequestBinding(Strict):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: Text
    encoding: Literal["json", "form"] = "form"
    id_field: Text | None = None
    success_status: int = Field(default=201, ge=200, le=399)
    response_id: Text | None = None
    location_id: bool = False

    @field_validator("path")
    @classmethod
    def path_valid(cls, value: str) -> str:
        safe_path(value)
        for part in value.split("/"):
            if ("{" in part or "}" in part) and not re.fullmatch(
                r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", part
            ):
                raise ValueError("invalid named path segment")
        return value


class Binding(Strict):
    id: Text
    entity: Text
    operation: Operation
    route: Text
    target: Target
    form: Text | None = None
    fields: dict[str, str] = Field(default_factory=dict[str, str])
    technical_fields: list[str] = Field(default_factory=list[str])
    field_values: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])
    request: RequestBinding | None = None
    record_parameter: Text = "id"

    @model_validator(mode="after")
    def valid(self) -> Self:
        RequestBinding.path_valid(self.route)
        if self.operation != "read" and self.request is None:
            raise ValueError("write binding requires request")
        if len(set(self.fields.values())) != len(self.fields):
            raise ValueError("field mappings must be unique")
        if set(self.fields.values()) & set(self.technical_fields):
            raise ValueError("technical fields cannot replace business fields")
        if any(
            key not in self.fields.values() or not values
            for key, values in self.field_values.items()
        ):
            raise ValueError("field_values requires mapped fields and nonempty choices")
        return self


class Match(Strict):
    operations: list[Operation] | None = None
    entities: list[Text] | None = None
    fields: list[Text] | None = None
    record_origins: list[Origin] | None = None
    methods: list[Literal["GET", "POST", "PUT", "PATCH", "DELETE"]] | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if any(value == [] for value in self.model_dump().values()):
            raise ValueError("empty match lists are invalid")
        return self


class Rule(Strict):
    id: Text
    effect: Effect
    match: Match = Field(default_factory=Match)


class Preauthorized(Strict):
    tenant: Text
    entity: Text
    id: Text

    @field_validator("tenant", "entity", "id")
    @classmethod
    def exact(cls, value: str) -> str:
        if "*" in value or not value.strip():
            raise ValueError("preauthorization requires exact nonempty identifiers")
        return value


class BrowserPolicy(Strict):
    version: Literal[1] = 1
    bindings: list[Binding] = Field(default_factory=list[Binding])
    rules: list[Rule] = Field(default_factory=list[Rule])
    preauthorized_records: list[Preauthorized] = Field(default_factory=list[Preauthorized])
    auxiliary_origins: list[str] = Field(default_factory=list[str])
    destructive_terms: list[Text] = Field(default_factory=list[str])

    @field_validator("auxiliary_origins")
    @classmethod
    def origins(cls, values: list[str]) -> list[str]:
        for value in values:
            url = urlsplit(value)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.path
                or url.query
                or url.fragment
                or "*" in value
                or url.hostname.endswith(".")
                or url.hostname != url.hostname.encode("idna").decode()
            ):
                raise ValueError("auxiliary origins must be exact HTTPS origins")
            _ = url.port
        return values

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        for items in (self.bindings, self.rules):
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate policy id")
        return self
