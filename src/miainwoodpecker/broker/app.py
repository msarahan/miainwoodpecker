"""
Run a broker over a device server: one command, one instrument, many clients.

This is the piece that makes the rest usable without writing a program.
It opens a device session exactly as
:func:`~miainwoodpecker.devices.remote.remote_instrument` does for the
viewer and the scripts, wraps it in a
:class:`~miainwoodpecker.broker.local.LocalBroker`, serves that on a
socket, and publishes where it is
(:class:`~miainwoodpecker.broker.invitation.BrokerInvitation`) so a
notebook two directories away can connect without being told a port.

``--backend`` and ``--server-module`` describe *one* device server, and
an instrument is often more than one: a Nion column plus a DECTRIS ELA
on the spectrometer, a column plus an EDX detector behind a vendor SDK.
``--config`` names an instrument configuration
(:mod:`miainwoodpecker.instrument_config`) instead, and then this module
starts every adapter that file enumerates, checks each one served the
hardware the file says the microscope has, and serves the lot as one
broker. The two forms are alternatives rather than layers - a file says
which backend each of its servers runs, so a command line that also said
``--backend`` would be saying it twice and disagreeing is possible.

The split between :func:`serve_instrument` and :func:`main` is
deliberate. Everything interesting happens in the first, which takes a
device container it did not open - so it can be exercised against fakes,
and so a caller with its own device session (a test, an embedded viewer,
a future launcher that supervises several instruments) reuses it rather
than shelling out. :func:`serve_targets` is the same idea one step
further down, for the configured path, which has several containers and
has already decided what each target is called.

Shutdown is the part worth reading, because parking the instrument
depends on it. The broker's ``close`` parks, and parking blanks the beam
where one exists, so every way of stopping this process that *can* run
cleanup must actually reach it. Ctrl-C does. So does an exception. So
does a service manager or a supervisor asking the process to stop -
which is why :func:`_wait_for_stop` installs handlers rather than
trusting the defaults: an unhandled ``SIGBREAK`` on Windows or
``SIGTERM`` on POSIX terminates the interpreter outright, cleanup and
all, and the instrument is left scanning nothing with the loops still
running.

A hard kill still cannot be caught here, by anyone. That case is covered
one layer down: the device server parks when its controlling client
dies, which is the device layer's promise rather than this module's.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import secrets
import signal
import threading
import typing

from miainwoodpecker.broker.invitation import BrokerInvitation
from miainwoodpecker.broker.local import LocalBroker
from miainwoodpecker.broker.server import serve_broker
from miainwoodpecker.devices.remote import (
    DEFAULT_SERVER_MODULE,
    HARDWARE_BACKEND,
    SIMULATED_BACKEND,
    remote_instrument,
)
from miainwoodpecker.devices.rpc import INSTRUMENT_TARGET, SCANNER_TARGET
from miainwoodpecker.instrument_config import (
    InstrumentConfigError,
    load_instrument_config,
)

if typing.TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from miainwoodpecker.instrument_config import InstrumentConfig, ServerConfig

_LOGGER = logging.getLogger("miainwoodpecker.broker.app")

_AUTHKEY_BYTES = 32
_SIGNAL_POLL_S = 0.25
_BACKEND_ENV_VAR = "MIAINWOODPECKER_BACKEND"
_CONFIG_ENV_VAR = "MIAINWOODPECKER_INSTRUMENT"
_PLUGINS_ENV_VAR = "MIAINWOODPECKER_HARDWARE_PLUGINS"
_CAMERA_SERVER_MODULE = "miainwoodpecker.devices.camera_server"


def instrument_targets(devices: object) -> dict[str, object]:
    """
    Build a broker's target map from an open device container.

    Takes whatever
    :func:`~miainwoodpecker.devices.remote.remote_instrument` yielded and
    turns it into the ``name -> device`` mapping
    :class:`~miainwoodpecker.broker.local.LocalBroker` wants. The names
    are the device server's own, so a client leasing ``eels_camera``
    names the same thing a script does.

    Cameras and spectrum detectors come from ``cameras()`` and
    ``spectrum_detectors()`` rather than the named attributes, because
    those are the accessors that answer "what is actually here" - an
    instrument with two cameras beyond the named slots is served whole
    rather than truncated to the three fields the client type has.

    Parameters
    ----------
    devices : object
        An open device container.

    Returns
    -------
    dict[str, object]
        Target name to device, omitting what this instrument lacks.
    """
    return {INSTRUMENT_TARGET: devices.instrument, **served_devices(devices)}


def served_devices(devices: object) -> dict[str, object]:
    """
    Return what one device server is serving, under its own names.

    :func:`instrument_targets` without the ``instrument`` entry, which
    is the part a configured instrument has to look at rather than
    forward: every adapter process has an ``instrument`` target, and on
    all but one of them it is that server's control channel rather than
    the microscope's controls.

    Parameters
    ----------
    devices : object
        An open device container.

    Returns
    -------
    dict[str, object]
        Served name to device, omitting what this server lacks.
    """
    targets: dict[str, object] = {}
    scanner = getattr(devices, "scanner", None)
    if scanner is not None:
        targets[SCANNER_TARGET] = scanner
    targets.update(devices.cameras())
    targets.update(devices.spectrum_detectors())
    return targets


def configured_targets(
    config: InstrumentConfig,
    sessions: Mapping[str, object],
) -> dict[str, object]:
    """
    Build a broker's target map from a configuration and its open servers.

    Where :func:`instrument_targets` asks one server what it has, this
    checks several servers against what the instrument is supposed to
    have, and renames as it goes. Three rules, each argued in
    :mod:`miainwoodpecker.instrument_config`'s docstring and each
    visible here:

    - a listed device its server did not serve raises, naming what that
      server did serve, because the alternative is a session that looks
      right until somebody reaches for the detector;
    - a served device the file does not list is dropped and logged by
      name, because a file that enumerates the hardware is either
      authoritative or decorative;
    - ``instrument`` comes from the one server that owns the column, and
      from nowhere if none does - a detector-only rig has no stage, and
      the broker already serves one of those.

    Parameters
    ----------
    config : InstrumentConfig
        The instrument this is serving.
    sessions : Mapping[str, object]
        Open device containers, by
        :attr:`~miainwoodpecker.instrument_config.ServerConfig.name` -
        what :func:`open_configured_instrument` yields.

    Returns
    -------
    dict[str, object]
        Target name to device, ready for
        :class:`~miainwoodpecker.broker.local.LocalBroker`.

    Raises
    ------
    InstrumentConfigError
        If a server named in the configuration has no open session, or
        did not serve a device the configuration says it has.
    """
    targets: dict[str, object] = {}
    for server in config.enabled_servers():
        session = sessions.get(server.name)
        if session is None:
            message = (
                f"{_where(config)}server {server.name!r} was never opened; "
                f"open sessions are {', '.join(sorted(sessions)) or 'none'}"
            )
            raise InstrumentConfigError(message)
        served = served_devices(session)
        for device in server.enabled_devices():
            if device.served_as not in served:
                message = (
                    f"{_where(config)}{config.name} is configured with "
                    f"{device.target!r} on server {server.name!r} "
                    f"({device.description or server.module}), but that server "
                    f"serves {', '.join(sorted(served)) or 'no devices'}. Either "
                    f"the hardware did not come up, or the file is wrong about "
                    f"this instrument"
                )
                raise InstrumentConfigError(message)
            targets[device.target] = served[device.served_as]
        _log_unlisted(server, served)
    controlling = config.controlling_server()
    if controlling is not None:
        targets[INSTRUMENT_TARGET] = sessions[controlling.name].instrument
    return targets


def _log_unlisted(server: ServerConfig, served: Mapping[str, object]) -> None:
    """
    Say which of a server's devices the configuration did not ask for.

    At warning level rather than info: an unlisted device is hardware
    that exists and that no client will be able to reach this session,
    which is worth interrupting a scroll for. The message names the
    target so the fix is to paste it into the file.

    Parameters
    ----------
    server : ServerConfig
        The server being reported on.
    served : Mapping[str, object]
        What its session actually served, by served name.
    """
    listed = {device.served_as for device in server.devices}
    unlisted = sorted(set(served) - listed)
    if unlisted:
        _LOGGER.warning(
            "server %r serves %s, which this instrument's configuration does "
            "not list; not serving it. Add a [[server.device]] for it to make "
            "it reachable",
            server.name,
            ", ".join(unlisted),
        )


def _where(config: InstrumentConfig) -> str:
    """
    Return the configuration's path as a message prefix, or nothing.

    Parameters
    ----------
    config : InstrumentConfig
        The configuration.

    Returns
    -------
    str
        ``"<path>: "``, or ``""`` for one built in memory.
    """
    return f"{config.source}: " if config.source is not None else ""


@contextlib.contextmanager
def open_configured_instrument(
    config: InstrumentConfig,
) -> Iterator[dict[str, object]]:
    """
    Start every device server an instrument configuration enumerates.

    One :func:`~miainwoodpecker.devices.remote.remote_instrument` per
    enabled server, stacked on a
    :class:`contextlib.ExitStack` so that a failure to start the third
    adapter still shuts down and parks the first two. That ordering is
    the whole reason this is a context manager rather than a function
    returning handles: a half-started instrument left with the beam on
    is the failure mode worth engineering against, and the device layer
    already parks on the way out of each of these.

    Servers are started in file order, and torn down in reverse, which
    is what an ``ExitStack`` gives for free and is the right order
    anyway - the column is conventionally first, so it is parked last,
    after the detectors that were watching through it have stopped.

    Parameters
    ----------
    config : InstrumentConfig
        The instrument to open.

    Yields
    ------
    dict[str, object]
        Open device containers by server name, as
        :func:`configured_targets` takes them.

    Raises
    ------
    InstrumentConfigError
        If a server could not be started, with the server's name and the
        file in the message - a bare
        :class:`~miainwoodpecker.devices.remote.DeviceServerStartupError`
        does not say which of four adapters failed.
    """
    with contextlib.ExitStack() as stack:
        sessions: dict[str, object] = {}
        for server in config.enabled_servers():
            _LOGGER.info(
                "starting %s (%s, %s backend)%s",
                server.name,
                server.module,
                server.backend,
                f": {server.description}" if server.description else "",
            )
            try:
                sessions[server.name] = stack.enter_context(
                    remote_instrument(
                        backend=server.backend,
                        plugin_names=server.plugins,
                        server_module=server.module,
                    ),
                )
            except Exception as error:
                message = (
                    f"{_where(config)}could not start server {server.name!r} "
                    f"({server.module}, {server.backend} backend): {error}"
                )
                raise InstrumentConfigError(message) from error
        yield sessions


@contextlib.contextmanager
def serve_instrument(
    devices: object,
    *,
    host: str = "localhost",
    port: int = 0,
    authkey: bytes | None = None,
    publish_to: str | os.PathLike[str] | None = None,
) -> Iterator[BrokerInvitation]:
    """
    Serve a broker over an already-open device session.

    Parameters
    ----------
    devices : object
        An open device container, as
        :func:`~miainwoodpecker.devices.remote.remote_instrument` yields.
        Not opened here, and not closed here: whoever opened the
        instrument is who should decide when it closes.
    host : str
        Interface to bind. The default keeps the broker on this machine.
    port : int
        Port to bind, or 0 to let the OS choose - which is the usual
        case, and the reason the invitation exists.
    authkey : bytes | None
        The shared secret, or None to generate one. Generating is the
        right default: a hard-coded key on an instrument PC is a key
        everybody on site knows.
    publish_to : str | os.PathLike[str] | None
        Where to write the invitation, or None to leave publishing to
        the caller. A directory gets ``broker.json`` inside it.

    Yields
    ------
    BrokerInvitation
        Where the broker is listening, and how to authenticate.
    """
    with serve_targets(
        instrument_targets(devices),
        host=host,
        port=port,
        authkey=authkey,
        publish_to=publish_to,
    ) as invitation:
        yield invitation


@contextlib.contextmanager
def serve_targets(
    targets: Mapping[str, object],
    *,
    host: str = "localhost",
    port: int = 0,
    authkey: bytes | None = None,
    publish_to: str | os.PathLike[str] | None = None,
) -> Iterator[BrokerInvitation]:
    """
    Serve a broker over an already-built target map.

    What :func:`serve_instrument` does once it has decided what the
    targets are, split out because the configured path decides that
    differently: several device servers, renamed per
    :func:`configured_targets`, with no single container to hand over.

    Parameters
    ----------
    targets : Mapping[str, object]
        Target name to device handle. Not opened here, and not closed
        here: whoever opened the devices decides when they close.
    host : str
        Interface to bind. The default keeps the broker on this machine.
    port : int
        Port to bind, or 0 to let the OS choose.
    authkey : bytes | None
        The shared secret, or None to generate one.
    publish_to : str | os.PathLike[str] | None
        Where to write the invitation, or None to leave publishing to
        the caller. A directory gets ``broker.json`` inside it.

    Yields
    ------
    BrokerInvitation
        Where the broker is listening, and how to authenticate.
    """
    broker = LocalBroker(targets, holder="broker")
    server = serve_broker(
        broker,
        host=host,
        port=port,
        authkey=authkey if authkey is not None else secrets.token_bytes(_AUTHKEY_BYTES),
    )
    invitation = BrokerInvitation(
        host=host,
        port=server.port,
        authkey=server.authkey,
    )
    if publish_to is not None:
        written = invitation.write_to(publish_to)
        _LOGGER.info("published the broker invitation to %s", written)
    try:
        yield invitation
    finally:
        server.close()
        # After the server, so no client can take a lease on an
        # instrument that is on its way to being parked.
        broker.close()


def _wait_for_stop() -> str:
    """
    Block until asked to stop, by whichever means the operator has.

    Three of them, and the defaults only cover one. ``SIGINT`` already
    raises ``KeyboardInterrupt``, so Ctrl-C unwinds normally. ``SIGTERM``
    (a POSIX supervisor stopping a service) and ``SIGBREAK`` (Windows
    Ctrl-Break, and what ``taskkill`` without ``/F`` sends) have default
    dispositions that **terminate the interpreter without unwinding** -
    measured, not assumed: a Ctrl-Break to this process exited it with
    ``0xC000013A`` and no shutdown logged, meaning no ``park``. Handling
    them turns each into an ordinary return from this function, and the
    caller's ``with`` blocks close as they would on any other exit.

    Returns
    -------
    str
        The name of the signal that stopped it, for the log - an
        operator reading "stopped by SIGTERM" knows to look at their
        supervisor rather than at the instrument.
    """
    stop = threading.Event()
    stopped_by = "SIGINT"

    def handle(signum: int, frame: object) -> None:
        """
        Record which signal arrived and release the wait.

        Parameters
        ----------
        signum : int
            The signal number.
        frame : object
            The interrupted stack frame, unused.
        """
        del frame
        nonlocal stopped_by
        stopped_by = signal.Signals(signum).name
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue  # SIGBREAK is Windows-only; SIGTERM is not on all hosts.
        # Only the main thread may install a handler, and a host that
        # refuses one is not a reason to refuse to run - it is a reason
        # to fall back to the default disposition for that one signal.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(number, handle)
    # Waited in slices, not once. An *untimed* lock acquire on the main
    # thread is not interruptible on Windows, so a plain ``stop.wait()``
    # swallows the very signals this function just installed handlers
    # for - measured, not assumed: with the handlers in place and an
    # untimed wait, a Ctrl-Break was accepted by the C-level handler and
    # the Python-level one never ran, so the process sat there. Each
    # timed slice returns to the interpreter, which is where a pending
    # handler gets to run.
    with contextlib.suppress(KeyboardInterrupt):
        while not stop.wait(_SIGNAL_POLL_S):
            pass
    return stopped_by


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse the broker's command-line arguments.

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
        default=os.environ.get(_CONFIG_ENV_VAR) or None,
        metavar="PATH",
        help=(
            "instrument configuration enumerating this microscope's hardware "
            "and the servers that drive it; a directory gets instrument.toml "
            f"inside it. Defaults to ${_CONFIG_ENV_VAR}. Serves every adapter "
            "the file lists, and replaces --backend, --plugin and "
            "--server-module rather than combining with them."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=(SIMULATED_BACKEND, HARDWARE_BACKEND),
        default=None,
        help=(
            f"device backend (default from ${_BACKEND_ENV_VAR}, else "
            f"{SIMULATED_BACKEND} - never silently hardware). Not for use "
            "with --config, whose servers each name their own."
        ),
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=None,
        metavar="MODULE",
        help=(
            "nionswift_plugin module providing hardware devices; repeatable. "
            f"Defaults to ${_PLUGINS_ENV_VAR} (comma separated), else the "
            "device server's own autodiscovery."
        ),
    )
    parser.add_argument(
        "--server-module",
        default=None,
        metavar="MODULE",
        help=(
            f"module to launch as the device server (default "
            f"{DEFAULT_SERVER_MODULE}). Use {_CAMERA_SERVER_MODULE} for a "
            "commodity USB camera, webcam, or video file. Not for use with "
            "--config, which names a module per server."
        ),
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help=(
            "interface to bind (default localhost). Binding anywhere else "
            "puts an instrument's controls on the network; do it knowingly."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="port to bind, or 0 (the default) to let the OS choose one",
    )
    parser.add_argument(
        "--publish",
        default=None,
        metavar="PATH",
        help=(
            "write the connection details here for clients to read; a "
            "directory gets broker.json inside it. Without this the "
            "authkey is printed once and nowhere else."
        ),
    )
    arguments = parser.parse_args(argv)
    # Refused rather than given a precedence, because either precedence
    # is a wrong answer somebody has to debug at an instrument: honour
    # the file and --backend hardware silently does nothing, honour the
    # flag and one flag overrides the backend of every server in the
    # file at once. There is no reading of "--config superstem2.toml
    # --backend simulated" that is not a mistake.
    conflicting = [
        flag
        for flag, value in (
            ("--backend", arguments.backend),
            ("--plugin", arguments.plugin),
            ("--server-module", arguments.server_module),
        )
        if value is not None
    ]
    if arguments.config is not None and conflicting:
        parser.error(
            f"{' and '.join(conflicting)} cannot be combined with --config; "
            f"an instrument configuration names a module, a backend and the "
            f"plug-ins for each of its servers",
        )
    if arguments.backend is None:
        arguments.backend = os.environ.get(_BACKEND_ENV_VAR, SIMULATED_BACKEND)
    if arguments.server_module is None:
        arguments.server_module = DEFAULT_SERVER_MODULE
    return arguments


def _hardware_plugins(chosen: list[str] | None) -> tuple[str, ...]:
    """
    Resolve which vendor plug-in modules to ask the device server to load.

    Parameters
    ----------
    chosen : list[str] | None
        ``--plugin`` values, or None if none were given.

    Returns
    -------
    tuple[str, ...]
        The plug-in module names, empty for "let the server autodiscover".
    """
    if chosen:
        # The server appends to $MIAINWOODPECKER_HARDWARE_PLUGINS rather
        # than replacing it, so an explicit choice has to clear what the
        # subprocess would otherwise inherit. Same reasoning as
        # viewer/app.py, which does this for the same server.
        os.environ[_PLUGINS_ENV_VAR] = ""
        return tuple(chosen)
    return tuple(
        name.strip()
        for name in os.environ.get(_PLUGINS_ENV_VAR, "").split(",")
        if name.strip()
    )


def main(argv: list[str] | None = None) -> None:
    """
    Open a device session, serve it as a broker, and wait.

    Runs until interrupted. On the way out the instrument is parked,
    whichever way the exit came.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments, or None to read ``sys.argv``.

    Raises
    ------
    SystemExit
        If the device servers serve no frame source at all, so there is
        nothing for a client to watch or lease; or, on the configured
        path, if the configuration could not be read or the hardware it
        enumerates did not appear. Saying so beats serving an instrument
        nobody can use, and beats a traceback about a file.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    try:
        with _open_targets(args) as targets:
            _serve_and_wait(targets, args)
    except InstrumentConfigError as error:
        # The message names the file and the server; a traceback through
        # ExitStack and remote_instrument names neither, and this is a
        # command an operator runs at an instrument.
        raise SystemExit(str(error)) from error


@contextlib.contextmanager
def _open_targets(args: argparse.Namespace) -> Iterator[dict[str, object]]:
    """
    Open whichever instrument the arguments describe, and map its targets.

    The one place the two forms meet. Below this everything is a target
    map; above it, one is a command line describing a single server and
    the other is a file describing a microscope.

    Parameters
    ----------
    args : argparse.Namespace
        The parsed command line.

    Yields
    ------
    dict[str, object]
        Target name to device handle.

    Raises
    ------
    SystemExit
        If nothing that could be watched or leased came up. The two
        messages differ because the two fixes do: one is a command line
        to correct, the other is a file that says the microscope has
        hardware it did not produce.
    """
    if args.config is not None:
        config = load_instrument_config(args.config)
        _LOGGER.info("%s", config.describe())
        if not config.devices():
            message = (
                f"{config.source} enumerates no enabled devices, so a broker "
                f"over {config.name} would have nothing to lease"
            )
            raise SystemExit(message)
        with open_configured_instrument(config) as sessions:
            yield configured_targets(config, sessions)
        return
    with remote_instrument(
        backend=args.backend,
        plugin_names=_hardware_plugins(args.plugin),
        server_module=args.server_module,
    ) as microscope:
        if microscope.scanner is None and not microscope.cameras():
            message = (
                f"the device server ({args.server_module}) serves neither a "
                "scanner nor a camera, so a broker over it would have nothing "
                "to lease. Check --backend and --plugin."
            )
            raise SystemExit(message)
        yield instrument_targets(microscope)


def _serve_and_wait(
    targets: Mapping[str, object],
    args: argparse.Namespace,
) -> None:
    """
    Serve a target map and stay alive until asked to stop.

    Parameters
    ----------
    targets : Mapping[str, object]
        Target name to device handle.
    args : argparse.Namespace
        The parsed command line, for where to bind and where to publish.
    """
    with serve_targets(
        targets,
        host=args.host,
        port=args.port,
        publish_to=args.publish,
    ) as invitation:
        _LOGGER.info("%s", invitation.describe())
        _LOGGER.info("serving %s", ", ".join(targets))
        if args.publish is None:
            _LOGGER.info(
                "authkey (hex, not written anywhere): %s",
                invitation.authkey.hex(),
            )
        # Nothing to do but stay alive: the work happens on the
        # server's own threads.
        stopped_by = _wait_for_stop()
        _LOGGER.info("stopped by %s; parking the instrument", stopped_by)


if __name__ == "__main__":
    main()
