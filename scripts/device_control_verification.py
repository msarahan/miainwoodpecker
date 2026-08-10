"""
Measure whether each exposed instrument control actually does anything.

This project's own principle is "measure, don't assume"
(docs/migration-plan.md, §1), and the device layer has already been bitten
by the specific failure this script exists to catch: ``nion.usim_device``
has controls that *accept* a value, report it back on read, and yet have
no effect on acquired data outside the full ``HardwareSource``/
``Application`` layer this project deliberately avoids. §7's "A real
4D-STEM acquisition mode" records exactly that for ``probe_position`` —
setting it and re-acquiring a Ronchigram frame changed nothing beyond
shot noise, because ``CameraSimulator._get_frame_settings`` silently drops
it back to a fixed centred default when no ``ScanHardwareSource`` is
registered.

So a successful setter proves nothing. For each control that
:class:`~miainwoodpecker.devices.interface.InstrumentController` exposes,
this script measures three separate things and prints them:

1. **Read-back**: does reading after writing return the written value?
2. **Effect on camera data**: is the mean absolute difference between
   frames taken at two control values larger than the mean absolute
   difference between two frames taken at the *same* value (the shot-noise
   floor)? A ratio near 1.0 means "no measurable effect", however happily
   the setter returned.
3. **Effect on scan data**: the same comparison for scanned frames.

Run against the simulator over the real IPC boundary the application
uses::

    uv run --extra device python scripts/device_control_verification.py

It takes a few minutes: the simulated Ronchigram camera has a ~1s
exposure and this acquires several frames per control.
"""

from __future__ import annotations

import statistics
import typing
from dataclasses import dataclass

import numpy as np

from miainwoodpecker.devices import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    STAGE_POSITION_CONTROL,
    ScanParameters,
)
from miainwoodpecker.devices.remote import remote_simulated_instrument

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from miainwoodpecker.devices.remote import (
        RemoteCamera,
        RemoteInstrumentDevices,
        RemoteScanner,
    )

_SCAN_PIXELS = 256
_FOV_FRACTION = 0.1
_REPEATS = 3
# A difference this many times the shot-noise floor counts as a real effect.
_EFFECT_RATIO = 2.0
# ...or a frame-mean shift this large a fraction of the baseline mean does.
_EFFECT_MEAN_FRACTION = 0.25


def _mean_abs_diff(first: np.ndarray, second: np.ndarray) -> float:
    """Return the mean absolute difference between two same-shape frames."""
    return float(np.mean(np.abs(first.astype(np.float64) - second.astype(np.float64))))


def _camera_frames(camera: RemoteCamera, count: int) -> list[np.ndarray]:
    """Acquire ``count`` camera frames, leaving the camera stopped."""
    camera.start()
    try:
        return [camera.acquire_frame().data for _ in range(count)]
    finally:
        camera.stop()


def _scan_frames(
    scanner: RemoteScanner,
    parameters: ScanParameters,
    count: int,
) -> list[np.ndarray]:
    """Acquire ``count`` scanned frames from channel 0."""
    return [scanner.scan_frame(parameters, 0).data for _ in range(count)]


@dataclass(frozen=True)
class _Measurement:
    """
    One control-versus-data-source measurement.

    Attributes
    ----------
    label : str
        Which data source was measured.
    noise : float
        Median mean-absolute-difference between two frames at a *fixed*
        control value: the shot-noise floor.
    difference : float
        Median mean-absolute-difference between a baseline frame and
        frames taken after changing the control.
    baseline_mean : float
        Mean pixel value before the change.
    changed_mean : float
        Mean pixel value after the change. Reported alongside the
        difference ratio because the two disagree in a useful way: a
        stage move barely shifts a sparse scan frame's *pixel-by-pixel*
        difference (ratio ~2) while collapsing its mean to zero, because
        it moved the field of view off the simulated sample entirely.
    """

    label: str
    noise: float
    difference: float
    baseline_mean: float
    changed_mean: float

    @property
    def ratio(self) -> float:
        """Return difference over noise floor; ~1.0 means no measurable effect."""
        return self.difference / self.noise if self.noise else float("inf")


def _measure(
    label: str,
    acquire: Callable[[], list[np.ndarray]],
    apply_change: Callable[[], None],
    revert: Callable[[], None],
) -> _Measurement:
    """
    Compare "same setting" differences against "changed setting" differences.

    Parameters
    ----------
    label : str
        Which data source is being measured.
    acquire : Callable[[], list[np.ndarray]]
        Returns a small batch of frames at the current settings.
    apply_change : Callable[[], None]
        Changes the control under test.
    revert : Callable[[], None]
        Restores the control's original value.

    Returns
    -------
    _Measurement
        The noise floor, the changed-setting difference, and both means.
    """
    baseline = acquire()
    noise = statistics.median(
        _mean_abs_diff(baseline[index], baseline[index + 1])
        for index in range(len(baseline) - 1)
    )
    apply_change()
    try:
        changed = acquire()
    finally:
        revert()
    return _Measurement(
        label=label,
        noise=noise,
        difference=statistics.median(
            _mean_abs_diff(baseline[0], frame) for frame in changed
        ),
        baseline_mean=float(np.mean(baseline[0])),
        changed_mean=float(np.mean(changed[0])),
    )


def _report(measurement: _Measurement) -> None:
    """Print one control/data-source measurement."""
    effect = measurement.ratio > _EFFECT_RATIO or (
        abs(measurement.changed_mean - measurement.baseline_mean)
        > _EFFECT_MEAN_FRACTION * abs(measurement.baseline_mean)
    )
    print(
        f"  {measurement.label:<20} noise {measurement.noise:9.4f}  "
        f"changed {measurement.difference:10.4f}  ratio {measurement.ratio:8.2f}  "
        f"mean {measurement.baseline_mean:10.4f} -> {measurement.changed_mean:10.4f}"
        f"  -> {'EFFECT' if effect else 'no measurable effect'}",
    )


def _verify_defocus(
    microscope: RemoteInstrumentDevices,
    parameters: ScanParameters,
) -> None:
    """Measure read-back and data effect for the defocus control."""
    instrument = microscope.instrument
    original = instrument.defocus_nm()
    target = original + 2500.0
    instrument.set_defocus_nm(target)
    read_back = instrument.defocus_nm()
    instrument.set_defocus_nm(original)
    print(f"\n{DEFOCUS_CONTROL}: read-back {target} nm -> {read_back} nm")

    camera = microscope.ronchigram_camera
    if camera is not None:
        _report(
            _measure(
                "ronchigram camera",
                lambda: _camera_frames(camera, _REPEATS),
                lambda: instrument.set_defocus_nm(target),
                lambda: instrument.set_defocus_nm(original),
            ),
        )
    _report(
        _measure(
            "scanner",
            lambda: _scan_frames(microscope.scanner, parameters, _REPEATS),
            lambda: instrument.set_defocus_nm(target),
            lambda: instrument.set_defocus_nm(original),
        ),
    )


def _verify_stage(
    microscope: RemoteInstrumentDevices,
    parameters: ScanParameters,
) -> None:
    """Measure read-back and data effect for the stage-position control."""
    instrument = microscope.instrument
    original = instrument.stage_position_nm()
    offset = microscope.stage_size_nm / 2.0
    target = (original[0] + offset, original[1] + offset)
    instrument.set_stage_position_nm(*target)
    read_back = instrument.stage_position_nm()
    instrument.set_stage_position_nm(*original)
    print(f"\n{STAGE_POSITION_CONTROL}: read-back {target} nm -> {read_back} nm")

    camera = microscope.ronchigram_camera
    if camera is not None:
        _report(
            _measure(
                "ronchigram camera",
                lambda: _camera_frames(camera, _REPEATS),
                lambda: instrument.set_stage_position_nm(*target),
                lambda: instrument.set_stage_position_nm(*original),
            ),
        )
    _report(
        _measure(
            "scanner",
            lambda: _scan_frames(microscope.scanner, parameters, _REPEATS),
            lambda: instrument.set_stage_position_nm(*target),
            lambda: instrument.set_stage_position_nm(*original),
        ),
    )


def _verify_blanker(
    microscope: RemoteInstrumentDevices,
    parameters: ScanParameters,
) -> None:
    """Measure read-back and data effect for the beam blanker."""
    instrument = microscope.instrument
    instrument.set_beam_blanked(blanked=True)
    blanked = instrument.is_beam_blanked()
    instrument.set_beam_blanked(blanked=False)
    unblanked = instrument.is_beam_blanked()
    print(
        f"\n{BEAM_BLANKER_CONTROL}: read-back blanked -> {blanked}, "
        f"unblanked -> {unblanked}",
    )
    for label, camera in (
        ("ronchigram camera", microscope.ronchigram_camera),
        ("eels camera", microscope.eels_camera),
    ):
        if camera is None:
            continue
        _report(
            _measure(
                label,
                lambda camera=camera: _camera_frames(camera, _REPEATS),
                lambda: instrument.set_beam_blanked(blanked=True),
                lambda: instrument.set_beam_blanked(blanked=False),
            ),
        )
    _report(
        _measure(
            "scanner",
            lambda: _scan_frames(microscope.scanner, parameters, _REPEATS),
            lambda: instrument.set_beam_blanked(blanked=True),
            lambda: instrument.set_beam_blanked(blanked=False),
        ),
    )


def main(argv: Sequence[str] | None = None) -> None:
    """
    Run every control measurement against the simulated microscope.

    Parameters
    ----------
    argv : Sequence[str] | None
        Unused; present so this can be wired to a console script later.
    """
    del argv
    with remote_simulated_instrument() as microscope:
        parameters = ScanParameters(
            height=_SCAN_PIXELS,
            width=_SCAN_PIXELS,
            pixel_time_us=1.0,
            fov_nm=microscope.stage_size_nm * _FOV_FRACTION,
        )
        print(f"backend description: {microscope.instrument.describe()}")
        controls = list(microscope.instrument.available_controls())
        print(f"controls reported available: {controls}")
        if DEFOCUS_CONTROL in controls:
            _verify_defocus(microscope, parameters)
        if STAGE_POSITION_CONTROL in controls:
            _verify_stage(microscope, parameters)
        if BEAM_BLANKER_CONTROL in controls:
            _verify_blanker(microscope, parameters)


if __name__ == "__main__":
    main()
