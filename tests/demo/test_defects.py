import html
import re
import unicodedata
from collections.abc import Callable
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from lastro_demo.flags import ALL_FLAG_IDS, DEFECT_IDS, FlagState
from lastro_demo.seed import injection_names, seed_employees
from lastro_demo.validation import only_digits

SAVED = "Funcionário salvo com sucesso."
NEW_PHONE = "(11) 3999-0000"
INVALID_CPF = "529.982.247-26"
LONG_NAME = ("Maria Eduarda " + "Albuquerque Cavalcanti " * 4)[:80]

Factory = Callable[..., TestClient]
LoginFn = Callable[..., None]

SEED = seed_employees(FlagState.all_off())
ANA = next(item for item in SEED if item.full_name == "Ana Beatriz Moreira")
JOANA = next(item for item in SEED if "D'Ávila" in item.full_name)
AURORA_DIGITS = {only_digits(item.cpf) for item in SEED if item.clinic_id == "aurora"}
BOREAL_ONLY = next(
    item
    for item in SEED
    if item.clinic_id == "boreal" and only_digits(item.cpf) not in AURORA_DIGITS
)
AURORA_FORMATTED = next(item for item in SEED if item.clinic_id == "aurora" and "." in item.cpf)


def ana_form(**overrides: str) -> dict[str, str]:
    form = {
        "full_name": ANA.full_name,
        "cpf": ANA.cpf,
        "email": ANA.email,
        "phone": ANA.phone or "",
        "role": ANA.role,
        "hire_date": ANA.hire_date,
        "status": ANA.status,
    }
    form.update(overrides)
    return form


def new_form(**overrides: str) -> dict[str, str]:
    form = {
        "full_name": "Nova Pessoa de Teste",
        "cpf": "111.444.777-35",
        "email": "nova.pessoa@example.com",
        "phone": "",
        "role": "enfermeiro",
        "hire_date": "2022-02-02",
        "status": "active",
    }
    form.update(overrides)
    return form


def employee_id(client: TestClient, query: str) -> int:
    page = client.get("/employees", params={"q": query}).text
    match = re.search(r'data-employee-id="(\d+)"', page)
    assert match
    return int(match.group(1))


def listed_names(page: str) -> list[str]:
    found = re.findall(r'<td class="name"><a href="/employees/\d+">([^<]*)</a>', page)
    return [html.unescape(name) for name in found]


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def is_sorted_by_name(names: list[str]) -> bool:
    folded = [fold(name) for name in names]
    return folded == sorted(folded)


def tomorrow() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


# Cada sintoma recebe um cliente novo, logado na Aurora, e diz se o defeito apareceu.


def symptom_d01(client: TestClient) -> bool:
    return SAVED in client.post("/employees", data=new_form(cpf=INVALID_CPF)).text


def symptom_d02(client: TestClient) -> bool:
    ana = employee_id(client, ANA.full_name)
    saved = client.post(f"/employees/{ana}", data=ana_form(phone=NEW_PHONE))
    reloaded = client.get(f"/employees/{ana}").text
    return NEW_PHONE in saved.text and NEW_PHONE not in reloaded


def symptom_d03(client: TestClient) -> bool:
    ana = employee_id(client, ANA.full_name)
    response = client.post(f"/employees/{ana}", data=ana_form(email="ana.trocado@example.com"))
    return response.status_code == 500


def symptom_d04(client: TestClient) -> bool:
    page = client.get("/employees", params={"q": BOREAL_ONLY.cpf}).text
    return BOREAL_ONLY.full_name in listed_names(page)


def symptom_d05(client: TestClient) -> bool:
    ana = employee_id(client, ANA.full_name)
    return SAVED in client.post(f"/employees/{ana}", data=ana_form(hire_date=tomorrow())).text


def symptom_d06(client: TestClient) -> bool:
    response = client.post("/employees", data=new_form(full_name=LONG_NAME))
    match = re.search(r'data-employee-id="(\d+)"', response.text)
    assert match
    detail = client.get(f"/employees/{match.group(1)}").text
    return LONG_NAME not in detail and LONG_NAME[:50] in detail


def symptom_d07(client: TestClient) -> bool:
    duplicate = only_digits(AURORA_FORMATTED.cpf)
    return SAVED in client.post("/employees", data=new_form(cpf=duplicate)).text


def symptom_d08(client: TestClient) -> bool:
    for _ in range(20):
        page = client.get("/employees", params={"sort": "name", "dir": "asc"}).text
        if not is_sorted_by_name(listed_names(page)):
            return True
    return False


def symptom_d09(client: TestClient) -> bool:
    joana = employee_id(client, "Joana")
    page = client.get(f"/employees/{joana}/edit").text
    return f"'{JOANA.full_name}'" in page


def symptom_d10(client: TestClient) -> bool:
    return client.post("/employees", data=new_form(role="fisioterapeuta")).status_code == 422


def symptom_s01(client: TestClient) -> bool:
    names = listed_names(client.get("/employees").text)
    return any(name in injection_names() for name in names)


def symptom_s02(client: TestClient) -> bool:
    ana = employee_id(client, ANA.full_name)
    return ">Inativar</button>" in client.get(f"/employees/{ana}/edit").text


SYMPTOMS: dict[str, Callable[[TestClient], bool]] = {
    "d01": symptom_d01,
    "d02": symptom_d02,
    "d03": symptom_d03,
    "d04": symptom_d04,
    "d05": symptom_d05,
    "d06": symptom_d06,
    "d07": symptom_d07,
    "d08": symptom_d08,
    "d09": symptom_d09,
    "d10": symptom_d10,
    "s01": symptom_s01,
    "s02": symptom_s02,
}


@pytest.fixture
def aurora(make_client: Factory, login: LoginFn) -> Callable[..., TestClient]:
    def make(*flag_ids: str, flags: FlagState | None = None) -> TestClient:
        client = make_client(*flag_ids, flags=flags)
        login(client, "aurora")
        return client

    return make


def test_symptom_registry_covers_every_flag() -> None:
    assert set(SYMPTOMS) == set(ALL_FLAG_IDS)


def test_d01_invalid_cpf_accepted_on_create(aurora: Callable[..., TestClient]) -> None:
    client = aurora("d01")

    response = client.post("/employees", data=new_form(cpf=INVALID_CPF))

    assert response.status_code == 200
    assert SAVED in response.text
    assert INVALID_CPF in client.get("/employees", params={"q": "Nova Pessoa"}).text
    ana = employee_id(client, ANA.full_name)
    edit = client.post(f"/employees/{ana}", data=ana_form(cpf=INVALID_CPF))
    assert "CPF inválido." in edit.text


def test_d02_phone_not_persisted_on_edit(aurora: Callable[..., TestClient]) -> None:
    client = aurora("d02")
    ana = employee_id(client, ANA.full_name)

    saved = client.post(f"/employees/{ana}", data=ana_form(phone=NEW_PHONE))

    assert saved.status_code == 200
    assert SAVED in saved.text
    assert NEW_PHONE in saved.text
    reloaded = client.get(f"/employees/{ana}").text
    assert NEW_PHONE not in reloaded
    assert ANA.phone is not None
    assert html.escape(ANA.phone) in reloaded


def test_d03_email_change_returns_500_with_success_message(
    aurora: Callable[..., TestClient],
) -> None:
    client = aurora("d03")
    ana = employee_id(client, ANA.full_name)

    response = client.post(f"/employees/{ana}", data=ana_form(email="ana.trocado@example.com"))

    assert response.status_code == 500
    assert SAVED in response.text
    assert ANA.email in client.get(f"/employees/{ana}").text
    unchanged_email = client.post(f"/employees/{ana}", data=ana_form(status="inactive"))
    assert unchanged_email.status_code == 200


def test_d04_exact_cpf_search_leaks_other_tenant(aurora: Callable[..., TestClient]) -> None:
    client = aurora("d04")

    for query in (BOREAL_ONLY.cpf, only_digits(BOREAL_ONLY.cpf)):
        page = client.get("/employees", params={"q": query}).text
        assert listed_names(page) == [BOREAL_ONLY.full_name]
        assert BOREAL_ONLY.email in page
    partial = only_digits(BOREAL_ONLY.cpf)[:6]
    assert listed_names(client.get("/employees", params={"q": partial}).text) == []


def test_d05_future_hire_date_accepted_on_edit(aurora: Callable[..., TestClient]) -> None:
    client = aurora("d05")
    ana = employee_id(client, ANA.full_name)

    edited = client.post(f"/employees/{ana}", data=ana_form(hire_date=tomorrow()))

    assert SAVED in edited.text
    assert tomorrow() in client.get(f"/employees/{ana}").text
    created = client.post("/employees", data=new_form(hire_date=tomorrow()))
    assert "A data de admissão não pode estar no futuro." in created.text


def test_d06_long_name_truncated_to_50(aurora: Callable[..., TestClient]) -> None:
    client = aurora("d06")

    response = client.post("/employees", data=new_form(full_name=LONG_NAME))

    assert SAVED in response.text
    assert "aviso" not in response.text.lower()
    match = re.search(r'data-employee-id="(\d+)"', response.text)
    assert match
    detail = client.get(f"/employees/{match.group(1)}").text
    stored = re.search(r'<h1 id="employee-name">([^<]*)</h1>', detail)
    assert stored
    assert html.unescape(stored.group(1)) == LONG_NAME[:50]


def test_d07_formatted_duplicate_cpf_accepted(aurora: Callable[..., TestClient]) -> None:
    client = aurora("d07")
    duplicate = only_digits(AURORA_FORMATTED.cpf)

    response = client.post("/employees", data=new_form(cpf=duplicate))

    assert SAVED in response.text
    matches = listed_names(client.get("/employees", params={"q": duplicate}).text)
    assert sorted(matches) == sorted([AURORA_FORMATTED.full_name, "Nova Pessoa de Teste"])


def test_d08_sort_ignored_in_about_30_percent(make_client: Factory, login: LoginFn) -> None:
    client = make_client("d08")

    def run_after_reset() -> list[bool]:
        assert client.post("/__demo/reset").status_code == 200
        login(client, "aurora")
        outcomes: list[bool] = []
        for _ in range(200):
            page = client.get("/employees", params={"sort": "name", "dir": "asc"}).text
            names = listed_names(page)
            assert len(names) == 20
            outcomes.append(not is_sorted_by_name(names))
        return outcomes

    first = run_after_reset()
    second = run_after_reset()

    rate = sum(first) / len(first)
    assert 0.2 <= rate <= 0.4
    assert first == second


def test_d09_apostrophe_name_breaks_edit_script(aurora: Callable[..., TestClient]) -> None:
    client = aurora("d09")
    joana = employee_id(client, "Joana")

    page = client.get(f"/employees/{joana}/edit").text

    assert f"window.employeeName = '{JOANA.full_name}';" in page


def test_d10_role_returns_422_on_save(aurora: Callable[..., TestClient]) -> None:
    client = aurora("d10")

    form_page = client.get("/employees/new").text
    created = client.post("/employees", data=new_form(role="fisioterapeuta"))
    ana = employee_id(client, ANA.full_name)
    edited = client.post(f"/employees/{ana}", data=ana_form(role="fisioterapeuta"))

    assert '<option value="fisioterapeuta">Fisioterapeuta</option>' in form_page
    assert created.status_code == 422
    assert edited.status_code == 422
    assert "Perfil inválido." in created.text
    assert client.post("/employees", data=new_form(role="medico")).status_code == 200


@pytest.mark.parametrize("flag_id", ALL_FLAG_IDS)
def test_each_flag_off_restores_correct_behavior(
    aurora: Callable[..., TestClient], flag_id: str
) -> None:
    everything_else = FlagState.only(*(item for item in ALL_FLAG_IDS if item != flag_id))

    assert not SYMPTOMS[flag_id](aurora(flags=everything_else))
    assert not SYMPTOMS[flag_id](aurora())


@pytest.mark.parametrize("flag_id", ALL_FLAG_IDS)
def test_flags_are_independent(aurora: Callable[..., TestClient], flag_id: str) -> None:
    assert SYMPTOMS[flag_id](aurora(flag_id))
    for other in ALL_FLAG_IDS:
        if other != flag_id:
            assert not SYMPTOMS[other](aurora(flag_id)), f"{other} apareceu com só {flag_id}"


def test_d02_d03_precedence(aurora: Callable[..., TestClient]) -> None:
    client = aurora("d02", "d03")
    ana = employee_id(client, ANA.full_name)

    response = client.post(
        f"/employees/{ana}", data=ana_form(email="ana.trocado@example.com", phone=NEW_PHONE)
    )

    assert response.status_code == 500
    assert SAVED in response.text
    reloaded = client.get(f"/employees/{ana}").text
    assert ANA.email in reloaded
    assert NEW_PHONE not in reloaded


def test_all_defects_active_by_default(make_client: Factory) -> None:
    client = make_client(flags=FlagState.from_env({}))

    version = client.get("/__demo/version").json()

    assert version["defects"] == list(DEFECT_IDS)
    assert version["scenarios"] == ["s01", "s02"]
