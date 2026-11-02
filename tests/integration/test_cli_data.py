import socket
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from typer.testing import CliRunner

from lastro.browser.events import record_call
from lastro.browser.policy import Decision
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.history import (
    CleanupStatus,
    CreatedRecord,
    CreatedRecordInput,
    HistoryStore,
    TextMission,
    open_history,
)


def create(
    store: HistoryStore,
    context: ProjectContext,
    identifiers: list[str],
    *,
    cleanup_required: bool = True,
) -> list[CreatedRecord]:
    run_id = store.create_run(context, TextMission("create")).record.id
    records: list[CreatedRecord] = []
    for index, identifier in enumerate(identifiers):
        action = f"{run_id}-{index}"
        record_call(store, run_id, action, "submit", {}, Decision("allow", "", ""))
        records.append(
            store.record_created(
                CreatedRecordInput(
                    run_id=run_id,
                    action_id=action,
                    entity="employee",
                    system_identifier=identifier or None,
                    tenant=context.profile.tenant_id,
                    expected_tenant=context.profile.tenant_id,
                    cleanup_required=cleanup_required,
                    detail_path=f"/employees/{identifier}" if identifier else None,
                    cleanup_instructions="Remover pela tela. password=fictional-value",
                )
            )
        )
    store.finish_run(run_id, "completed")
    return records


@pytest.fixture
def store(history_context: ProjectContext, fake_clock: Callable[[], datetime]) -> HistoryStore:
    return open_history(history_context, clock=fake_clock)


def test_pending_lists_records_and_profile_filter(
    runner: CliRunner,
    store: HistoryStore,
    history_context_for: Callable[[str], ProjectContext],
    clock_now: list[datetime],
) -> None:
    homolog = create(store, history_context_for("homolog"), ["41", ""])
    clock_now[0] += timedelta(hours=1)
    local = create(store, history_context_for("local"), ["7"])
    create(store, history_context_for("local"), ["8"], cleanup_required=False)

    result = runner.invoke(app, ["data", "pending"])

    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Registros pendentes de limpeza: 3" in out
    positions = [out.index(record.id) for record in [*homolog, *local]]
    assert positions == sorted(positions)
    assert "employee 41" in out and "employee unknown" in out
    assert history_context_for("homolog").profile.target_url + "/employees/41" in out
    assert "Alertas: identifier_not_extracted" in out
    assert "Limpeza: Remover pela tela." in out
    assert "fictional-value" not in out
    assert "employee 8" not in out
    assert "criado em 2026-10-30T14:32:05-03:00" in out

    filtered = runner.invoke(app, ["data", "pending", "--profile", "local"])
    assert filtered.exit_code == 0
    assert local[0].id in filtered.stdout
    assert homolog[0].id not in filtered.stdout
    globally = runner.invoke(app, ["--profile", "homolog", "data", "pending"])
    assert homolog[0].id in globally.stdout
    assert local[0].id not in globally.stdout


def test_pending_empty_is_success(runner: CliRunner, store: HistoryStore) -> None:
    result = runner.invoke(app, ["data", "pending"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "Nenhum registro pendente de limpeza."


def test_mark_cleaned_uses_tracking_id_and_timestamp(
    runner: CliRunner, store: HistoryStore, history_context: ProjectContext
) -> None:
    (record,) = create(store, history_context, ["41"])

    result = runner.invoke(app, ["data", "mark-cleaned", record.id])

    assert result.exit_code == 0, result.output
    assert f"Registro {record.id} marcado como limpo manualmente" in result.stdout
    (updated,) = store.list_created_records()
    assert updated.cleanup_status == CleanupStatus.CLEANED_MANUALLY
    assert updated.cleaned_at is not None
    assert updated.cleaned_at > record.created_at
    assert runner.invoke(app, ["data", "pending"]).stdout.strip() == (
        "Nenhum registro pendente de limpeza."
    )
    missing = runner.invoke(app, ["data", "mark-cleaned", "created_missing"])
    assert missing.exit_code == 1
    assert "registro criado não encontrado" in missing.stderr
    assert runner.invoke(app, ["data", "mark-cleaned", record.system_identifier]).exit_code == 1


def test_mark_cleaned_is_idempotent(
    runner: CliRunner, store: HistoryStore, history_context: ProjectContext
) -> None:
    (record,) = create(store, history_context, ["41"])
    first = runner.invoke(app, ["data", "mark-cleaned", record.id])
    cleaned_at = store.list_created_records()[0].cleaned_at

    again = runner.invoke(app, ["data", "mark-cleaned", record.id])

    assert first.exit_code == again.exit_code == 0
    assert "já estava marcado como limpo manualmente" in again.stdout
    assert store.list_created_records()[0].cleaned_at == cleaned_at


def test_not_required_cannot_be_marked(
    runner: CliRunner, store: HistoryStore, history_context: ProjectContext
) -> None:
    (record,) = create(store, history_context, ["41"], cleanup_required=False)

    result = runner.invoke(app, ["data", "mark-cleaned", record.id])

    assert result.exit_code == 1
    assert "não exige limpeza" in result.stderr
    (unchanged,) = store.list_created_records()
    assert unchanged.cleanup_status == CleanupStatus.NOT_REQUIRED
    assert unchanged.cleaned_at is None


def test_data_commands_never_issue_http_request(
    runner: CliRunner,
    store: HistoryStore,
    history_context: ProjectContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (record,) = create(store, history_context, ["41"])
    attempts: list[object] = []

    def refuse(self: socket.socket, address: object) -> None:
        attempts.append(address)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)

    assert runner.invoke(app, ["data", "pending"]).exit_code == 0
    assert runner.invoke(app, ["data", "mark-cleaned", record.id]).exit_code == 0
    assert attempts == []
    help_text = runner.invoke(app, ["data", "mark-cleaned", "--help"]).stdout
    for option in ("--url", "--method", "--execute", "--delete"):
        assert option not in help_text
    assert runner.invoke(app, ["data", "mark-cleaned", record.id, "--delete"]).exit_code != 0
