"""Private, bounded operational metadata. This store grants no trading authority."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat

LIMIT = 256 * 1024


class StateError(RuntimeError):
    """Operational state is unsafe, invalid, or already owned."""


def _name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise StateError("invalid operational state name")
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise StateError("duplicate operational state key")
        result[key] = value
    return result


class StateStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve(strict=True)

    @contextmanager
    def _directory(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in (".local", "operations"):
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                info = os.fstat(child)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    os.close(child)
                    raise StateError("operational directory must be private and owned")
                os.close(fd)
                fd = child
            yield fd
        except OSError as exc:
            raise StateError("cannot access private operational directory") from exc
        finally:
            os.close(fd)

    @staticmethod
    def _open(directory: int, name: str, *, create=False):
        flags = os.O_RDWR if create else os.O_RDONLY
        if create:
            flags |= os.O_CREAT
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > LIMIT):
            os.close(fd)
            raise StateError("operational file must be a bounded private regular file")
        return fd

    def _load(self, directory, name):
        try:
            fd = self._open(directory, name + ".json")
        except FileNotFoundError:
            return {}
        try:
            with os.fdopen(fd, "rb") as source:
                raw = source.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise StateError("operational state too large")
            value = json.loads(raw, object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(StateError("nonfinite state")))
            if not isinstance(value, dict):
                raise StateError("operational state must be an object")
            return value
        except (ValueError, UnicodeError) as exc:
            raise StateError("invalid operational JSON") from exc

    def load(self, name):
        name = _name(name)
        try:
            with self._directory() as directory:
                return self._load(directory, name)
        except OSError as exc:
            raise StateError("cannot read operational state") from exc

    @contextmanager
    def lease(self, name):
        name = _name(name)
        try:
            with self._directory() as directory:
                fd = self._open(directory, name + ".lock", create=True)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    yield
                finally:
                    os.close(fd)
        except OSError as exc:
            raise StateError("operational state is unavailable or already owned") from exc

    def update(self, name, change):
        name = _name(name)
        with self.lease(name):
            with self._directory() as directory:
                value = change(self._load(directory, name))
                try:
                    if not isinstance(value, dict):
                        raise ValueError("object required")
                    raw = (json.dumps(value, allow_nan=False, sort_keys=True) + "\n").encode()
                    if len(raw) > LIMIT:
                        raise ValueError("too large")
                except (TypeError, ValueError) as exc:
                    raise StateError("invalid replacement operational state") from exc
                temporary = "." + name + "-" + secrets.token_hex(12)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, "wb") as target:
                        target.write(raw)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, name + ".json", src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                return value
