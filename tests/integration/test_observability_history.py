from collections.abc import Callable
from datetime import timedelta
from typing import Any

from lastro.config.loader import ProjectContext
from lastro.history import RunStatus, TextMission, open_history
from lastro.history.events import insert_event, prepare_event
from lastro.observability import settings_source
from lastro.observability.models import ExportOutcome, SpanDescriptor
from lastro.observability.projection import span_id_for
from lastro.observability.worker import ExportWorker

from .obs_support import otlp_receiver


class Recorder:
    def __init__(self, outcome: ExportOutcome | None = None) -> None:
        self.batches: list[list[SpanDescriptor]] = []
        self.outcome = outcome or ExportOutcome("ok")

    def send(self, descriptors: list[SpanDescriptor]) -> ExportOutcome:
        self.batches.append(list(descriptors))
        return self.outcome

    @property
    def span_ids(self) -> list[str]:
        return [item.span_id for batch in self.batches for item in batch]


def worker(project: ProjectContext, history: Any, run_id: str, sender: Any) -> ExportWorker:
    return ExportWorker(history, run_id, settings_source(project), sender_factory=lambda *_: sender)


def test_event_span_bijection_including_internal_writes(
    obs_project: Callable[..., ProjectContext],
) -> None:
    with otlp_receiver() as receiver:
        project = obs_project(receiver.base)
        history: Any = open_history(project)
        run = history.create_run(project, TextMission("Explore employees"))
        run_id = run.record.id
        history.append_event(run_id, "tool_call", tool_name="observe", correlation_id="a1")
        history.append_event(
            run_id,
            "policy_decision",
            tool_name="observe",
            correlation_id="a1",
            policy_decision="allow",
            payload={"reason": "read only"},
        )
        history.finish_run(run_id, RunStatus.COMPLETED)
        # Inserção interna, fora da fachada pública de append_event.
        history._database.write(  # pyright: ignore[reportPrivateUsage]
            lambda conn: insert_event(
                conn,
                run_id,
                prepare_event("reproduction_started", payload={"round_id": "rr_1"}),
                "2026-11-07T13:00:00.000000Z",
            )
        )
        ExportWorker(history, run_id, settings_source(project)).drain()

        events = history.list_events(run_id)
        spans = receiver.span_attributes()

    assert sorted(item["lastro.event.id"] for item in spans) == sorted(item.id for item in events)
    assert len(spans) == len(events)
    roots = [span for span in receiver.spans() if not span.parent_span_id]
    assert len(roots) == 1
    assert attributes_of(roots[0])["lastro.event.type"] == "run_started"
    assert {item["lastro.event.type"] for item in spans} >= {"run_ended", "reproduction_started"}


def attributes_of(span: Any) -> dict[str, Any]:
    from .obs_support import attributes

    return attributes(span)


def test_second_process_observes_native_events(
    obs_project: Callable[..., ProjectContext],
) -> None:
    project = obs_project(executor="native")
    owner = open_history(project)
    run = owner.create_run(project, TextMission("Explore employees"))
    other_process = open_history(project)
    other_process.append_event(run.record.id, "native_started", payload={"model": "m"})
    sender = Recorder()

    worker(project, owner, run.record.id, sender).cycle()

    types = [item.attributes["lastro.event.type"] for item in sender.batches[0]]
    assert types == ["native_started"]


def test_root_materializes_only_after_terminal(
    obs_project: Callable[..., ProjectContext],
) -> None:
    project = obs_project()
    history: Any = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))
    sender = Recorder()
    exporter = worker(project, history, run.record.id, sender)

    exporter.drain()
    assert sender.batches == []
    assert history.observability_status(run.record.id).waiting_end == 1

    history.finish_run(run.record.id, RunStatus.COMPLETED)
    exporter.drain()

    names = [item.attributes["lastro.event.type"] for batch in sender.batches for item in batch]
    assert names == ["run_started", "run_ended"]
    root = sender.batches[0][0]
    assert root.parent_span_id is None
    assert root.end_time == history.get_run(run.record.id).ended_at


def test_post_terminal_reproduction_events_are_projected(
    obs_project: Callable[..., ProjectContext],
) -> None:
    project = obs_project()
    history: Any = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))
    history.finish_run(run.record.id, RunStatus.COMPLETED)
    sender = Recorder()
    worker(project, history, run.record.id, sender).drain()
    exported_root = [
        item for batch in sender.batches for item in batch if item.parent_span_id is None
    ]

    history._database.write(  # pyright: ignore[reportPrivateUsage]
        lambda conn: insert_event(
            conn,
            run.record.id,
            prepare_event(
                "reproduction_verdict", payload={"finding_id": "f_1", "classification": "confirmed"}
            ),
            "2026-11-07T14:00:00.000000Z",
        )
    )
    late = Recorder()
    worker(project, history, run.record.id, late).drain()

    assert len(exported_root) == 1
    assert [item.attributes["lastro.event.type"] for item in late.batches[0]] == [
        "reproduction_verdict"
    ]
    assert late.batches[0][0].trace_id == exported_root[0].trace_id
    assert late.batches[0][0].start_time == "2026-11-07T14:00:00.000000Z"


def test_lease_expiry_recovers_inflight(
    obs_project: Callable[..., ProjectContext], clock_now: list[Any], fake_clock: Any
) -> None:
    project = obs_project()
    history: Any = open_history(project, clock=fake_clock)
    run = history.create_run(project, TextMission("Explore employees"))
    history.append_event(run.record.id, "oracle_signal", payload={"kind": "console"})
    history.project_observability(run.record.id)
    assert history.claim_observability_lease(run.record.id, "token-a")
    assert history.take_observability_batch(run.record.id, "token-a")

    assert not history.claim_observability_lease(run.record.id, "token-b")
    clock_now[0] += timedelta(seconds=16)
    assert history.claim_observability_lease(run.record.id, "token-b")

    states = [state for _, state, _ in history.observability_spans(run.record.id)]
    assert states == ["waiting_end", "pending"]


def test_crash_after_remote_accept_reuses_ids(
    obs_project: Callable[..., ProjectContext], clock_now: list[Any], fake_clock: Any
) -> None:
    project = obs_project()
    history: Any = open_history(project, clock=fake_clock)
    run = history.create_run(project, TextMission("Explore employees"))
    event = history.append_event(run.record.id, "oracle_signal", payload={"kind": "console"})
    history.project_observability(run.record.id)
    history.claim_observability_lease(run.record.id, "crashed")
    first = history.take_observability_batch(run.record.id, "crashed")
    # Remoto aceitou, processo morreu antes do ack; o lease expira.
    clock_now[0] += timedelta(seconds=30)
    sender = Recorder()

    worker(project, history, run.record.id, sender).cycle()

    expected = span_id_for(history.project_id, run.record.id, event.id)
    assert [item.descriptor.span_id for item in first] == [expected]
    assert sender.span_ids == [expected]
    assert sender.batches[0][0].canonical() == first[0].descriptor.canonical()


def test_destination_change_does_not_redirect_pending(
    obs_project: Callable[..., ProjectContext],
) -> None:
    project = obs_project("http://127.0.0.1:3000")
    history: Any = open_history(project)
    run = history.create_run(project, TextMission("Explore employees"))
    history.append_event(run.record.id, "oracle_signal", payload={"kind": "console"})
    moved = obs_project("http://127.0.0.1:3999")
    sender = Recorder()

    worker(moved, history, run.record.id, sender).drain()

    assert sender.batches == []
    status = history.observability_status(run.record.id)
    assert (status.export_status, status.last_error_code) == (
        "suspended",
        "obs_destination_mismatch",
    )
    assert history.observability_context(run.record.id).destination.endpoint.startswith(
        "http://127.0.0.1:3000"
    )
