"""Tempo: armazenamento sempre em UTC, exibição no fuso configurado."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from lastro.config.schema import DEFAULT_TIMEZONE


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("datetime sem fuso não pode ser convertido para UTC com segurança")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return to_utc_iso(utc_now())


def parse_timestamp(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"timestamp sem fuso: {value}")
    return moment


def to_display_zone(moment: datetime | str, timezone: str = DEFAULT_TIMEZONE) -> datetime:
    parsed = parse_timestamp(moment) if isinstance(moment, str) else moment
    if parsed.tzinfo is None:
        raise ValueError("datetime sem fuso não pode ser exibido com segurança")
    return parsed.astimezone(ZoneInfo(timezone))


def format_for_display(moment: datetime | str, timezone: str = DEFAULT_TIMEZONE) -> str:
    return to_display_zone(moment, timezone).isoformat(timespec="seconds")
