import pytest

from lastro.change_context import VersionMatch, parse_version_response, version_match

OID = "a" * 40
OTHER = "b" * 40


def test_opaque_version_is_unknown() -> None:
    assert version_match(OID, "v1.2.3", dirty=False) == VersionMatch.UNKNOWN
    assert version_match(OID, "main", dirty=False) == VersionMatch.UNKNOWN
    assert version_match(OID, OID[:12], dirty=False) == VersionMatch.UNKNOWN
    assert version_match(None, OID, dirty=False) == VersionMatch.UNKNOWN
    assert version_match(OID, None, dirty=False) == VersionMatch.UNKNOWN


def test_full_oids_match_or_mismatch() -> None:
    assert version_match(OID, OID, dirty=False) == VersionMatch.MATCHED
    assert version_match(OID, OTHER, dirty=False) == VersionMatch.MISMATCHED


def test_dirty_head_is_not_deployment_proof() -> None:
    assert version_match(OID, OID, dirty=True) == VersionMatch.DIRTY_UNVERIFIED


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f'{{"commit": "{OID.upper()}"}}', OID),
        ('{"version": "1.4.0"}', "1.4.0"),
        (f"{OID}\n", OID),
        ("", None),
        ('{"other": 1}', None),
        ("two\nlines", None),
        ("x" * 300, None),
    ],
)
def test_version_response_parsing(body: str, expected: str | None) -> None:
    assert parse_version_response(body) == expected
