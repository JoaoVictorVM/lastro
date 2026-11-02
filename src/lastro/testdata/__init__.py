"""Dados fictícios para preenchimento, marcados pela run que os gerou."""

from lastro.testdata.generator import (
    derive_seed,
    generate,
    reference_date,
    run_email,
    run_marker,
)
from lastro.testdata.models import (
    ENTITIES,
    TYPES,
    GeneratedData,
    GeneratedEntity,
    GeneratedValue,
    TestDataError,
    TestDataRequest,
    parse_request,
)

__all__ = [
    "ENTITIES",
    "TYPES",
    "GeneratedData",
    "GeneratedEntity",
    "GeneratedValue",
    "TestDataError",
    "TestDataRequest",
    "derive_seed",
    "generate",
    "parse_request",
    "reference_date",
    "run_email",
    "run_marker",
]
