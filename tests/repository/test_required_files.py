import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_FILES = (
    "README.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    ".gitignore",
    ".env.example",
    ".editorconfig",
    ".pre-commit-config.yaml",
    ".github/workflows/ci.yml",
    ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/ISSUE_TEMPLATE/feature.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "docs/assets/demo.gif",
)
PLANNED_RELEASES = [f"0.{minor}.0" for minor in range(1, 10)] + ["1.0.0"]
FORBIDDEN_DASHES = (chr(0x2013), chr(0x2014))
BINARY_SUFFIXES = {".gif", ".png", ".ico", ".whl", ".gz", ".zip"}

GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", CORE_FILES)
def test_core_repository_files_exist_and_are_nonempty(relative: str) -> None:
    path = REPO_ROOT / relative

    assert path.is_file(), f"missing {relative}"
    assert path.stat().st_size > 0, f"empty {relative}"


def test_license_is_unmodified_apache_2_0() -> None:
    text = read("LICENSE")

    assert text.lstrip().startswith("Apache License\n")
    assert "Version 2.0, January 2004" in text
    assert "http://www.apache.org/licenses/" in text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text
    assert "9. Accepting Warranty or Additional Liability." in text
    assert "END OF TERMS AND CONDITIONS" in text
    assert "APPENDIX: How to apply the Apache License to your work." in text
    assert "Copyright [yyyy] [name of copyright owner]" in text


def test_notice_names_copyright_holder() -> None:
    text = read("NOTICE")

    assert "Copyright 2026 Joao Victor Ventura Martins" in text
    assert "Apache License, Version 2.0" in text


def test_code_of_conduct_is_contributor_covenant_2_1() -> None:
    text = read("CODE_OF_CONDUCT.md")

    assert text.startswith("# Contributor Covenant Code of Conduct")
    assert "version 2.1" in text
    assert "## Enforcement Guidelines" in text
    assert "responsible for enforcement at jvmartinscv@gmail.com" in text
    assert "[INSERT CONTACT METHOD]" not in text


def test_changelog_structure() -> None:
    text = read("CHANGELOG.md")
    versions = re.findall(r"^## \[([^\]]+)\](.*)$", text, flags=re.MULTILINE)

    assert "Keep a Changelog" in text
    assert "Semantic Versioning" in text
    assert versions[0] == ("Unreleased", "")
    released = [version for version, suffix in versions[1:] if suffix != " - Planned"]
    assert released == [], "no release exists yet, so every version must be marked planned"
    planned = sorted(
        (version for version, _ in versions[1:]),
        key=lambda value: tuple(int(part) for part in value.split(".")),
    )
    assert planned == PLANNED_RELEASES
    assert [version for version, _ in versions[1:]] == list(reversed(PLANNED_RELEASES))
    assert re.search(r"^\[Unreleased\]: https://\S+$", text, flags=re.MULTILINE)


def test_editorconfig_contract() -> None:
    sections: dict[str, dict[str, str]] = {}
    current = ""
    for line in read(".editorconfig").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = {}
        elif "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            sections.setdefault(current, {})[key] = value

    assert sections[""]["root"] == "true"
    defaults = sections["*"]
    assert defaults["charset"] == "utf-8"
    assert defaults["end_of_line"] == "lf"
    assert defaults["insert_final_newline"] == "true"
    assert defaults["indent_style"] == "space"
    assert sections["*.py"]["indent_size"] == "4"
    assert sections["*.{yml,yaml,toml,html,json}"]["indent_size"] == "2"


def test_pyproject_version_and_packages_preserved() -> None:
    pyproject = tomllib.loads(read("pyproject.toml"))

    assert pyproject["project"]["version"] == "0.1.0"
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/lastro",
        "src/lastro_demo",
    ]
    assert "fail_under" not in pyproject["tool"].get("coverage", {}).get("report", {})
    dev = pyproject["dependency-groups"]["dev"]
    assert any(item.startswith("pre-commit") for item in dev)
    assert any(item.startswith("pytest-cov") for item in dev)
    assert f"(v{pyproject['project']['version']}, unreleased)" in read("README.md")
    for document in ("README.md", "CHANGELOG.md"):
        prose = re.sub(r"https?://\S+", "", read(document))
        mentioned = set(re.findall(r"\bv(\d+\.\d+\.\d+)\b", prose))
        assert mentioned <= set(PLANNED_RELEASES), document


def public_text_files(git: GitRunner) -> list[Path]:
    listed = git(
        "ls-files", "--cached", "--others", "--exclude-standard", "-z", cwd=REPO_ROOT
    ).stdout
    return [
        REPO_ROOT / name
        for name in listed.split("\0")
        if name and Path(name).suffix not in BINARY_SUFFIXES and (REPO_ROOT / name).is_file()
    ]


def test_public_files_use_no_long_dashes(git: GitRunner) -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not running from a Git checkout")
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in public_text_files(git)
        for text in [path.read_text(encoding="utf-8", errors="ignore")]
        if any(dash in text for dash in FORBIDDEN_DASHES)
    ]

    assert offenders == []
