import socket

import pytest

from lastro.browser.network import AuxiliaryTransport, NetworkDenied, public_addresses


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:8.8.8.8"]
)
def test_private_dns_denied(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (address, 443))])
    with pytest.raises(NetworkDenied):
        public_addresses("keys.example.com", 443)


def test_mixed_dns_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(NetworkDenied):
        public_addresses("keys.example.com", 443)


def test_auxiliary_origins_are_exact() -> None:
    transport = AuxiliaryTransport(["https://keys.example.com"])
    assert transport.authorize_jwks_origin("https://keys.example.com/jwks")
    for url in [
        "http://keys.example.com/jwks",
        "https://keys.example.com.evil/jwks",
        "https://user@keys.example.com/jwks",
        "https://keys.example.com:444/jwks",
    ]:
        assert not transport.authorize_jwks_origin(url)
