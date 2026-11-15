from pathlib import Path
from typing import Any

from tests.integration.eval_support import FakeDemo, FakeExecutor, make_runner, small_suite

from lastro.config.loader import ProjectContext
from lastro.evaluation.export import collect, export, opaque_ref, render
from lastro.evaluation.runner import create_suite
from lastro.evaluation.suites import load_suite
from lastro.history import open_history


def finished(history_context: ProjectContext, tmp_path: Path, name: str = "suite") -> Any:
    history = open_history(history_context)
    folder = tmp_path / name
    folder.mkdir()
    suite = create_suite(
        history.evaluations, load_suite(str(small_suite(folder))), "claude-code", "2.1.0"
    )
    demo = FakeDemo(folder / "demo.log")
    make_runner(history, history.project_dir, demo, FakeExecutor(demo)).run(suite.id)
    return history, suite


def test_public_allowlist_removes_private_data(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite = finished(history_context, tmp_path)
    path = export(history, suite.id, public=True, root=tmp_path, timezone="UTC")
    text = path.read_text(encoding="utf-8")
    assert path.parent == tmp_path / "evals" / "results" / "public"
    assert suite.id not in text and opaque_ref(suite.id) in text
    for private in (
        str(history.project_dir),
        history.project_dir.as_posix(),
        "trial_",
        "att_",
        "aurora",
        "Clínica",
        "user@example.test",
        "secret",
        "Telefone volta",
        ".png",
    ):
        assert private not in text, private
    private_path = export(history, suite.id, public=False, root=tmp_path, timezone="UTC")
    assert history.project_dir in private_path.parents
    assert suite.id in private_path.read_text(encoding="utf-8")


def test_report_includes_metric_n_versions_and_limitations(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, suite = finished(history_context, tmp_path)
    text = render(collect(history, suite.id), public=True, timezone="UTC")
    assert "Executor: claude-code 2.1.0" in text
    assert "| `baseline` | 2 | 2 | 0 | 0 | 0 |" in text
    assert (
        "| `full` | base | base | claude-code 2.1.0 | detection_rate | 2 | 100.0% | 0.0% |" in text
    )
    assert "manual_review_pending" in text or "Status: **final**" in text
    assert "Judge-human agreement" in text and "SD is the sample standard deviation" in text


def test_name_collision_preserves_previous_export(
    history_context: ProjectContext, tmp_path: Path
) -> None:
    history, first = finished(history_context, tmp_path, "a")
    _, second = finished(history_context, tmp_path, "b")
    one = export(history, first.id, public=True, root=tmp_path, timezone="UTC")
    content = one.read_text(encoding="utf-8")
    two = export(history, second.id, public=True, root=tmp_path, timezone="UTC")
    assert one != two and one.read_text(encoding="utf-8") == content
    assert export(history, first.id, public=True, root=tmp_path, timezone="UTC") == one


def test_unknown_cost_is_not_zero(history_context: ProjectContext, tmp_path: Path) -> None:
    history, suite = finished(history_context, tmp_path)
    text = render(collect(history, suite.id), public=True, timezone="UTC")
    cost_rows = [line for line in text.splitlines() if "| cost_usd |" in line]
    assert cost_rows and all("| 0 | n/a | n/a |" in line for line in cost_rows)
    assert "$0.00" not in text
    assert "external_cost_unavailable" in text
