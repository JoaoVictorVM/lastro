"""Doubles determinísticos da avaliação: demo, executor e juiz sem rede nem modelo pago."""

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lastro.evaluation.baseline import ACTIONS_FILE, OUTPUT_FILE
from lastro.evaluation.demo import LogWindow, ResetResult, expected_flags, read_window
from lastro.evaluation.executors import TrialLaunch
from lastro.evaluation.models import (
    DemoSpec,
    EvalError,
    ExecutorIdentity,
    HeadlessResult,
    StopReason,
    Usage,
)
from lastro.evaluation.runner import RunnerDeps, SuiteRunner
from lastro.history import HistoryStore
from lastro_demo import __version__ as DEMO_VERSION

PHONE_FINDING = {
    "title": "Telefone volta ao valor anterior",
    "flow": "editar funcionario",
    "steps": ["Editar o telefone", "Salvar", "Reabrir o registro"],
    "expected": "O telefone novo permanece",
    "observed": "O telefone anterior aparece ao reabrir",
    "classification": "confirmed",
}
OTHER_FINDING = {
    "title": "Botão desalinhado",
    "flow": "listar funcionarios",
    "steps": ["Abrir a lista"],
    "expected": "Botões alinhados",
    "observed": "Botão deslocado",
    "classification": "suspected",
}


@dataclass
class FakeDemo:
    log: Path
    url: str = "http://127.0.0.1:9"
    reset_ok: Callable[[int], bool] = lambda index: True
    version_payload: Callable[[DemoSpec], dict[str, Any]] | None = None
    resets: list[DemoSpec] = field(default_factory=list[DemoSpec])
    seeds: list[int] = field(default_factory=list[int])
    stopped: int = 0
    current: DemoSpec | None = None

    def __post_init__(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.touch()

    def request(
        self, method: str, path: str, action: str, tenant: str | None = "aurora", status: int = 200
    ) -> None:
        line = {
            "method": method,
            "path": path,
            "action": action,
            "tenant": tenant,
            "status": status,
        }
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")

    def ensure(self, seed: int) -> str:
        self.seeds.append(seed)
        return self.url

    def reset(self, demo: DemoSpec) -> ResetResult:
        self.resets.append(demo)
        self.request("POST", "/__demo/reset", "control", None)
        if not self.reset_ok(len(self.resets)):
            return ResetResult(False, {"error": "boom"}, "http 500")
        self.current = demo
        return ResetResult(True, {"status": "ok", **expected_flags(demo)})

    def version(self) -> dict[str, Any]:
        assert self.current is not None
        self.request("GET", "/__demo/version", "control", None)
        if self.version_payload is not None:
            return self.version_payload(self.current)
        return {"version": DEMO_VERSION, **expected_flags(self.current)}

    def log_offset(self) -> int:
        return self.log.stat().st_size

    def window(self, start: int, end: int) -> LogWindow:
        return read_window(self.log, start, end)

    def stop(self) -> None:
        self.stopped += 1


@dataclass
class FakeExecutor:
    """Agente roteirizado: escreve saída estruturada e requisições no log do demo falso."""

    demo: FakeDemo
    statuses: dict[int, StopReason | str] = field(default_factory=dict[int, StopReason | str])
    findings: list[dict[str, Any]] = field(default_factory=lambda: [dict(PHONE_FINDING)])
    requests: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    version: str = "2.1.0"
    name: str = "claude-code"
    calls: list[TrialLaunch] = field(default_factory=list[TrialLaunch])
    on_launch: Callable[[TrialLaunch, threading.Event], None] | None = None

    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(self.name, self.version)

    def launch(self, launch: TrialLaunch, cancel: threading.Event) -> HeadlessResult:
        self.calls.append(launch)
        status = self.statuses.get(len(self.calls), StopReason.COMPLETED)
        if status == "raise":
            raise RuntimeError("executor crashed")
        if self.on_launch is not None:
            self.on_launch(launch, cancel)
        for method, path, action in self.requests:
            self.demo.request(method, path, action)
        root = launch.paths.root
        database = launch.paths.data / "eval-test" / "history.sqlite"
        database.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(database).close()
        with (root / ACTIONS_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"tool": "navigate", "outcome": "ok"}) + "\n")
            handle.write(json.dumps({"tool": "click", "outcome": "ok"}) + "\n")
            handle.write(
                json.dumps({"tool": "screenshot", "outcome": "ok", "evidence_id": "e1"}) + "\n"
            )
        output = {
            "findings": [{**item, "evidence": ["e1"]} for item in self.findings],
            "criteria": [{"id": "persistence", "outcome": "met", "evidence": ["e1"]}],
            "questions": [],
        }
        (root / OUTPUT_FILE).write_text(json.dumps(output), encoding="utf-8")
        assert isinstance(status, StopReason)
        return HeadlessResult(status=status, executor=self.identity(), usage=Usage(input_tokens=10))


@dataclass
class FakeJudge:
    """Juiz determinístico: match em D02 quando o finding fala de telefone."""

    garbage: int = 0
    limit_at: int | None = None
    prompts: list[str] = field(default_factory=list[str])

    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity("claude-code", "judge-1.0")

    def ask(self, prompt: str, workdir: Path) -> tuple[str, Usage, str | None]:
        self.prompts.append(prompt)
        if self.limit_at is not None and len(self.prompts) >= self.limit_at:
            raise EvalError("rate_limit", "judge limit")
        if self.garbage > 0:
            self.garbage -= 1
            return "not json at all", Usage(), "judge-model"
        data = json.loads(prompt.split("<judge-input>\n", 1)[1].split("\n</judge-input>", 1)[0])
        finding = data["finding"]
        bugs = {item["bug_id"] for item in data["applicable_bugs"]}
        phone = "telefone" in finding["title"].lower() and "D02" in bugs
        answer = {
            "schema_version": 1,
            "finding_ref": finding["finding_ref"],
            "decision": "match" if phone else "no_match",
            "bug_id": "D02" if phone else None,
            "rubric": {"same_flow": phone, "same_trigger": phone, "same_symptom": phone},
            "reason": "Comparação com o catálogo.",
            "evidence_refs": [],
        }
        return json.dumps(answer), Usage(), "judge-model"


def make_runner(
    history: HistoryStore,
    project_dir: Path,
    demo: FakeDemo,
    executor: FakeExecutor,
    judge: FakeJudge | None = None,
    **overrides: Any,
) -> SuiteRunner:
    deps = RunnerDeps(
        history=history,
        project_dir=project_dir,
        project_id="eval-test",
        demo=demo,
        executor=executor,
        judge=judge if judge is not None else FakeJudge(),
        credentials=("user@example.test", "secret"),
        decision_sleep=lambda seconds: None,
        owner_gone=lambda owner: True,
        **overrides,
    )
    return SuiteRunner(deps)


def small_suite(
    tmp: Path, *, conditions: str = "[baseline, full]", runs: int = 2, extra: str = ""
) -> Path:
    """Suite local mínima: uma missão, um cenário com D02."""
    folder = tmp / "suite"
    folder.mkdir(exist_ok=True)
    (folder / "mission.md").write_text(
        "# Objetivo\n\nEditar funcionários.\n\n# Critérios\n\n- Dados persistem\n", encoding="utf-8"
    )
    path = folder / "suite.yaml"
    path.write_text(
        f"""schema_version: 1
id: tiny
executor: claude-code
runs_per_condition: {runs}
seed: 3
conditions: {conditions}
missions:
  - id: crud
    file: mission.md
    criteria:
      - id: persistence
        text: Dados persistem
scenarios:
  - id: base
    mission: crud
    demo: {{defects: [d02], scenarios: [s02], variant: base}}
{extra}""",
        encoding="utf-8",
    )
    return path
