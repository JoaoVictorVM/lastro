import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Capturado na coleta, antes de o conftest isolar HOME e LOCALAPPDATA: o uv precisa do próprio
# cache para instalar sem rede.
HOST_ENV = dict(os.environ)
PACKAGE_DATA = (
    "lastro_demo/.env.example",
    "lastro_demo/seed/employees.json",
    "lastro_demo/seed/clinics.json",
    "lastro_demo/static/htmx.min.js",
    "lastro_demo/static/app.css",
    "lastro_demo/templates/base.html",
    "lastro_demo/templates/login.html",
    "lastro_demo/templates/select_clinic.html",
    "lastro_demo/templates/employees_list.html",
    "lastro_demo/templates/_employee_results.html",
    "lastro_demo/templates/employee_form.html",
    "lastro_demo/templates/employee_detail.html",
    "lastro_demo/templates/not_found.html",
    "lastro_demo/runner.py",
    "lastro_demo/__main__.py",
)


def uv_executable() -> str:
    found = HOST_ENV.get("UV") or shutil.which("uv", path=HOST_ENV.get("PATH"))
    if not found:
        pytest.skip("uv não encontrado; rode a suíte com uv run pytest")
    return found


def uv(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [uv_executable(), *args],
        cwd=cwd,
        env=HOST_ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=600,
    )


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_dir = tmp_path_factory.mktemp("wheel")
    result = uv("build", "--wheel", "--out-dir", str(out_dir), cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    (built,) = out_dir.glob("lastro-*.whl")
    return built


def test_wheel_contains_package_data(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    for expected in PACKAGE_DATA:
        assert expected in names, expected
    assert not any("evals/" in name or "demo-bugs" in name for name in names)
    assert not any(name.startswith("tests/") for name in names)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_installed_app_serves_from_other_cwd(wheel: Path, tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    created = uv("venv", str(venv), "--python", sys.executable, "--quiet", cwd=tmp_path)
    assert created.returncode == 0, created.stderr
    scripts = venv / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    installed = uv("pip", "install", "--python", str(python), str(wheel), "--quiet", cwd=tmp_path)
    assert installed.returncode == 0, installed.stderr
    elsewhere = tmp_path / "outro-diretorio"
    elsewhere.mkdir()
    state_dir = tmp_path / "estado"
    port = free_port()
    env = {**HOST_ENV, "PYTHONIOENCODING": "utf-8"}
    env.pop("VIRTUAL_ENV", None)
    script = (
        "import json, sys\n"
        "from lastro_demo import runner\n"
        "state = runner.start(state_dir=sys.argv[1], port=int(sys.argv[2]))\n"
        "print(json.dumps({'url': state.url, 'ready': state.ready_message}))\n"
    )
    started = subprocess.run(
        [str(python), "-P", "-c", script, str(state_dir), str(port)],
        cwd=elsewhere,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    try:
        assert started.returncode == 0, started.stderr
        assert "Demo ready: 2 clinics, 40 employees, 10 defects active" in started.stdout
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            login = client.post(
                "/login",
                data={"username": "aurora.teste@example.com", "password": "senha-falsa-aurora-123"},
                follow_redirects=True,
            )
            assert login.status_code == 200
            page = client.get("/employees")
            assert page.status_code == 200
            assert page.text.count('data-employee-id="') == 20
            assert client.get("/static/htmx.min.js").status_code == 200
    finally:
        stopped = subprocess.run(
            [
                str(python),
                "-P",
                "-c",
                "import sys\nfrom lastro_demo import runner\n"
                "print(runner.stop(state_dir=sys.argv[1]))",
                str(state_dir),
            ],
            cwd=elsewhere,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
    assert stopped.returncode == 0, stopped.stderr
    assert "was_running=True" in stopped.stdout
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (state_dir / "server.pid").exists():
        time.sleep(0.1)
    assert not (state_dir / "server.pid").exists()
