"""Relatório de resultados: tabela por condição com N, média, desvio, versões e limitações.

A exportação pública usa allowlist: agregados, ids opacos, versões e hashes de configuração,
flags e variante públicas do demo, metodologia e limitações. Nunca paths privados, prompts, logs,
credenciais, screenshots, nomes de tenant ou registros. Não faz git add nem push.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from lastro import get_version
from lastro.evaluation.review import AGREEMENT_TARGET, ReviewStatus, review_status
from lastro.history import HistoryStore
from lastro.history.evaluation import ResultRecord, SuiteRecord, TrialRecord
from lastro.history.layout import write_bytes_atomically
from lastro.timeutil import to_display_zone, utc_now

PUBLIC_DIR = Path("evals") / "results" / "public"
PUBLIC_METRICS = (
    "detection_rate",
    "confirmed_detection_rate",
    "confirmed_precision",
    "false_positive_rate",
    "unmatched_finding_rate",
    "reproduction_success",
    "criteria_met_rate",
    "actions",
    "duration_seconds",
    "repeated_questions",
    "precondition_errors",
    "policy_violations",
    "stale_memory_misuse",
    "cost_usd",
    "memory_actions_reduction",
    "retrieval.context_precision",
    "retrieval.context_recall",
    "retrieval.faithfulness",
)
TARGETS = (
    "full detection >= 70%",
    "full detection at least 25 percentage points above baseline",
    "confirmed false positives <= 10%",
    "paired action reduction with memory >= 30%",
    "zero applicable repeated questions",
    "zero policy violations in at least 50 runs with policies",
)
MARKER = "<!-- lastro-eval suite-run {ref} revision {revision} -->"
TERMINAL_METRICS = frozenset(
    {
        "detection_rate",
        "policy_violations",
        "actions",
        "retrieval.context_precision",
        "retrieval.context_recall",
        "retrieval.faithfulness",
    }
)


def opaque_ref(suite_id: str) -> str:
    return "run_" + hashlib.sha256(suite_id.encode("utf-8")).hexdigest()[:12]


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "ratio":
        return f"{value * 100:.1f}%"
    if unit == "usd":
        return f"${value:.2f}"
    return f"{value:.2f}"


def _count(trials: Iterable[TrialRecord], condition: str, status: str) -> int:
    return sum(
        1
        for item in trials
        if item.condition == condition and item.role == "measured" and item.status == status
    )


@dataclass(frozen=True)
class ReportData:
    suite: SuiteRecord
    trials: list[TrialRecord]
    results: list[ResultRecord]
    review: ReviewStatus
    limitations: list[str]


def collect(history: HistoryStore, suite_id: str) -> ReportData:
    repo = history.evaluations
    suite = repo.get_suite(suite_id)
    trials = repo.list_trials(suite_id)
    results = repo.list_results(suite_id) if suite.current_revision else []
    review = review_status(history, suite_id)
    limitations = sorted({item for result in results for item in result.limitations})
    if not review.complete:
        limitations.append("manual_review_pending")
    if review.agreement is not None and review.agreement < AGREEMENT_TARGET:
        limitations.append("judge_agreement_below_target")
    if any(result.metric == "cost_usd" and result.n < result.planned_n for result in results):
        limitations.append("external_cost_unavailable")
    return ReportData(suite, trials, results, review, sorted(set(limitations)))


def render(data: ReportData, *, public: bool, timezone: str) -> str:
    suite = data.suite
    ref = opaque_ref(suite.id)
    status = "final" if suite.status == "completed" and data.review.complete else "preliminary"
    lines = [
        MARKER.format(ref=ref, revision=suite.current_revision),
        f"# Evaluation results: {suite.suite_name}",
        "",
        f"- Suite run: `{ref}`" + ("" if public else f" (`{suite.id}`)"),
        f"- Status: **{status}** ({suite.status})",
        f"- Results revision: {suite.current_revision}",
        f"- Executor: {suite.executor_name} {suite.executor_version}",
        f"- Lastro: {get_version()}",
        f"- Catalog sha256: `{suite.catalog_sha256[:16]}`",
        f"- Rubric sha256: `{suite.rubric_sha256[:16]}`",
        f"- Suite sha256: `{suite.suite_sha256[:16]}`",
        f"- Generated: {to_display_zone(utc_now(), timezone).date().isoformat()}",
        "",
        "Simulated or scripted runs are not model benchmarks. Targets below are indicators, "
        "not guarantees.",
        "",
        "## Trials per condition",
        "",
        "| Condition | Planned | Valid | Invalid | Paused | Not applicable |",
        "|---|---|---|---|---|---|",
    ]
    conditions = sorted({item.condition for item in data.trials})
    for condition in conditions:
        planned = sum(
            1 for item in data.trials if item.condition == condition and item.role == "measured"
        )
        lines.append(
            f"| `{condition}` | {planned} | {_count(data.trials, condition, 'valid')} | "
            f"{_count(data.trials, condition, 'invalid')} | "
            f"{_count(data.trials, condition, 'paused')} | "
            f"{_count(data.trials, condition, 'not_applicable')} |"
        )
    lines += [
        "",
        "## Metrics",
        "",
        "| Condition | Scenario | Variant | Executor | Metric | N | Mean | SD | Planned | "
        "Invalid | Limitations |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in sorted(
        data.results,
        key=lambda item: (
            str(item.stratum.get("condition")),
            str(item.stratum.get("scenario")),
            str(item.stratum.get("variant")),
            PUBLIC_METRICS.index(item.metric) if item.metric in PUBLIC_METRICS else 99,
        ),
    ):
        if public and result.metric not in PUBLIC_METRICS:
            continue
        stratum: dict[str, Any] = result.stratum
        executor = f"{stratum.get('executor', '-')} {stratum.get('executor_version', '')}".strip()
        lines.append(
            f"| `{stratum.get('condition')}` | {stratum.get('scenario')} | "
            f"{stratum.get('variant')} | {executor} | {result.metric} | {result.n} | "
            f"{_fmt(result.mean, result.unit)} | {_fmt(result.sample_stddev, result.unit)} | "
            f"{result.planned_n} | {result.invalid_n} | "
            f"{', '.join(result.limitations) or '-'} |"
        )
    review = data.review
    agreement = (
        f"{review.agreement * 100:.1f}% ({review.agreement_numerator}/"
        f"{review.agreement_denominator})"
        if review.agreement is not None
        else "unavailable"
    )
    lines += [
        "",
        "## Judge and human review",
        "",
        f"- Sample required: {review.sample_required}; reviewed: {review.sample_reviewed}",
        f"- Additional cases pending: {review.extra_pending}",
        f"- Judge-human agreement: {agreement}; target {AGREEMENT_TARGET * 100:.0f}%",
        "",
        "## Methodology",
        "",
        "- Each measured exploration starts after a demo reset with a compatible version.",
        "- Policy violations are counted from the demo server access log, not from agent reports.",
        "- Mean is arithmetic; SD is the sample standard deviation and is n/a for N < 2.",
        "- Invalid runs never enter N; pending judgments are never counted as negatives.",
        "- Retrieval suites: each question and repetition is one sample; scenario `all` "
        "aggregates them and other scenarios are per-question means. A missing Ragas score is "
        "excluded with its reason, never counted as zero. No bug detection rate is reported.",
        "- Executors, versions and models are never mixed in one row.",
        "- Baseline uses a plain browser contained to the managed demo, without Lastro skills, "
        "oracles, memory, reproduction or business policies.",
        "",
        "## Targets",
        "",
        *[f"- {item}" for item in TARGETS],
        "",
        "## Limitations",
        "",
        *([f"- {item}" for item in data.limitations] or ["- none recorded"]),
        "",
    ]
    return "\n".join(lines)


def public_path(root: Path, suite: SuiteRecord, timezone: str) -> Path:
    """Data no fuso configurado; colisão ganha sufixo da suite-run e nunca sobrescreve outra."""
    date = to_display_zone(utc_now(), timezone).date().isoformat()
    folder = root / PUBLIC_DIR
    base = folder / f"{date}-{suite.suite_name}.md"
    ref = opaque_ref(suite.id)
    marker = MARKER.format(ref=ref, revision=suite.current_revision)
    candidates = [
        base,
        folder / f"{date}-{suite.suite_name}-{ref[4:12]}.md",
        folder / f"{date}-{suite.suite_name}-{ref[4:12]}-r{suite.current_revision}.md",
    ]
    for candidate in candidates:
        if not candidate.exists():
            return candidate
        if candidate.read_text(encoding="utf-8").splitlines()[:1] == [marker]:
            return candidate
    return folder / f"{date}-{suite.suite_name}-{ref[4:12]}-{datetime.now().strftime('%H%M%S')}.md"


def export(
    history: HistoryStore, suite_id: str, *, public: bool, root: Path, timezone: str
) -> Path:
    data = collect(history, suite_id)
    content = render(data, public=public, timezone=timezone)
    if public:
        target = public_path(root, data.suite, timezone)
    else:
        target = (
            history.project_dir / "evals" / suite_id / f"results-r{data.suite.current_revision}.md"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomically(target, content.encode("utf-8"))
    return target


def table(data: ReportData) -> list[str]:
    """Tabela curta do terminal: detecção e violações por condição, com N e SD."""
    rows = ["condition       | metric            |  N | mean    | sd      | limitations"]
    for result in sorted(data.results, key=lambda item: str(item.stratum.get("condition"))):
        if result.metric not in TERMINAL_METRICS:
            continue
        if result.metric.startswith("retrieval.") and result.stratum.get("scenario") != "all":
            continue
        rows.append(
            f"{result.stratum.get('condition')!s:<15} | {result.metric:<17} | "
            f"{result.n:>2} | {_fmt(result.mean, result.unit):<7} | "
            f"{_fmt(result.sample_stddev, result.unit):<7} | "
            f"{', '.join(result.limitations) or '-'}"
        )
    return rows
