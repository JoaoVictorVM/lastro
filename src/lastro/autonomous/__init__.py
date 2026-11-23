"""Modo autônomo: agenda, triggers pós-deploy, fila por perfil, comparação e notificações.

Somente contratos são exportados aqui; coordenador, adapters e CLI são importados sob demanda.
"""

from lastro.autonomous.models import (
    AutonomousError,
    ComparisonResult,
    ComparisonStatus,
    MatchLabel,
    MissionSnapshot,
    NotificationMessage,
    QueueItem,
    QueueStatus,
    TriggerInput,
    TriggerKind,
)

__all__ = [
    "AutonomousError",
    "ComparisonResult",
    "ComparisonStatus",
    "MatchLabel",
    "MissionSnapshot",
    "NotificationMessage",
    "QueueItem",
    "QueueStatus",
    "TriggerInput",
    "TriggerKind",
]
