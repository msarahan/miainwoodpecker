"""
MIT-licensed client for the GPL-3.0 device server.

Implements :class:`~miainwoodpecker.devices.interface.Camera` and
:class:`~miainwoodpecker.devices.interface.Scanner` by sending
:class:`~miainwoodpecker.devices.rpc.Call` objects to a
:mod:`miainwoodpecker.devices.nion_server` subprocess and waiting for
:class:`~miainwoodpecker.devices.rpc.Result` objects back. This module
imports nothing from ``nion.*`` — that is the entire point: everything
above the device layer (acquisition, viewer, storage) can depend on this
module, and by extension on Nion hardware, without the running
application ever linking GPL-3.0 code into its own process (see
docs/migration-plan.md, §6).

Process lifecycle: the server is torn down with ``Popen.terminate()``
(SIGTERM) rather than a graceful RPC shutdown handshake. That is
sufficient here because the *process* being killed, not the interpreter
reaching normal shutdown, is what reclaims its threads and sockets — the
"un-closed camera thread hangs the process" failure mode from Phase 0
only applies to an interpreter trying to exit on its own; it does not
apply to the OS tearing the whole process down from outside. A real
hardware backend would likely need a graceful path instead, to park the
instrument safely; simulated hardware has no such requirement.
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

from miainwoodpecker.devices.rpc import Call, send_call
from miainwoodpecker.devices.shared_frame import (
    SharedFrameRef,
    read_frame_from_shared_memory,
)

if typing.TYPE_CHECKING:
    from collections.abc import Iterator
    from multiprocessing.connection import Connection

    from miainwoodpecker.devices.interface import Frame, ScanParameters

_TARGET_NAMES = ("ronchigram_camera", "eels_camera", "scanner", "instrument")
_CONNECT_TIMEOUT_S = 15.0
_TERMINATE_TIMEOUT_S = 5.0


def _free_port() -> int:
    """Return a currently-unused localhost port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("localhost", 0))
        return probe.getsockname()[1]


def _connect_with_retry(port: int, authkey: bytes, deadline: float) -> Connection:
    """Connect to a Listener that may not have started accepting yet."""
    from multiprocessing.connection import Client  # noqa: PLC0415

    while True:
        try:
            return Client(("localhost", port), authkey=authkey)
        except (ConnectionRefusedError, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


class RemoteCamera:
    """A ``Camera`` implementation that delegates over IPC to a device server."""

    def __init__(self, connection: Connection, target: str) -> None:
        self._connection = connection
        self._target = target
        self._lock = threading.Lock()

    def _call(self, method: str, *args: object, **kwargs: object) -> object:
        return send_call(
            self._connection,
            self._lock,
            Call(self._target, method, args, kwargs),
        )

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
        result = self._call("acquire_frame")
        if isinstance(result, SharedFrameRef):
            return read_frame_from_shared_memory(result)
        return typing.cast("Frame", result)

    def close(self) -> None:
        """Release the remote device."""
        self._call("close")


class RemoteScanner:
    """A ``Scanner`` implementation that delegates over IPC to a device server."""

    def __init__(self, connection: Connection, target: str) -> None:
        self._connection = connection
        self._target = target
        self._lock = threading.Lock()

    def _call(self, method: str, *args: object, **kwargs: object) -> object:
        return send_call(
            self._connection,
            self._lock,
            Call(self._target, method, args, kwargs),
        )

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
        result = self._call("scan_frame", parameters, channel)
        if isinstance(result, SharedFrameRef):
            return read_frame_from_shared_memory(result)
        return typing.cast("Frame", result)

    def close(self) -> None:
        """Release the remote device."""
        self._call("close")


@dataclass(frozen=True)
class RemoteSimulatedInstrument:
    """
    Handles to the devices of a simulated microscope, running in a subprocess.

    Same shape as the in-process
    ``nion_server.SimulatedInstrument`` (deliberately: code written
    against one works against the other unchanged).

    Attributes
    ----------
    ronchigram_camera : RemoteCamera
        The simulated Ronchigram camera.
    eels_camera : RemoteCamera
        The simulated EELS camera.
    scanner : RemoteScanner
        The simulated scan device (HAADF/MAADF channels).
    stage_size_nm : float
        The simulated stage extent, useful for choosing a sensible
        ``ScanParameters.fov_nm``.
    """

    ronchigram_camera: RemoteCamera
    eels_camera: RemoteCamera
    scanner: RemoteScanner
    stage_size_nm: float


@contextlib.contextmanager
def remote_simulated_instrument() -> Iterator[RemoteSimulatedInstrument]:
    """
    Spawn the device server subprocess and connect to it.

    Yields
    ------
    RemoteSimulatedInstrument
        Handles talking to the simulated cameras and scanner over IPC.
    """
    ports = {name: _free_port() for name in _TARGET_NAMES}
    authkey = secrets.token_bytes(32)
    # os.environ already carries PYTHONWARNINGS from shared_frame's
    # module-level setdefault (imported above), so the server subprocess
    # inherits the same resource_tracker warning suppression.
    env = {**os.environ, "MIAINWOODPECKER_AUTHKEY": authkey.hex()}
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
        [
            sys.executable,
            "-m",
            "miainwoodpecker.devices.nion_server",
            *(str(ports[name]) for name in _TARGET_NAMES),
        ],
        env=env,
    )
    connections: dict[str, Connection] = {}
    try:
        deadline = time.monotonic() + _CONNECT_TIMEOUT_S
        for name in _TARGET_NAMES:
            connections[name] = _connect_with_retry(ports[name], authkey, deadline)

        instrument_lock = threading.Lock()
        stage_size_nm = typing.cast(
            "float",
            send_call(
                connections["instrument"],
                instrument_lock,
                Call("instrument", "stage_size_nm"),
            ),
        )

        yield RemoteSimulatedInstrument(
            ronchigram_camera=RemoteCamera(
                connections["ronchigram_camera"], "ronchigram_camera",
            ),
            eels_camera=RemoteCamera(connections["eels_camera"], "eels_camera"),
            scanner=RemoteScanner(connections["scanner"], "scanner"),
            stage_size_nm=stage_size_nm,
        )
    finally:
        for connection in connections.values():
            with contextlib.suppress(Exception):
                connection.close()
        process.terminate()
        try:
            process.wait(timeout=_TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_TERMINATE_TIMEOUT_S)
