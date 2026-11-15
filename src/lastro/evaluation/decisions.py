"""Decisões roteirizadas fora do modelo: approver por regra exata e respostas de fixtures.

Usa só as interfaces externas existentes (`decide_approval`, `answer_clarification`). Regra não
encontrada, vencida, ambígua ou divergente resulta em skip; informação ausente nunca é
inventada. Gate mode `unattended` nunca aprova.
"""

import contextlib
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from lastro.evaluation.models import (
    ApproverRule,
    ApproverSpec,
    ClarificationSpec,
    GateMode,
    as_list,
    as_map,
)
from lastro.history import (
    ApprovalChoice,
    ApprovalError,
    ApprovalRecord,
    ApprovalStatus,
    DecidedBy,
    HistoryError,
    HistoryStore,
)

POLL_SECONDS = 0.25
UNAVAILABLE_ANSWER = (
    "No information is available for this gap in this scenario. Do not assume the requirement; "
    "record the affected criterion as blocked or inconclusive."
)


def rule_sha256(rule: ApproverRule) -> str:
    return hashlib.sha256(rule.model_dump_json().encode("utf-8")).hexdigest()


def _path(data: object, *keys: str) -> object:
    for key in keys:
        data = as_map(data).get(key)
    return data


def route_matches(template: str, route: str) -> bool:
    """Segmento `{nome}` aceita exatamente um segmento não vazio; o resto é comparação literal."""
    expected, actual = template.strip("/").split("/"), route.strip("/").split("/")
    if len(expected) != len(actual):
        return False
    return all(
        (part.startswith("{") and part.endswith("}") and bool(value)) or part == value
        for part, value in zip(expected, actual, strict=True)
    )


def rule_matches(rule: ApproverRule, payload: dict[str, Any]) -> bool:
    """Compara o payload persistido do pedido atual, nunca texto da página."""
    if _path(payload, "tenant", "id") != rule.tenant:
        return False
    if _path(payload, "action", "operation") != rule.operation:
        return False
    route = _path(payload, "action", "route")
    if not isinstance(route, str) or not route_matches(rule.route, route):
        return False
    items = [as_map(item) for item in as_list(payload.get("changes"))]
    fields = sorted(str(item.get("field")) for item in items)
    if fields != sorted(rule.fields):
        return False
    values = {str(item.get("field")): str(item.get("new")) for item in items}
    return all(values.get(name) == expected for name, expected in rule.values.items())


@dataclass(frozen=True)
class Decision:
    action_id: str
    run_id: str
    choice: ApprovalChoice
    rule_id: str | None
    reason: str


@dataclass
class ScriptedApprover:
    history: HistoryStore
    spec: ApproverSpec
    gate_mode: GateMode = GateMode.SCRIPTED
    uses: dict[str, int] = field(default_factory=dict[str, int])
    decisions: list[Decision] = field(default_factory=list[Decision])

    def choose(self, record: ApprovalRecord) -> tuple[ApprovalChoice, ApproverRule | None, str]:
        if self.gate_mode == GateMode.UNATTENDED:
            return ApprovalChoice.SKIP, None, "unattended policy: never approve"
        if record.payload.get("fingerprint") != record.fingerprint:
            return ApprovalChoice.SKIP, None, "fingerprint differs from the stored request"
        matches = [rule for rule in self.spec.rules if rule_matches(rule, record.payload)]
        if not matches:
            return ApprovalChoice.SKIP, None, "no scenario rule matches"
        if len(matches) > 1:
            return ApprovalChoice.SKIP, None, "ambiguous scenario rules"
        rule = matches[0]
        if self.uses.get(rule.id, 0) >= rule.max_uses:
            return ApprovalChoice.SKIP, rule, "rule uses exhausted"
        choice = ApprovalChoice.APPROVE if rule.decision == "approve" else ApprovalChoice.SKIP
        return choice, rule, "scenario rule"

    def decide(self, run_ids: Iterable[str]) -> list[Decision]:
        """Uma passada: decide os pedidos pendentes das runs da própria tentativa."""
        made: list[Decision] = []
        for run_id in run_ids:
            try:
                record = self.history.pending_approval(run_id)
            except HistoryError:
                continue
            if record is None or record.status != ApprovalStatus.PENDING:
                continue
            if record.run_id != run_id:
                continue
            choice, rule, why = self.choose(record)
            note_data = {
                "rule_id": rule.id if rule else None,
                "rule_sha256": rule_sha256(rule) if rule else None,
                "why": why,
            }
            note = "scripted " + json.dumps(note_data, sort_keys=True)
            try:
                self.history.decide_approval(
                    record.action_id, choice, DecidedBy.SCRIPTED, note=note, run_id=run_id
                )
            except (ApprovalError, HistoryError):
                continue
            if rule is not None and choice == ApprovalChoice.APPROVE:
                self.uses[rule.id] = self.uses.get(rule.id, 0) + 1
            decision = Decision(record.action_id, run_id, choice, rule.id if rule else None, why)
            self.decisions.append(decision)
            made.append(decision)
        return made


def gap_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@dataclass
class ScriptedClarifier:
    history: HistoryStore
    fixtures: tuple[ClarificationSpec, ...]
    answered: list[tuple[str, str, bool]] = field(default_factory=list[tuple[str, str, bool]])

    def fixture_for(self, gap_id: str, question: str = "") -> ClarificationSpec | None:
        """Mapeamento inequívoco: a chave aparece no id da lacuna ou no texto da pergunta."""
        texts = (gap_key(gap_id), gap_key(question))
        found = [item for item in self.fixtures if any(item.gap_key in text for text in texts)]
        return found[0] if len(found) == 1 else None

    def answer(self, run_ids: Iterable[str]) -> int:
        from lastro.history.models import ClarificationStatus
        from lastro.mission.models import ClarificationAnswer

        count = 0
        for run_id in run_ids:
            try:
                pending = [
                    item
                    for item in self.history.list_clarifications(run_id)
                    if item.status == ClarificationStatus.PENDING
                ]
            except HistoryError:
                continue
            for item in pending:
                fixture = self.fixture_for(item.gap_id, item.question)
                available = fixture is not None and fixture.answer is not None
                text = fixture.answer if fixture is not None and fixture.answer else None
                try:
                    self.history.answer_clarification(
                        item.id, ClarificationAnswer(answer=text or UNAVAILABLE_ANSWER)
                    )
                except HistoryError:
                    continue
                self.answered.append((run_id, gap_key(item.gap_id), available))
                count += 1
        return count


@dataclass
class TrialDecisions:
    """Approver e clarificador de uma tentativa; contadores de uso valem a tentativa inteira."""

    spec: ApproverSpec
    fixtures: tuple[ClarificationSpec, ...]
    bound: Callable[[], HistoryStore | None]
    gate_mode: GateMode = GateMode.SCRIPTED
    approver: ScriptedApprover | None = None
    clarifier: ScriptedClarifier | None = None

    def tick(self) -> None:
        history = self.bound()
        if history is None:
            return
        if self.approver is None or self.clarifier is None:
            self.approver = ScriptedApprover(history, self.spec, self.gate_mode)
            self.clarifier = ScriptedClarifier(history, self.fixtures)
        # Inclui as runs filhas de reprodução da própria tentativa.
        ids = [record.id for record in history.list_non_terminal()]
        self.approver.decide(ids)
        self.clarifier.answer(ids)


@dataclass
class DecisionLoop:
    """Poll de 250 ms com relógio injetável; termina quando a tentativa encerra."""

    step: Callable[[], None]
    sleep: Callable[[float], None] = time.sleep
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def tick(self) -> None:
        self.step()

    def _loop(self) -> None:
        while not self.stop.is_set():
            # Falha de uma passada (lock momentâneo, run encerrando) não derruba o loop.
            with contextlib.suppress(Exception):
                self.tick()
            self.sleep(POLL_SECONDS)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="eval-decisions", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.tick()
