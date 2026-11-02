"""Prova de criação reconstruída apenas a partir de registros persistidos e identificados."""

from lastro.config.policy import Origin
from lastro.config.schema import Profile
from lastro.history import (
    UNKNOWN_IDENTIFIER,
    CreatedRecord,
    CreatedRecordInput,
    HistoryStore,
)


class Provenance:
    def __init__(self, history: HistoryStore, run_id: str, profile: Profile) -> None:
        self.history, self.run_id, self.profile = history, run_id, profile
        self.created: set[tuple[str, str]] = set()
        if history.get_active_run(run_id):
            for record in history.list_created_records(run_id=run_id):
                self.remember(record)

    def remember(self, record: CreatedRecord) -> None:
        # Id desconhecido ou tenant divergente nunca concede origem created_in_run.
        if (
            record.identified
            and record.run_id == self.run_id
            and record.tenant == self.profile.tenant_id
            and record.entity_type.strip()
            and record.system_identifier.strip()
            and record.system_identifier != UNKNOWN_IDENTIFIER
        ):
            self.created.add((record.entity_type, record.system_identifier))

    def origin(self, entity: str, record: str) -> Origin:
        if record and (entity, record) in self.created:
            return "created_in_run"
        if any(
            item.tenant == self.profile.tenant_id and item.entity == entity and item.id == record
            for item in self.profile.policy.preauthorized_records
        ):
            return "preauthorized"
        return "unknown"

    def record(self, data: CreatedRecordInput) -> CreatedRecord:
        """O cache em memória só muda depois que registro e evento foram confirmados."""
        created = self.history.record_created(data)
        self.remember(created)
        return created
