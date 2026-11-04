"""Registro local de capacidades e tarifas; dinheiro em Decimal e nanoUSD inteiros, nunca float."""

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

NANO_PER_USD = Decimal(1_000_000_000)
TOKENS_PER_MTOK = Decimal(1_000_000)
# Margem local sobre a contagem prévia: a contagem do provedor é estimativa, não teto.
MIN_INPUT_MARGIN = 1024
INPUT_MARGIN_RATIO = Decimal("0.10")
CACHE_TTL = "5m"
# Conferido na tabela oficial de preços em 2026-11-04; mudança de tarifa exige nova versão.
PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing (2026-11-04)"


@dataclass(frozen=True)
class Tier:
    """Tarifas em USD por milhão de tokens; `up_to` limita o tamanho do prompt do tier."""

    up_to: int | None
    input: Decimal
    cache_write_5m: Decimal
    cache_read: Decimal
    output: Decimal


@dataclass(frozen=True)
class ModelCapabilities:
    model: str
    context_window: int
    max_output_tokens: int
    tiers: tuple[Tier, ...]

    def tier_for(self, prompt_tokens: int) -> Tier:
        for tier in self.tiers:
            if tier.up_to is None or prompt_tokens <= tier.up_to:
                return tier
        return self.tiers[-1]


def _tier(up_to: int | None, i: str, cw: str, cr: str, o: str) -> Tier:
    return Tier(up_to, Decimal(i), Decimal(cw), Decimal(cr), Decimal(o))


REGISTRY: dict[str, ModelCapabilities] = {
    "claude-sonnet-5-5": ModelCapabilities(
        "claude-sonnet-5-5", 1_000_000, 128_000, (_tier(None, "2", "2.50", "0.10", "10"),)
    ),
    "claude-opus-5-5": ModelCapabilities(
        "claude-opus-5-5", 1_000_000, 128_000, (_tier(None, "4", "5", "0.20", "20"),)
    ),
    "claude-haiku-5-5": ModelCapabilities(
        "claude-haiku-5-5",
        1_000_000,
        128_000,
        (
            _tier(100_000, "0.10", "0.125", "0.01", "0.50"),
            _tier(None, "0.50", "0.625", "0.05", "2.50"),
        ),
    ),
}


class UnknownModelError(ValueError):
    def __init__(self, model: str) -> None:
        super().__init__(f"model '{model}' has no local capabilities or pricing")
        self.model = model


def capabilities(model: str) -> ModelCapabilities:
    """Sem tarifa conhecida não há estimativa: modelo desconhecido nunca custa zero."""
    found = REGISTRY.get(model)
    if found is None:
        raise UnknownModelError(model)
    return found


def to_nusd(usd: Decimal) -> int:
    return int((usd * NANO_PER_USD).to_integral_value(rounding=ROUND_CEILING))


def from_nusd(value: int) -> Decimal:
    return Decimal(value) / NANO_PER_USD


def format_usd(value: int) -> str:
    return f"{from_nusd(value):.9f}"


def parse_usd(text: str) -> int:
    return to_nusd(Decimal(text))


def _cost(tokens: int, price_per_mtok: Decimal) -> Decimal:
    return Decimal(tokens) * price_per_mtok / TOKENS_PER_MTOK


@dataclass(frozen=True)
class Usage:
    """Categorias separadas do provedor; o total de entrada soma as três sem dupla contagem."""

    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


def usage_cost_nusd(model: str, usage: Usage) -> int:
    tier = capabilities(model).tier_for(usage.total_input_tokens)
    total = (
        _cost(usage.input_tokens, tier.input)
        + _cost(usage.cache_read_tokens, tier.cache_read)
        + _cost(usage.cache_write_tokens, tier.cache_write_5m)
        + _cost(usage.output_tokens, tier.output)
    )
    return to_nusd(total)


def reserved_input_tokens(counted: int) -> int:
    return counted + max(MIN_INPUT_MARGIN, math.ceil(counted * INPUT_MARGIN_RATIO))


def reservation_nusd(model: str, counted_input: int, max_output_tokens: int) -> int:
    """Pior caso: toda entrada à maior tarifa entre normal e escrita de cache, saída integral."""
    reserved = reserved_input_tokens(counted_input)
    tier = capabilities(model).tier_for(reserved)
    worst_input = max(tier.input, tier.cache_write_5m)
    return to_nusd(_cost(reserved, worst_input) + _cost(max_output_tokens, tier.output))


def pricing_document(models: set[str]) -> dict[str, object]:
    """Tabela congelada no snapshot da run, só com os modelos efetivamente configurados."""
    rows: dict[str, object] = {}
    for name in sorted(models):
        caps = capabilities(name)
        rows[name] = {
            "context_window": caps.context_window,
            "max_output_tokens": caps.max_output_tokens,
            "tiers": [
                {
                    "up_to_prompt_tokens": tier.up_to,
                    "input_usd_per_mtok": str(tier.input),
                    "cache_write_5m_usd_per_mtok": str(tier.cache_write_5m),
                    "cache_read_usd_per_mtok": str(tier.cache_read),
                    "output_usd_per_mtok": str(tier.output),
                }
                for tier in caps.tiers
            ],
        }
    return {"source": PRICING_SOURCE, "cache_ttl": CACHE_TTL, "models": rows}


def document_sha256(document: object) -> str:
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
