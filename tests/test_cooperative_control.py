"""The app-only control channel never delegates shutdown to a PID signal."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
import socket
import stat
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from types import SimpleNamespace

import pytest


@contextmanager
def _short_project():
    """Keep AF_UNIX paths below macOS's fixed sockaddr length."""
    macos_short_root = Path("/private/tmp")
    short_root = macos_short_root if macos_short_root.is_dir() else Path(gettempdir())
    with TemporaryDirectory(prefix="ta-", dir=short_root) as directory:
        project = Path(directory) / "p"
        project.mkdir()
        yield project


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
        socket_mtime_ns=1_000,
        socket_ctime_ns=1_000,
    )


def _socket_stat(
    device=101,
    inode=202,
    mtime_ns=1_000,
    ctime_ns=1_000,
):
    return SimpleNamespace(
        st_mode=stat.S_IFSOCK | 0o600,
        st_dev=device,
        st_ino=inode,
        st_mtime_ns=mtime_ns,
        st_ctime_ns=ctime_ns,
    )


def test_stable_path_identity_rejects_reused_device_and_inode():
    from trading_assistant.ops.control import _stable_path_identity_matches

    before = SimpleNamespace(
        st_dev=101,
        st_ino=202,
        st_mode=stat.S_IFSOCK | 0o600,
        st_uid=os.getuid(),
        st_gid=os.getgid(),
        st_nlink=1,
        st_size=0,
        st_mtime_ns=1_000,
        st_ctime_ns=1_000,
    )
    replacement = SimpleNamespace(
        **{
            **vars(before),
            "st_ctime_ns": 1_001,
        },
    )

    assert _stable_path_identity_matches(before, before)
    assert not _stable_path_identity_matches(before, replacement)


def test_live_identity_rejects_reused_inode_with_changed_timestamp(tmp_path):
    from trading_assistant.ops.control import _matching_live_identity

    metadata = _metadata(tmp_path)
    replacement = SimpleNamespace(
        st_mode=stat.S_IFSOCK | 0o600,
        st_dev=metadata.socket_device,
        st_ino=metadata.socket_inode,
        st_mtime_ns=metadata.socket_mtime_ns,
        st_ctime_ns=metadata.socket_ctime_ns + 1,
    )

    assert not _matching_live_identity(
        metadata,
        project=Path(metadata.cwd),
        expected_argv=metadata.argv,
        inspect_process_fn=lambda _pid: _snapshot(metadata),
        socket_lstat=lambda _path: replacement,
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


def test_process_inspection_uses_absolute_system_lsof(
    tmp_path,
    monkeypatch,
):
    from trading_assistant.ops import control

    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "/bin/ps" and argv[-1] == "command=":
            output = "/opt/local/Python -m trading_assistant.ops.serve\n"
        elif argv[0] == "/bin/ps":
            output = "Sun Jul 27 20:00:00 2026\n"
        else:
            output = f"p4242\nfcwd\nn{tmp_path}\n"
        return SimpleNamespace(
            returncode=0,
            stdout=output,
            stderr="",
        )

    monkeypatch.setattr(control.subprocess, "run", runner)

    snapshot = control.inspect_process(4242)

    assert snapshot is not None
    assert calls[2][0] == [
        "/usr/sbin/lsof",
        "-a",
        "-p",
        "4242",
        "-d",
        "cwd",
        "-Fn",
    ]


def test_listener_readiness_is_tied_to_exact_process_pid():
    from trading_assistant.ops.control import _process_listens_on_tcp

    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="p4242\nf12\n",
            stderr="",
        )

    assert _process_listens_on_tcp(
        4242,
        8020,
        runner=runner,
    )
    assert calls[0][0] == [
        "/usr/sbin/lsof",
        "-nP",
        "-a",
        "-p",
        "4242",
        "-iTCP:8020",
        "-sTCP:LISTEN",
        "-Fp",
    ]


def test_listener_readiness_refuses_another_process_pid():
    from trading_assistant.ops.control import _process_listens_on_tcp

    assert not _process_listens_on_tcp(
        4242,
        8020,
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="p4343\n",
            stderr="",
        ),
    )


def test_expected_app_argv_uses_kernel_observed_interpreter_path():
    """A venv symlink must not make launcher identity differ from macOS ps."""
    from trading_assistant.ops import control

    calls: list[tuple[int, str]] = []
    observed = (
        "/opt/homebrew/Frameworks/Python.framework/Versions/3.11/"
        "Resources/Python.app/Contents/MacOS/Python"
    )

    expected = control.expected_app_argv(
        pid=4242,
        process_output=lambda pid, field: (
            calls.append((pid, field)) or observed
        ),
    )

    assert expected == (
        f"{observed} -m trading_assistant.ops.serve"
    )
    assert calls == [(4242, "comm")]


def test_process_absence_refuses_ps_diagnostic_as_proof(monkeypatch):
    from trading_assistant.ops import control

    monkeypatch.setattr(
        control.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="ps: process id too large",
        ),
    )

    assert control._process_absent(100000) is False


def test_process_absence_accepts_only_clean_not_found_result(monkeypatch):
    from trading_assistant.ops import control

    monkeypatch.setattr(
        control.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="",
        ),
    )

    assert control._process_absent(4242) is True


def test_control_cli_prints_kernel_observed_app_argv(capsys):
    from trading_assistant.ops import control

    observed = "/opt/local/Python.app/Contents/MacOS/Python"

    result = control._cli(
        ["expected-argv"],
        process_output=lambda _pid, _field: observed,
    )

    assert result == 0
    assert capsys.readouterr().out == (
        f"{observed} -m trading_assistant.ops.serve\n"
    )


def test_start_control_reclaims_exact_socket_only_for_proven_dead_process(
):
    from trading_assistant.ops.control import (
        AppControlMetadata,
        ProcessSnapshot,
        start_app_control,
        write_control_metadata,
    )

    with _short_project() as project:
        runtime = project / "runtime"
        runtime.mkdir(mode=0o700)
        socket_path = runtime / "app-control.sock"
        stale_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale_listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        socket_info = socket_path.lstat()
        stale_listener.close()
        metadata_path = project / "logs/app.pid"
        stale = AppControlMetadata(
            instance_id="a" * 64,
            pid=4242,
            start_identity="ps-lstart-v1:stale",
            cwd=str(project),
            argv="/opt/local/Python -m trading_assistant.ops.serve",
            socket_path=str(socket_path),
            socket_device=socket_info.st_dev,
            socket_inode=socket_info.st_ino,
            socket_mtime_ns=socket_info.st_mtime_ns,
            socket_ctime_ns=socket_info.st_ctime_ns,
        )
        write_control_metadata(metadata_path, stale)
        absence_checks: list[int] = []

        server = start_app_control(
            project,
            on_shutdown=lambda: None,
            inspect_process_fn=lambda pid: ProcessSnapshot(
                pid=pid,
                start_identity="ps-lstart-v1:current",
                cwd=str(project),
                argv="/opt/local/Python -m trading_assistant.ops.serve",
            ),
            process_absent_fn=lambda pid: (
                absence_checks.append(pid) or True
            ),
        )
        try:
            assert server.metadata.pid == os.getpid()
            assert server.metadata != stale
            assert absence_checks == [stale.pid]
            assert socket_path.is_socket()
        finally:
            server.close()

        assert not os.path.lexists(socket_path)
        assert not metadata_path.exists()


def test_start_control_preserves_stale_artifacts_when_death_is_uncertain(
):
    from trading_assistant.ops.control import (
        AppControlMetadata,
        ControlError,
        start_app_control,
        write_control_metadata,
    )

    with _short_project() as project:
        runtime = project / "runtime"
        runtime.mkdir(mode=0o700)
        socket_path = runtime / "app-control.sock"
        stale_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale_listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        socket_info = socket_path.lstat()
        stale_listener.close()
        metadata_path = project / "logs/app.pid"
        stale = AppControlMetadata(
            instance_id="a" * 64,
            pid=4242,
            start_identity="ps-lstart-v1:stale",
            cwd=str(project),
            argv="/opt/local/Python -m trading_assistant.ops.serve",
            socket_path=str(socket_path),
            socket_device=socket_info.st_dev,
            socket_inode=socket_info.st_ino,
            socket_mtime_ns=socket_info.st_mtime_ns,
            socket_ctime_ns=socket_info.st_ctime_ns,
        )
        write_control_metadata(metadata_path, stale)

        with pytest.raises(ControlError, match="control_socket_already_exists"):
            start_app_control(
                project,
                inspect_process_fn=lambda _pid: None,
                process_absent_fn=lambda _pid: False,
            )

        assert socket_path.is_socket()
        assert metadata_path.exists()


def test_start_control_recovers_metadata_only_cleanup_residue():
    from trading_assistant.ops.control import (
        AppControlMetadata,
        ProcessSnapshot,
        start_app_control,
        write_control_metadata,
    )

    with _short_project() as project:
        runtime = project / "runtime"
        runtime.mkdir(mode=0o700)
        socket_path = runtime / "app-control.sock"
        metadata_path = project / "logs/app.pid"
        stale = AppControlMetadata(
            instance_id="a" * 64,
            pid=4242,
            start_identity="ps-lstart-v1:stale",
            cwd=str(project),
            argv="/opt/local/Python -m trading_assistant.ops.serve",
            socket_path=str(socket_path),
            socket_device=101,
            socket_inode=202,
            socket_mtime_ns=1_000,
            socket_ctime_ns=1_000,
        )
        write_control_metadata(metadata_path, stale)

        server = start_app_control(
            project,
            on_shutdown=lambda: None,
            inspect_process_fn=lambda pid: ProcessSnapshot(
                pid=pid,
                start_identity="ps-lstart-v1:current",
                cwd=str(project),
                argv="/opt/local/Python -m trading_assistant.ops.serve",
            ),
            process_absent_fn=lambda pid: pid == stale.pid,
        )
        try:
            assert server.metadata != stale
            assert socket_path.is_socket()
        finally:
            server.close()


def test_reclaim_refuses_socket_replaced_during_identity_recheck(monkeypatch):
    from trading_assistant.ops import control
    from trading_assistant.ops.control import (
        AppControlMetadata,
        write_control_metadata,
    )

    with _short_project() as project:
        runtime = project / "runtime"
        runtime.mkdir(mode=0o700)
        socket_path = runtime / "app-control.sock"
        stale_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale_listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        stale_info = socket_path.lstat()
        stale_listener.close()
        metadata_path = project / "logs/app.pid"
        stale = AppControlMetadata(
            instance_id="a" * 64,
            pid=4242,
            start_identity="ps-lstart-v1:stale",
            cwd=str(project),
            argv="/opt/local/Python -m trading_assistant.ops.serve",
            socket_path=str(socket_path),
            socket_device=stale_info.st_dev,
            socket_inode=stale_info.st_ino,
            socket_mtime_ns=stale_info.st_mtime_ns,
            socket_ctime_ns=stale_info.st_ctime_ns,
        )
        write_control_metadata(metadata_path, stale)
        real_read = control.read_control_metadata
        reads = 0
        replacement_listener = None

        def replacing_read(path):
            nonlocal reads, replacement_listener
            reads += 1
            if reads == 2:
                socket_path.unlink()
                replacement_listener = socket.socket(
                    socket.AF_UNIX,
                    socket.SOCK_STREAM,
                )
                replacement_listener.bind(str(socket_path))
                os.chmod(socket_path, 0o600)
            return real_read(path)

        monkeypatch.setattr(
            control,
            "read_control_metadata",
            replacing_read,
        )
        try:
            reclaimed = control._reclaim_stale_control(
                root=project,
                socket_path=socket_path,
                metadata_path=metadata_path,
                process_absent_fn=lambda _pid: True,
            )

            assert reclaimed is False
            assert socket_path.is_socket()
            assert metadata_path.exists()
        finally:
            if replacement_listener is not None:
                replacement_listener.close()
            socket_path.unlink(missing_ok=True)


def test_reclaim_refuses_metadata_replaced_during_identity_recheck(
    monkeypatch,
):
    from trading_assistant.ops import control
    from trading_assistant.ops.control import (
        AppControlMetadata,
        write_control_metadata,
    )

    with _short_project() as project:
        runtime = project / "runtime"
        runtime.mkdir(mode=0o700)
        socket_path = runtime / "app-control.sock"
        stale_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale_listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        socket_info = socket_path.lstat()
        stale_listener.close()
        metadata_path = project / "logs/app.pid"
        stale = AppControlMetadata(
            instance_id="a" * 64,
            pid=4242,
            start_identity="ps-lstart-v1:stale",
            cwd=str(project),
            argv="/opt/local/Python -m trading_assistant.ops.serve",
            socket_path=str(socket_path),
            socket_device=socket_info.st_dev,
            socket_inode=socket_info.st_ino,
            socket_mtime_ns=socket_info.st_mtime_ns,
            socket_ctime_ns=socket_info.st_ctime_ns,
        )
        write_control_metadata(metadata_path, stale)
        real_read = control.read_control_metadata
        reads = 0

        def replacing_read(path):
            nonlocal reads
            reads += 1
            if reads == 2:
                write_control_metadata(metadata_path, stale)
            return real_read(path)

        monkeypatch.setattr(
            control,
            "read_control_metadata",
            replacing_read,
        )

        reclaimed = control._reclaim_stale_control(
            root=project,
            socket_path=socket_path,
            metadata_path=metadata_path,
            process_absent_fn=lambda _pid: True,
        )

        assert reclaimed is False
        assert socket_path.is_socket()
        assert metadata_path.exists()
        socket_path.unlink()


def test_startup_lock_refuses_a_second_starter():
    from trading_assistant.ops.control import (
        ControlError,
        _exclusive_startup_lock,
    )

    with _short_project() as project:
        runtime = project / "runtime"
        runtime.mkdir(mode=0o700)

        with _exclusive_startup_lock(runtime):
            with pytest.raises(
                ControlError,
                match="^control_start_in_progress$",
            ):
                with _exclusive_startup_lock(runtime):
                    raise AssertionError("second starter acquired lock")


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
            "socket_mtime_ns": metadata.socket_mtime_ns,
            "socket_ctime_ns": metadata.socket_ctime_ns,
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
            "socket_mtime_ns": metadata.socket_mtime_ns,
            "socket_ctime_ns": metadata.socket_ctime_ns,
        }
    ]


def test_start_intent_blocks_absence_until_matching_app_consumes_it():
    from trading_assistant.ops.control import (
        begin_start_intent,
        prove_start_absent,
        start_app_control,
    )

    with _short_project() as project:
        instance_id = "c" * 64
        begin_start_intent(project, instance_id=instance_id)

        assert prove_start_absent(project) is False

        metadata = None

        def inspect(pid):
            nonlocal metadata
            return SimpleNamespace(
                pid=pid,
                start_identity="start",
                cwd=str(project),
                argv="/opt/local/Python -m trading_assistant.ops.serve",
            )

        server = start_app_control(
            project,
            instance_id=instance_id,
            inspect_process_fn=inspect,
        )
        metadata = server.metadata
        try:
            assert prove_start_absent(project) is True
        finally:
            server.close()


def test_abandon_start_intent_requires_exact_instance_and_absent_child():
    from trading_assistant.ops.control import (
        abandon_start_intent,
        begin_start_intent,
        prove_start_absent,
    )

    with _short_project() as project:
        instance_id = "d" * 64
        begin_start_intent(project, instance_id=instance_id)

        assert (
            abandon_start_intent(
                project,
                instance_id="e" * 64,
                child_pid=4242,
                process_absent_fn=lambda _pid: True,
            )
            is False
        )
        assert (
            abandon_start_intent(
                project,
                instance_id=instance_id,
                child_pid=4242,
                process_absent_fn=lambda _pid: False,
            )
            is False
        )
        assert prove_start_absent(project) is False
        assert (
            abandon_start_intent(
                project,
                instance_id=instance_id,
                child_pid=4242,
                process_absent_fn=lambda _pid: True,
            )
            is True
        )
        assert prove_start_absent(project) is True


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    (
        (1, "", "", True),
        (0, "p4242\n", "", False),
        (1, "", "lsof: diagnostic\n", False),
        (2, "", "", False),
    ),
)
def test_tcp_port_absence_requires_clean_lsof_proof(
    returncode,
    stdout,
    stderr,
    expected,
):
    from trading_assistant.ops.control import prove_tcp_port_absent

    result = prove_tcp_port_absent(
        8020,
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )

    assert result is expected
