"""Extração dos quatro formatos aceitos para texto normalizado com seções e localização.

Markdown, texto e HTML são lidos no processo, como dados: nada é executado nem buscado. PDF roda
em subprocesso com tempo e memória limitados, porque o custo de descompactar um PDF não é
previsível pelo tamanho do arquivo.
"""

import hashlib
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any, cast

from lastro.sources.models import (
    EMPTY_DOCUMENT,
    EXTRACTED_CHARACTERS,
    EXTRACTION_FAILED,
    EXTRACTION_MEMORY_BYTES,
    EXTRACTION_TIMEOUT,
    EXTRACTION_TIMEOUT_SECONDS,
    EXTRACTOR_VERSION,
    FACT_FENCE,
    NORMALIZATION_VERSION,
    RESOURCE_LIMIT,
    SOURCES_UNAVAILABLE,
    UNSUPPORTED_ENCODING,
    Declaration,
    DocumentFormat,
    Extracted,
    ExtractionFailure,
    Section,
    SourcesError,
)

PDF_WORKER = "lastro.sources.pdf_worker"
HTML_IGNORED = frozenset({"script", "style", "template", "noscript", "head", "svg", "iframe"})
HTML_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
HTML_BLOCKS = frozenset(
    {
        "p", "div", "li", "tr", "br", "section", "article", "table", "ul", "ol", "pre",
        "blockquote", "header", "footer", "main", "nav", "aside", "dl", "dt", "dd", "figure",
        "figcaption", "form", "fieldset", "legend", "label", "caption", "hr", "address",
    }
)  # fmt: skip
HTML_CELLS = frozenset({"td", "th"})


def extraction_fingerprint() -> str:
    """Muda quando extrator ou normalização mudam: bytes iguais passam a exigir rebuild."""
    return hashlib.sha256(
        f"extractor={EXTRACTOR_VERSION};normalization={NORMALIZATION_VERSION}".encode()
    ).hexdigest()


def _module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        raise SourcesError(
            SOURCES_UNAVAILABLE,
            "Extra sources não instalado. Rode uv sync --extra sources "
            "(ou pip install 'lastro[sources]').",
        ) from None


def decode(data: bytes) -> str:
    """UTF-8 com BOM opcional; sem inferir outro encoding em silêncio."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ExtractionFailure(UNSUPPORTED_ENCODING) from None
    if "\x00" in text:
        raise ExtractionFailure(UNSUPPORTED_ENCODING)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _limited(text: str) -> str:
    if len(text) > EXTRACTED_CHARACTERS:
        raise ExtractionFailure(RESOURCE_LIMIT)
    if not text.strip():
        raise ExtractionFailure(EMPTY_DOCUMENT)
    return text


def _file_name(relative: str) -> str:
    return relative.rsplit("/", 1)[-1]


def extract_text(data: bytes, relative: str) -> Extracted:
    text = _limited(decode(data))
    return Extracted(text, (Section(_file_name(relative), text, 0, line_start=1),))


def extract_markdown(data: bytes, relative: str) -> Extracted:
    """Seções por heading; texto de listas e tabelas fica como está no arquivo.

    Blocos `lastro-fact` saem do texto citável (viram linhas vazias para preservar a
    numeração) e são guardados à parte como declarações do autor.
    """
    text = _limited(decode(data))
    markdown = _module("markdown_it")
    parser = markdown.MarkdownIt("commonmark").enable("table")
    tokens = cast(list[Any], parser.parse(text))
    lines = text.split("\n")
    headings: list[tuple[int, str]] = []
    fences: list[tuple[int, int, str]] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.map is not None:
            inline = tokens[index + 1]
            headings.append((int(token.map[0]), " ".join(str(inline.content).split())))
        elif (
            token.type == "fence"
            and token.map is not None
            and str(token.info).strip().split(" ")[0] == FACT_FENCE
        ):
            fences.append((int(token.map[0]), int(token.map[1]), str(token.content)))
    for start, end, _ in fences:
        for number in range(start, min(end, len(lines))):
            lines[number] = ""
    normalized = "\n".join(lines)
    boundaries = [start for start, _ in headings]
    titles = {start: title for start, title in headings}
    if not boundaries or boundaries[0] != 0:
        boundaries.insert(0, 0)
    sections: list[Section] = []
    offsets = _line_offsets(normalized)
    for position, start in enumerate(boundaries):
        end = boundaries[position + 1] if position + 1 < len(boundaries) else len(lines)
        body = "\n".join(lines[start:end])
        if not body.strip():
            continue
        heading = titles.get(start) or _nearest_heading(headings, start) or _file_name(relative)
        sections.append(Section(heading, body, offsets[start], line_start=start + 1))
    declarations = tuple(
        Declaration(
            heading=_nearest_heading(headings, start) or _file_name(relative),
            body=content,
            line=start + 1,
        )
        for start, _, content in fences
    )
    if not normalized.strip() or not sections:
        raise ExtractionFailure(EMPTY_DOCUMENT)
    return Extracted(normalized, tuple(sections), declarations)


def _nearest_heading(headings: list[tuple[int, str]], line: int) -> str | None:
    found: str | None = None
    for start, title in headings:
        if start <= line:
            found = title
    return found


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for index, char in enumerate(text):
        if char == "\n":
            offsets.append(index + 1)
    return offsets


def _hidden(tag: Any) -> bool:
    attrs = cast(dict[str, Any], tag.attrs or {})
    if "hidden" in attrs:
        return True
    if str(attrs.get("aria-hidden", "")).lower() == "true":
        return True
    style = str(attrs.get("style", "")).replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    return str(attrs.get("type", "")).lower() == "hidden"


def extract_html(data: bytes, relative: str) -> Extracted:
    """Texto estático em ordem aproximada; nunca abre recursos, links ou scripts."""
    source = decode(data)
    if len(source) > EXTRACTED_CHARACTERS:
        raise ExtractionFailure(RESOURCE_LIMIT)
    bs4 = _module("bs4")
    soup = bs4.BeautifulSoup(source, "html.parser")
    comment_type = bs4.Comment
    string_type = bs4.NavigableString
    skipped = (bs4.Declaration, bs4.Doctype, bs4.ProcessingInstruction, bs4.CData)
    blocks: list[tuple[str, list[str]]] = []
    title_tag = soup.find("title")
    default = (
        " ".join(str(title_tag.get_text()).split()) if title_tag is not None else ""
    ) or _file_name(relative)
    current: list[str] = []
    line: list[str] = []
    heading = default

    def flush_line() -> None:
        text = " ".join(" ".join(line).split()).rstrip(" |")
        line.clear()
        if text:
            current.append(text)

    def walk(node: Any) -> None:
        nonlocal heading, current
        for child in list(node.children):
            if isinstance(child, (comment_type, *skipped)):
                continue
            if isinstance(child, string_type):
                line.append(str(child))
                continue
            name = str(child.name or "").lower()
            if name in HTML_IGNORED or _hidden(child):
                continue
            if name in HTML_HEADINGS:
                flush_line()
                if current:
                    blocks.append((heading, current))
                current = []
                heading = " ".join(str(child.get_text(" ")).split()) or default
                current.append(heading)
                continue
            if name in HTML_BLOCKS:
                flush_line()
                walk(child)
                flush_line()
            elif name in HTML_CELLS:
                walk(child)
                line.append(" | ")
            else:
                walk(child)

    body = soup.body if soup.body is not None else soup
    walk(body)
    flush_line()
    if current:
        blocks.append((heading, current))
    return _assemble(blocks)


def _assemble(blocks: list[tuple[str, list[str]]]) -> Extracted:
    sections: list[Section] = []
    parts: list[str] = []
    offset = 0
    for heading, lines in blocks:
        body = "\n".join(lines).strip()
        if not body:
            continue
        if parts:
            parts.append("\n\n")
            offset += 2
        sections.append(Section(heading, body, offset))
        parts.append(body)
        offset += len(body)
    text = "".join(parts)
    _limited(text)
    return Extracted(text, tuple(sections))


def pages_to_document(pages: list[str]) -> Extracted:
    """Uma seção por página física; heading sintético porque PDF não traz estrutura confiável."""
    sections: list[Section] = []
    parts: list[str] = []
    offset = 0
    for number, raw in enumerate(pages, start=1):
        body = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
        if not body:
            continue
        if parts:
            parts.append("\n\n")
            offset += 2
        sections.append(Section(f"Página {number}", body, offset, page=number))
        parts.append(body)
        offset += len(body)
    text = "".join(parts)
    if len(text) > EXTRACTED_CHARACTERS:
        raise ExtractionFailure(RESOURCE_LIMIT)
    return Extracted(text, tuple(sections))


type PdfRunner = Callable[[bytes], list[str]]


def worker_command() -> list[str]:
    """Interpretador isolado (-I): sem PYTHONPATH, site do usuário nem diretório atual."""
    return [sys.executable, "-I", "-m", PDF_WORKER]


def run_pdf_worker(data: bytes) -> list[str]:
    """Executa o extrator isolado; o arquivo original nunca é tocado pelo filho."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env["PYTHONIOENCODING"] = "utf-8"
    command = worker_command()
    limiter = _MemoryLimiter()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            preexec_fn=limiter.preexec if sys.platform != "win32" else None,
        )
    except OSError:
        raise ExtractionFailure(EXTRACTION_FAILED) from None
    try:
        limiter.attach(process)
        try:
            stdout, _ = process.communicate(data, timeout=EXTRACTION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ExtractionFailure(EXTRACTION_TIMEOUT) from None
    finally:
        limiter.close()
    try:
        result = cast(dict[str, Any], json.loads(stdout.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        raise ExtractionFailure(EXTRACTION_FAILED) from None
    error = result.get("error")
    if isinstance(error, str):
        raise ExtractionFailure(error)
    pages = result.get("pages")
    if not isinstance(pages, list):
        raise ExtractionFailure(EXTRACTION_FAILED)
    return [str(page) for page in cast(list[object], pages)]


class _MemoryLimiter:
    """Teto de memória do extrator: RLIMIT_AS no POSIX, Job Object no Windows."""

    def __init__(self) -> None:
        self._job: int | None = None

    @staticmethod
    def preexec() -> None:
        if sys.platform != "win32":
            import resource

            limit = EXTRACTION_MEMORY_BYTES
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        if sys.platform == "win32":
            self._job = _assign_job(process)

    def close(self) -> None:
        if sys.platform == "win32" and self._job is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._job)
            self._job = None


def _assign_job(process: subprocess.Popen[bytes]) -> int | None:
    """Associa o filho a um Job Object com limite de memória e kill ao fechar o handle.

    O filho só lê a entrada depois que o pai a envia, e o pai só envia depois desta chamada;
    sem conseguir aplicar o limite, o filho é morto e a extração falha fechada.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    info = ExtendedLimits()
    # JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    info.BasicLimitInformation.LimitFlags = 0x100 | 0x2000
    info.ProcessMemoryLimit = EXTRACTION_MEMORY_BYTES
    handle = int(getattr(process, "_handle"))  # noqa: B009
    ok = bool(job) and bool(
        kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    )
    ok = ok and bool(kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), handle))
    if not ok:
        process.kill()
        process.communicate()
        if job:
            kernel32.CloseHandle(job)
        raise ExtractionFailure(RESOURCE_LIMIT)
    return int(job)


def extract(
    kind: DocumentFormat, data: bytes, relative: str, pdf: PdfRunner = run_pdf_worker
) -> Extracted:
    """Texto normalizado do documento ou ExtractionFailure com o motivo estável."""
    if kind == DocumentFormat.TEXT:
        return extract_text(data, relative)
    if kind == DocumentFormat.MARKDOWN:
        return extract_markdown(data, relative)
    if kind == DocumentFormat.HTML:
        return extract_html(data, relative)
    if not data:
        raise ExtractionFailure(EMPTY_DOCUMENT)
    document = pages_to_document(pdf(data))
    return document
