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
import math
import os
import pkgutil
import signal
import sys
import threading
import time
import typing
from dataclasses import dataclass

from nion.device_kit import ScanDevice as _ScanDeviceKit
from nion.instrumentation import camera_base as _camera_base
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
from miainwoodpecker.devices.rpc import (
    BACKENDS,
    HARDWARE_BACKEND,
    SIMULATED_BACKEND,
    Call,
    Result,
    disable_nagle,
)
from miainwoodpecker.devices.rpc import (
    DEVICE_TARGET_NAMES as _DEVICE_TARGET_NAMES,
)
from miainwoodpecker.devices.rpc import (
    SHARED_MEMORY_THRESHOLD_BYTES as _SHARED_MEMORY_THRESHOLD_BYTES,
)
from miainwoodpecker.devices.rpc import (
    TARGET_NAMES as _TARGET_NAMES,
)
from miainwoodpecker.devices.shared_frame import SharedFrameWriter
from miainwoodpecker.storage import calibration as _calibration

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
_ORPHAN_GRACE_ENV_VAR = "MIAINWOODPECKER_ORPHAN_GRACE_S"
# The hooks above do nothing unless this is set, which only
# --enable-test-hooks does. The environment alone used to be enough,
# and the environment is inherited wholesale by the subprocess: an
# operator with MIAINWOODPECKER_WEDGE_SHUTDOWN left set in a shell
# profile would have had a real instrument that could not be shut down
# or health-checked, with no indication why. A flag the launcher never
# passes cannot be set by accident.
_TEST_HOOKS_ENABLED = False


def _test_hook(name: str) -> str | None:
    """
    Return a test hook's value, or None unless test hooks are enabled.

    Parameters
    ----------
    name : str
        The hook's environment variable name.

    Returns
    -------
    str | None
        The value when hooks are armed and the variable is set;
        ``None`` otherwise, which every caller treats as 'no hook'.
    """
    if not _TEST_HOOKS_ENABLED:
        return None
    return os.environ.get(name)

# Named rather than taken from ``__name__``: this module's usual entry
# point is ``python -m miainwoodpecker.devices.nion_server``, under which
# ``__name__`` is ``"__main__"`` — a logger name that tells an operator
# reading interleaved stderr nothing about which program emitted the line,
# which is the whole problem this logging exists to solve.
_LOGGER = logging.getLogger("miainwoodpecker.devices.nion_server")
_DEFAULT_LOG_LEVEL = "WARNING"

# Target names, backend names, and the shared-memory threshold all come
# from rpc.py: they are protocol vocabulary, and both peers must agree.

# Exit status for "the hardware backend found no instrument". Distinct from
# 1 so a launcher can tell a missing microscope from a crash.
NO_HARDWARE_EXIT_STATUS = 2

# Exit status for "a termination signal arrived and the park did not finish
# in time". Distinct again, because the operator consequence is specific and
# serious: the process is gone and the instrument was left in an unknown
# state, which is worth surfacing rather than folding into a generic crash.
PARK_TIMEOUT_EXIT_STATUS = 3

# Exit status for "one of the ports handed to us was already taken".
# Distinct because it is the one startup failure worth *retrying*: the
# client picks ports by probing and releasing them, so a concurrent
# process can claim one in between - likelier the more sessions start at
# once, which is exactly what a parallel test run does. A retry with
# fresh ports succeeds; retrying a missing instrument or a crash would
# just repeat the failure, so those keep their own statuses.
PORT_UNAVAILABLE_EXIT_STATUS = 4

# How long a signal handler waits for the park before giving up and exiting.
# Comfortably inside the client's own 5s terminate-to-kill escalation
# (remote._TERMINATE_TIMEOUT_S), so a server that cannot park still dies of
# its own accord - with a status saying so - rather than being SIGKILLed.
_PARK_ON_SIGNAL_TIMEOUT_S = 3.0

# How long every client connection must stay gone before the server
# concludes its client died and parks. Generous because the cost of
# being wrong is parking a live instrument, and cheap because the
# alternative failure - an orphaned server holding a column - is only
# ever resolved by a human noticing.
_ORPHAN_GRACE_S = 30.0

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


def _axis_calibration_spec(
    calibration: typing.Any,  # noqa: ANN401 - nion.data.Calibration
) -> dict[str, object] | None:
    """
    Translate one Nion ``Calibration`` into this project's plain-data axis spec.

    Returns ``None`` — meaning "this axis is uncalibrated" — for the two
    ways a device can decline to calibrate an axis, which are deliberately
    not errors. ``build_calibration`` returns an empty ``Calibration``
    (no units) when the named controls are missing or nonsensical, and the
    EELS camera's slow axis reports empty units even when its controls
    resolve. An axis whose units are outside this project's vocabulary
    goes the same way, because a scale in units nothing downstream can
    interpret is worth less than an admitted pixel axis.

    Parameters
    ----------
    calibration : typing.Any
        A ``nion.data.Calibration.Calibration``.

    Returns
    -------
    dict[str, object] | None
        A mapping in the vocabulary
        :func:`miainwoodpecker.storage.calibration.resolve_frame_calibration`
        parses, or ``None``.
    """
    units = str(getattr(calibration, "units", "") or "").strip()
    if not units:
        return None
    try:
        kind = _calibration.axis_kind_for_units(units)
    except ValueError:
        return None
    scale = float(calibration.scale)
    if not math.isfinite(scale) or scale == 0.0:
        return None
    offset = float(calibration.offset)
    if not math.isfinite(offset):
        return None
    return {
        "kind": kind.value,
        "scale": scale,
        "offset": offset,
        "units": units,
    }


class NionCamera:
    """A ``Camera`` implementation wrapping a Nion device-kit camera device."""

    def __init__(
        self,
        camera_device: _DeviceKitCamera,
        controller: typing.Any = None,  # noqa: ANN401 - vendor STEMController
    ) -> None:
        self._device = camera_device
        self._controller = controller
        self._closed = False

    @property
    def is_closed(self) -> bool:
        """Return whether this device has been released."""
        return self._closed

    @property
    def camera_id(self) -> str:
        """Return the wrapped device's camera id."""
        return self._device.camera_id

    def calibration_metadata(
        self,
        shape: tuple[int, ...],
        binning: float = 1.0,
    ) -> dict[str, dict[str, object]] | None:
        """
        Resolve this camera's axis calibrations from the instrument, as plain data.

        A Nion camera device does not publish calibration *values*: it
        publishes a ``calibration_controls`` mapping naming the instrument
        controls that hold them, and ``camera_base.build_calibration``
        reads those at acquisition time. That indirection is the point — a
        camera's angular scale depends on the projector lenses, so it is
        instrument state, not a device constant.

        Calling Nion's own resolver rather than reimplementing it is what
        keeps this on the right side of the licence boundary *and*
        correct: the result crosses to the MIT application as a plain
        mapping of numbers and unit strings, and the resolution rules
        (including the ``_origin_override: "center"`` convention) stay
        Nion's.

        Parameters
        ----------
        shape : tuple[int, ...]
            The acquired frame's shape. 1D for a device-binned spectrum,
            in which case only the ``x`` axis is calibrated, as Nion does.
        binning : float
            The camera's binning factor, which multiplies the scale — a
            2x-binned pixel spans twice the angle.

        Returns
        -------
        dict[str, dict[str, object]] | None
            Axis name to spec for each axis that resolved, or ``None`` if
            none did — which is the answer for a camera that publishes no
            controls, and for one wrapped without an instrument.
        """
        controls = getattr(self._device, "calibration_controls", None)
        if self._controller is None or not controls:
            return None
        # Nion's own CalibrationControlsCalibrator passes data_shape[1] as
        # the y axis's length and data_shape[0] as x's. Each axis is given
        # its own length here instead. The difference is invisible on a
        # square sensor and only reaches the result through the "center"
        # origin override, but an axis centred on the *other* axis's
        # length is not a convention this can adopt knowingly.
        lengths = (
            {"x": shape[0]} if len(shape) == 1 else {"y": shape[0], "x": shape[1]}
        )
        axes = {}
        for name, length in lengths.items():
            spec = _axis_calibration_spec(
                _camera_base.build_calibration(
                    self._controller,
                    controls,
                    name,
                    binning,
                    length,
                ),
            )
            if spec is not None:
                axes[name] = spec
        return axes or None

    def start(self) -> None:
        """Begin continuous acquisition; blocks until the first frame is available."""
        self._device.start_live()

    def stop(self) -> None:
        """Pause continuous acquisition."""
        self._device.stop_live()

    def acquire_frame(self) -> Frame:
        """Return the next available frame; requires ``start`` to have been called."""
        data_element = self._device.acquire_image()
        data = data_element["data"]
        metadata = dict(data_element.get("properties") or {})
        calibration = self.calibration_metadata(
            data.shape,
            float(metadata.get("binning", 1) or 1),
        )
        if calibration is not None:
            metadata[_calibration.METADATA_KEY] = calibration
        return Frame(
            data=data,
            timestamp=_aware_utc(data_element.get("timestamp")),
            metadata=metadata,
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
        delay_s = _test_hook(_DELAY_SCAN_ENV_VAR)
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
                # The per-axis extent alongside the scalar, so storage
                # divides what the scan actually covered rather than
                # re-deriving it from a convention it could disagree with.
                # Square pixels mean these differ on a non-square scan.
                "fov_size_nm": parameters.fov_size_nm,
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
    controller = configuration.instrument
    ronchigram_camera = NionCamera(configuration.ronchigram_camera_device, controller)
    eels_camera = NionCamera(configuration.eels_camera_device, controller)
    scanner = NionScanner(configuration.scan_module.device)
    instrument = NionInstrument(controller)
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
    controller: typing.Any = None,  # noqa: ANN401 - vendor STEMController
) -> tuple[NionCamera | None, NionCamera | None]:
    """
    Sort registered camera modules into (Ronchigram, EELS) by ``camera_type``.

    Parameters
    ----------
    camera_modules : Sequence[typing.Any]
        ``camera_module`` components from the Nion registry.
    controller : typing.Any
        The registered ``stem_controller``, which each camera needs to
        resolve its calibration controls against.

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
        NionCamera(ronchigram, controller) if ronchigram is not None else None,
        NionCamera(eels, controller) if eels is not None else None,
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
    ronchigram_camera, eels_camera = _cameras_from_registry(camera_modules, controller)
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
        # Live client connections across every target, and whether we
        # have ever had one. Together they are how this server notices
        # its client died: see _orphan_watchdog.
        self._connection_lock = threading.Lock()
        self._connections = 0
        self._ever_connected = False
        self._connection_change = threading.Event()
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

    def connection_opened(self) -> None:
        """Record that a client connection was accepted."""
        with self._connection_lock:
            self._connections += 1
            self._ever_connected = True
        self._connection_change.set()

    def connection_closed(self) -> None:
        """Record that a client connection ended."""
        with self._connection_lock:
            self._connections = max(0, self._connections - 1)
        self._connection_change.set()

    @property
    def is_orphaned(self) -> bool:
        """Return whether every client connection has gone after having some."""
        with self._connection_lock:
            return self._ever_connected and self._connections == 0

    def wait_for_connection_change(self, timeout: float | None = None) -> None:
        """
        Block until a connection opens or closes, or the timeout elapses.

        Parameters
        ----------
        timeout : float | None
            Seconds to wait, or None to wait indefinitely.
        """
        self._connection_change.wait(timeout)
        self._connection_change.clear()

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
        if _test_hook(_WEDGE_HEALTH_ENV_VAR):
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
        if _test_hook(_WEDGE_SHUTDOWN_ENV_VAR):
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


def _invoke(
    target: object,
    call: Call,
    writer: SharedFrameWriter | None,
    name: str,
) -> Result:
    """
    Run one call against a target and package the outcome as a Result.

    Split out of :func:`_serve_connection` so the socket loop reads as a
    socket loop; the dispatch rules (properties are read rather than
    invoked, large frames go around the pickle channel, a device's close
    retires its segment) all live here.

    Parameters
    ----------
    target : object
        The device or ``NionInstrument`` the call dispatches to.
    call : Call
        The client's request.
    writer : SharedFrameWriter | None
        This target's reused shared-memory writer, or ``None`` for a
        target that never returns a ``Frame``.
    name : str
        Server-side target name, for log attribution.

    Returns
    -------
    Result
        The value, or a stringified error - never a raised exception, so
        one failing call cannot take down the connection or the server.
    """
    if not hasattr(target, call.method):
        _LOGGER.warning("target %s: unknown method %r requested", name, call.method)
        return Result(error=f"unknown method {call.method!r} on {call.target!r}")
    try:
        # getattr already evaluates properties (camera_id, scanner_id,
        # channel_names): call the result only when it's a bound
        # method, or a property's value gets invoked as a function.
        attribute = getattr(target, call.method)
        value = (
            attribute(*call.args, **call.kwargs) if callable(attribute) else attribute
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
        return Result(
            error=f"{type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
        )
    return Result(value=value)


def _serve_connection(  # noqa: PLR0913 - one dispatch loop, not a call signature
    connection: object,
    name: str,
    target: object,
    writer: SharedFrameWriter | None,
    stop_event: threading.Event,
    *,
    rejected: str | None = None,
    release: threading.Lock | None = None,
    on_close: typing.Callable[[], None] | None = None,
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
    rejected : str | None
        When set, this connection lost the race for an exclusive target:
        every call is answered with this message instead of reaching the
        device. Answering rather than hanging up means the client raises
        ``RemoteCallError`` naming the real reason on its first call.
    release : threading.Lock | None
        The exclusivity lock this connection holds, released when it ends
        so the next client can take the target over.
    on_close : typing.Callable[[], None] | None
        Called when this connection ends, so the session can notice
        when the last one has gone.
    """
    try:
        while True:
            try:
                call: Call = connection.recv()
            except (EOFError, ConnectionError):
                # ConnectionError (reset/broken pipe) is an abortive close
                # rather than a clean one; both mean "the client is gone",
                # and letting it escape would kill this thread with a
                # traceback on the stderr the parent shares.
                _LOGGER.debug("target %s: client disconnected", name)
                return
            result = (
                Result(error=rejected)
                if rejected is not None
                else _invoke(target, call, writer, name)
            )
            connection.send(result)
            if result.error is None and call.method == "shutdown":
                stop_event.set()
                return
    except OSError:
        # A client that vanished mid-reply breaks send() the same way.
        # Same reasoning as the recv() guard above: this is the client's
        # departure, not a server fault worth a thread-death traceback.
        _LOGGER.debug("target %s: connection broke while replying", name)
    finally:
        with contextlib.suppress(OSError):
            connection.close()
        if release is not None:
            release.release()
        if on_close is not None:
            on_close()


def _accept_loop(
    listener: Listener,
    name: str,
    target: object,
    writer: SharedFrameWriter | None,
    session: _ServerSession,
) -> None:
    """
    Accept connections for one target, one handler thread per connection.

    A frame-producing target (one with a ``writer``) admits **one**
    connection at a time. Its ``SharedFrameWriter`` reuses a single
    segment per shape, which is safe only while exactly one
    request/response is in flight: two clients interleaving calls would
    have the second's publish overwrite the segment while the first is
    still copying out of it, silently splicing two frames together. That
    invariant used to rest on client convention alone — one
    ``_RemoteDevice`` per target — which nothing server-side could check
    and a second viewer pointed at these ports would break. A rejected
    connection is served, not dropped, so the client gets a diagnosis on
    its first call rather than a bare EOF.
    """
    in_use = threading.Lock() if writer is not None else None
    while True:
        try:
            connection = listener.accept()
        except OSError:
            _LOGGER.debug("target %s: listener closed, no longer accepting", name)
            return  # listener.close() from elsewhere unblocks accept() this way.
        rejected: str | None = None
        if in_use is not None and not in_use.acquire(blocking=False):
            rejected = (
                f"target {name!r} is already driven by another connection; "
                f"a frame-producing device admits one client at a time"
            )
            _LOGGER.warning("target %s: refused a second connection", name)
        else:
            _LOGGER.info("target %s: accepted a connection", name)
        disable_nagle(connection)
        session.connection_opened()
        thread = threading.Thread(
            target=_serve_connection,
            args=(connection, name, target, writer, session.stop_event),
            kwargs={
                "rejected": rejected,
                "release": None if rejected is not None else in_use,
                "on_close": session.connection_closed,
            },
            daemon=True,
        )
        thread.start()


def _orphan_watchdog(session: _ServerSession, grace_s: float) -> None:
    """
    Park and stop the server once its client is gone for good.

    Without this, a client that dies without shutting down — a crash, a
    ``SIGKILL``, a notebook kernel restart — leaves ``serve()`` blocked on
    its stop event forever, holding the vendor devices open, the camera
    acquisition threads running, the ports bound, and the beam unblanked.
    Nothing reclaims that; on real hardware it is an instrument nobody can
    use and nobody is watching.

    **"The client is gone" is inferred rather than guessed at**, and that
    is what makes this safe to act on. Every connection closing would be
    ambiguous for most servers — a client might be about to reconnect —
    but this project deliberately does not support reconnect
    (docs/migration-plan.md, §6: a fresh subprocess is a fresh instrument
    construction, so silently resuming would hand the operator a session
    whose device state is quietly wrong). A client therefore holds its
    connections for its entire life by construction, and their
    disappearance is that life ending. No idle timeout is involved, so
    this cannot park an instrument someone is still using: an idle client
    is still a connected one.

    The grace period is a guard against a torn-down-and-restarting race,
    not a policy about how long an instrument may sit unused.

    Parameters
    ----------
    session : _ServerSession
        The session to watch, park, and stop.
    grace_s : float
        Seconds to wait after the last connection closes before acting,
        in case another arrives.
    """
    while not session.stop_event.is_set():
        if not session.is_orphaned:
            session.wait_for_connection_change(timeout=grace_s)
            continue
        # Everything closed. Give a moment in case this is a race with a
        # connection being replaced, then check once more.
        session.wait_for_connection_change(timeout=grace_s)
        if session.stop_event.is_set() or not session.is_orphaned:
            continue
        _LOGGER.warning(
            "client gone for %.1fs with no connections left; parking and exiting",
            grace_s,
        )
        try:
            report = session.park_and_release()
        except Exception:
            _LOGGER.exception("park after losing the client failed")
        else:
            _LOGGER.info("parked after losing the client: %s", report)
        session.stop_event.set()
        return


@contextlib.contextmanager
def _parking_signal_handlers(session: _ServerSession) -> Iterator[None]:
    """
    Park the instrument on SIGTERM/SIGINT instead of dying with the beam on.

    The client's teardown falls back to ``terminate()`` whenever the
    graceful ``shutdown`` RPC times out or reports errors — which is
    precisely the wedged-server case the fallback exists for — and a
    signal-less server answers that by dying with the column live. The
    same applies to a Ctrl-C reaching the process group. Parking is the
    one thing :meth:`InstrumentController.park` promises for exactly this
    situation, so it should not be reachable only through the RPC that a
    wedged server cannot serve.

    **The park attempt is bounded, and that is the whole subtlety.** The
    server SIGTERM most needs to reach is a wedged one — and what is
    wedged may be the park itself, since it drives the same devices the
    stuck call is holding. Parking straight from the handler therefore
    made a wedged server *unkillable by SIGTERM*: the handler blocked
    forever, the client waited out its 5 s and escalated to ``SIGKILL``,
    and the signal handling achieved nothing except converting a prompt
    ``-SIGTERM`` into a slower ``-SIGKILL``. (Found by an existing test
    that pins the wedged-server fallback, which is exactly what it is for.)

    So the park runs on its own thread with a deadline comfortably inside
    the client's escalation window. If it finishes, the process leaves
    through :func:`serve`'s normal return. If it does not, the handler
    stops waiting and exits anyway with
    :data:`PARK_TIMEOUT_EXIT_STATUS`, because a termination request that
    cannot be honoured gracefully must still be honoured: refusing to die
    does not park the beam either, and it denies the operator the one
    remedy left. The distinct status says which happened.

    :meth:`_ServerSession.park_and_release` is memoized, so a signal
    arriving during a graceful shutdown returns the existing report
    rather than parking twice.

    Handlers are restored on the way out, and installation is skipped off
    the main thread, so an in-process caller of :func:`serve` does not
    inherit process-wide signal behaviour it never asked for.

    Parameters
    ----------
    session : _ServerSession
        The session to park and stop.

    Yields
    ------
    None
        With the handlers installed for the duration.
    """

    def _handle(signum: int, _frame: object) -> None:
        name = signal.Signals(signum).name
        _LOGGER.warning("received %s: parking the instrument before exit", name)
        parked = threading.Event()

        def _park() -> None:
            try:
                report = session.park_and_release()
            except Exception:
                _LOGGER.exception("park on %s failed", name)
            else:
                _LOGGER.info("parked on %s: %s", name, report)
            parked.set()

        threading.Thread(target=_park, name="park-on-signal", daemon=True).start()
        if not parked.wait(_PARK_ON_SIGNAL_TIMEOUT_S):
            _LOGGER.error(
                "park did not finish within %.1fs of %s; exiting anyway with "
                "status %d - the instrument state is unknown",
                _PARK_ON_SIGNAL_TIMEOUT_S,
                name,
                PARK_TIMEOUT_EXIT_STATUS,
            )
            # os._exit rather than sys.exit: this runs on the main thread
            # inside a signal handler, and a SystemExit raised here would
            # unwind into whatever it interrupted (serve()'s wait, or a
            # device call) and could block on the same thing the park did.
            os._exit(PARK_TIMEOUT_EXIT_STATUS)
        # Unblocks serve()'s wait, so the process exits through its normal
        # path (listeners closed, devices released) rather than abruptly.
        session.stop_event.set()

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous: list[tuple[int, object]] = []
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(OSError, ValueError):
                previous.append((signum, signal.signal(signum, _handle)))
        yield
    finally:
        for signum, handler in previous:
            with contextlib.suppress(OSError, ValueError, TypeError):
                signal.signal(signum, handler)  # type: ignore[arg-type]


def serve(
    ports: typing.Mapping[str, int],
    authkey: bytes,
    *,
    backend: str = SIMULATED_BACKEND,
    plugin_names: Sequence[str] = (),
    orphan_grace_s: float = _ORPHAN_GRACE_S,
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
    orphan_grace_s : float
        Seconds with no client connections before the server parks the
        instrument and exits. See :func:`_orphan_watchdog`.

    Raises
    ------
    SystemExit
        With :data:`PORT_UNAVAILABLE_EXIT_STATUS` when a listener
        cannot bind, so the client can retry with fresh ports.
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
        try:
            listeners = [
                _Listener(("localhost", ports[name]), authkey=authkey)
                for name in names
            ]
        except OSError as error:
            _LOGGER.error(  # noqa: TRY400 - a traceback adds nothing here
                "could not bind a listener (%s); the client will retry with "
                "fresh ports",
                error,
            )
            raise SystemExit(PORT_UNAVAILABLE_EXIT_STATUS) from error
        _LOGGER.info(
            "serving %s on ports %s",
            ", ".join(names),
            ", ".join(str(ports[name]) for name in names),
        )
        with _parking_signal_handlers(session):
            threading.Thread(
                target=_orphan_watchdog,
                args=(session, orphan_grace_s),
                name="orphan-watchdog",
                daemon=True,
            ).start()
            for listener, name in zip(listeners, names, strict=True):
                thread = threading.Thread(
                    target=_accept_loop,
                    args=(
                        listener,
                        name,
                        session.targets[name],
                        session.writers[name],
                        session,
                    ),
                    daemon=True,
                )
                thread.start()
            session.stop_event.wait()
        _LOGGER.info("shutdown acknowledged; closing listeners and exiting")
        for listener in listeners:
            listener.close()


def _orphan_grace_from_hook() -> float:
    """
    Return the orphan grace period, shortened only by an armed test hook.

    A test cannot wait 30 s to prove the watchdog works, and the value
    must not be settable in an operator's environment - shortening it
    there would park instruments during ordinary connection churn. So
    it rides the same --enable-test-hooks gate as the wedge hooks.

    Returns
    -------
    float
        The configured grace period in seconds.
    """
    raw = _test_hook(_ORPHAN_GRACE_ENV_VAR)
    if raw is None:
        return _ORPHAN_GRACE_S
    try:
        return float(raw)
    except ValueError:
        _LOGGER.warning("ignoring unparseable %s=%r", _ORPHAN_GRACE_ENV_VAR, raw)
        return _ORPHAN_GRACE_S


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
    parser.add_argument(
        "--enable-test-hooks",
        action="store_true",
        help=argparse.SUPPRESS,  # not an operator-facing option
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
    if arguments.enable_test_hooks:
        global _TEST_HOOKS_ENABLED  # noqa: PLW0603 - process-wide by nature
        _TEST_HOOKS_ENABLED = True
        _LOGGER.warning("test hooks enabled; this is not a production launch")
    ports = dict(zip(_TARGET_NAMES, arguments.ports, strict=True))
    authkey = bytes.fromhex(os.environ[_AUTHKEY_ENV_VAR])
    try:
        serve(
            ports,
            authkey,
            backend=arguments.backend,
            plugin_names=arguments.plugin,
            orphan_grace_s=_orphan_grace_from_hook(),
        )
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
