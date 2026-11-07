"""Canal de controle local em diretório privado, sem porta TCP nem tool MCP."""

import asyncio
import contextlib
import json
import secrets
import time
from pathlib import Path
from typing import Any, cast

from lastro.approvals.proof import ProofError, verify
from lastro.auth.storage import create_private_file, ensure_private_directory
from lastro.browser.tools import BrowserTools
from lastro.history import ApprovalChoice, DecidedBy, HistoryError, RunStatus
from lastro.history.approvals import nonce_digest

TERMINATE_OPERATION = "terminate_run"


def write_private(path: Path, data: dict[str, object]) -> None:
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    create_private_file(temporary)
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_message(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.stat().st_size > 8192:
        raise ValueError("invalid_control_message")
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid_control_message")
    return cast(dict[str, Any], data)


def control_directory(root: Path, run_id: str) -> Path:
    return root / ".auth" / ("control-" + run_id)


def owner_secret(root: Path, run_id: str) -> str:
    """Segredo do browser owner, legível só pelo usuário dono do diretório privado."""
    descriptor = read_message(control_directory(root, run_id) / "owner.json")
    secret = descriptor.get("secret")
    if descriptor.get("run_id") != run_id or not isinstance(secret, str):
        raise ValueError("Control belongs to another run")
    return secret


class HumanControl:
    def __init__(self, runtime: BrowserTools) -> None:
        self.runtime = runtime
        self.directory = control_directory(runtime.project.project_data_dir, runtime.run_id)
        self.secret = secrets.token_hex(32)
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        ensure_private_directory(self.directory)
        descriptor = self.directory / "owner.json"
        if descriptor.exists():
            raise ValueError("A browser owner already exists for this run")
        create_private_file(descriptor)
        descriptor.write_text(
            json.dumps({"run_id": self.runtime.run_id, "secret": self.secret}), encoding="utf-8"
        )
        self.task = asyncio.create_task(self.listen())

    async def listen(self) -> None:
        request = self.directory / "request.json"
        while True:
            await asyncio.sleep(0.1)
            if not request.exists():
                continue
            try:
                message = read_message(request)
                request.unlink()
                if not self.authentic(message):
                    continue
                if message["operation"] == TERMINATE_OPERATION:
                    await self.terminate(message)
                    continue
                if set(message) != {"secret", "run_id", "operation", "nonce"} or (
                    message["operation"] != "reverify-tenant"
                ):
                    continue
                async with self.runtime.session.lock:
                    self.runtime.active()
                    if self.runtime.session.context is None:
                        result = "browser_not_started"
                    else:
                        result = (
                            "verified"
                            if await self.runtime.session.verify(recovery=True)
                            else "denied"
                        )
                write_private(
                    self.directory / "response.json", {"nonce": message["nonce"], "result": result}
                )
            except Exception:
                self.runtime.session.policy.locks.add("tenant")

    def authentic(self, message: dict[str, Any]) -> bool:
        secret = message.get("secret")
        return (
            isinstance(secret, str)
            and secrets.compare_digest(secret, self.secret)
            and message.get("run_id") == self.runtime.run_id
            and isinstance(message.get("nonce"), str)
        )

    async def terminate(self, message: dict[str, Any]) -> None:
        """Encerramento confiável do runner nativo: estado e motivo restritos, eco do nonce."""
        from lastro.browser.lifecycle import (
            PARTIAL_STATUSES,
            TRUSTED_REASONS,
            TRUSTED_STATUSES,
            finalize,
        )

        status, reason = message.get("status"), message.get("reason")
        if (
            set(message) != {"secret", "run_id", "operation", "nonce", "status", "reason"}
            or status not in {item.value for item in TRUSTED_STATUSES}
            or reason not in TRUSTED_REASONS
        ):
            response: dict[str, object] = {"nonce": message["nonce"], "error": "invalid_request"}
        else:
            target, cause = RunStatus(status), str(reason)
            self.runtime.monitor.engage()
            if target in PARTIAL_STATUSES:
                # Mesmo latch dos limites: o primeiro motivo confirmado vence.
                with contextlib.suppress(Exception):
                    latch = self.runtime.history.latch_stop(
                        self.runtime.run_id, target, cause, origin="trusted_channel"
                    )
                    if latch.state.stop_status and latch.state.stop_reason:
                        target = RunStatus(latch.state.stop_status)
                        cause = latch.state.stop_reason
            async with self.runtime.session.lock:
                result = await finalize(self.runtime, target, cause, skip_reason=cause)
            response = {
                "nonce": message["nonce"],
                "status": result["status"],
                "already_ended": bool(result.get("already_ended")),
                "report_status": result.get("report_status"),
                "report_kind": result.get("report_kind"),
                "report_path": result.get("report_path"),
            }
            if "capture_error" in result:
                response["capture_error"] = str(result["capture_error"])[:500]
            if "report_error" in result:
                response["report_error"] = str(result["report_error"])[:500]
        write_private(self.directory / "response.json", response)

    def accept_proof(self, action_id: str, token: str) -> bool:
        """Prova do prompt nativo: grava approve do executor uma única vez, nunca por argumento."""
        pending = self.runtime.approvals.pending()
        if pending is None or pending.action_id != action_id:
            return False
        try:
            nonce = verify(
                self.secret, token, pending.action_id, pending.fingerprint, self.runtime.clock()
            )
            self.runtime.history.decide_approval(
                action_id,
                ApprovalChoice.APPROVE,
                DecidedBy.EXECUTOR_PROMPT,
                run_id=self.runtime.run_id,
                proof_nonce_sha256=nonce_digest(nonce),
            )
        except (ProofError, HistoryError):
            return False
        return True

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        for name in ("owner.json", "request.json", "response.json"):
            (self.directory / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self.directory.rmdir()


def request_reverification(root: Path, run_id: str) -> str:
    directory = control_directory(root, run_id)
    secret = owner_secret(root, run_id)
    nonce = secrets.token_hex(16)
    write_private(
        directory / "request.json",
        {
            "secret": secret,
            "run_id": run_id,
            "operation": "reverify-tenant",
            "nonce": nonce,
        },
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = directory / "response.json"
        if response.exists():
            data = read_message(response)
            if data.get("nonce") == nonce:
                response.unlink()
                return str(data["result"])
        time.sleep(0.1)
    raise ValueError("Browser owner unavailable or busy")


def request_termination(
    root: Path, run_id: str, status: str, reason: str, timeout: float = 60.0
) -> dict[str, Any]:
    """Pede ao owner que bloqueie escritas, finalize e gere o relatório; nunca abre browser."""
    directory = control_directory(root, run_id)
    secret = owner_secret(root, run_id)
    nonce = secrets.token_hex(16)
    write_private(
        directory / "request.json",
        {
            "secret": secret,
            "run_id": run_id,
            "operation": TERMINATE_OPERATION,
            "nonce": nonce,
            "status": status,
            "reason": reason,
        },
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = directory / "response.json"
        if response.exists():
            data = read_message(response)
            if data.get("nonce") == nonce:
                response.unlink()
                if "error" in data:
                    raise ValueError(str(data["error"]))
                return data
        time.sleep(0.1)
    raise ValueError("Browser owner unavailable or busy")
