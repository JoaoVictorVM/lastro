from datetime import UTC, datetime, timedelta

import pytest

from lastro.timeutil import format_for_display, to_display_zone, to_utc_iso, utc_now_iso


def test_now_is_utc_iso8601() -> None:
    stamp = utc_now_iso()

    assert stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp)
    assert parsed.utcoffset() == timedelta(0)


def test_display_in_configured_zone() -> None:
    moment = to_display_zone("2026-07-01T15:00:00Z")

    assert moment.utcoffset() == timedelta(hours=-3)
    assert format_for_display("2026-07-01T15:00:00Z") == "2026-07-01T12:00:00-03:00"


def test_display_in_custom_zone() -> None:
    moment = to_display_zone(datetime(2026, 1, 15, 12, 0, tzinfo=UTC), "Europe/Lisbon")

    assert moment.utcoffset() == timedelta(0)
    assert format_for_display("2026-07-01T12:00:00Z", "Asia/Tokyo") == "2026-07-01T21:00:00+09:00"


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValueError):
        to_utc_iso(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        to_display_zone("2026-01-01T00:00:00")
