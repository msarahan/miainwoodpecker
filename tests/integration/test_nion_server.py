"""
Integration tests: nion_server's in-process device logic against usim.

Imports ``nion_server`` directly rather than going through
:mod:`miainwoodpecker.devices.remote`, so this validates the underlying
device wrapping itself (GPL-3.0-encumbered, dev/test-only — never
imported by the shipped application). See
``tests/integration/test_remote_nion.py`` for the same devices exercised
over the actual IPC boundary the application uses.

Two things here are deliberately more than "does the wrapper work":

- The **hardware backend's discovery machinery** is exercised for real, by
  pointing it at the usim plug-in as a stand-in vendor plug-in. That
  covers the whole path (import a ``nionswift_plugin`` module → call its
  ``run()`` → read the ``nion.utils.Registry`` components it registered →
  wrap the device objects), leaving only *which* plug-in a real
  instrument ships untested. The no-hardware-present error path is
  tested too, since that is the behaviour a misconfigured instrument
  computer actually hits.
- The **instrument controls** are checked for *effect on data*, not just
  for a successful setter and a matching read-back. usim has controls
  that accept a value and then quietly ignore it outside the full
  ``HardwareSource``/``Application`` layer (docs/migration-plan.md, §7,
  on ``probe_position``), so read-back alone would prove nothing.
  Thresholds here come from ``scripts/device_control_verification.py``.

Skipped automatically unless the ``device`` optional dependency group is
installed (``uv run --extra device --extra tests pytest tests/integration``).
"""

import typing

import numpy as np
import pytest

pytest.importorskip("nion.usim_device", reason="requires the 'device' extra")

from miainwoodpecker.devices import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    IMAGE_READOUT,
    PROJECTED_READOUT,
    STAGE_POSITION_CONTROL,
    Camera,
    CameraParameters,
    Instrument,
    ScanParameters,
    Scanner,
)
from miainwoodpecker.devices.nion_server import (
    HARDWARE_BACKEND,
    SIMULATED_BACKEND,
    _SUM_PROJECT_PROCESSING,
    HardwareNotAvailableError,
    NionCamera,
    NionScanner,
    _axis_calibration_spec,
    _camera_base,
    _instrument_state,
    _parse_args,
    hardware_instrument,
    open_instrument,
    simulated_instrument,
)
from miainwoodpecker.devices.rpc import TARGET_NAMES
from miainwoodpecker.storage.calibration import (
    AxisKind,
    FrameCalibration,
    resolve_frame_calibration,
)

# The usim plug-in, used as a stand-in for a vendor hardware plug-in so the
# discovery path itself is covered without a microscope.
_STANDIN_HARDWARE_PLUGIN = "usim"

# Bounds on usim's default defocus (C10 = 500nm): wide enough to be a
# sanity check rather than a hard-coded expectation, tight enough that a
# metres/nanometres mix-up (a factor of 1e9) cannot slip through.
_MIN_PLAUSIBLE_DEFOCUS_NM = 1.0
_MAX_PLAUSIBLE_DEFOCUS_NM = 1e5
# A control's effect must exceed the shot-noise floor by this factor.
_MIN_EFFECT_RATIO = 3.0
# The simulated sample's mean signal at the origin is ~0.55 counts.
_MIN_ON_SAMPLE_MEAN = 0.1
# Off the sample, the mean must fall to well under half of that.
_MAX_OFF_SAMPLE_FRACTION = 0.5
# A blanked Ronchigram frame's mean is ~0.004 counts against ~11840.
_MIN_UNBLANKED_MEAN = 1.0
_MAX_BLANKED_FRACTION = 0.01


def test_simulated_devices_satisfy_the_protocols():
    """The adapted usim devices are recognized by the runtime-checkable protocols."""
    with simulated_instrument() as microscope:
        assert isinstance(microscope.ronchigram_camera, Camera)
        assert isinstance(microscope.eels_camera, Camera)
        assert isinstance(microscope.scanner, Scanner)


def test_camera_round_trip_produces_a_2d_frame():
    """Start/acquire/stop against the simulated Ronchigram camera."""
    with simulated_instrument() as microscope:
        camera = microscope.ronchigram_camera
        assert camera.camera_id == "usim_ronchigram_camera"
        camera.start()
        try:
            frame = camera.acquire_frame()
        finally:
            camera.stop()
        expected_ndim = 2
        assert frame.data.ndim == expected_ndim
        assert frame.timestamp.tzinfo is not None
        assert frame.metadata["frame_number"] >= 1


def test_scanner_honors_non_square_shape_and_reports_channel():
    """A non-square scan pins the (height, width) convention through the adapter."""
    with simulated_instrument() as microscope:
        scanner = microscope.scanner
        assert "HAADF" in scanner.channel_names
        parameters = ScanParameters(
            height=32,
            width=48,
            pixel_time_us=1.0,
            fov_nm=microscope.stage_size_nm * 0.1,
        )
        frame = scanner.scan_frame(parameters, channel=0)
        assert frame.data.shape == parameters.shape
        assert frame.metadata["channel_name"] == "HAADF"
        assert frame.metadata["fov_nm"] == parameters.fov_nm


# --------------------------------------- the simultaneous multi-channel scan
#
# One pass of the probe, every enabled detector reading out at once, is
# what a scanned instrument *is* (docs/vendor-support.md). These tests are
# against the vendor device itself rather than a stand-in, because the
# whole claim ``scan_frames`` makes is about what the *device* was asked
# to do: a shared ``scan_pass_id`` that some emulation layer could have
# minted around two separate scans would be exactly the fiction the
# ``Frame`` docstring spent three phases refusing.

# Small enough that the scan-box simulator's per-pixel timing keeps these
# tests to milliseconds; non-square so the (height, width) convention is
# under test here too.
_PASS_PARAMETERS = ScanParameters(
    height=16,
    width=24,
    pixel_time_us=1.0,
    fov_nm=40.0,
)
_TWO_CHANNELS = 2


class _CountingScanDevice:
    """
    A pass-through around Nion's scan device that counts what it was asked.

    Everything is delegated to the real ``nion.device_kit.ScanDevice``,
    so the adapter under test drives the genuine vendor object; the only
    additions are counters on the two entry points that mean "acquire".
    ``start_frame`` is the one that matters for dose — it is what begins
    a traversal of the specimen — and its return value is the device's
    own frame number, which is the device saying, in its own bookkeeping,
    how many frames it thinks it has run.

    Parameters
    ----------
    device : object
        The vendor scan device to wrap.
    """

    def __init__(self, device: object) -> None:
        self._device = device
        self.started_frames: list[int] = []
        self.generated_scans = 0

    def __getattr__(self, name: str) -> object:
        """
        Delegate everything not overridden to the wrapped vendor device.

        Parameters
        ----------
        name : str
            Attribute being looked up.

        Returns
        -------
        object
            The vendor device's attribute.
        """
        return getattr(self._device, name)

    def start_frame(self, is_continuous: bool) -> int:  # noqa: FBT001 - vendor signature
        """
        Begin one frame, recording the frame number the device gives back.

        Parameters
        ----------
        is_continuous : bool
            The vendor's continuous-acquisition flag.

        Returns
        -------
        int
            The device's frame number for the frame just started.
        """
        number = self._device.start_frame(is_continuous)
        self.started_frames.append(int(number))
        return number

    def get_scan_data(self, frame_parameters: object, channel: object) -> np.ndarray:
        """
        Synthesise one channel's data, counting the call.

        Parameters
        ----------
        frame_parameters : object
            The vendor scan parameters.
        channel : object
            Channel index or channel object.

        Returns
        -------
        np.ndarray
            The simulated frame.
        """
        self.generated_scans += 1
        return self._device.get_scan_data(frame_parameters, channel)


def test_a_two_channel_pass_asks_the_device_for_exactly_one_scan():
    """
    The dose accounting, asserted against the device's own frame counter.

    This is the entire point of the call: two channels must cost *one*
    pass over the specimen, not two. Two independent witnesses are used
    because a single call count could be satisfied by an adapter that
    scanned twice and reported once — the device is asked to begin a
    frame exactly once, and its own frame number (which it hands back
    from ``start_frame``) advances by one per two-channel pass rather
    than by one per channel.

    The single-channel path is counted alongside as the contrast: it
    reaches the vendor through ``get_scan_data``, which is one
    synthesised traversal per call, so the naive way of getting two
    channels really is two of them.
    """
    with simulated_instrument() as microscope:
        device = _CountingScanDevice(microscope.scanner._device)  # noqa: SLF001
        scanner = NionScanner(device, None)

        first = scanner.scan_frames(_PASS_PARAMETERS, [0, 1])
        second = scanner.scan_frames(_PASS_PARAMETERS, [0, 1])

        assert len(first) == _TWO_CHANNELS
        assert len(second) == _TWO_CHANNELS
        assert len(device.started_frames) == _TWO_CHANNELS  # two passes, not four
        assert device.started_frames[1] == device.started_frames[0] + 1
        assert device.generated_scans == 0  # the pass path never goes there

        scanner.scan_frame(_PASS_PARAMETERS, channel=0)
        scanner.scan_frame(_PASS_PARAMETERS, channel=1)
        assert device.generated_scans == _TWO_CHANNELS


def test_two_channels_of_one_pass_share_an_identity_and_agree_on_geometry():
    """
    What the shared pass id has to mean for DPC or iDPC to be valid.

    Both halves matter. The *identity* says these two frames came from
    one traversal, so a difference taken between them is a difference at
    the same probe position. The *geometry* is what makes that difference
    computable at all: same shape, same field of view, same rotation and
    centre, so pixel (i, j) of one names the same place as pixel (i, j)
    of the other. A pair agreeing on one but not the other would be
    quietly useless.

    The arrays themselves must be separate objects, since a downstream
    subtraction that turned out to be a frame minus itself would produce
    a perfectly plausible field of zeros.
    """
    with simulated_instrument() as microscope:
        frames = microscope.scanner.scan_frames(_PASS_PARAMETERS, [0, 1])

        assert len(frames) == _TWO_CHANNELS
        haadf, maadf = frames
        assert haadf.metadata["scan_pass_id"] == maadf.metadata["scan_pass_id"]
        assert haadf.metadata["scan_pass_id"]  # a real value, not an empty string
        assert haadf.metadata["simultaneous_channels"] == [0, 1]
        assert maadf.metadata["simultaneous_channels"] == [0, 1]
        # Requested order, and each frame still says which channel it is.
        assert haadf.metadata["channel_index"] == 0
        assert maadf.metadata["channel_index"] == 1
        assert haadf.metadata["channel_name"] == "HAADF"
        assert maadf.metadata["channel_name"] == "MAADF"
        # One pass, one clock.
        assert haadf.timestamp == maadf.timestamp
        for key in ("fov_nm", "fov_size_nm", "pixel_time_us", "rotation_rad",
                    "center_nm", "line_time_us", "frame_time_s"):
            assert haadf.metadata[key] == maadf.metadata[key]
        assert haadf.data.shape == _PASS_PARAMETERS.shape
        assert maadf.data.shape == _PASS_PARAMETERS.shape
        assert haadf.data is not maadf.data


def test_a_second_pass_is_a_second_identity():
    """
    Two calls are two traversals, so they must not share an id.

    The failure this rules out is the one that would be least visible: a
    pass id minted once per device or per session would group every frame
    ever acquired, and every difference computed across two of them would
    silently be a difference across drift.
    """
    with simulated_instrument() as microscope:
        scanner = microscope.scanner
        first = scanner.scan_frames(_PASS_PARAMETERS, [0, 1])
        second = scanner.scan_frames(_PASS_PARAMETERS, [0, 1])
        assert (
            first[0].metadata["scan_pass_id"] != second[0].metadata["scan_pass_id"]
        )


def test_a_single_channel_scan_carries_no_pass_identity():
    """
    ``scan_frame`` fabricates nothing, and a pass of one is still honest.

    The rule the ``Frame`` docstring now states: the identity exists
    where a call established it and nowhere else. A ``scan_frame`` frame
    has no siblings, so it carries neither key — a reader finding a pass
    id on it would have to conclude some other frame shares its probe
    positions, and none does.

    A one-channel ``scan_frames`` is the interesting boundary: that
    genuinely *was* a pass, so it does carry the identity, and its
    sibling list names only itself. Nothing is claimed that did not
    happen either way.
    """
    with simulated_instrument() as microscope:
        scanner = microscope.scanner
        single = scanner.scan_frame(_PASS_PARAMETERS, channel=0)
        assert "scan_pass_id" not in single.metadata
        assert "simultaneous_channels" not in single.metadata

        (alone,) = scanner.scan_frames(_PASS_PARAMETERS, [0])
        assert alone.metadata["scan_pass_id"]
        assert alone.metadata["simultaneous_channels"] == [0]


def test_a_pass_rejects_channels_the_scanner_does_not_have():
    """
    An impossible request fails, and the device is fine afterwards.

    ``IndexError`` deliberately, because that is what the single-channel
    path already raises for the same operator mistake (a bad channel
    index reaching Nion's own channel list), so an adapter-conformance
    suite or a caller branching on the error kind sees one vocabulary
    rather than two. The message names the range, since "99 is not a
    channel" is not actionable if the caller cannot see what is.

    The recovery half is not decoration: this call enables and disables
    detector channels on the device, so a request that dies partway
    through must not leave the scan unit configured for a scan nobody
    asked for.
    """
    with simulated_instrument() as microscope:
        scanner = microscope.scanner
        before = tuple(microscope.scanner._device.channels_enabled)  # noqa: SLF001

        with pytest.raises(IndexError, match=r"channels 0\.\.3"):
            scanner.scan_frames(_PASS_PARAMETERS, [0, 99])

        assert tuple(microscope.scanner._device.channels_enabled) == before  # noqa: SLF001
        recovered = scanner.scan_frames(_PASS_PARAMETERS, [0, 1])
        assert len(recovered) == _TWO_CHANNELS


def test_a_pass_rejects_a_request_no_scanner_could_honour():
    """
    No channels, or the same channel twice, is a caller bug rather than a scan.

    ``ValueError`` rather than ``IndexError`` because these are not about
    which channels exist: zero channels asks for no readout at all, and a
    detector cannot be read out twice during one traversal — a duplicate
    would have to be answered with two copies of one frame, which is a
    claim about dose that nothing in the device supports.
    """
    with simulated_instrument() as microscope:
        scanner = microscope.scanner
        with pytest.raises(ValueError, match="at least one channel"):
            scanner.scan_frames(_PASS_PARAMETERS, [])
        with pytest.raises(ValueError, match="duplicate channels"):
            scanner.scan_frames(_PASS_PARAMETERS, [1, 1])


def test_a_pass_restores_the_channels_the_device_had_enabled():
    """
    Enabling channels for a pass is borrowed, not taken.

    The device's enabled-channel set is instrument state that outlives
    this call: it is what a subsequent vendor-level operation (Nion
    Swift's own live view, on a shared instrument PC) would scan. A
    ``scan_frames`` that left its own selection behind would silently
    change what someone else's next acquisition recorded.
    """
    with simulated_instrument() as microscope:
        device = microscope.scanner._device  # noqa: SLF001
        before = tuple(device.channels_enabled)
        microscope.scanner.scan_frames(_PASS_PARAMETERS, [1, 2])
        assert tuple(device.channels_enabled) == before


def _mean_abs_diff(first: np.ndarray, second: np.ndarray) -> float:
    """Return the mean absolute difference between two frames."""
    return float(np.mean(np.abs(first.astype(np.float64) - second.astype(np.float64))))


def _camera_frame(camera) -> np.ndarray:
    """Acquire one frame, leaving the camera stopped."""
    camera.start()
    try:
        return camera.acquire_frame().data
    finally:
        camera.stop()


# ------------------------------------------------------- command line
#
# The documented precedence is "each defaulting to an environment
# variable, with the command line winning" (docs/migration-plan.md, §5
# Phase 1). Asserted rather than assumed, because the natural argparse
# spelling of it is silently wrong: ``action="append"`` appends to its
# default instead of replacing it, so seeding that default from the
# environment made ``--plugin foo`` mean "the environment's plug-ins *and*
# foo". On a hardware backend that loads vendor plug-ins nobody asked for.

# Derived from the protocol rather than written out: the server takes one
# positional port per target name, so a hard-coded list silently becomes
# an argparse error the day a target is added - which is exactly what
# happened when the neutral "camera" target arrived.
_PORTS = [str(5001 + index) for index in range(len(TARGET_NAMES))]


def test_named_plugins_override_the_environment(monkeypatch):
    """``--plugin`` replaces ``$MIAINWOODPECKER_HARDWARE_PLUGINS``, not adds to it."""
    monkeypatch.setenv("MIAINWOODPECKER_HARDWARE_PLUGINS", "from_environment")
    arguments = _parse_args(["--plugin", "from_command_line", *_PORTS])
    assert arguments.plugin == ["from_command_line"]


def test_repeated_plugin_flags_accumulate(monkeypatch):
    """Repeating the flag is still how you ask for several plug-ins."""
    monkeypatch.delenv("MIAINWOODPECKER_HARDWARE_PLUGINS", raising=False)
    arguments = _parse_args(["--plugin", "first", "--plugin", "second", *_PORTS])
    assert arguments.plugin == ["first", "second"]


def test_plugins_fall_back_to_the_environment(monkeypatch):
    """With no flag, the comma-separated environment variable is used."""
    monkeypatch.setenv("MIAINWOODPECKER_HARDWARE_PLUGINS", "one, two")
    arguments = _parse_args(_PORTS)
    assert arguments.plugin == ["one", "two"]


def test_plugins_default_to_autodiscovery(monkeypatch):
    """Neither flag nor environment means an empty list, i.e. autodiscovery."""
    monkeypatch.delenv("MIAINWOODPECKER_HARDWARE_PLUGINS", raising=False)
    arguments = _parse_args(_PORTS)
    assert arguments.plugin == []


def test_backend_flag_overrides_the_environment(monkeypatch):
    """The same precedence for --backend, which argparse gets right on its own."""
    monkeypatch.setenv("MIAINWOODPECKER_BACKEND", HARDWARE_BACKEND)
    assert _parse_args(_PORTS).backend == HARDWARE_BACKEND
    overridden = _parse_args(["--backend", SIMULATED_BACKEND, *_PORTS])
    assert overridden.backend == SIMULATED_BACKEND


# ---------------------------------------------------------------- backends


def test_open_instrument_dispatches_the_simulated_backend():
    """The selector's simulated path is the unchanged usim construction."""
    with open_instrument(SIMULATED_BACKEND) as microscope:
        assert microscope.ronchigram_camera.camera_id == "usim_ronchigram_camera"
        assert microscope.eels_camera.camera_id == "usim_eels_camera"
        assert microscope.scanner.scanner_id == "usim_scan_device"


def test_open_instrument_defaults_to_the_simulated_backend():
    """No argument means the simulator, so existing callers are unaffected."""
    with open_instrument() as microscope:
        assert microscope.ronchigram_camera.camera_id == "usim_ronchigram_camera"


def test_open_instrument_rejects_an_unknown_backend():
    """A typo in the backend name fails immediately and names the valid ones."""
    with pytest.raises(ValueError, match="unknown backend"):
        open_instrument("hardwear")


def test_hardware_backend_reports_no_hardware_actionably():
    """
    With no vendor plug-in installed, the real path fails clearly.

    This is the behaviour that *can* be verified without a microscope, and
    the first thing a misconfigured instrument computer will hit, so the
    message is asserted to name a way forward rather than just to exist.
    """
    with pytest.raises(HardwareNotAvailableError) as failure, hardware_instrument():
        pass  # pragma: no cover - construction must not get this far
    message = str(failure.value)
    assert "no Nion hardware found" in message
    assert "stem_controller" in message
    assert "MIAINWOODPECKER_HARDWARE_PLUGINS" in message
    assert f"--backend={SIMULATED_BACKEND}" in message


def test_hardware_backend_discovers_devices_from_a_named_plugin():
    """
    The real discovery path works, driven with usim as a stand-in plug-in.

    Covers importing a ``nionswift_plugin`` module, calling its ``run()``,
    reading back the ``stem_controller``/``scan_module``/``camera_module``
    components it registered, and classifying the cameras by their
    ``camera_type``. Only the vendor plug-in's identity is left untested.
    """
    with hardware_instrument([_STANDIN_HARDWARE_PLUGIN]) as microscope:
        assert microscope.ronchigram_camera.camera_id == "usim_ronchigram_camera"
        assert microscope.eels_camera.camera_id == "usim_eels_camera"
        assert microscope.scanner.scanner_id == "usim_scan_device"
        assert isinstance(microscope.instrument, Instrument)
        assert microscope.stage_size_nm > 0


def test_hardware_backend_unregisters_the_plugin_on_the_way_out():
    """
    Teardown calls the plug-in's ``stop()``, so a second attempt starts clean.

    Without this, the registry would still hold the first run's components
    and the "no hardware" error path would stop being reachable.
    """
    with hardware_instrument([_STANDIN_HARDWARE_PLUGIN]) as microscope:
        assert microscope.scanner.scanner_id == "usim_scan_device"
    with pytest.raises(HardwareNotAvailableError), hardware_instrument():
        pass  # pragma: no cover - must not get this far


def test_open_instrument_dispatches_the_hardware_backend():
    """The selector routes plug-in names through to the hardware path."""
    with open_instrument(HARDWARE_BACKEND, [_STANDIN_HARDWARE_PLUGIN]) as microscope:
        assert microscope.scanner.scanner_id == "usim_scan_device"


# -------------------------------------------------------------- controls


def test_instrument_reports_the_controls_usim_actually_has():
    """All four neutral controls resolve to real usim controls."""
    with simulated_instrument() as microscope:
        controls = set(microscope.instrument.available_controls())
        assert controls == {
            STAGE_POSITION_CONTROL,
            DEFOCUS_CONTROL,
            BEAM_BLANKER_CONTROL,
            ENERGY_OFFSET_CONTROL,
        }


def test_defocus_round_trips_in_nanometres():
    """Defocus is set and read back in operator units, not the vendor's metres."""
    with simulated_instrument() as microscope:
        instrument = microscope.instrument
        original = instrument.defocus_nm()
        # usim's default C10 is 500nm; a metres/nanometres mix-up would
        # show up here as a 1e9 error, not a rounding one.
        assert _MIN_PLAUSIBLE_DEFOCUS_NM < original < _MAX_PLAUSIBLE_DEFOCUS_NM
        instrument.set_defocus_nm(1234.0)
        assert instrument.defocus_nm() == pytest.approx(1234.0)
        instrument.set_defocus_nm(original)
        assert instrument.defocus_nm() == pytest.approx(original)


def test_defocus_actually_changes_camera_data():
    """
    A defocus change moves the Ronchigram well beyond the shot-noise floor.

    Measured ratio is ~6x (scripts/device_control_verification.py); 3x is
    asserted so noise cannot pass this vacuously and a genuinely dropped
    control cannot pass it at all.
    """
    with simulated_instrument() as microscope:
        camera = microscope.ronchigram_camera
        instrument = microscope.instrument
        original = instrument.defocus_nm()
        first = _camera_frame(camera)
        noise_floor = _mean_abs_diff(first, _camera_frame(camera))
        instrument.set_defocus_nm(original + 2500.0)
        try:
            changed = _mean_abs_diff(first, _camera_frame(camera))
        finally:
            instrument.set_defocus_nm(original)
        assert changed > _MIN_EFFECT_RATIO * noise_floor


def test_stage_position_round_trips_and_moves_the_scanned_field():
    """
    Moving the stage a whole stage-width scans empty vacuum instead of sample.

    The pixel-by-pixel difference is a weak signal here (usim's sample
    features are sparse, so the measured ratio is only ~2.2x), but the
    frame *mean* collapses from ~0.55 to ~0.0 because the field of view
    has left the simulated sample entirely — an unambiguous effect, and
    the reason this asserts on the mean.
    """
    with simulated_instrument() as microscope:
        instrument = microscope.instrument
        parameters = ScanParameters(
            height=128,
            width=128,
            pixel_time_us=1.0,
            fov_nm=microscope.stage_size_nm * 0.1,
        )
        origin = instrument.stage_position_nm()
        on_sample = float(np.mean(microscope.scanner.scan_frame(parameters).data))
        offset = microscope.stage_size_nm
        instrument.set_stage_position_nm(origin[0] + offset, origin[1] + offset)
        try:
            moved = instrument.stage_position_nm()
            assert moved == pytest.approx((origin[0] + offset, origin[1] + offset))
            off_sample = float(np.mean(microscope.scanner.scan_frame(parameters).data))
        finally:
            instrument.set_stage_position_nm(*origin)
        assert on_sample > _MIN_ON_SAMPLE_MEAN
        assert abs(off_sample) < _MAX_OFF_SAMPLE_FRACTION * on_sample


def test_beam_blanker_round_trips_and_blanks_the_ronchigram():
    """
    Blanking the beam zeroes the Ronchigram camera, not merely a flag.

    usim's RonchigramCameraSimulator checks ``value_manager.is_blanked``
    before plotting any sample features, so a blanked frame really is
    empty: the measured mean goes from ~11840 counts to ~0.004.
    """
    with simulated_instrument() as microscope:
        instrument = microscope.instrument
        camera = microscope.ronchigram_camera
        assert not instrument.is_beam_blanked()
        unblanked_mean = float(np.mean(_camera_frame(camera)))
        instrument.set_beam_blanked(blanked=True)
        try:
            assert instrument.is_beam_blanked()
            blanked_mean = float(np.mean(_camera_frame(camera)))
        finally:
            instrument.set_beam_blanked(blanked=False)
        assert unblanked_mean > _MIN_UNBLANKED_MEAN
        assert blanked_mean < _MAX_BLANKED_FRACTION * unblanked_mean
        assert not instrument.is_beam_blanked()


def test_park_blanks_the_beam():
    """The park hook leaves the instrument in the state a teardown wants it in."""
    with simulated_instrument() as microscope:
        instrument = microscope.instrument
        assert not instrument.is_beam_blanked()
        instrument.park()
        assert instrument.is_beam_blanked()


# ------------------------------------------------- calibration from the
#                                                    instrument
#
# A Nion camera publishes no calibration *values*. It publishes a
# ``calibration_controls`` mapping naming the instrument controls that
# hold them, which ``camera_base.build_calibration`` resolves at
# acquisition time - because a camera's angular scale depends on the
# projector lenses, so it is instrument state rather than a device
# constant. These pin what usim actually reports through that path, which
# is what the migration plan's §7 "nothing feeds calibration from the
# instrument" gap needed.


class _FakeCalibration:
    """The two fields ``_axis_calibration_spec`` reads off a nion Calibration."""

    def __init__(self, scale: float, units: str, offset: float = 0.0) -> None:
        self.scale = scale
        self.offset = offset
        self.units = units


class _ControllerWithoutControls:
    """A STEM controller that reports no control this project asks for."""

    def TryGetVal(  # noqa: N802 - vendor spelling
        self,
        name: str,  # noqa: ARG002 - part of the vendor signature being stood in for
    ) -> tuple[bool, None]:
        """
        Report every control as absent, the way a bare STEMController does.

        Parameters
        ----------
        name : str
            The vendor control name, ignored — every one is absent here.

        Returns
        -------
        tuple[bool, None]
            Always ``(False, None)``.
        """
        return (False, None)


class _CameraWithoutControls:
    """A camera device that publishes no calibration controls at all."""

    def __init__(self) -> None:
        self.calibration_controls = {}


def _frame_calibration(camera) -> tuple[FrameCalibration, tuple[int, ...]]:
    """Acquire one frame and resolve the calibration its metadata carries."""
    camera.start()
    try:
        frame = camera.acquire_frame()
    finally:
        camera.stop()
    calibration = resolve_frame_calibration(frame.data.shape, metadata=frame.metadata)
    return calibration, frame.data.shape


def test_the_ronchigram_camera_reports_angular_axes():
    """
    Both Ronchigram axes arrive calibrated in radians, centred on the optic axis.

    The centring is not this project's convention imposed on the data: the
    offset comes back from the instrument's own ``ronchigram_x_offset``
    control, and it equals ``-scale * n / 2`` because that is what an axis
    through the optic axis means. Measured against usim: 2048 pixels at
    ~9.83e-05 rad each, offset ~-0.1007 rad.
    """
    with simulated_instrument() as microscope:
        calibration, shape = _frame_calibration(microscope.ronchigram_camera)
        for name, length in zip(("y", "x"), shape, strict=True):
            axis = calibration.axis(name)
            assert axis.kind is AxisKind.ANGLE
            assert axis.units == "rad"
            assert axis.scale > 0.0
            assert axis.offset == pytest.approx(-axis.scale * length / 2.0)


def test_the_eels_camera_reports_energy_on_the_axis_the_device_names():
    """
    The dispersive axis is *reported*, not defaulted.

    usim's EELS camera publishes ``eels_x_scale``/``eels_x_offset`` with
    units eV and leaves the slow axis's units empty - the device's way of
    saying that direction is not calibrated. Both halves matter: an
    honest uncalibrated y axis is what stops a spectrum being flattened
    along the wrong direction, and ``energy_axis_name`` is what the
    HyperSpy adapter asks.
    """
    with simulated_instrument() as microscope:
        calibration, _shape = _frame_calibration(microscope.eels_camera)
        assert calibration.x.kind is AxisKind.ENERGY
        assert calibration.x.units == "eV"
        assert calibration.x.scale > 0.0
        assert calibration.y.kind is AxisKind.UNCALIBRATED
        assert calibration.energy_axis_name() == "x"


def test_a_camera_with_no_controls_yields_an_uncalibrated_frame():
    """
    Missing controls produce an uncalibrated axis, not an error.

    This is the failure mode that matters more than the success one: a
    vendor camera that publishes no ``calibration_controls``, or names
    controls this instrument does not have, must still acquire. Nion's own
    ``test_calibrator_with_missing_controls`` asserts the same thing one
    layer down.
    """
    # No instrument reference is needed for either case, and that is the
    # point: both short-circuit before anything would be read off one.
    unusable_controller = object()
    assert (
        NionCamera(_CameraWithoutControls(), unusable_controller).calibration_metadata(
            (16, 16),
        )
        is None
    )
    # And a camera wrapped without an instrument at all, which is how the
    # in-process helpers may construct one.
    assert NionCamera(_CameraWithoutControls()).calibration_metadata((16, 16)) is None


def test_binning_multiplies_the_calibration_scale():
    """
    A binned pixel spans proportionally more of the axis.

    This is why binning and calibration are one piece of work rather than
    two: adding binning without threading it through here would write axes
    wrong by an integer factor, silently.
    """
    with simulated_instrument() as microscope:
        camera = microscope.ronchigram_camera
        unbinned = camera.calibration_metadata((2048, 2048))
        binned = camera.calibration_metadata((1024, 1024), binning=2.0)
        assert binned["x"]["scale"] == pytest.approx(2.0 * unbinned["x"]["scale"])


def test_an_axis_in_units_this_project_cannot_express_stays_uncalibrated():
    """
    An unrecognized unit degrades to pixels rather than propagating.

    Nion's calibration vocabulary is open (its own tests use ``"rad-old"``,
    ``"counts-old2"``); this project's is a short closed list chosen so an
    axis kind is recoverable from its units alone. A scale in units
    nothing downstream can interpret is worth less than an admitted pixel
    axis, so it is dropped here rather than raised at the writer.
    """
    assert _axis_calibration_spec(_FakeCalibration(scale=2.0, units="rad-old")) is None
    assert _axis_calibration_spec(_FakeCalibration(scale=2.0, units="")) is None
    assert _axis_calibration_spec(_FakeCalibration(scale=0.0, units="eV")) is None
    assert _axis_calibration_spec(_FakeCalibration(scale=2.0, units="eV")) == {
        "kind": "energy",
        "scale": 2.0,
        "offset": 0.0,
        "units": "eV",
    }


# ------------------------------------------- the frame metadata contract
#
# Nion has two tests whose entire purpose is to enumerate what must be
# attached to every acquired frame
# (CameraControl_test.test_acquire_attaches_required_metadata and
# ScanControl_test.test_context_scan_attaches_required_metadata). The set
# is theirs; the names are ours, for the reason interface.py gives. Before
# this, a scan frame carried four keys and a camera frame carried two.

# usim's instrument: EHT = 100 kV, C10 = 5e-07 m, BeamCurrent = 2e-10 A.
_EXPECTED_HIGH_TENSION_V = 100000.0
_EXPECTED_DEFOCUS_NM = 500.0


def _assert_instrument_state(metadata) -> None:
    """Assert the instrument state every frame carries, in operator units."""
    assert metadata["high_tension_v"] == pytest.approx(_EXPECTED_HIGH_TENSION_V)
    # In nanometres, not the vendor's metres. A 1e9 error here is the one
    # the hardware checklist calls the highest-consequence check on it.
    assert metadata["defocus_nm"] == pytest.approx(_EXPECTED_DEFOCUS_NM)
    assert metadata["beam_current_a"] > 0.0


def test_a_scan_frame_carries_the_required_metadata():
    """A scan frame says which detector, which region, and under what beam."""
    with simulated_instrument() as microscope:
        parameters = ScanParameters(
            height=32,
            width=48,
            pixel_time_us=2.0,
            fov_nm=microscope.stage_size_nm * 0.1,
        )
        metadata = microscope.scanner.scan_frame(parameters, channel=0).metadata
        assert metadata["device_id"] == microscope.scanner.scanner_id
        assert metadata["channel_index"] == 0
        assert metadata["channel_name"] == "HAADF"
        assert metadata["fov_nm"] == parameters.fov_nm
        assert metadata["pixel_time_us"] == parameters.pixel_time_us
        # Derived rather than left to a reader, which cannot know the
        # flyback convention - so the convention is recorded beside them.
        assert metadata["line_time_us"] == pytest.approx(48 * 2.0)
        assert metadata["frame_time_s"] == pytest.approx(32 * 48 * 2.0 / 1e6)
        assert metadata["flyback_time_us"] > 0.0
        # Without these the field of view says how much was scanned but
        # not which region, so two scans of different areas would be
        # indistinguishable in the file.
        assert metadata["rotation_rad"] == pytest.approx(0.0)
        assert metadata["center_nm"] == (0.0, 0.0)
        _assert_instrument_state(metadata)


def test_a_camera_frame_carries_the_required_metadata():
    """A camera frame names the detector and the beam it was taken under."""
    with simulated_instrument() as microscope:
        camera = microscope.eels_camera
        camera.start()
        try:
            metadata = camera.acquire_frame().metadata
        finally:
            camera.stop()
        assert metadata["device_id"] == camera.camera_id
        # The vendor's own label for the detector, which is what tells an
        # analysis tool what kind of data this is.
        assert metadata["camera_type"] == "eels"
        assert metadata["camera_name"]
        assert metadata["counts_per_electron"] > 0.0
        _assert_instrument_state(metadata)


def test_frame_index_counts_without_gaps_from_zero():
    """
    Frames are numbered so a dropped one is visible rather than absent.

    A recording that simply has fewer frames than expected says nothing
    about whether any were lost. A gap in a monotonic index does.
    """
    with simulated_instrument() as microscope:
        camera = microscope.ronchigram_camera
        camera.start()
        try:
            indexes = [camera.acquire_frame().metadata["frame_index"] for _ in range(3)]
        finally:
            camera.stop()
        assert indexes == [0, 1, 2]

        parameters = ScanParameters(
            height=16,
            width=16,
            pixel_time_us=1.0,
            fov_nm=microscope.stage_size_nm * 0.1,
        )
        scanner = microscope.scanner
        # Counted per device, across channels: every frame the scanner
        # produced, in the order it produced them. Nion's scan_id groups
        # the channels of one simultaneous scan instead; this interface
        # has no such call, so a second channel really is a second pass of
        # the beam and gets its own index rather than a shared id.
        scan_indexes = [
            scanner.scan_frame(parameters, channel=channel).metadata["frame_index"]
            for channel in (0, 1, 0)
        ]
        assert scan_indexes == [0, 1, 2]


def test_the_defocus_a_frame_reports_is_its_own():
    """
    Instrument state is read per frame, not cached when the device opened.

    ``focal_series`` changes the defocus *between* frames, so a cached
    value would label every frame in a sweep with the first one's
    defocus — wrong in exactly the workflow that most needs it right.
    """
    with simulated_instrument() as microscope:
        camera = microscope.ronchigram_camera
        instrument = microscope.instrument
        original_nm = instrument.defocus_nm()
        camera.start()
        try:
            first = camera.acquire_frame().metadata["defocus_nm"]
            instrument.set_defocus_nm(original_nm * 2.0)
            second = camera.acquire_frame().metadata["defocus_nm"]
        finally:
            instrument.set_defocus_nm(original_nm)
            camera.stop()
        assert first == pytest.approx(original_nm)
        assert second == pytest.approx(original_nm * 2.0)


def test_an_instrument_that_reports_nothing_contributes_no_keys():
    """
    A control the instrument does not publish is omitted, never defaulted.

    An absent key reads as "not reported"; a stored zero reads as a
    measurement — an instrument with its high tension off, a beam with no
    current. The device layer says nothing rather than something false.
    """
    assert _instrument_state(None) == {}
    assert _instrument_state(_ControllerWithoutControls()) == {}


# ------------------------------------------------- exposure and binning
#
# §7 wanted these done *with* calibration rather than after it, and the
# reason is now mechanical rather than stylistic: binning multiplies the
# calibration scale (build_calibration's relative_scale), so a binning
# control that did not reach the calibration would write axes wrong by an
# integer factor on every binned frame.

_UNSUPPORTED_BINNING = 3
_IMAGE_RANK = 2
# The projection is a float32 sum either way; only the order of the
# additions differs between numpy's reduction and the assertion's.
_PROJECTION_RTOL = 1e-5
# A supported factor big enough that a wrongly-scaled axis is unmistakable.
_TEST_BINNING = 4


def test_binning_values_are_reported_and_not_empty():
    """A camera says what binning it supports, so a caller need not guess."""
    with simulated_instrument() as microscope:
        values = microscope.eels_camera.binning_values
        assert values
        assert list(values) == sorted(values)
        assert values[0] == 1


def test_configuring_before_start_takes_effect_on_the_first_frame():
    """
    Settings applied to a stopped camera are in force from its first frame.

    This is the path a caller who needs a frame taken at known settings
    should use, and the reason it is worth stating: configuring a camera
    that is already running does *not* affect the frame already in
    flight — see the test below.
    """
    with simulated_instrument() as microscope:
        camera = microscope.eels_camera
        binned = camera.configure(
            CameraParameters(exposure_ms=20.0, binning=_TEST_BINNING),
        )
        assert binned.binning == _TEST_BINNING
        assert camera.parameters() == binned
        camera.start()
        try:
            frame = camera.acquire_frame()
        finally:
            camera.stop()
        assert frame.data.shape == camera._device.get_expected_dimensions(_TEST_BINNING)  # noqa: SLF001
        assert frame.metadata["binning"] == _TEST_BINNING
        assert frame.metadata["exposure_ms"] == pytest.approx(20.0)


def test_a_frame_in_flight_keeps_the_settings_it_was_started_with():
    """
    Reconfiguring a running camera does not relabel the frame already begun.

    Nion asserts the same thing one layer up
    (``test_changing_frame_parameters_during_view_does_not_affect_current_acquisition``).
    What makes it matter here is the calibration: binning multiplies the
    axis scale, so labelling an unbinned frame with the new binning would
    put an axis on stored data that is wrong by the whole factor. The
    binning a frame reports is therefore recovered from its shape, not
    from the setting.
    """
    with simulated_instrument() as microscope:
        camera = microscope.eels_camera
        camera.start()
        try:
            unbinned = camera.acquire_frame()
            camera.configure(CameraParameters(exposure_ms=20.0, binning=_TEST_BINNING))
            in_flight = camera.acquire_frame()
            settled = camera.acquire_frame()
        finally:
            camera.stop()
        assert in_flight.data.shape == unbinned.data.shape
        assert in_flight.metadata["binning"] == 1
        assert settled.data.shape != unbinned.data.shape
        assert settled.metadata["binning"] == _TEST_BINNING


def test_binning_multiplies_the_axis_scale_of_the_frame_it_applied_to():
    """
    A binned frame's calibration scale grows with the binning, per frame.

    The whole reason binning and calibration are one piece of work: a
    binned pixel spans proportionally more of the axis, and the frame
    that was *not* binned must not be scaled as though it were.
    """
    with simulated_instrument() as microscope:
        camera = microscope.eels_camera
        camera.start()
        try:
            unbinned = camera.acquire_frame()
            camera.configure(CameraParameters(exposure_ms=20.0, binning=_TEST_BINNING))
            camera.acquire_frame()  # the in-flight frame, still unbinned
            binned = camera.acquire_frame()
        finally:
            camera.stop()
        unbinned_scale = unbinned.metadata["calibration"]["x"]["scale"]
        binned_scale = binned.metadata["calibration"]["x"]["scale"]
        assert binned_scale == pytest.approx(float(_TEST_BINNING) * unbinned_scale)


def test_an_unsupported_binning_is_refused_rather_than_rounded():
    """
    A binning the camera does not advertise raises, naming what it accepts.

    usim's ``validate_frame_parameters`` is a pass-through — measured, it
    returns ``binning=3`` unchanged on a camera advertising [1, 2, 4, 8] —
    so nothing downstream would have caught it. Refused rather than
    rounded to a neighbour, because a caller asking for 3 has a bug and
    silently giving them 2 makes every axis wrong by a third.
    """
    with simulated_instrument() as microscope:
        camera = microscope.eels_camera
        assert _UNSUPPORTED_BINNING not in camera.binning_values
        with pytest.raises(ValueError, match="not supported"):
            camera.configure(
                CameraParameters(exposure_ms=10.0, binning=_UNSUPPORTED_BINNING),
            )
        # And the camera is unchanged, not left half-configured.
        assert camera.parameters().binning == 1


def test_the_plain_acquire_path_does_not_honour_nions_sum_project():
    """
    Measured, not assumed: this is why the server projects and says it did.

    ``processing = "sum_project"`` is the only per-axis reduction Nion's
    camera API has, and the mapping is right — but the device consults it
    only on the sequence/SI paths (``_acquire_sequence``,
    ``CameraTask.start``). ``acquire_image``, which is the whole of what
    this project drives, never reads it and asserts a 2D buffer. Nion's
    own tests say the same in prose ('"sum_project" is for sequence/SI
    only'), and ``camera_base.CameraHardwareSource.__init__`` clears the
    field off view parameters outright.

    This asserts it against the installed simulator rather than trusting
    that prose, because the whole honesty of ``projected_by`` rests on
    it: if a future usim *did* honour it here, the frames would arrive 1D
    and be labelled ``"sensor"``, and this test failing is how anyone
    would find out.
    """
    with simulated_instrument() as microscope:
        device = microscope.eels_camera._device  # noqa: SLF001
        frame_parameters = _camera_base.CameraFrameParameters(
            exposure_ms=10.0,
            binning=1,
            processing=_SUM_PROJECT_PROCESSING,
        )
        validated = device.validate_frame_parameters(frame_parameters)
        # The device keeps the mode - it is not rejected or dropped...
        assert validated.processing == _SUM_PROJECT_PROCESSING
        device.set_frame_parameters(validated)
        device.start_live()
        try:
            element = device.acquire_image()
        finally:
            device.stop_live()
        # ...and then ignores it on this path, returning the full sensor.
        expected_2d = 2
        assert element["data"].ndim == expected_2d
        assert element["data"].shape == device.get_expected_dimensions(1)


def test_a_projected_readout_is_configured_and_reported_back():
    """
    ``configure`` says what the device took, readout included.

    The standing contract, extended to the new field: a caller that needs
    to know what the next frame will be must be able to read it back
    rather than assume the request was honoured — which matters more here
    than for exposure, because this one changes the frame's *rank* and so
    which writer the recording path will use.
    """
    with simulated_instrument() as microscope:
        camera = microscope.eels_camera
        took = camera.configure(
            CameraParameters(exposure_ms=10.0, readout=PROJECTED_READOUT),
        )
        assert took.readout == PROJECTED_READOUT
        assert camera.parameters() == took
        # And back again, so the mode is a setting rather than a latch.
        assert camera.configure(CameraParameters(exposure_ms=10.0)).readout == (
            IMAGE_READOUT
        )


def test_a_projected_readout_produces_a_1d_frame_that_says_who_summed_it():
    """
    The frame is 1D, and its metadata does not claim the sensor did it.

    ``projected_by`` is the whole point of recording this rather than
    just ``readout``: a sensor that sums before readout pays one
    readout's noise and a server summing N rows pays N of them, so a
    recording that implied the first while getting the second would
    misstate its own noise statistics — invisibly, and in exactly the
    direction that flatters the data.
    """
    with simulated_instrument() as microscope:
        camera = microscope.eels_camera
        camera.configure(CameraParameters(exposure_ms=10.0, readout=PROJECTED_READOUT))
        camera.start()
        try:
            frame = camera.acquire_frame()
        finally:
            camera.stop()

    assert frame.data.ndim == 1
    assert frame.data.shape == (camera._device.get_expected_dimensions(1)[1],)  # noqa: SLF001
    assert frame.metadata["readout"] == PROJECTED_READOUT
    assert frame.metadata["projected_by"] == "server"
    assert frame.metadata["binning"] == 1


def test_a_projected_frame_keeps_the_dispersive_axis_the_image_frame_had():
    """
    Verbatim: same dispersion, same offset, same units. The summed axis is gone.

    The correctness claim the storage route depends on. The projection
    removes the non-dispersive direction and nothing else, so an energy
    axis that shifted or rescaled between the two readouts would mean the
    projection had silently reinterpreted the detector — and every eV in
    every projected recording would be wrong with the file still
    perfectly readable.
    """
    with simulated_instrument() as microscope:
        camera = microscope.eels_camera
        camera.configure(CameraParameters(exposure_ms=10.0))
        camera.start()
        try:
            imaged = camera.acquire_frame()
            camera.configure(
                CameraParameters(exposure_ms=10.0, readout=PROJECTED_READOUT),
            )
            # One frame of overlap, as for any other reconfigure.
            camera.acquire_frame()
            projected = camera.acquire_frame()
        finally:
            camera.stop()

    assert imaged.data.ndim == _IMAGE_RANK
    assert projected.data.ndim == 1
    assert projected.metadata["calibration"]["x"] == imaged.metadata["calibration"]["x"]
    # The other axis is not merged into the survivor: it is absent.
    assert "y" not in projected.metadata["calibration"]


class _FixedFrameCamera:
    """A camera device returning one known array, so the sum is checkable."""

    camera_id = "fixed"
    camera_type = "eels"
    binning_values = (1,)
    calibration_controls: typing.ClassVar[dict[str, object]] = {}

    def __init__(self, data) -> None:
        self._data = data
        self.processing = None

    def get_expected_dimensions(self, binning: int) -> tuple[int, int]:
        """
        Return the shape this device produces at the given binning.

        Parameters
        ----------
        binning : int
            The binning factor.

        Returns
        -------
        tuple[int, int]
            The unbinned shape divided by the factor.
        """
        rows, columns = self._data.shape
        return (rows // binning, columns // binning)

    def set_frame_parameters(self, frame_parameters: typing.Any) -> None:  # noqa: ANN401 - a vendor CameraFrameParameters
        """
        Record what was asked for, as a real device would.

        Parameters
        ----------
        frame_parameters : typing.Any
            A vendor ``CameraFrameParameters``.
        """
        self.processing = frame_parameters.processing

    def acquire_image(self) -> dict:
        """
        Return the fixed array, ignoring ``processing`` exactly as usim does.

        Returns
        -------
        dict
            A Nion data element wrapping the fixed array.
        """
        return {"data": self._data, "properties": {}}

    def close(self) -> None:
        """Release nothing; this device owns no threads."""


def test_the_server_side_projection_is_a_sum_along_the_non_dispersive_axis():
    """
    Which axis is summed is Nion's own definition, checked arithmetically.

    ``sum_project`` sums axis 0 and keeps the trailing one — that is what
    ``Core.function_sum(..., 0)`` does in ``_acquire_sequence`` and what
    ``CameraTask.start``'s ``[1:]`` slice of the expected dimensions
    agrees with. Following that definition rather than inventing one is
    what makes the surviving axis the same ``x`` the device calibrates.

    Against a fixed array rather than the simulator, because the
    simulator's counts change frame to frame: comparing a projected frame
    to a *different* image frame's column sums would test nothing except
    that both are noisy.
    """
    data = np.arange(24, dtype="float32").reshape(4, 6)
    camera = NionCamera(_FixedFrameCamera(data))
    camera.configure(CameraParameters(exposure_ms=1.0, readout=PROJECTED_READOUT))

    frame = camera.acquire_frame()

    np.testing.assert_allclose(frame.data, data.sum(axis=0), rtol=_PROJECTION_RTOL)
    assert frame.metadata["projected_by"] == "server"
    # The mode still reached the device, so a camera that can project in
    # its own readout gets the chance to.
    assert camera._device.processing == _SUM_PROJECT_PROCESSING  # noqa: SLF001


def test_a_device_that_projects_in_hardware_is_credited_rather_than_re_summed():
    """
    A 1D frame from the device is labelled ``"sensor"``, and is not summed again.

    Unreachable through usim, and that is exactly why it is pinned here:
    the branch exists for a vendor camera that honours the mode in its
    own readout, and a bug in it would either double-sum a spectrum or
    quietly credit the sensor for the server's arithmetic — the second
    being the misstatement about noise this metadata exists to prevent.
    """
    spectrum = np.arange(6, dtype="float32")
    camera = NionCamera(_FixedFrameCamera(spectrum))
    camera.configure(CameraParameters(exposure_ms=1.0, readout=PROJECTED_READOUT))

    frame = camera.acquire_frame()

    np.testing.assert_array_equal(frame.data, spectrum)
    assert frame.metadata["projected_by"] == "sensor"
    assert frame.metadata["readout"] == PROJECTED_READOUT


def test_a_1d_frames_binning_is_recovered_from_its_shape_like_any_other():
    """
    Shape recovery survives the rank change, which keeps in-flight frames honest.

    ``_binning_of`` exists because a camera reconfigured while running
    finishes the frame already in flight at the old settings, and
    labelling that frame with the new binning puts an axis on stored data
    wrong by the whole factor. A device that projects in its own readout
    delivers 1D frames, and the recovery has to keep working for them —
    matched against the *trailing* axis of ``get_expected_dimensions``,
    which is the dimension Nion's own ``sum_project`` keeps
    (``CameraTask.start`` slices ``[1:]``).

    Driven against the real device's dimensions rather than a stub, so
    the widths being distinct per factor — which is what makes the match
    unambiguous — is a fact about the camera and not about the test.
    """
    with simulated_instrument() as microscope:
        camera = microscope.eels_camera
        for value in camera.binning_values:
            rows, columns = camera._device.get_expected_dimensions(value)  # noqa: SLF001
            assert camera._binning_of((rows, columns)) == value  # noqa: SLF001
            assert camera._binning_of((columns,)) == value  # noqa: SLF001


def test_a_shape_no_binning_explains_falls_back_to_the_configured_one():
    """
    An unrecognisable shape yields the setting, which is the documented answer.

    The fallback matters because it is what a vendor camera with a
    cropped readout area would hit: guessing a factor by division would
    put a confidently wrong number in the metadata, where reporting the
    configured one is at least the thing that was asked for.
    """
    with simulated_instrument() as microscope:
        camera = microscope.eels_camera
        camera.configure(CameraParameters(exposure_ms=10.0, binning=_TEST_BINNING))
        assert camera._binning_of((7, 13)) == _TEST_BINNING  # noqa: SLF001
        assert camera._binning_of((13,)) == _TEST_BINNING  # noqa: SLF001
