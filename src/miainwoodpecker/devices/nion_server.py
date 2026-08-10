"""
GPL-3.0 device server: hosts Nion's device layer in an isolated process.

License note: this module imports Nion's GPL-3.0 device stack
(``nion.device_kit``, ``nion.usim_device``) directly and in-process, which
makes *this file* GPL-3.0-encumbered. The rest of this project is MIT.
That split is safe specifically because this module is never imported by
the main application — it only ever runs as a standalone subprocess,
launched via ``python -m miainwoodpecker.devices.nion_server``, and talks
to the MIT-licensed client (:mod:`miainwoodpecker.devices.remote`) only
through the plain-data protocol in :mod:`miainwoodpecker.devices.rpc`.
Two independent programs communicating over a socket, rather than one
program importing another's internals, is the standard boundary the
GPL's copyleft does not reach across (see docs/migration-plan.md, §6).

Camera/scanner logic here (``NionCamera``, ``NionScanner``,
``simulated_instrument``) is unchanged from the in-process adapter this
module replaces (validated by ``scripts/phase0_usim_smoke_test.py`` and
by direct tests in ``tests/integration/test_nion_server.py``); what's new
is the serving loop that exposes it over :mod:`miainwoodpecker.devices.rpc`.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import sys
import threading
import typing
from dataclasses import dataclass

from nion.device_kit import ScanDevice as _ScanDeviceKit
from nion.usim_device import DeviceConfiguration as _UsimConfiguration

from miainwoodpecker.devices.interface import Frame, ScanParameters
from miainwoodpecker.devices.rpc import Call, Result
from miainwoodpecker.devices.shared_frame import write_frame_to_shared_memory

# Below this, route Frame results through the plain pickle-over-socket
# channel instead of shared memory. Measured with
# scripts/ipc_overhead_benchmark.py: shared memory's fixed per-call cost
# (shm_open/mmap/munmap/close/unlink, twice each - once per side) makes it
# *worse* than plain pickling at 2.1MB (+17.6ms vs +7.4ms overhead) and
# clearly better at 18.9-33.6MB (+53-78ms vs an extrapolated +130-170ms
# for plain pickling at that size). The exact crossover is noisier than
# that range suggests - a 1024x1024/8.4MB probe measured *more* overhead
# than the larger 1536x1536/18.9MB one, most likely cold-subprocess/paging
# variance rather than a real non-monotonic effect, since each size in
# that benchmark launches a fresh subprocess. 8MB is a deliberately
# conservative choice given that noise, not a precisely fitted number;
# revisit with a less noisy methodology (many frames per warm subprocess,
# per size) if a size in the disputed middle range turns out to matter.
_SHARED_MEMORY_THRESHOLD_BYTES = 8 * 1024 * 1024

if typing.TYPE_CHECKING:
    from collections.abc import Iterator
    from multiprocessing.connection import Listener

    from nion.device_kit.CameraDevice import Camera as _DeviceKitCamera
    from nion.device_kit.ScanDevice import Device as _DeviceKitScanDevice

_AUTHKEY_ENV_VAR = "MIAINWOODPECKER_AUTHKEY"
_TARGET_NAMES = ("ronchigram_camera", "eels_camera", "scanner", "instrument")


def _aware_utc(timestamp: datetime.datetime | None) -> datetime.datetime:
    """Return a timezone-aware UTC timestamp; Nion reports naive UTC datetimes."""
    if timestamp is None:
        return datetime.datetime.now(tz=datetime.UTC)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=datetime.UTC)
    return timestamp


class NionCamera:
    """A ``Camera`` implementation wrapping a Nion device-kit camera device."""

    def __init__(self, camera_device: _DeviceKitCamera) -> None:
        self._device = camera_device

    @property
    def camera_id(self) -> str:
        """Return the wrapped device's camera id."""
        return self._device.camera_id

    def start(self) -> None:
        """Begin continuous acquisition; blocks until the first frame is available."""
        self._device.start_live()

    def stop(self) -> None:
        """Pause continuous acquisition."""
        self._device.stop_live()

    def acquire_frame(self) -> Frame:
        """Return the next available frame; requires ``start`` to have been called."""
        data_element = self._device.acquire_image()
        return Frame(
            data=data_element["data"],
            timestamp=_aware_utc(data_element.get("timestamp")),
            metadata=dict(data_element.get("properties") or {}),
        )

    def close(self) -> None:
        """Release the device and join its acquisition thread."""
        self._device.close()


class NionScanner:
    """A ``Scanner`` implementation wrapping a Nion device-kit scan device."""

    def __init__(self, scan_device: _DeviceKitScanDevice) -> None:
        self._device = scan_device

    @property
    def scanner_id(self) -> str:
        """Return the wrapped device's scan device id."""
        return self._device.scan_device_id

    @property
    def channel_names(self) -> typing.Sequence[str]:
        """Return the detector channel names, indexed by channel number."""
        return [
            self._device.get_channel_name(index)
            for index in range(self._device.channel_count)
        ]

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Scan and return a single frame from the given detector channel."""
        frame_parameters = _ScanDeviceKit.ScanFrameParameters(
            pixel_size=(parameters.height, parameters.width),
            pixel_time_us=parameters.pixel_time_us,
            fov_nm=parameters.fov_nm,
        )
        data = self._device.get_scan_data(frame_parameters, channel)
        return Frame(
            data=data,
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={
                "channel_index": channel,
                "channel_name": self._device.get_channel_name(channel),
                "fov_nm": parameters.fov_nm,
                "pixel_time_us": parameters.pixel_time_us,
            },
        )

    def close(self) -> None:
        """Release the device."""
        self._device.close()


class InstrumentInfo:
    """Small RPC target exposing instrument-level facts that aren't a device."""

    def __init__(self, stage_size_nm: float) -> None:
        self._stage_size_nm = stage_size_nm

    def stage_size_nm(self) -> float:
        """Return the simulated stage extent, in nanometres."""
        return self._stage_size_nm


@dataclass(frozen=True)
class SimulatedInstrument:
    """
    Handles to the devices of a simulated STEM microscope.

    Attributes
    ----------
    ronchigram_camera : NionCamera
        The simulated Ronchigram camera.
    eels_camera : NionCamera
        The simulated EELS camera.
    scanner : NionScanner
        The simulated scan device (HAADF/MAADF channels).
    stage_size_nm : float
        The simulated stage extent, useful for choosing a sensible
        ``ScanParameters.fov_nm``.
    """

    ronchigram_camera: NionCamera
    eels_camera: NionCamera
    scanner: NionScanner
    stage_size_nm: float


@contextlib.contextmanager
def simulated_instrument() -> Iterator[SimulatedInstrument]:
    """
    Build the nionswift-usim simulated microscope and guarantee clean teardown.

    usim starts a background acquisition thread per camera at construction
    time, and it constructs *both* cameras up front; every camera must be
    closed on the way out or the process hangs at exit waiting for the
    un-closed thread to join. This context manager owns that lifecycle.

    Yields
    ------
    SimulatedInstrument
        Adapted handles to the simulated cameras and scanner.
    """
    configuration = _UsimConfiguration.AcquisitionContextConfiguration(
        set_configuration_location=False,
    )
    ronchigram_camera = NionCamera(configuration.ronchigram_camera_device)
    eels_camera = NionCamera(configuration.eels_camera_device)
    scanner = NionScanner(configuration.scan_module.device)
    try:
        yield SimulatedInstrument(
            ronchigram_camera=ronchigram_camera,
            eels_camera=eels_camera,
            scanner=scanner,
            stage_size_nm=configuration.instrument.stage_size_nm,
        )
    finally:
        ronchigram_camera.close()
        eels_camera.close()
        scanner.close()


def _serve_connection(connection: object, target: object) -> None:
    """Handle Calls on one accepted connection until the client disconnects."""
    try:
        while True:
            try:
                call: Call = connection.recv()
            except EOFError:
                return
            if not hasattr(target, call.method):
                connection.send(
                    Result(error=f"unknown method {call.method!r} on {call.target!r}"),
                )
                continue
            try:
                # getattr already evaluates properties (camera_id, scanner_id,
                # channel_names): call the result only when it's a bound
                # method, or a property's value gets invoked as a function.
                attribute = getattr(target, call.method)
                value = (
                    attribute(*call.args, **call.kwargs)
                    if callable(attribute)
                    else attribute
                )
                # Only large Frame arrays are worth routing around the
                # pickle-over-socket channel; every other return value here
                # is small (a string, a list of names, None), and shared
                # memory's fixed per-call cost makes it a net loss below
                # the measured threshold - see its definition above.
                if (
                    isinstance(value, Frame)
                    and value.data.nbytes >= _SHARED_MEMORY_THRESHOLD_BYTES
                ):
                    value = write_frame_to_shared_memory(value)
            except Exception as exc:  # noqa: BLE001 - reported to the client, not raised here
                connection.send(Result(error=f"{type(exc).__name__}: {exc}"))
            else:
                connection.send(Result(value=value))
    finally:
        connection.close()


def _accept_loop(listener: Listener, target: object) -> None:
    """Accept connections for one target, one handler thread per connection."""
    while True:
        try:
            connection = listener.accept()
        except OSError:
            return  # listener.close() from elsewhere unblocks accept() this way.
        thread = threading.Thread(
            target=_serve_connection,
            args=(connection, target),
            daemon=True,
        )
        thread.start()


def serve(ports: typing.Mapping[str, int], authkey: bytes) -> None:
    """
    Build the simulated instrument and serve it, one Listener per target.

    Blocks forever (or until the process is killed by its parent, which
    is how :mod:`miainwoodpecker.devices.remote` tears this down — see
    its docstring for why that is sufficient here).

    Parameters
    ----------
    ports : typing.Mapping[str, int]
        Port number for each of ``"ronchigram_camera"``, ``"eels_camera"``,
        ``"scanner"``, and ``"instrument"``.
    authkey : bytes
        Shared secret authenticating connections to every Listener.
    """
    from multiprocessing.connection import Listener as _Listener  # noqa: PLC0415

    with simulated_instrument() as instrument:
        targets: dict[str, object] = {
            "ronchigram_camera": instrument.ronchigram_camera,
            "eels_camera": instrument.eels_camera,
            "scanner": instrument.scanner,
            "instrument": InstrumentInfo(instrument.stage_size_nm),
        }
        listeners = [
            _Listener(("localhost", ports[name]), authkey=authkey)
            for name in _TARGET_NAMES
        ]
        threads = [
            threading.Thread(
                target=_accept_loop,
                args=(listener, targets[name]),
                daemon=True,
            )
            for listener, name in zip(listeners, _TARGET_NAMES, strict=True)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()


def main() -> None:
    """
    Parse ports from argv and the authkey from the environment, then serve.

    Invoked as ``python -m miainwoodpecker.devices.nion_server PORT PORT
    PORT PORT`` (one port per name in ``_TARGET_NAMES``, in that order) by
    :mod:`miainwoodpecker.devices.remote`, which also sets
    ``MIAINWOODPECKER_AUTHKEY``.
    """
    ports = dict(zip(_TARGET_NAMES, (int(arg) for arg in sys.argv[1:]), strict=True))
    authkey = bytes.fromhex(os.environ[_AUTHKEY_ENV_VAR])
    serve(ports, authkey)


if __name__ == "__main__":
    main()
