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

Bounding that attempt fixed the hang and left a second failure behind
it, which the last test here is about: the bound is the caller's whole
deadline, so one attempt that blocks spends all of it and the child is
never polled again. A server that has already exited with a curable
status is then reported as one that is running and wedged.

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

from miainwoodpecker.devices.remote import (
    PORT_UNAVAILABLE_EXIT_STATUS,
    DeviceServerStartupError,
    _PortsLostError,
    _connect_once,
    _connect_with_retry,
)

_AUTHKEY = b"x" * 32
# Generous ceiling for the bounded-failure assertion: the deadline is
# 0.5s, so anything near this means the bound is not working.
_BOUNDED_FAILURE_CEILING_S = 5.0
# Never dialled, because the ``Client`` that would dial it is replaced,
# so any number does. It appears only in messages nothing asserts on.
_A_PORT_NOTHING_DIALS = 51234
# One poll finds the child alive, the rest find it gone. The first is the
# retry loop's own, made before it tries to connect at all, so the exit
# lands squarely underneath a blocked attempt.
_POLLS_BEFORE_THE_EXIT = 1
# Long enough that a client which waits its deadline out fails the class
# assertion rather than the clock one - the class is what the respawn
# dispatches on - and short enough that the wrong answer is cheap.
_A_DEADLINE_A_WEDGE_WOULD_BURN_S = 3.0
# The exit is noticed a poll interval after it happens; the slack is for
# a loaded CI machine, not for a second deadline.
_EXIT_NOTICED_CEILING_S = 1.5


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


class _DiesUnderTheAttempt:
    """
    A child that exits the moment the client is blocked on connecting to it.

    Stands in for the real ordering rather than for a process: the first
    poll — the one the retry loop makes before it tries anything — finds
    it alive, and every poll after that finds it gone with the status
    that means "your port was taken". A ``Popen`` would have to be raced
    into that state; this cannot be anything else.
    """

    def __init__(self, status: int) -> None:
        self._status = status
        self._polls = 0
        self.returncode: int | None = None

    def poll(self) -> int | None:
        """
        Report the child as alive once, then as exited.

        Returns
        -------
        int | None
            ``None`` on the first call, the exit status afterwards.
        """
        self._polls += 1
        if self._polls > _POLLS_BEFORE_THE_EXIT:
            self.returncode = self._status
        return self.returncode


def test_a_server_that_dies_under_a_blocked_attempt_is_read_as_a_lost_port(
    monkeypatch,
):
    """
    An attempt that blocks must not cost the client the child's exit status.

    The second way a connect attempt hangs, and the one that made the
    bound worthless: it is bounded by the caller's *whole* deadline, so a
    single blocked attempt spends all of it and the retry loop never
    polls the child again. A port bound by something that is not
    listening does exactly that on macOS, where such a connect is neither
    refused nor completed but left in flight, while Linux and Windows
    refuse it at once — and it is the port a server reports as
    ``PORT_UNAVAILABLE_EXIT_STATUS`` a moment later. The client then
    spent fifteen seconds on a dead process and announced it was
    "alive but not completing connections": the one startup failure a
    respawn cures, reported as the one it cannot.

    The socket semantics are macOS's, so they are not what is provoked
    here — the ``Client`` that never answers is, which is the same thing
    from the client's side and is the same on every platform. What must
    come back is ``_PortsLostError``, the class the respawn dispatches
    on, and it must come back at the speed of the exit rather than of the
    deadline.
    """
    still_blocked = threading.Event()

    def never_answers(*_args: object, **_kwargs: object) -> None:
        """
        Block like a connect nothing ever answers.

        Parameters
        ----------
        *_args : object
            The address, ignored.
        **_kwargs : object
            The authkey, ignored.

        Raises
        ------
        ConnectionRefusedError
            Once the test releases it, so the scrap thread ends rather
            than outliving the test wedged.
        """
        still_blocked.wait(timeout=_BOUNDED_FAILURE_CEILING_S)
        raise ConnectionRefusedError

    monkeypatch.setattr(multiprocessing.connection, "Client", never_answers)
    process = _DiesUnderTheAttempt(PORT_UNAVAILABLE_EXIT_STATUS)

    started = time.monotonic()
    try:
        with pytest.raises(_PortsLostError, match="probed as free"):
            _connect_with_retry(
                _A_PORT_NOTHING_DIALS,
                _AUTHKEY,
                time.monotonic() + _A_DEADLINE_A_WEDGE_WOULD_BURN_S,
                process,
                "acme_miainwoodpecker_adapter.server",
            )
        # Noticed because the child was polled under the attempt, not
        # because the deadline eventually fired: burning the deadline is
        # the bug, and it is what raises the wrong class above.
        assert time.monotonic() - started < _EXIT_NOTICED_CEILING_S
    finally:
        still_blocked.set()
