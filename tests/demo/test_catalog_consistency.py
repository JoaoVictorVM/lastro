from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

import lastro_demo
from lastro_demo.flags import ALL_FLAG_IDS, DEFECT_IDS, SCENARIO_IDS

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "evals" / "catalog" / "demo-bugs.yaml"
REQUIRED_FIELDS = {
    "id",
    "flag",
    "kind",
    "category",
    "flow",
    "trigger",
    "expected_symptom",
    "intermittent",
}


def entries() -> list[dict[str, Any]]:
    data = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    items = data["entries"]
    assert isinstance(items, list)
    return items


def test_catalog_lists_all_flags_exactly_once() -> None:
    flags = [entry["flag"] for entry in entries()]

    assert sorted(flags) == sorted(ALL_FLAG_IDS)
    assert len(flags) == len(set(flags))


def test_catalog_entries_have_required_fields() -> None:
    for entry in entries():
        assert set(entry) >= REQUIRED_FIELDS, entry.get("id")
        for field in REQUIRED_FIELDS - {"intermittent"}:
            assert isinstance(entry[field], str), (entry["id"], field)
            assert entry[field].strip(), (entry["id"], field)
        assert entry["id"].lower() == entry["flag"]
        assert isinstance(entry["intermittent"], bool)
        assert entry["intermittent"] == (entry["flag"] == "d08")
        expected_kind = "defect" if entry["flag"] in DEFECT_IDS else "safety"
        assert entry["kind"] == expected_kind
    assert {entry["flag"] for entry in entries() if entry["kind"] == "safety"} == set(SCENARIO_IDS)


def test_catalog_outside_package_and_not_served(make_client: Callable[..., TestClient]) -> None:
    package_dir = Path(str(resources.files(lastro_demo)))
    assert CATALOG.is_file()
    assert package_dir not in CATALOG.parents
    assert not list(package_dir.rglob("demo-bugs*"))
    assert not any(path.suffix in {".yaml", ".yml"} for path in package_dir.rglob("*"))
    client = make_client()
    for url in (
        "/evals/catalog/demo-bugs.yaml",
        "/static/../evals/catalog/demo-bugs.yaml",
        "/static/%2e%2e/%2e%2e/%2e%2e/evals/catalog/demo-bugs.yaml",
        "/demo-bugs.yaml",
        "/static/demo-bugs.yaml",
    ):
        response = client.get(url)
        assert response.status_code == 404, url
        assert "expected_symptom" not in response.text


def test_app_source_never_mentions_catalog() -> None:
    package_dir = Path(str(resources.files(lastro_demo)))
    for path in package_dir.rglob("*.py"):
        assert "demo-bugs" not in path.read_text(encoding="utf-8"), path
