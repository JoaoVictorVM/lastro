import sys
from pathlib import Path

import pytest

from lastro.paths import (
    default_private_data_root,
    ensure_writable,
    is_inside_git_tree,
    resolve_private_data_root,
)


def test_default_root_windows(tmp_path: Path) -> None:
    local = tmp_path / "Local"

    root = default_private_data_root("win32", {"LOCALAPPDATA": str(local)}, tmp_path)

    assert root == local / "lastro"


def test_default_root_windows_without_localappdata(tmp_path: Path) -> None:
    root = default_private_data_root("win32", {}, tmp_path)

    assert root == tmp_path / "AppData" / "Local" / "lastro"


def test_default_root_linux(tmp_path: Path) -> None:
    root = default_private_data_root("linux", {"LOCALAPPDATA": "ignorado"}, tmp_path)

    assert root == tmp_path / ".local" / "share" / "lastro"


def test_default_root_uses_current_platform(workdir: Path) -> None:
    root = default_private_data_root()

    home = workdir.parent / "home"
    if sys.platform.startswith("win"):
        assert root == home / "AppData" / "Local" / "lastro"
    else:
        assert root == home / ".local" / "share" / "lastro"


def test_override_from_config(workdir: Path) -> None:
    home = workdir.parent / "home"

    assert resolve_private_data_root("~/dados-lastro", workdir) == home / "dados-lastro"
    assert resolve_private_data_root("relativo", workdir) == workdir / "relativo"
    absolute = workdir.parent / "abs"
    assert resolve_private_data_root(str(absolute), workdir) == absolute


def test_inside_git_tree_detected(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)

    assert is_inside_git_tree(tmp_path / "repo" / "a" / "b" / "nao-existe")


def test_outside_git_tree(tmp_path: Path) -> None:
    # A pasta temporária do sistema pode estar dentro de um repositório em alguma máquina.
    if is_inside_git_tree(tmp_path):
        pytest.skip("diretório temporário está dentro de um repositório Git")

    assert not is_inside_git_tree(tmp_path / "projeto")


def test_git_file_marker_detected(tmp_path: Path) -> None:
    repo = tmp_path / "worktree"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: ../main/.git/worktrees/x\n", encoding="utf-8")

    assert is_inside_git_tree(repo / "dados")


def test_ensure_writable_creates_and_cleans(tmp_path: Path) -> None:
    root = tmp_path / "novo" / "root"

    ensure_writable(root)

    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_ensure_writable_fails_when_not_creatable(tmp_path: Path) -> None:
    blocker = tmp_path / "arquivo"
    blocker.write_text("x", encoding="utf-8")

    with pytest.raises(OSError):
        ensure_writable(blocker / "root")
