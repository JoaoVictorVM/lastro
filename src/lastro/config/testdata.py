"""Link de detalhe e limpeza manual por entidade criada nas runs."""

from typing import Annotated
from urllib.parse import quote

from pydantic import Field, field_validator

from lastro.config.policy import Strict, safe_path

ID_PLACEHOLDER = "{id}"


class EntityTestData(Strict):
    detail_path: str | None = None
    cleanup_instructions: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    cleanup_required: bool = True

    @field_validator("detail_path")
    @classmethod
    def detail_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.count(ID_PLACEHOLDER) != 1:
            raise ValueError("detail_path requires exactly one {id} placeholder")
        for part in value.split("/"):
            if part != ID_PLACEHOLDER and ("{" in part or "}" in part):
                raise ValueError("detail_path accepts only literal segments and {id}")
        safe_path(value.replace(ID_PLACEHOLDER, "id"))
        return value

    def detail_for(self, identifier: str) -> str | None:
        """Path same-origin do registro, ou nulo quando o id não forma um segmento seguro."""
        if self.detail_path is None or not identifier.strip():
            return None
        path = self.detail_path.replace(ID_PLACEHOLDER, quote(identifier, safe=""))
        try:
            return safe_path(path)
        except ValueError:
            return None


class TestDataSettings(Strict):
    entities: dict[str, EntityTestData] = Field(default_factory=dict[str, EntityTestData])

    def for_entity(self, entity: str) -> EntityTestData:
        return self.entities.get(entity) or EntityTestData()
