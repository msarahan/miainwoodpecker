"""
MIT-licensed client for the GPL-3.0 device server.

Implements :class:`~miainwoodpecker.devices.interface.Camera`,
:class:`~miainwoodpecker.devices.interface.Scanner` and
:class:`~miainwoodpecker.devices.interface.InstrumentController` by
sending :class:`~miainwoodpecker.devices.rpc.Call` objects to a
:mod:`miainwoodpecker.devices.nion_server` subprocess and waiting for
:class:`~miainwoodpecker.devices.rpc.Result` objects back. This module
imports nothing from ``nion.*`` — that is the entire point: everything
above the device layer (acquisition, viewer, storage) can depend on this
module, and by extension on Nion hardware, without the running
application ever linking GPL-3.0 code into its own process (see
docs/migration-plan.md, §6).

``remote_instrument(backend=...)`` chooses which devices the server
builds; ``remote_simulated_instrument()`` is the unchanged
nionswift-usim entry point, kept as a thin delegation because the viewer
and the benchmark scripts are written against it.

Process lifecycle — the interesting part, and load-bearing rather than
polite. A named POSIX shared-memory segment is *not* reclaimed when the
process that created it dies (unlike its threads, sockets, or anonymous
memory): it is a persistent tmpfs entry until explicitly ``unlink()``-ed,
so the server must be given the chance to retire the reused segments
:mod:`miainwoodpecker.devices.shared_frame` hands out. Teardown therefore
goes, in order:

1. Ask the server to shut down (``instrument.shutdown()``). It stops the
   cameras, parks the instrument (blanks the beam if a blanker exists),
   closes every device — which unlinks that device's segment — and
   acknowledges with a report of what it did. Only then does it exit.
2. Wait a bounded time for the process to exit on its own.
3. If either step times out or errors, fall back to closing each device
   individually (the pre-handshake behaviour, which unlinks the same
   segments over a different connection) and then ``Popen.terminate()``,
   escalating to ``kill()``. A hung server must never hang the
   application, and it must not be allowed to leak segments either.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import typing
from dataclasses import dataclass

from miainwoodpecker.devices.rpc import Call, disable_nagle, send_call
from miainwoodpecker.devices.shared_frame import SharedFrameReader, SharedFrameRef

if typing.TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from multiprocessing.connection import Connection

    from miainwoodpecker.devices.interface import Frame, ScanParameters

_TARGET_NAMES = ("ronchigram_camera", "eels_camera", "scanner", "instrument")
_CONNECT_TIMEOUT_S = 15.0
_TERMINATE_TIMEOUT_S = 5.0
# How long to wait for the graceful-shutdown acknowledgement. Generous
# because the server is stopping detectors and blanking the beam, which on
# real hardware is a physical operation, not a flag flip - but bounded,
# because the whole point of the handshake is to not hang the application.
_SHUTDOWN_TIMEOUT_S = 10.0

SIMULATED_BACKEND = "simulated"
HARDWARE_BACKEND = "hardware"


class DeviceServerStartupError(RuntimeError):
    """Raised when the device server process died before it served anything."""


def _free_port() -> int:
    """Return a currently-unused localhost port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("localhost", 0))
        return probe.getsockname()[1]


def _connect_with_retry(
    port: int,
    authkey: bytes,
    deadline: float,
    process: subprocess.Popen[bytes],
) -> Connection:
    """
    Connect to a Listener that may not have started accepting yet.

    Checks the server process on every attempt rather than only the clock.
    Without that, the most likely real-world failure — asking for the
    hardware backend on a machine with no instrument attached, where the
    server prints what it looked for and exits — would show up here as a
    silent 15-second wait ending in ``ConnectionRefusedError``, throwing
    away the one diagnostic that matters.

    Parameters
    ----------
    port : int
        Port the target's Listener was told to bind.
    authkey : bytes
        Shared secret for the connection.
    deadline : float
        ``time.monotonic()`` value after which to give up.
    process : subprocess.Popen[bytes]
        The server process, watched for early exit.

    Returns
    -------
    Connection
        The connected client end, with Nagle disabled.

    Raises
    ------
    DeviceServerStartupError
        If the server exited before accepting connections.
    ConnectionRefusedError
        If ``deadline`` passed with the server still alive but not listening.
    OSError
        For any other socket-level failure past the deadline.
    """
    from multiprocessing.connection import Client  # noqa: PLC0415

    while True:
        if process.poll() is not None:
            msg = (
                f"the device server exited with status {process.returncode} "
                f"before accepting connections. Its own diagnostic went to "
                f"stderr (inherited from this process): a hardware backend "
                f"with no instrument present reports which nion Registry "
                f"components it looked for, which plug-ins it loaded or "
                f"skipped, and how to name the right one."
            )
            raise DeviceServerStartupError(msg)
        try:
            connection = Client(("localhost", port), authkey=authkey)
        except (ConnectionRefusedError, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)
        else:
            disable_nagle(connection)
            return connection


class _RemoteDevice:
    """
    Shared plumbing for a device driven over one RPC connection.

    Holds the connection, the lock serializing round trips on it, and the
    shared-memory reader that large frames arrive through.
    """

    def __init__(self, connection: Connection, target: str) -> None:
        self._connection = connection
        self._target = target
        self._lock = threading.Lock()
        self._reader = SharedFrameReader()

    def _call(self, method: str, *args: object, **kwargs: object) -> object:
        """Send one call to this device's target and return its value."""
        return send_call(
            self._connection,
            self._lock,
            Call(self._target, method, args, kwargs),
        )

    def _frame(self, method: str, *args: object) -> Frame:
        """Make a call that returns a frame, following a shared-memory reference."""
        result = self._call(method, *args)
        if isinstance(result, SharedFrameRef):
            return self._reader.read(result)
        return typing.cast("Frame", result)

    def detach(self) -> None:
        """
        Detach from the server's shared segment without unlinking it.

        Used on the graceful-shutdown path, where the server has already
        retired the segment itself and there is no device left to call
        ``close`` on.
        """
        self._reader.close()

    def close(self) -> None:
        """Release the remote device and detach from its shared segment."""
        self._call("close")
        self._reader.close()


class RemoteCamera(_RemoteDevice):
    """A ``Camera`` implementation that delegates over IPC to a device server."""

    @property
    def camera_id(self) -> str:
        """Return the remote device's camera id."""
        return typing.cast("str", self._call("camera_id"))

    def start(self) -> None:
        """Begin continuous acquisition on the remote device."""
        self._call("start")

    def stop(self) -> None:
        """Pause continuous acquisition on the remote device."""
        self._call("stop")

    def acquire_frame(self) -> Frame:
        """Return the next available frame from the remote device."""
        return self._frame("acquire_frame")


class RemoteScanner(_RemoteDevice):
    """A ``Scanner`` implementation that delegates over IPC to a device server."""

    @property
    def scanner_id(self) -> str:
        """Return the remote device's scan device id."""
        return typing.cast("str", self._call("scanner_id"))

    @property
    def channel_names(self) -> typing.Sequence[str]:
        """Return the remote device's detector channel names."""
        return typing.cast("typing.Sequence[str]", self._call("channel_names"))

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Scan and return a single frame from the remote device."""
        return self._frame("scan_frame", parameters, channel)


class RemoteInstrument:
    """
    An ``InstrumentController`` that delegates over IPC to a device server.

    Also carries the two calls that are about the *server* rather than the
    instrument — :meth:`describe` and :meth:`shutdown` — because the
    server serves both on the same ``instrument`` target.
    """

    def __init__(self, connection: Connection, target: str = "instrument") -> None:
        self._connection = connection
        self._target = target
        self._lock = threading.Lock()

    def _call(
        self,
        method: str,
        *args: object,
        timeout_s: float | None = None,
        **kwargs: object,
    ) -> object:
        """Send one call to the instrument target and return its value."""
        return send_call(
            self._connection,
            self._lock,
            Call(self._target, method, args, kwargs),
            timeout_s=timeout_s,
        )

    def describe(self) -> dict[str, object]:
        """Return the server's report of its backend, targets, and controls."""
        return typing.cast("dict[str, object]", self._call("describe"))

    def stage_size_nm(self) -> float:
        """Return the stage extent, in nanometres."""
        return typing.cast("float", self._call("stage_size_nm"))

    def available_controls(self) -> Sequence[str]:
        """Return the neutral control names the remote instrument implements."""
        return typing.cast("Sequence[str]", self._call("available_controls"))

    def stage_position_nm(self) -> tuple[float, float]:
        """Return the stage position as ``(y, x)``, in nanometres."""
        position = typing.cast(
            "tuple[float, float]",
            self._call("stage_position_nm"),
        )
        return (float(position[0]), float(position[1]))

    def set_stage_position_nm(self, y_nm: float, x_nm: float) -> None:
        """
        Move the stage to an absolute ``(y, x)`` position, in nanometres.

        Parameters
        ----------
        y_nm : float
            Target position along the slow scan axis, in nanometres.
        x_nm : float
            Target position along the fast scan axis, in nanometres.
        """
        self._call("set_stage_position_nm", y_nm, x_nm)

    def defocus_nm(self) -> float:
        """Return the defocus, in nanometres."""
        return typing.cast("float", self._call("defocus_nm"))

    def set_defocus_nm(self, defocus_nm: float) -> None:
        """
        Set the defocus, in nanometres.

        Parameters
        ----------
        defocus_nm : float
            Target defocus, in nanometres.
        """
        self._call("set_defocus_nm", defocus_nm)

    def is_beam_blanked(self) -> bool:
        """Return whether the beam is currently blanked."""
        return bool(self._call("is_beam_blanked"))

    def set_beam_blanked(self, *, blanked: bool) -> None:
        """
        Blank or unblank the beam.

        Parameters
        ----------
        blanked : bool
            ``True`` to blank the beam, ``False`` to unblank it.
        """
        self._call("set_beam_blanked", blanked=blanked)

    def park(self) -> None:
        """Put the instrument in a safe state (blank the beam, if it can)."""
        self._call("park")

    def shutdown(self) -> dict[str, object]:
        """
        Ask the server to park, release its devices, and exit; return its report.

        Returns
        -------
        dict[str, object]
            The server's park report: what was stopped, whether the beam
            ended up blanked, which devices were released, and any errors
            it hit while doing so.
        """
        return typing.cast(
            "dict[str, object]",
            self._call("shutdown", timeout_s=_SHUTDOWN_TIMEOUT_S),
        )


@dataclass(frozen=True)
class RemoteInstrumentDevices:
    """
    Handles to the devices of one microscope, running in a server subprocess.

    Same shape as the in-process ``nion_server.InstrumentDevices``
    (deliberately: code written against one works against the other
    unchanged).

    Attributes
    ----------
    ronchigram_camera : RemoteCamera | None
        The Ronchigram camera, if this instrument has one.
    eels_camera : RemoteCamera | None
        The EELS camera, if this instrument has one.
    scanner : RemoteScanner
        The scan device (HAADF/MAADF channels on the simulator).
    instrument : RemoteInstrument
        Stage/defocus/blanker controls.
    stage_size_nm : float
        The stage extent, useful for choosing a sensible
        ``ScanParameters.fov_nm``.
    """

    ronchigram_camera: RemoteCamera | None
    eels_camera: RemoteCamera | None
    scanner: RemoteScanner
    instrument: RemoteInstrument
    stage_size_nm: float


# Historical name, kept because the migration plan and README refer to it.
RemoteSimulatedInstrument = RemoteInstrumentDevices


def _spawn_server(
    ports: typing.Mapping[str, int],
    authkey: bytes,
    backend: str,
    plugin_names: Sequence[str],
) -> subprocess.Popen[bytes]:
    """
    Launch the device server subprocess.

    Parameters
    ----------
    ports : typing.Mapping[str, int]
        Port to bind for each target name.
    authkey : bytes
        Shared secret for every Listener.
    backend : str
        ``"simulated"`` or ``"hardware"``.
    plugin_names : Sequence[str]
        ``nionswift_plugin`` modules for the hardware backend.

    Returns
    -------
    subprocess.Popen[bytes]
        The running server process.
    """
    # os.environ already carries PYTHONWARNINGS from shared_frame's
    # module-level setdefault (imported above), so the server subprocess
    # inherits the same resource_tracker warning suppression.
    env = {**os.environ, "MIAINWOODPECKER_AUTHKEY": authkey.hex()}
    plugin_arguments = [
        argument for name in plugin_names for argument in ("--plugin", name)
    ]
    return subprocess.Popen(  # noqa: S603 - fixed argv shape, no shell
        [
            sys.executable,
            "-m",
            "miainwoodpecker.devices.nion_server",
            "--backend",
            backend,
            *plugin_arguments,
            *(str(ports[name]) for name in _TARGET_NAMES),
        ],
        env=env,
    )


def _shut_down_server(
    instrument: RemoteInstrument,
    devices: Sequence[_RemoteDevice],
    process: subprocess.Popen[bytes],
) -> None:
    """
    Tear the server down gracefully if it answers, by force if it does not.

    Parameters
    ----------
    instrument : RemoteInstrument
        The instrument target carrying the shutdown handshake.
    devices : Sequence[_RemoteDevice]
        Every device handle, for the fallback path's per-device ``close()``
        (which is what unlinks the shared-memory segments when the
        handshake did not get that far).
    process : subprocess.Popen[bytes]
        The server process.
    """
    if process.poll() is not None:
        # A caller already ran the handshake explicitly and the server has
        # exited: it has parked itself and retired its own segments, so
        # there is nothing left to ask and nothing left to kill.
        for device in devices:
            with contextlib.suppress(Exception):
                device.detach()
        return
    graceful = False
    try:
        report = instrument.shutdown()
    except Exception:  # noqa: BLE001, S110 - any failure means "fall back", not "raise"
        pass
    else:
        # A report listing errors means the server got *part* way through
        # releasing devices. Treat that as ungraceful so the per-device
        # fallback below gets a second attempt at the shared-memory
        # segments, which are the one resource killing the process cannot
        # reclaim.
        graceful = not report.get("errors")
        for device in devices:
            with contextlib.suppress(Exception):
                device.detach()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_TERMINATE_TIMEOUT_S)
    if not graceful or process.poll() is None:
        for device in devices:
            with contextlib.suppress(Exception):
                device.close()
        process.terminate()
        try:
            process.wait(timeout=_TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_TERMINATE_TIMEOUT_S)


@contextlib.contextmanager
def remote_instrument(
    backend: str = SIMULATED_BACKEND,
    plugin_names: Sequence[str] = (),
) -> Iterator[RemoteInstrumentDevices]:
    """
    Spawn the device server subprocess for a backend and connect to it.

    The ``instrument`` target is connected first and asked to
    ``describe()`` itself, so the client only connects to the device
    targets the instrument actually has. usim always has both cameras; a
    real instrument need not.

    Parameters
    ----------
    backend : str
        ``"simulated"`` for nionswift-usim, ``"hardware"`` for real
        devices. Anything else is rejected by the server, which reports
        the error back before this function returns.
    plugin_names : Sequence[str]
        ``nionswift_plugin`` modules providing hardware devices; ignored
        by the simulated backend.

    Yields
    ------
    RemoteInstrumentDevices
        Handles talking to the cameras, scanner, and instrument over IPC.
    """
    ports = {name: _free_port() for name in _TARGET_NAMES}
    authkey = secrets.token_bytes(32)
    process = _spawn_server(ports, authkey, backend, plugin_names)
    connections: dict[str, Connection] = {}
    try:
        deadline = time.monotonic() + _CONNECT_TIMEOUT_S
        connections["instrument"] = _connect_with_retry(
            ports["instrument"], authkey, deadline, process,
        )
        instrument = RemoteInstrument(connections["instrument"])
        description = instrument.describe()
        served = typing.cast("Sequence[str]", description["targets"])
        for name in served:
            connections[name] = _connect_with_retry(
                ports[name], authkey, deadline, process,
            )

        cameras = {
            name: RemoteCamera(connections[name], name)
            for name in ("ronchigram_camera", "eels_camera")
            if name in connections
        }
        scanner = RemoteScanner(connections["scanner"], "scanner")
        devices: list[_RemoteDevice] = [*cameras.values(), scanner]
        try:
            yield RemoteInstrumentDevices(
                ronchigram_camera=cameras.get("ronchigram_camera"),
                eels_camera=cameras.get("eels_camera"),
                scanner=scanner,
                instrument=instrument,
                stage_size_nm=float(
                    typing.cast("float", description["stage_size_nm"]),
                ),
            )
        finally:
            _shut_down_server(instrument, devices, process)
    finally:
        for connection in connections.values():
            with contextlib.suppress(Exception):
                connection.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_TERMINATE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_TERMINATE_TIMEOUT_S)


@contextlib.contextmanager
def remote_simulated_instrument() -> Iterator[RemoteInstrumentDevices]:
    """
    Spawn the device server subprocess for nionswift-usim and connect to it.

    Yields
    ------
    RemoteInstrumentDevices
        Handles talking to the simulated cameras and scanner over IPC.
    """
    with remote_instrument(SIMULATED_BACKEND) as devices:
        yield devices
