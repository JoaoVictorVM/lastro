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


def test_pinned_connection_uses_validated_ip_and_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    import ssl
    from typing import Any

    from lastro.browser.network import PinnedHTTPS

    calls: list[object] = []

    class Raw:
        def close(self) -> None:
            calls.append("closed")

    raw = Raw()

    def connect(address: object, timeout: object) -> Raw:
        calls.append(address)
        return raw

    class TLS:
        def wrap_socket(self, stream: object, *, server_hostname: str) -> Any:
            calls.append(server_hostname)
            assert stream is raw
            return raw

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(ssl, "create_default_context", TLS)
    connection = PinnedHTTPS("keys.example.com", 443, "8.8.8.8")
    connection.connect()
    assert calls == [("8.8.8.8", 443), "keys.example.com"]


def test_request_grant_matches_body_and_is_single_use() -> None:
    from lastro.browser.bindings import ExpectedRequest
    from lastro.config.policy import Binding

    binding = Binding.model_validate(
        {
            "id": "x",
            "entity": "employee",
            "operation": "update",
            "route": "/employees/{id}",
            "target": {"label": "Save"},
            "request": {"method": "POST", "path": "/employees/{id}"},
        }
    )
    grant = ExpectedRequest(binding, "https://example.com/employees/1", {"email": "a"}, "action")
    assert not grant.consume("POST", "https://example.com/employees/2", "email=a")
    assert not grant.consume("POST", grant.url, "email=a&role=admin")
    assert not grant.consume("POST", grant.url, "email=b")
    assert grant.consume("POST", grant.url, "email=a")
    assert not grant.consume("POST", grant.url, "email=a")
