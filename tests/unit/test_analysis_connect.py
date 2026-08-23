"""
Unit tests for the analysis worker's bounded connection attempt.

The device client was hung twice by the same thing and fixed twice —
``multiprocessing.connection.Client`` blocks with no timeout through both
the TCP connect and the authentication handshake, so an attempt that
blocks outlives every deadline the loop around it thinks it has, and the
child is never polled again while it does. The analysis client is the
other copy of that loop, and it had the hole in the more basic form: no
bound on the attempt at all.

It matters more here than the arithmetic suggests. A device server is
launched once at session start; an analysis worker is launched from a
button, on whichever thread the analysis was asked for, so the hang lands
where the operator is.

Nothing here spawns a worker: a bare socket that listens and never
accepts is what a worker stopped short of its handshake looks like from
the client's side, and a stub process is a more exact instrument for the
racy half than a real one could be.
"""

from __future__ import annotations

import multiprocessing.connection
import socket
import threading
import time

import pytest

from miainwoodpecker.analysis import remote as analysis_remote
from miainwoodpecker.analysis.remote import AnalysisWorkerError, _connect_with_retry

_AUTHKEY = b"y" * 32
# The whole connect budget for these tests, in place of the module's 30 s.
# Long enough not to be tripped by a loaded machine, short enough that a
# test which does wait it out is still a test rather than a hang.
_A_SHORT_BUDGET_S = 0.5
# Generous ceiling on a bounded failure: anything near it means the bound
# is not working. Well above _A_SHORT_BUDGET_S, so the two never overlap.
_BOUNDED_FAILURE_CEILING_S = 5.0
# Any status will do; this one is not 0 and not 1, so it cannot be
# confused with a clean exit or with a plain interpreter error.
_AN_EXIT_STATUS = 7
# One poll finds the worker alive, the rest find it gone. The first is
# the retry loop's own, made before it tries to connect at all, so the
# exit lands squarely underneath a blocked attempt.
_POLLS_BEFORE_THE_EXIT = 1
# Never dialled, because the ``Client`` that would dial it is replaced,
# so any number does. It appears only in messages nothing asserts on.
_A_PORT_NOTHING_DIALS = 51235


class _NeverExits:
    """A worker that stays up, so only the deadline can end the wait."""

    returncode: int | None = None

    def poll(self) -> int | None:
        """
        Report the worker as running.

        Returns
        -------
        int | None
            Always ``None``.
        """
        return None


class _DiesUnderTheAttempt:
    """
    A worker that exits the moment the client is blocked on connecting.

    Stands in for the ordering rather than for a process: the first poll
    — the one the retry loop makes before it tries anything — finds it
    alive, and every poll after that finds it gone. A real ``Popen``
    would have to be raced into that state; this cannot be anything else.
    """

    def __init__(self, status: int) -> None:
        self._status = status
        self._polls = 0
        self.returncode: int | None = None

    def poll(self) -> int | None:
        """
        Report the worker as alive once, then as exited.

        Returns
        -------
        int | None
            ``None`` on the first call, the exit status afterwards.
        """
        self._polls += 1
        if self._polls > _POLLS_BEFORE_THE_EXIT:
            self.returncode = self._status
        return self.returncode


@pytest.fixture
def short_budget(monkeypatch: pytest.MonkeyPatch) -> float:
    """
    Shrink the connect budget, since it is a module constant and 30 s.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Used to rebind the constant for the duration of one test.

    Returns
    -------
    float
        The budget now in force, which is what both tests measure
        against: one failure has to arrive when it expires, the other
        well before it.
    """
    monkeypatch.setattr(analysis_remote, "_CONNECT_TIMEOUT_S", _A_SHORT_BUDGET_S)
    return _A_SHORT_BUDGET_S


def test_a_worker_that_never_handshakes_fails_by_the_deadline(short_budget):
    """
    A worker that accepts TCP and says nothing must not hang the caller.

    The socket below listens and is never accepted from, so the client's
    connect succeeds against the backlog and its handshake then waits for
    a challenge that will never come. Before the attempt was bounded this
    was unbounded on the calling thread: the loop's ``while
    time.monotonic() < deadline`` could not end an attempt it had already
    started, so the 30 s budget bounded how often it tried and not how
    long it waited, and a viewer that asked for an analysis stopped
    there for good.
    """
    quiet = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    quiet.bind(("localhost", 0))
    quiet.listen(1)
    port = quiet.getsockname()[1]
    try:
        started = time.monotonic()
        with pytest.raises(AnalysisWorkerError, match="not answering"):
            _connect_with_retry(port, _AUTHKEY, _NeverExits())
        # At the deadline, and not before it or long after: the bound
        # is the whole claim.
        assert short_budget <= time.monotonic() - started < _BOUNDED_FAILURE_CEILING_S
    finally:
        quiet.close()


def test_a_worker_that_dies_under_a_blocked_attempt_is_named_by_its_status(
    monkeypatch, short_budget,
):
    """
    An attempt that blocks must not cost the caller the worker's exit status.

    The second failure, and the one a bound alone does not fix: the bound
    is the caller's *whole* budget, so a single blocked attempt spends
    all of it and the worker is never polled again. What comes back is
    then a timeout, when what happened was an exit with a status — and
    the status is the half an operator can act on, since it is what the
    worker's own log line is attached to.

    A ``Client`` that never answers is the platform-independent way to
    provoke that. The ways a real connect can block — a half-open peer,
    a firewalled port, or a bound-but-not-listening socket, which
    swallows the connection on macOS instead of refusing it — differ by
    platform; what the client does about them must not.
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

    monkeypatch.setattr(
        multiprocessing.connection, "Client", never_answers,
    )
    worker = _DiesUnderTheAttempt(_AN_EXIT_STATUS)

    started = time.monotonic()
    try:
        with pytest.raises(
            AnalysisWorkerError, match=f"exited with status {_AN_EXIT_STATUS}",
        ):
            _connect_with_retry(_A_PORT_NOTHING_DIALS, _AUTHKEY, worker)
        # Noticed because the worker was polled under the attempt, not
        # because the budget ran out: waiting it out is the bug, and it
        # is what produces the timeout message instead of this one.
        assert time.monotonic() - started < short_budget
    finally:
        still_blocked.set()
