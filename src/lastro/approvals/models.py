"""Contratos do gate: o pedido mostrado ao humano, a comparação por campo e as mensagens."""

from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import Field

from lastro.config.policy import Strict
from lastro.history import (
    MAX_APPROVAL_NOTE_LENGTH,
    ApprovalRecord,
    JsonObject,
    JsonValue,
)
from lastro.history.redaction import canonical_json, redact

UNKNOWN_VALUE = "unknown"
# Ordem estável de comparação: o primeiro campo divergente é o que a mensagem informa.
FINGERPRINT_FIELDS = ("tenant", "target", "record", "operation", "fields", "values", "page_state")
PAYLOAD_VERSION = 1
NOTIFICATION_TITLE = "Lastro needs approval"
VERBS = {"update": "edit", "create": "create"}
MAX_TEXT = MAX_APPROVAL_NOTE_LENGTH


def changed_message(field_name: str) -> str:
    return f"The action changed after approval ({field_name}); approval required again."


def approve_command(action_id: str) -> str:
    return f"lastro approve {action_id}"


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: str
    new: str

    def as_json(self) -> JsonObject:
        return {"field": self.field, "old": self.old, "new": self.new}


@dataclass(frozen=True)
class ApprovalAction:
    """Tudo que o humano precisa ver para decidir; `components` define a identidade da ação."""

    tool: str
    run_id: str
    profile: str
    environment_url: str
    tenant: str
    tenant_display: str
    entity: str
    record_id: str
    operation: str
    method: str
    route: str
    target: JsonObject
    origin: str
    changes: tuple[FieldChange, ...]
    components: JsonObject
    fingerprint: str
    reason: str = ""
    known_effects: tuple[str, ...] = field(default_factory=tuple[str, ...])

    @property
    def summary(self) -> str:
        verb = VERBS.get(self.operation, self.operation)
        record = f" #{self.record_id}" if self.record_id else ""
        fields = ", ".join(change.field for change in self.changes) or "no field changes"
        return f"{verb} {self.entity}{record} ({fields}) in {self.tenant_display}"

    def payload(self) -> JsonObject:
        return {
            "version": PAYLOAD_VERSION,
            "tool": self.tool,
            "run_id": self.run_id,
            "environment": {"profile": self.profile, "url": self.environment_url},
            "tenant": {"id": self.tenant, "display": self.tenant_display},
            "record": {"entity": self.entity, "id": self.record_id},
            "action": {
                "operation": self.operation,
                "method": self.method,
                "route": self.route,
                "target": self.target,
                "origin": self.origin,
            },
            "changes": [change.as_json() for change in self.changes],
            "reason": self.reason[:MAX_TEXT],
            "known_effects": [effect[:MAX_TEXT] for effect in self.known_effects],
            "summary": self.summary,
            "fingerprint": self.fingerprint,
            "components": self.components,
        }


def first_difference(approved: JsonValue, current: JsonObject) -> str | None:
    """Campo canônico divergente, comparando as duas versões já redigidas."""
    stored = approved if isinstance(approved, dict) else {}
    fresh = cast(dict[str, Any], redact(current))
    for name in FINGERPRINT_FIELDS:
        if canonical_json(stored.get(name)) != canonical_json(fresh.get(name)):
            return name
    return None


def _text(data: JsonObject, *path: str) -> str:
    value: JsonValue = data
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return str(value) if value is not None else ""


def request_lines(record: ApprovalRecord, expires_display: str) -> list[str]:
    """Pedido completo, igual na CLI, no tail e no prompt nativo do executor."""
    data = record.payload
    target = data.get("action")
    target_json = canonical_json(target.get("target")) if isinstance(target, dict) else "{}"
    lines = [
        NOTIFICATION_TITLE + ": " + record.summary,
        f"Run: {record.run_id}",
        f"Environment: {_text(data, 'environment', 'profile')} "
        f"({_text(data, 'environment', 'url')})",
        f"Tenant: {_text(data, 'tenant', 'display')} ({_text(data, 'tenant', 'id')})",
        f"Record: {_text(data, 'record', 'entity')} #{_text(data, 'record', 'id') or '(new)'}",
        f"Action: {_text(data, 'action', 'operation')} via {_text(data, 'action', 'method')} "
        f"on {_text(data, 'action', 'route')} ({_text(data, 'tool')} {target_json})",
        "Changes:",
    ]
    changes = data.get("changes")
    items = changes if isinstance(changes, list) else []
    for item in items:
        if isinstance(item, dict):
            lines.append(f"  - {item.get('field')}: {item.get('old')} -> {item.get('new')}")
    if not items:
        lines.append("  - none")
    lines.append(f"Reason: {_text(data, 'reason') or '(not given)'}")
    effects = data.get("known_effects")
    lines.append("Known effects:")
    lines += [f"  - {effect}" for effect in effects] if isinstance(effects, list) else []
    lines += [
        f"Fingerprint: {record.fingerprint}",
        f"Expires: {expires_display}",
        "Options: approve | skip | end run",
        f"Command: {approve_command(record.action_id)} [--skip | --end]",
    ]
    return lines


class ApprovalStatusInput(Strict):
    approval_id: str = Field(min_length=1, max_length=200)
    wait_seconds: int = Field(default=30, ge=0, le=30)
