from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from lastro_demo.config import SeedUser

LoginFn = Callable[..., None]


def test_login_form_has_stable_selectors(make_client: Callable[..., TestClient]) -> None:
    page = make_client().get("/login").text

    assert 'name="username"' in page
    assert 'name="password"' in page
    assert 'type="submit"' in page


def test_login_single_clinic_user_lands_on_list(
    make_client: Callable[..., TestClient], seed_users: dict[str, SeedUser]
) -> None:
    client = make_client()
    user = seed_users["aurora"]

    response = client.post(
        "/login", data={"username": user.email, "password": user.password}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/employees"
    assert client.get("/employees").status_code == 200
    assert client.get("/api/me").json() == {
        "user": {"email": "aurora.teste@example.com", "name": "Usuário Teste Aurora"},
        "tenant": {"id": "aurora", "name": "Clínica Aurora"},
    }


def test_login_wrong_password_rejected(
    make_client: Callable[..., TestClient], seed_users: dict[str, SeedUser]
) -> None:
    client = make_client()

    response = client.post(
        "/login", data={"username": seed_users["aurora"].email, "password": "errada"}
    )

    assert "Usuário ou senha inválidos." in response.text
    assert client.get("/api/me").status_code == 401
    assert client.get("/employees", follow_redirects=False).headers["location"] == "/login"


def test_credentials_come_from_environment(
    make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setenv("DEMO_BOREAL_USER", "outro.boreal@example.com")
    monkeypatch.setenv("DEMO_BOREAL_PASSWORD", "outra-senha-falsa")
    client = make_client()

    response = client.post(
        "/login", data={"username": "outro.boreal@example.com", "password": "outra-senha-falsa"}
    )

    assert response.status_code == 200
    assert client.get("/api/me").json()["tenant"]["id"] == "boreal"


def test_multi_clinic_user_must_select(
    make_client: Callable[..., TestClient], seed_users: dict[str, SeedUser]
) -> None:
    client = make_client()
    user = seed_users["multi"]

    response = client.post(
        "/login", data={"username": user.email, "password": user.password}, follow_redirects=False
    )

    assert response.headers["location"] == "/select-clinic"
    pending = client.get("/api/me")
    assert pending.status_code == 409
    assert pending.json()["error"] == "tenant_not_selected"
    assert client.get("/employees", follow_redirects=False).headers["location"] == "/select-clinic"
    page = client.get("/select-clinic").text
    assert 'name="tenant"' in page
    assert 'value="aurora"' in page
    assert 'value="boreal"' in page

    client.post("/select-clinic", data={"tenant": "boreal"})

    assert client.get("/api/me").json()["tenant"] == {"id": "boreal", "name": "Clínica Boreal"}
    client.post("/select-clinic", data={"tenant": "aurora"})
    assert client.get("/api/me").json()["tenant"]["id"] == "aurora"


def test_cannot_select_unlinked_clinic(client: TestClient) -> None:
    response = client.post("/select-clinic", data={"tenant": "boreal"})

    assert response.status_code == 403
    assert "Você não tem acesso a esta clínica." in response.text
    assert client.get("/api/me").json()["tenant"]["id"] == "aurora"


def test_cross_tenant_id_returns_404(
    make_client: Callable[..., TestClient],
    login: LoginFn,
    find_employee: Callable[[TestClient, str], int],
) -> None:
    boreal = make_client()
    login(boreal, "boreal")
    boreal_id = find_employee(boreal, "Lúcia Helena")
    aurora = make_client()
    login(aurora, "aurora")

    assert aurora.get(f"/employees/{boreal_id}").status_code == 404
    assert aurora.get(f"/employees/{boreal_id}/edit").status_code == 404
    update = aurora.post(f"/employees/{boreal_id}", data={"full_name": "Invasor"})
    assert update.status_code == 404
    assert aurora.post(f"/employees/{boreal_id}/deactivate").status_code == 404
    assert "Lúcia Helena" in boreal.get(f"/employees/{boreal_id}").text
    assert "Lúcia Helena" not in aurora.get("/employees").text


def test_api_me_unauthenticated_401(make_client: Callable[..., TestClient]) -> None:
    response = make_client().get("/api/me")

    assert response.status_code == 401
    assert response.json()["error"] == "not_authenticated"


def test_logout_ends_session(client: TestClient) -> None:
    response = client.post("/logout", follow_redirects=False)

    assert response.headers["location"] == "/login"
    assert client.get("/api/me").status_code == 401


def test_root_redirects_by_session(make_client: Callable[..., TestClient], login: LoginFn) -> None:
    client = make_client()

    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    login(client)
    assert client.get("/", follow_redirects=False).headers["location"] == "/employees"
