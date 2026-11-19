"""Chunks de até 800 tokens por seção e janelas de embedding que cobrem todo o chunk.

Chunk nunca cruza documento, heading ou página. O modelo aceita só 512 tokens, então cada chunk
é subdividido em janelas de 480 tokens de conteúdo com overlap de 64: nada do final do chunk é
truncado em silêncio.
"""

import hashlib

from lastro.knowledge.models import JsonObject
from lastro.sources.embeddings import Tokenizer
from lastro.sources.models import (
    CHUNK_OVERLAP,
    CHUNK_TOKENS,
    CHUNKING_VERSION,
    WINDOW_OVERLAP,
    WINDOW_TOKENS,
    ChunkDraft,
    Extracted,
)


def chunking_fingerprint(tokenizer: Tokenizer) -> str:
    raw = (
        f"chunking={CHUNKING_VERSION};size={CHUNK_TOKENS};overlap={CHUNK_OVERLAP};"
        f"window={WINDOW_TOKENS};window_overlap={WINDOW_OVERLAP};tokenizer={tokenizer.fingerprint}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def spans_windows(count: int, size: int, overlap: int) -> list[tuple[int, int]]:
    """Intervalos [início, fim) cobrindo 0..count; o último sempre termina em count."""
    if count <= 0:
        return []
    windows: list[tuple[int, int]] = []
    start = 0
    while True:
        end = min(start + size, count)
        windows.append((start, end))
        if end >= count:
            return windows
        start = end - overlap


def chunk_document(
    document: Extracted, relative_path: str, tokenizer: Tokenizer
) -> list[ChunkDraft]:
    chunks: list[ChunkDraft] = []
    for section in document.sections:
        spans = tokenizer.spans(section.text)
        for start, end in spans_windows(len(spans), CHUNK_TOKENS, CHUNK_OVERLAP):
            char_start = spans[start][0]
            char_end = spans[end - 1][1]
            text = section.text[char_start:char_end]
            locator: JsonObject = {
                "path": relative_path,
                "heading": section.heading,
                "char_start": section.start + char_start,
                "char_end": section.start + char_end,
            }
            if section.line_start is not None:
                locator["line_start"] = section.line_start + section.text.count("\n", 0, char_start)
                locator["line_end"] = section.line_start + section.text.count("\n", 0, char_end)
            if section.page is not None:
                locator["page_start"] = section.page
                locator["page_end"] = section.page
            local = [(left - char_start, right - char_start) for left, right in spans[start:end]]
            windows = tuple(spans_windows(end - start, WINDOW_TOKENS, WINDOW_OVERLAP))
            window_texts = tuple(
                text[local[left][0] : local[right - 1][1]] for left, right in windows
            )
            chunks.append(
                ChunkDraft(
                    ordinal=len(chunks),
                    heading=section.heading,
                    text=text,
                    start=section.start + char_start,
                    end=section.start + char_end,
                    token_count=end - start,
                    locator=locator,
                    content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    windows=windows,
                    window_texts=window_texts,
                )
            )
    return chunks
