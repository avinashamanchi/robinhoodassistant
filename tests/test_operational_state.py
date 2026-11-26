"""Private operational metadata never grants order authority."""
import json
import os

import pytest


def test_private_state_survives_reopening_and_callback_failure(tmp_path):
    from trading_assistant.ops.operational_state import StateStore
    store = StateStore(tmp_path)
    assert store.load("sample") == {}
    assert store.update("sample", lambda old: {"count": old.get("count", 0) + 1}) == {"count": 1}
    reopened = StateStore(tmp_path)
    assert reopened.load("sample") == {"count": 1}
    def failed(old):
        old["count"] = 99
        raise RuntimeError("synthetic interruption")
    with pytest.raises(RuntimeError):reopened.update("sample", failed)
    assert reopened.load("sample") == {"count": 1}
    path = tmp_path / ".local/operations/sample.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink", "public", "malformed", "parent_symlink"])
def test_unsafe_state_is_rejected(tmp_path, kind):
    from trading_assistant.ops.operational_state import StateStore, StateError
    store = StateStore(tmp_path)
    store.update("sample", lambda _: {"safe": True})
    path = tmp_path / ".local/operations/sample.json"
    if kind == "symlink":
        target=path.with_name("other.json");path.rename(target);path.symlink_to(target)
    if kind == "fifo":path.unlink();os.mkfifo(path,0o600)
    if kind == "hardlink":os.link(path,path.with_name("other.json"))
    if kind == "public":path.chmod(0o644)
    if kind == "malformed":path.write_text('{"a":1,"a":2}')
    if kind == "parent_symlink":
        parent=path.parent;other=parent.with_name("other");parent.rename(other);parent.symlink_to(other)
    with pytest.raises(StateError):store.load("sample")


def test_state_lease_rejects_a_second_owner(tmp_path):
    from trading_assistant.ops.operational_state import StateStore, StateError
    first=StateStore(tmp_path);second=StateStore(tmp_path)
    with first.lease("power-owner"):
        with pytest.raises(StateError):
            with second.lease("power-owner"):pass
    with second.lease("power-owner"):pass


def test_unserializable_state_preserves_previous_bytes(tmp_path):
    from trading_assistant.ops.operational_state import StateStore, StateError
    store=StateStore(tmp_path);store.update("sample",lambda _: {"safe":True})
    with pytest.raises(StateError):store.update("sample",lambda _: {"bad":float("nan")})
    assert store.load("sample")=={"safe":True}
    with pytest.raises(StateError):store.update("sample",lambda _: {1:"one","1":"duplicate"})
    assert store.load("sample")=={"safe":True}
