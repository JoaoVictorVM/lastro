"""Limites e sanitização compartilhados pela captura e pela fachada."""

from urllib.parse import urlsplit, urlunsplit

from lastro.history.redaction import preview, redact_text
from lastro.oracles.models import OracleOccurrence


def safe_url(url: str | None) -> tuple[str | None, str]:
    try:
        parts = urlsplit(url or "")
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None, ""
        host = parts.hostname
        if ":" in host:
            host = f"[{host}]"
        origin = host + (f":{parts.port}" if parts.port is not None else "")
        path = redact_text(parts.path or "/")
        return redact_text(urlunsplit((parts.scheme, origin, path, "", ""))), path
    except ValueError:
        return None, ""


def normalize(occurrence: OracleOccurrence) -> OracleOccurrence:
    url, path = safe_url(occurrence.url)
    return OracleOccurrence.model_validate(
        {
            **occurrence.model_dump(),
            "url": url,
            "url_path": path,
            "message": preview(redact_text(occurrence.message), 8000),
            "http_method": redact_text(occurrence.http_method) if occurrence.http_method else None,
        }
    )
