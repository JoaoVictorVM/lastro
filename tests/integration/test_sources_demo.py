import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.integration.sources_support import make_service

from lastro.browser import tools
from lastro.browser.demo import FIELDS, demo_policy, demo_test_data
from lastro.browser.tools import BrowserTools
from lastro.history import TextMission, open_history
from lastro.sources.embeddings import HashingModel

pytest.importorskip("sqlite_vec")
pytest.importorskip("markdown_it")
pytest.importorskip("bs4")

DOCS = Path(__file__).resolve().parents[2] / "evals" / "retrieval" / "demo" / "docs"
FLOW = "employee.create"


def test_demo_optional_email_conflicts_with_required_ui(
    make_auth_project: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "sources_model_factory", lambda root: HashingModel())
    project = make_auth_project().context()
    project.profile.policy = demo_policy()
    project.profile.test_data = demo_test_data()
    project.profile.editable_fields = {"employee": list(FIELDS)}
    history = open_history(project)
    make_service(history).add(DOCS, project.loaded.project_dir.name)
    run_id = history.create_run(project, TextMission("Explorar o cadastro")).record.id
    runtime = BrowserTools(project, history, run_id)
    base = project.profile.target_url

    async def scenario() -> dict[str, Any]:
        try:
            context = await runtime.call("mission_context", {})
            assert context["sources"]["available"] is True
            found = await runtime.call(
                "sources_query", {"flow": FLOW, "query": "O email é obrigatório no cadastro?"}
            )
            body = str(found["content"]).split("\n", 2)[2].rsplit("\n", 1)[0]
            passage = json.loads(body)["passages"][0]
            assert passage["citation"] == "[docs/cadastro.md § Campos obrigatórios]"
            data = (await runtime.call("generate_test_data", {"entity": "employee"}))["values"]
            await runtime.call("navigate", {"url": base + "/employees/new"})
            for label, value in (
                ("Nome completo", data["name"]),
                ("CPF", data["cpf"]),
                ("Data de admissão", data["hire_date"]),
            ):
                assert (await runtime.call("fill", {"target": {"label": label}, "value": value}))[
                    "status"
                ] == "ok"
            await runtime.call("select", {"target": {"label": "Perfil"}, "value": "medico"})
            await runtime.call("submit", {"target": {"role": "button", "name": "Salvar"}})
            observed = await runtime.call("observe", {})
            shot = await runtime.call("screenshot", {})
            conflict = await runtime.call(
                "sources_report_conflict",
                {
                    "flow": FLOW,
                    "left": {
                        "kind": "passage",
                        "reference_id": passage["reference_id"],
                        "quote": "O email é opcional no cadastro de funcionário.",
                    },
                    "right": {
                        "kind": "observation",
                        "reference_id": observed["observation_id"],
                        "quote": "O e-mail é obrigatório.",
                    },
                    "explanation": "A documentação permite omitir o email, mas a interface "
                    "recusa o cadastro sem ele.",
                    "idempotency_key": "email-required-conflict",
                },
            )
            finding = await runtime.call(
                "report_finding",
                {
                    "title": "Cadastro exige email que a documentação declara opcional",
                    "flow": "Cadastro de funcionário",
                    "steps": [
                        "Abrir novo funcionário",
                        "Preencher os campos sem email",
                        "Salvar",
                    ],
                    "expected": {
                        "text": "O cadastro deveria aceitar email vazio conforme documentação.",
                        "source": "documentation",
                        "source_ref": passage["reference_id"],
                    },
                    "observed": "A interface recusa o cadastro com a mensagem de e-mail "
                    "obrigatório.",
                    "evidence": [{"artifact_id": shot["artifact_id"]}],
                    "classification": "suspected",
                    "failure_domain": "system",
                },
            )
            ended = await runtime.call("end_run", {"outcome": "completed"})
            return {"conflict": conflict, "finding": finding, "ended": ended}
        finally:
            runtime.approvals.close()
            await runtime.session.close()

    result = asyncio.run(scenario())

    assert result["conflict"]["assessment"] == "reported", result
    assert result["conflict"]["status"] == "open"
    assert "finding_id" in result["finding"], result
    report = Path(result["ended"]["report_path"]).read_text(encoding="utf-8")
    assert "## Source conflicts" in report
    assert "documentation (passage)" in report and "observation (observation)" in report
    assert "O e-mail é obrigatório" in report
    assert r"\[docs/cadastro.md § Campos obrigatórios\]" in report
    assert "**Sources used**" in report
