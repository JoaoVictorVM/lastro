import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lastro_demo.app import create_app
from lastro_demo.config import DemoConfig, SeedUser, load_seed_users
from lastro_demo.flags import FlagState

LOOPBACK_CLIENT = ("127.0.0.1", 50000)


@pytest.fixture
def seed_users() -> dict[str, SeedUser]:
    return {user.key: user for user in load_seed_users({})}


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def make_client(state_dir: Path) -> Iterator[Callable[..., TestClient]]:
    """Fábrica de clientes HTTP em memória; sem argumentos, todas as flags ficam desligadas."""
    clients: list[TestClient] = []

    def make(*flag_ids: str, flags: FlagState | None = None) -> TestClient:
        active = flags if flags is not None else FlagState.only(*flag_ids)
        app = create_app(DemoConfig(state_dir=state_dir), active)
        client = TestClient(app, client=LOOPBACK_CLIENT)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def login(seed_users: dict[str, SeedUser]) -> Callable[..., None]:
    def do_login(client: TestClient, key: str = "aurora", tenant: str | None = None) -> None:
        user = seed_users[key]
        response = client.post("/login", data={"username": user.email, "password": user.password})
        assert response.status_code == 200, response.text
        if tenant is not None:
            selected = client.post("/select-clinic", data={"tenant": tenant})
            assert selected.status_code == 200, selected.text

    return do_login


@pytest.fixture
def client(make_client: Callable[..., TestClient], login: Callable[..., None]) -> TestClient:
    """Cliente com todas as flags desligadas e logado como o usuário da Aurora."""
    demo = make_client()
    login(demo, "aurora")
    return demo


@pytest.fixture
def find_employee() -> Callable[[TestClient, str], int]:
    def find(client: TestClient, name: str) -> int:
        page = client.get("/employees", params={"q": name}).text
        match = re.search(r'data-employee-id="(\d+)"', page)
        assert match, f"funcionário {name!r} não encontrado na lista"
        return int(match.group(1))

    return find


@pytest.fixture
def employee_form() -> Callable[..., dict[str, str]]:
    def build(**overrides: str) -> dict[str, str]:
        form = {
            "full_name": "Fulana de Teste",
            "cpf": "529.982.247-25",
            "email": "fulana.teste@example.com",
            "phone": "(11) 91234-5678",
            "role": "enfermeiro",
            "hire_date": "2021-05-10",
            "status": "active",
        }
        form.update(overrides)
        return form

    return build
