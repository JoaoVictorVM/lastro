"""Ocorrências imutáveis e agregados consultáveis sem sessão de browser."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type SignalType = Literal["js_error", "http_4xx", "http_5xx", "request_failed"]


@dataclass(frozen=True)
class ActionContext:
    action_id: str
    action_sequence: int
    started_at: float


class OracleOccurrence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    occurrence_id: str = Field(min_length=1)
    signal_type: SignalType
    url: str | None
    url_path: str
    http_method: str | None
    http_status: int | None
    message: str = Field(max_length=8000)
    timestamp: str
    action_id: str | None
    action_sequence: int | None = Field(ge=1)

    @field_validator("timestamp")
    @classmethod
    def utc_timestamp(cls, value: str) -> str:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
        if len(value) != 27 or parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
            raise ValueError("expected fixed-width UTC timestamp")
        return value

    @model_validator(mode="after")
    def consistent_metadata(self) -> Self:
        status = self.http_status
        if self.signal_type == "http_4xx":
            valid = status is not None and 400 <= status <= 499 and status not in (401, 403)
        elif self.signal_type == "http_5xx":
            valid = status is not None and 500 <= status <= 599
        else:
            valid = status is None
        if not valid:
            raise ValueError("status does not match signal type")
        if (self.action_id is None) != (self.action_sequence is None):
            raise ValueError("action id and sequence must be provided together")
        return self


@dataclass(frozen=True)
class OracleSignal:
    id: str
    run_id: str
    signal_type: SignalType
    url_path: str
    message: str
    first_seen_at: str
    last_seen_at: str
    occurrence_count: int
    first_occurrence: OracleOccurrence
    last_occurrence: OracleOccurrence
    last_event_id: int
