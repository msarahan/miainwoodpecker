"""
Unit tests: the tray's supervisor, with stand-ins for both children.

No Qt and no devices. What is being tested is the sequence
:class:`~miainwoodpecker.tray.session.InstrumentSession` turned inside
out from the launcher's blocking one - a broker that is started and then
*polled*, a window that cannot be opened before there is something to
open it on, and a shutdown that goes in the order that parks an
instrument rather than the order that is convenient.

The children are real processes, because everything interesting here is
about process lifetime: a fake that "exits" by setting a flag would pass
tests that a Ctrl-Break on Windows would fail. What is faked instead is
the *broker's* behaviour - it publishes because the test writes the
invitation, not because it opened a microscope.
"""

from __future__ import annotations

import json
import sys
import time
import typing

import pytest

from miainwoodpecker.broker.invitation import DEFAULT_FILENAME, BrokerInvitation
from miainwoodpecker.tray.session import FrontEnd, InstrumentSession, SessionState

if typing.TYPE_CHECKING:
    from pathlib import Path

_DEADLINE_S = 30.0
_SLEEPER = "import time; time.sleep(60)"
_PUBLISHED_PORT = 65000
_WINDOWS = 2


def _sleeping() -> list[str]:
    """
    Return a command for a child that stays up until it is stopped.

    Returns
    -------
    list[str]
        The argv.
    """
    return [sys.executable, "-c", _SLEEPER]


def _viewer(command: list[str] | None = None) -> list[FrontEnd]:
    """
    Return the one front end most of these tests need.

    Parameters
    ----------
    command : list[str] | None
        What opening it runs, or None for a child that stays up.

    Returns
    -------
    list[FrontEnd]
        A single "viewer".
    """
    return [FrontEnd(label="viewer", command=tuple(command or _sleeping()))]


def _session(tmp_path: Path, **kwargs: object) -> InstrumentSession:
    """
    Build a session whose broker and window both merely stay alive.

    Parameters
    ----------
    tmp_path : Path
        Where the invitation is published.
    **kwargs : object
        Passed through to
        :class:`~miainwoodpecker.tray.session.InstrumentSession`.

    Returns
    -------
    InstrumentSession
        Built, not started.
    """
    return InstrumentSession(_sleeping(), _viewer(), tmp_path, **kwargs)


def _publish(directory: Path, port: int = _PUBLISHED_PORT) -> BrokerInvitation:
    """
    Write an invitation, as a broker that had finished starting would.

    Parameters
    ----------
    directory : Path
        Where to publish.
    port : int
        The port to claim. Nothing connects to it in these tests.

    Returns
    -------
    BrokerInvitation
        What was written.
    """
    invitation = BrokerInvitation(host="localhost", port=port, authkey=b"secret")
    invitation.write_to(directory)
    return invitation


def _poll_until(session: InstrumentSession, state: SessionState) -> bool:
    """
    Poll a session until it reaches a state, or the deadline passes.

    Parameters
    ----------
    session : InstrumentSession
        The session to poll.
    state : SessionState
        What is being waited for.

    Returns
    -------
    bool
        Whether it got there.
    """
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        if session.poll().state is state:
            return True
        time.sleep(0.05)
    return False


def test_a_session_is_serving_once_the_broker_says_where_it_is(tmp_path):
    """
    Starting does not block, and the invitation is what ends the wait.

    The whole reason this exists beside the launcher: the port is chosen
    by the OS and the authkey generated at startup, so the handshake is
    the published file either way - but here it has to be *noticed* on a
    tick rather than waited for, because the caller is an event loop
    that must keep drawing a menu while a device server starts.
    """
    session = _session(tmp_path)
    session.start()
    try:
        starting = session.poll()
        assert starting.state is SessionState.STARTING
        assert starting.invitation is None

        _publish(tmp_path)
        serving = session.poll()

        assert serving.state is SessionState.SERVING
        assert serving.invitation is not None
        assert serving.invitation.port == _PUBLISHED_PORT
        assert "localhost:65000" in serving.message
        # Reported once. A menu that showed "the instrument is served"
        # four times a second would be a menu nobody reads.
        assert serving.changed
        assert not session.poll().changed
    finally:
        session.shutdown()


def test_a_broker_that_exits_before_publishing_is_reported_rather_than_waited_out(
    tmp_path,
):
    """
    A device server that will not start ends the wait, and says why.

    The alternative is two minutes of a tray icon saying "starting"
    about a process that has already gone, followed by a timeout that
    names the wait rather than the instrument.
    """
    session = InstrumentSession(
        [sys.executable, "-c", "raise SystemExit(3)"],
        _viewer(),
        tmp_path,
    )
    session.start()
    try:
        assert _poll_until(session, SessionState.FAILED)
        assert "status 3" in session.message
        assert "before it said where it was listening" in session.message
    finally:
        session.shutdown()


def test_a_broker_that_never_publishes_is_given_up_on_and_stopped(tmp_path):
    """
    The wait is bounded, and what it was waiting for is asked to stop.

    Left running, that broker holds a device server - and possibly a
    microscope - with nothing supervising it and nothing able to reach
    it, which is the one outcome worse than a failure.
    """
    session = _session(tmp_path, timeout_s=0.05)
    session.start()
    try:
        time.sleep(0.2)
        status = session.poll()

        assert status.state is SessionState.FAILED
        assert "did not publish" in status.message
    finally:
        session.shutdown()


def test_a_broker_that_dies_while_serving_is_a_fault_rather_than_an_ending(tmp_path):
    """
    Nothing asked it to stop, so this is news rather than a shutdown.

    The distinction is the operator's next question: an instrument put
    down deliberately needs starting again, and one that fell over needs
    looking at first. It is also why the two states are separate at all.
    """
    session = InstrumentSession(
        [sys.executable, "-c", "import time; time.sleep(0.2)"],
        _viewer(),
        tmp_path,
    )
    session.start()
    try:
        _publish(tmp_path)
        assert session.poll().state is SessionState.SERVING
        assert _poll_until(session, SessionState.FAILED)
        assert "no longer served" in session.message
    finally:
        session.shutdown()


def test_a_window_cannot_be_opened_before_there_is_one_to_open(tmp_path):
    """
    And once there is, more than one may be: the broker arbitrates.

    Two windows on one column - one per detector, or one per person - is
    a thing people do, and refusing the second would be this process
    inventing a restriction the broker does not have.
    """
    session = _session(tmp_path)
    session.start()
    try:
        assert session.open_front_end() is None
        assert session.front_ends == 0

        _publish(tmp_path)
        session.poll()

        assert session.open_front_end() is not None
        assert session.open_front_end() is not None
        assert session.poll().front_ends == _WINDOWS
    finally:
        session.shutdown()


def test_each_kind_of_client_is_opened_and_counted_on_its_own(tmp_path):
    """
    A window and a dashboard at once, and neither counted as the other.

    They are separate programs wanting separate environments — Qt in
    one, marimo and no Qt in the other — so the menu offers them
    separately, and an entry that said "Open another viewer (2 open)"
    about one viewer and one dashboard would be counting the wrong
    thing.
    """
    session = InstrumentSession(
        _sleeping(),
        [
            FrontEnd(label="viewer", command=tuple(_sleeping())),
            FrontEnd(label="dashboard", command=tuple(_sleeping())),
        ],
        tmp_path,
    )
    session.start()
    try:
        _publish(tmp_path)
        session.poll()

        assert session.open_front_end("viewer") is not None
        assert session.open_front_end("viewer") is not None
        assert session.open_front_end("dashboard") is not None
        session.poll()

        assert session.open_count("viewer") == _WINDOWS
        assert session.open_count("dashboard") == 1
        assert session.front_ends == _WINDOWS + 1
        # And a kind nobody offered opens nothing rather than the first
        # one that happened to be there.
        assert session.open_front_end("notebook") is None
        # No label means the first offered, which is what a double-click
        # on the icon gets.
        assert session.open_front_end() is not None
        assert session.open_count("viewer") == _WINDOWS + 1
    finally:
        session.shutdown()


def test_a_window_that_closes_on_its_own_stops_being_counted(tmp_path):
    """
    The count is of windows that are still there, not of ones ever opened.

    It is what the menu offers ("Open another viewer (2 open)") and what
    the confirmation before quitting counts, and both would drift upward
    for a session in which people open and close windows all day.
    """
    session = InstrumentSession(
        _sleeping(),
        _viewer([sys.executable, "-c", "pass"]),
        tmp_path,
    )
    session.start()
    try:
        _publish(tmp_path)
        session.poll()
        opened = session.open_front_end()
        assert opened is not None
        opened.wait(timeout=_DEADLINE_S)

        deadline = time.monotonic() + _DEADLINE_S
        while session.poll().front_ends and time.monotonic() < deadline:
            time.sleep(0.05)

        assert session.front_ends == 0
    finally:
        session.shutdown()


def test_a_window_is_told_where_the_broker_is(tmp_path):
    """
    Through the environment, so any front end can be one.

    The same variable the launcher sets and the dashboard reads: a
    command started from this menu needs to know nothing about the tray
    that started it.
    """
    report = tmp_path / "seen.txt"
    session = InstrumentSession(
        _sleeping(),
        _viewer(
            [
                sys.executable,
                "-c",
                (
                    "import os, pathlib; pathlib.Path("
                    f"{str(report)!r}"
                    ").write_text(os.environ['MIAINWOODPECKER_BROKER'], "
                    "encoding='utf-8')"
                ),
            ],
        ),
        tmp_path,
    )
    session.start()
    try:
        _publish(tmp_path)
        session.poll()
        window = session.open_front_end()
        assert window is not None
        window.wait(timeout=_DEADLINE_S)

        assert report.read_text(encoding="utf-8") == str(tmp_path)
    finally:
        session.shutdown()


def test_a_leftover_invitation_is_not_read_as_this_session(tmp_path):
    """
    Yesterday's port and authkey would fail as an authentication error.

    Which is the least diagnosable failure available: a window that
    cannot authenticate against a broker that is not there reads as a
    permissions problem rather than as a stale file.
    """
    stale = _publish(tmp_path, port=1)
    session = _session(tmp_path)
    session.start()
    try:
        status = session.poll()

        assert status.state is SessionState.STARTING
        assert not (tmp_path / DEFAULT_FILENAME).exists()
        assert stale.port == 1
    finally:
        session.shutdown()


def test_shutting_down_stops_the_windows_and_then_the_broker(tmp_path):
    """
    In that order, because the broker's exit is what parks the column.

    A probe parked out from under a window that is still driving it is
    the failure this ordering exists to prevent - and it is the same
    ordering the launcher's supervisor uses, for the same reason.
    """
    session = _session(tmp_path)
    session.start()
    try:
        _publish(tmp_path)
        session.poll()
        window = session.open_front_end()
        assert window is not None

        session.shutdown()

        assert window.poll() is not None
        assert session.state is SessionState.STOPPED
        # And the invitation goes with it: a client starting tomorrow
        # must not find today's port waiting for it.
        assert not (tmp_path / DEFAULT_FILENAME).exists()
    finally:
        session.shutdown()


def test_shutting_down_twice_is_not_an_error(tmp_path):
    """
    Three paths can each be the second to arrive: menu, signal, event loop.

    A second attempt that raised would replace an orderly shutdown with
    a traceback, on the way out, where nobody is looking.
    """
    session = _session(tmp_path)
    session.start()
    session.shutdown()
    session.shutdown()

    assert session.state is SessionState.STOPPED


def test_a_session_holds_one_instrument(tmp_path):
    """
    Starting twice would be a second broker over the same hardware.

    Which the device layer would refuse in its own way, later, in a
    message about a port or a device that is already open. Refusing it
    here says what actually happened.
    """
    session = _session(tmp_path)
    session.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            session.start()
    finally:
        session.shutdown()


def test_health_is_unreachable_until_something_is_being_served(tmp_path):
    """
    "No devices" and "no answer" must not look the same.

    An empty report from an instrument that has not started yet would
    render as a health panel saying everything is fine about nothing.
    """
    session = _session(tmp_path)
    session.start()
    try:
        report = session.health()

        assert not report.servers
        assert "starting" in report.summary
        assert session.busy() == ()
    finally:
        session.shutdown()


def test_health_survives_a_broker_that_will_not_answer(tmp_path):
    """
    Asking is a courtesy; a shutdown that depended on it would be a trap.

    The invitation here points at a port nothing is listening on, which
    is exactly the state a broker that has just died leaves behind - and
    the confirmation dialog before quitting is one of this method's two
    callers.
    """
    session = _session(tmp_path)
    session.start()
    try:
        # Port 0 cannot be connected to, so this is a published
        # invitation that no broker answers.
        _publish(tmp_path, port=0)
        assert session.poll().state is SessionState.SERVING

        report = session.health()

        assert not report.servers
        assert session.busy() == ()
    finally:
        session.shutdown()


def test_the_published_invitation_is_where_clients_are_told_to_look(tmp_path):
    """
    The directory, not the file: it is what a client's --broker takes.

    Both are accepted by every reader in the project, but the directory
    is what the launcher passes, what the dashboard's environment
    variable holds, and what the confirmation dialog prints.
    """
    published = tmp_path / "instrument"
    session = InstrumentSession(_sleeping(), _viewer(), published)
    session.start()
    try:
        assert session.publish == published
        assert published.is_dir()
        _publish(published)
        assert (
            json.loads(
                (published / DEFAULT_FILENAME).read_text(encoding="utf-8"),
            )["port"]
            == _PUBLISHED_PORT
        )
    finally:
        session.shutdown()
