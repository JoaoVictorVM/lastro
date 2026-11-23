"""Fila por perfil: enfileiramento idempotente, lease renovável com fencing e recuperação.

O lease só é tomado de outro owner quando expirou e o processo anterior não existe mais neste
host. A renovação roda numa thread supervisionada; perder o lease marca o keeper e todo efeito
seguinte passa a falhar com `lease_lost`.
"""

import contextlib
import os
import secrets
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lastro.autonomous.models import (
    HEARTBEAT_SECONDS,
    LEASE_LOST,
    LEASE_SECONDS,
    AutonomousError,
    EnqueueResult,
    Lease,
    QueueItem,
    TriggerInput,
)
from lastro.history.autonomous import AutonomousRepository
from lastro.history.database import db_timestamp
from lastro.history.processes import process_exists
from lastro.timeutil import utc_now

type ProcessChecker = Callable[[int], bool]


def owner_identity(clock: Callable[[], datetime] = utc_now) -> dict[str, Any]:
    return {"pid": os.getpid(), "host": socket.gethostname(), "since": db_timestamp(clock())}


@dataclass
class LeaseKeeper:
    """Lease vivo de um worker; a thread de heartbeat renova até `stop`."""

    repository: AutonomousRepository
    lease: Lease
    ttl_seconds: int = LEASE_SECONDS
    interval: float = HEARTBEAT_SECONDS
    lost: threading.Event = field(default_factory=threading.Event)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="autonomous-lease", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.renew()

    def renew(self) -> None:
        try:
            self.lease = self.repository.renew_lease(self.lease, self.ttl_seconds)
        except AutonomousError as exc:
            if exc.code == LEASE_LOST:
                self.lost.set()
                self._stop.set()
            # Falha transitória do banco: a próxima batida tenta de novo antes de expirar.

    def ensure(self) -> Lease:
        """Confere token e geração antes de um efeito externo."""
        if self.lost.is_set():
            raise AutonomousError(LEASE_LOST, f"lease for profile {self.lease.profile} was lost")
        try:
            self.repository.check_lease(self.lease)
        except AutonomousError as exc:
            if exc.code == LEASE_LOST:
                self.lost.set()
            raise
        return self.lease

    def stop(self, *, release: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if release and not self.lost.is_set():
            with contextlib.suppress(AutonomousError):
                self.repository.release_lease(self.lease)


@dataclass
class AutonomousQueue:
    repository: AutonomousRepository
    process_checker: ProcessChecker = process_exists
    host: str = field(default_factory=socket.gethostname)
    clock: Callable[[], datetime] = utc_now
    ttl_seconds: int = LEASE_SECONDS
    heartbeat_seconds: float = HEARTBEAT_SECONDS

    def enqueue(self, trigger: TriggerInput) -> EnqueueResult:
        return self.repository.enqueue(trigger)

    def _owner_alive(self, profile: str) -> bool:
        owner = self.repository.lease_owner(profile)
        if owner is None:
            return False
        if owner.get("host") != self.host:
            # Outro host não é verificável daqui; só a expiração libera o lease.
            return False
        pid = owner.get("pid")
        return isinstance(pid, int) and pid != os.getpid() and self.process_checker(pid)

    def acquire(self, profile: str) -> LeaseKeeper | None:
        """Lease ocupado (ou expirado com owner ainda vivo neste host) devolve None."""
        if self._owner_alive(profile):
            return None
        lease = self.repository.acquire_lease(
            profile, owner_identity(self.clock), self.ttl_seconds, secrets.token_hex(16)
        )
        if lease is None:
            return None
        keeper = LeaseKeeper(
            self.repository, lease, ttl_seconds=self.ttl_seconds, interval=self.heartbeat_seconds
        )
        keeper.start()
        return keeper

    def claim(self, keeper: LeaseKeeper) -> QueueItem | None:
        return self.repository.claim_next(keeper.ensure())
