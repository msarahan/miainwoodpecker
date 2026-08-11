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

import numpy as np
import pytest

pytest.importorskip("nion.usim_device", reason="requires the 'device' extra")

from miainwoodpecker.devices import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    STAGE_POSITION_CONTROL,
    Camera,
    InstrumentController,
    ScanParameters,
    Scanner,
)
from miainwoodpecker.devices.nion_server import (
    HARDWARE_BACKEND,
    SIMULATED_BACKEND,
    HardwareNotAvailableError,
    NionCamera,
    _axis_calibration_spec,
    _parse_args,
    hardware_instrument,
    open_instrument,
    simulated_instrument,
)
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

_PORTS = ["5001", "5002", "5003", "5004"]


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
        assert isinstance(microscope.instrument, InstrumentController)
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
    """All three neutral controls resolve to real usim controls."""
    with simulated_instrument() as microscope:
        controls = set(microscope.instrument.available_controls())
        assert controls == {
            STAGE_POSITION_CONTROL,
            DEFOCUS_CONTROL,
            BEAM_BLANKER_CONTROL,
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
