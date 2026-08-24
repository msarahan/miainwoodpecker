"""
Integration tests: the tray icon, over a real broker, over a real instrument.

Three processes again - this one, a broker, and whatever the menu opens
- and what is added here over ``test_launcher`` is the part an operator
touches: a menu whose entries are disabled until there is an instrument
to use them on, a health report read from the broker rather than
composed here, and a Quit that asks first and means it either way.

The instrument is the camera server's synthetic one (``--backend
simulated --server-module miainwoodpecker.devices.camera_server``), for
the same reason the launcher's tests use it: no vendor SDK, no hardware,
and it starts fast enough to be worth waiting for in a test.

Qt is needed, but napari is not - this is a tray icon and two plain
widgets - so what is guarded on is a *notification area*, which some
Linux desktops genuinely do not have.
"""

from __future__ import annotations

import socket
import sys
import time
import typing

import pytest

pytest.importorskip("qtpy")

from qtpy import QtWidgets

from miainwoodpecker.broker.invitation import DEFAULT_FILENAME
from miainwoodpecker.tray import app as tray_app
from miainwoodpecker.tray.health import Condition
from miainwoodpecker.tray.session import FrontEnd, InstrumentSession, SessionState

if typing.TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_CAMERA_SERVER = "miainwoodpecker.devices.camera_server"
_DEADLINE_S = 120.0
_SLEEPER = "import time; time.sleep(60)"
_WINDOWS = 2


def _front_ends() -> list[FrontEnd]:
    """
    Return a viewer and a dashboard, both stood in for by a sleeper.

    Both, because the menu has an entry per front end and the point of
    several tests below is that the two are offered and counted apart.

    Returns
    -------
    list[FrontEnd]
        In menu order.
    """
    return [
        FrontEnd(label="viewer", command=(sys.executable, "-c", _SLEEPER)),
        FrontEnd(label="dashboard", command=(sys.executable, "-c", _SLEEPER)),
    ]


@pytest.fixture(name="application")
def _application() -> Iterator[QtWidgets.QApplication]:
    """
    Provide the one QApplication these tests share.

    Yields
    ------
    QtWidgets.QApplication
        The application, not exec'd: the tests drive the tray by
        calling its tick directly, so nothing here needs an event loop
        and nothing here can hang in one.
    """
    existing = QtWidgets.QApplication.instance()
    application = existing or QtWidgets.QApplication(sys.argv[:1])
    application.setQuitOnLastWindowClosed(False)
    yield application
    application.processEvents()


@pytest.fixture(name="tray")
def _tray(
    tmp_path: Path,
    application: QtWidgets.QApplication,
) -> Iterator[tuple[tray_app.TrayInstrument, InstrumentSession]]:
    """
    Build a tray over the synthetic instrument, started and serving.

    Parameters
    ----------
    tmp_path : Path
        Where the broker publishes.
    application : QtWidgets.QApplication
        The Qt application, so that widgets can be built.

    Yields
    ------
    tuple[tray_app.TrayInstrument, InstrumentSession]
        The tray and the session under it, with the instrument served.
    """
    del application
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        pytest.skip("this desktop has no notification area to put an icon in")
    published = tmp_path / "published"
    session = InstrumentSession(
        [
            sys.executable,
            "-m",
            "miainwoodpecker.broker.app",
            "--backend",
            "simulated",
            "--server-module",
            _CAMERA_SERVER,
            "--publish",
            str(published),
        ],
        _front_ends(),
        published,
    )
    instrument = tray_app.TrayInstrument(session)
    instrument.start()
    try:
        assert _serving(instrument, session)
        yield instrument, session
    finally:
        instrument.shut_down()


def _serving(tray: tray_app.TrayInstrument, session: InstrumentSession) -> bool:
    """
    Drive the tray's tick until the instrument is served.

    Parameters
    ----------
    tray : tray_app.TrayInstrument
        The tray to tick.
    session : InstrumentSession
        The session to watch.

    Returns
    -------
    bool
        Whether it got there before the deadline.
    """
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        tray.tick()
        if session.state is SessionState.SERVING:
            return True
        if session.state is SessionState.FAILED:
            pytest.fail(session.message)
        time.sleep(0.05)
    return False


def _listening(port: int) -> bool:
    """
    Report whether anything is accepting connections on a port.

    Parameters
    ----------
    port : int
        The port the broker published.

    Returns
    -------
    bool
        True while something answers there.
    """
    with socket.socket() as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("localhost", port)) == 0


def _entry(tray: tray_app.TrayInstrument, wording: str) -> QtWidgets.QAction:
    """
    Find one menu entry by what it says.

    Looked up by its text rather than held as an index, so that adding
    an entry does not silently repoint a test at its neighbour.

    Parameters
    ----------
    tray : tray_app.TrayInstrument
        The tray whose menu to search.
    wording : str
        A substring of the entry's text.

    Returns
    -------
    QtWidgets.QAction
        The action.

    Raises
    ------
    AssertionError
        If the menu has no such entry - a change in the menu rather
        than a failure of whatever the caller was checking.
    """
    for action in tray.menu.actions():
        if wording.lower() in action.text().lower():
            return action
    message = f"no menu entry mentioning {wording!r}"
    raise AssertionError(message)


def test_the_menu_offers_an_instrument_only_once_there_is_one(tmp_path, application):
    """
    Disabled until the broker publishes, and enabled the moment it does.

    A cold microscope PC spends tens of seconds importing a vendor stack
    and opening hardware, and during all of it the icon is there and the
    instrument is not. An entry that could be clicked then would open a
    window against a port nothing is listening on.
    """
    del application
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        pytest.skip("this desktop has no notification area to put an icon in")
    published = tmp_path / "published"
    session = InstrumentSession(
        [sys.executable, "-c", _SLEEPER],
        _front_ends(),
        published,
    )
    tray = tray_app.TrayInstrument(session)
    tray.start()
    try:
        tray.tick()

        assert not _entry(tray, "viewer").isEnabled()
        assert not _entry(tray, "health").isEnabled()
        # And Quit is never disabled: the one thing that must work while
        # an instrument is starting is stopping it.
        assert _entry(tray, "quit").isEnabled()
        # Clicking through the disabled entry anyway opens nothing.
        tray.open_viewer()
        assert session.front_ends == 0
    finally:
        tray.shut_down()


def test_the_tray_serves_an_instrument_and_opens_windows_on_it(tray):
    """
    The whole point, end to end, with nothing passed between by hand.

    The broker chooses its port and generates its authkey at startup, so
    the window learns both from the published invitation - and the tray
    is what noticed the file appear, enabled the entry, and set the
    variable the window reads.
    """
    instrument, session = tray

    assert _entry(instrument, "viewer").isEnabled()
    assert _entry(instrument, "dashboard").isEnabled()
    assert (session.publish / DEFAULT_FILENAME).exists()

    instrument.open_viewer()
    instrument.open_viewer()
    instrument.tick()

    assert session.front_ends == _WINDOWS
    # Two windows on one column is not an accident: arbitrating between
    # clients is what the broker underneath this is for.
    assert "2 open" in _entry(instrument, "viewer").text()


def test_the_window_and_the_dashboard_are_separate_entries(tray):
    """
    Two front ends, two entries, and neither counted as the other.

    They are separate programs wanting separate environments — Qt in
    one, marimo and no Qt in the other — so a single "open the front
    end" entry could only ever offer one of them.
    """
    instrument, session = tray

    instrument.open_viewer("dashboard")
    instrument.tick()

    assert session.open_count("dashboard") == 1
    assert session.open_count("viewer") == 0
    assert "1 open" in _entry(instrument, "dashboard").text()
    # The viewer's entry is untouched by a dashboard being open.
    assert _entry(instrument, "viewer").text() == "Open a viewer"


def test_the_health_report_names_what_the_broker_is_wrapping(tray):
    """
    Read from the broker over a socket, not composed from what was spawned.

    This process knows it started one broker; it does not know what came
    up underneath that broker, and the whole value of the panel is that
    the answer comes from the thing that does.
    """
    instrument, _ = tray

    instrument.refresh_health()
    report = instrument.health

    assert report.condition is Condition.HEALTHY
    served = {device.name for server in report.servers for device in server.devices}
    # The camera server serves an instrument and at least one detector.
    assert "instrument" in served
    assert len(served) > 1
    assert "all answering" in report.summary


def test_the_health_window_lists_every_device_under_its_server(tray):
    """
    One row per device, under the process that was supposed to bring it.

    The rows are the content: a panel that showed only the summary would
    say "something is wrong" without saying which detector, which is the
    one question worth opening a panel for.
    """
    instrument, _ = tray

    instrument.open_health_window()
    window = instrument.health_window
    assert window is not None
    rows = window.rows()

    assert rows
    assert all(condition is Condition.HEALTHY for _, condition in rows)
    assert any(name.startswith("instrument") for name, _ in rows)
    window.close()


def test_quitting_asks_first_and_a_refused_question_changes_nothing(
    tray,
    monkeypatch,
):
    """
    Ending everybody's session, not only the clicker's, is worth a question.

    A notebook halfway through a spectrum image is a client of this
    broker and gets no say, so the question is asked - and a Cancel that
    stopped the instrument anyway would be the worst bug in the
    application.
    """
    instrument, session = tray
    asked = []

    def refuse(question: str, detail: str) -> bool:
        """
        Stand in for the operator, and say no.

        Parameters
        ----------
        question : str
            What was about to happen.
        detail : str
            What it would have interrupted.

        Returns
        -------
        bool
            False, always.
        """
        asked.append((question, detail))
        return False

    monkeypatch.setattr(tray_app, "confirm", refuse)
    instrument.open_viewer()
    instrument.open_viewer("dashboard")
    instrument.tick()

    instrument.confirm_quit()

    assert session.state is SessionState.SERVING
    assert len(asked) == 1
    question, detail = asked[0]
    assert "everyone connected to it" in question
    # And it says what stopping would cost, in the terms an operator
    # can check: where the instrument is published, and what is open -
    # counted per kind, since "2 clients" would not say which two.
    assert str(session.publish) in detail
    assert "1 viewer and 1 dashboard" in detail


def test_confirming_stops_the_windows_the_broker_and_the_instrument(
    tray,
    monkeypatch,
):
    """
    And in that order, which is what lets the broker park the column.

    Asserted from outside: the port the broker published stops
    answering, which it only does once the broker has run its shutdown
    rather than been shot.
    """
    instrument, session = tray
    monkeypatch.setattr(tray_app, "confirm", lambda *_args: True)
    instrument.open_viewer()
    instrument.tick()
    assert session.invitation is not None
    port = session.invitation.port
    assert _listening(port)

    instrument.confirm_quit()

    assert session.state is SessionState.STOPPED
    assert session.front_ends == 0
    assert not _listening(port)
    # The invitation goes with it, so tomorrow's client cannot read
    # today's port and authkey.
    assert not (session.publish / DEFAULT_FILENAME).exists()


def test_a_broker_that_dies_is_reported_rather_than_left_looking_healthy(
    tmp_path,
    application,
    monkeypatch,
):
    """
    An icon that stays green over a dead instrument is worse than no icon.

    The window is still open, still shows its last frame, and is
    connected to nothing - so the tray says what happened and stops
    being a tray icon for an instrument that has gone.
    """
    del application
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        pytest.skip("this desktop has no notification area to put an icon in")
    published = tmp_path / "published"
    session = InstrumentSession(
        [sys.executable, "-c", "raise SystemExit(4)"],
        _front_ends(),
        published,
    )
    tray = tray_app.TrayInstrument(session)
    told = []
    monkeypatch.setattr(tray_app, "report_failure", told.append)
    tray.start()
    try:
        deadline = time.monotonic() + _DEADLINE_S
        while not told:
            if time.monotonic() > deadline:
                pytest.fail("the tray never noticed the broker had gone")
            tray.tick()
            time.sleep(0.05)

        assert "status 4" in told[0]
        # Noticing and telling are one tick: the failure is reported and
        # the session is put down in the same breath, so the state to
        # find afterwards is the one it ended in rather than the one it
        # passed through.
        assert session.state is SessionState.STOPPED
    finally:
        tray.shut_down()
