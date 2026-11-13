"""Coordenador da rodada: tentativas sequenciais, parada antecipada, retomada e veredito.

Roda no processo da CLI que o chamou, nunca dentro do browser owner da run explorada. Uma queda
no meio da rodada deixa os findings suspected e as tentativas gravadas; a próxima chamada assume
a rodada aberta e continua só pelas tentativas que faltam.
"""

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from lastro.config.loader import ProjectContext
from lastro.config.reproduction import (
    ReproductionConfigError,
    ReproductionSettings,
    settings_from_snapshot,
)
from lastro.history import (
    REPRODUCTION_CHILD_BLOCK,
    ActiveRunExistsError,
    AttemptDetail,
    AttemptReason,
    EventRecord,
    EventType,
    Finding,
    FindingOrigin,
    HistoryError,
    HistoryStore,
    InvalidMissionError,
    PolicyDecision,
    ReproductionAttempt,
    ReproductionError,
    ReproductionOutcome,
    ReproductionRound,
    ReproductionVerdict,
    RunRecord,
    RunStatus,
    TextMission,
)
from lastro.history.errors import (
    REPRODUCTION_CONFIG_INVALID,
    REPRODUCTION_NOT_ELIGIBLE,
)
from lastro.observability import drain_run
from lastro.reproduction.brief import build_brief
from lastro.reproduction.decide import decide
from lastro.reproduction.interpret import (
    AttemptFacts,
    OracleIdentity,
    authentication_failed,
    interpret,
    oracle_identity,
    policy_signatures,
    signature,
)
from lastro.reproduction.launchers import (
    ExecutorUnavailable,
    ReproducerLauncher,
    default_launchers,
    unavailable_hint,
)
from lastro.reproduction.models import AttemptResult, ReproductionBrief

type Writer = Callable[[str], None]

REPRODUCER_EXITED = "reproducer_exited"
# Bloqueio e pré-condição não mudam ao repetir: as tentativas restantes do finding param.
STOP_REASONS = frozenset({AttemptReason.BLOCKED, AttemptReason.PRECONDITIONS})
SYMBOLS = {"confirmed": "✓", "intermittent": "◐", "not reproduced": "✗"}


def verdict_line(label: str, title: str = "") -> str:
    """Linha do tail e da CLI, nas formas do PRD: ✓, ◐, ✗ ou ?."""
    if label.startswith("confirmed"):
        symbol = SYMBOLS["intermittent"] if "intermittent" in label else SYMBOLS["confirmed"]
    else:
        symbol = SYMBOLS.get(label, "?")
    return f"{symbol} {label}" + (f"  {title}" if title else "")


def started_line(count: int) -> str:
    noun = "finding" if count == 1 else "findings"
    return f"Reproducing {count} suspected {noun}…"


def _ignore(_: str) -> None:
    return None


@dataclass(frozen=True)
class FindingOutcome:
    finding_id: str
    title: str
    verdict: ReproductionVerdict

    @property
    def line(self) -> str:
        line = verdict_line(self.verdict.label, self.title)
        if self.verdict.missing_precondition:
            label = self.verdict.label
            line = line.replace(label, f"{label}: {self.verdict.missing_precondition}", 1)
        return line


@dataclass(frozen=True)
class RoundOutcome:
    run_id: str
    round_id: str | None
    findings: tuple[FindingOutcome, ...]
    message: str | None = None
    report_path: str | None = None


class ReproductionCoordinator:
    def __init__(
        self,
        project: ProjectContext,
        history: HistoryStore,
        *,
        launchers: Mapping[str, ReproducerLauncher] | None = None,
        write: Writer = _ignore,
        build_report: bool = True,
    ) -> None:
        self.project, self.history = project, history
        self.write = write
        self.launchers = launchers if launchers is not None else default_launchers(write)
        self.build_report = build_report
        self._original_events: dict[str, list[EventRecord]] = {}

    def run(
        self, run_id: str, *, finding_id: str | None = None, verifier: str | None = None
    ) -> RoundOutcome:
        record = self.history.get_run(run_id)
        if REPRODUCTION_CHILD_BLOCK in record.config_snapshot:
            raise ReproductionError(
                REPRODUCTION_NOT_ELIGIBLE, f"Run {run_id} is a reproduction attempt."
            )
        if not record.status.is_terminal:
            raise ReproductionError(REPRODUCTION_NOT_ELIGIBLE, f"Run {run_id} is still running.")
        findings = self.history.eligible_findings(run_id)
        if finding_id is not None:
            findings = [item for item in findings if item.id == finding_id]
            if not findings:
                raise ReproductionError(
                    REPRODUCTION_NOT_ELIGIBLE,
                    f"Finding {finding_id} is not a suspected finding of run {run_id} "
                    "without a verdict.",
                )
        if not findings:
            return RoundOutcome(run_id, None, (), message="No suspected findings to reproduce.")
        settings = settings_from_snapshot(
            record.config_snapshot.get("reproduction"), self.project.profile.reproduction
        )
        chosen = self._verifier(settings, record, verifier)
        active = self.history.get_active_run()
        if active is not None:
            raise ActiveRunExistsError(active.id)
        opened = self.history.start_reproduction_round(
            run_id,
            mode=settings.mode,
            explorer=record.executor_name,
            verifier=chosen,
            finding_ids=[item.id for item in findings],
        )
        self.write(started_line(len(findings)))
        outcomes: list[FindingOutcome] = []
        for finding in findings:
            verdict = self.reproduce(finding, opened, settings)
            outcome = FindingOutcome(finding.id, finding.title, verdict)
            self.write(outcome.line)
            outcomes.append(outcome)
        self.history.close_reproduction_round(opened.id)
        # Eventos de reprodução chegam depois do término da run original, na mesma trace.
        drain_run(self.project, self.history, run_id)
        return RoundOutcome(run_id, opened.id, tuple(outcomes), report_path=self._report(record))

    def _verifier(
        self, settings: ReproductionSettings, record: RunRecord, requested: str | None
    ) -> str:
        try:
            chosen = settings.verifier_for(record.executor_name)
        except ReproductionConfigError as error:
            raise ReproductionError(REPRODUCTION_CONFIG_INVALID, str(error)) from None
        if requested is not None:
            if settings.mode != "cross":
                raise ReproductionError(
                    REPRODUCTION_CONFIG_INVALID, "--verifier requires reproduction mode = cross."
                )
            if requested != chosen:
                raise ReproductionError(
                    REPRODUCTION_CONFIG_INVALID,
                    f"Verifier {requested} does not match the configured pair "
                    f"{record.executor_name} -> {chosen}.",
                )
        return chosen

    def reproduce(
        self, finding: Finding, opened: ReproductionRound, settings: ReproductionSettings
    ) -> ReproductionVerdict:
        verifier = opened.verifier_executor
        planned = settings.planned_attempts(finding.intermittent)
        children = {item.ordinal: item for item in self.history.reproduction_children(finding.id)}
        linked = {item.child_run_id for item in children.values()}
        recorded = {
            item.ordinal: item
            for item in finding.reproduction_attempts
            if item.child_run_id in linked
        }
        brief = build_brief(finding, self._entity(finding))
        results: list[AttemptResult] = []
        executor_note: str | None = None
        launcher = self.launchers.get(verifier)
        for ordinal in range(1, planned + 1):
            if ordinal in recorded:
                result = self._from_record(recorded[ordinal], finding)
            elif ordinal in children:
                result = self._settle(finding, children[ordinal].child_run_id, ordinal, verifier)
            elif launcher is None:
                executor_note = unavailable_hint(verifier, "no reproducer adapter.")
                break
            else:
                try:
                    child_id = self._launch(finding, brief, opened, ordinal, launcher)
                except ExecutorUnavailable as error:
                    # Sem binário ou chave nada muda nas tentativas seguintes; sem fallback.
                    executor_note = str(error)
                    break
                except InvalidMissionError as error:
                    results.append(
                        AttemptResult(
                            ordinal,
                            ReproductionOutcome.INCONCLUSIVE,
                            AttemptReason.EVIDENCE,
                            note=f"brief rejected: {error}",
                        )
                    )
                    break
                result = self._settle(finding, child_id, ordinal, verifier)
            results.append(result)
            if result.reason in STOP_REASONS:
                break
        verdict = decide(
            results,
            finding_id=finding.id,
            round_id=opened.id,
            planned=planned,
            explorer=opened.explorer_executor,
            verifier=verifier,
            executor_note=executor_note,
        )
        return self.history.settle_verdict(verdict)

    def _launch(
        self,
        finding: Finding,
        brief: ReproductionBrief,
        opened: ReproductionRound,
        ordinal: int,
        launcher: ReproducerLauncher,
    ) -> str:
        child_project = self.project.with_executor(launcher.executor).for_reproduction()
        prepared = launcher.prepare(child_project)
        created = self.history.create_run(
            child_project,
            TextMission(brief.mission_text()),
            prepared.executor,
            skill_bundle_version=prepared.skill_bundle_version,
            executor_snapshot={
                **prepared.executor_snapshot,
                REPRODUCTION_CHILD_BLOCK: {"brief_sha256": brief.sha256},
            },
            reproduction_of=(opened.id, finding.id, ordinal),
        )
        child_id = created.record.id
        try:
            outcome = launcher.launch(child_project, self.history, created, prepared)
        finally:
            self._ensure_terminal(child_id)
        if not outcome.available:
            raise ExecutorUnavailable(outcome.note)
        return child_id

    def _ensure_terminal(self, child_id: str) -> None:
        """O reprodutor saiu sem encerrar: a run filha termina interrompida, com relatório."""
        record = self.history.get_run(child_id)
        if record.status.is_terminal:
            return
        with contextlib.suppress(HistoryError):
            self.history.seal_criterion_results(child_id)
        self.history.finish_run(child_id, RunStatus.INTERRUPTED, REPRODUCER_EXITED)
        from lastro.reporting.builder import ReportBuilder

        with contextlib.suppress(Exception):
            ReportBuilder(self.history).build_partial(child_id, REPRODUCER_EXITED)

    def _settle(
        self, finding: Finding, child_id: str, ordinal: int, verifier: str
    ) -> AttemptResult:
        self._ensure_terminal(child_id)
        result = interpret(self.facts(finding, child_id, ordinal))
        self.history.record_attempt_detail(
            AttemptDetail(
                finding_id=finding.id,
                child_run_id=child_id,
                ordinal=ordinal,
                outcome=result.outcome,
                reason=result.reason,
                evidence_artifact_ids=result.evidence_artifact_ids,
                note=result.note,
                precondition_index=result.precondition_index,
                executor=verifier,
            )
        )
        return result

    def _from_record(self, attempt: ReproductionAttempt, finding: Finding) -> AttemptResult:
        index = attempt.precondition_index
        missing = (
            finding.preconditions[index]
            if index is not None and 0 <= index < len(finding.preconditions)
            else None
        )
        return AttemptResult(
            ordinal=attempt.ordinal,
            outcome=attempt.outcome,
            reason=attempt.reason,
            evidence_artifact_ids=attempt.evidence_artifact_ids,
            missing_precondition=missing,
            precondition_index=index,
            note=attempt.note,
        )

    def original_events(self, run_id: str) -> list[EventRecord]:
        if run_id not in self._original_events:
            self._original_events[run_id] = self.history.list_events(run_id)
        return self._original_events[run_id]

    def _entity(self, finding: Finding) -> str | None:
        """Entidade da ação citada pelo finding, lida da decisão de política da run explorada."""
        if finding.action_id is None:
            return None
        for event in self.original_events(finding.run_id):
            if (
                event.event_type == EventType.POLICY_DECISION
                and event.correlation_id == finding.action_id
            ):
                item = signature(event.payload, event.tool_name)
                if item is not None and item[2]:
                    return item[2]
        return None

    def _required_oracles(self, finding: Finding) -> frozenset[OracleIdentity] | None:
        if finding.origin != FindingOrigin.ORACLE:
            return None
        signals = self.history.list_oracle_signals(finding.run_id)
        linked = [
            item
            for item in signals
            if finding.action_id is not None
            and finding.action_id
            in (item.first_occurrence.action_id, item.last_occurrence.action_id)
        ]
        return frozenset(oracle_identity(item) for item in (linked or signals))

    def facts(self, finding: Finding, child_id: str, ordinal: int) -> AttemptFacts:
        child = self.history.get_run(child_id)
        events = self.history.list_events(child_id)
        return AttemptFacts(
            ordinal=ordinal,
            preconditions=finding.preconditions,
            child_status=child.status,
            child_reason=child.status_reason,
            report=self.history.reproducer_report(child_id),
            child_artifact_ids=frozenset(item.id for item in self.history.list_artifacts(child_id)),
            auth_failed=authentication_failed(events),
            denied_signatures=frozenset(policy_signatures(events, PolicyDecision.DENY)),
            allowed_signatures=frozenset(
                policy_signatures(self.original_events(finding.run_id), PolicyDecision.ALLOW)
            ),
            required_oracles=self._required_oracles(finding),
            child_oracles=frozenset(
                oracle_identity(item) for item in self.history.list_oracle_signals(child_id)
            ),
        )

    def _report(self, record: RunRecord) -> str | None:
        """Relatório novo da run explorada com os vereditos; o anterior nunca é sobrescrito."""
        if not self.build_report:
            return None
        from lastro.browser.lifecycle import PARTIAL_STATUSES
        from lastro.reporting.builder import ReportBuilder

        builder = ReportBuilder(self.history)
        try:
            current = self.history.get_run(record.id)
            report = (
                builder.build_partial(record.id, current.status_reason or current.status.value)
                if current.status in PARTIAL_STATUSES
                else builder.build_final(record.id)
            )
        except Exception as error:
            self.write(f"! report not rebuilt: {error}")
            return None
        path = report.report_path or report.fallback_path
        return str(path) if path else None
