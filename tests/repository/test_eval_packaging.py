import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST_ENV = dict(os.environ)
CATALOG = REPO_ROOT / "evals" / "catalog" / "demo-bugs.yaml"
PACKAGED = "lastro/evaluation/resources/catalog/demo-bugs.yaml"
RESOURCES = [
    "lastro/evaluation/resources/suites/demo-core.yaml",
    "lastro/evaluation/resources/suites/demo-scenarios.yaml",
    "lastro/evaluation/resources/suites/demo-memory.yaml",
    "lastro/evaluation/resources/judge-rubric.md",
    "lastro/evaluation/resources/missions/demo-crud.md",
    "lastro/evaluation/resources/demo-profile.toml.tmpl",
]


def uv() -> str:
    found = HOST_ENV.get("UV") or shutil.which("uv", path=HOST_ENV.get("PATH"))
    if not found:
        pytest.skip("uv não encontrado; rode a suíte com uv run pytest")
    return found


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [uv(), "build", "--out-dir", str(out)],
        cwd=REPO_ROOT,
        env=HOST_ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr
    (wheel,) = out.glob("lastro-*.whl")
    (sdist,) = out.glob("lastro-*.tar.gz")
    return wheel, sdist


def test_wheel_contains_suites_and_private_evaluator_catalog(built: tuple[Path, Path]) -> None:
    wheel, sdist = built
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for expected in [*RESOURCES, PACKAGED]:
        assert expected in names, expected
    assert not any(name.startswith("lastro_demo/") and "demo-bugs" in name for name in names)
    with tarfile.open(sdist) as archive:
        members = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    assert "hatch_build.py" in members and "evals/catalog/demo-bugs.yaml" in members


def test_packaged_catalog_matches_canonical_hash(built: tuple[Path, Path]) -> None:
    wheel, _ = built
    with zipfile.ZipFile(wheel) as archive:
        packaged = archive.read(PACKAGED)
    assert hashlib.sha256(packaged).hexdigest() == hashlib.sha256(CATALOG.read_bytes()).hexdigest()


def test_demo_never_imports_or_serves_catalog() -> None:
    demo = REPO_ROOT / "src" / "lastro_demo"
    for path in demo.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "demo-bugs" not in text and "lastro.evaluation" not in text, path
    assert not list(demo.rglob("*.yaml"))


def test_installed_suite_resolves_without_checkout(
    built: tuple[Path, Path], tmp_path: Path
) -> None:
    wheel, _ = built
    venv = tmp_path / "venv"
    created = subprocess.run(
        [uv(), "venv", str(venv), "--python", sys.executable, "--quiet"],
        cwd=tmp_path,
        env=HOST_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    installed = subprocess.run(
        [uv(), "pip", "install", "--python", str(python), f"{wheel}[eval]", "--quiet"],
        cwd=tmp_path,
        env=HOST_ENV,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert installed.returncode == 0, installed.stderr
    code = (
        "import hashlib;"
        "from lastro.evaluation.suites import load_suite, expand_slots, catalog_bytes, "
        "canonical_catalog_path;"
        "s = load_suite('demo-core');"
        "assert canonical_catalog_path() is None;"
        "print(len(expand_slots(s.spec)), hashlib.sha256(catalog_bytes()).hexdigest())"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        [str(python), "-I", "-c", code],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    count, digest = result.stdout.split()
    assert count == "30"
    assert digest == hashlib.sha256(CATALOG.read_bytes()).hexdigest()
