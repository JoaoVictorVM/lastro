"""Triggers manual e pós-deploy: missão congelada e somente campos em allowlist do evento.

O evento do GitHub nunca escolhe missão, perfil, executor, configuração, webhook ou credencial:
eles vêm do passo confiável do workflow. Um evento só dispara após conclusão bem-sucedida da
implantação configurada, em branch permitida.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from lastro.autonomous.models import (
    INVALID_INPUT,
    AutonomousError,
    MissionSnapshot,
    TriggerInput,
    TriggerKind,
    sha256_of,
)
from lastro.config.autonomous import GithubTriggers

MAX_EVENT_BYTES = 1024 * 1024
SUPPORTED_EVENTS = ("workflow_run", "deployment_status")


def _map(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _text(value: object, limit: int = 300) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


def manual_trigger(
    profile: str, mission: MissionSnapshot, *, executor: str | None = None, key: str | None = None
) -> TriggerInput:
    """Sem chave explícita, o próprio conteúdo é a chave: repetir o comando não duplica."""
    body = {"profile": profile, "mission": mission.sha256, "executor": executor}
    return TriggerInput(
        kind=TriggerKind.MANUAL,
        key=key or f"manual:{sha256_of(body)[:32]}",
        profile=profile,
        mission=mission,
        payload={},
        executor=executor,
    )


@dataclass(frozen=True)
class GithubEvent:
    """Projeção em allowlist do payload real; o restante do evento é descartado."""

    event: str
    delivery_key: str
    repository: str | None
    workflow: str | None
    conclusion: str | None
    head_sha: str | None
    branch: str | None
    environment: str | None
    run_url: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "delivery_key": self.delivery_key,
            "repository": self.repository,
            "workflow": self.workflow,
            "conclusion": self.conclusion,
            "head_sha": self.head_sha,
            "branch": self.branch,
            "environment": self.environment,
            "run_url": self.run_url,
        }


def read_event_file(
    path: Path, event_name: str, *, environ: Mapping[str, str] | None = None, fixture: bool = False
) -> dict[str, Any]:
    """Só aceita o payload indicado pelo ambiente confiável do Actions, salvo fixture explícita."""
    env = environ if environ is not None else os.environ
    if event_name not in SUPPORTED_EVENTS:
        raise AutonomousError(INVALID_INPUT, f"unsupported event: {event_name}")
    if not fixture:
        if env.get("GITHUB_ACTIONS") != "true":
            raise AutonomousError(
                INVALID_INPUT, "event files are only accepted inside GitHub Actions"
            )
        trusted = env.get("GITHUB_EVENT_PATH")
        if not trusted or Path(trusted).resolve() != path.resolve():
            raise AutonomousError(INVALID_INPUT, "--event-file must be GITHUB_EVENT_PATH")
        if env.get("GITHUB_EVENT_NAME") not in (None, event_name):
            raise AutonomousError(INVALID_INPUT, "--event-name differs from GITHUB_EVENT_NAME")
    try:
        if path.stat().st_size > MAX_EVENT_BYTES:
            raise AutonomousError(INVALID_INPUT, "event payload exceeds 1 MB")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise AutonomousError(INVALID_INPUT, "event payload is not readable JSON") from None
    if not isinstance(data, dict):
        raise AutonomousError(INVALID_INPUT, "event payload must be a JSON object")
    return cast(dict[str, Any], data)


def parse_event(event_name: str, payload: Mapping[str, Any]) -> GithubEvent:
    repository = _text(_map(payload.get("repository")).get("full_name"))
    if event_name == "workflow_run":
        run = _map(payload.get("workflow_run"))
        run_id = _text(run.get("id"))
        attempt = _text(run.get("run_attempt")) or "1"
        if run_id is None:
            raise AutonomousError(INVALID_INPUT, "workflow_run without id")
        return GithubEvent(
            event=event_name,
            delivery_key=f"workflow_run:{run_id}:{attempt}",
            repository=repository,
            workflow=_text(run.get("name")) or _text(_map(payload.get("workflow")).get("name")),
            conclusion=_text(run.get("conclusion"))
            if payload.get("action") == "completed"
            else None,
            head_sha=_text(run.get("head_sha"), 64),
            branch=_text(run.get("head_branch")),
            environment=None,
            run_url=_text(run.get("html_url"), 500),
        )
    if event_name == "deployment_status":
        status = _map(payload.get("deployment_status"))
        deployment = _map(payload.get("deployment"))
        status_id = _text(status.get("id"))
        if status_id is None:
            raise AutonomousError(INVALID_INPUT, "deployment_status without id")
        return GithubEvent(
            event=event_name,
            delivery_key=f"deployment_status:{status_id}",
            repository=repository,
            workflow=None,
            conclusion=_text(status.get("state")),
            head_sha=_text(deployment.get("sha"), 64),
            branch=_text(deployment.get("ref")),
            environment=_text(status.get("environment")) or _text(deployment.get("environment")),
            run_url=_text(status.get("log_url"), 500) or _text(status.get("target_url"), 500),
        )
    raise AutonomousError(INVALID_INPUT, f"unsupported event: {event_name}")


def ignore_reason(event: GithubEvent, settings: GithubTriggers) -> str | None:
    """Motivo para não disparar; None quando a implantação configurada terminou com sucesso."""
    if settings.repository is not None and event.repository != settings.repository:
        return "repository_not_allowed"
    if event.conclusion != "success":
        return "deployment_not_successful"
    if event.branch not in settings.branches:
        return "branch_not_allowed"
    if event.event == "workflow_run":
        if not settings.workflows:
            return "no_deploy_workflow_configured"
        if event.workflow not in settings.workflows:
            return "workflow_not_configured"
    else:
        if not settings.environments:
            return "no_environment_configured"
        if event.environment not in settings.environments:
            return "environment_not_configured"
    return None


def github_trigger(
    event: GithubEvent, profile: str, mission: MissionSnapshot, *, executor: str | None = None
) -> TriggerInput:
    return TriggerInput(
        kind=TriggerKind(event.event),
        key=event.delivery_key,
        profile=profile,
        mission=mission,
        payload=event.as_json(),
        executor=executor,
    )
