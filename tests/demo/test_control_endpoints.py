import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lastro_demo import __version__, db
from lastro_demo.app import create_app
from lastro_demo.config import DemoConfig
from lastro_demo.flags import DEFECT_IDS, FlagState

Factory = Callable[..., TestClient]
LoginFn = Callable[..., None]
FindFn = Callable[[TestClient, str], int]
FormFn = Callable[..., dict[str, str]]


def snapshot(state_dir: Path) -> list[tuple[object, ...]]:
    with db.connection(state_dir / "demo.sqlite3") as conn:
        return [tuple(row) for row in conn.execute("SELECT * FROM employees ORDER BY id")]


def test_reset_restores_seed_after_mutations(
    make_client: Factory,
    login: LoginFn,
    find_employee: FindFn,
    employee_form: FormFn,
    state_dir: Path,
) -> None:
    client = make_client()
    login(client)
    seed = snapshot(state_dir)
    ana = find_employee(client, "Ana Beatriz")
    client.post("/employees", data=employee_form())
    client.post(
        f"/employees/{ana}", data=employee_form(full_name="Nome Trocado", cpf="11144477735")
    )
    client.post(f"/employees/{find_employee(client, 'Bruno')}/deactivate")
    with db.connection(state_dir / "demo.sqlite3") as conn:
        conn.execute("DELETE FROM employees WHERE id = ?", (find_employee(client, "Thiago"),))
    assert snapshot(state_dir) != seed

    response = client.post("/__demo/reset")

    assert response.status_code == 200
    assert response.json()["clinics"] == 2
    assert response.json()["employees"] == 40
    assert snapshot(state_dir) == seed
    assert client.get("/api/me").status_code == 401
    login(client)
    assert "Ana Beatriz Moreira" in client.get(f"/employees/{ana}").text


def test_reset_under_two_seconds(make_client: Factory) -> None:
    client = make_client(flags=FlagState.all_on())

    for _ in range(3):
        response = client.post("/__demo/reset")
        assert response.status_code == 200
        assert response.json()["duration_ms"] < 2000


def test_reset_with_flags_replaces_active_set(make_client: Factory) -> None:
    client = make_client(flags=FlagState.all_on())

    response = client.post("/__demo/reset", json={"defects": ["d04", "d01"], "scenarios": []})

    assert response.status_code == 200
    body = response.json()
    assert body["defects"] == ["d01", "d04"]
    assert body["scenarios"] == []
    assert body["variant"] == "base"
    assert body["status"] == "ok"
    assert isinstance(body["duration_ms"], int)
    version = client.get("/__demo/version").json()
    assert version["defects"] == ["d01", "d04"]
    assert version["scenarios"] == []


def test_reset_with_scenario_reloads_seed_variation(make_client: Factory, login: LoginFn) -> None:
    client = make_client()

    client.post("/__demo/reset", json={"scenarios": ["s01"]})
    login(client)

    assert "ASSISTENTE" in client.get("/employees").text


def test_reset_without_body_keeps_flags(make_client: Factory) -> None:
    client = make_client("d02", "s02")

    response = client.post("/__demo/reset")

    assert response.json()["defects"] == ["d02"]
    assert response.json()["scenarios"] == ["s02"]
    partial = client.post("/__demo/reset", json={"scenarios": []})
    assert partial.json()["defects"] == ["d02"]
    assert partial.json()["scenarios"] == []


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"defects": ["d01", "d99"]}, "unknown_flag"),
        ({"scenarios": ["d01"]}, "unknown_flag"),
        ({"variant": "campo-alternativo"}, "unknown_variant"),
        ({"defects": "d01"}, "invalid_body"),
        ({"flags": ["d01"]}, "invalid_body"),
        (["d01"], "invalid_body"),
    ],
)
def test_reset_unknown_flag_422_no_change(
    make_client: Factory, login: LoginFn, body: object, code: str
) -> None:
    client = make_client("d03")
    login(client)

    response = client.post("/__demo/reset", content=json.dumps(body))

    assert response.status_code == 422
    assert response.json()["error"] == code
    assert client.get("/__demo/version").json()["defects"] == ["d03"]
    assert client.get("/api/me").status_code == 200


def test_reset_invalid_json_422(make_client: Factory) -> None:
    response = make_client().post("/__demo/reset", content=b"{nao e json")

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_body"


def test_reset_keeps_access_log(make_client: Factory, state_dir: Path) -> None:
    client = make_client()
    client.get("/login")
    log = state_dir / "access.log"
    before = log.read_text(encoding="utf-8").splitlines()

    client.post("/__demo/reset")
    client.get("/login")

    after = log.read_text(encoding="utf-8").splitlines()
    assert after[: len(before)] == before
    assert len(after) == len(before) + 2
    entry = json.loads(after[-2])
    assert entry["method"] == "POST"
    assert entry["path"] == "/__demo/reset"
    assert entry["status"] == 200


def test_access_log_survives_restart(state_dir: Path) -> None:
    first = TestClient(create_app(DemoConfig(state_dir=state_dir)), client=("127.0.0.1", 1))
    first.get("/login")
    second = TestClient(create_app(DemoConfig(state_dir=state_dir)), client=("127.0.0.1", 1))
    second.get("/login")

    lines = (state_dir / "access.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_version_contract(make_client: Factory) -> None:
    client = make_client(flags=FlagState.all_on())

    body = client.get("/__demo/version").json()

    assert set(body) == {"version", "variant", "defects", "scenarios"}
    assert body["version"] == __version__
    assert isinstance(body["version"], str)
    assert body["variant"] == "base"
    assert body["defects"] == list(DEFECT_IDS)
    assert body["scenarios"] == ["s01", "s02"]


def test_control_endpoints_only_on_loopback(state_dir: Path) -> None:
    remote = TestClient(create_app(DemoConfig(state_dir=state_dir)), client=("192.168.0.20", 1))

    assert remote.get("/__demo/version").status_code == 403
    assert remote.post("/__demo/reset").status_code == 403
    ipv6 = TestClient(create_app(DemoConfig(state_dir=state_dir)), client=("::1", 1))
    assert ipv6.get("/__demo/version").status_code == 200
