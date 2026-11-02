"""Verificação somente leitura dos artefatos: tamanho e SHA-256 recalculados do disco."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from lastro.history import ArtifactRecord, UnsafeArtifactPathError
from lastro.history.artifacts import file_digest, resolve_inside


class ProblemKind(StrEnum):
    MISSING = "missing"
    MODIFIED = "modified"
    UNSAFE = "unsafe"


@dataclass(frozen=True)
class ArtifactProblem:
    relative_path: str
    expected_sha256: str
    kind: ProblemKind

    @property
    def message(self) -> str:
        if self.kind == ProblemKind.MODIFIED:
            return (
                f"Artifact {self.relative_path} was modified after the run "
                f"(expected {self.expected_sha256})"
            )
        if self.kind == ProblemKind.MISSING:
            return f"Artifact {self.relative_path} is missing (expected {self.expected_sha256})"
        return f"Artifact {self.relative_path} resolves outside the run folder"


@dataclass(frozen=True)
class IntegrityReport:
    checked: int
    problems: tuple[ArtifactProblem, ...]

    @property
    def valid(self) -> bool:
        return not self.problems


def verify_artifacts(run_dir: Path, artifacts: Iterable[ArtifactRecord]) -> IntegrityReport:
    """Nunca grava: nem arquivo, nem banco, nem estado da run."""
    problems: list[ArtifactProblem] = []
    checked = 0
    for artifact in artifacts:
        checked += 1
        try:
            path = resolve_inside(run_dir, artifact.relative_path)
        except UnsafeArtifactPathError:
            problems.append(
                ArtifactProblem(artifact.relative_path, artifact.sha256, ProblemKind.UNSAFE)
            )
            continue
        if not path.is_file():
            problems.append(
                ArtifactProblem(artifact.relative_path, artifact.sha256, ProblemKind.MISSING)
            )
            continue
        size, digest = file_digest(path)
        if size != artifact.size_bytes or digest != artifact.sha256:
            problems.append(
                ArtifactProblem(artifact.relative_path, artifact.sha256, ProblemKind.MODIFIED)
            )
    return IntegrityReport(checked=checked, problems=tuple(problems))
