import json
import shutil
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.integration.sources_support import CleanScanner
from typer.testing import CliRunner

from lastro.cli import eval_cmd
from lastro.cli.app import app
from lastro.config.loader import ProjectContext
from lastro.evaluation.models import (
    RETRIEVAL_METRICS,
    EvalError,
    ExecutorIdentity,
    Usage,
)
from lastro.evaluation.ragas_adapter import MetricOutcome, RagasAdapter, RetrievalCase
from lastro.evaluation.retrieval import (
    RetrievalDeps,
    RetrievalRunner,
    create_retrieval_suite,
    load_retrieval_suite,
)
from lastro.history import HistoryStore, open_history
from lastro.history.database import Database
from lastro.history.migrations import apply_migrations
from lastro.history.sources import SourceRepository
from lastro.sources.embeddings import HashingModel
from lastro.sources.retrieval import vec_connector
from lastro.sources.sync import SourceService
from lastro.timeutil import utc_now

pytest.importorskip("sqlite_vec")
pytest.importorskip("markdown_it")
pytest.importorskip("bs4")

REPO = Path(__file__).resolve().parents[2]
SUITE = """schema_version: 1
id: retrieval-test
purpose: retrieval
executor: claude-code
runs_per_condition: {runs}
conditions: [full]
corpus: evals/retrieval/demo/docs
questions: evals/retrieval/demo/questions.json
limits:
  suite_budget_usd: "{budget}"
"""


class FakePort:
    """Sessão headless dublê: sem tools, devolve texto e conta cada chamada."""

    def __init__(self, name: str = "claude-code", fail_at: int | None = None) -> None:
        self.name = name
        self.calls: list[str] = []
        self.fail_at = fail_at

    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(self.name, "1.0.0", None, "fake-model")

    def ask(self, prompt: str, workdir: Path) -> tuple[str, Usage, str | None]:
        self.calls.append(prompt)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise EvalError("rate_limit", "provider rate limit")
        answer = prompt.split("Trechos:", 1)[-1].strip().splitlines()
        text = answer[1] if len(answer) > 1 else "A documentação não responde."
        return text, Usage(10, 5, 1_000_000, "fake"), "fake-model"


class FakeRagas:
    def __init__(self, missing: str | None = None) -> None:
        self.cases: list[RetrievalCase] = []
        self.missing = missing

    def identity(self) -> dict[str, Any]:
        return {"name": "ragas", "version": "test", "metrics": list(RETRIEVAL_METRICS)}

    def score(self, case: RetrievalCase, ask: Callable[[str], str]) -> dict[str, MetricOutcome]:
        self.cases.append(case)
        ask("judge: " + case.question)
        hit = any(word in " ".join(case.contexts) for word in case.reference.split()[:3])
        return {
            name: MetricOutcome(None, "metric_error:Timeout")
            if name == self.missing
            else MetricOutcome(1.0 if hit else 0.5)
            for name in RETRIEVAL_METRICS
        }


def service_factory(directory: Path) -> SourceService:
    path = directory / "sources.sqlite"
    database = Database(path)
    apply_migrations(database)
    repository = SourceRepository(
        database, lambda: Database(path, connector=vec_connector), "retrieval-eval", utc_now
    )
    model = HashingModel()
    return SourceService(
        repository,
        project_names=("retrieval-eval",),
        model_factory=lambda: model,
        scanner_factory=CleanScanner,
    )


@pytest.fixture
def project(history_context: ProjectContext, workdir: Path) -> ProjectContext:
    shutil.copytree(REPO / "evals" / "retrieval", workdir / "evals" / "retrieval")
    return history_context


def write_suite(workdir: Path, runs: int = 1, budget: str = "10.00") -> Path:
    path = workdir / "evals" / "suites" / "retrieval-test.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SUITE.format(runs=runs, budget=budget), encoding="utf-8")
    return path


def make(
    history: HistoryStore,
    workdir: Path,
    *,
    answerer: FakePort | None = None,
    judge: FakePort | None = None,
    ragas: FakeRagas | None = None,
) -> RetrievalDeps:
    return RetrievalDeps(
        history=history,
        project_dir=history.project_dir,
        project_root=workdir,
        answerer=answerer or FakePort(),
        judge=judge or FakePort(),
        ragas=ragas or FakeRagas(),
        service_factory=service_factory,
    )


def created(deps: RetrievalDeps, workdir: Path, runs: int = 1, budget: str = "10.00") -> str:
    loaded = load_retrieval_suite(str(write_suite(workdir, runs, budget)), workdir, cwd=workdir)
    record = create_retrieval_suite(
        deps.history.evaluations,
        loaded,
        deps.answerer.identity().as_json(),
        deps.judge.identity().as_json(),
        deps.ragas.identity(),
    )
    return record.id


def test_f14_retrieval_uses_headless_lifecycle(project: ProjectContext, workdir: Path) -> None:
    history = open_history(project)
    answerer, judge = FakePort(), FakePort()
    deps = make(history, workdir, answerer=answerer, judge=judge)
    suite_id = created(deps, workdir)

    outcome = RetrievalRunner(deps).run(suite_id)

    repo = history.evaluations
    trials = repo.list_trials(suite_id)
    assert outcome.exit_code == 0 and outcome.suite.status == "completed"
    assert {item.status for item in trials} == {"valid"}
    assert len(answerer.calls) == len(trials)
    assert all("Trechos:" in prompt and "[1] [docs/" in prompt for prompt in answerer.calls)
    components = {entry.component for entry in repo.list_budget(suite_id)}
    assert components == {"retrieval", "judge"}
    assert all(entry.status == "settled" for entry in repo.list_budget(suite_id))
    sample = repo.list_samples(suite_id)[0]
    assert sample.snapshot["contexts"][0]["citation"].startswith("[docs/")
    assert sample.snapshot["calls"]
    assert [item.event_type for item in repo.list_events(suite_id)].count("retrieval_index") == 1


def test_ragas_receives_real_response_and_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    prompts: list[str] = []

    class Prompt:
        def __init__(self, text: str) -> None:
            self.text = text

        def to_string(self) -> str:
            return self.text

    class BaseRagasLLM:
        def __init__(self) -> None:
            pass

    class Generation:
        def __init__(self, text: str) -> None:
            self.text = text

    class LLMResult:
        def __init__(self, generations: list[list[Generation]]) -> None:
            self.generations = generations

    class SingleTurnSample:
        def __init__(self, **fields: Any) -> None:
            captured["sample"] = fields

    def metric(score: float) -> type:
        class Metric:
            def __init__(self, llm: Any) -> None:
                self.llm = llm

            async def single_turn_ascore(self, sample: Any) -> float:
                result = await self.llm.agenerate_text(Prompt("avalie"))
                prompts.append(result.generations[0][0].text)
                return score

        return Metric

    modules = {
        "ragas": types.ModuleType("ragas"),
        "ragas.llms": types.ModuleType("ragas.llms"),
        "ragas.llms.base": types.SimpleNamespace(BaseRagasLLM=BaseRagasLLM),
        "ragas.metrics": types.SimpleNamespace(
            LLMContextPrecisionWithReference=metric(0.8),
            LLMContextRecall=metric(0.6),
            Faithfulness=metric(1.5),
        ),
        "ragas.dataset_schema": types.SimpleNamespace(SingleTurnSample=SingleTurnSample),
        "langchain_core": types.ModuleType("langchain_core"),
        "langchain_core.outputs": types.SimpleNamespace(LLMResult=LLMResult, Generation=Generation),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    asked: list[str] = []

    scores = RagasAdapter().score(
        RetrievalCase("Email é obrigatório?", ("O email é opcional.",), "Não, é opcional.", "Não."),
        lambda prompt: asked.append(prompt) or "veredito",
    )

    assert captured["sample"] == {
        "user_input": "Email é obrigatório?",
        "retrieved_contexts": ["O email é opcional."],
        "response": "Não, é opcional.",
        "reference": "Não.",
    }
    assert asked == ["avalie", "avalie", "avalie"] and prompts == ["veredito"] * 3
    assert scores["retrieval.context_precision"] == MetricOutcome(0.8)
    assert scores["retrieval.context_recall"] == MetricOutcome(0.6)
    assert scores["retrieval.faithfulness"] == MetricOutcome(None, "invalid_score")


def test_thirty_questions_and_five_repetitions(project: ProjectContext, workdir: Path) -> None:
    shutil.copy(REPO / "evals" / "suites" / "retrieval-demo.yaml", workdir / "suite.yaml")
    loaded = load_retrieval_suite("suite.yaml", workdir, cwd=workdir)
    history = open_history(project)
    deps = make(history, workdir)
    suite_id = create_retrieval_suite(
        history.evaluations,
        loaded,
        deps.answerer.identity().as_json(),
        deps.judge.identity().as_json(),
        deps.ragas.identity(),
    ).id

    assert len(loaded.dataset.questions) >= 30
    assert loaded.spec.runs_per_condition == 5
    trials = history.evaluations.list_trials(suite_id)
    assert len(trials) == 5 * len(loaded.dataset.questions)
    assert {item.repetition for item in trials} == {1, 2, 3, 4, 5}


def test_retrieval_results_share_eval_store(project: ProjectContext, workdir: Path) -> None:
    history = open_history(project)
    deps = make(history, workdir)
    suite_id = created(deps, workdir)
    RetrievalRunner(deps).run(suite_id)

    results = history.evaluations.list_results(suite_id)
    overall = {item.metric: item for item in results if item.stratum["scenario"] == "all"}

    assert set(overall) == set(RETRIEVAL_METRICS)
    assert all(item.stratum["purpose"] == "retrieval" for item in results)
    assert overall["retrieval.context_recall"].n == overall["retrieval.context_recall"].planned_n
    assert not any(item.metric == "detection_rate" for item in results)
    per_question = [item for item in results if item.stratum["scenario"] == "email-optional"]
    assert len(per_question) == 3 and all(item.planned_n == 1 for item in per_question)
    assert suite_id in {item.id for item in history.evaluations.list_suites()}


def test_resume_preserves_completed_samples(project: ProjectContext, workdir: Path) -> None:
    history = open_history(project)
    answerer = FakePort(fail_at=4)
    deps = make(history, workdir, answerer=answerer)
    suite_id = created(deps, workdir)

    paused = RetrievalRunner(deps).run(suite_id)

    assert paused.exit_code == 4 and paused.suite.status == "paused"
    valid_before = [
        item.id for item in history.evaluations.list_trials(suite_id) if item.status == "valid"
    ]
    assert len(valid_before) == 3
    answerer.fail_at = None
    resumed = RetrievalRunner(make(history, workdir, answerer=answerer)).run(suite_id, resume=True)

    assert resumed.exit_code == 0
    trials = history.evaluations.list_trials(suite_id)
    assert len(answerer.calls) == len(trials) + 1
    samples = history.evaluations.list_samples(suite_id)
    assert len({(item.question_id, item.repetition) for item in samples}) == len(trials)


def test_cost_cap_and_rate_limit_pause(project: ProjectContext, workdir: Path) -> None:
    history = open_history(project)
    native = FakePort("native")
    deps = make(history, workdir, answerer=native, judge=FakePort("native"))
    suite_id = created(deps, workdir, budget="0.15")
    history.evaluations._db.write(  # pyright: ignore[reportPrivateUsage]
        lambda conn: conn.execute(
            "UPDATE eval_suites SET executor_name = 'native' WHERE id = ?", (suite_id,)
        )
    )

    outcome = RetrievalRunner(deps).run(suite_id)

    assert outcome.exit_code == 4 and outcome.message == "suite_cost_cap"
    assert history.evaluations.get_suite(suite_id).pause_reason == "suite_cost_cap"
    rate = FakePort(fail_at=1)
    limited = make(open_history(project), workdir, judge=rate)
    other = created(limited, workdir)
    stopped = RetrievalRunner(limited).run(other)
    assert stopped.exit_code == 4 and stopped.message == "rate_limit"


def test_missing_score_is_not_zero(project: ProjectContext, workdir: Path) -> None:
    history = open_history(project)
    deps = make(history, workdir, ragas=FakeRagas(missing="retrieval.faithfulness"))
    suite_id = created(deps, workdir)
    RetrievalRunner(deps).run(suite_id)

    results = {
        item.metric: item
        for item in history.evaluations.list_results(suite_id)
        if item.stratum["scenario"] == "all"
    }

    faithfulness = results["retrieval.faithfulness"]
    assert faithfulness.n == 0 and faithfulness.mean is None
    assert faithfulness.excluded_n == faithfulness.planned_n
    assert any(
        item.startswith("excluded:metric_error:Timeout=") for item in faithfulness.limitations
    )
    assert results["retrieval.context_recall"].mean is not None


def test_manifest_change_blocks_resume(project: ProjectContext, workdir: Path) -> None:
    history = open_history(project)
    answerer = FakePort(fail_at=2)
    deps = make(history, workdir, answerer=answerer)
    suite_id = created(deps, workdir)
    assert RetrievalRunner(deps).run(suite_id).exit_code == 4
    questions = workdir / "evals" / "retrieval" / "demo" / "questions.json"
    data = json.loads(questions.read_text(encoding="utf-8"))
    data["questions"][0]["reference"] = "Outra referência."
    questions.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(EvalError) as caught:
        RetrievalRunner(make(history, workdir, answerer=answerer)).run(suite_id, resume=True)

    assert caught.value.code == "snapshot_changed"


def test_cli_runs_retrieval_suite(
    project: ProjectContext, workdir: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(history: HistoryStore, *args: Any) -> RetrievalDeps:
        return make(history, workdir)

    monkeypatch.setattr(eval_cmd, "RETRIEVAL_DEPS_FACTORY", factory)
    write_suite(workdir)

    result = runner.invoke(
        app,
        ["--profile", "homolog", "eval", "run", "evals/suites/retrieval-test.yaml", "--json"],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["state"] == "completed"
    assert summary["trials"]["valid"] == summary["trials"]["planned"]


def test_question_anchors_exist_in_corpus() -> None:
    from lastro.evaluation.models import RetrievalDataset
    from lastro.sources.extraction import extract
    from lastro.sources.models import document_format

    docs = REPO / "evals" / "retrieval" / "demo" / "docs"
    raw = (REPO / "evals" / "retrieval" / "demo" / "questions.json").read_bytes()
    dataset = RetrievalDataset.model_validate_json(raw)
    sections: dict[tuple[str, str], str] = {}
    for path in docs.iterdir():
        kind = document_format(path.name)
        assert kind is not None
        for section in extract(kind, path.read_bytes(), path.name).sections:
            sections[(path.name, section.heading)] = " ".join(section.text.split())

    assert len(dataset.questions) >= 30
    for question in dataset.questions:
        for location in question.reference_locations:
            text = sections.get((location.path, location.heading))
            assert text is not None, (question.id, location.path, location.heading)
            assert " ".join(location.quote.split()) in text, question.id
