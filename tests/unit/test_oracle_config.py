import pytest
from pydantic import ValidationError

from lastro.config.loader import ProjectContext
from lastro.history import TextMission, open_history
from lastro.oracles.ignore import IgnoreRule, OracleSettings, glob_matches, ignored


def test_default_empty_ignore_is_compatible(history_context: ProjectContext) -> None:
    assert history_context.profile.oracles.ignore == []
    store = open_history(history_context)
    run = store.create_run(history_context, TextMission("observe"))
    assert '"oracles"' in str(run.record.config_snapshot).replace("'", '"')


@pytest.mark.parametrize(
    "rule",
    [
        {},
        {"url_path": ""},
        {"url_path": "analytics/*"},
        {"message": "x" * 257},
        {"message": "[abc]"},
        {"message": "a\\b"},
        {"message": None},
        {"message": "x", "url_path": "/x"},
        {"other": "x"},
    ],
)
def test_invalid_ignore_rules_rejected(rule: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        IgnoreRule.model_validate(rule)


def test_ignore_limits_and_duplicates() -> None:
    for rules in ([{"message": "x"}] * 2, [{"message": str(i)} for i in range(101)]):
        with pytest.raises(ValidationError):
            OracleSettings.model_validate({"ignore": rules})


def test_globs_are_full_match_and_case_sensitive() -> None:
    assert glob_matches("/a/*", "/a/b/c")
    assert glob_matches("?x*", "ax")
    assert glob_matches("a.b", "a.b")
    assert not glob_matches("a.b", "acb")
    assert not glob_matches("*Noise*", "noise")
    rules = [IgnoreRule(url_path="/a/*"), IgnoreRule(message="*Noise*")]
    assert ignored(rules, "/a/b", "ok")
    assert ignored(rules, "/b", "Noise here")
    assert not ignored(rules, "/b", "noise here")
