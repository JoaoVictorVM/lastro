"""Fontes documentais: ingestão local, recuperação híbrida citável e conflitos entre fontes.

A instalação base importa só contratos; extração, embeddings e busca vetorial carregam os
pacotes do extra `sources` sob demanda.
"""

from lastro.sources.models import (
    Assessment,
    ConflictReceipt,
    ConflictReportInput,
    DocumentFormat,
    FileCounts,
    Passage,
    QueryOutcome,
    ReferenceKind,
    SourceConflict,
    SourcesError,
    SourcesQueryInput,
    SourceStatus,
    SyncResult,
)

__all__ = [
    "Assessment",
    "ConflictReceipt",
    "ConflictReportInput",
    "DocumentFormat",
    "FileCounts",
    "Passage",
    "QueryOutcome",
    "ReferenceKind",
    "SourceConflict",
    "SourceStatus",
    "SourcesError",
    "SourcesQueryInput",
    "SyncResult",
]
