import ast
import inspect
from decimal import Decimal

import pytest

from lastro.native import pricing
from lastro.native.pricing import (
    UnknownModelError,
    Usage,
    capabilities,
    format_usd,
    reservation_nusd,
    usage_cost_nusd,
)


def test_cache_categories_are_not_double_counted() -> None:
    usage = Usage(
        input_tokens=1200, cache_read_tokens=2000, cache_write_tokens=0, output_tokens=300
    )
    assert usage.total_input_tokens == 3200
    # 1200 * 2 + 2000 * 0.10 + 300 * 10 (USD/MTok), o exemplo de telemetria da especificação.
    assert format_usd(usage_cost_nusd("claude-sonnet-5-5", usage)) == "0.005600000"
    with_write = Usage(1200, 2000, 1000, 300)
    assert (
        usage_cost_nusd("claude-sonnet-5-5", with_write)
        - usage_cost_nusd("claude-sonnet-5-5", usage)
        == 1000 * 2500
    )


def test_reservation_uses_cache_write_worst_case() -> None:
    counted = 10_000
    margin = max(1024, 1000)
    reserved_input = counted + margin
    expected = reserved_input * 2500 + 2048 * 10_000
    assert reservation_nusd("claude-sonnet-5-5", counted, 2048) == expected
    small = reservation_nusd("claude-sonnet-5-5", 100, 256)
    assert small == (100 + 1024) * 2500 + 256 * 10_000


def test_haiku_long_prompt_tier() -> None:
    caps = capabilities("claude-haiku-5-5")
    assert caps.tier_for(100_000).input == Decimal("0.10")
    assert caps.tier_for(100_001).input == Decimal("0.50")
    # A reserva usa o tier do tamanho reservado, nunca o mais barato.
    assert reservation_nusd("claude-haiku-5-5", 99_500, 256) == (99_500 + 9950) * 625 + 256 * 2500


def test_money_never_uses_float() -> None:
    tree = ast.parse(inspect.getsource(pricing))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "float" not in names
    floats = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert floats == []
    assert isinstance(usage_cost_nusd("claude-opus-5-5", Usage(1, 1, 1, 1)), int)


def test_unknown_model_has_no_zero_price_fallback() -> None:
    with pytest.raises(UnknownModelError):
        usage_cost_nusd("claude-unknown", Usage(0, 0, 0, 0))
    with pytest.raises(UnknownModelError):
        reservation_nusd("claude-unknown", 10, 256)


def test_pricing_document_is_stable() -> None:
    first = pricing.pricing_document({"claude-sonnet-5-5"})
    second = pricing.pricing_document({"claude-sonnet-5-5"})
    assert pricing.document_sha256(first) == pricing.document_sha256(second)
    models = first["models"]
    assert isinstance(models, dict)
    assert set(models) == {"claude-sonnet-5-5"}
