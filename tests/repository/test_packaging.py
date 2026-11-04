import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from lastro.skills import SKILL_FILES

REPO_ROOT = Path(__file__).resolve().parents[2]
# Capturado na coleta, antes de o conftest isolar HOME e LOCALAPPDATA: o uv reutiliza o cache.
HOST_ENV = dict(os.environ)
BUNDLE = [f"lastro/skills/{name}" for name in SKILL_FILES]
TEMPLATES = [
    "lastro/scaffold/templates/CLAUDE.md.tmpl",
    "lastro/scaffold/templates/mission.md.tmpl",
    # Regras confiáveis do scanner de segredos do contexto de mudança.
    "lastro/change_context/gitleaks.toml",
]


def build(kind: str, out_dir: Path) -> Path:
    uv = HOST_ENV.get("UV") or shutil.which("uv", path=HOST_ENV.get("PATH"))
    if not uv:
        pytest.skip("uv não encontrado; rode a suíte com uv run pytest")
    result = subprocess.run(
        [uv, "build", f"--{kind}", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        env=HOST_ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    (built,) = out_dir.glob("lastro-*.whl" if kind == "wheel" else "lastro-*.tar.gz")
    return built


def test_wheel_contains_mission_skill_bundle(tmp_path: Path) -> None:
    with zipfile.ZipFile(build("wheel", tmp_path / "wheel")) as archive:
        names = set(archive.namelist())
    with tarfile.open(build("sdist", tmp_path / "sdist")) as archive:
        sdist = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}

    for expected in [*BUNDLE, *TEMPLATES]:
        assert expected in names, expected
        assert f"src/{expected}" in sdist, expected
