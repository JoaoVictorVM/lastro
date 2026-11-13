"""Receptor OTLP local e utilitários dos testes de observabilidade; nenhum serviço externo."""

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

TRACES_PATH = "/api/public/otel/v1/traces"


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes

    def request(self) -> ExportTraceServiceRequest:
        message = ExportTraceServiceRequest()
        message.ParseFromString(self.body)
        return message

    def spans(self) -> list[Any]:
        return [
            span
            for resource in self.request().resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/x-protobuf"
    headers: dict[str, str] = field(default_factory=dict[str, str])
    delay: float = 0.0


@dataclass
class Receiver:
    base: str
    received: list[Received]
    replies: list[Reply]

    def spans(self) -> list[Any]:
        return [span for item in self.received for span in item.spans()]

    def span_attributes(self) -> list[dict[str, Any]]:
        return [attributes(span) for span in self.spans()]


def value_of(value: Any) -> Any:
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [value_of(item) for item in value.array_value.values]
    return getattr(value, kind) if kind else None


def attributes(span: Any) -> dict[str, Any]:
    return {item.key: value_of(item.value) for item in span.attributes}


def partial_reply(rejected: int) -> Reply:
    message = ExportTraceServiceResponse()
    message.partial_success.rejected_spans = rejected
    message.partial_success.error_message = "rejected by backend"
    return Reply(body=message.SerializeToString())


class QuietServer(ThreadingHTTPServer):
    """Sem traceback em stderr: ele vazaria para a saída capturada de outros testes."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        return None


@contextmanager
def otlp_receiver(replies: list[Reply] | None = None) -> Iterator[Receiver]:
    received: list[Received] = []
    queue = list(replies or [])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            received.append(
                Received(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            )
            reply = queue.pop(0) if queue else Reply()
            if reply.delay:
                time.sleep(reply.delay)
            self.send_response(reply.status)
            self.send_header("content-type", reply.content_type)
            for key, value in reply.headers.items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(reply.body)))
            self.end_headers()
            # O cliente pode ter desistido por timeout; a resposta tardia não é erro do teste.
            with contextlib.suppress(ConnectionError):
                self.wfile.write(reply.body)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = QuietServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Receiver(f"http://127.0.0.1:{server.server_port}", received, queue)
    finally:
        server.shutdown()
        server.server_close()


def closed_port_base() -> str:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def wait_until(condition: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()
