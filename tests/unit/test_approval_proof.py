import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from lastro.approvals.proof import PREFIX, ProofError, issue, verify

SECRET = "s" * 64
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
FINGERPRINT = "f" * 64


def test_native_proof_is_bound_to_fingerprint_and_nonce() -> None:
    token = issue(SECRET, "act1", FINGERPRINT, NOW, nonce="n" * 32)

    assert token.startswith(PREFIX)
    assert SECRET not in token
    assert verify(SECRET, token, "act1", FINGERPRINT, NOW + timedelta(seconds=59)) == "n" * 32
    for secret, action_id, fingerprint, code in (
        ("x" * 64, "act1", FINGERPRINT, "invalid_signature"),
        (SECRET, "act2", FINGERPRINT, "wrong_request"),
        (SECRET, "act1", "0" * 64, "wrong_fingerprint"),
    ):
        with pytest.raises(ProofError) as error:
            verify(secret, token, action_id, fingerprint, NOW)
        assert error.value.code == code
    with pytest.raises(ProofError) as expired:
        verify(SECRET, token, "act1", FINGERPRINT, NOW + timedelta(seconds=61))
    assert expired.value.code == "expired_proof"


def test_tampered_or_forged_proof_is_rejected() -> None:
    token = issue(SECRET, "act1", FINGERPRINT, NOW)
    encoded, _, mac = token[len(PREFIX) :].rpartition(".")
    claims = json.loads(base64.urlsafe_b64decode(encoded))
    claims["f"] = "0" * 64
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()

    for candidate in (
        PREFIX + forged + "." + mac,
        token[:-1] + ("0" if token[-1] != "0" else "1"),
        "opaque.v1.redacted",
        "approved",
        PREFIX + "." + mac,
        "x" * 3000,
    ):
        with pytest.raises(ProofError):
            verify(SECRET, candidate, "act1", FINGERPRINT, NOW)


def test_each_proof_has_its_own_nonce() -> None:
    first = issue(SECRET, "act1", FINGERPRINT, NOW)
    second = issue(SECRET, "act1", FINGERPRINT, NOW)

    assert first != second
    assert verify(SECRET, first, "act1", FINGERPRINT, NOW) != verify(
        SECRET, second, "act1", FINGERPRINT, NOW
    )
