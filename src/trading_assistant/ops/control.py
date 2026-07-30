"""Private cooperative shutdown control for the local HTTPS app process."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import tempfile
from threading import Event, Thread
from typing import Callable, Mapping


_INSTANCE_ID_BYTES = 32
_MAX_MESSAGE_BYTES = 4096
_SOCKET_NAME = "app-control.sock"
_START_LOCK_NAME = "app-control.start.lock"
_START_INTENT_NAME = "app-control.starting"
_PROCESS_START_IDENTITY_PREFIX = "ps-lstart-v1:"
_PROCESS_INSPECTION_ENV = {
    "LC_ALL": "C",
    "LANG": "C",
    "TZ": "UTC",
}


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    start_identity: str
    cwd: str
    argv: str


@dataclass(frozen=True)
class AppControlMetadata:
    instance_id: str
    pid: int
    start_identity: str
    cwd: str
    argv: str
    socket_path: str
    socket_device: int
    socket_inode: int

    def handshake(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "pid": self.pid,
            "start_identity": self.start_identity,
            "cwd": self.cwd,
            "argv": self.argv,
            "socket_device": self.socket_device,
            "socket_inode": self.socket_inode,
        }


class ControlError(RuntimeError):
    """A stable local-control setup or validation failure."""


def _trim(value: str) -> str:
    return value.strip()


def _canonical_project(project: Path) -> Path:
    try:
        return project.resolve(strict=True)
    except OSError as exc:
        raise ControlError("project_root_invalid") from exc


def _runtime_paths(project: Path) -> tuple[Path, Path, Path, Path]:
    root = _canonical_project(project)
    runtime = root / "runtime"
    if runtime.is_symlink():
        raise ControlError("runtime_directory_symlink_forbidden")
    try:
        runtime.mkdir(mode=0o700, exist_ok=True)
        os.chmod(runtime, 0o700)
    except OSError as exc:
        raise ControlError("runtime_directory_invalid") from exc
    if not runtime.is_dir() or runtime.is_symlink():
        raise ControlError("runtime_directory_invalid")
    return root, runtime, runtime / _SOCKET_NAME, root / "logs/app.pid"


def _valid_instance_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == _INSTANCE_ID_BYTES * 2
        and all(character in "0123456789abcdef" for character in value)
    )


def _start_intent_path(runtime: Path) -> Path:
    return runtime / _START_INTENT_NAME


def _read_start_intent(path: Path) -> tuple[str, os.stat_result] | None:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            return None
        value = path.read_text(encoding="ascii").removesuffix("\n")
    except (OSError, UnicodeError):
        return None
    if not _valid_instance_id(value):
        return None
    return value, info


def begin_start_intent(project: Path, *, instance_id: str) -> None:
    """Publish a private intent before the manual launcher spawns the app."""
    if not _valid_instance_id(instance_id):
        raise ControlError("control_start_intent_invalid")
    _root, runtime, _socket_path, _metadata_path = _runtime_paths(project)
    intent_path = _start_intent_path(runtime)
    with _exclusive_startup_lock(runtime):
        descriptor: int | None = None
        try:
            descriptor = os.open(
                intent_path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, f"{instance_id}\n".encode("ascii"))
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            named = intent_path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or opened.st_dev != named.st_dev
                or opened.st_ino != named.st_ino
            ):
                raise ControlError("control_start_intent_invalid")
        except FileExistsError as exc:
            raise ControlError("control_start_already_pending") from exc
        except ControlError:
            try:
                intent_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except OSError as exc:
            try:
                intent_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ControlError("control_start_intent_invalid") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _consume_start_intent(path: Path, *, instance_id: str) -> None:
    if not os.path.lexists(path):
        return
    current = _read_start_intent(path)
    if current is None or current[0] != instance_id:
        raise ControlError("control_start_intent_mismatch")
    _value, initial = current
    try:
        latest = path.lstat()
        if (
            latest.st_dev != initial.st_dev
            or latest.st_ino != initial.st_ino
            or latest.st_uid != os.getuid()
            or latest.st_nlink != 1
            or not stat.S_ISREG(latest.st_mode)
        ):
            raise ControlError("control_start_intent_changed")
        path.unlink()
    except ControlError:
        raise
    except OSError as exc:
        raise ControlError("control_start_intent_changed") from exc


def abandon_start_intent(
    project: Path,
    *,
    instance_id: str,
    child_pid: int,
    process_absent_fn: Callable[[int], bool] | None = None,
) -> bool:
    """Clear a failed manual start only after its exact child is proven gone."""
    if not _valid_instance_id(instance_id) or child_pid <= 0:
        return False
    _root, runtime, socket_path, metadata_path = _runtime_paths(project)
    intent_path = _start_intent_path(runtime)
    absence_checker = process_absent_fn or _process_absent
    try:
        with _exclusive_startup_lock(runtime):
            if (
                os.path.lexists(socket_path)
                or os.path.lexists(metadata_path)
                or not absence_checker(child_pid)
            ):
                return False
            current = _read_start_intent(intent_path)
            if current is None or current[0] != instance_id:
                return False
            _consume_start_intent(
                intent_path,
                instance_id=instance_id,
            )
            return True
    except ControlError:
        return False


def prove_start_absent(project: Path) -> bool:
    """Prove no serialized manual start is pending at this instant."""
    try:
        _root, runtime, _socket_path, _metadata_path = _runtime_paths(project)
        with _exclusive_startup_lock(runtime):
            return not os.path.lexists(_start_intent_path(runtime))
    except ControlError:
        return False


def prove_tcp_port_absent(
    port: int,
    *,
    runner: Callable[..., object] = subprocess.run,
) -> bool:
    """Return true only for lsof's clean, explicit no-listener result."""
    if not 1 <= port <= 65535:
        return False
    try:
        result = runner(
            [
                "/usr/sbin/lsof",
                "-nP",
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-Fp",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
            env=dict(_PROCESS_INSPECTION_ENV),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    return bool(
        getattr(result, "returncode", None) == 1
        and isinstance(stdout, str)
        and not stdout.strip()
        and isinstance(stderr, str)
        and not stderr.strip()
    )


def prove_app_absent(project: Path, *, port: int) -> bool:
    """Atomically exclude a pending manual start and all app artifacts."""
    try:
        _root, runtime, socket_path, metadata_path = _runtime_paths(project)
        with _exclusive_startup_lock(runtime):
            return bool(
                not os.path.lexists(_start_intent_path(runtime))
                and not os.path.lexists(socket_path)
                and not os.path.lexists(metadata_path)
                and prove_tcp_port_absent(port)
            )
    except ControlError:
        return False


def _process_output(pid: int, field: str) -> str | None:
    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", f"{field}="],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
            env=dict(_PROCESS_INSPECTION_ENV),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = _trim(result.stdout)
    if not value:
        return None
    if field == "lstart":
        return f"{_PROCESS_START_IDENTITY_PREFIX}{' '.join(value.split())}"
    return value


def _process_absent(pid: int) -> bool:
    """Return true only when the kernel process table proves a PID is absent."""
    if pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "pid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
            env=dict(_PROCESS_INSPECTION_ENV),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        result.returncode == 1
        and not _trim(result.stdout)
        and not _trim(result.stderr)
    )


@contextmanager
def _exclusive_startup_lock(runtime: Path):
    """Serialize stale recovery, socket bind, and metadata publication."""
    lock_path = runtime / _START_LOCK_NAME
    descriptor: int | None = None
    locked = False
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
        ):
            raise ControlError("control_start_lock_invalid")
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        named = lock_path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or opened.st_dev != named.st_dev
            or opened.st_ino != named.st_ino
        ):
            raise ControlError("control_start_lock_invalid")
        try:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise ControlError("control_start_in_progress") from exc
        locked = True
        current = lock_path.lstat()
        if (
            current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
            or not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_uid != os.getuid()
            or current.st_nlink != 1
        ):
            raise ControlError("control_start_lock_changed")
        yield
    except ControlError:
        raise
    except OSError as exc:
        raise ControlError("control_start_lock_invalid") from exc
    finally:
        if descriptor is not None:
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)


def expected_app_argv(
    *,
    pid: int | None = None,
    process_output: Callable[[int, str], str | None] = _process_output,
) -> str:
    """Build the exact app command as the kernel process table will report it."""
    observed = process_output(
        os.getpid() if pid is None else pid,
        "comm",
    )
    if (
        observed is None
        or not Path(observed).is_absolute()
        or "\x00" in observed
        or "\n" in observed
    ):
        raise ControlError("control_process_command_unavailable")
    return f"{observed} -m trading_assistant.ops.serve"


def inspect_process(pid: int) -> ProcessSnapshot | None:
    """Return exact local process identity without ever signalling that PID."""
    if pid <= 0:
        return None
    argv = _process_output(pid, "command")
    start = _process_output(pid, "lstart")
    if argv is None or start is None:
        return None
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    cwd = next(
        (line[1:] for line in result.stdout.splitlines() if line.startswith("n")),
        "",
    )
    if not cwd:
        return None
    try:
        canonical_cwd = str(Path(cwd).resolve(strict=True))
    except OSError:
        return None
    return ProcessSnapshot(
        pid=pid,
        start_identity=" ".join(start.split()),
        cwd=canonical_cwd,
        argv=argv,
    )


def _process_listens_on_tcp(
    pid: int,
    port: int,
    *,
    runner: Callable[..., object] = subprocess.run,
) -> bool:
    if pid <= 0 or not 1 <= port <= 65535:
        return False
    try:
        result = runner(
            [
                "/usr/sbin/lsof",
                "-nP",
                "-a",
                "-p",
                str(pid),
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-Fp",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
            env=dict(_PROCESS_INSPECTION_ENV),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    process_fields = (
        [
            line
            for line in stdout.splitlines()
            if line.startswith("p")
        ]
        if isinstance(stdout, str)
        else []
    )
    return bool(
        getattr(result, "returncode", None) == 0
        and process_fields == [f"p{pid}"]
        and isinstance(stderr, str)
        and not stderr.strip()
    )


def _metadata_is_well_formed(metadata: AppControlMetadata) -> bool:
    return (
        isinstance(metadata.instance_id, str)
        and len(metadata.instance_id) == _INSTANCE_ID_BYTES * 2
        and all(character in "0123456789abcdef" for character in metadata.instance_id)
        and isinstance(metadata.pid, int)
        and not isinstance(metadata.pid, bool)
        and metadata.pid > 0
        and isinstance(metadata.start_identity, str)
        and bool(metadata.start_identity)
        and isinstance(metadata.cwd, str)
        and bool(metadata.cwd)
        and isinstance(metadata.argv, str)
        and bool(metadata.argv)
        and isinstance(metadata.socket_path, str)
        and Path(metadata.socket_path).is_absolute()
        and isinstance(metadata.socket_device, int)
        and not isinstance(metadata.socket_device, bool)
        and metadata.socket_device >= 0
        and isinstance(metadata.socket_inode, int)
        and not isinstance(metadata.socket_inode, bool)
        and metadata.socket_inode > 0
    )


def write_control_metadata(path: Path, metadata: AppControlMetadata) -> None:
    """Atomically publish private, structured metadata for this app instance."""
    if not _metadata_is_well_formed(metadata):
        raise ControlError("control_metadata_invalid")
    parent = path.parent
    if parent.is_symlink():
        raise ControlError("control_metadata_parent_symlink_forbidden")
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".app.pid.",
            dir=parent,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(asdict(metadata), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        try:
            Path(locals().get("temporary_name", "")).unlink(missing_ok=True)
        except OSError:
            pass
        raise ControlError("control_metadata_write_failed") from exc


def read_control_metadata(path: Path) -> AppControlMetadata | None:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "instance_id",
            "pid",
            "start_identity",
            "cwd",
            "argv",
            "socket_path",
            "socket_device",
            "socket_inode",
        }:
            return None
        metadata = AppControlMetadata(**payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return metadata if _metadata_is_well_formed(metadata) else None


def _matching_live_identity(
    metadata: AppControlMetadata,
    *,
    project: Path,
    expected_argv: str,
    inspect_process_fn: Callable[[int], ProcessSnapshot | None],
    socket_lstat: Callable[[Path], os.stat_result],
) -> bool:
    if not _metadata_is_well_formed(metadata):
        return False
    try:
        root = _canonical_project(project)
        runtime = root / "runtime"
        if runtime.is_symlink() or runtime.resolve(strict=True) != runtime:
            return False
        expected_socket = runtime / _SOCKET_NAME
        current = inspect_process_fn(metadata.pid)
        socket_info = socket_lstat(Path(metadata.socket_path))
    except (ControlError, OSError):
        return False
    return bool(
        metadata.cwd == str(root)
        and metadata.argv == expected_argv
        and Path(metadata.socket_path) == expected_socket
        and current
        and current.pid == metadata.pid
        and current.start_identity == metadata.start_identity
        and current.cwd == metadata.cwd
        and current.argv == metadata.argv
        and stat.S_ISSOCK(socket_info.st_mode)
        and stat.S_IMODE(socket_info.st_mode) == 0o600
        and socket_info.st_dev == metadata.socket_device
        and socket_info.st_ino == metadata.socket_inode
    )


def _reclaim_stale_control(
    *,
    root: Path,
    socket_path: Path,
    metadata_path: Path,
    process_absent_fn: Callable[[int], bool],
) -> bool:
    """Remove only an exact prior socket whose recorded PID is proven gone."""
    try:
        metadata_info = metadata_path.lstat()
    except OSError:
        return False
    metadata = read_control_metadata(metadata_path)
    if (
        metadata is None
        or not stat.S_ISREG(metadata_info.st_mode)
        or stat.S_IMODE(metadata_info.st_mode) != 0o600
        or metadata_info.st_uid != os.getuid()
        or metadata_info.st_nlink != 1
        or metadata.cwd != str(root)
        or Path(metadata.socket_path) != socket_path
        or not process_absent_fn(metadata.pid)
    ):
        return False
    try:
        socket_info = socket_path.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISSOCK(socket_info.st_mode)
        or stat.S_IMODE(socket_info.st_mode) != 0o600
        or socket_info.st_dev != metadata.socket_device
        or socket_info.st_ino != metadata.socket_inode
        or read_control_metadata(metadata_path) != metadata
    ):
        return False
    try:
        current_socket = socket_path.lstat()
        current_metadata = metadata_path.lstat()
        if (
            not stat.S_ISSOCK(current_socket.st_mode)
            or stat.S_IMODE(current_socket.st_mode) != 0o600
            or current_socket.st_dev != socket_info.st_dev
            or current_socket.st_ino != socket_info.st_ino
            or not stat.S_ISREG(current_metadata.st_mode)
            or current_metadata.st_dev != metadata_info.st_dev
            or current_metadata.st_ino != metadata_info.st_ino
            or current_metadata.st_uid != os.getuid()
            or current_metadata.st_nlink != 1
        ):
            return False
        socket_path.unlink()
        metadata_path.unlink()
    except OSError:
        return False
    return True


def _reclaim_stale_metadata_only(
    *,
    root: Path,
    socket_path: Path,
    metadata_path: Path,
    process_absent_fn: Callable[[int], bool],
) -> bool:
    """Recover the only safe partial state from a prior two-step cleanup."""
    if os.path.lexists(socket_path):
        return False
    try:
        initial = metadata_path.lstat()
    except OSError:
        return False
    metadata = read_control_metadata(metadata_path)
    if (
        metadata is None
        or not stat.S_ISREG(initial.st_mode)
        or stat.S_IMODE(initial.st_mode) != 0o600
        or initial.st_uid != os.getuid()
        or initial.st_nlink != 1
        or metadata.cwd != str(root)
        or Path(metadata.socket_path) != socket_path
        or not process_absent_fn(metadata.pid)
        or os.path.lexists(socket_path)
        or read_control_metadata(metadata_path) != metadata
    ):
        return False
    try:
        current = metadata_path.lstat()
        if (
            current.st_dev != initial.st_dev
            or current.st_ino != initial.st_ino
            or current.st_uid != os.getuid()
            or current.st_nlink != 1
        ):
            return False
        metadata_path.unlink()
    except OSError:
        return False
    return True


def _socket_request(path: Path, request: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1.0)
        connection.connect(str(path))
        connection.sendall(encoded)
        response = connection.recv(_MAX_MESSAGE_BYTES + 1)
    if not response or len(response) > _MAX_MESSAGE_BYTES:
        return {}
    try:
        parsed = json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def request_cooperative_stop(
    metadata: AppControlMetadata,
    *,
    project: Path,
    expected_argv: str,
    inspect_process: Callable[[int], ProcessSnapshot | None] = inspect_process,
    socket_lstat: Callable[[Path], os.stat_result] = Path.lstat,
    requester: Callable[[Path, dict[str, object]], dict[str, object]] = _socket_request,
) -> bool:
    """Request self-shutdown only after every recorded local identity matches."""
    if not _matching_live_identity(
        metadata,
        project=project,
        expected_argv=expected_argv,
        inspect_process_fn=inspect_process,
        socket_lstat=socket_lstat,
    ):
        return False
    request = {"operation": "shutdown", **metadata.handshake()}
    try:
        response = requester(Path(metadata.socket_path), request)
    except (OSError, ValueError, TypeError):
        return False
    return response == {"ok": True, **metadata.handshake()}


class CooperativeControlServer:
    """The app-owned Unix socket that may ask only this process to stop itself."""

    def __init__(
        self,
        metadata: AppControlMetadata,
        *,
        on_shutdown: Callable[[], None],
        listener: socket.socket | None = None,
        metadata_path: Path | None = None,
    ) -> None:
        self.metadata = metadata
        self._on_shutdown = on_shutdown
        self._listener = listener
        self._metadata_path = metadata_path
        self._closed = Event()
        self._thread: Thread | None = None

    def handle_request(self, request: Mapping[str, object]) -> dict[str, object]:
        if request != {"operation": "shutdown", **self.metadata.handshake()}:
            return {"ok": False}
        self._on_shutdown()
        return {"ok": True, **self.metadata.handshake()}

    def start(self) -> None:
        if self._listener is None:
            return
        self._thread = Thread(target=self._serve, name="app-control", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._closed.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1.0)
                try:
                    payload = connection.recv(_MAX_MESSAGE_BYTES + 1)
                    request = json.loads(payload.decode("utf-8"))
                    response = self.handle_request(request)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    response = {"ok": False}
                try:
                    connection.sendall(json.dumps(response).encode("utf-8"))
                except OSError:
                    pass

    def close(self) -> None:
        self._closed.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        path = Path(self.metadata.socket_path)
        try:
            socket_info = path.lstat()
            if (
                stat.S_ISSOCK(socket_info.st_mode)
                and socket_info.st_dev == self.metadata.socket_device
                and socket_info.st_ino == self.metadata.socket_inode
            ):
                path.unlink()
        except OSError:
            pass
        if self._metadata_path is not None:
            current = read_control_metadata(self._metadata_path)
            if current == self.metadata:
                try:
                    self._metadata_path.unlink()
                except OSError:
                    pass


def _self_shutdown() -> None:
    """Only the serving process may signal itself after a validated request."""
    os.kill(os.getpid(), signal.SIGTERM)


def start_app_control(
    project: Path,
    *,
    instance_id: str | None = None,
    on_shutdown: Callable[[], None] = _self_shutdown,
    inspect_process_fn: Callable[[int], ProcessSnapshot | None] = inspect_process,
    process_absent_fn: Callable[[int], bool] = _process_absent,
) -> CooperativeControlServer:
    """Bind the private socket then atomically publish this app's identity."""
    root, runtime, socket_path, metadata_path = _runtime_paths(project)
    with _exclusive_startup_lock(runtime):
        identity = (
            instance_id
            or os.environ.get("TRADING_APP_INSTANCE_ID")
            or secrets.token_hex(_INSTANCE_ID_BYTES)
        )
        intent_path = _start_intent_path(runtime)
        if os.path.lexists(intent_path):
            intent = _read_start_intent(intent_path)
            if intent is None or intent[0] != identity:
                raise ControlError("control_start_intent_mismatch")
        artifacts_exist = (
            os.path.lexists(socket_path)
            or os.path.lexists(metadata_path)
        )
        if artifacts_exist:
            complete_pair = (
                os.path.lexists(socket_path)
                and os.path.lexists(metadata_path)
            )
            reclaimed = (
                _reclaim_stale_control(
                    root=root,
                    socket_path=socket_path,
                    metadata_path=metadata_path,
                    process_absent_fn=process_absent_fn,
                )
                if complete_pair
                else _reclaim_stale_metadata_only(
                    root=root,
                    socket_path=socket_path,
                    metadata_path=metadata_path,
                    process_absent_fn=process_absent_fn,
                )
            )
            if not reclaimed:
                raise ControlError("control_socket_already_exists")
        listener: socket.socket | None = None
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            listener.listen(1)
            listener.settimeout(0.2)
            socket_info = socket_path.lstat()
        except OSError as exc:
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            raise ControlError("control_socket_setup_failed") from exc
        snapshot = inspect_process_fn(os.getpid())
        if (
            snapshot is None
            or snapshot.pid != os.getpid()
            or snapshot.cwd != str(root)
        ):
            listener.close()
            socket_path.unlink(missing_ok=True)
            raise ControlError("control_process_identity_unavailable")
        metadata = AppControlMetadata(
            instance_id=identity,
            pid=snapshot.pid,
            start_identity=snapshot.start_identity,
            cwd=snapshot.cwd,
            argv=snapshot.argv,
            socket_path=str(socket_path),
            socket_device=socket_info.st_dev,
            socket_inode=socket_info.st_ino,
        )
        server = CooperativeControlServer(
            metadata,
            on_shutdown=on_shutdown,
            listener=listener,
            metadata_path=metadata_path,
        )
        try:
            write_control_metadata(metadata_path, metadata)
            server.start()
            _consume_start_intent(
                intent_path,
                instance_id=identity,
            )
        except Exception:
            server.close()
            raise
        return server


def _cli(
    argv: list[str] | None = None,
    *,
    process_output: Callable[[int, str], str | None] = _process_output,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "abandon-start",
            "app-absent",
            "begin-start",
            "expected-argv",
            "ready",
            "stop",
            "validate",
        ),
    )
    parser.add_argument("--project", type=Path)
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--expected-argv")
    parser.add_argument("--port", type=int)
    parser.add_argument("--instance-id")
    parser.add_argument("--child-pid", type=int)
    args = parser.parse_args(argv)
    if args.command == "expected-argv":
        try:
            print(expected_app_argv(process_output=process_output))
        except ControlError:
            return 1
        return 0
    if args.command == "begin-start":
        if args.project is None or args.instance_id is None:
            return 1
        try:
            begin_start_intent(
                args.project,
                instance_id=args.instance_id,
            )
        except ControlError:
            return 1
        return 0
    if args.command == "abandon-start":
        if (
            args.project is None
            or args.instance_id is None
            or args.child_pid is None
        ):
            return 1
        return int(
            not abandon_start_intent(
                args.project,
                instance_id=args.instance_id,
                child_pid=args.child_pid,
            )
        )
    if args.command == "app-absent":
        if args.project is None or args.port is None:
            return 1
        return int(
            not prove_app_absent(
                args.project,
                port=args.port,
            )
        )
    if (
        args.project is None
        or args.pid_file is None
        or args.expected_argv is None
    ):
        return 1
    metadata = read_control_metadata(args.pid_file)
    if metadata is None:
        return 1
    if args.command == "validate":
        return int(
            not _matching_live_identity(
                metadata,
                project=args.project,
                expected_argv=args.expected_argv,
                inspect_process_fn=inspect_process,
                socket_lstat=Path.lstat,
            )
        )
    if args.command == "ready":
        return int(
            args.port is None
            or not _matching_live_identity(
                metadata,
                project=args.project,
                expected_argv=args.expected_argv,
                inspect_process_fn=inspect_process,
                socket_lstat=Path.lstat,
            )
            or not _process_listens_on_tcp(
                metadata.pid,
                args.port,
            )
        )
    return int(
        not request_cooperative_stop(
            metadata,
            project=args.project,
            expected_argv=args.expected_argv,
        )
    )


if __name__ == "__main__":
    raise SystemExit(_cli())
