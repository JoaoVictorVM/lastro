import html
import re
import unicodedata
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lastro_demo import db
from lastro_demo.flags import FlagState
from lastro_demo.seed import seed_employees
from lastro_demo.validation import ROLES, only_digits

FormFn = Callable[..., dict[str, str]]
FindFn = Callable[[TestClient, str], int]

SAVED = "Funcionário salvo com sucesso."


def row(state_dir: Path, employee_id: int) -> dict[str, object]:
    with db.connection(state_dir / "demo.sqlite3") as conn:
        found = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
    assert found is not None
    return dict(found)


def listed_names(page: str) -> list[str]:
    found = re.findall(r'<td class="name"><a href="/employees/\d+">([^<]*)</a>', page)
    return [html.unescape(name) for name in found]


def test_create_valid_employee(client: TestClient, employee_form: FormFn) -> None:
    response = client.post("/employees", data=employee_form())

    assert response.status_code == 200
    assert SAVED in response.text
    assert "Fulana de Teste" in client.get("/employees").text
    employee_id = int(re.findall(r'data-employee-id="(\d+)"', response.text)[0])
    detail = client.get(f"/employees/{employee_id}")
    assert "529.982.247-25" in detail.text
    assert "Enfermeiro" in detail.text


def test_create_rejects_invalid_cpf_duplicate_and_bad_fields(
    client: TestClient, employee_form: FormFn
) -> None:
    bad = employee_form(
        full_name="Al",
        cpf="529.982.247-26",
        email="sem-arroba",
        phone="1234",
        role="",
        hire_date="",
    )

    response = client.post("/employees", data=bad)

    assert response.status_code == 200
    for message in (
        "O nome deve ter entre 3 e 120 caracteres.",
        "CPF inválido.",
        "E-mail inválido.",
        "Telefone inválido.",
        "O perfil é obrigatório.",
        "A data de admissão é obrigatória.",
    ):
        assert message in response.text
    assert 'value="sem-arroba"' in response.text
    assert SAVED not in response.text
    aurora_cpf = next(item.cpf for item in seed_employees(FlagState.all_off()))
    duplicate = client.post("/employees", data=employee_form(cpf=aurora_cpf))
    assert "CPF já cadastrado nesta clínica." in duplicate.text
    missing_email = client.post("/employees", data=employee_form(email=""))
    assert "O e-mail é obrigatório." in missing_email.text


def test_duplicate_cpf_by_digits_rejected(client: TestClient, employee_form: FormFn) -> None:
    formatted = next(
        item.cpf
        for item in seed_employees(FlagState.all_off())
        if item.clinic_id == "aurora" and "." in item.cpf
    )

    response = client.post("/employees", data=employee_form(cpf=only_digits(formatted)))

    assert "CPF já cadastrado nesta clínica." in response.text
    assert SAVED not in response.text


def test_same_cpf_allowed_in_other_clinic(
    make_client: Callable[..., TestClient], login: Callable[..., None], employee_form: FormFn
) -> None:
    boreal_cpf = next(
        item.cpf for item in seed_employees(FlagState.all_off()) if item.clinic_id == "boreal"
    )
    client = make_client()
    login(client, "aurora")

    response = client.post("/employees", data=employee_form(cpf=boreal_cpf))

    assert SAVED in response.text


def test_edit_persists_phone_and_email(
    client: TestClient, employee_form: FormFn, find_employee: FindFn, state_dir: Path
) -> None:
    employee_id = find_employee(client, "Ana Beatriz")
    form = employee_form(
        full_name="Ana Beatriz Moreira",
        cpf="954.255.503-24",
        email="ana.nova@example.com",
        phone="(11) 3222-1111",
        role="recepcionista",
        hire_date="2023-11-10",
    )

    response = client.post(f"/employees/{employee_id}", data=form)

    assert response.status_code == 200
    assert SAVED in response.text
    detail = client.get(f"/employees/{employee_id}").text
    assert "ana.nova@example.com" in detail
    assert "(11) 3222-1111" in detail
    stored = row(state_dir, employee_id)
    assert stored["email"] == "ana.nova@example.com"
    assert stored["phone"] == "(11) 3222-1111"


def test_edit_form_prefills_values(client: TestClient, find_employee: FindFn) -> None:
    employee_id = find_employee(client, "Ana Beatriz")

    page = client.get(f"/employees/{employee_id}/edit").text

    assert 'value="Ana Beatriz Moreira"' in page
    assert 'value="954.255.503-24"' in page
    assert '<option value="recepcionista" selected>' in page


def test_edit_rejects_future_hire_date(
    client: TestClient, employee_form: FormFn, find_employee: FindFn, state_dir: Path
) -> None:
    employee_id = find_employee(client, "Ana Beatriz")
    before = row(state_dir, employee_id)["hire_date"]
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    response = client.post(
        f"/employees/{employee_id}",
        data=employee_form(cpf="954.255.503-24", hire_date=tomorrow),
    )

    assert "A data de admissão não pode estar no futuro." in response.text
    assert row(state_dir, employee_id)["hire_date"] == before


def test_long_name_up_to_120_preserved(
    client: TestClient, employee_form: FormFn, state_dir: Path
) -> None:
    name = "Maria " + "Albuquerque " * 6 + "Silva"
    name = name[:80]
    assert len(name) == 80

    response = client.post("/employees", data=employee_form(full_name=name))

    employee_id = int(re.findall(r'data-employee-id="(\d+)"', response.text)[0])
    assert row(state_dir, employee_id)["full_name"] == name.strip()


def test_search_by_exact_cpf_scoped_to_tenant(client: TestClient) -> None:
    employees = seed_employees(FlagState.all_off())
    aurora_digits = {only_digits(item.cpf) for item in employees if item.clinic_id == "aurora"}
    boreal_only = next(
        item
        for item in employees
        if item.clinic_id == "boreal" and only_digits(item.cpf) not in aurora_digits
    )
    own = next(item for item in employees if item.clinic_id == "aurora")

    for query in (boreal_only.cpf, only_digits(boreal_only.cpf)):
        page = client.get("/employees", params={"q": query}).text
        assert listed_names(page) == []
        assert "Nenhum funcionário encontrado." in page
    assert listed_names(client.get("/employees", params={"q": only_digits(own.cpf)}).text) == [
        own.full_name
    ]


def test_search_by_name_ignores_case_and_accents(client: TestClient) -> None:
    page = client.get("/employees", params={"q": "joao pedro"}).text

    assert listed_names(page) == ["João Pedro Antunes"]


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def sort_keys(names: list[str], sort: str) -> list[str]:
    by_name = {
        item.full_name: item
        for item in seed_employees(FlagState.all_off())
        if item.clinic_id == "aurora"
    }
    keys: list[str] = []
    for name in names:
        item = by_name[name]
        value = {"role": ROLES[item.role], "hire_date": item.hire_date, "status": item.status}
        keys.append(fold(value.get(sort, name)))
    return keys


@pytest.mark.parametrize("sort", ["name", "role", "hire_date", "status"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_sort_orders_respected(client: TestClient, sort: str, direction: str) -> None:
    for _ in range(20):
        page = client.get("/employees", params={"sort": sort, "dir": direction}).text
        names = listed_names(page)
        assert len(names) == 20
        keys = sort_keys(names, sort)
        assert keys == sorted(keys, reverse=direction == "desc")


def test_htmx_request_returns_fragment(client: TestClient) -> None:
    response = client.get("/employees", params={"q": "Ana"}, headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'id="employee-count"' in response.text
    assert "Ana Beatriz Moreira" in response.text


def test_list_page_loads_versioned_htmx(client: TestClient) -> None:
    page = client.get("/employees").text

    assert '<script src="/static/htmx.min.js"' in page
    script = client.get("/static/htmx.min.js")
    assert script.status_code == 200
    assert 'version:"2.0.11"' in script.text
    assert "cdn" not in page.lower()


@pytest.mark.parametrize("role", list(ROLES))
def test_all_roles_save_successfully(client: TestClient, employee_form: FormFn, role: str) -> None:
    response = client.post("/employees", data=employee_form(role=role))

    assert response.status_code == 200
    assert SAVED in response.text
    assert ROLES[role] in response.text


def test_deactivate_via_status_field(
    client: TestClient, employee_form: FormFn, find_employee: FindFn, state_dir: Path
) -> None:
    employee_id = find_employee(client, "Ana Beatriz")
    form = employee_form(
        full_name="Ana Beatriz Moreira",
        cpf="954.255.503-24",
        email="ana.moreira@example.com",
        role="recepcionista",
        hire_date="2023-11-10",
        status="inactive",
    )

    response = client.post(f"/employees/{employee_id}", data=form)

    assert SAVED in response.text
    assert row(state_dir, employee_id)["status"] == "inactive"
    assert "Inativo" in client.get(f"/employees/{employee_id}").text
