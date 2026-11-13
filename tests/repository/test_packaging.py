import os
import shutil
import subprocess
import sys
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


COMPOSE = "lastro/observability/resources/compose.yaml"
COMPOSE_README = "lastro/observability/resources/README.md"


def test_wheel_contains_pinned_langfuse_compose(tmp_path: Path) -> None:
    with zipfile.ZipFile(build("wheel", tmp_path / "wheel")) as archive:
        names = set(archive.namelist())
        compose = archive.read(COMPOSE).decode("utf-8")

    assert COMPOSE in names and COMPOSE_README in names
    images = [
        line.split("image:", 1)[1].strip() for line in compose.splitlines() if "image:" in line
    ]
    assert len(images) == 6
    for image in images:
        assert "@sha256:" in image, image
        assert ":latest" not in image and "${" not in image, image


def test_base_install_does_not_require_obs_extra() -> None:
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    base = " ".join(project["dependencies"])
    assert "opentelemetry" not in base
    assert any("opentelemetry-sdk" in item for item in project["optional-dependencies"]["obs"])
    code = (
        "import sys, lastro.cli.app, lastro.history, lastro.observability;"
        "print([m for m in sys.modules if m.startswith('opentelemetry.sdk')])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def test_redistributed_resources_have_provenance() -> None:
    resources = REPO_ROOT / "src" / "lastro" / "observability" / "resources"
    readme = (resources / "README.md").read_text(encoding="utf-8")
    compose = (resources / "compose.yaml").read_text(encoding="utf-8")
    notices = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert "github.com/langfuse/langfuse" in readme
    commit = readme.split("commit `", 1)[1].split("`", 1)[0]
    assert len(commit) == 40 and commit in notices
    for line in compose.splitlines():
        if "image:" in line:
            digest = line.split("@", 1)[1].strip()
            assert digest in readme, digest
