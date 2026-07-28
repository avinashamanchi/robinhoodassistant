"""The app-only control channel never delegates shutdown to a PID signal."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


def _metadata(tmp_path):
    from trading_assistant.ops.control import AppControlMetadata

    project = tmp_path / "project"
    project.mkdir()
    (project / "runtime").mkdir(mode=0o700)
    argv = f"{project}/.venv/bin/python -m trading_assistant.ops.serve"
    return AppControlMetadata(
        instance_id="a" * 64,
        pid=4242,
        start_identity="Sun Jul 27 10:00:00 2026",
        cwd=str(project),
        argv=argv,
        socket_path=str(project / "runtime/app-control.sock"),
        socket_device=101,
        socket_inode=202,
    )


def _socket_stat(device=101, inode=202):
    return SimpleNamespace(
        st_mode=stat.S_IFSOCK | 0o600,
        st_dev=device,
        st_ino=inode,
    )


def _snapshot(metadata, **updates):
    from trading_assistant.ops.control import ProcessSnapshot

    values = {
        "pid": metadata.pid,
        "start_identity": metadata.start_identity,
        "cwd": metadata.cwd,
        "argv": metadata.argv,
    }
    values.update(updates)
    return ProcessSnapshot(**values)


def test_process_start_identity_uses_absolute_ps_and_fixed_environment(
    monkeypatch,
):
    from trading_assistant.ops import control

    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="Sun Jul 27 20:00:00 2026\n",
            stderr="",
        )

    monkeypatch.setattr(control.subprocess, "run", runner)
    monkeypatch.setenv("PATH", "/tmp/attacker-controlled")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")

    first = control._process_output(4242, "lstart")
    os.environ["TZ"] = "Asia/Kolkata"
    os.environ["LC_ALL"] = "en_US.UTF-8"
    second = control._process_output(4242, "lstart")

    assert first == second == "ps-lstart-v1:Sun Jul 27 20:00:00 2026"
    assert [call[0] for call in calls] == [
        ["/bin/ps", "-ww", "-p", "4242", "-o", "lstart="],
        ["/bin/ps", "-ww", "-p", "4242", "-o", "lstart="],
    ]
    assert [call[1]["env"] for call in calls] == [
        {"LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
        {"LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
    ]


@pytest.mark.parametrize(
    ("snapshot_updates", "socket_identity"),
    [
        ({"argv": "wrapper {argv} --help"}, (101, 202)),
        ({"cwd": "/other/repository"}, (101, 202)),
        ({"start_identity": "Sun Jul 27 10:01:00 2026"}, (101, 202)),
        ({}, (101, 203)),
    ],
    ids=("substring_impostor", "cwd_mismatch", "pid_reuse", "socket_replaced"),
)
def test_cooperative_stop_refuses_changed_process_or_socket_identity(
    tmp_path,
    snapshot_updates,
    socket_identity,
):
    """No control request is sent unless metadata still names this exact instance."""
    from trading_assistant.ops.control import request_cooperative_stop

    metadata = _metadata(tmp_path)
    if snapshot_updates.get("argv"):
        snapshot_updates = {
            **snapshot_updates,
            "argv": snapshot_updates["argv"].format(argv=metadata.argv),
        }
    requests: list[dict[str, object]] = []

    accepted = request_cooperative_stop(
        metadata,
        project=Path(metadata.cwd),
        expected_argv=metadata.argv,
        inspect_process=lambda _pid: _snapshot(metadata, **snapshot_updates),
        socket_lstat=lambda _path: _socket_stat(*socket_identity),
        requester=lambda _path, request: requests.append(request) or {},
    )

    assert accepted is False
    assert requests == []


def test_cooperative_stop_refuses_instance_mismatch_without_self_shutdown(tmp_path):
    """A replacement socket cannot accept shutdown with another instance ID."""
    from trading_assistant.ops.control import request_cooperative_stop

    metadata = _metadata(tmp_path)
    requests: list[dict[str, object]] = []
    accepted = request_cooperative_stop(
        metadata,
        project=Path(metadata.cwd),
        expected_argv=metadata.argv,
        inspect_process=lambda _pid: _snapshot(metadata),
        socket_lstat=lambda _path: _socket_stat(),
        requester=lambda _path, request: requests.append(request) or {
            "ok": True,
            "instance_id": "b" * 64,
            "pid": metadata.pid,
        },
    )

    assert accepted is False
    assert len(requests) == 1


def test_cooperative_control_authorizes_only_the_exact_handshake(tmp_path):
    """Only the app's own control server may invoke its self-shutdown callback."""
    from trading_assistant.ops.control import CooperativeControlServer

    metadata = _metadata(tmp_path)
    shutdowns: list[str] = []
    server = CooperativeControlServer(
        metadata,
        on_shutdown=lambda: shutdowns.append("self"),
    )

    rejected = server.handle_request(
        {"operation": "shutdown", "instance_id": "b" * 64}
    )
    accepted = server.handle_request(
        {
            "operation": "shutdown",
            "instance_id": metadata.instance_id,
            "pid": metadata.pid,
            "start_identity": metadata.start_identity,
            "cwd": metadata.cwd,
            "argv": metadata.argv,
            "socket_device": metadata.socket_device,
            "socket_inode": metadata.socket_inode,
        }
    )

    assert rejected == {"ok": False}
    assert accepted == {
        "ok": True,
        **metadata.handshake(),
    }
    assert shutdowns == ["self"]


def test_control_metadata_is_atomic_and_malformed_data_is_refused(tmp_path):
    """PID metadata is structured, private, and never parsed as a bare PID."""
    from trading_assistant.ops.control import (
        read_control_metadata,
        write_control_metadata,
    )

    metadata = _metadata(tmp_path)
    path = Path(metadata.cwd) / "logs/app.pid"
    path.parent.mkdir()
    write_control_metadata(path, metadata)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_control_metadata(path) == metadata
    path.write_text("4242\n", encoding="utf-8")
    assert read_control_metadata(path) is None
    malformed = asdict(metadata)
    malformed["pid"] = "4242"
    path.write_text(json.dumps(malformed), encoding="utf-8")
    assert read_control_metadata(path) is None


def test_valid_cooperative_stop_sends_no_external_pid_signal(tmp_path):
    """The client sends a local handshake; only the app controls its own exit."""
    from trading_assistant.ops.control import request_cooperative_stop

    metadata = _metadata(tmp_path)
    requests: list[dict[str, object]] = []
    accepted = request_cooperative_stop(
        metadata,
        project=Path(metadata.cwd),
        expected_argv=metadata.argv,
        inspect_process=lambda _pid: _snapshot(metadata),
        socket_lstat=lambda _path: _socket_stat(),
        requester=lambda _path, request: requests.append(request) or {
            "ok": True,
            **metadata.handshake(),
        },
    )

    assert accepted is True
    assert requests == [
        {
            "operation": "shutdown",
            "instance_id": metadata.instance_id,
            "pid": metadata.pid,
            "start_identity": metadata.start_identity,
            "cwd": metadata.cwd,
            "argv": metadata.argv,
            "socket_device": metadata.socket_device,
            "socket_inode": metadata.socket_inode,
        }
    ]
