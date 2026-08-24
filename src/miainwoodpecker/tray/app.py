"""
The instrument, in the notification area: start it, open a window, stop it.

``pixi run serve`` already holds a microscope open for whoever connects.
What it also does is occupy a terminal, and end when that terminal is
closed - which is a poor fit for the machine this is meant to run on,
where the instrument is served all day, the person at the console is not
the person who started it, and "close the black window" is a thing that
happens. This is the same session with a tray icon instead of a console:
the broker outlives every window, and the only two things anybody needs
are on a right-click.

**What is on the menu**, and each is here because it is the thing that
cannot be done from anywhere else:

- *Open a viewer*, because the window is a client rather than the
  application - it joins the instrument the way a notebook does.
- *Open a dashboard*, when a command for one was given, because the
  browser dashboard is the other front end this project has and it is
  not a variant of the window: it wants marimo and no Qt, so it runs in
  its own environment and gets its own entry.
- *Instrument health*, because a broker over a configured microscope is
  a broker over several device servers, and from outside them the only
  evidence of a spectrometer that did not come up is a menu that is one
  item short. See :mod:`miainwoodpecker.tray.health`.
- *Quit*, which stops the windows, stops the broker, and parks the
  instrument - in that order, and asking rather than killing.

**The first two become "Show the ..." once one is running**, and that is
a deliberate reading of what the second click means. An entry pressed
again because a window went behind a browser is not a request for a
second window, and answering it with one is how a column ends up with
four viewers on it by four o'clock. So the tray keeps one of each kind
and goes and finds it instead - see :mod:`miainwoodpecker.tray.raising`
for how, and for the two ways that can honestly fail. Anyone who
genuinely wants a second window still starts one by hand, against the
same published invitation; what this declines to do is start one by
accident.

**Quitting asks first, and the question is not a formality.** This one
menu item ends everybody's session, not just the session of whoever
clicked it: a notebook halfway through a spectrum image and a dashboard
on the wall are clients of this broker and neither gets a say. So the
confirmation names what is running at the moment it is asked - read from
the broker rather than from what this process happens to have spawned -
and defaults to "no".

**Why the broker is still a subprocess.** Everything
:mod:`miainwoodpecker.launcher` says about that applies unchanged: the
vendor device stack is GPL-3.0 and lives in its own environment, and
parking depends on the broker exiting cleanly under a signal it handles.
A tray icon that imported the device layer would give up both. So this
process spawns, watches and asks - it never opens a device - and
:class:`~miainwoodpecker.tray.session.InstrumentSession` is where that
happens without blocking the event loop.
"""

from __future__ import annotations

import argparse
import logging
import sys
import typing
from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets

from miainwoodpecker.devices.remote import (
    DEFAULT_SERVER_MODULE,
    HARDWARE_BACKEND,
    SIMULATED_BACKEND,
)
from miainwoodpecker.instrument_config import (
    InstrumentConfigError,
    load_instrument_config,
)
from miainwoodpecker.launcher import (
    BROKER_ENV_VAR,
    BROKER_MODULE,
    broker_arguments,
    child_command,
    resolve_environment,
    stop_requests,
    viewer_command,
)
from miainwoodpecker.tray import raising
from miainwoodpecker.tray.health import Condition, InstrumentHealth
from miainwoodpecker.tray.session import (
    FrontEnd,
    InstrumentSession,
    Opened,
    SessionState,
)

if typing.TYPE_CHECKING:
    from miainwoodpecker.instrument_config import InstrumentConfig
    from miainwoodpecker.tray.session import SessionStatus

_LOGGER = logging.getLogger("miainwoodpecker.tray.app")

APPLICATION_NAME = "miainwoodpecker"
"""What the tray icon calls itself, in tooltips and dialog titles."""

_TICK_MS = 250
"""
How often the children are polled and a pending signal gets a chance.

The same order as the launcher's own loop. It is a ``waitpid`` and a
file check, and it is also what lets ``SIGTERM`` be noticed: a Qt event
loop runs no Python between events, so a handler set by
:func:`~miainwoodpecker.launcher.stop_requests` only runs when the
interpreter is next given control - which this timer guarantees.
"""

_HEALTH_MS = 3000
"""
How often the servers underneath the broker are asked how they are.

Two watch-side calls over a held-open connection, so the cost is small,
but not free and not urgent: a device server that has fallen over is not
less fallen over three seconds later, and this runs for an entire
session.
"""

_ICON_EDGE = 64
_ICON_INSET = 8
_ICON_RING = 5

_COLOURS = {
    Condition.HEALTHY: "#2e9e4f",
    Condition.DEGRADED: "#d08b16",
    Condition.FAILED: "#c0392b",
    Condition.UNREACHABLE: "#8d8d8d",
}
"""
The dot's colour per condition: green, amber, red, grey.

Colour is never the only signal - the tooltip and the menu say the same
thing in words, and the health panel says it per device - because a
tray icon is 16 pixels of colour and some operators cannot tell two of
these apart.
"""


class HealthWindow(QtWidgets.QDialog):
    """
    What the broker is wrapping, one row per device under its server.

    A window rather than a submenu, because the interesting content is
    two levels deep and several words wide - "eels_camera: stopped:
    timed out waiting for a frame" is not a menu item - and because it
    is something an operator leaves open on a second screen while they
    chase a detector that keeps dropping out.

    Parameters
    ----------
    parent : QtWidgets.QWidget | None
        Qt parent, if any. A tray application usually has none.
    """

    _COLUMNS = ("Device", "State", "Detail")

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APPLICATION_NAME} - instrument health")
        self.resize(720, 360)
        layout = QtWidgets.QVBoxLayout(self)
        self._summary = QtWidgets.QLabel("asking the broker...")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)
        self._tree = QtWidgets.QTreeWidget()
        self._tree.setColumnCount(len(self._COLUMNS))
        self._tree.setHeaderLabels(list(self._COLUMNS))
        self._tree.setRootIsDecorated(True)
        layout.addWidget(self._tree)
        self._note = QtWidgets.QLabel(
            "Read from the broker's own record of what each device said. "
            "Nothing here touches the hardware, so a device can be listed as "
            "answering and still be unhappy in a way only an acquisition "
            "would show.",
        )
        self._note.setWordWrap(True)
        layout.addWidget(self._note)
        self._rows: list[tuple[str, Condition]] = []

    def rows(self) -> tuple[tuple[str, Condition], ...]:
        """
        Return what is currently on show, one entry per device.

        Returns
        -------
        tuple[tuple[str, Condition], ...]
            Each device's name and condition, in the order drawn.
        """
        return tuple(self._rows)

    def show_health(self, health: InstrumentHealth) -> None:
        """
        Redraw the tree from a fresh report.

        Rebuilt rather than patched in place: it is a handful of rows a
        few times a minute, and a tree that is diffed is a tree that can
        show a device which has gone.

        Parameters
        ----------
        health : InstrumentHealth
            What the broker last said.
        """
        expanded = {
            self._tree.topLevelItem(index).text(0)
            for index in range(self._tree.topLevelItemCount())
            if self._tree.topLevelItem(index).isExpanded()
        }
        self._summary.setText(health.summary)
        self._tree.clear()
        self._rows = []
        for server in health.servers:
            heading = QtWidgets.QTreeWidgetItem(
                [server.name, _wording(server.condition), server.description],
            )
            heading.setForeground(1, QtGui.QBrush(_colour(server.condition)))
            for device in server.devices:
                named = (
                    f"{device.name} ({device.label})"
                    if device.label and device.label != device.name
                    else device.name
                )
                row = QtWidgets.QTreeWidgetItem(
                    [named, _wording(device.condition), device.detail],
                )
                row.setForeground(1, QtGui.QBrush(_colour(device.condition)))
                heading.addChild(row)
                self._rows.append((named, device.condition))
            self._tree.addTopLevelItem(heading)
            # Expanded by default, and kept expanded across refreshes:
            # the devices are the content, and a panel that collapsed
            # itself every three seconds would be unusable.
            heading.setExpanded(not expanded or server.name in expanded)
        for column in range(len(self._COLUMNS)):
            self._tree.resizeColumnToContents(column)


class TrayInstrument(QtCore.QObject):
    """
    The tray icon, its menu, and the session the two of them drive.

    Parameters
    ----------
    session : InstrumentSession
        The broker to hold open, already built but not started.
    """

    def __init__(self, session: InstrumentSession) -> None:
        super().__init__()
        self._session = session
        self._health = InstrumentHealth(
            condition=Condition.UNREACHABLE,
            summary="the instrument is starting",
        )
        self._window: HealthWindow | None = None
        self._quitting = False
        self._stop_requested = stop_requests()
        self._icon = QtWidgets.QSystemTrayIcon()
        self._icon.setToolTip(f"{APPLICATION_NAME} - starting the instrument")
        self._icon.activated.connect(self._activated)
        self._build_menu()
        self._paint(Condition.UNREACHABLE)
        self._tick = QtCore.QTimer(self)
        self._tick.timeout.connect(self.tick)
        self._health_tick = QtCore.QTimer(self)
        self._health_tick.timeout.connect(self.refresh_health)

    def start(self) -> None:
        """Start the broker, show the icon, and begin polling."""
        self._session.start()
        self._icon.show()
        self._tick.start(_TICK_MS)

    @property
    def menu(self) -> QtWidgets.QMenu:
        """
        Return the right-click menu.

        The tray icon's entire interface, so it is the thing to look at
        to know what this application is currently offering.

        Returns
        -------
        QtWidgets.QMenu
            The menu.
        """
        return self._menu

    @property
    def health(self) -> InstrumentHealth:
        """
        Return the last health report read from the broker.

        Returns
        -------
        InstrumentHealth
            As of the last :meth:`refresh_health`.
        """
        return self._health

    @property
    def health_window(self) -> HealthWindow | None:
        """
        Return the health panel, if one has been opened.

        Returns
        -------
        HealthWindow | None
            The window, or None until the menu entry is used.
        """
        return self._window

    def _build_menu(self) -> None:
        """
        Build the right-click menu, with everything but Quit disabled.

        Disabled rather than absent, so that the menu has the same
        shape while the instrument is starting as it does once it has
        started - an operator wondering whether the viewer entry exists
        yet is an operator who cannot tell "starting" from "broken".

        One entry per front end the session was given, in its order, so
        that a session started without a dashboard command simply has no
        dashboard entry rather than one that fails when it is used.
        """
        self._menu = QtWidgets.QMenu()
        self._state_line = self._menu.addAction("starting the instrument...")
        self._state_line.setEnabled(False)
        self._health_line = self._menu.addAction("")
        self._health_line.setEnabled(False)
        self._health_line.setVisible(False)
        self._menu.addSeparator()
        self._open: dict[str, QtWidgets.QAction] = {}
        for front_end in self._session.openable:
            action = self._menu.addAction(_opens(front_end.label, running=False))
            # Bound rather than closed over the loop variable, which
            # would give every entry the last front end's label.
            action.triggered.connect(
                lambda _checked=False, label=front_end.label: self.open_viewer(label),
            )
            action.setEnabled(False)
            self._open[front_end.label] = action
        self._show_health = self._menu.addAction("Instrument health...")
        self._show_health.triggered.connect(self.open_health_window)
        self._show_health.setEnabled(False)
        self._menu.addSeparator()
        self._quit = self._menu.addAction("Quit and stop the instrument")
        self._quit.triggered.connect(self.confirm_quit)
        self._icon.setContextMenu(self._menu)

    def tick(self) -> None:
        """
        Advance the session, and answer anything that asked us to stop.

        The signal check is here rather than in a handler that acts
        directly, for the reason :func:`~miainwoodpecker.launcher.stop_requests`
        gives: a handler runs at an arbitrary point in the interpreter,
        and this one would be tearing down an instrument from inside
        whatever Qt happened to be doing.
        """
        if self._quitting:
            return
        if self._stop_requested.is_set():
            _LOGGER.info("asked to stop; stopping the instrument")
            self.shut_down()
            return
        status = self._session.poll()
        self._apply(status)

    def _apply(self, status: SessionStatus) -> None:
        """
        Put a poll's result on the icon and the menu.

        Parameters
        ----------
        status : SessionStatus
            What the session last reported.
        """
        self._state_line.setText(status.message)
        serving = status.state is SessionState.SERVING
        self._show_health.setEnabled(serving)
        for label, action in self._open.items():
            action.setEnabled(serving)
            action.setText(
                _opens(label, running=self._session.running(label) is not None),
            )
        if not status.changed:
            return
        if serving:
            self.refresh_health()
            self._health_tick.start(_HEALTH_MS)
            self._icon.showMessage(
                APPLICATION_NAME,
                f"The instrument is served: {status.message}. Right-click the "
                f"icon to open a window on it.",
                QtWidgets.QSystemTrayIcon.MessageIcon.Information,
            )
        elif status.state is SessionState.FAILED:
            self._health_tick.stop()
            self._paint(Condition.FAILED)
            self._icon.setToolTip(f"{APPLICATION_NAME} - {status.message}")
            self._broker_lost(status.message)

    def refresh_health(self) -> None:
        """Ask the broker how its device servers are, and show the answer."""
        if self._quitting:
            return
        self._health = self._session.health()
        self._health_line.setText(self._health.summary)
        self._health_line.setVisible(True)
        self._paint(self._health.condition)
        self._icon.setToolTip(
            f"{APPLICATION_NAME} - {self._session.message}\n{self._health.summary}",
        )
        if self._window is not None and self._window.isVisible():
            self._window.show_health(self._health)

    def _paint(self, condition: Condition) -> None:
        """
        Draw the tray dot in the colour of a condition.

        Drawn rather than shipped as a file: the icon has to carry a
        state, and one painted circle is less to maintain than four
        images and the packaging rules that would put them in the wheel.

        Parameters
        ----------
        condition : Condition
            What to say.
        """
        pixmap = QtGui.QPixmap(_ICON_EDGE, _ICON_EDGE)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setBrush(QtGui.QBrush(_colour(condition)))
            painter.setPen(QtGui.QPen(QtGui.QColor("#1c1c1c"), _ICON_RING))
            painter.drawEllipse(
                _ICON_INSET,
                _ICON_INSET,
                _ICON_EDGE - 2 * _ICON_INSET,
                _ICON_EDGE - 2 * _ICON_INSET,
            )
        finally:
            # Ended explicitly: a QPainter still active when its pixmap
            # is used prints a Qt warning and paints nothing.
            painter.end()
        self._icon.setIcon(QtGui.QIcon(pixmap))

    def _activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        """
        Open a window on a double-click, which is what a tray icon does.

        Parameters
        ----------
        reason : QtWidgets.QSystemTrayIcon.ActivationReason
            How the icon was activated. Only a double-click acts; a
            single click belongs to the platform, which uses it for the
            menu on some desktops.
        """
        if reason is QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_viewer()

    def open_viewer(self, label: str | None = None) -> None:
        """
        Open a client on the instrument, or say why there is not one.

        Parameters
        ----------
        label : str | None
            Which kind to open - a
            :attr:`~miainwoodpecker.tray.session.FrontEnd.label` - or
            None for the first one offered, which is what a double-click
            on the icon gets.
        """
        opened = self._session.open_front_end(label)
        if opened is Opened.UNAVAILABLE:
            self._icon.showMessage(
                APPLICATION_NAME,
                "The instrument is not being served yet, so there is nothing "
                "for a client to connect to.",
                QtWidgets.QSystemTrayIcon.MessageIcon.Warning,
            )
        elif opened is Opened.ALREADY_OPEN:
            self._show_running(label or self._session.openable[0].label)

    def _show_running(self, label: str) -> None:
        """
        Bring the client of one kind that is already up to the front.

        Parameters
        ----------
        label : str
            Which kind - a
            :attr:`~miainwoodpecker.tray.session.FrontEnd.label`.
        """
        process = self._session.running(label)
        if process is not None and raising.show(process, self._session.url(label)):
            return
        # Not an error, and said as a notification rather than a dialog:
        # what happened is that the operator's window is somewhere this
        # process cannot reach, and the useful part of the answer is
        # that they already have one.
        self._icon.showMessage(
            APPLICATION_NAME,
            f"A {label} is already open on this instrument. This desktop "
            f"would not bring it to the front - look for it among your "
            f"windows.",
            QtWidgets.QSystemTrayIcon.MessageIcon.Information,
        )

    def open_health_window(self) -> None:
        """Show the health panel, with a reading no older than this click."""
        if self._window is None:
            self._window = HealthWindow()
        self.refresh_health()
        self._window.show_health(self._health)
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def confirm_quit(self) -> None:
        """
        Ask before ending everybody's session, then end it.

        The list of what is running is read now rather than remembered,
        because the whole value of the question is that it describes the
        instrument at the moment somebody reached for the menu.
        """
        if self._quitting:
            return
        if not self._ask_to_quit():
            return
        self.shut_down()

    def _ask_to_quit(self) -> bool:
        """
        Put the question, defaulting to no.

        Returns
        -------
        bool
            Whether the operator confirmed.
        """
        if self._session.state is not SessionState.SERVING:
            # Nothing is served, so there is nothing to interrupt and
            # nothing worth asking about - the menu item is then just
            # "close this".
            return True
        return confirm(
            "Stopping the instrument closes it for everyone connected to it, "
            "not only this machine's windows, and parks the column on the way "
            "out.",
            _what_stops(self._session, self._session.busy()),
        )

    def _broker_lost(self, message: str) -> None:
        """
        Say that the instrument has gone, and stop being a tray icon for it.

        There is nothing left to serve and nothing left to open, so
        staying in the notification area would be an icon that lies.

        Parameters
        ----------
        message : str
            What the session said went wrong.
        """
        report_failure(message)
        self.shut_down()

    def shut_down(self) -> None:
        """
        Stop everything, in order, and leave the event loop.

        The stop blocks - parking is bounded by whatever the hardware
        takes to reach a safe state, and the launcher gives it thirty
        seconds - so the icon says what is happening first and the
        pending paint is flushed before the wait begins. Without that
        the tray simply freezes with a cheerful green dot on it.
        """
        if self._quitting:
            return
        self._quitting = True
        self._tick.stop()
        self._health_tick.stop()
        self._menu.setEnabled(False)
        self._paint(Condition.UNREACHABLE)
        self._icon.setToolTip(f"{APPLICATION_NAME} - stopping the instrument...")
        if self._window is not None:
            self._window.close()
        QtWidgets.QApplication.processEvents()
        self._session.shutdown()
        self._icon.hide()
        QtWidgets.QApplication.quit()


def report_failure(message: str) -> None:
    """
    Tell the operator that the instrument has gone, and why.

    Modal, and a function for the same reason :func:`confirm` is one: it
    is the one thing in this module that stops everything until somebody
    reads it, which makes it the one thing a test must be able to stand
    in for.

    Parameters
    ----------
    message : str
        What went wrong.
    """
    _LOGGER.error("%s", message)
    box = QtWidgets.QMessageBox()
    box.setWindowTitle(f"{APPLICATION_NAME} - the instrument has stopped")
    box.setIcon(QtWidgets.QMessageBox.Icon.Critical)
    box.setText(message)
    box.setInformativeText(
        "Any window still open is no longer connected to anything. Start the "
        "instrument again once the cause is dealt with; the broker's own "
        "output says more than this dialog can.",
    )
    box.exec()


def confirm(question: str, detail: str) -> bool:
    """
    Put an irreversible question to the operator, defaulting to no.

    A function rather than a method so that the *decision* to stop an
    instrument can be exercised without a modal dialog standing in a
    test's way, and so that there is exactly one place that decides
    which button is the safe one.

    Parameters
    ----------
    question : str
        What is about to happen.
    detail : str
        What it will interrupt.

    Returns
    -------
    bool
        Whether the operator said yes. Anything else - Cancel, Escape,
        the window closed - is no.
    """
    box = QtWidgets.QMessageBox()
    box.setWindowTitle(f"{APPLICATION_NAME} - stop the instrument?")
    box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
    box.setText(question)
    box.setInformativeText(detail)
    box.setStandardButtons(
        QtWidgets.QMessageBox.StandardButton.Cancel
        | QtWidgets.QMessageBox.StandardButton.Yes,
    )
    box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
    box.activateWindow()
    return box.exec() == QtWidgets.QMessageBox.StandardButton.Yes


def _opens(label: str, *, running: bool) -> str:
    """
    Write what a front end's menu entry says, given whether one is up.

    In the entry rather than beside it, because it is not a status: it
    is what the click will *do*. "Open a viewer" starts one and "Show
    the viewer" goes and finds the one that exists, and an operator who
    has lost a window behind a browser should be able to read which of
    those they are about to get.

    Parameters
    ----------
    label : str
        The front end's label - "viewer", "dashboard".
    running : bool
        Whether one of that kind is already up.

    Returns
    -------
    str
        The menu text.
    """
    return f"Show the {label}" if running else f"Open a {label}"


def _colour(condition: Condition) -> QtGui.QColor:
    """
    Return the colour that stands for a condition.

    Parameters
    ----------
    condition : Condition
        The condition.

    Returns
    -------
    QtGui.QColor
        Its colour.
    """
    return QtGui.QColor(_COLOURS[condition])


def _wording(condition: Condition) -> str:
    """
    Return the word that stands for a condition, for anyone not reading colour.

    Parameters
    ----------
    condition : Condition
        The condition.

    Returns
    -------
    str
        A word for a table cell.
    """
    return {
        Condition.HEALTHY: "answering",
        Condition.DEGRADED: "partly answering",
        Condition.FAILED: "stopped",
        Condition.UNREACHABLE: "no answer",
    }[condition]


def _what_stops(session: InstrumentSession, busy: tuple[str, ...]) -> str:
    """
    Describe what quitting is about to interrupt.

    Parameters
    ----------
    session : InstrumentSession
        The session being asked about.
    busy : tuple[str, ...]
        The devices that are acquiring or held, one line each.

    Returns
    -------
    str
        The dialog's informative text.
    """
    lines = [f"The instrument is published at {session.publish}."]
    opened = [
        f"{count} {label}{'s' if count != 1 else ''}"
        for label, count in (
            (front_end.label, session.open_count(front_end.label))
            for front_end in session.openable
        )
        if count
    ]
    if opened:
        lines.append(f"{' and '.join(opened)} opened from here will be stopped.")
    if busy:
        lines.append("Running right now:")
        lines += [f"  - {line}" for line in busy]
    else:
        lines.append("Nothing is acquiring and no device is held.")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse the tray application's command-line arguments.

    The same passthrough flags the launcher takes, because it is the
    same broker underneath and an operator who has learned one command
    has learned both.

    Parameters
    ----------
    argv : list[str] | None
        Argument list, or None to read ``sys.argv``.

    Returns
    -------
    argparse.Namespace
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            "instrument configuration enumerating this microscope's hardware "
            "and the servers that drive it; a directory gets instrument.toml "
            "inside it. It is also what lets the health panel group devices "
            "under the server that was supposed to bring them. Replaces "
            "--backend, --plugin and --server-module rather than combining "
            "with them"
        ),
    )
    parser.add_argument(
        "--backend",
        choices=(SIMULATED_BACKEND, HARDWARE_BACKEND),
        default=None,
        help=(
            f"device backend for the broker (default {SIMULATED_BACKEND} - "
            "never silently hardware)"
        ),
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=None,
        metavar="MODULE",
        help="nionswift_plugin module providing hardware devices; repeatable",
    )
    parser.add_argument(
        "--server-module",
        default=None,
        metavar="MODULE",
        help=(
            f"module the broker launches as the device server (default "
            f"{DEFAULT_SERVER_MODULE})"
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "interface for the broker to bind (default localhost). Binding "
            "anywhere else puts an instrument's controls on the network; do "
            "it knowingly"
        ),
    )
    parser.add_argument(
        "--publish",
        default=None,
        metavar="PATH",
        help=(
            "where to publish the broker's connection details, so a notebook, "
            "a dashboard or a second window can join this instrument. "
            "Defaults to ~/.miainwoodpecker, which is where a client looks "
            "when it is told nothing at all"
        ),
    )
    parser.add_argument("--session", default=None, help="session directory")
    parser.add_argument("--operator", default=None, help="who is on the instrument")
    parser.add_argument("--sample", default=None, help="sample identifier")
    parser.add_argument("--notes", default=None, help="free-text session notes")
    parser.add_argument(
        "--broker-env",
        default=None,
        metavar="NAME",
        help=(
            "pixi environment to run the broker in - the one with the vendor "
            "device stack. Requires this to be running under pixi"
        ),
    )
    parser.add_argument(
        "--ui-env",
        default=None,
        metavar="NAME",
        help="pixi environment to run each window in",
    )
    parser.add_argument(
        "--dashboard-env",
        default=None,
        metavar="NAME",
        help=(
            "pixi environment to run the browser dashboard in. Its own, and "
            "not --ui-env: the dashboard needs marimo and no Qt, which is the "
            "whole reason that environment exists"
        ),
    )
    parser.add_argument(
        "dashboard",
        nargs=argparse.REMAINDER,
        metavar="-- COMMAND",
        help=(
            "a command that opens the browser dashboard on this instrument - "
            "'-- marimo run notebooks/instrument_dashboard.py'. It gets an "
            "'Open a dashboard' entry on the menu, and $"
            f"{BROKER_ENV_VAR} is set for it. Without one there is no such "
            "entry, rather than one that fails when it is used"
        ),
    )
    return parser.parse_args(argv)


def default_publish() -> Path:
    """
    Return where a tray-held instrument publishes when nothing says.

    ``~/.miainwoodpecker``, which is the convention the instrument
    configuration already established: the file describing the
    microscope lives there, and a notebook that reads
    ``~/.miainwoodpecker/broker.json`` needs to be told nothing at all.
    A temporary directory - the launcher's default - would be wrong
    here for the reason ``--serve`` refuses one: an instrument held open
    for people to join has to be findable.

    Returns
    -------
    Path
        The directory.
    """
    return Path.home() / f".{APPLICATION_NAME}"


def _load_config(path: str | None) -> InstrumentConfig | None:
    """
    Read the instrument configuration, if one was named.

    Read here as well as in the broker, and only to group the health
    panel by server. A file the broker will refuse is refused here
    first, which is the better place for it: a dialog-less startup
    failure beats a tray icon that appears and then vanishes.

    Parameters
    ----------
    path : str | None
        The ``--config`` value, or None.

    Returns
    -------
    InstrumentConfig | None
        The configuration, or None if none was named.

    Raises
    ------
    SystemExit
        If the file cannot be read or does not describe an instrument.
    """
    if path is None:
        return None
    try:
        return load_instrument_config(path)
    except (InstrumentConfigError, OSError) as error:
        raise SystemExit(str(error)) from error


def _front_ends(args: argparse.Namespace, publish: Path) -> list[FrontEnd]:
    """
    Build the list of things the menu can open, in the order it shows them.

    The viewer is always there; the dashboard is there when a command
    for it was given, in its own environment. Both are resolved for
    that environment here rather than at spawn time, so a dashboard
    that is not installed where it was told to run fails at startup
    with a message naming the environment, rather than on a click.

    Parameters
    ----------
    args : argparse.Namespace
        The parsed arguments.
    publish : Path
        Where the broker will publish, which is what each client is
        pointed at.

    Returns
    -------
    list[FrontEnd]
        One entry per menu item.
    """
    viewer_environment = resolve_environment(args.ui_env)
    front_ends = [
        FrontEnd(
            label="viewer",
            command=tuple(
                child_command(
                    viewer_command(
                        publish,
                        session=args.session,
                        operator=args.operator,
                        sample=args.sample,
                        notes=args.notes,
                    ),
                    viewer_environment,
                ),
            ),
            environment=viewer_environment,
        ),
    ]
    given = [argument for argument in args.dashboard if argument != "--"]
    if given:
        dashboard_environment = resolve_environment(args.dashboard_env)
        front_ends.append(
            FrontEnd(
                label="dashboard",
                command=tuple(child_command(given, dashboard_environment)),
                environment=dashboard_environment,
            ),
        )
    return front_ends


def _build_session(args: argparse.Namespace, publish: Path) -> InstrumentSession:
    """
    Assemble the session from the command line.

    Parameters
    ----------
    args : argparse.Namespace
        The parsed arguments.
    publish : Path
        Where the broker will publish.

    Returns
    -------
    InstrumentSession
        Built, not started.
    """
    broker_environment = resolve_environment(args.broker_env)
    return InstrumentSession(
        child_command(
            [sys.executable, "-m", BROKER_MODULE, *broker_arguments(args, publish)],
            broker_environment,
        ),
        _front_ends(args, publish),
        publish,
        broker_environment=broker_environment,
        config=_load_config(args.config),
    )


def main(argv: list[str] | None = None) -> int:
    """
    Hold one instrument open from the notification area until told not to.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments, or None to read ``sys.argv``.

    Returns
    -------
    int
        The status to exit with.

    Raises
    ------
    SystemExit
        If this desktop has no notification area to put an icon in.
        There is no useful degraded mode - an application whose entire
        interface is one icon cannot run without it - and the command
        that does the same job in a terminal is named instead.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        sys.argv[:1],
    )
    application.setApplicationName(APPLICATION_NAME)
    # Every window this raises - the health panel, a confirmation - is
    # incidental to an application that lives in the tray. Without this,
    # closing one of them ends the session and the instrument with it.
    application.setQuitOnLastWindowClosed(False)
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        message = (
            "this desktop has no notification area, so there is nowhere to "
            "put the icon. Use 'miainwoodpecker-instrument --serve --publish "
            "<path>' instead, which holds the same instrument open from a "
            "terminal and ends on Ctrl-C."
        )
        raise SystemExit(message)
    publish = Path(args.publish) if args.publish is not None else default_publish()
    tray = TrayInstrument(_build_session(args, publish))
    tray.start()
    try:
        return int(application.exec())
    finally:
        # Whatever ended the loop - an exception, a desktop session
        # logging out - the broker is holding an instrument and must be
        # asked to put it down.
        tray.shut_down()


if __name__ == "__main__":
    sys.exit(main())
