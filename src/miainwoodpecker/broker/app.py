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

The split between :func:`serve_instrument` and :func:`main` is
deliberate. Everything interesting happens in the first, which takes a
device container it did not open - so it can be exercised against fakes,
and so a caller with its own device session (a test, an embedded viewer,
a future launcher that supervises several instruments) reuses it rather
than shelling out.

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

if typing.TYPE_CHECKING:
    from collections.abc import Iterator

_LOGGER = logging.getLogger("miainwoodpecker.broker.app")

_AUTHKEY_BYTES = 32
_SIGNAL_POLL_S = 0.25
_BACKEND_ENV_VAR = "MIAINWOODPECKER_BACKEND"
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
    targets: dict[str, object] = {INSTRUMENT_TARGET: devices.instrument}
    scanner = getattr(devices, "scanner", None)
    if scanner is not None:
        targets[SCANNER_TARGET] = scanner
    targets.update(devices.cameras())
    targets.update(devices.spectrum_detectors())
    return targets


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
    broker = LocalBroker(instrument_targets(devices), holder="broker")
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
        "--backend",
        choices=(SIMULATED_BACKEND, HARDWARE_BACKEND),
        default=os.environ.get(_BACKEND_ENV_VAR, SIMULATED_BACKEND),
        help=(
            f"device backend (default from ${_BACKEND_ENV_VAR}, else "
            f"{SIMULATED_BACKEND} - never silently hardware)"
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
        default=DEFAULT_SERVER_MODULE,
        metavar="MODULE",
        help=(
            f"module to launch as the device server (default "
            f"{DEFAULT_SERVER_MODULE}). Use {_CAMERA_SERVER_MODULE} for a "
            "commodity USB camera, webcam, or video file."
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
    return parser.parse_args(argv)


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
        If the device server serves no frame source at all, so there is
        nothing for a client to watch or lease. Saying so beats serving
        an instrument nobody can use.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
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
        with serve_instrument(
            microscope,
            host=args.host,
            port=args.port,
            publish_to=args.publish,
        ) as invitation:
            _LOGGER.info("%s", invitation.describe())
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
