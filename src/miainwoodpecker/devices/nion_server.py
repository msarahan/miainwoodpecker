"""
GPL-3.0 device server: hosts Nion's device layer in an isolated process.

License note: this module imports Nion's GPL-3.0 device stack
(``nion.device_kit``, ``nion.usim_device``, ``nion.utils.Registry``)
directly and in-process, which makes *this file* GPL-3.0-encumbered. The
rest of this project is MIT. That split is safe specifically because this
module is never imported by the main application — it only ever runs as a
standalone subprocess, launched via
``python -m miainwoodpecker.devices.nion_server``, and talks to the
MIT-licensed client (:mod:`miainwoodpecker.devices.remote`) only through
the plain-data protocol in :mod:`miainwoodpecker.devices.rpc`. Two
independent programs communicating over a socket, rather than one program
importing another's internals, is the standard boundary the GPL's
copyleft does not reach across (see docs/migration-plan.md, §6).

Camera/scanner logic here (``NionCamera``, ``NionScanner``,
``simulated_instrument``) is unchanged from the in-process adapter this
module replaces (validated by ``scripts/phase0_usim_smoke_test.py`` and
by direct tests in ``tests/integration/test_nion_server.py``).

Three things layered on top of that, all Phase 1-hardware/Phase 3 work:

**Backend selection.** ``serve()`` no longer hard-constructs the
simulator. ``open_instrument(backend)`` chooses between
``simulated_instrument()`` (nionswift-usim, unchanged) and
``hardware_instrument()``, which discovers real devices the way Nion Swift
itself does: importing installed ``nionswift_plugin.*`` packages, calling
each module's ``run()``, and reading the ``nion.utils.Registry``
components (``stem_controller``, ``scan_module``, ``camera_module``) those
plug-ins register. That machinery is exercised in tests by pointing it at
the usim plug-in as a stand-in vendor plug-in; what stays untested until
hardware day is only which plug-in package a real instrument ships.

**Instrument controls.** ``NionInstrument`` exposes stage position,
defocus, and beam blanker through
:class:`~miainwoodpecker.devices.interface.InstrumentController`. It
drives them through the *named-control* API on
``nion.instrumentation.stem_controller.STEMController``
(``does_control_exist``/``get_control_output``/``set_control_output``, and
``GetVal2D``/``SetVal2D`` for the 2D stage) rather than the
``defocus_m``/``stage_position_m`` convenience properties, because the
named-control API is what the base ``STEMController`` class itself
defines, so any vendor controller implements it; the properties happen to
exist on Nion's own ``device_kit`` reference implementation.

**Graceful shutdown.** A ``shutdown`` call on the ``instrument`` target
stops detectors, parks the instrument, releases devices (which retires
their shared-memory segments), acknowledges, and only then lets the
process exit. SIGTERM remains the client's fallback for a wedged server.

**A health check, alongside it on the same target.** ``health`` answers
from server-process state alone — pid, uptime, backend, which targets are
served, which devices are still open — and deliberately touches no vendor
object and no device. That is what makes it both cheap enough to poll
routinely and unable to perturb an acquisition in flight: it takes no
device lock, and it is served on its own connection's handler thread, so
a blocked ``acquire_frame`` on the camera target does not delay it. The
consequence the client relies on is the useful one: if ``health`` does
not answer within its (bounded) timeout, the server really is wedged, not
merely busy. See :mod:`miainwoodpecker.devices.remote` for how the client
turns that into a three-way verdict.

**Logging.** Structured stdlib logging, off unless asked for, because
this process's stderr is inherited by whoever launched it and its output
would otherwise interleave anonymously with the application's. Startup
and backend selection, plug-in load notes, per-target connection accepts,
per-call failures (naming the target and method), and shutdown are all
logged; nothing on the frame path is, so the shared-memory publish/read
hot path pays nothing. ``MIAINWOODPECKER_DEVICE_LOG_LEVEL`` sets the
level (default ``WARNING``, i.e. quiet) and
``MIAINWOODPECKER_DEVICE_LOG_FILE`` redirects it out of the shared
terminal entirely. Configured only in :func:`main`, so importing this
module in-process (as the tests do) leaves logging inert.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import importlib
import logging
import os
import pkgutil
import sys
import threading
import time
import typing
from dataclasses import dataclass

from nion.device_kit import ScanDevice as _ScanDeviceKit
from nion.usim_device import DeviceConfiguration as _UsimConfiguration
from nion.utils import Geometry as _Geometry
from nion.utils import Registry as _Registry

from miainwoodpecker.devices.interface import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    STAGE_POSITION_CONTROL,
    Frame,
    ScanParameters,
)
from miainwoodpecker.devices.rpc import Call, Result, disable_nagle
from miainwoodpecker.devices.shared_frame import SharedFrameWriter

# Below this, route Frame results through the plain pickle-over-socket
# channel instead of shared memory. Measured with
# scripts/ipc_overhead_benchmark.py against the *reused-segment* writer
# (shared_frame.py) with Nagle disabled on the RPC connections
# (rpc.disable_nagle - a real, separate bug this benchmark surfaced: two
# sizes on the plain-pickle path showed a strikingly consistent ~44ms
# stall, the signature of Nagle's algorithm and the receiver's delayed ACK
# waiting on each other; TCP_NODELAY was unset by default and is now set
# on every connection this project opens, not just those two sizes).
#
# With reuse, per-call cost above a first-use/resize is just the memcpy,
# so pickle and shared memory are within noise of each other from ~30KB
# up to ~500KB (all +0.3 to +0.5ms overhead over direct in-process calls)
# and shared memory pulls ahead smoothly above that: +1.5ms at ~1MB,
# +2.8ms at 2.1MB, +9.5ms at 8.4MB, +13.3ms at 18.9MB, +25ms at 33.6MB
# (versus a naive per-frame-create/destroy design's +72ms, and naive
# pickle's +168ms, at that largest size). 64KB sits comfortably in the
# "doesn't matter much either way" band measured above, so it is kept
# rather than tuned further.
_SHARED_MEMORY_THRESHOLD_BYTES = 64 * 1024

if typing.TYPE_CHECKING:
    import types
    from collections.abc import Iterator, Sequence
    from multiprocessing.connection import Listener

    from nion.device_kit.CameraDevice import Camera as _DeviceKitCamera
    from nion.device_kit.ScanDevice import Device as _DeviceKitScanDevice

_AUTHKEY_ENV_VAR = "MIAINWOODPECKER_AUTHKEY"
_BACKEND_ENV_VAR = "MIAINWOODPECKER_BACKEND"
_PLUGINS_ENV_VAR = "MIAINWOODPECKER_HARDWARE_PLUGINS"
_LOG_LEVEL_ENV_VAR = "MIAINWOODPECKER_DEVICE_LOG_LEVEL"
_LOG_FILE_ENV_VAR = "MIAINWOODPECKER_DEVICE_LOG_FILE"
# Test-only hooks, honoured by the shutdown and health handlers so the
# client's timeout paths can be exercised against a genuinely unresponsive
# server rather than a mocked-out one. Never set in normal operation; see
# tests/integration/test_remote_nion.py.
_WEDGE_SHUTDOWN_ENV_VAR = "MIAINWOODPECKER_WEDGE_SHUTDOWN"
_WEDGE_HEALTH_ENV_VAR = "MIAINWOODPECKER_WEDGE_HEALTH"
_DELAY_SCAN_ENV_VAR = "MIAINWOODPECKER_DELAY_SCAN_S"

# Named rather than taken from ``__name__``: this module's usual entry
# point is ``python -m miainwoodpecker.devices.nion_server``, under which
# ``__name__`` is ``"__main__"`` — a logger name that tells an operator
# reading interleaved stderr nothing about which program emitted the line,
# which is the whole problem this logging exists to solve.
_LOGGER = logging.getLogger("miainwoodpecker.devices.nion_server")
_DEFAULT_LOG_LEVEL = "WARNING"

_TARGET_NAMES = ("ronchigram_camera", "eels_camera", "scanner", "instrument")
_DEVICE_TARGET_NAMES = ("ronchigram_camera", "eels_camera", "scanner")

SIMULATED_BACKEND = "simulated"
HARDWARE_BACKEND = "hardware"
BACKENDS = (SIMULATED_BACKEND, HARDWARE_BACKEND)

# Exit status for "the hardware backend found no instrument". Distinct from
# 1 so a launcher can tell a missing microscope from a crash.
NO_HARDWARE_EXIT_STATUS = 2

# Vendor control names behind the neutral controls in interface.py.
# "C10" (defocus) and "stage_position_m" come from Nion's own device_kit
# reference implementation, which reads/writes exactly these names in its
# defocus_m/stage_position_m properties. "C_Blank" is Nion's documented
# blanker control: nion.instrumentation.AcquisitionPreferences declares
# ControlDescription("blanker", ..., "C_Blank", "bool", ...) and
# MultiAcquire defaults blanker="C_Blank". All three are therefore Nion's
# names rather than ours - but they are only *verified* against the
# simulator here; see docs/migration-plan.md's hardware-day checklist.
_DEFOCUS_CONTROL_NAME = "C10"
_BLANKER_CONTROL_NAME = "C_Blank"
_STAGE_POSITION_CONTROL_NAME = "stage_position_m"

_NM_PER_M = 1e9
# Only a geometry hint for choosing a field of view. Nion's device_kit
# instrument publishes stage_size_nm; a vendor controller need not, so a
# 1µm stand-in is used rather than failing the whole connection over a
# convenience value.
_FALLBACK_STAGE_SIZE_NM = 1000.0

_PLUGIN_NAMESPACE = "nionswift_plugin"
# Plug-in modules the hardware backend never auto-loads:
#   usim  - the simulator. Loading it under --backend=hardware would let
#           "hardware" silently mean "simulator", the one failure mode
#           this selector exists to prevent.
#   none  - an empty placeholder module shipped by the nion stack.
#   DM_IO/TIFF_IO       - file-format readers, no devices.
#   nion_instrumentation_ui - Swift's own acquisition UI panels.
# Any of them can still be loaded deliberately by name (--plugin), which
# is how the tests drive the real discovery path using usim as a stand-in
# vendor plug-in.
_SKIPPED_PLUGIN_MODULES = frozenset(
    {"usim", "none", "DM_IO", "TIFF_IO", "nion_instrumentation_ui"},
)


class HardwareNotAvailableError(RuntimeError):
    """Raised when the hardware backend finds no real instrument to drive."""


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
        self._closed = False

    @property
    def is_closed(self) -> bool:
        """Return whether this device has been released."""
        return self._closed

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
        """
        Release the device and join its acquisition thread.

        Idempotent: the shutdown handshake closes devices, and the
        owning context manager closes them again on its way out.
        """
        if self._closed:
            return
        self._closed = True
        self._device.close()


class NionScanner:
    """A ``Scanner`` implementation wrapping a Nion device-kit scan device."""

    def __init__(self, scan_device: _DeviceKitScanDevice) -> None:
        self._device = scan_device
        self._closed = False

    @property
    def is_closed(self) -> bool:
        """Return whether this device has been released."""
        return self._closed

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
        delay_s = os.environ.get(_DELAY_SCAN_ENV_VAR)
        if delay_s:
            # Test hook, in the same spirit as the wedge hooks below: hold a
            # scan open for a known duration so a test can kill the server
            # while a call is genuinely in flight. Sizing a scan to be "slow
            # enough" instead is a race against whatever machine CI runs on,
            # and it lost one: a 4096x4096 scan finished inside the window on
            # a fast runner, so the test failed on its own premise rather
            # than on the behaviour it checks.
            time.sleep(float(delay_s))
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
        """Release the device; idempotent, for the same reason as ``NionCamera``."""
        if self._closed:
            return
        self._closed = True
        self._device.close()


class NionInstrument:
    """
    An ``InstrumentController`` over a Nion ``STEMController``.

    Also the device server's ``instrument`` RPC target, which is why it
    carries ``describe`` and ``shutdown`` alongside the controls: the
    client needs one place to ask "what does this instrument have?" and
    one place to ask for a clean teardown, and neither belongs to a
    camera or a scanner.
    """

    def __init__(self, controller: typing.Any) -> None:  # noqa: ANN401 - vendor object
        self._controller = controller
        self._session: _ServerSession | None = None

    def bind_session(self, session: _ServerSession) -> None:
        """
        Attach the serving session that ``describe``/``shutdown`` report on.

        Called by :func:`serve`. Left unbound for in-process use, where
        the caller owns the devices and needs neither call.

        Parameters
        ----------
        session : _ServerSession
            The session holding this instrument's devices and writers.
        """
        self._session = session

    def stage_size_nm(self) -> float:
        """Return the usable stage extent in nanometres, or a documented default."""
        size = getattr(self._controller, "stage_size_nm", None)
        return float(size) if size is not None else _FALLBACK_STAGE_SIZE_NM

    def available_controls(self) -> Sequence[str]:
        """
        Return the neutral control names this instrument actually implements.

        Probed, not assumed: 1D controls answer through the base
        ``STEMController.does_control_exist`` (itself a ``TryGetVal``),
        and the 2D stage is probed by reading it, since ``TryGetVal`` is
        1D-only and reports a real 2D control as absent.

        Returns
        -------
        Sequence[str]
            Some subset of ``STAGE_POSITION_CONTROL``,
            ``DEFOCUS_CONTROL``, ``BEAM_BLANKER_CONTROL``.
        """
        available = []
        if self._has_stage():
            available.append(STAGE_POSITION_CONTROL)
        if self._has_control(_DEFOCUS_CONTROL_NAME):
            available.append(DEFOCUS_CONTROL)
        if self._has_control(_BLANKER_CONTROL_NAME):
            available.append(BEAM_BLANKER_CONTROL)
        return available

    def stage_position_nm(self) -> tuple[float, float]:
        """Return the stage position as ``(y, x)`` in nanometres."""
        point = self._get_stage_point()
        return (point.y * _NM_PER_M, point.x * _NM_PER_M)

    def set_stage_position_nm(self, y_nm: float, x_nm: float) -> None:
        """
        Move the stage to an absolute ``(y, x)`` position in nanometres.

        Parameters
        ----------
        y_nm : float
            Target position along the slow scan axis, in nanometres.
        x_nm : float
            Target position along the fast scan axis, in nanometres.
        """
        point = _Geometry.FloatPoint(y=y_nm / _NM_PER_M, x=x_nm / _NM_PER_M)
        self._set_stage_point(point)

    def defocus_nm(self) -> float:
        """Return the defocus in nanometres (the vendor's ``C10``, in metres)."""
        metres = self._controller.get_control_output(_DEFOCUS_CONTROL_NAME)
        return float(metres) * _NM_PER_M

    def set_defocus_nm(self, defocus_nm: float) -> None:
        """
        Set the defocus in nanometres.

        Parameters
        ----------
        defocus_nm : float
            Target defocus, in nanometres.
        """
        self._controller.set_control_output(
            _DEFOCUS_CONTROL_NAME,
            defocus_nm / _NM_PER_M,
        )

    def is_beam_blanked(self) -> bool:
        """Return whether the beam is blanked (the vendor's ``C_Blank``, 0 or 1)."""
        return bool(self._controller.get_control_output(_BLANKER_CONTROL_NAME))

    def set_beam_blanked(self, *, blanked: bool) -> None:
        """
        Blank or unblank the beam.

        Parameters
        ----------
        blanked : bool
            ``True`` to blank the beam, ``False`` to unblank it.
        """
        self._controller.set_control_output(
            _BLANKER_CONTROL_NAME,
            1.0 if blanked else 0.0,
        )

    def park(self) -> None:
        """
        Blank the beam if this instrument has a blanker; otherwise do nothing.

        An instrument without a blanker has no vendor-neutral safe state
        to move to, and inventing one (parking the stage, dropping the
        high tension) would be a guess about the hardware, not a service
        to it.
        """
        if self._has_control(_BLANKER_CONTROL_NAME):
            self.set_beam_blanked(blanked=True)

    def describe(self) -> dict[str, object]:
        """
        Report what this device server is serving, as plain data.

        The client connects to this target first and uses the answer to
        decide which other targets exist — a real instrument need not
        have both a Ronchigram and an EELS camera the way usim does.

        Returns
        -------
        dict[str, object]
            ``backend``, ``targets`` (device target names), ``controls``
            (neutral control names), and ``stage_size_nm``.
        """
        session = self._session
        targets = (
            [name for name in _DEVICE_TARGET_NAMES if name in session.targets]
            if session is not None
            else []
        )
        return {
            "backend": session.backend if session is not None else "",
            "targets": targets,
            "controls": list(self.available_controls()),
            "stage_size_nm": self.stage_size_nm(),
        }

    def health(self) -> dict[str, object]:
        """
        Answer "is this server alive and working?" without touching any device.

        Deliberately reports only server-process state — pid, uptime,
        backend, served targets, which devices are still open, whether a
        shutdown has run. No vendor object is consulted and no device lock
        is taken, which is what makes this safe to call while an
        acquisition is in flight *and* meaningful as a liveness probe: the
        answer cannot be delayed by a long exposure, so a client that
        waits a bounded time and gets nothing has learned the server is
        wedged rather than merely busy. Probing the instrument's controls
        here instead (``available_controls``, a defocus read) would trade
        that away for information the client already has from
        ``describe``.

        Returns
        -------
        dict[str, object]
            ``ok``, ``pid``, ``backend``, ``uptime_s``, ``targets``,
            ``open_devices``, and ``shutting_down`` — all plain data, so
            it crosses the RPC boundary unchanged.
        """
        session = self._session
        if session is None:
            # In-process use: nothing is being served, so there is no
            # session state to report, but "this object is alive" is still
            # a true and useful answer.
            return {
                "ok": True,
                "pid": os.getpid(),
                "backend": "",
                "uptime_s": 0.0,
                "targets": [],
                "open_devices": [],
                "shutting_down": False,
            }
        return session.health()

    def shutdown(self) -> dict[str, object]:
        """
        Park the instrument and release every device, then report what happened.

        Returns
        -------
        dict[str, object]
            The park report from :meth:`_ServerSession.park_and_release`.

        Raises
        ------
        RuntimeError
            If this instrument is not being served (no session bound).
        """
        if self._session is None:
            msg = "shutdown() requires a served instrument; none is bound"
            raise RuntimeError(msg)
        return self._session.park_and_release()

    def _has_control(self, name: str) -> bool:
        """Return whether a 1D vendor control of this name exists."""
        try:
            return bool(self._controller.does_control_exist(name))
        except Exception:  # noqa: BLE001 - a vendor controller may raise instead
            return False

    def _has_stage(self) -> bool:
        """Return whether the 2D stage-position control can be read."""
        try:
            self._get_stage_point()
        except Exception:  # noqa: BLE001 - absence is reported as an exception
            return False
        return True

    def _get_stage_point(self) -> typing.Any:  # noqa: ANN401 - vendor Geometry.FloatPoint
        """Read the stage control, tolerating either GetVal2D axis convention."""
        try:
            return self._controller.GetVal2D(_STAGE_POSITION_CONTROL_NAME)
        except TypeError:
            # STEMController's base signature makes `axis` keyword-*required*
            # while Nion's own device_kit implementation defaults it to the
            # control's native axis. Unverified which a vendor controller
            # does, so both are tried; ("x", "y") is what
            # STEMController.resolve_axis falls back to.
            return self._controller.GetVal2D(
                _STAGE_POSITION_CONTROL_NAME,
                axis=("x", "y"),
            )

    def _set_stage_point(self, point: typing.Any) -> None:  # noqa: ANN401 - vendor type
        """Write the stage control, tolerating either SetVal2D axis convention."""
        try:
            self._controller.SetVal2D(_STAGE_POSITION_CONTROL_NAME, point)
        except TypeError:
            self._controller.SetVal2D(
                _STAGE_POSITION_CONTROL_NAME,
                point,
                axis=("x", "y"),
            )


@dataclass(frozen=True)
class InstrumentDevices:
    """
    Handles to the devices of one STEM microscope, simulated or real.

    Attributes
    ----------
    ronchigram_camera : NionCamera | None
        The Ronchigram (diffraction-plane) camera, if the instrument has one.
    eels_camera : NionCamera | None
        The EELS camera, if the instrument has one. usim always has both;
        a real instrument need not.
    scanner : NionScanner
        The scan device (HAADF/MAADF channels on usim).
    instrument : NionInstrument
        Stage/defocus/blanker controls, and the shutdown handshake.
    stage_size_nm : float
        The stage extent, useful for choosing a sensible
        ``ScanParameters.fov_nm``.
    """

    ronchigram_camera: NionCamera | None
    eels_camera: NionCamera | None
    scanner: NionScanner
    instrument: NionInstrument
    stage_size_nm: float

    def cameras(self) -> Sequence[tuple[str, NionCamera]]:
        """Return ``(target name, camera)`` for each camera present."""
        pairs = (
            ("ronchigram_camera", self.ronchigram_camera),
            ("eels_camera", self.eels_camera),
        )
        return [(name, camera) for name, camera in pairs if camera is not None]


# Historical name, kept because README and the migration plan refer to it.
SimulatedInstrument = InstrumentDevices


@contextlib.contextmanager
def simulated_instrument() -> Iterator[InstrumentDevices]:
    """
    Build the nionswift-usim simulated microscope and guarantee clean teardown.

    usim starts a background acquisition thread per camera at construction
    time, and it constructs *both* cameras up front; every camera must be
    closed on the way out or the process hangs at exit waiting for the
    un-closed thread to join. This context manager owns that lifecycle.

    Yields
    ------
    InstrumentDevices
        Adapted handles to the simulated cameras, scanner, and instrument.
    """
    configuration = _UsimConfiguration.AcquisitionContextConfiguration(
        set_configuration_location=False,
    )
    ronchigram_camera = NionCamera(configuration.ronchigram_camera_device)
    eels_camera = NionCamera(configuration.eels_camera_device)
    scanner = NionScanner(configuration.scan_module.device)
    instrument = NionInstrument(configuration.instrument)
    try:
        yield InstrumentDevices(
            ronchigram_camera=ronchigram_camera,
            eels_camera=eels_camera,
            scanner=scanner,
            instrument=instrument,
            stage_size_nm=instrument.stage_size_nm(),
        )
    finally:
        ronchigram_camera.close()
        eels_camera.close()
        scanner.close()


def _plugin_module_names(plugin_names: Sequence[str]) -> tuple[list[str], list[str]]:
    """
    Resolve which ``nionswift_plugin`` modules to load, and what was skipped.

    Parameters
    ----------
    plugin_names : Sequence[str]
        Explicitly requested modules, bare (``"usim"``) or fully
        qualified (``"nionswift_plugin.usim"``). Empty means autodiscover.

    Returns
    -------
    tuple[list[str], list[str]]
        Fully qualified module names to load, and skipped module names
        (for the diagnostic in :exc:`HardwareNotAvailableError`).

    Raises
    ------
    HardwareNotAvailableError
        If the ``nionswift_plugin`` namespace package is not importable.
    """
    if plugin_names:
        return (
            [
                name if "." in name else f"{_PLUGIN_NAMESPACE}.{name}"
                for name in plugin_names
            ],
            [],
        )
    try:
        namespace = importlib.import_module(_PLUGIN_NAMESPACE)
    except ImportError as error:
        msg = (
            f"cannot import the {_PLUGIN_NAMESPACE!r} namespace package, so no "
            f"Nion device plug-in can be discovered: {error}. Install the "
            f"instrument's plug-in package, or name it explicitly with "
            f"--plugin/{_PLUGINS_ENV_VAR}."
        )
        raise HardwareNotAvailableError(msg) from error
    discovered = sorted(
        info.name for info in pkgutil.iter_modules(namespace.__path__)
    )
    return (
        [
            f"{_PLUGIN_NAMESPACE}.{name}"
            for name in discovered
            if name not in _SKIPPED_PLUGIN_MODULES
        ],
        [name for name in discovered if name in _SKIPPED_PLUGIN_MODULES],
    )


def _load_device_plugins(
    plugin_names: Sequence[str],
) -> tuple[list[types.ModuleType], list[str]]:
    """
    Import plug-in modules and run them, the way Nion Swift's own loader does.

    ``nion.swift.model.PlugInManager`` imports every ``nionswift_plugin``
    submodule and calls its ``run()``; a device plug-in's ``run()`` is what
    registers its ``stem_controller``/``scan_module``/``camera_module``
    components with ``nion.utils.Registry``. This reproduces just that
    step, without Swift's plug-in directories, manifests, or ``Application``.

    Parameters
    ----------
    plugin_names : Sequence[str]
        Explicitly requested modules, or empty to autodiscover.

    Returns
    -------
    tuple[list[types.ModuleType], list[str]]
        Successfully run modules, and human-readable notes about every
        module considered (loaded, skipped, or failed).
    """
    module_names, skipped = _plugin_module_names(plugin_names)
    notes = [f"skipped (non-device/simulator): {', '.join(skipped)}"] if skipped else []
    loaded: list[types.ModuleType] = []
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except Exception as error:  # noqa: BLE001 - one bad plug-in must not abort the rest
            notes.append(
                f"{module_name}: import failed "
                f"({type(error).__name__}: {error})",
            )
            continue
        runner = getattr(module, "run", None)
        if not callable(runner):
            notes.append(f"{module_name}: no run(), not a device plug-in")
            continue
        try:
            runner()
        except Exception as error:  # noqa: BLE001 - reported, not fatal
            notes.append(
                f"{module_name}: run() failed "
                f"({type(error).__name__}: {error})",
            )
            continue
        notes.append(f"{module_name}: loaded")
        loaded.append(module)
    for note in notes:
        _LOGGER.info("plug-in: %s", note)
    return loaded, notes


def _stop_device_plugins(modules: Sequence[types.ModuleType]) -> None:
    """Call each loaded plug-in module's ``stop()``, if it has one."""
    for module in reversed(list(modules)):
        stopper = getattr(module, "stop", None)
        if callable(stopper):
            with contextlib.suppress(Exception):
                stopper()


def _cameras_from_registry(
    camera_modules: Sequence[typing.Any],
) -> tuple[NionCamera | None, NionCamera | None]:
    """
    Sort registered camera modules into (Ronchigram, EELS) by ``camera_type``.

    Parameters
    ----------
    camera_modules : Sequence[typing.Any]
        ``camera_module`` components from the Nion registry.

    Returns
    -------
    tuple[NionCamera | None, NionCamera | None]
        The diffraction-plane camera and the EELS camera. A camera whose
        ``camera_type`` is neither becomes the Ronchigram camera if that
        slot is still empty, so an instrument with one unlabelled camera
        is still usable.
    """
    # Registry.get_components_by_type returns a set; sort for determinism.
    ordered = sorted(camera_modules, key=lambda module: module.camera_device.camera_id)
    by_type: dict[str, typing.Any] = {}
    unlabelled = []
    for module in ordered:
        camera_type = getattr(module.camera_device, "camera_type", "")
        if camera_type in ("ronchigram", "eels") and camera_type not in by_type:
            by_type[camera_type] = module.camera_device
        else:
            unlabelled.append(module.camera_device)
    ronchigram = by_type.get("ronchigram")
    if ronchigram is None and unlabelled:
        ronchigram = unlabelled.pop(0)
    eels = by_type.get("eels")
    return (
        NionCamera(ronchigram) if ronchigram is not None else None,
        NionCamera(eels) if eels is not None else None,
    )


def _devices_from_registry(notes: Sequence[str]) -> InstrumentDevices:
    """
    Build device handles from whatever the Nion registry now holds.

    Parameters
    ----------
    notes : Sequence[str]
        Plug-in load notes, quoted in the error if nothing was found.

    Returns
    -------
    InstrumentDevices
        Handles to the registered scan device, cameras, and controller.

    Raises
    ------
    HardwareNotAvailableError
        If no STEM controller, no scan module, or no camera is registered.
    """
    controller = _Registry.get_component("stem_controller")
    scan_module = _Registry.get_component("scan_module")
    camera_modules = list(_Registry.get_components_by_type("camera_module"))
    ronchigram_camera, eels_camera = _cameras_from_registry(camera_modules)
    missing = [
        label
        for label, present in (
            ("stem_controller", controller is not None),
            ("scan_module", scan_module is not None),
            ("camera_module", ronchigram_camera is not None or eels_camera is not None),
        )
        if not present
    ]
    if missing:
        detail = "; ".join(notes) if notes else "no plug-in modules were considered"
        msg = (
            f"no Nion hardware found: the nion Registry has no "
            f"{', '.join(missing)} after loading device plug-ins [{detail}]. "
            f"On an instrument control computer the vendor's nionswift_plugin "
            f"package must be installed and importable; name it explicitly with "
            f"--plugin/{_PLUGINS_ENV_VAR} if autodiscovery skipped it. Use "
            f"--backend={SIMULATED_BACKEND} to run against nionswift-usim instead."
        )
        raise HardwareNotAvailableError(msg)
    instrument = NionInstrument(controller)
    return InstrumentDevices(
        ronchigram_camera=ronchigram_camera,
        eels_camera=eels_camera,
        scanner=NionScanner(scan_module.device),
        instrument=instrument,
        stage_size_nm=instrument.stage_size_nm(),
    )


@contextlib.contextmanager
def hardware_instrument(
    plugin_names: Sequence[str] = (),
) -> Iterator[InstrumentDevices]:
    """
    Discover and drive real Nion hardware, and guarantee clean teardown.

    **Untested against real hardware.** Everything here except the
    identity of the vendor plug-in package is exercised by the tests
    (which point ``plugin_names`` at usim as a stand-in vendor plug-in),
    but no Nion microscope has been on the other end of it. The specific
    unverified assumptions are: that a vendor plug-in registers its
    devices from a module-level ``run()`` the way usim and Swift's own
    loader assume; that ``camera_device.camera_type`` labels a real
    camera as ``"ronchigram"``/``"eels"``; and that a real controller
    answers to the control names in this module's constants.

    Parameters
    ----------
    plugin_names : Sequence[str]
        Plug-in modules to load, bare or fully qualified. Empty
        autodiscovers every ``nionswift_plugin`` submodule except the
        simulator/non-device ones in ``_SKIPPED_PLUGIN_MODULES``.

    Yields
    ------
    InstrumentDevices
        Handles to the discovered cameras, scanner, and instrument.

    Raises
    ------
    HardwareNotAvailableError
        If no instrument was found. This is the path that *is* testable
        without hardware, and it is expected to be the first thing a
        misconfigured instrument computer hits, so the message names what
        was loaded, what was skipped, and what to do about it.
    """
    modules, notes = _load_device_plugins(plugin_names)
    try:
        devices = _devices_from_registry(notes)
    except HardwareNotAvailableError:
        _stop_device_plugins(modules)
        raise
    try:
        yield devices
    finally:
        for _name, camera in devices.cameras():
            with contextlib.suppress(Exception):
                camera.close()
        with contextlib.suppress(Exception):
            devices.scanner.close()
        _stop_device_plugins(modules)


def open_instrument(
    backend: str = SIMULATED_BACKEND,
    plugin_names: Sequence[str] = (),
) -> typing.ContextManager[InstrumentDevices]:
    """
    Return the context manager for the requested backend.

    Parameters
    ----------
    backend : str
        ``"simulated"`` for nionswift-usim, ``"hardware"`` for real devices.
    plugin_names : Sequence[str]
        Plug-in modules for the hardware backend; ignored by the simulator.

    Returns
    -------
    typing.ContextManager[InstrumentDevices]
        An unentered context manager yielding device handles.

    Raises
    ------
    ValueError
        If ``backend`` is not one of ``BACKENDS``.
    """
    if backend == SIMULATED_BACKEND:
        return simulated_instrument()
    if backend == HARDWARE_BACKEND:
        return hardware_instrument(plugin_names)
    msg = f"unknown backend {backend!r}; expected one of {BACKENDS}"
    raise ValueError(msg)


class _ServerSession:
    """
    One serving session: the targets, their shared-memory writers, and teardown.

    Exists so the ``shutdown`` RPC has something to act on. The park
    sequence is deliberately here rather than in ``NionInstrument``: it
    spans every device, and "release the devices" is also what retires
    their shared-memory segments (see
    :mod:`miainwoodpecker.devices.shared_frame`), which is a server-wide
    obligation, not an instrument control.
    """

    def __init__(self, devices: InstrumentDevices, backend: str) -> None:
        self.backend = backend
        self._devices = devices
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._report: dict[str, object] | None = None
        self._started_monotonic = time.monotonic()
        self.targets: dict[str, object] = {}
        self.writers: dict[str, SharedFrameWriter | None] = {}
        for name, camera in devices.cameras():
            self.targets[name] = camera
            self.writers[name] = SharedFrameWriter()
        self.targets["scanner"] = devices.scanner
        self.writers["scanner"] = SharedFrameWriter()
        # "instrument" never returns a Frame, so it gets no writer.
        self.targets["instrument"] = devices.instrument
        self.writers["instrument"] = None
        devices.instrument.bind_session(self)

    def health(self) -> dict[str, object]:
        """
        Report this session's liveness as plain data, without touching a device.

        Takes no lock — not even this session's own, which
        :meth:`park_and_release` holds for the duration of a park. Waiting
        on it would make the health check block for exactly as long as a
        slow park, which is precisely the situation the client needs an
        answer about. ``shutting_down`` conveys that instead, and every
        field read here is either immutable for the session's life or a
        single attribute whose torn read is harmless.

        Returns
        -------
        dict[str, object]
            See :meth:`NionInstrument.health`, which is the RPC entry
            point and documents the fields.
        """
        if os.environ.get(_WEDGE_HEALTH_ENV_VAR):
            # Test hook: behave like a server wedged with its listeners
            # still bound, so the client's bounded-wait path is exercised
            # against a genuinely unresponsive server rather than a mock.
            threading.Event().wait()
        open_devices = [
            name
            for name in _DEVICE_TARGET_NAMES
            if (device := self.targets.get(name)) is not None
            and not device.is_closed  # type: ignore[attr-defined]
        ]
        return {
            "ok": True,
            "pid": os.getpid(),
            "backend": self.backend,
            "uptime_s": time.monotonic() - self._started_monotonic,
            "targets": [name for name in _DEVICE_TARGET_NAMES if name in self.targets],
            "open_devices": open_devices,
            "shutting_down": self._report is not None or self.stop_event.is_set(),
        }

    def park_and_release(self) -> dict[str, object]:
        """
        Stop detectors, park the instrument, release devices; report the outcome.

        Every step is individually guarded and its failure recorded rather
        than raised, so a half-successful park still acknowledges *and*
        still unlinks the shared-memory segments — a hard SIGTERM would
        leave those behind (they are tmpfs entries, not process
        resources), which is the one thing teardown must not skip.

        Returns
        -------
        dict[str, object]
            ``backend``, ``cameras_stopped``, ``beam_blanked``,
            ``devices_released``, and ``errors``, all plain data so it
            crosses the RPC boundary unchanged. Idempotent: a second
            call returns the first call's report.
        """
        if os.environ.get(_WEDGE_SHUTDOWN_ENV_VAR):
            # Test hook: behave like a server wedged mid-shutdown, so the
            # client's timeout-and-SIGTERM fallback is exercised for real.
            threading.Event().wait()
        with self._lock:
            if self._report is not None:
                return self._report
            errors: list[str] = []
            stopped: list[str] = []
            for name, camera in self._devices.cameras():
                try:
                    camera.stop()
                except Exception as error:  # noqa: BLE001 - recorded, not raised
                    errors.append(f"{name}.stop(): {type(error).__name__}: {error}")
                else:
                    stopped.append(name)
            blanked = False
            try:
                self._devices.instrument.park()
                blanked = BEAM_BLANKER_CONTROL in (
                    self._devices.instrument.available_controls()
                ) and self._devices.instrument.is_beam_blanked()
            except Exception as error:  # noqa: BLE001 - recorded, not raised
                errors.append(f"instrument.park(): {type(error).__name__}: {error}")
            released = self._release_devices(errors)
            self._report = {
                "backend": self.backend,
                "cameras_stopped": stopped,
                "beam_blanked": blanked,
                "devices_released": released,
                "errors": errors,
            }
            if errors:
                # Above the default level on purpose: a half-successful
                # park is the case where the client falls back to SIGTERM,
                # and its reasons must not need an env var to be seen.
                _LOGGER.warning(
                    "shutdown: parked with %d error(s): %s",
                    len(errors),
                    "; ".join(errors),
                )
            else:
                _LOGGER.info(
                    "shutdown: stopped %s, beam_blanked=%s, released %s",
                    ", ".join(stopped) or "(no cameras)",
                    blanked,
                    ", ".join(released),
                )
            return self._report

    def _release_devices(self, errors: list[str]) -> list[str]:
        """Close every device and retire its shared-memory segment."""
        released: list[str] = []
        for name in _DEVICE_TARGET_NAMES:
            device = self.targets.get(name)
            if device is None:
                continue
            try:
                device.close()  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001 - recorded, not raised
                errors.append(f"{name}.close(): {type(error).__name__}: {error}")
            writer = self.writers.get(name)
            if writer is not None:
                try:
                    writer.close()
                except Exception as error:  # noqa: BLE001 - recorded, not raised
                    errors.append(
                        f"{name} shared memory: {type(error).__name__}: {error}",
                    )
            released.append(name)
        return released


def _serve_connection(
    connection: object,
    name: str,
    target: object,
    writer: SharedFrameWriter | None,
    stop_event: threading.Event,
) -> None:
    """
    Handle Calls on one accepted connection until the client disconnects.

    Logging here is deliberately failure-only. A successful call is not
    logged at all, at any level: the frame path runs through this loop, so
    even a disabled ``debug()`` call would sit between the client's request
    and its frame, and §6's shared-memory benchmarks are the standard this
    must not move.

    Parameters
    ----------
    connection : object
        The accepted connection, typed loosely to avoid importing
        ``multiprocessing.connection`` for a type-only reference.
    name : str
        This connection's server-side target name, used to attribute log
        records. Taken from the server's own binding rather than from
        ``call.target``, which is whatever the client claimed.
    target : object
        The device (or ``NionInstrument``) this connection's calls dispatch to.
    writer : SharedFrameWriter | None
        This target's reused shared-memory writer, or ``None`` for a
        target (``instrument``) that never returns a ``Frame``.
    stop_event : threading.Event
        Set after a ``shutdown`` call's reply has been sent, to let
        :func:`serve` return. Setting it only *after* the send is what
        keeps the acknowledgement from racing the listener teardown.
    """
    try:
        while True:
            try:
                call: Call = connection.recv()
            except EOFError:
                _LOGGER.debug("target %s: client disconnected", name)
                return
            if not hasattr(target, call.method):
                _LOGGER.warning(
                    "target %s: unknown method %r requested", name, call.method,
                )
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
                # Only frames worth the round trip are routed around the
                # pickle-over-socket channel; everything else returned here
                # is small (a string, a list of names, None).
                if (
                    writer is not None
                    and isinstance(value, Frame)
                    and value.data.nbytes >= _SHARED_MEMORY_THRESHOLD_BYTES
                ):
                    value = writer.publish(value)
                # The device's own close() just stopped its acquisition
                # thread; retire its shared-memory segment too, or it leaks
                # in /dev/shm - named segments aren't reclaimed when a
                # process dies, unlike its threads or anonymous memory.
                if call.method == "close" and writer is not None:
                    writer.close()
            except Exception as exc:
                # The client gets the message; the log gets the traceback,
                # which is the only place it survives - Result carries a
                # string, so a stringified error is all that crosses the
                # boundary. This is the per-call diagnostic the whole
                # logging setup exists for.
                _LOGGER.exception("target %s: call %s() raised", name, call.method)
                connection.send(Result(error=f"{type(exc).__name__}: {exc}"))
            else:
                connection.send(Result(value=value))
                if call.method == "shutdown":
                    stop_event.set()
                    return
    finally:
        connection.close()


def _accept_loop(
    listener: Listener,
    name: str,
    target: object,
    writer: SharedFrameWriter | None,
    stop_event: threading.Event,
) -> None:
    """Accept connections for one target, one handler thread per connection."""
    while True:
        try:
            connection = listener.accept()
        except OSError:
            _LOGGER.debug("target %s: listener closed, no longer accepting", name)
            return  # listener.close() from elsewhere unblocks accept() this way.
        _LOGGER.info("target %s: accepted a connection", name)
        disable_nagle(connection)
        thread = threading.Thread(
            target=_serve_connection,
            args=(connection, name, target, writer, stop_event),
            daemon=True,
        )
        thread.start()


def serve(
    ports: typing.Mapping[str, int],
    authkey: bytes,
    *,
    backend: str = SIMULATED_BACKEND,
    plugin_names: Sequence[str] = (),
) -> None:
    """
    Build the requested instrument and serve it, one Listener per target.

    Blocks until a client's ``shutdown`` call has been acknowledged, or
    until the process is killed by its parent —
    :mod:`miainwoodpecker.devices.remote` tries the former and falls back
    to the latter.

    Only targets the instrument actually has get a Listener; the client
    learns which those are from ``instrument.describe()`` before it
    connects to any of them.

    Parameters
    ----------
    ports : typing.Mapping[str, int]
        Port number for each of ``"ronchigram_camera"``, ``"eels_camera"``,
        ``"scanner"``, and ``"instrument"``.
    authkey : bytes
        Shared secret authenticating connections to every Listener.
    backend : str
        ``"simulated"`` or ``"hardware"``; see :func:`open_instrument`.
    plugin_names : Sequence[str]
        Plug-in modules for the hardware backend.
    """
    from multiprocessing.connection import Listener as _Listener  # noqa: PLC0415

    _LOGGER.info(
        "starting: pid=%d backend=%s plugins=%s",
        os.getpid(),
        backend,
        list(plugin_names) or "(autodiscover)",
    )
    with open_instrument(backend, plugin_names) as devices:
        session = _ServerSession(devices, backend)
        names = list(session.targets)
        listeners = [
            _Listener(("localhost", ports[name]), authkey=authkey) for name in names
        ]
        _LOGGER.info(
            "serving %s on ports %s",
            ", ".join(names),
            ", ".join(str(ports[name]) for name in names),
        )
        for listener, name in zip(listeners, names, strict=True):
            thread = threading.Thread(
                target=_accept_loop,
                args=(
                    listener,
                    name,
                    session.targets[name],
                    session.writers[name],
                    session.stop_event,
                ),
                daemon=True,
            )
            thread.start()
        session.stop_event.wait()
        _LOGGER.info("shutdown acknowledged; closing listeners and exiting")
        for listener in listeners:
            listener.close()


def _plugins_from_environment() -> list[str]:
    """
    Read the comma-separated plug-in list out of the environment.

    Returns
    -------
    list[str]
        Plug-in module names, empty if the variable is unset or blank.
    """
    return [
        name.strip()
        for name in os.environ.get(_PLUGINS_ENV_VAR, "").split(",")
        if name.strip()
    ]


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """
    Parse the server's command line.

    Ports stay positional, in ``_TARGET_NAMES`` order, as they have been
    since this module gained a serving loop; the backend selector and the
    plug-in list are flags falling back to the environment, so a launcher
    can set either one either way, **with the command line winning**.

    That last part needs code rather than an argparse ``default`` for
    ``--plugin``, and getting it wrong is silent: ``action="append"``
    appends to its default rather than replacing it, so seeding the default
    from ``$MIAINWOODPECKER_HARDWARE_PLUGINS`` made ``--plugin foo`` mean
    "the environment's plug-ins *and* foo". On a hardware backend that is
    not a cosmetic difference — it loads vendor plug-ins the operator
    explicitly did not ask for. So the flag defaults to ``None`` and the
    environment is consulted afterwards, only when nothing was passed.

    Parameters
    ----------
    argv : Sequence[str]
        Arguments after the program name.

    Returns
    -------
    argparse.Namespace
        With ``ports`` (list[int]), ``backend`` (str), ``plugin`` (list[str]).
    """
    parser = argparse.ArgumentParser(
        prog="python -m miainwoodpecker.devices.nion_server",
        description="Serve Nion devices (simulated or real) over the rpc protocol.",
    )
    parser.add_argument(
        "ports",
        type=int,
        nargs=len(_TARGET_NAMES),
        metavar="PORT",
        help=f"one port per target, in order: {', '.join(_TARGET_NAMES)}",
    )
    parser.add_argument(
        "--backend",
        choices=BACKENDS,
        default=os.environ.get(_BACKEND_ENV_VAR, SIMULATED_BACKEND),
        help=f"device backend (default from ${_BACKEND_ENV_VAR}, else simulated)",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=None,  # see this function's docstring: append() adds to its default
        metavar="MODULE",
        help=(
            "nionswift_plugin module providing hardware devices; repeatable. "
            f"If not given at all, falls back to ${_PLUGINS_ENV_VAR} (comma "
            "separated); if neither, autodiscovery. Passing this overrides "
            "the environment rather than adding to it."
        ),
    )
    arguments = parser.parse_args(list(argv))
    if arguments.plugin is None:
        arguments.plugin = _plugins_from_environment()
    return arguments


def _configure_logging() -> None:
    """
    Attach this process's log handler, quiet by default.

    Called only from :func:`main`, never at import: a library that
    configures logging on import steals the decision from whoever imported
    it, and this module *is* imported in-process by its own tests.

    Two things the default format buys, both of which the plain inherited
    stderr this replaces did not have: the pid, so a server's lines can be
    told from the application's in one interleaved terminal, and the logger
    name, so they can be told from any other library's.
    ``MIAINWOODPECKER_DEVICE_LOG_FILE`` avoids the interleaving entirely by
    sending the log somewhere else; a path that cannot be opened falls back
    to stderr with a warning rather than taking the server down over its
    own diagnostics.
    """
    level_name = os.environ.get(_LOG_LEVEL_ENV_VAR, _DEFAULT_LOG_LEVEL).upper()
    level = logging.getLevelNamesMapping().get(level_name)
    handler: logging.Handler | None = None
    log_path = os.environ.get(_LOG_FILE_ENV_VAR)
    open_error: OSError | None = None
    if log_path:
        try:
            handler = logging.FileHandler(log_path, encoding="utf-8")
        except OSError as error:
            open_error = error
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [nion_server pid=%(process)d] "
                "%(name)s: %(message)s",
        ),
    )
    _LOGGER.handlers.clear()
    _LOGGER.addHandler(handler)
    _LOGGER.propagate = False
    _LOGGER.setLevel(level if level is not None else logging.WARNING)
    if level is None:
        _LOGGER.warning(
            "%s=%r is not a logging level name; using %s",
            _LOG_LEVEL_ENV_VAR,
            level_name,
            _DEFAULT_LOG_LEVEL,
        )
    if open_error is not None:
        _LOGGER.warning(
            "cannot write %s=%r (%s); logging to stderr instead",
            _LOG_FILE_ENV_VAR,
            log_path,
            open_error,
        )


def main(argv: Sequence[str] | None = None) -> None:
    """
    Parse ports and backend from argv, the authkey from the environment, then serve.

    Invoked as ``python -m miainwoodpecker.devices.nion_server [--backend
    NAME] [--plugin MODULE] PORT PORT PORT PORT`` by
    :mod:`miainwoodpecker.devices.remote`, which also sets
    ``MIAINWOODPECKER_AUTHKEY``.

    Parameters
    ----------
    argv : Sequence[str] | None
        Arguments after the program name, or ``None`` for ``sys.argv[1:]``.

    Raises
    ------
    SystemExit
        With :data:`NO_HARDWARE_EXIT_STATUS` when the hardware backend
        finds no instrument — the expected outcome on a machine with
        nothing attached, so it exits with a message rather than a
        traceback.
    """
    _configure_logging()
    arguments = _parse_args(argv if argv is not None else sys.argv[1:])
    ports = dict(zip(_TARGET_NAMES, arguments.ports, strict=True))
    authkey = bytes.fromhex(os.environ[_AUTHKEY_ENV_VAR])
    try:
        serve(ports, authkey, backend=arguments.backend, plugin_names=arguments.plugin)
    except HardwareNotAvailableError as error:
        # The expected failure on a machine with no instrument attached, so
        # it gets a message rather than a traceback. Logged at ERROR so it
        # is visible at the default level, and so it follows
        # MIAINWOODPECKER_DEVICE_LOG_FILE if the operator redirected the
        # log - the client only ever learns that the process died with
        # status 2, so this text is the whole diagnostic.
        _LOGGER.error("%s: %s", type(error).__name__, error)  # noqa: TRY400 - a traceback adds nothing here
        raise SystemExit(NO_HARDWARE_EXIT_STATUS) from error


if __name__ == "__main__":
    main()
