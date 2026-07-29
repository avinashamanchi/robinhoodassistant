"""Python-process socket guard used only by the offline release verifier.

This is intentionally described as a Python guard, not an OS network sandbox.
It blocks Python AF_INET/AF_INET6 sockets and DNS helpers while preserving
AF_UNIX sockets needed by local test infrastructure.
"""

from __future__ import annotations

import errno
import os
import _socket
import socket


_GUARD_MARKER = "python_socket_guard_v1"
_INSTALLED = False


def _blocked(*_args, **_kwargs):
    raise OSError(errno.EPERM, "python_network_guard_blocked")


def install_network_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    if os.environ.get("TRADING_ASSISTANT_PYTHON_NETWORK_GUARD") != _GUARD_MARKER:
        raise RuntimeError("python_network_guard_marker_missing")

    original_socket = socket.socket
    original_raw_socket = _socket.socket

    class GuardedRawSocket:
        __init__ = original_raw_socket.__init__

        def __new__(
            cls,
            family: int = -1,
            type: int = -1,
            proto: int = -1,
            fileno: int | None = None,
        ):
            del cls
            if family != socket.AF_UNIX:
                _blocked()
            return original_raw_socket(family, type, proto, fileno)

    class GuardedSocket(original_socket):
        def __init__(
            self,
            family: int = -1,
            type: int = -1,
            proto: int = -1,
            fileno: int | None = None,
        ) -> None:
            if family != socket.AF_UNIX:
                _blocked()
            super().__init__(family, type, proto, fileno)

    socket.socket = GuardedSocket
    socket.SocketType = GuardedRawSocket
    _socket.socket = GuardedRawSocket
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked
    socket.getfqdn = _blocked
    socket.gethostbyname = _blocked
    socket.gethostbyname_ex = _blocked
    socket.gethostbyaddr = _blocked
    socket.getnameinfo = _blocked
    _socket.getaddrinfo = _blocked
    _socket.gethostbyname = _blocked
    if hasattr(_socket, "gethostbyname_ex"):
        _socket.gethostbyname_ex = _blocked
    _socket.gethostbyaddr = _blocked
    _socket.getnameinfo = _blocked
    _INSTALLED = True
