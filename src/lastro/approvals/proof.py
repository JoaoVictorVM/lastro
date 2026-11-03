"""Prova opaca do prompt nativo: HMAC curto, ligado ao pedido, ao fingerprint e a um nonce.

O segredo vem do canal privado do browser owner e nunca passa pelo modelo. A prova só chega à
tool quando o executor aplica o input atualizado depois da permissão humana.
"""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime
from typing import cast

from lastro.history import JsonObject
from lastro.history.redaction import canonical_json

PREFIX = "opaque.v1."
TTL_SECONDS = 60
MAX_PROOF_LENGTH = 2000


class ProofError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _mac(secret: str, encoded: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), (PREFIX + encoded).encode("ascii"), hashlib.sha256
    ).hexdigest()


def issue(
    secret: str, action_id: str, fingerprint: str, now: datetime, nonce: str | None = None
) -> str:
    claims: JsonObject = {
        "a": action_id,
        "f": fingerprint,
        "d": "approve",
        "n": nonce or secrets.token_hex(16),
        "e": int(now.timestamp()) + TTL_SECONDS,
    }
    encoded = base64.urlsafe_b64encode(canonical_json(claims).encode("utf-8")).decode("ascii")
    return PREFIX + encoded + "." + _mac(secret, encoded)


def verify(secret: str, token: str, action_id: str, fingerprint: str, now: datetime) -> str:
    """Valida assinatura, vínculo e validade; devolve o nonce para o registro de uso único."""
    if len(token) > MAX_PROOF_LENGTH or not token.startswith(PREFIX):
        raise ProofError("malformed_proof")
    encoded, _, mac = token[len(PREFIX) :].rpartition(".")
    if not encoded or not secrets.compare_digest(mac, _mac(secret, encoded)):
        raise ProofError("invalid_signature")
    try:
        decoded: object = json.loads(base64.urlsafe_b64decode(encoded))
    except ValueError:
        raise ProofError("malformed_proof") from None
    if not isinstance(decoded, dict):
        raise ProofError("malformed_proof")
    claims = cast(dict[str, object], decoded)
    if claims.get("d") != "approve":
        raise ProofError("invalid_decision")
    if not isinstance(claims.get("a"), str) or not secrets.compare_digest(
        str(claims["a"]), action_id
    ):
        raise ProofError("wrong_request")
    if not isinstance(claims.get("f"), str) or not secrets.compare_digest(
        str(claims["f"]), fingerprint
    ):
        raise ProofError("wrong_fingerprint")
    expires = claims.get("e")
    if not isinstance(expires, int) or now.timestamp() > expires:
        raise ProofError("expired_proof")
    nonce = claims.get("n")
    if not isinstance(nonce, str) or len(nonce) < 16:
        raise ProofError("malformed_proof")
    return nonce
