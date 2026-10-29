from collections.abc import Callable
from pathlib import Path

import pytest

from lastro.config.errors import ConfigError
from lastro.config.loader import load_config


def _errors_for(path: Path) -> list[str]:
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    return excinfo.value.lines


def test_error_message_has_file_path_and_expected(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    path = write_config(profile_toml("homolog", extra='editable_fields = { employee = "phone" }'))

    assert _errors_for(path) == [
        "lastro.toml › profiles.homolog.editable_fields.employee: "
        "expected list of field names, got string"
    ]


def test_all_errors_listed_together(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    path = write_config(
        'timezone = "Nowhere/City"\n' + profile_toml("homolog", extra="tenant_id_x = 1")
    )

    lines = _errors_for(path)

    assert len(lines) == 2
    assert lines[0].startswith("lastro.toml › timezone: expected IANA timezone name")
    assert lines[1].startswith("lastro.toml › profiles.homolog.tenant_id_x: ")


def test_missing_required_field_reported(write_config: Callable[..., Path]) -> None:
    path = write_config('timezone = "UTC"\n')

    assert _errors_for(path) == [
        "lastro.toml › profiles: expected a value (required field), got nothing"
    ]


def test_toml_syntax_error_has_line(write_config: Callable[..., Path]) -> None:
    path = write_config('timezone = "UTC"\n\n[profiles.homolog\ntarget_url = "x"\n')

    lines = _errors_for(path)

    assert len(lines) == 1
    assert lines[0].startswith("lastro.toml › line 3, column ")
    assert "expected valid TOML" in lines[0]


def test_secret_values_never_in_message(
    write_config: Callable[..., Path], profile_toml: Callable[..., str]
) -> None:
    secret = "Sup3r-Secret-Value"
    text = profile_toml("homolog").replace('"HOMOLOG_PASSWORD"', f'"{secret}"')
    text += f'\n[profiles.homolog.extra]\npassword = "{secret}"\n'
    path = write_config(text)

    lines = _errors_for(path)

    assert lines
    assert all(secret not in line for line in lines)
    assert any("credentials.password_env" in line for line in lines)


def test_file_label_relative_to_cwd(
    write_config: Callable[..., Path], workdir: Path, profile_toml: Callable[..., str]
) -> None:
    path = write_config(profile_toml("homolog", extra="denied_methods = 1"), workdir / "sub")

    assert _errors_for(path)[0].startswith(
        f"{Path('sub') / 'lastro.toml'} › profiles.homolog.denied_methods: "
        "expected list of HTTP methods, got integer"
    )
