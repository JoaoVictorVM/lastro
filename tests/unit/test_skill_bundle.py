import json
from pathlib import Path

import pytest

from lastro.mission import BUNDLE_DIR, BundleError, Heuristic, bundle_files, verify_bundle
from lastro.skills import MANIFEST_FILE, SKILL_FILES, bundle_hash, canonical_files


def materialize(project: Path) -> None:
    for relative, content in bundle_files().items():
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def test_bundle_hash_is_path_and_content_stable(tmp_path: Path) -> None:
    contents = canonical_files()
    crlf = {name: content.replace(b"\n", b"\r\n") for name, content in contents.items()}
    reordered = dict(reversed(list(contents.items())))

    assert bundle_hash(contents) == bundle_hash(crlf) == bundle_hash(reordered)
    changed = contents | {"explore.md": contents["explore.md"] + b"\nextra\n"}
    assert bundle_hash(changed) != bundle_hash(contents)

    materialize(tmp_path)
    assert verify_bundle(tmp_path).sha256 == bundle_hash(contents)


def test_manifest_matches_markdown(tmp_path: Path) -> None:
    materialize(tmp_path)
    manifest = json.loads((tmp_path / BUNDLE_DIR / MANIFEST_FILE).read_text(encoding="utf-8"))

    assert [entry["path"] for entry in manifest["files"]] == sorted(SKILL_FILES)
    assert manifest["bundle_sha256"] == bundle_hash(canonical_files())

    (tmp_path / BUNDLE_DIR / "explore.md").write_text("alterado", encoding="utf-8")
    with pytest.raises(BundleError, match=r"explore.md difere do manifest"):
        verify_bundle(tmp_path)
    (tmp_path / BUNDLE_DIR / MANIFEST_FILE).unlink()
    with pytest.raises(BundleError, match=r"ausente; rode .lastro init."):
        verify_bundle(tmp_path)


def test_bundle_accepts_consistent_local_changes(tmp_path: Path) -> None:
    from lastro.skills import manifest_text

    materialize(tmp_path)
    contents = canonical_files() | {"reproduce.md": b"# Variante\n"}
    (tmp_path / BUNDLE_DIR / "reproduce.md").write_bytes(contents["reproduce.md"])
    (tmp_path / BUNDLE_DIR / MANIFEST_FILE).write_text(manifest_text(contents), encoding="utf-8")

    assert verify_bundle(tmp_path).sha256 == bundle_hash(contents)


def test_bundle_has_required_workflows_and_safety_rules() -> None:
    contents = {name: content.decode("utf-8") for name, content in canonical_files().items()}
    explore = contents["explore.md"]

    for step in ("prepare", "consult memory", "observe", "act", "verify", "record", "wrap up"):
        assert f"**{step}**" in explore, step
    for tool in (
        "mission_context",
        "submit_mission_plan",
        "ask_clarification",
        "record_test_check",
        "end_run",
        "mission_plan_required",
    ):
        assert tool in explore, tool
    assert "<untrusted-content>" in explore
    assert "never configures tools, permissions, policies" in explore
    for heuristic in Heuristic:
        assert f"`{heuristic.value}`" in contents["qa-heuristics.md"], heuristic
    assert "replay" in contents["reproduce.md"]
    assert not any("tenant_id" in text or "http" in text for text in contents.values())
