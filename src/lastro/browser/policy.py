"""Decisões sem dependência do modelo e bloqueios com motivos independentes."""

import re
import unicodedata
from dataclasses import dataclass, field

from lastro.config.policy import Effect, Operation, Origin
from lastro.config.schema import Profile
from lastro.history.redaction import canonical_json, is_sensitive_key, redact, sha256_hex


@dataclass(frozen=True)
class Action:
    operation: Operation
    entity: str = ""
    record_id: str = ""
    values: dict[str, str] = field(default_factory=dict[str, str])
    method: str = "GET"
    origin: Origin = "unknown"


@dataclass(frozen=True)
class Decision:
    effect: Effect
    reason: str
    fingerprint: str


class PolicyEngine:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile.model_copy(deep=True)
        self.locks: set[str] = set()

    def destructive(self, text: str) -> bool:
        normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
        terms = ["Excluir", "Inativar", "Remover", "Delete", *self.profile.policy.destructive_terms]
        return any(
            re.search(r"(?<!\w)" + re.escape(term.casefold()) + r"(?!\w)", normalized)
            for term in terms
        )

    def decide(self, action: Action, verified: bool) -> Decision:
        fingerprint = sha256_hex(
            canonical_json(
                redact(
                    {
                        "version": 1,
                        "tenant": self.profile.tenant_id,
                        "record": action.record_id,
                        "operation": action.operation,
                        "entity": action.entity,
                        "values": action.values,
                    }
                )
            )
        )

        def result(effect: Effect, reason: str) -> Decision:
            return Decision(effect, reason, fingerprint)

        if action.operation in {"delete", "deactivate", "switch_tenant"}:
            return result("deny", "no-destructive")
        if action.operation != "read":
            if self.locks:
                return result("deny", "write_lock")
            if not verified:
                return result("deny", "tenant_unverified")
            if action.entity not in self.profile.allowed_entities:
                return result("deny", "entity_not_allowed")
            if any(is_sensitive_key(key) for key in action.values):
                return result("deny", "sensitive_field")
            if not set(action.values) <= set(self.profile.editable_fields.get(action.entity, [])):
                return result("deny", "field_not_editable")
            if action.operation == "update" and action.origin == "unknown":
                return result("require_approval", "unconfirmed_origin")
        for rule in self.profile.policy.rules:
            match = rule.match
            if (
                (match.operations is not None and action.operation not in match.operations)
                or (match.entities is not None and action.entity not in match.entities)
                or (match.fields is not None and not set(action.values) <= set(match.fields))
                or (match.record_origins is not None and action.origin not in match.record_origins)
                or (match.methods is not None and action.method not in match.methods)
            ):
                continue
            return result(rule.effect, rule.id)
        return result("allow", "default")
