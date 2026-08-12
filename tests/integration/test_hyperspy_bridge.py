"""
Integration tests: the HyperSpy adapter against a real NeXus HDF5 file.

Skipped unless the ``analysis`` optional dependency group is installed
(``uv run --extra analysis --extra tests pytest tests/integration``).
"""

import datetime
import re

import numpy as np
import pytest

pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")

import hyperspy.api as hs

from miainwoodpecker.analysis.hyperspy_bridge import (
    hyperspy_signal_from_frames,
    hyperspy_spectrum_from_frames,
    load_as_hyperspy_signal,
    load_as_hyperspy_spectrum,
)
from miainwoodpecker.devices import Frame
from miainwoodpecker.storage import (
    AxisCalibration,
    AxisKind,
    FrameCalibration,
    FrameStack,
    read_frames,
    write_frames,
)

_FRAME_COUNT = 3
_HEIGHT, _WIDTH = 4, 6


def _frame(index: int, *, fov_nm: float | None = None) -> Frame:
    """Return a frame whose pixels all equal its index, one second apart."""
    metadata: dict[str, object] = {"index": index}
    if fov_nm is not None:
        metadata["fov_nm"] = fov_nm
    return Frame(
        data=np.full((_HEIGHT, _WIDTH), index, dtype=np.float32),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        + datetime.timedelta(seconds=index),
        metadata=metadata,
    )


def test_calibrated_recording_round_trips_shape_dtype_and_axes(tmp_path):
    """A recording with fov_nm survives as a correctly calibrated Signal2D."""
    path = tmp_path / "calibrated.nxs"
    fov_nm = 24.0
    write_frames(
        path, [_frame(i, fov_nm=fov_nm) for i in range(_FRAME_COUNT)]
    )

    signal = load_as_hyperspy_signal(path)

    assert signal.data.shape == (_FRAME_COUNT, _HEIGHT, _WIDTH)
    assert signal.data.dtype == np.float32
    for index in range(_FRAME_COUNT):
        assert np.array_equal(
            signal.data[index], np.full((_HEIGHT, _WIDTH), index, dtype=np.float32)
        )

    nav_axis = signal.axes_manager.navigation_axes[0]
    x_axis, y_axis = signal.axes_manager.signal_axes

    assert x_axis.units == "nm"
    assert y_axis.units == "nm"
    # Square pixels: fov_nm spans the *longer* axis, so both scales are
    # fov_nm / max(height, width). This asserted fov_nm / height on the
    # slow axis until the convention was pinned against Nion's own
    # get_scan_calibrations - a 4x6 frame is exactly the non-square case
    # where the two readings disagree (see ScanParameters.fov_nm).
    pixel_nm = fov_nm / max(_HEIGHT, _WIDTH)
    assert x_axis.scale == pytest.approx(pixel_nm)
    assert y_axis.scale == pytest.approx(pixel_nm)
    assert x_axis.offset == pytest.approx(0.0)
    assert y_axis.offset == pytest.approx(0.0)

    assert nav_axis.units == "s"
    assert nav_axis.scale == pytest.approx(1.0)  # one second between frames
    assert nav_axis.offset == pytest.approx(0.0)


def test_uncalibrated_recording_falls_back_to_pixel_units(tmp_path):
    """Without fov_nm the spatial axes carry the honest 'pixel' units through."""
    path = tmp_path / "uncalibrated.nxs"
    write_frames(path, [_frame(0)])

    signal = load_as_hyperspy_signal(path)

    x_axis, y_axis = signal.axes_manager.signal_axes
    assert x_axis.units == "pixel"
    assert y_axis.units == "pixel"
    assert x_axis.scale == pytest.approx(1.0)
    assert y_axis.scale == pytest.approx(1.0)


def test_a_diffraction_recording_reaches_hyperspy_in_reciprocal_units(tmp_path):
    """
    A camera frame's reciprocal-space axes survive into AxesManager as-is.

    AxesManager carries units per axis natively, so unlike py4DSTEM there
    is nothing to convert or squeeze - '1/nm' arrives as '1/nm', including
    the centred origin a diffraction axis has.
    """
    path = tmp_path / "diffraction.nxs"
    write_frames(
        path,
        [_frame(0)],
        calibration=FrameCalibration.diffraction(0.05, shape=(_HEIGHT, _WIDTH)),
    )

    signal = load_as_hyperspy_signal(path)
    x_axis, y_axis = signal.axes_manager.signal_axes

    assert x_axis.units == "1/nm"
    assert y_axis.units == "1/nm"
    assert x_axis.scale == pytest.approx(0.05)
    assert x_axis.offset == pytest.approx(-0.05 * _WIDTH / 2)
    assert y_axis.offset == pytest.approx(-0.05 * _HEIGHT / 2)


def test_a_spectrum_recordings_two_axes_stay_different_kinds(tmp_path):
    """
    The per-axis model round-trips heterogeneous axes, which is the point.

    An EELS frame's dispersive axis is eV and the other is genuinely
    uncalibrated; a single unit per signal could not say that.
    """
    path = tmp_path / "eels.nxs"
    dispersion, zero_loss = 0.5, -20.0
    write_frames(
        path,
        [_frame(0)],
        calibration=FrameCalibration.spectrum(dispersion, offset=zero_loss),
    )

    signal = load_as_hyperspy_signal(path)
    x_axis, y_axis = signal.axes_manager.signal_axes

    assert x_axis.units == "eV"
    assert x_axis.scale == pytest.approx(dispersion)
    assert x_axis.offset == pytest.approx(zero_loss)
    assert y_axis.units == "pixel"
    assert y_axis.scale == pytest.approx(1.0)


def test_a_spectrum_image_flattens_to_a_calibrated_signal1d(tmp_path):
    """
    The domain's actual request: acquired as a 2D image, flattened to 1D.

    HyperSpy's own type for a spectrum is Signal1D, not the Signal2D the
    frame-stack adapter produces, and the energy axis has to survive the
    flattening or the result is uninterpretable.
    """
    path = tmp_path / "eels.nxs"
    dispersion, zero_loss = 0.5, -20.0
    write_frames(
        path,
        [_frame(index) for index in range(_FRAME_COUNT)],
        calibration=FrameCalibration.spectrum(dispersion, offset=zero_loss),
    )

    spectrum = load_as_hyperspy_spectrum(path)

    assert isinstance(spectrum, hs.signals.Signal1D)
    assert spectrum.data.shape == (_FRAME_COUNT, _WIDTH)
    # summed along the non-dispersive direction: each frame is filled with
    # its own index, so a column sums to index * height.
    for index in range(_FRAME_COUNT):
        assert np.allclose(spectrum.data[index], index * _HEIGHT)

    energy_axis = spectrum.axes_manager.signal_axes[0]
    assert energy_axis.units == "eV"
    assert energy_axis.scale == pytest.approx(dispersion)
    assert energy_axis.offset == pytest.approx(zero_loss)
    assert spectrum.axes_manager.navigation_axes[0].units == "s"


def test_flattening_a_recording_with_no_energy_axis_is_refused(tmp_path):
    """
    Guessing which axis is dispersive would silently produce wrong eV values.

    Picking the longer axis is a plausible heuristic and wrong on a rotated
    camera, so the adapter requires the recording to say.
    """
    path = tmp_path / "uncalibrated.nxs"
    write_frames(path, [_frame(0)])

    with pytest.raises(ValueError, match="no single energy-calibrated axis"):
        load_as_hyperspy_spectrum(path)


def test_a_dispersive_y_axis_flattens_along_the_other_direction(tmp_path):
    """The dispersive direction is read from the file, not assumed to be x."""
    path = tmp_path / "rotated.nxs"
    write_frames(
        path,
        [_frame(1)],
        calibration=FrameCalibration.spectrum(0.5, dispersive_axis="y"),
    )

    spectrum = load_as_hyperspy_spectrum(path)

    assert spectrum.data.shape == (1, _HEIGHT)
    assert np.allclose(spectrum.data[0], _WIDTH)  # summed across x


def _axes(signal) -> list[tuple[str, str, float, float]]:
    """Return every axis of a signal as (name, units, scale, offset) tuples."""
    return [
        (axis.name, axis.units, axis.scale, axis.offset)
        for axis in signal.axes_manager._axes  # noqa: SLF001 - nav + signal, in order
    ]


def test_frames_in_memory_produce_the_same_signal_as_reading_the_file(tmp_path):
    """
    The two entry points are one implementation, so they cannot disagree.

    This is what makes the viewer's read-once path safe: analyzing a
    recording it has already read must give the answer analyzing the file
    would, or the saved read has been paid for in a different result.
    Compared on data *and* every axis, because a mismatch in the axes is
    exactly the failure that would otherwise pass unnoticed.
    """
    path = tmp_path / "diffraction.nxs"
    write_frames(
        path,
        [_frame(i) for i in range(_FRAME_COUNT)],
        calibration=FrameCalibration.diffraction(0.05, shape=(_HEIGHT, _WIDTH)),
    )

    from_file = load_as_hyperspy_signal(path)
    from_memory = hyperspy_signal_from_frames(read_frames(path))

    assert np.array_equal(from_memory.data, from_file.data)
    assert from_memory.data.dtype == from_file.data.dtype
    assert _axes(from_memory) == _axes(from_file)


def test_frames_in_memory_need_no_file_at_all(tmp_path):
    """
    Nothing is read here, which is the whole point, so assert it structurally.

    The frames are built in memory and never written, so if this function
    reached for a file there would be nothing to reach for. That is a
    stronger statement than counting opens: there is no path to open.
    ``tmp_path`` is taken only to keep the signature uniform with its
    neighbours.
    """
    assert tmp_path.exists()
    data = np.arange(_FRAME_COUNT * _HEIGHT * _WIDTH, dtype=np.float32).reshape(
        _FRAME_COUNT, _HEIGHT, _WIDTH
    )
    dispersion, zero_loss = 0.5, -20.0
    frames = FrameStack(
        data=data,
        frame_time=np.array([0.0, 2.0, 4.0]),
        calibration=FrameCalibration.spectrum(dispersion, offset=zero_loss),
    )

    signal = hyperspy_signal_from_frames(frames)

    assert np.array_equal(signal.data, data)
    x_axis, y_axis = signal.axes_manager.signal_axes
    assert x_axis.units == "eV"
    assert x_axis.scale == pytest.approx(dispersion)
    assert x_axis.offset == pytest.approx(zero_loss)
    assert y_axis.units == "pixel"
    nav_axis = signal.axes_manager.navigation_axes[0]
    assert nav_axis.units == "s"
    assert nav_axis.scale == pytest.approx(2.0)  # two seconds between frames


def test_an_uncalibrated_stack_stays_uncalibrated_rather_than_borrowing_axes(tmp_path):
    """
    Frames without a calibration produce the honest pixel axes, not silence.

    The in-memory form cannot invent axes from a file it never opened, so
    the ``FrameStack`` has to carry them — this asserts the fallback is the
    same admitted "pixel" the file-reading form produces, rather than
    something that merely looks calibrated.
    """
    path = tmp_path / "uncalibrated.nxs"
    write_frames(path, [_frame(0)])

    from_memory = hyperspy_signal_from_frames(read_frames(path))
    from_file = load_as_hyperspy_signal(path)

    assert _axes(from_memory) == _axes(from_file)
    for axis in from_memory.axes_manager.signal_axes:
        assert axis.units == "pixel"
        assert axis.scale == pytest.approx(1.0)


def test_the_spectrum_flattening_matches_between_the_two_entry_points(tmp_path):
    """
    The Signal1D path is shared the same way, energy axis included.

    Flattening depends entirely on the calibration saying which direction
    disperses, so this is the case where dropping the calibration on the
    way to memory would be loudest — and it is asserted rather than assumed.
    """
    path = tmp_path / "eels.nxs"
    dispersion, zero_loss = 0.5, -20.0
    write_frames(
        path,
        [_frame(index) for index in range(_FRAME_COUNT)],
        calibration=FrameCalibration.spectrum(dispersion, offset=zero_loss),
    )

    from_file = load_as_hyperspy_spectrum(path)
    from_memory = hyperspy_spectrum_from_frames(read_frames(path))

    assert isinstance(from_memory, hs.signals.Signal1D)
    assert np.array_equal(from_memory.data, from_file.data)
    assert _axes(from_memory) == _axes(from_file)


def test_frames_with_no_energy_axis_are_refused_and_named_by_the_caller(tmp_path):
    """
    The refusal survives the move off disk, and can still say which file it was.

    An array has no provenance of its own, so ``source=`` is how the
    file-reading form keeps the filename in a message an operator reads in
    a status label — with a neutral phrase when nobody supplies one.
    """
    path = tmp_path / "uncalibrated.nxs"
    write_frames(path, [_frame(0)])
    frames = read_frames(path)

    with pytest.raises(ValueError, match="this recording has no single energy"):
        hyperspy_spectrum_from_frames(frames)
    with pytest.raises(ValueError, match=re.escape("0007-camera.nxs has no")):
        hyperspy_spectrum_from_frames(frames, source="0007-camera.nxs")
    with pytest.raises(ValueError, match=re.escape(str(path))):
        load_as_hyperspy_spectrum(path)


def test_a_hand_built_frame_stack_calibrates_its_two_axes_independently(tmp_path):
    """
    The per-axis model survives the in-memory route, which is where it could rot.

    A frame with one energy axis and one real-space axis is the case a
    single scale per signal cannot express; a carrier that flattened the
    two would lose it quietly rather than fail.
    """
    assert tmp_path.exists()
    frames = FrameStack(
        data=np.ones((1, _HEIGHT, _WIDTH), dtype=np.float32),
        frame_time=np.array([0.0]),
        calibration=FrameCalibration(
            y=AxisCalibration(AxisKind.REAL_SPACE, 1.5, units="nm"),
            x=AxisCalibration(AxisKind.ENERGY, 0.25, -10.0, "eV"),
        ),
    )

    signal = hyperspy_signal_from_frames(frames)

    x_axis, y_axis = signal.axes_manager.signal_axes
    assert (x_axis.units, x_axis.scale, x_axis.offset) == ("eV", 0.25, -10.0)
    assert (y_axis.units, y_axis.scale, y_axis.offset) == ("nm", 1.5, 0.0)


def test_empty_recording_is_rejected_with_a_clear_error(tmp_path):
    """A zero-frame file has no /entry/data group and fails loudly, not silently."""
    path = tmp_path / "empty.nxs"
    write_frames(path, [])

    with pytest.raises(ValueError, match="no frames"):
        load_as_hyperspy_signal(path)
    with pytest.raises(ValueError, match="no frames"):
        load_as_hyperspy_spectrum(path)
