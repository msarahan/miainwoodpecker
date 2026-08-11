"""
Unit tests for the client's bounded connection attempt.

These exist because of a hang that reached ``main``: a server whose
instrument accept thread crashed at startup kept the TCP port open (the
listener socket survives the thread) but never completed the
authentication handshake, and ``multiprocessing.connection.Client``
blocks with no timeout — so the client hung forever *inside* its first
attempt, where the retry loop's deadline could never fire. Every test
run that touched the device server then hung until something external
killed it.

No device extra needed: the wedged server is simulated with a bare
socket that accepts TCP and then says nothing, which is exactly what the
real failure looked like from the client's side.
"""

from __future__ import annotations

import multiprocessing.connection
import socket
import threading
import time

import pytest

from miainwoodpecker.devices.remote import DeviceServerStartupError, _connect_once

_AUTHKEY = b"x" * 32
# Generous ceiling for the bounded-failure assertion: the deadline is
# 0.5s, so anything near this means the bound is not working.
_BOUNDED_FAILURE_CEILING_S = 5.0


def test_a_working_listener_connects_and_authenticates():
    """The happy path: a real Listener with the right key answers quickly."""
    listener = multiprocessing.connection.Listener(
        ("localhost", 0), authkey=_AUTHKEY
    )
    port = listener.address[1]

    def _serve_one() -> None:
        with listener.accept() as server_end:
            server_end.send("hello")

    thread = threading.Thread(target=_serve_one, daemon=True)
    thread.start()
    try:
        connection = _connect_once(port, _AUTHKEY, time.monotonic() + 10.0)
        try:
            assert connection.recv() == "hello"
        finally:
            connection.close()
        thread.join(timeout=5)
    finally:
        listener.close()


def test_a_wedged_handshake_fails_by_the_deadline_instead_of_hanging():
    """
    A server that accepts TCP but never handshakes must not hang the client.

    The raw socket below listens and is never accepted from, so a
    client's connect() succeeds against the backlog and its handshake
    then waits for a challenge that will never come — the exact state a
    crashed accept thread leaves behind. The attempt must end with a
    diagnosis by the deadline, not block until something kills the
    process.
    """
    quiet = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    quiet.bind(("localhost", 0))
    quiet.listen(1)
    port = quiet.getsockname()[1]
    try:
        started = time.monotonic()
        with pytest.raises(DeviceServerStartupError, match="not completing"):
            _connect_once(port, _AUTHKEY, time.monotonic() + 0.5)
        # Bounded means bounded: well under the old forever, with slack
        # for a loaded CI machine.
        assert time.monotonic() - started < _BOUNDED_FAILURE_CEILING_S
    finally:
        quiet.close()


def test_nothing_listening_is_the_ordinary_retryable_refusal():
    """A dead port raises ConnectionRefusedError for the retry loop, fast."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("localhost", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing is listening here now

    with pytest.raises(ConnectionRefusedError):
        _connect_once(port, _AUTHKEY, time.monotonic() + 5.0)
