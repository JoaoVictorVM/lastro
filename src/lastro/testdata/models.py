"""Pedidos e resultados de generate_test_data, validados antes de qualquer geração."""

from datetime import date
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

ENTITIES = ("employee",)
TYPES = ("name", "cpf", "email", "phone", "date", "text")
ENTITY_VARIANTS: dict[str, tuple[str, ...]] = {"employee": ("valid", "invalid_cpf")}
TYPE_VARIANTS: dict[str, tuple[str, ...]] = {
    "name": ("valid",),
    "cpf": ("valid", "invalid"),
    "email": ("valid",),
    "phone": ("valid",),
    "date": ("past", "today", "future", "boundary"),
    "text": ("valid",),
}
TEXT_MIN, TEXT_MAX = 1, 500
BOUNDARY_OFFSETS = (-1, 0, 1)


class TestDataError(ValueError):
    """Pedido recusado com mensagem pronta para o agente."""


class TestDataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    entity: str | None = None
    type: str | None = None
    variant: str | None = None
    length: int | None = None
    boundary_date: str | None = None
    offset_days: int | None = None

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (self.entity is None) == (self.type is None):
            raise TestDataError("Use exactly one of 'entity' or 'type'.")
        if self.entity is not None:
            if self.entity not in ENTITIES:
                raise TestDataError(
                    f"Unsupported test data entity '{self.entity}'. "
                    f"Supported: {', '.join(ENTITIES)}."
                )
            extra = [
                name
                for name in ("length", "boundary_date", "offset_days")
                if getattr(self, name) is not None
            ]
            if extra:
                raise TestDataError(f"'{extra[0]}' is not accepted with 'entity'.")
            self._variant(ENTITY_VARIANTS[self.entity], "valid")
            return self
        kind = self.type or ""
        if kind not in TYPES:
            raise TestDataError(
                f"Unsupported test data type '{kind}'. Supported: {', '.join(TYPES)}."
            )
        variant = self._variant(TYPE_VARIANTS[kind], "today" if kind == "date" else "valid")
        if kind == "text":
            if self.length is None or not TEXT_MIN <= self.length <= TEXT_MAX:
                raise TestDataError(f"'length' must be between {TEXT_MIN} and {TEXT_MAX}.")
        elif self.length is not None:
            raise TestDataError("'length' is accepted only with type 'text'.")
        if variant == "boundary":
            if self.boundary_date is None or self.offset_days is None:
                raise TestDataError("'boundary_date' and 'offset_days' are required.")
            try:
                date.fromisoformat(self.boundary_date)
            except ValueError:
                raise TestDataError("'boundary_date' must be an ISO date (YYYY-MM-DD).") from None
            if len(self.boundary_date) != 10:
                raise TestDataError("'boundary_date' must be an ISO date (YYYY-MM-DD).")
            if self.offset_days not in BOUNDARY_OFFSETS:
                raise TestDataError("'offset_days' must be -1, 0 or 1.")
        elif self.boundary_date is not None or self.offset_days is not None:
            raise TestDataError("'boundary_date' and 'offset_days' require variant 'boundary'.")
        return self

    def _variant(self, accepted: tuple[str, ...], default: str) -> str:
        if self.variant is None:
            self.variant = default
            return default
        if self.variant not in accepted:
            raise TestDataError(
                f"Unsupported variant '{self.variant}'. Supported: {', '.join(accepted)}."
            )
        return self.variant

    @property
    def resolved_variant(self) -> str:
        assert self.variant is not None
        return self.variant


def parse_request(raw: dict[str, Any]) -> TestDataRequest:
    """Converte o erro do Pydantic na mensagem curta definida pelo contrato."""
    try:
        return TestDataRequest.model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        context = first.get("ctx", {})
        cause = context.get("error")
        if isinstance(cause, TestDataError):
            raise TestDataError(str(cause)) from None
        field = ".".join(str(part) for part in first["loc"]) or "request"
        raise TestDataError(f"Invalid '{field}': {first['msg']}.") from None


class GeneratedEntity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    entity: str
    variant: str
    generation_index: int
    reference_date: str
    marker: str
    values: dict[str, str]


class GeneratedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    type: str
    variant: str
    generation_index: int
    reference_date: str
    value: str


type GeneratedData = GeneratedEntity | GeneratedValue
