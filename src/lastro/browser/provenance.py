"""Prova mínima de criação compartilhada com consumidores de histórico."""

from lastro.config.policy import Origin
from lastro.config.schema import Profile
from lastro.history import EventType, HistoryStore


class Provenance:
    def __init__(self, history: HistoryStore, run_id: str, profile: Profile) -> None:
        self.history, self.run_id, self.profile = history, run_id, profile
        self.created: set[tuple[str, str]] = set()
        self.actions: dict[str, tuple[str, str]] = {}
        for event in history.list_events(run_id) if history.get_active_run(run_id) else []:
            payload = event.payload
            if event.event_type == EventType.RECORD_CREATED and isinstance(payload, dict):
                entity, record = payload.get("entity"), payload.get("record_id")
                if (
                    payload.get("tenant") == profile.tenant_id
                    and isinstance(entity, str)
                    and isinstance(record, str)
                ):
                    self.created.add((entity, record))
                    action = payload.get("action_id")
                    if isinstance(action, str):
                        self.actions[action] = (entity, record)

    def origin(self, entity: str, record: str) -> Origin:
        if (entity, record) in self.created:
            return "created_in_run"
        if any(
            item.tenant == self.profile.tenant_id and item.entity == entity and item.id == record
            for item in self.profile.policy.preauthorized_records
        ):
            return "preauthorized"
        return "unknown"

    def record(self, entity: str, record: str, action_id: str) -> None:
        if action_id in self.actions:
            if self.actions[action_id] == (entity, record):
                return
            raise ValueError("ambiguous_creation")
        if not record or (entity, record) in self.created:
            raise ValueError("ambiguous_creation")
        self.history.append_event(
            self.run_id,
            EventType.RECORD_CREATED,
            payload={
                "tenant": self.profile.tenant_id,
                "entity": entity,
                "record_id": record,
                "action_id": action_id,
            },
        )
        self.created.add((entity, record))
        self.actions[action_id] = (entity, record)
