"""Erros públicos do histórico; mensagens prontas para o terminal e sem valores sensíveis."""

HISTORY_UNAVAILABLE_MESSAGE = "Banco de histórico indisponível; nenhuma nova ação será executada."


class HistoryError(Exception):
    pass


class HistoryUnavailableError(HistoryError):
    def __init__(self, message: str = HISTORY_UNAVAILABLE_MESSAGE) -> None:
        super().__init__(message)


class MigrationError(HistoryError):
    def __init__(self, version: int, detail: str | None = None) -> None:
        message = (
            f"A migração {version} do banco falhou; o banco não foi alterado. "
            "Execute 'lastro backup' e reporte o problema."
        )
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.version = version


class InvalidMissionError(HistoryError):
    pass


class InvalidEventError(HistoryError):
    pass


class InvalidTransitionError(HistoryError):
    pass


class RunNotFoundError(HistoryError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"run não encontrada: {run_id}")
        self.run_id = run_id


class RunNotActiveError(HistoryError):
    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(f"a run {run_id} está em estado terminal ({status})")
        self.run_id = run_id


class ActiveRunExistsError(HistoryError):
    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"já existe uma run ativa neste projeto ({run_id}); finalize-a antes de iniciar outra"
        )
        self.run_id = run_id


class UnsafeArtifactPathError(HistoryError):
    pass


class ArtifactExistsError(HistoryError):
    pass


class DiskFullError(HistoryError):
    def __init__(self, path: str) -> None:
        super().__init__(f"Disco cheio em {path}; a evidência não pôde ser salva")
        self.path = path


class BackupError(HistoryError):
    pass


class CreatedRecordNotFoundError(HistoryError):
    def __init__(self, tracking_id: str) -> None:
        super().__init__(f"registro criado não encontrado neste projeto: {tracking_id}")
        self.tracking_id = tracking_id


class CleanupTransitionError(HistoryError):
    pass


class MissionPlanError(HistoryError):
    """Plano, referência ou integridade da missão rejeitados; code é estável para o MCP."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
