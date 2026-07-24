"""Process-safe ordering barrier for submissions and execution-risk writers."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import local

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


_ownership = local()


def _owned_paths() -> dict[str, int]:
    paths = getattr(_ownership, "paths", None)
    if paths is None:
        paths = {}
        _ownership.paths = paths
    return paths


class SubmissionGuard:
    """Claim gate for a snapshot evaluated under the main process lock."""

    def __init__(self, barrier: "SubmissionBarrier") -> None:
        self._barrier = barrier

    @contextmanager
    def claim_if_current(self) -> Iterator[bool]:
        """Exclude writers through claim/send/persist, or report stale risk."""
        descriptor = self._barrier._open(self._barrier.intent_path)
        acquired = False
        try:
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                yield False
                return
            acquired = True
            yield True
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


class SubmissionBarrier:
    """OS locks shared by every process using one SQLite database.

    The sidecar lock is independent of SQLite transactions, so a submission can
    retain ordering ownership across broker I/O without holding a database
    transaction open. Risk writers first take a shared intent lock, then the
    exclusive main lock. A submission takes both exclusively to begin a fresh
    snapshot, releases intent during evaluation, and reacquires it
    non-blockingly before claim. A queued writer therefore invalidates the
    evaluated snapshot instead of waiting until after a stale broker send.

    ``flock`` releases every lock automatically if a process exits.
    """

    def __init__(
        self,
        source: sessionmaker[Session] | Session | Engine,
    ) -> None:
        if isinstance(source, Session):
            bind = source.get_bind()
        elif isinstance(source, Engine):
            bind = source
        else:
            bind = source.kw.get("bind")
        if not isinstance(bind, Engine) or bind.dialect.name != "sqlite":
            raise ValueError("submission barrier requires a bound SQLite engine")
        database = bind.url.database
        if not database or database == ":memory:":
            raise ValueError(
                "submission barrier requires file-backed SQLite storage"
            )
        database_path = Path(database).expanduser().resolve()
        self.path = database_path.with_name(
            f"{database_path.name}.submission.lock"
        )
        self.intent_path = self.path.with_name(f"{self.path.name}.intent")

    @staticmethod
    def _open(path: Path) -> int:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        return descriptor

    def _is_owned_by_current_thread(self) -> bool:
        return _owned_paths().get(str(self.path), 0) > 0

    @contextmanager
    def _mark_owned(self) -> Iterator[None]:
        key = str(self.path)
        paths = _owned_paths()
        paths[key] = paths.get(key, 0) + 1
        try:
            yield
        finally:
            remaining = paths[key] - 1
            if remaining:
                paths[key] = remaining
            else:
                del paths[key]

    @contextmanager
    def hold_writer(self) -> Iterator[None]:
        """Announce and serialize one execution-risk write through commit."""
        if self._is_owned_by_current_thread():
            yield
            return

        intent_descriptor = self._open(self.intent_path)
        main_descriptor = self._open(self.path)
        try:
            fcntl.flock(intent_descriptor, fcntl.LOCK_SH)
            fcntl.flock(main_descriptor, fcntl.LOCK_EX)
            with self._mark_owned():
                yield
        finally:
            fcntl.flock(main_descriptor, fcntl.LOCK_UN)
            os.close(main_descriptor)
            fcntl.flock(intent_descriptor, fcntl.LOCK_UN)
            os.close(intent_descriptor)

    @contextmanager
    def hold_submission(self) -> Iterator[SubmissionGuard]:
        """Start a snapshot only after all earlier risk writers commit."""
        if self._is_owned_by_current_thread():
            raise RuntimeError("submission barrier cannot be nested")

        intent_descriptor = self._open(self.intent_path)
        main_descriptor = self._open(self.path)
        intent_locked = False
        try:
            fcntl.flock(intent_descriptor, fcntl.LOCK_EX)
            intent_locked = True
            fcntl.flock(main_descriptor, fcntl.LOCK_EX)
            fcntl.flock(intent_descriptor, fcntl.LOCK_UN)
            intent_locked = False
            with self._mark_owned():
                yield SubmissionGuard(self)
        finally:
            if intent_locked:
                fcntl.flock(intent_descriptor, fcntl.LOCK_UN)
            os.close(intent_descriptor)
            fcntl.flock(main_descriptor, fcntl.LOCK_UN)
            os.close(main_descriptor)

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Backward-compatible writer barrier."""
        with self.hold_writer():
            yield
