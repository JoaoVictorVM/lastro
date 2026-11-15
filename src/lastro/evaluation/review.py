"""Revisão humana rastreável: amostra congelada, exportação cega e importação append-only.

A amostra é `ceil(0,20 * M)` decisões automáticas `match`/`no_match`, sorteadas sem reposição
com seed gravado e persistidas antes da revisão. Casos `uncertain` e de parse vão para a fila
adicional, fora da concordância. A concordância compara a decisão humana com a decisão
automática original, incluindo o bug quando `match`.
"""

import hashlib
import json
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lastro.evaluation.judge import applicable_entries, judge_input
from lastro.evaluation.metrics import applicable_bugs, load_snapshot
from lastro.evaluation.models import EvalError
from lastro.history import HistoryStore
from lastro.history.evaluation import JudgmentRecord
from lastro.history.layout import write_bytes_atomically

AGREEMENT_TARGET = 0.90
REVIEW_DIR = "review"


@dataclass(frozen=True)
class ReviewStatus:
    frozen: bool
    sample_required: int
    sample_reviewed: int
    extra_pending: int
    agreement: float | None
    agreement_numerator: int
    agreement_denominator: int

    @property
    def complete(self) -> bool:
        return (
            self.frozen and self.sample_reviewed >= self.sample_required and not self.extra_pending
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "sample_required": self.sample_required,
            "sample_reviewed": self.sample_reviewed,
            "extra_pending": self.extra_pending,
            "agreement": self.agreement,
            "agreement_numerator": self.agreement_numerator,
            "agreement_denominator": self.agreement_denominator,
            "agreement_target": AGREEMENT_TARGET,
        }


def sample_size(decisions: int, fraction: float) -> int:
    return math.ceil(round(fraction * decisions, 9))


def select_sample(judgment_ids: list[str], fraction: float, seed: int) -> list[str]:
    ordered = sorted(judgment_ids)
    size = min(len(ordered), sample_size(len(ordered), fraction))
    return sorted(random.Random(seed).sample(ordered, size))


def _automatic(judgments: list[JudgmentRecord]) -> list[JudgmentRecord]:
    return [
        item
        for item in judgments
        if item.status == "judged" and item.decision in {"match", "no_match"}
    ]


def ensure_sample(history: HistoryStore, suite_id: str) -> ReviewStatus:
    """Congela a amostra quando nenhum julgamento automático está em andamento."""
    repo = history.evaluations
    if repo.review_items(suite_id):
        return review_status(history, suite_id)
    judgments = repo.list_judgments(suite_id)
    if any(item.status in {"pending", "running"} for item in judgments):
        return review_status(history, suite_id)
    suite = repo.get_suite(suite_id)
    fraction = float(suite.manifest["suite"]["judge"].get("sample_fraction", 0.2))
    automatic = _automatic(judgments)
    sampled = set(select_sample([item.id for item in automatic], fraction, suite.seed))
    extra = [
        item
        for item in judgments
        if item.status == "manual_required" or item.decision == "uncertain"
    ]
    rows = [
        (item.id, item.input_sha256, item.id in sampled) for item in automatic if item.id in sampled
    ]
    rows += [(item.id, item.input_sha256, False) for item in extra]
    if rows:
        manifest_id = (
            "review_"
            + hashlib.sha256(
                json.dumps(sorted(row[0] for row in rows)).encode("utf-8")
            ).hexdigest()[:16]
        )
        repo.freeze_review_items(suite_id, manifest_id, rows, suite.seed)
    return review_status(history, suite_id)


def review_status(history: HistoryStore, suite_id: str) -> ReviewStatus:
    repo = history.evaluations
    items = repo.review_items(suite_id)
    judgments = {item.id: item for item in repo.list_judgments(suite_id)}
    latest = repo.latest_reviews(suite_id)
    required = sum(1 for item in items if item.sampled)
    reviewed = numerator = denominator = extra_pending = 0
    for item in items:
        review = latest.get(item.id)
        if not item.sampled:
            if review is None or review.decision == "uncertain":
                extra_pending += 1
            continue
        if review is None or review.decision == "uncertain":
            continue
        reviewed += 1
        original = judgments[item.judgment_id]
        denominator += 1
        same = review.decision == original.decision and (
            review.decision != "match" or review.bug_id == original.bug_id
        )
        numerator += int(same)
    frozen = bool(items) or not _automatic(list(judgments.values()))
    automatic_pending = any(item.status in {"pending", "running"} for item in judgments.values())
    return ReviewStatus(
        frozen=frozen and not automatic_pending,
        sample_required=required,
        sample_reviewed=reviewed,
        extra_pending=extra_pending,
        agreement=(numerator / denominator) if denominator else None,
        agreement_numerator=numerator,
        agreement_denominator=denominator,
    )


def _finding_inputs(history: HistoryStore, suite_id: str, project_dir: Path) -> dict[str, Any]:
    """Entrada original de cada julgamento, reconstruída do snapshot conferido pelo hash."""
    repo = history.evaluations
    suite = repo.get_suite(suite_id)
    by_attempt: dict[str, Any] = {}
    for trial in repo.list_trials(suite_id):
        if trial.selected_attempt_id is None:
            continue
        attempt = repo.get_attempt(trial.selected_attempt_id)
        snapshot = load_snapshot(project_dir, attempt)
        if snapshot is None:
            continue
        entries = applicable_entries(trial.variant, applicable_bugs(suite.manifest, trial.scenario))
        for finding in snapshot.get("findings", []):
            by_attempt[f"{attempt.id}:{finding['ref']}"] = judge_input(finding, entries)
    return by_attempt


def export_review(history: HistoryStore, suite_id: str, project_dir: Path) -> Path:
    """JSONL cego: esconde decisão do juiz, condição e executor; manifesto fica privado."""
    status = ensure_sample(history, suite_id)
    if not status.frozen:
        raise EvalError("review_not_ready", "automatic judgments are still pending")
    repo = history.evaluations
    judgments = {item.id: item for item in repo.list_judgments(suite_id)}
    inputs = _finding_inputs(history, suite_id, project_dir)
    lines: list[str] = []
    for item in repo.review_items(suite_id):
        judgment = judgments[item.judgment_id]
        data = inputs.get(f"{judgment.attempt_id}:{judgment.finding_ref}")
        if data is None:
            continue
        lines.append(
            json.dumps(
                {
                    "manifest_id": item.manifest_id,
                    "item_id": item.id,
                    "input_sha256": item.input_sha256,
                    "queue": "sample" if item.sampled else "additional",
                    "finding": data["finding"],
                    "applicable_bugs": data["applicable_bugs"],
                    "decision": None,
                    "bug_id": None,
                    "reason": None,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    target = project_dir / "evals" / suite_id / REVIEW_DIR / "eval-review.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomically(target, ("\n".join(lines) + "\n").encode("utf-8"))
    return target


class ReviewLine(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    manifest_id: str = Field(min_length=1, max_length=128)
    item_id: str = Field(min_length=1, max_length=128)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["match", "no_match", "uncertain"]
    bug_id: str | None = None
    reason: str = Field(min_length=1, max_length=2000)


def import_reviews(
    history: HistoryStore, suite_id: str, path: Path, reviewer: str
) -> tuple[int, int]:
    """Valida o lote inteiro antes da transação; qualquer divergência rejeita tudo."""
    if not reviewer.strip() or len(reviewer) > 200:
        raise EvalError("review_manifest_mismatch", "reviewer is required")
    repo = history.evaluations
    items = {item.id: item for item in repo.review_items(suite_id)}
    if not items:
        raise EvalError("review_manifest_mismatch", "no frozen review manifest for this suite")
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise EvalError("review_manifest_mismatch", "review file unreadable") from None
    entries: list[tuple[str, str, str, str | None, str, str]] = []
    seen: set[str] = set()
    for number, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        try:
            payload: Mapping[str, Any] = json.loads(raw)
            if payload.get("decision") is None:
                continue
            line = ReviewLine.model_validate(payload)
        except (ValueError, ValidationError):
            raise EvalError("review_manifest_mismatch", f"line {number} is invalid") from None
        item = items.get(line.item_id)
        if (
            item is None
            or item.manifest_id != line.manifest_id
            or item.input_sha256 != line.input_sha256
        ):
            raise EvalError("review_manifest_mismatch", f"line {number} does not match manifest")
        if line.item_id in seen:
            raise EvalError("review_manifest_mismatch", f"line {number} repeats an item")
        seen.add(line.item_id)
        if line.decision == "match" and not line.bug_id:
            raise EvalError("review_manifest_mismatch", f"line {number}: match requires bug_id")
        if line.decision != "match" and line.bug_id:
            raise EvalError("review_manifest_mismatch", f"line {number}: bug_id only with match")
        canonical = json.dumps(
            {
                "item": line.item_id,
                "decision": line.decision,
                "bug_id": line.bug_id,
                "reason": line.reason,
                "reviewer": reviewer,
            },
            sort_keys=True,
        )
        entries.append(
            (
                line.item_id,
                reviewer,
                line.decision,
                line.bug_id,
                line.reason,
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            )
        )
    return repo.import_reviews(entries)
