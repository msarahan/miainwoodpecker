"""
An instrument held open by something with no terminal to press Ctrl-C in.

:mod:`miainwoodpecker.launcher` supervises the same two processes and
does it by blocking: ``wait_for_invitation`` sits there until the broker
publishes, and ``_serve`` sits in a sleep loop until a signal arrives.
Neither shape survives being put behind a tray icon, because the thing
that has to stay responsive is an event loop that must not be blocked
for a second, let alone the two minutes a cold device server may take.

So the same sequence is turned inside out here: :meth:`~InstrumentSession.start`
returns immediately, :meth:`~InstrumentSession.poll` is called on a tick
and reports what has changed since the last one, and nothing in this
module waits for anything. That is the only real difference, and it is
the reason this is a class rather than another function in the launcher.

**Qt is deliberately absent.** Everything here is processes, files and
time; :mod:`miainwoodpecker.tray.app` is the part that draws a menu. The
split is the same one :mod:`miainwoodpecker.broker.app` makes between
``serve_instrument`` and ``main``, and it buys the same thing: the
sequence that holds an instrument can be tested with fake children on a
machine with no display, which is where the interesting failures are.

**The shutdown order is not a detail.** Front ends first, then the
broker, because the broker's exit is what parks the instrument and a
probe must not be parked out from under a window that is still driving
it. Both are *asked* rather than killed, which is what
:func:`~miainwoodpecker.launcher.stop` is careful about and what makes
the park run at all on Windows.

One thing here is not process supervision: :meth:`InstrumentSession.health`
connects to the broker as an ordinary watch-side client and asks it how
the device servers underneath it are doing. It is here because the
answer is wanted in two places that must agree - the health panel, and
the confirmation before quitting, which has to be able to say what is
running before it stops it.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import re
import threading
import time
import typing
from dataclasses import dataclass, field
from pathlib import Path

from miainwoodpecker.broker.invitation import DEFAULT_FILENAME, BrokerInvitation
from miainwoodpecker.broker.remote import connect_broker
from miainwoodpecker.launcher import (
    BROKER_ENV_VAR,
    FRONT_END_TIMEOUT_S,
    INVITATION_TIMEOUT_S,
    spawn,
    stop,
)
from miainwoodpecker.tray import health as health_report

if typing.TYPE_CHECKING:
    import subprocess
    from collections.abc import Sequence

    from miainwoodpecker.broker.remote import RemoteBroker
    from miainwoodpecker.instrument_config import InstrumentConfig

_LOGGER = logging.getLogger("miainwoodpecker.tray.session")

_URL = re.compile(r"https?://[^\s\"'<>|]+")
"""
The first address a front end prints, which is how it is reopened.

Marimo announces where it is listening and includes the access token in
the query string, so what this catches is a link that works rather than
a bare host and port that would land on a login page. Deliberately
greedy about the query string and stopped by whitespace, quotes and the
box-drawing bars marimo frames its banner with.
"""

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
"""
Colour codes, removed before a line is logged or searched.

A front end writing to a pipe should not be colouring anything, and
marimo does anyway; left in, the escape that follows a URL becomes part
of the URL.
"""


class SessionState(enum.Enum):
    """
    Where a tray-held instrument has got to.

    Four, and the menu differs in each: nothing can be opened on an
    instrument that is still starting, and there is nothing to put down
    once one has stopped.

    ``STARTING`` is a broker that is running but has not yet published
    where it is listening - on a cold microscope PC, the vendor stack
    importing and the hardware being opened. ``SERVING`` is an
    instrument clients can join. ``FAILED`` is a broker that exited on
    its own or never published, which the operator has to be told about
    rather than left with a tray icon that looks fine. ``STOPPED`` is
    asked to stop and stopped: the front ends have gone and the
    instrument has been parked.
    """

    STARTING = "starting"
    SERVING = "serving"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class SessionStatus:
    """
    What one :meth:`InstrumentSession.poll` found.

    A snapshot rather than a set of accessors, so that a menu built from
    it cannot show a state from one tick beside a count from another -
    the same reasoning the broker interface gives for reading a target's
    state and its frames under one lock.

    Attributes
    ----------
    state : SessionState
        Where the session has got to.
    message : str
        A sentence for the operator: where the broker is listening, or
        what went wrong. Never empty.
    front_ends : int
        How many windows this session has open and still running.
    invitation : BrokerInvitation | None
        Where the broker is listening, once it has said. None until
        then, and None again after a failure.
    changed : bool
        Whether :attr:`state` differs from the previous poll. What a
        caller should act on rather than comparing states itself: a
        failure has to be shown once, not on every tick.
    """

    state: SessionState
    message: str
    front_ends: int
    invitation: BrokerInvitation | None = None
    changed: bool = False


class Opened(enum.Enum):
    """
    What came of asking for a client, which is three things and not two.

    ``STARTED`` is a new one running. ``ALREADY_OPEN`` is one of that
    kind up already, and is not a refusal: it is the caller's cue to
    show what is there, which is a window raised or a browser tab
    opened rather than a message about why nothing happened.
    ``UNAVAILABLE`` is nothing to open it on - an instrument still
    starting, or a kind this session was never given a command for -
    and is the only one worth telling the operator about in words.
    """

    STARTED = "started"
    ALREADY_OPEN = "already open"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FrontEnd:
    """
    One thing the menu can open on the served instrument.

    There is more than one because the project has more than one, and
    they are not variants of each other: the Qt window and the browser
    dashboard are separate programs, wanting separate environments -
    Qt in one, marimo and no Qt in the other - and both are ordinary
    clients of the broker rather than the application it belongs to.
    Carrying the environment here rather than one per session is what
    lets the tray offer both at once.

    Attributes
    ----------
    label : str
        What it is, in one word, and what the menu says: "viewer" gets
        "Open a viewer" and "Open another viewer (2 open)". Also the
        identity a count is kept against, so two dashboards do not read
        as two windows.
    command : tuple[str, ...]
        The argv to run, already resolved for whichever environment it
        belongs in - see
        :func:`~miainwoodpecker.launcher.child_command`.
    environment : dict[str, str]
        Variables to add to its environment, from
        :func:`~miainwoodpecker.launcher.resolve_environment`.
    """

    label: str
    command: tuple[str, ...]
    environment: dict[str, str] = field(default_factory=dict)


class InstrumentSession:
    """
    One broker, the windows opened on it, and the order they stop in.

    Started once, polled repeatedly, and shut down exactly once. Every
    method is non-blocking except :meth:`shutdown`, which has to wait for
    the instrument to be parked and is the one place a caller should
    expect to be held up.

    Parameters
    ----------
    broker_command : Sequence[str]
        The argv that runs the broker, already resolved for whichever
        environment it belongs in - see
        :func:`~miainwoodpecker.launcher.child_command`.
    front_ends : Sequence[FrontEnd]
        What can be opened on it, in menu order. The first is what a
        double-click opens.
    publish : Path
        Where the broker publishes its invitation, and where clients
        look for it. Created if it does not exist.
    broker_environment : dict[str, str] | None
        Variables to add to the broker's environment, from
        :func:`~miainwoodpecker.launcher.resolve_environment`.
    config : InstrumentConfig | None
        The instrument this broker was started from, when it was started
        from a file. Read only to say which device server each target
        came from - see :mod:`miainwoodpecker.tray.health`.
    timeout_s : float
        How long the broker gets to publish before this gives up on it.
    """

    def __init__(  # noqa: PLR0913 - one construction site, everything named
        self,
        broker_command: Sequence[str],
        front_ends: Sequence[FrontEnd],
        publish: Path,
        *,
        broker_environment: dict[str, str] | None = None,
        config: InstrumentConfig | None = None,
        timeout_s: float = INVITATION_TIMEOUT_S,
    ) -> None:
        self._broker_command = list(broker_command)
        self._openable = {front_end.label: front_end for front_end in front_ends}
        self._publish = Path(publish)
        self._broker_environment = dict(broker_environment or {})
        self._config = config
        self._timeout_s = timeout_s
        self._invitation_path = self._publish / DEFAULT_FILENAME
        self._broker: subprocess.Popen | None = None
        self._running: list[tuple[str, subprocess.Popen]] = []
        self._urls: dict[str, str] = {}
        self._invitation: BrokerInvitation | None = None
        self._watcher: RemoteBroker | None = None
        self._state = SessionState.STARTING
        self._message = "starting the instrument"
        self._reported = SessionState.STARTING
        self._started_at = 0.0

    @property
    def publish(self) -> Path:
        """
        Return where the broker publishes its invitation.

        Returns
        -------
        Path
            The directory a client is pointed at.
        """
        return self._publish

    @property
    def invitation(self) -> BrokerInvitation | None:
        """
        Return where the broker said it is listening.

        Returns
        -------
        BrokerInvitation | None
            The invitation, or None before it has published and again
            after it has gone.
        """
        return self._invitation

    @property
    def state(self) -> SessionState:
        """
        Return where the session has got to, as of the last poll.

        Returns
        -------
        SessionState
            The state.
        """
        return self._state

    @property
    def message(self) -> str:
        """
        Return the sentence that goes with :attr:`state`.

        Returns
        -------
        str
            Where the broker is listening, or what went wrong.
        """
        return self._message

    @property
    def front_ends(self) -> int:
        """
        Return how many clients this session opened and still has.

        Returns
        -------
        int
            The count, as of the last poll.
        """
        return len(self._running)

    @property
    def openable(self) -> tuple[FrontEnd, ...]:
        """
        Return what can be opened on this instrument, in menu order.

        Returns
        -------
        tuple[FrontEnd, ...]
            As given at construction.
        """
        return tuple(self._openable.values())

    def open_count(self, label: str | None = None) -> int:
        """
        Return how many of one kind of client are open, or of every kind.

        Per kind because the menu counts per entry: two dashboards must
        not read as two windows on an entry that opens windows.

        Parameters
        ----------
        label : str | None
            The kind to count, or None for all of them.

        Returns
        -------
        int
            The count, as of the last poll.
        """
        if label is None:
            return len(self._running)
        return sum(1 for opened, _ in self._running if opened == label)

    def start(self) -> None:
        """
        Spawn the broker and return, without waiting for it to be ready.

        Raises
        ------
        RuntimeError
            If called twice. One session holds one instrument; a second
            broker over the same hardware is not a state this can be in.
        """
        if self._broker is not None:
            message = "this session has already started a broker"
            raise RuntimeError(message)
        self._publish.mkdir(parents=True, exist_ok=True)
        # A leftover from a previous run would be read as this one's,
        # and a window would dial a port nothing is listening on with an
        # authkey that no longer means anything - so the failure would
        # be an authentication error rather than a refused connection.
        with contextlib.suppress(OSError):
            self._invitation_path.unlink(missing_ok=True)
        self._started_at = time.monotonic()
        self._broker = spawn(
            self._broker_command,
            env={**os.environ, **self._broker_environment},
        )

    def poll(self) -> SessionStatus:
        """
        Advance the session by one tick and report where it is.

        Cheap enough to call several times a second: a file check, a
        handful of ``waitpid`` calls, and nothing over a socket. It
        never raises, because the caller is an event loop that would
        have nowhere to put the exception and an instrument still
        running underneath it.

        Returns
        -------
        SessionStatus
            What the session is doing now.
        """
        if self._state in (SessionState.FAILED, SessionState.STOPPED):
            return self._status()
        self._running = [
            (label, process)
            for label, process in self._running
            if process.poll() is None
        ]
        alive = {label for label, _ in self._running}
        for label in [label for label in self._urls if label not in alive]:
            # A dashboard that has gone takes its address with it: the
            # next one picks another port and another access token, and
            # an old link would open a page that does not answer.
            self._urls.pop(label, None)
        if self._state is SessionState.STARTING:
            self._poll_starting()
        elif self._state is SessionState.SERVING:
            self._poll_serving()
        return self._status()

    def _poll_starting(self) -> None:
        """
        Look for the invitation, or for a reason there will never be one.

        The file is checked before the exit status, so a broker that
        published and then died is reported as having served rather than
        as never having started - the operator's next question differs
        between the two.
        """
        invitation = self._read_invitation()
        if invitation is not None:
            self._invitation = invitation
            self._state = SessionState.SERVING
            self._message = invitation.describe()
            _LOGGER.info("%s; publishing to %s", self._message, self._publish)
            return
        status = self._broker.poll() if self._broker is not None else None
        if status is not None:
            self._fail(
                f"the broker exited with status {status} before it said where "
                f"it was listening. The usual causes are a device server that "
                f"could not start, and a --backend, --plugin or --config "
                f"naming hardware that is not there.",
            )
            return
        if time.monotonic() - self._started_at > self._timeout_s:
            if self._broker is not None:
                stop(self._broker)
            self._fail(
                f"the broker did not publish {self._invitation_path.name} "
                f"within {self._timeout_s:.0f}s and has been stopped. Start it "
                f"on its own with 'miainwoodpecker-broker --publish .' to see "
                f"where it gets to.",
            )

    def _poll_serving(self) -> None:
        """
        Notice the broker going away under a session that was serving.

        A fault rather than an ending: nothing asked it to stop, so the
        instrument is now served by nobody and whatever is connected is
        about to find out the hard way.
        """
        status = self._broker.poll() if self._broker is not None else None
        if status is not None:
            self._fail(
                f"the broker exited with status {status}. The instrument is "
                f"no longer served, and anything connected to it has been "
                f"disconnected.",
            )

    def _read_invitation(self) -> BrokerInvitation | None:
        """
        Read the published invitation, if it is there and complete.

        A malformed read is treated as "not yet" rather than as a
        failure: the file is written in one call, but a reader that
        arrives mid-write would otherwise end a session that was about
        to work. The timeout is what covers a file that never becomes
        readable.

        Returns
        -------
        BrokerInvitation | None
            The invitation, or None if it is not there yet.
        """
        try:
            return BrokerInvitation.read_from(self._invitation_path)
        except (OSError, ValueError):
            return None

    def _fail(self, message: str) -> None:
        """
        Record that the instrument is no longer served, and why.

        Parameters
        ----------
        message : str
            What to tell the operator.
        """
        _LOGGER.error("%s", message)
        self._close_watch()
        self._state = SessionState.FAILED
        self._message = message
        self._invitation = None

    def _status(self) -> SessionStatus:
        """
        Build the snapshot :meth:`poll` returns, and mark it reported.

        Returns
        -------
        SessionStatus
            The current state, with ``changed`` set on the first poll
            that sees each new one.
        """
        changed = self._state is not self._reported
        self._reported = self._state
        return SessionStatus(
            state=self._state,
            message=self._message,
            front_ends=len(self._running),
            invitation=self._invitation,
            changed=changed,
        )

    def open_front_end(self, label: str | None = None) -> Opened:
        """
        Start a client on the instrument, unless that kind is already up.

        One of each kind, and the second click is a request to *look at*
        the one that exists rather than to have another. That is the
        judgement an operator actually makes at a tray icon: a menu
        entry pressed twice because the window went behind a browser is
        not an ask for two windows, and answering it with two is how a
        column ends up with four viewers on it by four o'clock.

        The broker itself has no such restriction and neither does this:
        a second window started by hand joins the same instrument the
        way a notebook does. What this declines to do is start one *by
        accident*.

        Parameters
        ----------
        label : str | None
            Which kind to open, or None for the first one offered -
            what a double-click on the icon gets.

        Returns
        -------
        Opened
            What came of asking. :attr:`Opened.ALREADY_OPEN` is the
            caller's cue to raise what :meth:`running` returns rather
            than to report anything.
        """
        if self._state is not SessionState.SERVING:
            return Opened.UNAVAILABLE
        front_end = (
            self._openable.get(label)
            if label is not None
            else next(iter(self._openable.values()), None)
        )
        if front_end is None:
            return Opened.UNAVAILABLE
        if self.running(front_end.label) is not None:
            return Opened.ALREADY_OPEN
        process = spawn(
            front_end.command,
            env={
                **os.environ,
                **front_end.environment,
                BROKER_ENV_VAR: str(self._publish),
            },
            capture=True,
        )
        self._running.append((front_end.label, process))
        self._read_output(front_end.label, process)
        return Opened.STARTED

    def running(self, label: str) -> subprocess.Popen | None:
        """
        Return the client of one kind that is up, if one is.

        Parameters
        ----------
        label : str
            The kind - a :attr:`FrontEnd.label`.

        Returns
        -------
        subprocess.Popen | None
            The process, or None if none of that kind is running.
        """
        for opened, process in self._running:
            if opened == label and process.poll() is None:
                return process
        return None

    def url(self, label: str) -> str | None:
        """
        Return the address one kind of client printed as it started, if any.

        The dashboard is a server with a page on it rather than a window
        on this desktop, so "show me the one that is already running"
        means opening its address - and the only thing that knows that
        address is the process itself, which prints it. A front end that
        prints no URL has none, which is every window.

        Parameters
        ----------
        label : str
            The kind - a :attr:`FrontEnd.label`.

        Returns
        -------
        str | None
            The first address it printed, or None.
        """
        return self._urls.get(label)

    def _read_output(self, label: str, process: subprocess.Popen) -> None:
        """
        Follow a client's output on a thread, for the log and for its URL.

        Read at all because a tray application has no console: without
        this, whatever a front end says on its way up - a traceback, a
        port it chose - goes nowhere. Read on a *thread* because the
        pipe has to be drained whether anybody is interested or not; a
        child whose output nobody reads blocks once the buffer fills,
        which for marimo is a few seconds in.

        Parameters
        ----------
        label : str
            The kind, for the log line and for recording its URL.
        process : subprocess.Popen
            The client, spawned with its output captured.
        """
        if process.stdout is None:
            return
        thread = threading.Thread(
            target=self._follow,
            args=(label, process),
            name=f"tray-{label}-output",
            daemon=True,
        )
        thread.start()

    def _follow(self, label: str, process: subprocess.Popen) -> None:
        """
        Log a client's output until it ends, noting the first URL in it.

        Parameters
        ----------
        label : str
            The kind.
        process : subprocess.Popen
            The client being followed.
        """
        with contextlib.suppress(OSError, ValueError):
            for line in process.stdout:
                said = _ANSI.sub("", line).rstrip()
                if not said:
                    continue
                _LOGGER.info("%s: %s", label, said)
                if label in self._urls:
                    continue
                found = _URL.search(said)
                if found is not None:
                    # One assignment into a dict, read from the main
                    # thread and never mutated in place, which needs no
                    # lock of its own.
                    self._urls[label] = found.group(0)
                    _LOGGER.info("the %s is at %s", label, found.group(0))

    def health(self) -> health_report.InstrumentHealth:
        """
        Ask the broker how the servers underneath it are doing.

        Two calls over the watch side of one connection, held open
        between reads so that a panel refreshing every few seconds is
        not a new socket and a new authentication each time. Nothing
        here takes a lease, so asking cannot itself disturb an
        acquisition - and nothing here touches a device, for the reason
        :mod:`miainwoodpecker.tray.health` gives at length.

        Returns
        -------
        health_report.InstrumentHealth
            Every server and its devices, or
            :func:`~miainwoodpecker.tray.health.unreachable` when the
            broker could not be asked.
        """
        if self._state is not SessionState.SERVING:
            return health_report.unreachable(self._message)
        broker = self._watch()
        if broker is None:
            return health_report.unreachable("the broker did not answer")
        try:
            return health_report.assess(
                broker.describe(),
                broker.targets(),
                config=self._config,
            )
        except Exception as error:  # noqa: BLE001 - see below
            # Anything at all, because this runs on a display tick and
            # on the way into a shutdown. An exception escaping here
            # would either stop the tray refreshing or leave an
            # instrument running because a dialog could not be filled
            # in; a connection dropped and retried next tick is the
            # worse-case behaviour worth having instead.
            _LOGGER.warning("the broker did not report its health: %s", error)
            self._close_watch()
            return health_report.unreachable(f"the broker did not answer: {error}")

    def busy(self) -> tuple[str, ...]:
        """
        Say what is acquiring or held, for a question asked before stopping.

        The clients that matter most here are the ones this process did
        not open - a notebook in somebody's home directory, a dashboard
        on a wall - and the broker is the only thing that knows about
        them. A count of this session's own windows would answer a
        different and much less useful question.

        Returns
        -------
        tuple[str, ...]
            One line per device that is acquiring or leased, empty when
            the instrument is idle or could not be asked.
        """
        return tuple(
            f"{device.name}: {device.detail}"
            for server in self.health().servers
            for device in server.devices
            if device.is_live or device.holder is not None
        )

    def _watch(self) -> RemoteBroker | None:
        """
        Return a watch-side connection to the broker, opening one if needed.

        Returns
        -------
        RemoteBroker | None
            The client, or None if the broker did not accept a
            connection - which is a fact about this moment rather than
            about the session, so it is retried on the next read.
        """
        if self._watcher is not None:
            return self._watcher
        if self._invitation is None:
            return None
        try:
            self._watcher = connect_broker(
                self._invitation.address(),
                self._invitation.authkey,
            )
        except (OSError, ValueError) as error:
            _LOGGER.warning("could not connect to the broker: %s", error)
            return None
        return self._watcher

    def _close_watch(self) -> None:
        """
        Drop the watch connection, if there is one.

        Errors are suppressed: this runs when something has already gone
        wrong with the connection, and closing a broken socket failing
        is not news.
        """
        if self._watcher is None:
            return
        with contextlib.suppress(Exception):
            self._watcher.close()
        self._watcher = None

    def shutdown(self) -> None:
        """
        Stop the clients, then the broker, and park the instrument.

        Blocks until both have gone, which is the point: the caller is
        on its way out and the instrument has to be put down before it
        gets there. Doing nothing on a session already stopped, because
        the paths that call it - a menu item, a closing event loop, a
        signal - can each be the second one to arrive.
        """
        if self._state is SessionState.STOPPED:
            return
        # Before the front ends, so that this process is not still
        # holding a connection to a broker it is about to ask to stop.
        self._close_watch()
        for _, process in self._running:
            # The front end's timeout rather than the broker's, for the
            # reason :data:`~miainwoodpecker.launcher.FRONT_END_TIMEOUT_S`
            # measures - and more so here, where several may be open and
            # the broker is not asked to park until the last of them has
            # been dealt with.
            stop(process, FRONT_END_TIMEOUT_S)
        self._running.clear()
        if self._broker is not None:
            _LOGGER.info("stopping the broker; the instrument will be parked")
            stop(self._broker)
        # The broker publishes on the way up and nothing removes it on
        # the way down, so a client starting tomorrow would read today's
        # port and authkey. Removed after the broker has gone, not
        # before, so it stays findable for as long as it is answerable.
        with contextlib.suppress(OSError):
            self._invitation_path.unlink(missing_ok=True)
        self._state = SessionState.STOPPED
        self._message = "the instrument has been put down"
        self._invitation = None
