"""
One command that starts an instrument and something to look at it with.

The pieces have been separately launchable for a while, and separately
launching them is four things to get right in order: start the broker,
find the port it chose, start the window against it, and stop the broker
afterwards *without* killing it. This is that sequence, written once.

    pixi run instrument

It is a supervisor, not a new layer. Nothing here talks to a device, or
to a broker; it spawns two processes and gets their lifetimes right,
which is the only part that was manual. Everything either of them can do
from its own command line it can still do from this one - the flags are
passed through.

**Two shapes of session, and the difference is about the instrument
rather than about this command.** The default is one sitting: a broker,
one front end, and an instrument put down when that front end closes.
``--serve`` is the other - hold the microscope open and let people come
and go, a window this morning, a notebook after lunch - and it ends on
Ctrl-C rather than on anything closing. Which front end the first shape
opens is a command after ``--``; the second opens none, and clients
find it through the invitation it publishes.

**Why a process rather than a task graph.** ``depends-on`` in a pixi
manifest is sequential: a task that depends on the broker and the viewer
runs the broker to completion and then starts the viewer, which is
exactly not the arrangement wanted. Measured rather than assumed - two
three-second tasks joined by a third take six seconds. Something has to
sit there holding both, and that something is a process.

**Why the two children can be in different environments.** ``pixi run``
exports ``PIXI_EXE`` and ``PIXI_PROJECT_MANIFEST``, so this process can
ask pixi about an environment other than the one it is in. That is what
``--broker-env`` and ``--ui-env`` do, and it is worth having for a
reason beyond convenience: the broker is the process that imports the
vendor device stack, and running the window in an environment that does
not contain it keeps the licensing boundary this project claims (README,
"Licensing") a fact about what is installed rather than a statement
about who imports what. Without those flags both children run in this
process's own environment, which is what a ``pip`` or ``uv`` install
gets.

**The environment is resolved, not wrapped, and that is a shutdown
requirement rather than a preference.** The obvious spelling is to spawn
``pixi run -e device miainwoodpecker-broker``, and it is wrong here.
Measured: a broker started that way and asked to stop exited with
``0xC000013A`` - the console-control kill - and **never ran its shutdown,
so the instrument was not parked**; started directly it exits zero and
logs the park. What happens is that the signal reaches ``pixi.exe``,
which is the process this one can address, and the broker underneath it
is taken down with it rather than being asked. So ``pixi shell-hook
--json`` is used to *resolve* the environment - its variables and its
interpreter - and the broker is then a direct child of this process,
which is the only arrangement in which "ask it to stop, and it parks"
is true.

**Stopping is the part worth reading.** The broker parks the instrument
on its way out, and parking is what makes a stationary probe stop being
one - so the broker must be *asked* to stop rather than killed, and it
must be asked last, after whatever was using it has gone. That is what
:func:`stop` and :func:`_supervise` are careful about, and it is why the
children are each put in their own process group: a Ctrl-C in this
terminal reaches this process alone, and the orderly sequence happens
here rather than three processes racing each other to die.

**The two children are not owed the same patience, and finding that out
cost an operator thirty seconds.** A Ctrl-C a second after the window
was launched produced "pid 24332 did not stop within 30s; terminating
it", and the instrument sat unparked behind a front end that was never
going to answer: a napari process signalled during its own startup
stops responding to console control events altogether, measured, for as
long as anyone cares to keep asking. The broker is worth thirty seconds
because parking is worth thirty seconds. A window is worth five, after
which it is terminated - see :data:`FRONT_END_TIMEOUT_S`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import typing
from pathlib import Path

from miainwoodpecker.broker.invitation import DEFAULT_FILENAME
from miainwoodpecker.devices.remote import (
    DEFAULT_SERVER_MODULE,
    HARDWARE_BACKEND,
    SIMULATED_BACKEND,
    own_process_group,
)

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

_LOGGER = logging.getLogger("miainwoodpecker.launcher")

BROKER_MODULE = "miainwoodpecker.broker.app"
"""The module launched as the instrument's broker."""

VIEWER_MODULE = "miainwoodpecker.viewer.app"
"""The module launched as the front end, unless one is given."""

BROKER_ENV_VAR = "MIAINWOODPECKER_BROKER"
"""
Where the front end is told to find the broker.

The environment rather than a flag, because the front end may be
anything - the Qt window takes ``--broker``, the marimo dashboard reads
this variable, and a script somebody writes next month can read it too
without this module learning about it.
"""

INVITATION_TIMEOUT_S = 120.0
"""
How long to wait for the broker to publish where it is listening.

Generous, because what is being waited for is a device server starting:
importing a vendor stack and opening hardware, which on a cold
microscope PC is tens of seconds before anything is wrong. A broker that
*fails* does not cost this wait - the wait ends the moment the process
exits, and says so.
"""

_STOP_TIMEOUT_S = 30.0
"""
How long the broker gets to shut down after being asked, before it is made to.

Sized for what it has to do: stop live loops, park the instrument and
close a device server, with parking bounded by whatever the hardware
takes to reach a safe state. Worth waiting out, because the alternative
to a parked instrument is a stationary probe.
"""

FRONT_END_TIMEOUT_S = 5.0
"""
How long a front end gets, which is much less, and deliberately.

A window has nothing to park. What it can lose by being terminated is a
recording in flight, which the storage layer already treats as a case
rather than a catastrophe - an unfinalized file displays and will not
analyze, and says so.

Against that: **a napari front end asked to stop during its own startup
may answer nothing at all, ever.** Measured, on Windows, against a
window opened by this launcher - a console control event delivered a
second after the process starts leaves it neither dead nor started, and
it then ignores twenty more over the next forty seconds; the same event
to the same window once it is up ends it in 0.1s. So the case this
timeout is sized for is not a slow shutdown, it is a front end that will
never shut down, and thirty seconds of an operator watching a terminal
buys nothing over five.

Public because :mod:`miainwoodpecker.tray` stops the same front ends in
the same order and would otherwise reintroduce the wait this exists to
remove - and there it is worse, since the tray may have several windows
open and would spend this on each of them before the broker is asked to
park anything.
"""

_KILL_GRACE_S = 5.0
_POLL_S = 0.2


def resolve_environment(env_name: str | None) -> dict[str, str]:
    """
    Return the variables that put a child in one pixi environment.

    Asked of ``pixi shell-hook --json``, which is the supported way to
    get an activation without running under ``pixi run`` - and running
    under ``pixi run`` is the thing this module cannot do to the broker,
    for the reason the module docstring measures.

    Parameters
    ----------
    env_name : str | None
        The environment to resolve, or None for "this one", which needs
        nothing resolved.

    Returns
    -------
    dict[str, str]
        Variables to add to the child's environment. Empty for None.

    Raises
    ------
    SystemExit
        If this is not running under pixi, if pixi cannot describe the
        environment, or if that environment ships **activation scripts**
        - which this cannot run, and skipping them silently is how a
        vendor stack half-works on a microscope PC. All three name the
        two commands to run by hand instead.
    """
    if env_name is None:
        return {}
    pixi = os.environ.get("PIXI_EXE")
    manifest = os.environ.get("PIXI_PROJECT_MANIFEST")
    if not pixi or not manifest:
        message = (
            f"asked for the {env_name!r} environment, but $PIXI_EXE is not "
            "set, so this is not running under pixi and there is no other "
            "environment to reach. Drop --broker-env/--ui-env to run both "
            "processes in this one."
        )
        raise SystemExit(message)
    hook = subprocess.run(  # noqa: S603 - argv built here, no shell
        [pixi, "shell-hook", "--json", "--manifest-path", manifest, "-e", env_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if hook.returncode != 0:
        message = (
            f"pixi could not describe the {env_name!r} environment: "
            f"{hook.stderr.strip() or hook.returncode}"
        )
        raise SystemExit(message)
    described = json.loads(hook.stdout)
    variables = dict(described["environment_variables"])
    # shell-hook reports the *difference* from the environment it is run
    # in, so asking for the environment this process is already in
    # answers without a CONDA_PREFIX - and that is the one variable
    # everything below needs, since it is where the interpreter lives.
    # `pixi info` knows it whichever environment is being asked about.
    variables.setdefault("CONDA_PREFIX", _prefix_of(pixi, manifest, env_name))
    if described.get("activation_scripts"):
        message = (
            f"the {env_name!r} environment activates through scripts "
            f"({', '.join(described['activation_scripts'])}), which this "
            "cannot run - and a child started without them is a child whose "
            "vendor stack half-works. Start the two processes by hand "
            f"instead:\n"
            f"    pixi run -e {env_name} miainwoodpecker-broker --publish .\n"
            "    miainwoodpecker-viewer --broker ."
        )
        raise SystemExit(message)
    return variables


def _prefix_of(pixi: str, manifest: str, env_name: str) -> str:
    """
    Return where pixi has installed one environment.

    Parameters
    ----------
    pixi : str
        The pixi executable.
    manifest : str
        The workspace manifest.
    env_name : str
        The environment to look up.

    Returns
    -------
    str
        Its prefix directory.

    Raises
    ------
    SystemExit
        If the workspace has no such environment - a typo in
        ``--broker-env``, or a manifest that does not define it.
    """
    described = subprocess.run(  # noqa: S603 - argv built here, no shell
        [pixi, "info", "--json", "--manifest-path", manifest],
        capture_output=True,
        text=True,
        check=False,
    )
    if described.returncode != 0:
        message = f"pixi could not describe this workspace: {described.stderr.strip()}"
        raise SystemExit(message)
    for environment in json.loads(described.stdout).get("environments_info", ()):
        if environment.get("name") == env_name:
            return str(environment["prefix"])
    message = (
        f"this workspace defines no {env_name!r} environment. "
        "'pixi info' lists the ones it does."
    )
    raise SystemExit(message)


def child_command(
    command: Sequence[str],
    environment: dict[str, str],
) -> list[str]:
    """
    Return ``command`` with its executable resolved in a given environment.

    Two substitutions, both of which are wrong to leave out. This
    process's ``sys.executable`` is *this* environment's interpreter and
    would run the other environment's code against this one's packages,
    which is the failure that switching environments exists to avoid.
    And a name like ``marimo`` is looked up by ``CreateProcess`` against
    the **parent's** search path rather than the child's, so a front end
    that lives only in the other environment would not be found at all.

    Parameters
    ----------
    command : Sequence[str]
        The command as it would be run in this process's environment.
    environment : dict[str, str]
        What :func:`resolve_environment` returned; empty for "this one".

    Returns
    -------
    list[str]
        The argv to spawn.

    Raises
    ------
    SystemExit
        If the command names something that environment does not have.
    """
    prefix = environment.get("CONDA_PREFIX")
    if not prefix:
        return list(command)
    if command[0] == sys.executable:
        interpreter = Path(prefix) / (
            "python.exe" if sys.platform == "win32" else "bin/python"
        )
        return [str(interpreter), *command[1:]]
    found = shutil.which(command[0], path=environment.get("PATH"))
    if found is None:
        message = (
            f"{command[0]!r} is not in the environment it was asked to run "
            f"in ({prefix})."
        )
        raise SystemExit(message)
    return [found, *command[1:]]


def spawn(
    command: Sequence[str],
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """
    Start one child, in its own process group.

    Its own group so that a Ctrl-C in this terminal arrives *here* and
    nowhere else. Sharing a group would deliver it to all three at once,
    and the broker would be interrupted alongside the window it is
    serving rather than after it - which is the difference between an
    instrument that parks and one that is left as it was.

    Parameters
    ----------
    command : Sequence[str]
        The argv to run.
    env : dict[str, str] | None
        The child's environment, or None to inherit this one's.

    Returns
    -------
    subprocess.Popen
        The running child.
    """
    _LOGGER.info("starting: %s", " ".join(command))
    return subprocess.Popen(  # noqa: S603 - argv built here, no shell
        list(command),
        env=env,
        **own_process_group(),
    )


def stop(process: subprocess.Popen, timeout_s: float = _STOP_TIMEOUT_S) -> int | None:
    """
    Ask a child to stop, and make it if it will not.

    Asked, not killed, and the distinction is the whole reason this
    function exists rather than a ``terminate()`` call: the broker's
    shutdown is what parks the instrument, and on Windows
    ``terminate()`` is a ``TerminateProcess`` - no handler runs, no
    cleanup happens, and a scan that was running is left running with
    nothing driving it. So the group gets a Ctrl-Break there and a
    ``SIGTERM`` elsewhere, both of which the broker installs handlers
    for, and only a child that ignores that is terminated.

    Parameters
    ----------
    process : subprocess.Popen
        The child to stop.
    timeout_s : float
        How long to wait for it to go on its own. The default is the
        broker's, which is the child worth waiting for; a front end is
        given :data:`FRONT_END_TIMEOUT_S` instead, because one that has
        not answered in five seconds has been measured not to answer at
        all.

    Returns
    -------
    int | None
        Its exit status.
    """
    if process.poll() is not None:
        return process.returncode
    try:
        if sys.platform == "win32":
            # Windows has no per-process signal: this goes to the group,
            # which is why each child was given one of its own.
            os.kill(process.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        # It exited between the poll above and here, or the group is
        # already gone. Either way there is nothing left to ask.
        return process.poll()
    try:
        return process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _LOGGER.warning(
            "pid %s did not stop within %.0fs; terminating it",
            process.pid,
            timeout_s,
        )
    process.terminate()
    try:
        return process.wait(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


def wait_for_invitation(
    process: subprocess.Popen,
    invitation: Path,
    timeout_s: float = INVITATION_TIMEOUT_S,
) -> None:
    """
    Wait until the broker has published where it is listening.

    The handshake between the two children, and it is the broker's own
    ``--publish`` rather than anything invented here: the port is chosen
    by the OS and the authkey is generated per run, so neither can be
    put on a command line in advance.

    Parameters
    ----------
    process : subprocess.Popen
        The broker, watched so that a failure to start is reported as
        one rather than waited out.
    invitation : Path
        The file the broker writes.
    timeout_s : float
        How long to wait before giving up.

    Raises
    ------
    SystemExit
        If the broker exits first, or never publishes. Both are things
        an operator can act on - a missing plug-in, a device server that
        will not start - and neither is worth a traceback.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if invitation.exists():
            _LOGGER.info("the broker published %s", invitation)
            return
        status = process.poll()
        if status is not None:
            message = (
                f"the broker exited with status {status} before it published "
                f"{invitation.name}. Its own output above says why; the usual "
                "causes are a device server that could not start and a "
                "--backend or --plugin naming hardware that is not there."
            )
            raise SystemExit(message)
        time.sleep(_POLL_S)
    stop(process)
    message = (
        f"the broker did not publish {invitation} within {timeout_s:.0f}s. It "
        "has been stopped; start it on its own with 'miainwoodpecker-broker "
        "--publish .' to see where it gets to."
    )
    raise SystemExit(message)


def _supervise(
    broker: subprocess.Popen,
    front_end: subprocess.Popen,
    requested: threading.Event,
) -> int:
    """
    Watch both children, and stop the other when either finishes.

    The ordering is the point. Closing the window is the ordinary way a
    session ends, and the broker is stopped *after* it, so the client is
    gone before the instrument is parked. The reverse - a broker that
    dies under a running window - is a fault rather than an ending, and
    the window is stopped because there is nothing left for it to talk
    to.

    Parameters
    ----------
    broker : subprocess.Popen
        The broker.
    front_end : subprocess.Popen
        The window, dashboard or script being run against it.
    requested : threading.Event
        Set when this process has been asked to stop.

    Returns
    -------
    int
        What this command should exit with: the front end's status when
        it ended the session, a failure when the broker ended it, and
        130 when something ended it from outside - a session cut short
        is not a session that finished.
    """
    try:
        while not requested.is_set():
            status = front_end.poll()
            if status is not None:
                _LOGGER.info(
                    "the front end exited with status %s; stopping the broker",
                    status,
                )
                stop(broker)
                return status
            status = broker.poll()
            if status is not None:
                _LOGGER.error(
                    "the broker exited with status %s; stopping the front end",
                    status,
                )
                stop(front_end, FRONT_END_TIMEOUT_S)
                return status or 1
            time.sleep(_POLL_S)
    except KeyboardInterrupt:
        # Only reachable where the handlers could not be installed.
        _LOGGER.info("interrupted")
    _LOGGER.info("asked to stop; stopping the front end, then the broker")
    stop(front_end, FRONT_END_TIMEOUT_S)
    stop(broker)
    # The conventional status for "ended from outside", and not zero:
    # this command was asked to run a session and the session did not
    # end on its own terms.
    return 130


def stop_requests() -> threading.Event:
    """
    Install handlers for every way this process may be asked to stop.

    The same three :mod:`miainwoodpecker.broker.app` installs, and for
    the same reason one layer up: ``SIGTERM`` from a service manager and
    ``SIGBREAK`` on Windows both **terminate the interpreter without
    unwinding** by default, and this process is the one holding the
    broker that holds the instrument. Left to the defaults, a supervisor
    stopping this would leave the broker orphaned and the instrument
    unparked - the exact failure the broker's own handlers exist to
    prevent, reintroduced by putting a launcher in front of it.

    Ctrl-C is included so that all three arrive the same way: as an
    event the loops below poll, rather than as an exception in whichever
    line happened to be executing.

    Returns
    -------
    threading.Event
        Set when a stop has been asked for.
    """
    requested = threading.Event()

    def handle(signum: int, frame: object) -> None:
        """
        Record that a stop was asked for.

        Parameters
        ----------
        signum : int
            The signal that arrived.
        frame : object
            The interrupted frame, unused.
        """
        del frame
        _LOGGER.info("asked to stop by %s", signal.Signals(signum).name)
        requested.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue  # SIGBREAK is Windows-only; SIGTERM is not on all hosts.
        # A host that refuses a handler - or a caller running this off
        # the main thread, as a test may - is not a reason to refuse to
        # run. The loops keep their KeyboardInterrupt path for that.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(number, handle)
    return requested


def _serve(broker: subprocess.Popen, requested: threading.Event) -> int:
    """
    Hold the instrument open with no front end of its own, until stopped.

    The other shape a session comes in. :func:`_supervise` runs one
    front end and ends when it does, which is right for "open a window
    on the microscope" and wrong for an instrument that stays served
    while people come and go - a window this morning, a notebook after
    lunch, a dashboard on the wall throughout. Here the broker is the
    session, and Ctrl-C is how it ends.

    Nothing is spawned, so nothing has to be stopped in order: the
    clients are somebody else's processes, started and closed whenever
    they like against the invitation this published.

    Parameters
    ----------
    broker : subprocess.Popen
        The broker to hold open.
    requested : threading.Event
        Set when this process has been asked to stop.

    Returns
    -------
    int
        Zero when it was asked to stop and did, because in this mode
        that is the ordinary ending rather than a session cut short -
        the same status the broker itself reports for a requested
        shutdown. A broker that exited on its own reports why it did.
    """
    try:
        while not requested.is_set():
            status = broker.poll()
            if status is not None:
                _LOGGER.error("the broker exited with status %s", status)
                return status or 1
            time.sleep(_POLL_S)
    except KeyboardInterrupt:
        # Only reachable where the handlers could not be installed.
        _LOGGER.info("interrupted")
    _LOGGER.info("stopping the broker")
    stop(broker)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse the launcher's command-line arguments.

    Parameters
    ----------
    argv : list[str] | None
        Argument list, or None to read ``sys.argv``.

    Returns
    -------
    argparse.Namespace
        The parsed arguments, with anything after ``--`` in
        ``front_end``.
    """
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[1],
        epilog=(
            "Anything after -- replaces the window: "
            "'miainwoodpecker-instrument -- marimo run "
            "notebooks/instrument_dashboard.py' serves the instrument and "
            f"opens the dashboard on it. ${BROKER_ENV_VAR} is set for it."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            "instrument configuration enumerating this microscope's hardware "
            "and the servers that drive it; a directory gets instrument.toml "
            "inside it. Passed to the broker, which starts every adapter the "
            "file lists - so it replaces --backend, --plugin and "
            "--server-module rather than combining with them"
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
            f"{DEFAULT_SERVER_MODULE}). Use "
            "miainwoodpecker.devices.camera_server for a webcam, a USB "
            "microscope or a video file, which needs no vendor SDK"
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
        "--serve",
        action="store_true",
        help=(
            "hold the instrument open with no front end of its own, until "
            "Ctrl-C. For a microscope that stays served while people come "
            "and go - a window now, a notebook later - rather than one "
            "session that ends when its window closes. Needs --publish, "
            "since clients have to be able to find it"
        ),
    )
    parser.add_argument(
        "--publish",
        default=None,
        metavar="PATH",
        help=(
            "where to publish the broker's connection details, so a notebook "
            "or a second window can join this instrument too. The default is "
            "a temporary directory, removed on the way out - which keeps a "
            "session that nobody else is joining from leaving an authkey on "
            "disk"
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
            "device stack. Requires this to be running under pixi; without "
            "it the broker runs in this environment"
        ),
    )
    parser.add_argument(
        "--ui-env",
        default=None,
        metavar="NAME",
        help=(
            "pixi environment to run the front end in. Naming one that does "
            "not contain the vendor stack is how the application demonstrably "
            "runs without it"
        ),
    )
    parser.add_argument(
        "front_end",
        nargs=argparse.REMAINDER,
        metavar="-- COMMAND",
        help="a command to run instead of the viewer; see below",
    )
    return parser.parse_args(argv)


def broker_arguments(args: argparse.Namespace, publish: Path) -> list[str]:
    """
    Build the broker's command line from what was passed through.

    Only what was actually given, so the broker's own defaults stay the
    broker's - this command has no opinion about which backend is the
    safe one, and repeating that opinion here would be a second place to
    change it. That includes the contradictions: ``--config`` with
    ``--backend`` is refused by the broker's own parser, in the one
    message that can explain why, rather than being caught twice.

    Public because :mod:`miainwoodpecker.tray` is the other supervisor
    over the same broker and passes the same flags through. Both parsers
    define these arguments; this builds the command line from either.

    Parameters
    ----------
    args : argparse.Namespace
        The parsed arguments.
    publish : Path
        Where the broker should publish its invitation.

    Returns
    -------
    list[str]
        Arguments for :data:`BROKER_MODULE`.
    """
    arguments = ["--publish", str(publish)]
    for name in ("config", "backend", "server_module", "host"):
        value = getattr(args, name, None)
        if value is not None:
            arguments += [f"--{name.replace('_', '-')}", str(value)]
    for plugin in getattr(args, "plugin", None) or ():
        arguments += ["--plugin", plugin]
    return arguments


def viewer_command(
    publish: Path,
    *,
    session: str | None = None,
    operator: str | None = None,
    sample: str | None = None,
    notes: str | None = None,
) -> list[str]:
    """
    Build the command that opens a window on a broker that is running.

    ``-m`` rather than the ``miainwoodpecker-viewer`` script, for the
    reason :mod:`miainwoodpecker.__main__` gives: the module path works
    in an environment whose entry-point scripts are missing or stale,
    and this is spawned into an environment that may not be this one.

    Parameters
    ----------
    publish : Path
        Where the broker published its invitation.
    session : str | None
        Session directory for recordings, or None for the viewer's own
        default.
    operator : str | None
        Who is on the instrument.
    sample : str | None
        Sample identifier.
    notes : str | None
        Free-text session notes.

    Returns
    -------
    list[str]
        The argv to run.
    """
    arguments = [sys.executable, "-m", VIEWER_MODULE, "--broker", str(publish)]
    for name, value in (
        ("session", session),
        ("operator", operator),
        ("sample", sample),
        ("notes", notes),
    ):
        if value is not None:
            arguments += [f"--{name}", str(value)]
    return arguments


def _front_end_command(args: argparse.Namespace, publish: Path) -> list[str]:
    """
    Build the front end's command line, or take the one that was given.

    Parameters
    ----------
    args : argparse.Namespace
        The parsed arguments.
    publish : Path
        Where the broker published its invitation.

    Returns
    -------
    list[str]
        The argv to run as the front end.
    """
    given = [argument for argument in args.front_end if argument != "--"]
    if given:
        return given
    return viewer_command(
        publish,
        session=args.session,
        operator=args.operator,
        sample=args.sample,
        notes=args.notes,
    )


def main(argv: list[str] | None = None) -> int:
    """
    Serve one instrument, with a front end on it or with none.

    Two shapes, and which one is wanted is a question about the
    *instrument* rather than about this command. ``--serve`` holds a
    microscope open for whoever connects, all day, and ends on Ctrl-C.
    Without it this is one session: a broker, one front end, and an
    instrument put down when that front end closes.

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
        If ``--serve`` was asked for without ``--publish``, or with a
        front end to run. Both are contradictions rather than
        preferences: nothing can join a broker whose invitation is in a
        temporary directory nobody was told about, and a mode whose
        point is having no front end cannot be given one.
    BaseException
        Whatever went wrong on the way up - a broker that would not
        publish, an environment that cannot be resolved, an interrupt
        during startup - re-raised once the broker has been stopped. It
        is holding an instrument, and a failure here must not leave it
        holding one.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    if args.serve and args.publish is None:
        message = (
            "--serve holds the instrument open for clients to join, and they "
            "join at the path it publishes - so it needs somewhere to "
            "publish. Add --publish . (which is where a notebook looks by "
            "default), or drop --serve to run one session with its own front "
            "end."
        )
        raise SystemExit(message)
    if args.serve and [argument for argument in args.front_end if argument != "--"]:
        message = (
            "--serve runs no front end, so the command after -- would never "
            "be started. Run it yourself against the published invitation, "
            "or drop --serve."
        )
        raise SystemExit(message)
    temporary = args.publish is None
    publish = (
        Path(tempfile.mkdtemp(prefix="miainwoodpecker-"))
        if temporary
        else Path(args.publish)
    )
    publish.mkdir(parents=True, exist_ok=True)
    invitation = publish / DEFAULT_FILENAME
    # A leftover from a previous run would be read as this one's, and
    # the front end would dial a port nothing is listening on - with an
    # authkey that no longer means anything, so the failure would be an
    # authentication error rather than a refused connection.
    invitation.unlink(missing_ok=True)
    broker_environment = resolve_environment(args.broker_env)
    front_end_environment = resolve_environment(None if args.serve else args.ui_env)
    # Before the broker exists, so that a stop arriving while it is
    # starting is still noticed rather than terminating this process
    # with a child it has not begun to supervise.
    requested = stop_requests()
    broker = spawn(
        child_command(
            [sys.executable, "-m", BROKER_MODULE, *broker_arguments(args, publish)],
            broker_environment,
        ),
        env={**os.environ, **broker_environment},
    )
    try:
        wait_for_invitation(broker, invitation)
        if args.serve:
            _LOGGER.info(
                "serving %s; connect with 'miainwoodpecker-viewer --broker %s' "
                "or $%s, and press Ctrl-C here to stop",
                invitation,
                publish,
                BROKER_ENV_VAR,
            )
            return _serve(broker, requested)
        front_end = spawn(
            child_command(_front_end_command(args, publish), front_end_environment),
            env={
                **os.environ,
                **front_end_environment,
                BROKER_ENV_VAR: str(publish),
            },
        )
        return _supervise(broker, front_end, requested)
    except BaseException:
        # Including KeyboardInterrupt and SystemExit: whatever went
        # wrong on the way up, the broker is holding an instrument and
        # must be asked to put it down.
        stop(broker)
        raise
    finally:
        if temporary:
            shutil.rmtree(publish, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
