import socket
from typing import Any

import pytest
from tests.integration.sources_support import CADASTRO, make_pdf

from lastro.sources.extraction import extract, extraction_fingerprint
from lastro.sources.models import (
    EMPTY_DOCUMENT,
    ENCRYPTED_DOCUMENT,
    EXTRACTION_FAILED,
    NO_TEXT_LAYER,
    UNSUPPORTED_ENCODING,
    DocumentFormat,
    ExtractionFailure,
    document_format,
)

pytest.importorskip("markdown_it")
pytest.importorskip("bs4")
pytest.importorskip("pypdf")


def test_extract_four_formats_with_locators() -> None:
    markdown = extract(DocumentFormat.MARKDOWN, CADASTRO.encode(), "docs/cadastro.md")
    headings = [section.heading for section in markdown.sections]
    assert headings == ["Cadastro de funcionário", "Campos obrigatórios", "Permissões"]
    required = markdown.sections[1]
    assert required.line_start == 5
    assert "O email é opcional" in required.text
    assert markdown.text[required.start : required.start + len(required.text)] == required.text
    # O bloco declarado sai do texto citável e é guardado à parte.
    assert "lastro-fact" not in markdown.text
    assert len(markdown.declarations) == 1
    assert markdown.declarations[0].heading == "Campos obrigatórios"

    text = extract(DocumentFormat.TEXT, "﻿Linha um\r\nLinha dois".encode(), "a/notas.txt")
    assert [(item.heading, item.line_start) for item in text.sections] == [("notas.txt", 1)]
    assert text.text == "Linha um\nLinha dois"

    html = extract(
        DocumentFormat.HTML,
        b"<html><head><title>Ajuda</title><style>p{}</style></head><body>"
        b"<p>Intro</p><h2>Regras</h2><ul><li>Email opcional</li></ul>"
        b"<table><tr><td>a</td><td>b</td></tr></table></body></html>",
        "ajuda.html",
    )
    assert [section.heading for section in html.sections] == ["Ajuda", "Regras"]
    assert "Email opcional" in html.sections[1].text
    assert "a | b" in html.sections[1].text

    pdf = extract(DocumentFormat.PDF, make_pdf(["Primeira", "", "Terceira"]), "m.pdf")
    assert [(item.heading, item.page) for item in pdf.sections] == [
        ("Página 1", 1),
        ("Página 3", 3),
    ]


def test_html_never_fetches_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    document = extract(
        DocumentFormat.HTML,
        b'<link rel="stylesheet" href="https://example.test/x.css">'
        b'<img src="https://example.test/a.png"><script src="https://example.test/a.js">'
        b"alert(1)</script><template><p>modelo</p></template><!-- comentario -->"
        b'<p hidden>oculto</p><p aria-hidden="true">aria</p><p style="display: none">css</p>'
        b"<p>Visivel</p>",
        "p.html",
    )
    assert document.text == "Visivel"


def test_pdf_without_text_is_explicit() -> None:
    with pytest.raises(ExtractionFailure) as caught:
        extract(DocumentFormat.PDF, make_pdf(["", ""]), "vazio.pdf")
    assert caught.value.reason == NO_TEXT_LAYER


def test_encrypted_and_malformed_pdf() -> None:
    with pytest.raises(ExtractionFailure) as encrypted:
        extract(DocumentFormat.PDF, make_pdf(["x"], encrypted=True), "c.pdf")
    assert encrypted.value.reason == ENCRYPTED_DOCUMENT
    with pytest.raises(ExtractionFailure) as broken:
        extract(DocumentFormat.PDF, b"%PDF-1.4\nnot really a pdf", "b.pdf")
    assert broken.value.reason == EXTRACTION_FAILED


def test_encoding_and_empty_documents() -> None:
    with pytest.raises(ExtractionFailure) as latin:
        extract(DocumentFormat.TEXT, "ação".encode("latin-1"), "a.txt")
    assert latin.value.reason == UNSUPPORTED_ENCODING
    with pytest.raises(ExtractionFailure) as empty:
        extract(DocumentFormat.MARKDOWN, b"\n  \n", "a.md")
    assert empty.value.reason == EMPTY_DOCUMENT
    with pytest.raises(ExtractionFailure) as nul:
        extract(DocumentFormat.TEXT, b"a\x00b", "a.txt")
    assert nul.value.reason == UNSUPPORTED_ENCODING


def test_eligible_extensions_ignore_case() -> None:
    assert document_format("A/B.MD") == DocumentFormat.MARKDOWN
    assert document_format("x.Markdown") == DocumentFormat.MARKDOWN
    assert document_format("x.HTM") == DocumentFormat.HTML
    assert document_format("x.Pdf") == DocumentFormat.PDF
    assert document_format("x.docx") is None
    assert document_format(".md") is None
    assert len(extraction_fingerprint()) == 64
