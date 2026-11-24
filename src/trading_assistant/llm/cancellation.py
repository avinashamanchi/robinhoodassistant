"""Cooperative cancellation shared by an async research owner and its sync worker."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event

from ..dependencies import RequiredDependencyUnavailable

_current: ContextVar[Event | None] = ContextVar("research_cancellation", default=None)


class ResearchCancelled(RequiredDependencyUnavailable):
    pass


def cancellation_event() -> Event:
    return _current.get() or Event()


@contextmanager
def research_cancellation():
    event = Event()
    token = _current.set(event)
    try:
        yield event
    finally:
        event.set()
        _current.reset(token)
