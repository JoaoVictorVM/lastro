import itertools
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lastro_demo.flags import FlagState, variant_parameter

LONG_NAME = "Maria " + "Silva " * 10 + "Souza"
NAME_45 = "Ana Beatriz " + "Teixeira " * 3 + "Lima Costa"


def valid_cpfs() -> Iterator[str]:
    """CPFs com dígitos verificadores válidos e fora do seed."""
    for base in itertools.count(100_000_001):
        digits = [int(char) for char in f"{base:09d}"]
        for size in (9, 10):
            total = sum(value * (size + 1 - index) for index, value in enumerate(digits[:size]))
            digits.append((total * 10 % 11) % 10)
        yield "".join(str(value) for value in digits)


CPFS = valid_cpfs()


def new_form(**overrides: str) -> dict[str, str]:
    cpf = next(CPFS)
    form = {
        "full_name": "Nova Pessoa de Teste",
        "cpf": cpf,
        "email": f"p{cpf}@example.com",
        "phone": "",
        "role": "enfermeiro",
        "hire_date": "2022-02-02",
        "status": "active",
    }
    form.update(overrides)
    return form


@pytest.fixture
def variant_client(
    make_client: Callable[..., TestClient], login: Callable[..., None]
) -> Callable[..., TestClient]:
    def make(variant: str, *defects: str) -> TestClient:
        client = make_client(flags=FlagState(frozenset(defects), frozenset(), variant))
        login(client, "aurora")
        return client

    return make


def stored_name(client: TestClient, name: str) -> str:
    response = client.post("/employees", data=new_form(full_name=name))
    match = re.search(r'data-employee-id="(\d+)"', response.text)
    assert match, response.text[:300]
    detail = client.get(f"/employees/{match.group(1)}").text
    for candidate in (name, name[:50], name[:40]):
        if candidate in detail:
            return candidate
    raise AssertionError("nome não encontrado no detalhe")


def save_status(client: TestClient, role: str) -> int:
    return client.post("/employees", data=new_form(role=role)).status_code


def test_shifted_d06_changes_real_truncation(variant_client: Callable[..., TestClient]) -> None:
    base = variant_client("base", "d06")
    shifted = variant_client("shifted", "d06")
    assert len(LONG_NAME) > 50 and 40 < len(NAME_45) <= 50
    assert stored_name(base, LONG_NAME) == LONG_NAME[:50]
    assert stored_name(shifted, LONG_NAME) == LONG_NAME[:40]
    # Controle negativo do gatilho antigo: até 50 caracteres a base preserva o nome.
    assert stored_name(base, NAME_45) == NAME_45
    assert stored_name(shifted, NAME_45) == NAME_45[:40]


def test_shifted_d10_changes_rejected_role(variant_client: Callable[..., TestClient]) -> None:
    base = variant_client("base", "d10")
    shifted = variant_client("shifted", "d10")
    assert save_status(base, "fisioterapeuta") == 422
    assert save_status(base, "tecnico_enfermagem") == 200
    assert save_status(shifted, "tecnico_enfermagem") == 422
    assert save_status(shifted, "fisioterapeuta") == 200


@pytest.mark.parametrize("variant", ["base", "shifted"])
def test_disabled_defects_are_correct_in_both_variants(
    variant_client: Callable[..., TestClient], variant: str
) -> None:
    client = variant_client(variant)
    assert stored_name(client, LONG_NAME) == LONG_NAME
    assert save_status(client, "fisioterapeuta") == 200
    assert save_status(client, "tecnico_enfermagem") == 200


def test_catalog_matches_variant_behavior() -> None:
    from lastro.evaluation.judge import load_catalog

    labels = {"fisioterapeuta": "Fisioterapeuta", "tecnico_enfermagem": "Tecnico de enfermagem"}
    for variant in ("base", "shifted"):
        catalog = {entry.id: entry for entry in load_catalog(variant)}
        length = int(variant_parameter(variant, "d06_truncate_length"))
        assert f"cortado em {length} caracteres" in catalog["D06"].expected_symptom
        role = str(variant_parameter(variant, "d10_rejected_role"))
        assert labels[role] in catalog["D10"].trigger


def test_log_context_comes_from_server(variant_client: Callable[..., TestClient]) -> None:
    client = variant_client("base")
    client.get("/employees", headers={"X-Demo-Request-Id": "forged"})
    client.post("/employees", data=new_form())
    log = Path(client.app.state.access_log.path)  # type: ignore[attr-defined]
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    listing = [item for item in entries if item["path"] == "/employees"]
    assert listing[0]["request_id"] != "forged" and listing[0]["tenant"] == "aurora"
    assert listing[-1]["action"] == "create"
    text = json.dumps(entries).lower()
    assert "password" not in text and "cookie" not in text and "fulana" not in text
