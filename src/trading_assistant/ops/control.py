"""Private cooperative shutdown control for the local HTTPS app process."""

from __future__ import annotations

import argparse
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


def _process_output(pid: int, field: str) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", f"{field}="],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = _trim(result.stdout)
    return value or None


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
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
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
) -> CooperativeControlServer:
    """Bind the private socket then atomically publish this app's identity."""
    root, _runtime, socket_path, metadata_path = _runtime_paths(project)
    if os.path.lexists(socket_path):
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
    identity = instance_id or os.environ.get("TRADING_APP_INSTANCE_ID")
    metadata = AppControlMetadata(
        instance_id=identity or secrets.token_hex(_INSTANCE_ID_BYTES),
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
    except Exception:
        server.close()
        raise
    return server


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stop", "validate"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--expected-argv", required=True)
    args = parser.parse_args(argv)
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
    return int(
        not request_cooperative_stop(
            metadata,
            project=args.project,
            expected_argv=args.expected_argv,
        )
    )


if __name__ == "__main__":
    raise SystemExit(_cli())
