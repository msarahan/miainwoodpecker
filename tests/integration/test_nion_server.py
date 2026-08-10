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
    hardware_instrument,
    open_instrument,
    simulated_instrument,
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
