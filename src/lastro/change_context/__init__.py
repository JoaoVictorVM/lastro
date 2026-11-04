"""Contexto de mudança auditável: issues importadas, coleta Git controlada e versões.

Python controla leitura, filtragem, persistência e proveniência; o agente só recebe o conteúdo
congelado da run pela interface de missão.
"""

from lastro.change_context.models import (
    EXCLUDED_SECRET,
    ChangeContextDraft,
    ChangedFile,
    ComparisonMode,
    Exclusion,
    FileOrigin,
    IssueCapture,
    IssueComment,
    IssueContent,
    Layer,
    PreparedContext,
    RemoteFreshness,
    ScanLevel,
    SecurityNotice,
    SourceFile,
    SourceKind,
    SourceState,
    VersionMatch,
)
from lastro.change_context.versions import is_full_oid, parse_version_response, version_match

__all__ = [
    "EXCLUDED_SECRET",
    "ChangeContextDraft",
    "ChangedFile",
    "ComparisonMode",
    "Exclusion",
    "FileOrigin",
    "IssueCapture",
    "IssueComment",
    "IssueContent",
    "Layer",
    "PreparedContext",
    "RemoteFreshness",
    "ScanLevel",
    "SecurityNotice",
    "SourceFile",
    "SourceKind",
    "SourceState",
    "VersionMatch",
    "is_full_oid",
    "parse_version_response",
    "version_match",
]
