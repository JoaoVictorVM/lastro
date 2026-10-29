"""Demo do Lastro: cadastro fictício de funcionários de duas clínicas com defeitos semeados."""

from importlib.metadata import PackageNotFoundError, version

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
DISTRIBUTION_NAME = "lastro"
FALLBACK_VERSION = "0.0.0+unknown"


def get_version() -> str:
    try:
        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return FALLBACK_VERSION


__version__ = get_version()
