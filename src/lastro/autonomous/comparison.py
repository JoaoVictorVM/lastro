"""Comparação determinística de achados entre a run atual e a última run comparável.

Algoritmo `finding-match-v1`: candidatos só no mesmo fluxo; assinatura normalizada de passos
igual vence; sem igualdade, Jaccard de tokens com pesos (passos 0,50, observado 0,30, título
0,20), aceito com score >= 0,80 e vantagem >= 0,10 sobre o segundo candidato. Empate ou
ambiguidade vira `new`. Nada aqui lê Markdown: as entradas são achados estruturados do histórico.
"""

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lastro.autonomous.models import (
    NO_BASELINE_MESSAGE,
    ComparisonResult,
    ComparisonStatus,
    MatchLabel,
    MatchRecord,
    QueueItem,
    canonical,
)
from lastro.history import (
    FailureDomain,
    Finding,
    HistoryStore,
    ReportKind,
    RunRecord,
    RunStatus,
    SnapshotStatus,
)

if TYPE_CHECKING:
    from lastro.reporting.models import ComparisonView

ALGORITHM = "finding-match-v1"
WEIGHTS = {"steps": 0.50, "observed": 0.30, "title": 0.20}
THRESHOLD = 0.80
MARGIN = 0.10
EXACT_RULE = "exact_signature"
JACCARD_RULE = "weighted_jaccard"
COMPLETE = "complete"
PARTIAL = "partial"
NONE = "none"
_SPACES = re.compile(r"\s+")
_SELECTOR_MARKS = ("#", "[", "]", "=", ">")


def algorithm_sha256() -> str:
    """Versão, pesos, limiares e regra de normalização entram no fingerprint da comparação."""
    spec = {
        "algorithm": ALGORITHM,
        "weights": WEIGHTS,
        "threshold": THRESHOLD,
        "margin": MARGIN,
        "normalization": "nfkc+casefold+collapse-spaces+strip-punctuation-keep-selectors",
        "scope": "same-flow",
    }
    return hashlib.sha256(canonical(spec).encode("utf-8")).hexdigest()


def _is_selector(token: str) -> bool:
    return any(mark in token for mark in _SELECTOR_MARKS) or bool(
        re.fullmatch(r"\.[a-z][\w-]*", token)
    )


def normalize(text: str) -> str:
    """NFKC, caixa dobrada e espaços colapsados; remove só pontuação, preservando números,
    seletores e a ordem das palavras."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for token in _SPACES.split(folded.strip()):
        if not token:
            continue
        if _is_selector(token):
            # Pontuação de frase grudada no seletor não faz parte dele.
            tokens.append(token.rstrip(",;!?").rstrip(".") or token)
            continue
        kept = "".join(char for char in token if not unicodedata.category(char).startswith("P"))
        if kept:
            tokens.append(kept)
    return " ".join(tokens)


def tokens(texts: Iterable[str]) -> frozenset[str]:
    return frozenset(token for text in texts for token in normalize(text).split(" ") if token)


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


@dataclass(frozen=True)
class Comparable:
    """Projeção estruturada de um finding; a classificação original fica só como detalhe."""

    id: str
    flow: str
    title: str
    steps: tuple[str, ...]
    observed: str
    classification: str = "suspected"

    @property
    def flow_key(self) -> str:
        return normalize(self.flow)

    @property
    def signature(self) -> str:
        text = "\n".join(normalize(step) for step in self.steps)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def score(self, other: "Comparable") -> float:
        value = (
            WEIGHTS["steps"] * jaccard(tokens(self.steps), tokens(other.steps))
            + WEIGHTS["observed"] * jaccard(tokens([self.observed]), tokens([other.observed]))
            + WEIGHTS["title"] * jaccard(tokens([self.title]), tokens([other.title]))
        )
        return round(value, 6)


@dataclass(frozen=True)
class Candidate:
    finding: Comparable
    # True: resolvido na comparação anterior; reaparecer é regressão, não recorrência.
    resolved_before: bool = False


@dataclass(frozen=True)
class _Proposal:
    current: Comparable
    candidate: Candidate
    score: float
    rule: str


def _propose(
    current: Comparable, candidates: Sequence[Candidate]
) -> tuple[_Proposal | None, str, int]:
    pool = [item for item in candidates if item.finding.flow_key == current.flow_key]
    if not pool:
        return None, "no_candidate", 0
    exact = [item for item in pool if item.finding.signature == current.signature]
    if len(exact) == 1:
        return _Proposal(current, exact[0], 1.0, EXACT_RULE), EXACT_RULE, len(pool)
    if len(exact) > 1:
        return None, "ambiguous", len(pool)
    ranked = sorted(
        ((current.score(item.finding), item) for item in pool),
        key=lambda pair: (-pair[0], pair[1].finding.signature, pair[1].finding.id),
    )
    best_score, best = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else None
    if best_score < THRESHOLD:
        return None, "below_threshold", len(pool)
    if second is not None and best_score - second < MARGIN:
        return None, "ambiguous", len(pool)
    return _Proposal(current, best, best_score, JACCARD_RULE), JACCARD_RULE, len(pool)


def match(
    current: Sequence[Comparable],
    candidates: Sequence[Candidate],
    coverage: dict[str, str],
    *,
    current_complete: bool,
) -> tuple[MatchRecord, ...]:
    """Cada finding em no máximo um par; seleção estável por score, assinatura e ids."""
    proposals: list[_Proposal] = []
    unmatched_rule: dict[str, tuple[str, int]] = {}
    for item in current:
        proposal, rule, pool = _propose(item, candidates)
        if proposal is None:
            unmatched_rule[item.id] = (rule, pool)
        else:
            proposals.append(proposal)
            unmatched_rule[item.id] = ("candidate_taken", pool)
    proposals.sort(
        key=lambda p: (-p.score, p.current.signature, p.current.id, p.candidate.finding.id)
    )
    taken: set[str] = set()
    paired: dict[str, _Proposal] = {}
    for proposal in proposals:
        previous = proposal.candidate.finding.id
        if previous in taken or proposal.current.id in paired:
            continue
        taken.add(previous)
        paired[proposal.current.id] = proposal
    records: list[MatchRecord] = []
    for item in current:
        proposal = paired.get(item.id)
        if proposal is not None:
            label = (
                MatchLabel.REGRESSED if proposal.candidate.resolved_before else MatchLabel.RECURRING
            )
            records.append(
                MatchRecord(
                    flow=item.flow,
                    label=label,
                    current_finding_id=item.id,
                    previous_finding_id=proposal.candidate.finding.id,
                    signature_sha256=item.signature,
                    score=proposal.score,
                    match_rule=proposal.rule,
                    detail={"classification": item.classification},
                )
            )
            continue
        rule, pool = unmatched_rule[item.id]
        records.append(
            MatchRecord(
                flow=item.flow,
                label=MatchLabel.NEW,
                current_finding_id=item.id,
                previous_finding_id=None,
                signature_sha256=item.signature,
                score=None,
                match_rule=rule,
                detail={"classification": item.classification, "candidates": pool},
            )
        )
    for candidate in candidates:
        previous = candidate.finding
        if candidate.resolved_before or previous.id in taken:
            continue
        covered = current_complete and coverage.get(previous.flow_key) == COMPLETE
        records.append(
            MatchRecord(
                flow=previous.flow,
                label=MatchLabel.RESOLVED if covered else MatchLabel.NOT_COMPARABLE,
                current_finding_id=None,
                previous_finding_id=previous.id,
                signature_sha256=previous.signature,
                score=None,
                match_rule="absent_in_covered_flow" if covered else "flow_not_covered",
                detail={"classification": previous.classification},
            )
        )
    records.sort(
        key=lambda r: (
            normalize(r.flow),
            r.label.value,
            r.current_finding_id or "",
            r.previous_finding_id or "",
        )
    )
    return tuple(records)


# Leitura do histórico -------------------------------------------------------------------------


def evidence_intact(history: HistoryStore, finding: Finding) -> bool:
    """Evidência ausente ou com hash divergente invalida o achado para matching."""
    run_dir = history.run_dir(finding.run_id)
    for item in finding.evidence:
        path = run_dir / Path(item.relative_path)
        try:
            data = path.read_bytes()
        except OSError:
            return False
        if hashlib.sha256(data).hexdigest() != item.sha256:
            return False
    return True


def comparable(finding: Finding) -> Comparable:
    return Comparable(
        id=finding.id,
        flow=finding.flow,
        title=finding.title,
        steps=finding.steps,
        observed=finding.observed,
        classification=finding.classification.value,
    )


def eligible_findings(history: HistoryStore, run_id: str) -> tuple[list[Finding], list[str]]:
    """Achados do sistema com evidência íntegra; falhas do agente e da tool ficam de fora."""
    eligible: list[Finding] = []
    excluded: list[str] = []
    for finding in history.list_findings(run_id):
        if finding.failure_domain != FailureDomain.SYSTEM:
            continue
        if evidence_intact(history, finding):
            eligible.append(finding)
        else:
            excluded.append(finding.id)
    return eligible, excluded


def evaluated_flows(findings: Sequence[Finding]) -> set[str]:
    """Fluxos com prova de exploração na run: achados registrados nele.

    Escopo do plano e fluxos afetados são intenção ou hipótese, não prova de cobertura.
    """
    flows = {normalize(item.flow) for item in findings}
    flows.discard("")
    return flows


def all_criteria_evaluated(history: HistoryStore, run_id: str) -> bool:
    plan = history.get_active_mission_plan(run_id)
    if plan is None or not plan.criteria:
        return False
    return not any(item.kind == "criterion" for item in history.pending_items(run_id))


def coverage_for(
    history: HistoryStore,
    run: RunRecord,
    findings: Sequence[Finding],
    flows: Iterable[str],
) -> dict[str, str]:
    """Cobertura comprovada por fluxo: só run completed cobre; missão inteira avaliada cobre
    todos os fluxos dela; fora disso, só os fluxos com achado registrado."""
    touched = evaluated_flows(findings)
    if run.status != RunStatus.COMPLETED:
        return {flow: (PARTIAL if flow in touched else NONE) for flow in flows}
    whole = all_criteria_evaluated(history, run.id)
    return {flow: (COMPLETE if whole or flow in touched else NONE) for flow in flows}


MAX_BASELINE_RUNS = 20


@dataclass(frozen=True)
class BaselineRun:
    """Run anterior elegível: completed, relatório final íntegro e mesma missão congelada."""

    record: RunRecord
    findings: tuple[Finding, ...]
    excluded: tuple[str, ...]
    whole: bool

    def covers(self, flow: str) -> bool:
        return self.whole or flow in evaluated_flows(self.findings)


def baseline_runs(history: HistoryStore, item: QueueItem) -> list[BaselineRun]:
    """Mais recentes primeiro; bloqueadas, canceladas ou sem relatório íntegro ficam de fora."""
    runs: list[BaselineRun] = []
    for previous in history.autonomous.baseline_items(item):
        if previous.run_id is None:
            continue
        record = history.get_run(previous.run_id)
        if record.status != RunStatus.COMPLETED:
            continue
        reports = history.list_report_snapshots(record.id, ReportKind.FINAL)
        if not any(
            snapshot.status == SnapshotStatus.READY and snapshot.integrity_valid
            for snapshot in reports
        ):
            continue
        findings, excluded = eligible_findings(history, record.id)
        runs.append(
            BaselineRun(
                record,
                tuple(findings),
                tuple(excluded),
                all_criteria_evaluated(history, record.id),
            )
        )
        if len(runs) >= MAX_BASELINE_RUNS:
            break
    return runs


def resolved_chain(history: HistoryStore, item: QueueItem) -> list[str]:
    """Findings cuja ocorrência mais recente na cadeia é `resolved` e que não voltaram depois."""
    resolved: dict[str, None] = {}
    for previous in reversed(history.autonomous.baseline_items(item)):
        stored = history.autonomous.comparison_for_item(previous.id)
        if stored is None:
            continue
        for record in stored.result.matches:
            if record.previous_finding_id is None:
                continue
            if record.label == MatchLabel.RESOLVED:
                resolved[record.previous_finding_id] = None
            elif record.label == MatchLabel.REGRESSED:
                resolved.pop(record.previous_finding_id, None)
    return list(resolved)


def compare(history: HistoryStore, item: QueueItem, run: RunRecord) -> ComparisonResult:
    """Baseline por fluxo: a run elegível mais recente com cobertura comprovada daquele fluxo."""
    current, excluded = eligible_findings(history, run.id)
    limitations = [
        f"Finding {finding_id} has missing or altered evidence and was not matched."
        for finding_id in excluded
    ]
    previous_runs = baseline_runs(history, item)
    complete = run.status == RunStatus.COMPLETED
    items = [comparable(finding) for finding in current]
    if not previous_runs:
        records = match(items, [], {}, current_complete=False)
        return ComparisonResult(
            status=ComparisonStatus.NO_BASELINE,
            run_id=run.id,
            baseline_run_id=None,
            algorithm=ALGORITHM,
            algorithm_sha256=algorithm_sha256(),
            coverage={},
            matches=tuple(replace(record, match_rule="no_baseline") for record in records),
            limitations=(NO_BASELINE_MESSAGE, *limitations),
        )
    universe = {normalize(finding.flow) for finding in current}
    for previous in previous_runs:
        universe |= evaluated_flows(previous.findings)
    candidates: list[Candidate] = []
    used: list[str] = []
    for flow in sorted(universe):
        source = next((entry for entry in previous_runs if entry.covers(flow)), None)
        if source is None:
            continue
        if source.record.id not in used:
            used.append(source.record.id)
        candidates += [
            Candidate(comparable(finding))
            for finding in source.findings
            if normalize(finding.flow) == flow
        ]
    for previous in previous_runs:
        if previous.record.id in used:
            limitations += [
                f"Baseline finding {finding_id} has missing or altered evidence and was not "
                "matched."
                for finding_id in previous.excluded
            ]
    known = {candidate.finding.id for candidate in candidates}
    for finding_id in resolved_chain(history, item):
        if finding_id in known:
            continue
        try:
            finding = history.get_finding(finding_id)
        except Exception:
            continue
        candidates.append(Candidate(comparable(finding), resolved_before=True))
    flows = sorted({c.finding.flow_key for c in candidates if not c.resolved_before})
    coverage = coverage_for(history, run, current, flows)
    if not complete:
        limitations.append(
            f"The run ended as {run.status.value}; previous findings cannot be marked resolved."
        )
    uncovered = [flow for flow, state in coverage.items() if state != COMPLETE]
    if uncovered and complete:
        limitations.append(
            "Flows without proven coverage are not compared: " + ", ".join(sorted(uncovered))
        )
    records = match(items, candidates, coverage, current_complete=complete)
    # Ordem de `previous_runs` é do mais recente; a baseline exibida é a mais recente usada.
    baseline = next(
        (entry.record.id for entry in previous_runs if entry.record.id in used),
        previous_runs[0].record.id,
    )
    status = ComparisonStatus.COMPARED if complete else ComparisonStatus.NOT_COMPARABLE
    return ComparisonResult(
        status=status,
        run_id=run.id,
        baseline_run_id=baseline,
        algorithm=ALGORITHM,
        algorithm_sha256=algorithm_sha256(),
        coverage=coverage,
        matches=records,
        limitations=tuple(limitations),
    )


def summary_line(result: ComparisonResult) -> str:
    if result.baseline_run_id is None:
        return NO_BASELINE_MESSAGE
    return f"compared with {result.baseline_run_id} ({result.status.value})"


def counts_text(counts: dict[str, Any]) -> str:
    return ", ".join(
        f"{key} {counts.get(key, 0)}" for key in ("new", "regressed", "recurring", "resolved")
    )


def to_view(
    history: HistoryStore,
    result: ComparisonResult,
    trigger: str,
    *,
    skipped_approvals: int = 0,
    clarification_gaps: int = 0,
) -> "ComparisonView":
    """Projeção para o relatório; títulos vêm dos findings estruturados, nunca do Markdown."""
    from lastro.reporting.models import ComparisonItemView, ComparisonView

    titles: dict[str, str] = {}
    for record in result.matches:
        for finding_id in (record.current_finding_id, record.previous_finding_id):
            if finding_id and finding_id not in titles:
                try:
                    titles[finding_id] = history.get_finding(finding_id).title
                except Exception:
                    titles[finding_id] = "(unavailable)"
    counts = result.counts
    items = tuple(
        ComparisonItemView(
            label=record.label.value,
            flow=record.flow,
            finding_id=record.current_finding_id,
            previous_finding_id=record.previous_finding_id,
            title=titles.get(record.current_finding_id or record.previous_finding_id or "", ""),
            score=record.score,
            match_rule=record.match_rule,
        )
        for record in result.matches
    )
    return ComparisonView(
        status=result.status.value,
        trigger=trigger,
        baseline_run_id=result.baseline_run_id,
        algorithm=result.algorithm,
        new=counts["new"],
        regressed=counts["regressed"],
        recurring=counts["recurring"],
        resolved=counts["resolved"],
        not_comparable=sum(1 for r in result.matches if r.label == MatchLabel.NOT_COMPARABLE),
        coverage=tuple(sorted(result.coverage.items())),
        items=items,
        limitations=result.limitations,
        skipped_approvals=skipped_approvals,
        clarification_gaps=clarification_gaps,
    )


def failed_result(run: RunRecord, detail: str) -> ComparisonResult:
    return ComparisonResult(
        status=ComparisonStatus.FAILED,
        run_id=run.id,
        baseline_run_id=None,
        algorithm=ALGORITHM,
        algorithm_sha256=algorithm_sha256(),
        coverage={},
        matches=(),
        limitations=(f"Comparison unavailable: {detail} (comparison_unavailable).",),
    )
