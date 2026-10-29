import ast
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SECURITY = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
NOTICES = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
TEST_LINK = re.compile(r"\[`(test_\w+)`\]\((tests/[\w/]+\.py)\)")
NOTICE_COLUMNS = [
    "Project",
    "Author or organization",
    "URL",
    "Version",
    "License",
    "Modifications",
    "Required notices",
]


def section(markdown: str, title: str) -> str:
    start = markdown.index(f"\n## {title}\n")
    end = markdown.find("\n## ", start + 1)
    return markdown[start : end if end != -1 else len(markdown)]


def bullets(text: str) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        if line.startswith("- "):
            items.append(line[2:])
        elif line.startswith("  ") and items:
            items[-1] += " " + line.strip()
    return items


def defined_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_security_policy_has_supported_versions_channel_and_sla() -> None:
    assert "## Supported versions" in SECURITY
    assert "| `main` (0.1.0, unreleased) | Yes |" in SECURITY
    reporting = section(SECURITY, "Reporting a vulnerability")
    assert "private vulnerability reporting" in reporting
    assert "jvmartinscv@gmail.com" in reporting
    assert "within 7 days" in reporting
    assert "## Threat model summary" in SECURITY


def test_each_guarantee_links_to_existing_test() -> None:
    guarantees = bullets(section(SECURITY, "Guarantees demonstrated by tests"))

    assert len(guarantees) >= 5
    for guarantee in guarantees:
        links = TEST_LINK.findall(guarantee)
        assert links, f"guarantee without a test link: {guarantee}"
        for name, relative in links:
            path = REPO_ROOT / relative
            assert path.is_file(), relative
            assert name in defined_functions(path), f"{relative} has no {name}"


def test_non_guarantees_include_executor_bypass() -> None:
    non_guarantees = " ".join(bullets(section(SECURITY, "Non-guarantees")))

    assert (
        "Lastro cannot prevent an executor configured without the provided permission rules "
        "from reaching the target system by other means." in non_guarantees
    )
    guarantees = section(SECURITY, "Guarantees demonstrated by tests")
    assert "permission rules" not in guarantees


def notice_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in NOTICES.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
        elif not set("".join(cells)) <= {"-"}:
            rows.append(dict(zip(header, cells, strict=True)))
    return rows


def runtime_packages() -> dict[str, str]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {item["name"]: item for item in lock["package"]}
    pending = [dep["name"] for dep in packages["lastro"]["dependencies"]]
    found: dict[str, str] = {}
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found[name] = packages[name]["version"]
        pending += [dep["name"] for dep in packages[name].get("dependencies", [])]
    return found


def test_third_party_entries_have_required_fields() -> None:
    rows = notice_rows()

    assert rows
    for row in rows:
        assert list(row) == NOTICE_COLUMNS
        assert all(row.values()), row
        assert row["URL"].startswith("https://"), row
    listed = {row["Project"].split(" (")[0]: row["Version"] for row in rows}
    for name, version in runtime_packages().items():
        assert listed.get(name) == version, f"{name} {version} missing or outdated"


def test_bundled_htmx_is_attributed() -> None:
    htmx = next(row for row in notice_rows() if row["Project"] == "htmx")
    bundled = (REPO_ROOT / "src/lastro_demo/static/htmx.min.js").read_text(encoding="utf-8")

    assert htmx["Version"] == "2.0.11"
    assert 'version:"2.0.11"' in bundled
    assert htmx["License"] == "0BSD"
    assert htmx["URL"] == "https://github.com/bigskysoftware/htmx"
    assert "htmx 2.0.11" in (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
