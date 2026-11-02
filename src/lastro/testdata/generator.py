"""Geração reproduzível de dados fictícios brasileiros, isolada por chamada."""

import hashlib
import json
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from faker import Faker

from lastro.testdata.models import (
    GeneratedData,
    GeneratedEntity,
    GeneratedValue,
    TestDataRequest,
)

SEED_VERSION = 1
MAX_DATE_OFFSET_DAYS = 365
# DDDs em uso no Brasil; nenhum começa ou termina com zero, como exige a validação do demo.
AREA_CODES = (
    *range(11, 20),
    21,
    22,
    24,
    27,
    28,
    *range(31, 36),
    37,
    38,
    *range(41, 47),
    *range(47, 50),
    51,
    *range(53, 56),
    *range(61, 70),
    71,
    *range(73, 76),
    77,
    79,
    *range(81, 90),
    *range(91, 100),
)


def run_marker(run_id: str) -> str:
    return f"LASTRO-{run_id[-4:]}-"


def run_email(run_id: str) -> str:
    return f"lastro+{run_id}@example.com"


def reference_date(moment: datetime, timezone: str) -> date:
    """Data-base no fuso do projeto; a seed nunca a altera."""
    return moment.astimezone(ZoneInfo(timezone)).date()


def derive_seed(run_id: str, generation_index: int, request: TestDataRequest) -> int:
    envelope = {
        "version": SEED_VERSION,
        "run_id": run_id,
        "generation_index": generation_index,
        "request": request.model_dump(mode="json"),
    }
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    return int.from_bytes(hashlib.sha256(canonical.encode("utf-8")).digest()[:8], "big")


def cpf_check_digit(digits: list[int]) -> int:
    size = len(digits)
    total = sum(
        digit * weight for digit, weight in zip(digits, range(size + 1, 1, -1), strict=True)
    )
    return (total * 10) % 11 % 10


def cpf(rand: random.Random, *, valid: bool = True) -> str:
    body = [rand.randint(0, 9) for _ in range(9)]
    while len(set(body)) == 1:
        body = [rand.randint(0, 9) for _ in range(9)]
    first = cpf_check_digit(body)
    second = cpf_check_digit([*body, first])
    if not valid:
        second = (second + rand.randint(1, 9)) % 10
    digits = "".join(str(digit) for digit in [*body, first, second])
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def phone(rand: random.Random) -> str:
    area = rand.choice(AREA_CODES)
    number = "".join(str(rand.randint(0, 9)) for _ in range(8))
    return f"({area}) 9{number[:4]}-{number[4:]}"


def text(fake: Faker, length: int) -> str:
    words: list[str] = []
    while len(" ".join(words)) < length:
        words.append(fake.word())
    value = " ".join(words)[:length]
    if value.endswith(" "):
        value = value[:-1] + "a"
    return value


def shifted(rand: random.Random, reference: date, direction: int) -> date:
    return reference + timedelta(days=direction * rand.randint(1, MAX_DATE_OFFSET_DAYS))


def generate(
    request: TestDataRequest, *, run_id: str, generation_index: int, reference_date: date
) -> GeneratedData:
    """Uma instância Faker local por chamada; o gerador global nunca é alterado."""
    fake = Faker("pt_BR")
    fake.seed_instance(derive_seed(run_id, generation_index, request))
    rand: random.Random = fake.random
    marker = run_marker(run_id)
    variant = request.resolved_variant
    reference = reference_date.isoformat()
    if request.entity is not None:
        values = {
            "name": f"{marker}{fake.first_name()} {fake.last_name()}",
            "cpf": cpf(rand, valid=variant != "invalid_cpf"),
            "email": run_email(run_id),
            "phone": phone(rand),
            "hire_date": shifted(rand, reference_date, -1).isoformat(),
        }
        return GeneratedEntity(
            entity=request.entity,
            variant=variant,
            generation_index=generation_index,
            reference_date=reference,
            marker=marker,
            values=values,
        )
    kind = request.type or ""
    if kind == "name":
        value = f"{marker}{fake.first_name()} {fake.last_name()}"
    elif kind == "cpf":
        value = cpf(rand, valid=variant == "valid")
    elif kind == "email":
        value = run_email(run_id)
    elif kind == "phone":
        value = phone(rand)
    elif kind == "text":
        value = text(fake, request.length or 0)
    elif variant == "boundary":
        base = date.fromisoformat(request.boundary_date or "")
        value = (base + timedelta(days=request.offset_days or 0)).isoformat()
    elif variant == "today":
        value = reference
    else:
        value = shifted(rand, reference_date, -1 if variant == "past" else 1).isoformat()
    return GeneratedValue(
        type=kind,
        variant=variant,
        generation_index=generation_index,
        reference_date=reference,
        value=value,
    )
