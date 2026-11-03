import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from lastro.cli.app import app
from lastro.history import RunStatus
from lastro.knowledge import QueryInput


def seed_conflict(knowledge: Any) -> tuple[Any, str]:
    history, run_id = knowledge.start()
    observation = knowledge.observe(history, run_id)
    knowledge.propose(history, run_id, observation, value=True)
    knowledge.propose(history, run_id, observation, value=False)
    history.finish_run(run_id, RunStatus.COMPLETED)
    return history, run_id


def invoke(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(app, ["knowledge", *args])


def test_list_highlights_contested_alternatives(runner: CliRunner, knowledge: Any) -> None:
    history, _ = seed_conflict(knowledge)
    first, second = history.list_knowledge()

    result = invoke(runner, "list", "--flow", "employee")
    as_json = json.loads(invoke(runner, "list", "--json").stdout)

    assert result.exit_code == 0, result.output
    assert "contested" in result.stdout
    assert f"{first.id} contestada; alternativas: {second.id}" in result.stdout
    assert {item["validation_state"] for item in as_json["entries"]} == {"contested"}
    assert as_json["total"] == 2
    page = json.loads(invoke(runner, "list", "--json", "--page-size", "1", "--page", "2").stdout)
    assert [item["id"] for item in page["entries"]] == [second.id]
    assert invoke(runner, "list", "--state", "unknown").exit_code == 2
    assert invoke(runner, "list", "--page-size", "101").exit_code == 2


def test_show_returns_history(runner: CliRunner, knowledge: Any) -> None:
    history, run_id = seed_conflict(knowledge)
    entry = history.list_knowledge()[0]

    text = invoke(runner, "show", entry.id)
    data = json.loads(invoke(runner, "show", entry.id, "--json").stdout)

    assert text.exit_code == 0, text.output
    assert entry.statement in text.stdout and "Alternativas contestadas" in text.stdout
    assert [item["operation"] for item in data["history"]] == ["created", "contested"]
    assert data["evidence"][0]["run_id"] == run_id
    assert len(data["alternatives"]) == 1
    assert invoke(runner, "show", "kn_missing").exit_code == 1


def edit_file(tmp_path: Path, value: bool, discard: list[str] | None = None) -> Path:
    path = tmp_path / "edit.json"
    document: dict[str, Any] = {
        "fact": {
            "type": "field_property",
            "subject": "employee",
            "field": "cpf",
            "attribute": "required",
            "value": value,
        }
    }
    if discard:
        document["discard_alternatives"] = discard
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_edit_and_discard_append_history(runner: CliRunner, knowledge: Any, tmp_path: Path) -> None:
    history, _ = seed_conflict(knowledge)
    kept, other = history.list_knowledge()
    value = kept.fact["value"]

    only_text = invoke(
        runner,
        "edit",
        kept.id,
        "--file",
        str(edit_file(tmp_path, bool(value))),
        "--expected-revision",
        str(kept.revision),
        "--reason",
        "Ajuste de texto",
    )
    after_text = history.knowledge_detail(kept.id).entry
    resolved = invoke(
        runner,
        "edit",
        kept.id,
        "--file",
        str(edit_file(tmp_path, bool(value), [other.id])),
        "--expected-revision",
        str(after_text.revision),
        "--reason",
        "Confirmado com o time",
    )

    assert only_text.exit_code == 0, only_text.output
    assert after_text.validation_state == "contested"
    assert resolved.exit_code == 0, resolved.output
    detail = history.knowledge_detail(kept.id)
    assert detail.entry.validation_state == "user_provided"
    assert detail.entry.source_type == "user" and detail.entry.conflict_ids == ()
    edited = [item for item in detail.history if item.operation == "edited"]
    assert edited[-1].payload["before"]["source"] == "user"
    assert edited[0].payload["before"]["source"] == "observation"
    assert edited[-1].actor == "user" and edited[-1].payload["reason"] == "Confirmado com o time"
    discarded = history.knowledge_detail(other.id)
    assert discarded.entry.discarded_at is not None
    assert discarded.history[-1].operation == "discarded"

    current = history.knowledge_detail(kept.id).entry
    gone = invoke(
        runner,
        "discard",
        kept.id,
        "--expected-revision",
        str(current.revision),
        "--reason",
        "Regra removida",
    )
    assert gone.exit_code == 0, gone.output
    assert history.list_knowledge() == []
    assert len(history.list_knowledge(include_discarded=True)) == 2


def test_edit_during_referencing_run_is_deferred(
    runner: CliRunner, knowledge: Any, tmp_path: Path
) -> None:
    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    history.finish_run(run_id, RunStatus.COMPLETED)
    (entry,) = history.list_knowledge()
    reader, active = knowledge.start()
    reader.query_knowledge(active, QueryInput(flow="employee"))

    result = invoke(
        runner,
        "discard",
        entry.id,
        "--expected-revision",
        str(entry.revision),
        "--reason",
        "Obsoleto",
    )

    assert result.exit_code == 0, result.output
    assert "ainda não aplicado" in result.stdout
    assert reader.list_knowledge()[0].revision == entry.revision
    detail = reader.knowledge_detail(entry.id)
    assert detail.history[-1].operation == "mutation_requested"
    reader.finish_run(active, RunStatus.COMPLETED)
    assert reader.list_knowledge() == []
    assert reader.knowledge_detail(entry.id).mutations[0].status == "applied"


def test_stale_deferred_mutation_reports_conflict(
    runner: CliRunner, knowledge: Any, tmp_path: Path
) -> None:
    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    history.finish_run(run_id, RunStatus.COMPLETED)
    (entry,) = history.list_knowledge()
    reader, active = knowledge.start()
    reader.query_knowledge(active, QueryInput(flow="employee"))
    deferred = invoke(
        runner, "discard", entry.id, "--expected-revision", "1", "--reason", "Obsoleto"
    )
    # Atestado imediato não é possível com a run ativa; força nova revisão pelo verificador.
    current = knowledge.observe(reader, active)
    from lastro.knowledge import RevalidateInput

    reader.revalidate_knowledge(
        active,
        RevalidateInput(
            entry_id=entry.id, expected_revision=1, observation_id=current, idempotency_key="v"
        ),
    )
    reader.finish_run(active, RunStatus.COMPLETED)

    assert deferred.exit_code == 0
    detail = reader.knowledge_detail(entry.id)
    assert detail.entry.discarded_at is None and detail.entry.revision == 2
    assert detail.mutations[0].status == "conflict"
    assert detail.history[-1].operation == "mutation_rejected"
    outdated = invoke(
        runner, "discard", entry.id, "--expected-revision", "1", "--reason", "Obsoleto"
    )
    assert outdated.exit_code == 1
    assert "Revisão corrente: 2" in outdated.stderr


def test_human_revalidation_has_user_provenance(runner: CliRunner, knowledge: Any) -> None:
    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    history.finish_run(run_id, RunStatus.COMPLETED)
    (entry,) = history.list_knowledge()

    result = invoke(
        runner,
        "revalidate",
        entry.id,
        "--expected-revision",
        str(entry.revision),
        "--reason",
        "Conferido manualmente na homologação",
        "--json",
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "applied" and data["entry"]["system_version"] is None
    assert data["entry"]["version_unknown"] is True
    last = history.knowledge_detail(entry.id).history[-1]
    assert (last.operation, last.actor, last.run_id) == ("revalidated", "user", None)
    assert last.payload["attested"] is True
    assert invoke(runner, "revalidate", entry.id, "--expected-revision", "2").exit_code == 2


def test_consolidate_retry_is_idempotent(runner: CliRunner, knowledge: Any) -> None:
    history, run_id = knowledge.start()
    knowledge.propose(history, run_id, knowledge.observe(history, run_id))
    history.finish_run(run_id, RunStatus.CANCELLED, "cancelada")

    first = invoke(runner, "consolidate", run_id, "--json")
    second = invoke(runner, "consolidate", run_id, "--json")

    assert first.exit_code == 0, first.output
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert json.loads(first.stdout)["outcome"] == "updated"
    assert len(history.list_consolidation_attempts(run_id)) == 2
    assert invoke(runner, "consolidate", "run_missing").exit_code == 1
