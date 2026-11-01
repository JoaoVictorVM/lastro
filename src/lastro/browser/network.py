"""Transporte auxiliar sem cookies, com conexão vinculada ao endereço DNS validado."""

import asyncio
import http.client
import ipaddress
import json
import socket
import ssl
from typing import cast
from urllib.parse import urljoin, urlsplit

from lastro.config.schema import origin_of

MAX_BODY = 1024 * 1024


class NetworkDenied(ValueError):
    pass


def public_addresses(host: str, port: int) -> list[str]:
    addresses = list(
        dict.fromkeys(
            str(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        )
    )
    if not addresses:
        raise NetworkDenied("empty_dns")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            not ip.is_global
            or ip.is_multicast
            or ip.is_reserved
            or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None)
        ):
            raise NetworkDenied("private_dns")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str) -> None:
        super().__init__(host, port, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class AuxiliaryTransport:
    def __init__(self, origins: list[str]) -> None:
        self.origins = {origin_of(url) for url in origins}

    def authorize_jwks_origin(self, url: str) -> bool:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and not parts.username
            and not parts.password
            and not parts.fragment
            and origin_of(url) in self.origins
        )

    def fetch(self, url: str) -> tuple[int, dict[str, str], bytes]:
        if not self.authorize_jwks_origin(url):
            raise NetworkDenied("auxiliary_origin_denied")
        original = origin_of(url)
        for _ in range(4):
            parts = urlsplit(url)
            if origin_of(url) != original or not self.authorize_jwks_origin(url):
                raise NetworkDenied("redirect_denied")
            host = parts.hostname
            if host is None:
                raise NetworkDenied("missing_host")
            port = parts.port or 443
            addresses = public_addresses(host, port)
            connection = PinnedHTTPS(host, port, addresses[0])
            try:
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                connection.request("GET", path, headers={"Accept": "application/json"})
                response = connection.getresponse()
                headers = {key.lower(): value for key, value in response.getheaders()}
                data = response.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise NetworkDenied("response_too_large")
                if response.status in {301, 302, 303, 307, 308}:
                    location = headers.get("location")
                    if not location:
                        raise NetworkDenied("missing_redirect")
                    url = urljoin(url, location)
                    continue
                return response.status, headers, data
            finally:
                connection.close()
        raise NetworkDenied("too_many_redirects")

    async def fetch_jwks(self, url: str) -> dict[str, object]:
        status, _, body = await asyncio.to_thread(self.fetch, url)
        if not 200 <= status < 300:
            raise NetworkDenied("jwks_http_error")
        data: object = json.loads(body)
        if not isinstance(data, dict):
            raise NetworkDenied("jwks_invalid")
        return cast(dict[str, object], data)
