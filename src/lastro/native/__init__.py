"""Executor nativo. LangGraph e Anthropic SDK só são importados quando a run nativa começa."""

import importlib.util

NATIVE_MODULES = ("langgraph", "anthropic")
MISSING_EXTRA_MESSAGE = (
    "The native executor requires the optional 'native' extra: "
    "run 'uv sync --extra native' or 'pip install \"lastro[native]\"'."
)


def missing_dependencies() -> list[str]:
    return [name for name in NATIVE_MODULES if importlib.util.find_spec(name) is None]
