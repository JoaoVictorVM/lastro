"""Vídeo WebM sob demanda por rotação controlada do contexto Playwright, limitado a 60 s."""

import asyncio
import contextlib
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from lastro.history import ArtifactType, HistoryStore

MAX_VIDEO_SECONDS = 60
VIDEO_ALREADY_ACTIVE = "video_already_active"
VIDEO_NOT_ACTIVE = "video_not_active"
VIDEO_SESSION_RESTORE_FAILED = "video_session_restore_failed"
TENANT_REVERIFICATION_FAILED = "tenant_reverification_failed"
_SCRATCH_DIR = ".video"


class VideoError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VideoHost(Protocol):
    """Parte da sessão de browser de que o gravador precisa."""

    lock: asyncio.Lock

    async def rotate_context(self, *, record_video_dir: Path | None) -> Path | None: ...

    async def verify(self, *, recovery: bool = False) -> bool: ...


@dataclass(frozen=True)
class Recording:
    id: str
    reason: str
    started: float
    directory: Path


class EvidenceVideoRecorder:
    """Uma captura por vez; parar ou estourar o limite registra o WebM e restaura o contexto."""

    def __init__(
        self,
        host: VideoHost,
        history: HistoryStore,
        run_id: str,
        run_dir: Path,
        *,
        max_seconds: float = MAX_VIDEO_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.host, self.history, self.run_id = host, history, run_id
        self.scratch = run_dir / _SCRATCH_DIR
        self.max_seconds = max_seconds
        self.clock, self.sleep = clock, sleep
        self.active: Recording | None = None
        self.finished: dict[str, dict[str, Any]] = {}
        self.timer: asyncio.Task[None] | None = None

    async def start(self, reason: str) -> dict[str, Any]:
        """Chamado com o lock da sessão; nenhuma outra ação roda durante a troca."""
        if self.active is not None:
            raise VideoError(VIDEO_ALREADY_ACTIVE)
        recording = Recording(
            id="video_" + uuid.uuid4().hex[:16],
            reason=reason,
            started=self.clock(),
            directory=self.scratch / uuid.uuid4().hex,
        )
        recording.directory.mkdir(parents=True)
        try:
            await self.host.rotate_context(record_video_dir=recording.directory)
        except Exception:
            await self._restore_after_failure(recording)
            raise VideoError(VIDEO_SESSION_RESTORE_FAILED) from None
        if not await self.host.verify(recovery=True):
            await self._restore_after_failure(recording)
            raise VideoError(TENANT_REVERIFICATION_FAILED)
        self.active = recording
        self.timer = asyncio.create_task(self._timeout(recording.id))
        return {"recording_id": recording.id, "max_duration_seconds": int(self.max_seconds)}

    async def stop(self, recording_id: str) -> dict[str, Any]:
        if recording_id in self.finished:
            return self.finished[recording_id]
        if self.active is None or self.active.id != recording_id:
            raise VideoError(VIDEO_NOT_ACTIVE)
        if self.timer is not None and self.timer is not asyncio.current_task():
            self.timer.cancel()
        return await self._finish("requested")

    async def close(self) -> None:
        """Encerra a captura pendente antes do fim da run, sem impedir o encerramento."""
        if self.active is not None:
            with contextlib.suppress(Exception):
                await self._finish("run_ended")
        if self.timer is not None:
            self.timer.cancel()

    async def _timeout(self, recording_id: str) -> None:
        await self.sleep(self.max_seconds)
        async with self.host.lock:
            if self.active is not None and self.active.id == recording_id:
                with contextlib.suppress(VideoError):
                    await self._finish("timeout")

    async def _finish(self, stop_reason: str) -> dict[str, Any]:
        recording = self.active
        assert recording is not None
        self.active = None
        duration = min(self.clock() - recording.started, self.max_seconds)
        try:
            try:
                video = await self.host.rotate_context(record_video_dir=None)
            except Exception:
                raise VideoError(VIDEO_SESSION_RESTORE_FAILED) from None
            # A captura só vale como evidência se o contexto restaurado continua no tenant certo.
            if not await self.host.verify(recovery=True):
                raise VideoError(TENANT_REVERIFICATION_FAILED)
            if video is None or not video.is_file():
                raise VideoError(VIDEO_SESSION_RESTORE_FAILED)
            artifact = self.history.register_artifact(
                self.run_id,
                f"evidence/{recording.id}.webm",
                ArtifactType.VIDEO,
                source=video,
            )
        finally:
            shutil.rmtree(recording.directory, ignore_errors=True)
        result: dict[str, Any] = {
            "recording_id": recording.id,
            "artifact_id": artifact.id,
            "sha256": artifact.sha256,
            "duration_seconds": round(duration, 3),
            "stop_reason": stop_reason,
        }
        self.finished[recording.id] = result
        return result

    async def _restore_after_failure(self, recording: Recording) -> None:
        with contextlib.suppress(Exception):
            await self.host.rotate_context(record_video_dir=None)
        shutil.rmtree(recording.directory, ignore_errors=True)
