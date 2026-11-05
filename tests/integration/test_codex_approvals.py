"""O adapter não cria caminho novo de aprovação: a decisão continua fora do modelo."""

from pathlib import Path

import pytest

from lastro.approvals import proof
from lastro.browser.permissions import PROOF_FIELD
from lastro.codex import config as cc
from lastro.scaffold.files import _template

from .codex_support import CodexProject, make_project


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexProject:
    monkeypatch.chdir(tmp_path)
    return make_project(tmp_path)


def test_gate_uses_lastro_approve() -> None:
    instructions = _template("AGENTS.md.tmpl").template
    assert "lastro approve" in instructions
    assert "approval_status" in instructions


def test_generic_codex_approval_is_not_proof(project: CodexProject) -> None:
    """Aprovação automática da CLI só cobre chamar a tool; o adapter nunca emite prova."""
    run_id = project.create_run()
    project.launch(run_id)
    arguments = project.call()["argv"]
    argv = " ".join(item for item in arguments if not item.startswith("developer_instructions="))
    assert PROOF_FIELD not in argv
    assert "--approve-for-me" not in argv
    assert "dangerously" not in argv
    assert cc.expected_policy()["features"]["guardian_approval"] is False


def _token(secret: str, action: str, fingerprint: str, moment: object) -> str:
    return proof.issue(secret, action, fingerprint, moment)  # type: ignore[arg-type]


def test_expired_gate_denied() -> None:
    from datetime import UTC, datetime, timedelta

    moment = datetime(2026, 11, 4, tzinfo=UTC)
    token = _token("s", "a", "f", moment)
    with pytest.raises(proof.ProofError):
        proof.verify("s", token, "a", "f", moment + timedelta(days=1))


def test_changed_fingerprint_denied() -> None:
    from datetime import UTC, datetime

    moment = datetime(2026, 11, 4, tzinfo=UTC)
    token = _token("s", "a", "f", moment)
    with pytest.raises(proof.ProofError):
        proof.verify("s", token, "a", "other", moment)
