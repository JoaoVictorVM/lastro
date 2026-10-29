"""Orquestração do lastro doctor: ordem dos checks, dependências e código de saída."""

from collections.abc import Sequence
from pathlib import Path

from lastro.cli.output import Status
from lastro.config.errors import ConfigError
from lastro.config.loader import (
    ConfigNotFoundError,
    LoadedConfig,
    load_config_file,
    select_profiles,
)
from lastro.config.schema import DEFAULT_EXECUTOR, Profile
from lastro.doctor import checks
from lastro.doctor.checks import CheckResult, DoctorEnvironment
from lastro.paths import default_private_data_root

EXIT_OK = 0
EXIT_WARNINGS = 1
EXIT_FAILURES = 2


def default_environment() -> DoctorEnvironment:
    return DoctorEnvironment()


def exit_code_for(results: Sequence[CheckResult]) -> int:
    statuses = {result.status for result in results}
    if Status.FAIL in statuses:
        return EXIT_FAILURES
    if Status.WARN in statuses:
        return EXIT_WARNINGS
    return EXIT_OK


def _run_config_check(
    config_option: Path | None, profile_option: str | None, cwd: Path
) -> tuple[CheckResult, LoadedConfig | None, list[Profile]]:
    try:
        loaded = load_config_file(config_option, cwd)
        profiles = select_profiles(loaded.config, profile_option)
    except ConfigNotFoundError as exc:
        return checks.config_missing(exc.lines), None, []
    except ConfigError as exc:
        return checks.config_invalid(exc.lines), None, []
    return checks.config_valid(loaded, profiles), loaded, profiles


def run_doctor(
    env: DoctorEnvironment,
    config_option: Path | None = None,
    profile_option: str | None = None,
    cwd: Path | None = None,
) -> list[CheckResult]:
    cwd = cwd or Path.cwd()
    # A configuração roda primeiro porque os checks 6, 10, 11 e 12 dependem dela.
    config_result, loaded, profiles = _run_config_check(config_option, profile_option, cwd)

    executor = loaded.config.executor if loaded else DEFAULT_EXECUTOR
    root = loaded.private_data_root if loaded else default_private_data_root()
    project_dir = loaded.project_dir if loaded else cwd

    results: list[CheckResult] = [
        checks.check_python(env),
        checks.check_uv(env),
        checks.check_playwright(env),
        checks.check_executor_cli(env, executor),
        config_result,
    ]
    if loaded is None:
        results.append(checks.depends_on_config(6, checks.NAME_CREDENTIAL_VARS))
    else:
        results.append(checks.check_credential_vars(env, profiles))
    results += [
        checks.check_private_root_writable(root),
        checks.check_private_root_outside_git(root),
        checks.check_env_ignored(env, project_dir),
    ]
    if loaded is None:
        results += [
            checks.depends_on_config(10, checks.NAME_TARGET),
            checks.depends_on_config(11, checks.NAME_VERSION_ENDPOINT),
            checks.depends_on_config(12, checks.NAME_PERMISSION),
        ]
    else:
        results += [checks.check_target(env, profile) for profile in profiles]
        results += [checks.check_version_endpoint(env, profile) for profile in profiles]
        results.append(checks.check_permission_file(project_dir, executor))
    return results
