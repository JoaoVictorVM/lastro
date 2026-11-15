import json
from pathlib import Path

import pytest

from lastro.browser.tools import catalog
from lastro.evaluation.conditions import (
    DERIVED_MANIFEST,
    EVALUATION_BLOCK,
    MEMORY_TOOLS,
    EffectiveCondition,
    binding_block,
    child_snapshot_block,
    derive_bundle,
    from_snapshot,
    verify_derived_bundle,
    write_derived_bundle,
)
from lastro.evaluation.models import Condition, EvalError
from lastro.mission import BundleError, verify_bundle
from lastro.skills import bundle_hash


def test_derived_bundle_hash_matches_actual_content(tmp_path: Path) -> None:
    for condition in Condition:
        effective = EffectiveCondition.of(condition)
        bundle = derive_bundle(effective)
        if condition == Condition.BASELINE:
            assert bundle is None
            continue
        assert bundle is not None
        assert bundle.sha256 == bundle_hash(bundle.files)
        directory = tmp_path / condition.value / "skills" / "lastro"
        write_derived_bundle(directory, bundle)
        verified = verify_derived_bundle(directory, expected_condition=condition)
        assert verified.sha256 == bundle.sha256
    full = derive_bundle(EffectiveCondition.of(Condition.FULL))
    ablated = derive_bundle(EffectiveCondition.of(Condition.NO_ORACLES))
    assert full is not None and ablated is not None
    assert full.sha256 != ablated.sha256
    assert ablated.manifest["base_bundle_sha256"] == full.manifest["base_bundle_sha256"]


def test_tampered_derived_bundle_is_refused(tmp_path: Path) -> None:
    bundle = derive_bundle(EffectiveCondition.of(Condition.NO_MEMORY))
    assert bundle is not None
    directory = tmp_path / "skills" / "lastro"
    write_derived_bundle(directory, bundle)
    (directory / "explore.md").write_text("Use knowledge_query always.", encoding="utf-8")
    with pytest.raises(EvalError, match="changed"):
        verify_derived_bundle(directory, expected_condition=Condition.NO_MEMORY)
    with pytest.raises(EvalError, match="another condition"):
        verify_derived_bundle(directory, expected_condition=Condition.FULL)


def test_normal_run_rejects_eval_manifest(tmp_path: Path) -> None:
    bundle = derive_bundle(EffectiveCondition.of(Condition.FULL))
    assert bundle is not None
    write_derived_bundle(tmp_path / "skills" / "lastro", bundle)
    assert (tmp_path / "skills" / "lastro" / DERIVED_MANIFEST).is_file()
    with pytest.raises(BundleError):
        verify_bundle(tmp_path)


def test_heuristics_removed_from_all_prompts() -> None:
    bundle = derive_bundle(EffectiveCondition.of(Condition.NO_HEURISTICS))
    assert bundle is not None
    assert "qa-heuristics.md" not in bundle.files
    text = bundle.text().lower()
    assert "heuristic" not in text
    # Regras de segurança e evidência obrigatória continuam no bundle.
    assert "untrusted" in text and "evidence" in text
    memory = derive_bundle(EffectiveCondition.of(Condition.NO_MEMORY))
    assert memory is not None and "knowledge_" not in memory.text()
    reproduction = derive_bundle(EffectiveCondition.of(Condition.NO_REPRODUCTION))
    assert reproduction is not None and "reproduce.md" not in reproduction.files


def test_child_inherits_effective_condition() -> None:
    effective = EffectiveCondition.of(Condition.NO_ORACLES)
    block = binding_block(effective, suite_id="eval_1", trial_id="trial_1", bundle_sha256="a" * 64)
    parent = {EVALUATION_BLOCK: block, "other": 1}
    child = child_snapshot_block(parent)
    assert from_snapshot(child) == effective
    assert child_snapshot_block({"other": 1}) == {}


def test_tampered_binding_is_refused() -> None:
    block = binding_block(
        EffectiveCondition.of(Condition.NO_MEMORY), suite_id="s", trial_id="t", bundle_sha256=None
    )
    forged = json.loads(json.dumps(block))
    forged["condition"] = "full"
    with pytest.raises(EvalError, match="binding"):
        from_snapshot({EVALUATION_BLOCK: forged})
    assert from_snapshot({}) is None


def test_memory_ablation_removes_memory_tools_from_catalog() -> None:
    disabled = EffectiveCondition.of(Condition.NO_MEMORY).disabled_tools
    assert disabled == MEMORY_TOOLS
    assert not set(catalog(False, disabled)) & MEMORY_TOOLS
    assert set(catalog(False)) >= MEMORY_TOOLS
