from lastro.knowledge.consolidation import ActiveValue, decide, outcome
from lastro.knowledge.facts import identity
from lastro.knowledge.models import (
    ClarificationFact,
    ExperienceFact,
    FieldPropertyFact,
    MemoryOutcomeKind,
    SourceType,
    ValidationState,
    base_state,
)


def required(value: bool) -> FieldPropertyFact:
    return FieldPropertyFact(
        type="field_property", subject="employee", field="cpf", attribute="required", value=value
    )


def test_same_fact_deduplicates_and_merges_evidence() -> None:
    fact = required(True)
    ident = identity(fact)

    decision = decide(fact, ident.value_sha256, [ActiveValue("kn_1", ident.value_sha256)])

    assert decision.kind == "deduplicate" and decision.entry_id == "kn_1"


def test_conflicting_values_contest_both() -> None:
    old = identity(required(True))
    new = required(False)

    decision = decide(new, identity(new).value_sha256, [ActiveValue("kn_1", old.value_sha256)])

    assert decision.kind == "create" and decision.conflicts_with == ("kn_1",)


def test_third_alternative_preserves_conflict_group() -> None:
    fact = FieldPropertyFact(
        type="field_property", subject="employee", field="cpf", attribute="format", value="text"
    )
    actives = [ActiveValue("kn_2", "b" * 64), ActiveValue("kn_1", "a" * 64)]

    decision = decide(fact, identity(fact).value_sha256, actives)

    assert decision.conflicts_with == ("kn_1", "kn_2")


def experience(observation: str, outcome_value: str = "failure") -> ExperienceFact:
    return ExperienceFact.model_validate(
        {
            "type": "experience",
            "attempt": "create",
            "outcome": outcome_value,
            "context": {"route": "/employees/new", "observation_id": observation},
        }
    )


def test_single_failure_is_experience() -> None:
    failure = experience("obs_1")
    success = experience("obs_2", "success")

    assert identity(failure).key != identity(success).key
    decision = decide(failure, identity(failure).value_sha256, [])
    assert decision.kind == "create" and decision.conflicts_with == ()


def test_absence_remains_contextual() -> None:
    absent = experience("obs_1", "not_found_in_context")
    assert identity(absent).key.startswith("experience:obs_1:")


def test_text_ambiguity_requires_review() -> None:
    redacted = ClarificationFact(
        type="clarification", gap_id="g1", subject="employee", answer="[REDACTED]"
    )
    decision = decide(redacted, identity(redacted).value_sha256, [])
    assert decision.kind == "review" and decision.reason is not None


def test_source_types_never_imply_policy_authority() -> None:
    assert base_state(SourceType.USER) == ValidationState.USER_PROVIDED
    for source in (SourceType.OBSERVATION, SourceType.DOCUMENTATION, SourceType.CODE):
        assert base_state(source) == ValidationState.OBSERVED


def test_outcome_precedence() -> None:
    assert outcome(changed=0, pending=0) == MemoryOutcomeKind.NOTHING_NEW
    assert outcome(changed=2, pending=0) == MemoryOutcomeKind.UPDATED
    assert outcome(changed=2, pending=1) == MemoryOutcomeKind.CONSOLIDATION_PENDING
    assert (
        outcome(changed=2, pending=1, persistence_failed=True)
        == MemoryOutcomeKind.PERSISTENCE_FAILED
    )
