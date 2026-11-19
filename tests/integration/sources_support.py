"""Apoio dos testes de fontes: PDFs mínimos, scanners dublês e projeto com documentos."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lastro.change_context.scanner import ScannerError
from lastro.sources.embeddings import HashingModel

if TYPE_CHECKING:
    from lastro.history import HistoryStore
    from lastro.sources.sync import SourceService


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: Sequence[str], *, encrypted: bool = False) -> bytes:
    """PDF 1.4 com uma linha de texto Helvetica por página; vazio vira página sem texto."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b""]
    objects[2] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    kids: list[int] = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({_escape(text)}) Tj ET".encode("latin-1")
        if not text:
            stream = b""
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        content = len(objects)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>"
            ).encode()
        )
        kids.append(len(objects))
    references = " ".join(f"{kid} 0 R" for kid in kids)
    objects[1] = f"<< /Type /Pages /Kids [{references}] /Count {len(kids)} >>".encode()
    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    encrypt = (
        " /Encrypt << /Filter /Standard /V 1 /R 2 /O (aaaa) /U (bbbb) /P -4 >> /ID [(a) (a)]"
        if encrypted
        else ""
    )
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{encrypt} >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(output)


class CleanScanner:
    """Nenhum achado; conta textos varridos."""

    def __init__(self) -> None:
        self.scanned: list[str] = []

    def findings(self, texts: Sequence[str]) -> list[set[int]]:
        self.scanned.extend(texts)
        return [set() for _ in texts]


class MarkerScanner(CleanScanner):
    """Marca como segredo qualquer linha com o marcador sintético."""

    MARKER = "SEGREDO-SINTETICO"

    def findings(self, texts: Sequence[str]) -> list[set[int]]:
        self.scanned.extend(texts)
        return [
            {number for number, line in enumerate(text.split("\n"), 1) if self.MARKER in line}
            for text in texts
        ]


class BrokenScanner(CleanScanner):
    def findings(self, texts: Sequence[str]) -> list[set[int]]:
        raise ScannerError("scanner quebrado")


def write_docs(root: Path, files: Mapping[str, str | bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8", newline="\n")
    return root


CADASTRO = """# Cadastro de funcionário

Esta página descreve o cadastro de funcionários da demo.

## Campos obrigatórios

- Nome completo e CPF são obrigatórios.
- O email é opcional no cadastro de funcionário.

```lastro-fact
{"flow": "employee.create", "category": "screen_component",
 "fact": {"type": "field_property", "subject": "employee", "field": "email",
          "attribute": "required", "value": false}}
```

## Permissões

Somente administradores podem excluir funcionários.
"""


def make_service(
    store: "HistoryStore",
    *,
    model: HashingModel | None = None,
    scanner: CleanScanner | None = None,
    **options: Any,
) -> "SourceService":
    """Serviço com dublês: hashing no lugar do E5 e scanner configurável."""
    from lastro.sources.conflicts import declared_facts_hook
    from lastro.sources.sync import SourceService

    options.setdefault("publish_hook", declared_facts_hook(store.project_id))
    chosen = model or HashingModel()
    detector = scanner or CleanScanner()
    return SourceService(
        store.sources,
        project_names=("work", store.project_id),
        model_factory=lambda: chosen,
        scanner_factory=lambda: detector,
        **options,
    )


__all__ = [
    "CADASTRO",
    "BrokenScanner",
    "CleanScanner",
    "HashingModel",
    "MarkerScanner",
    "make_pdf",
    "write_docs",
]
