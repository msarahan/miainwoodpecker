"""
Unit tests for the RPC wire protocol.

``rpc.py`` is described as "the entire license boundary" (see
docs/migration-plan.md, §6), yet it was previously exercised only through
``tests/integration/test_remote_nion.py``, which skips unless the
``device`` extra is installed. In a base environment - which is what most
contributors and the default CI matrix job run - the module carrying the
project's central licensing argument had no coverage at all.

Nothing here needs a vendor SDK, a subprocess, or a device: the module is
pure standard library, so a ``socket.socketpair`` and a thread are enough
to drive every path.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from multiprocessing.connection import Connection

import pytest

from miainwoodpecker.devices.rpc import (
    BACKENDS,
    DEVICE_TARGET_NAMES,
    HARDWARE_BACKEND,
    SHARED_MEMORY_THRESHOLD_BYTES,
    SIMULATED_BACKEND,
    TARGET_NAMES,
    Call,
    RemoteCallError,
    RemoteCallTimeoutError,
    RemoteConnectionLostError,
    Result,
    disable_nagle,
    send_call,
)


@pytest.fixture
def connection_pair():
    """Yield two connected Connections standing in for client and server."""
    left, right = socket.socketpair()
    client = Connection(left.detach())
    server = Connection(right.detach())
    try:
        yield client, server
    finally:
        client.close()
        server.close()


def _serve_once(server: Connection, result: Result) -> threading.Thread:
    """Answer exactly one call with a fixed result, on a worker thread."""

    def _run() -> None:
        server.recv()
        server.send(result)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def test_a_successful_call_returns_the_servers_value(connection_pair):
    """The happy path: one round trip, the value comes back unwrapped."""
    client, server = connection_pair
    thread = _serve_once(server, Result(value=[1, 2, 3]))
    assert send_call(client, threading.Lock(), Call("scanner", "channel_names")) == [
        1,
        2,
        3,
    ]
    thread.join(timeout=5)


def test_a_server_side_error_becomes_a_remote_call_error(connection_pair):
    """A stringified server exception is raised client-side, naming the call."""
    client, server = connection_pair
    thread = _serve_once(server, Result(error="ValueError: bad channel"))
    with pytest.raises(RemoteCallError, match=r"scanner\.scan_frame\(\).*bad channel"):
        send_call(client, threading.Lock(), Call("scanner", "scan_frame"))
    thread.join(timeout=5)


def test_a_closed_server_becomes_a_connection_lost_error(connection_pair):
    """
    A dead server must not surface as a bare, messageless EOFError.

    This is the failure the client sees when the device server process
    dies mid-call, and the whole reason the wrapper exists.
    """
    client, server = connection_pair
    server.close()
    with pytest.raises(RemoteConnectionLostError, match="lost its connection"):
        send_call(client, threading.Lock(), Call("camera", "acquire_frame"))


def test_a_silent_server_times_out_when_a_timeout_is_requested(connection_pair):
    """
    An opt-in timeout bounds the wait; the message names the call and the limit.

    Ordinary device calls pass no timeout deliberately (an acquisition
    takes as long as it takes), so this path only runs for the shutdown
    handshake and the health check.
    """
    client, _server = connection_pair
    pattern = r"instrument\.shutdown\(\).*0\.05"
    with pytest.raises(RemoteCallTimeoutError, match=pattern):
        send_call(
            client,
            threading.Lock(),
            Call("instrument", "shutdown"),
            timeout_s=0.05,
        )


def test_a_timeout_is_also_a_remote_call_error(connection_pair):
    """Callers that only care 'the call failed' need one except clause."""
    client, _server = connection_pair
    with pytest.raises(RemoteCallError):
        send_call(
            client,
            threading.Lock(),
            Call("instrument", "health"),
            timeout_s=0.05,
        )


def test_the_lock_serializes_concurrent_callers(connection_pair):
    """
    Two threads sharing one connection must not interleave their round trips.

    Without the lock a send/recv pair can interleave and each caller gets
    the other's reply - which for a frame-returning call is how a client
    ends up copying out of a segment the server is refilling.
    """
    client, server = connection_pair
    lock = threading.Lock()

    def _echo_forever() -> None:
        while True:
            try:
                call = server.recv()
            except (EOFError, OSError):
                return
            server.send(Result(value=call.args[0]))

    responder = threading.Thread(target=_echo_forever, daemon=True)
    responder.start()

    mismatches: list[tuple[int, object]] = []

    def _hammer(tag: int) -> None:
        for _ in range(50):
            answer = send_call(client, lock, Call("scanner", "echo", (tag,)))
            if answer != tag:
                mismatches.append((tag, answer))

    threads = [threading.Thread(target=_hammer, args=(tag,)) for tag in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not mismatches


def test_disable_nagle_preserves_the_fds_blocking_mode(connection_pair):
    """
    Wrapping the fd to set TCP_NODELAY must not leave it non-blocking.

    ``socket.socket(fileno=...)`` applies the process-wide default socket
    timeout at construction, and setting a timeout puts the underlying fd
    into non-blocking mode - which ``detach()`` does not undo. So any
    library anywhere in the process calling ``setdefaulttimeout`` (an
    HTTP-capable napari plugin, say) would silently switch every
    connection this project opens to non-blocking, and ``recv`` would then
    raise ``BlockingIOError``. That is an ``OSError``, so ``send_call``
    would report it as a lost connection: the operator is told the device
    server died when it is perfectly healthy.
    """
    client, server = connection_pair
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5.0)
    try:
        disable_nagle(client)
    finally:
        socket.setdefaulttimeout(previous)

    # Asserted through behaviour rather than through os.get_blocking, which
    # is Unix-only: a non-blocking endpoint raises BlockingIOError here
    # instead of waiting for the reply.
    thread = _serve_once(server, Result(value="alive"))
    assert send_call(client, threading.Lock(), Call("instrument", "health")) == "alive"
    thread.join(timeout=5)


def test_disable_nagle_still_delivers_calls_after_a_default_timeout_is_set():
    """The end-to-end consequence: a round trip still works, not just the flag."""
    left, right = socket.socketpair()
    client = Connection(left.detach())
    server = Connection(right.detach())
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(5.0)
    try:
        disable_nagle(client)
        disable_nagle(server)
        thread = _serve_once(server, Result(value="alive"))
        assert send_call(client, threading.Lock(), Call("instrument", "health")) == (
            "alive"
        )
        thread.join(timeout=5)
    finally:
        socket.setdefaulttimeout(previous)
        client.close()
        server.close()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.pipe() fds are not valid multiprocessing Connection handles on Windows",
)
def test_disable_nagle_is_a_no_op_on_a_non_socket_connection():
    """
    A pipe-backed Connection has no TCP_NODELAY, and that is not an error.

    Windows named pipes take this same path in production - the wrap
    raises and the function returns - which is why it documents itself as
    silently doing nothing rather than refusing to run there.
    """
    read_fd, write_fd = os.pipe()
    reader = Connection(read_fd, writable=False)
    writer = Connection(write_fd, readable=False)
    try:
        disable_nagle(reader)  # must not raise
        writer.send(b"still usable")
        assert reader.recv() == b"still usable"
    finally:
        reader.close()
        writer.close()


def test_call_and_result_survive_a_round_trip_as_plain_data(connection_pair):
    """
    The protocol carries only plain data, which is what the boundary rests on.

    Two programs exchanging dataclasses of builtins is the "arm's length"
    arrangement §6 relies on; a Call that only worked by shipping a live
    object would quietly undermine it.
    """
    client, server = connection_pair
    original = Call("instrument", "set_defocus_nm", (12.5,), {"note": "abc"})
    client.send(original)
    received = server.recv()
    assert received == original
    server.send(Result(value={"ok": True, "uptime_s": 1.5}))
    assert client.recv().value == {"ok": True, "uptime_s": 1.5}


def test_a_server_error_carries_its_exception_type(connection_pair):
    """
    The class name crosses the boundary, so callers can branch on it.

    Everything used to arrive as one indistinguishable RemoteCallError: a
    vendor timeout, a bad argument, and a full /dev/shm were separable
    only by matching on message text.
    """
    client, server = connection_pair
    thread = _serve_once(
        server,
        Result(
            error="TimeoutError: detector did not respond",
            error_type="TimeoutError",
        ),
    )
    with pytest.raises(RemoteCallError) as raised:
        send_call(client, threading.Lock(), Call("camera", "acquire_frame"))
    assert raised.value.error_type == "TimeoutError"
    thread.join(timeout=5)


def test_a_client_side_failure_has_no_server_exception_type(connection_pair):
    """A timeout never reached the server, so it names no server exception."""
    client, _server = connection_pair
    with pytest.raises(RemoteCallError) as raised:
        send_call(
            client,
            threading.Lock(),
            Call("instrument", "health"),
            timeout_s=0.05,
        )
    assert raised.value.error_type is None


def test_a_result_without_an_error_type_still_works(connection_pair):
    """
    Wire compatibility in both directions.

    error_type has a default, so a Result pickled by a peer that predates
    it unpickles here with None rather than failing to construct.
    """
    client, server = connection_pair
    thread = _serve_once(server, Result(error="ValueError: nope"))
    with pytest.raises(RemoteCallError) as raised:
        send_call(client, threading.Lock(), Call("scanner", "scan_frame"))
    assert raised.value.error_type is None
    thread.join(timeout=5)


def test_the_protocol_vocabulary_lives_on_the_mit_side():
    """
    Target names, backends, and the threshold are defined here, not in the server.

    The threshold decides whether a client receives a Frame or a
    SharedFrameRef, which makes it a protocol decision; it lived in the
    GPL server module, and the MIT test suite had to import that module
    to learn a number. The argv ordering of TARGET_NAMES is likewise part
    of the protocol - the client passes one port per target positionally
    and the server zips them back against the same tuple, so two copies
    could disagree in a way strict=True cannot catch.
    """
    assert TARGET_NAMES[-1] == "instrument"
    assert set(DEVICE_TARGET_NAMES) == set(TARGET_NAMES) - {"instrument"}
    assert BACKENDS == (SIMULATED_BACKEND, HARDWARE_BACKEND)
    assert SHARED_MEMORY_THRESHOLD_BYTES == 64 * 1024
