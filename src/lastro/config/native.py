"""Configuração estrita do executor nativo por perfil; a chave de API nunca fica no arquivo."""

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from lastro.native.pricing import REGISTRY, UnknownModelError, capabilities

NODES = (
    "prepare_mission",
    "consult_memory",
    "observe",
    "decide",
    "act",
    "verify",
    "record",
    "wrap_up",
)
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_OUTPUT_TOKENS = 2048
MIN_OUTPUT_TOKENS = 256
MAX_OUTPUT_TOKENS = 8192
DEFAULT_COST_CAP = "2.00"
DEFAULT_SUITE_COST_CAP = "30.00"
DEFAULT_CONTEXT_LIMIT = 64000
MIN_CONTEXT_LIMIT = 4096
_DECIMAL = re.compile(r"^\d{1,9}(\.\d{1,6})?$")


def _supported_model(value: str) -> str:
    try:
        capabilities(value)
    except UnknownModelError:
        raise PydanticCustomError(
            "lastro_native_model",
            "expected a supported model ({models})",
            {"models": ", ".join(sorted(REGISTRY))},
        ) from None
    return value


def _usd(value: str) -> str:
    if not _DECIMAL.match(value):
        raise PydanticCustomError(
            "lastro_usd", "expected positive decimal text with up to six decimal places"
        )
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise PydanticCustomError("lastro_usd", "expected decimal text") from None
    if amount <= 0:
        raise PydanticCustomError("lastro_usd", "expected a positive amount")
    return value


Model = Annotated[str, AfterValidator(_supported_model)]
Usd = Annotated[str, AfterValidator(_usd)]


class NativeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: Model = DEFAULT_MODEL
    node_models: dict[str, Model] = Field(default_factory=dict[str, str])
    max_output_tokens: int = Field(
        default=DEFAULT_MAX_OUTPUT_TOKENS, ge=MIN_OUTPUT_TOKENS, le=MAX_OUTPUT_TOKENS
    )
    cost_cap_usd: Usd = DEFAULT_COST_CAP
    suite_cost_cap_usd: Usd = DEFAULT_SUITE_COST_CAP
    context_token_limit: int = Field(default=DEFAULT_CONTEXT_LIMIT, ge=MIN_CONTEXT_LIMIT)

    @model_validator(mode="after")
    def _within_capabilities(self) -> Self:
        unknown = sorted(set(self.node_models) - set(NODES))
        if unknown:
            raise PydanticCustomError(
                "lastro_native_node",
                "expected node names among {nodes} (invalid: {names})",
                {"nodes": ", ".join(NODES), "names": ", ".join(unknown)},
            )
        for name in self.models():
            caps = capabilities(name)
            if self.max_output_tokens > caps.max_output_tokens:
                raise PydanticCustomError(
                    "lastro_native_output", "max_output_tokens exceeds the model capability"
                )
            if self.context_token_limit > caps.context_window:
                raise PydanticCustomError(
                    "lastro_native_context", "context_token_limit exceeds the model window"
                )
        return self

    def model_for(self, node: str) -> str:
        return self.node_models.get(node, self.model)

    def models(self) -> set[str]:
        return {self.model, *self.node_models.values()}
