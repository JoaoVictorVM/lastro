import json
from datetime import UTC, datetime, timedelta

from lastro.knowledge.models import JsonObject
from lastro.knowledge.retrieval import (
    NEVER_VERIFIED,
    OLDER_THAN_LIMIT,
    VERSION_CHANGED,
    fit_entries,
    literal_terms,
    match_expression,
    staleness,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def test_stale_after_thirty_days() -> None:
    result = staleness(NOW - timedelta(days=30, seconds=1), NOW, "v1", "v1")
    assert result.stale and result.reasons == (OLDER_THAN_LIMIT,)


def test_exactly_thirty_days_not_stale() -> None:
    result = staleness(NOW - timedelta(days=30), NOW, "v1", "v1")
    assert not result.stale and not result.version_unknown


def test_changed_version_stale() -> None:
    result = staleness(NOW, NOW, "v1", "v2")
    assert result.stale and result.reasons == (VERSION_CHANGED,)


def test_missing_version_requires_revalidation() -> None:
    for entry, current in ((None, "v1"), ("v1", None), (None, None)):
        result = staleness(NOW, NOW, entry, current)
        assert result.version_unknown and not result.stale


def test_unknown_verification_is_stale() -> None:
    result = staleness(None, NOW, "v1", "v1")
    assert result.stale and result.reasons == (NEVER_VERIFIED,)


def test_fts_terms_are_literal() -> None:
    terms = literal_terms('CPF* OR "x" NEAR(a b) NOT -y column:z')
    expression = match_expression(terms)

    assert terms == ["CPF", "OR", "x", "NEAR", "a", "b", "NOT", "y", "column", "z"]
    assert expression is not None
    assert all(part.startswith('"') and part.endswith('"') for part in expression.split(" OR "))
    assert match_expression([]) is None
    assert len(literal_terms(" ".join(f"t{i}" for i in range(50)))) == 20


def entry(index: int, statement: str) -> JsonObject:
    return {
        "id": f"kn_{index}",
        "statement": statement,
        "stale": False,
        "stale_reasons": [],
        "conflict_ids": [],
    }


def test_complete_content_budget_includes_flags_and_unicode() -> None:
    entries = [entry(i, "ç" * 500) for i in range(20)]
    result = fit_entries(entries, limit=10, budget=4000)

    assert len(result.content) <= 4000
    assert json.loads(result.content)["entries"] == list(result.included)
    assert result.omitted == 20 - len(result.included)
    assert len(result.included) < 10

    big = [entry(0, "x" * 5000), entry(1, "short")]
    result = fit_entries(big, limit=10)
    assert [item["id"] for item in result.included] == ["kn_1"]
    assert result.omitted == 1


def test_limit_caps_entry_count() -> None:
    result = fit_entries([entry(i, "s") for i in range(15)], limit=10)
    assert len(result.included) == 10 and result.omitted == 5
