"""Extrator de PDF isolado: lê bytes do stdin e escreve JSON com o texto de cada página.

Roda em processo próprio com teto de memória aplicado pelo pai. Não executa JavaScript, não
extrai anexos e não tenta senha: PDF cifrado é relatado e descartado.
"""

import importlib
import io
import json
import sys
from typing import Any

from lastro.sources.models import (
    ENCRYPTED_DOCUMENT,
    EXTRACTED_CHARACTERS,
    EXTRACTION_FAILED,
    NO_TEXT_LAYER,
    RESOURCE_LIMIT,
    SOURCES_UNAVAILABLE,
)


def pages(data: bytes) -> dict[str, Any]:
    try:
        pypdf: Any = importlib.import_module("pypdf")
    except ImportError:
        return {"error": SOURCES_UNAVAILABLE}
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return {"error": ENCRYPTED_DOCUMENT}
        texts: list[str] = []
        total = 0
        for page in reader.pages:
            text = str(page.extract_text() or "")
            total += len(text)
            if total > EXTRACTED_CHARACTERS:
                return {"error": RESOURCE_LIMIT}
            texts.append(text)
    except MemoryError:
        return {"error": RESOURCE_LIMIT}
    except Exception:
        return {"error": EXTRACTION_FAILED}
    if not any(text.strip() for text in texts):
        return {"error": NO_TEXT_LAYER}
    return {"pages": texts}


def main() -> None:
    data = sys.stdin.buffer.read()
    try:
        result = pages(data)
    except MemoryError:
        result = {"error": RESOURCE_LIMIT}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
