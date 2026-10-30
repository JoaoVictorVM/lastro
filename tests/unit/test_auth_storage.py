import json
import os
import re
import stat
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lastro.auth import MAX_STATE_AGE, SessionRefError, SessionStateStore
from lastro.auth import storage as storage_module
from lastro.config.loader import ProjectContext
from lastro.history import open_history

STATE = {
    "cookies": [
        {
            "name": "lastro_demo_session",
            "value": "cookie-sentinela-storage-77",
            "domain": "127.0.0.1",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": False,
            "sameSite": "Lax",
        }
    ],
    "origins": [],
}

OTHER_PROJECT_ID = "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d"


@pytest.fixture
def store(history_context: ProjectContext, fake_clock: Callable[[], datetime]) -> SessionStateStore:
    return SessionStateStore(history_context.project_data_dir, clock=fake_clock)


def test_storage_uses_stable_project_namespace(
    history_context_for: Callable[[str], ProjectContext],
) -> None:
    homolog = history_context_for("homolog")
    local = history_context_for("local")
    homolog_store = SessionStateStore(homolog.project_data_dir)
    local_store = SessionStateStore(local.project_data_dir)
    other_store = SessionStateStore(homolog.private_data_root / OTHER_PROJECT_ID)

    homolog_path = homolog_store.path_for("homolog")
    assert homolog_path == homolog.private_data_root / homolog.require_project_id() / ".auth" / (
        "homolog.json"
    )
    assert local_store.path_for("local").parent == homolog_path.parent
    assert local_store.path_for("local") != homolog_path
    assert other_store.path_for("homolog") != homolog_path
    assert other_store.path_for("homolog").parent.parent.name == OTHER_PROJECT_ID


def test_storage_layout_matches_history_project_dir(
    history_context: ProjectContext, store: SessionStateStore
) -> None:
    history = open_history(history_context)
    ref = store.publish("homolog", STATE)

    auth_dir = ref.path.parent
    assert auth_dir.name == ".auth"
    assert auth_dir.parent == history.project_dir.resolve()
    assert (auth_dir.parent / "history.sqlite").is_file()
    assert history.database_path.parent.resolve() == auth_dir.parent


def test_publish_then_load_round_trips_opaque_state(store: SessionStateStore) -> None:
    ref = store.publish("homolog", STATE)
    loaded = store.load("homolog")

    assert loaded is not None
    assert loaded.state == STATE
    assert loaded.ref.path == ref.path
    assert "cookie-sentinela-storage-77" not in repr(loaded)
    assert "cookie-sentinela-storage-77" not in repr(ref)
    assert ref.expires_at - ref.created_at == timedelta(hours=8)


def test_storage_before_and_at_eight_hour_boundary(
    store: SessionStateStore, clock_now: list[datetime]
) -> None:
    created = clock_now[0]
    store.publish("homolog", STATE)

    clock_now[0] = created + MAX_STATE_AGE - timedelta(seconds=1)
    assert store.load("homolog") is not None

    clock_now[0] = created + MAX_STATE_AGE
    assert store.load("homolog") is None
    assert not store.path_for("homolog").exists()


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"\xff\xfe\x00", b'{"cookies": "x", "origins": []}', b"[]", b'{"cookies": []}'],
)
def test_corrupt_state_file_is_removed(store: SessionStateStore, content: bytes) -> None:
    path = store.publish("homolog", STATE).path
    path.write_bytes(content)

    assert store.load("homolog") is None
    assert not path.exists()


def test_failed_publish_keeps_previous_state_and_no_temporary(
    store: SessionStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.publish("homolog", STATE)

    def broken_replace(source: object, target: object) -> None:
        raise OSError("disco cheio")

    monkeypatch.setattr(storage_module.os, "replace", broken_replace)
    changed = {"cookies": [], "origins": []}
    with pytest.raises(OSError, match="disco cheio"):
        store.publish("homolog", changed)

    monkeypatch.undo()
    loaded = store.load("homolog")
    assert loaded is not None
    assert loaded.state == STATE
    assert [item.name for item in store.directory.iterdir()] == ["homolog.json"]


def test_publish_rejects_invalid_structure(store: SessionStateStore) -> None:
    with pytest.raises(ValueError, match="estrutura"):
        store.publish("homolog", {"cookies": "nope"})
    assert not store.path_for("homolog").exists()


def test_reference_is_rejected_when_expired_foreign_or_replaced(
    store: SessionStateStore, clock_now: list[datetime]
) -> None:
    ref = store.publish("homolog", STATE)
    store.validate_ref(ref, "homolog")

    with pytest.raises(SessionRefError) as other:
        store.validate_ref(ref, "local")
    assert other.value.reason_code == "profile_mismatch"

    clock_now[0] = clock_now[0] + timedelta(minutes=5)
    store.publish("homolog", STATE)
    with pytest.raises(SessionRefError) as replaced:
        store.validate_ref(ref, "homolog")
    assert replaced.value.reason_code == "replaced"

    fresh = store.publish("homolog", STATE)
    clock_now[0] = fresh.expires_at
    with pytest.raises(SessionRefError) as expired:
        store.validate_ref(fresh, "homolog")
    assert expired.value.reason_code == "expired"


def test_invalid_profile_name_never_escapes_auth_dir(store: SessionStateStore) -> None:
    with pytest.raises(SessionRefError):
        store.path_for("../escape")


@pytest.mark.skipif(sys.platform == "win32", reason="permissões POSIX")
def test_posix_storage_is_owner_only(store: SessionStateStore) -> None:
    previous = os.umask(0)
    try:
        ref = store.publish("homolog", STATE)
    finally:
        os.umask(previous)

    assert stat.S_IMODE(ref.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(ref.path.stat().st_mode) == 0o600


def _assert_owner_only_dacl(path: Path) -> None:
    if sys.platform != "win32":
        pytest.fail("verificação de DACL só existe no Windows")
    from lastro.auth import winacl

    sddl = winacl.read_dacl_sddl(path)
    match = re.fullmatch(r"D:([A-Z]*)((?:\([^)]*\))*)", sddl)
    assert match is not None, sddl
    assert "P" in match.group(1), f"DACL sem proteção contra herança: {sddl}"
    aces = re.findall(r"\(([^)]*)\)", match.group(2))
    assert len(aces) == 1, f"esperada uma única ACE: {sddl}"
    fields = aces[0].split(";")
    assert fields[0] == "A"
    assert "ID" not in fields[1]
    assert winacl.same_sid(fields[5], winacl.current_user_sid())


@pytest.mark.skipif(sys.platform != "win32", reason="DACL do Windows")
def test_windows_storage_acl_is_current_user_only(store: SessionStateStore) -> None:
    ref = store.publish("homolog", STATE)

    _assert_owner_only_dacl(ref.path.parent)
    _assert_owner_only_dacl(ref.path)


@pytest.mark.skipif(sys.platform != "win32", reason="DACL do Windows")
def test_windows_temporary_state_is_never_broadly_readable(
    store: SessionStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "win32":
        pytest.fail("DACL do Windows")
    from lastro.auth import winacl

    original = winacl.create_owner_only_file
    checked: list[Path] = []

    def observed(path: Path) -> None:
        original(path)
        assert path.stat().st_size == 0
        _assert_owner_only_dacl(path)
        checked.append(path)

    monkeypatch.setattr(winacl, "create_owner_only_file", observed)
    ref = store.publish("homolog", STATE)

    assert len(checked) == 1
    assert checked[0].parent == ref.path.parent
    assert checked[0].name.endswith(".tmp")
    assert not checked[0].exists()
    assert json.loads(ref.path.read_text(encoding="utf-8")) == STATE
