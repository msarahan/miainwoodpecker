"""
Integration tests: the py4DSTEM adapter against a real NeXus HDF5 file.

Skipped unless the ``py4dstem`` optional dependency group is installed
(``uv run --extra py4dstem --extra tests pytest tests/integration``).
"""

import datetime

import numpy as np
import pytest

pytest.importorskip("py4DSTEM", reason="requires the 'py4dstem' extra")

from py4DSTEM.data import DiffractionSlice
from py4DSTEM.process.calibration import get_probe_size

from miainwoodpecker.analysis.py4dstem_bridge import load_as_diffraction_slice
from miainwoodpecker.devices import Frame
from miainwoodpecker.storage import (
    AxisCalibration,
    AxisKind,
    FrameCalibration,
    write_frames,
)

_HEIGHT, _WIDTH = 8, 8


def _frame(index: int, *, fov_nm: float | None = None) -> Frame:
    """Return a frame whose pixels all equal its index."""
    metadata: dict[str, object] = {"index": index}
    if fov_nm is not None:
        metadata["fov_nm"] = fov_nm
    return Frame(
        data=np.full((_HEIGHT, _WIDTH), index, dtype=np.float32),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        + datetime.timedelta(seconds=index),
        metadata=metadata,
    )


def test_single_frame_becomes_a_2d_diffraction_slice(tmp_path):
    """One camera frame round-trips as a bare (height, width) DiffractionSlice."""
    path = tmp_path / "single.nxs"
    write_frames(path, [_frame(3)])

    diffraction_slice = load_as_diffraction_slice(path)

    assert isinstance(diffraction_slice, DiffractionSlice)
    assert diffraction_slice.data.shape == (_HEIGHT, _WIDTH)
    assert np.array_equal(
        diffraction_slice.data, np.full((_HEIGHT, _WIDTH), 3, dtype=np.float32)
    )
    # honest "pixel" fallback (no fov_nm was written): a single scale for
    # both axes, matching what py4DSTEM's Calibration models.
    assert diffraction_slice.calibration.Q_pixel_units == "pixels"
    assert diffraction_slice.calibration.Q_pixel_size == pytest.approx(1.0)


def test_multi_frame_becomes_a_labelled_diffraction_slice_stack(tmp_path):
    """Several camera frames round-trip as a DiffractionSlice with slicelabels."""
    path = tmp_path / "stack.nxs"
    frame_count = 3
    write_frames(path, [_frame(i) for i in range(frame_count)])

    diffraction_slice = load_as_diffraction_slice(path)

    assert diffraction_slice.data.shape == (frame_count, _HEIGHT, _WIDTH)
    assert diffraction_slice.slicelabels == ["0", "1", "2"]
    for index in range(frame_count):
        assert np.array_equal(
            diffraction_slice.get_slice(str(index)).data,
            np.full((_HEIGHT, _WIDTH), index, dtype=np.float32),
        )


def test_real_diffraction_pattern_survives_a_genuine_py4dstem_operation(tmp_path):
    """The loaded slice is real py4DSTEM data: get_probe_size runs on it unmodified."""
    path = tmp_path / "disk.nxs"
    data = np.zeros((_HEIGHT, _WIDTH), dtype=np.float32)
    data[2:6, 2:6] = 100.0  # a bright, roughly centred "direct beam" disk
    write_frames(
        path,
        [
            Frame(
                data=data,
                timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
                metadata={},
            )
        ],
    )

    diffraction_slice = load_as_diffraction_slice(path)
    radius, x0, y0 = get_probe_size(diffraction_slice.data)

    assert radius > 0
    assert 0 <= x0 <= _WIDTH
    assert 0 <= y0 <= _HEIGHT


def test_scan_recording_is_rejected_not_silently_miscalibrated(tmp_path):
    """
    A scan (real-space, nanometre-calibrated) recording is refused, loudly.

    py4DSTEM's Calibration.Q_pixel_units models a diffraction-plane scale
    ('pixels', 'A^-1', 'mrad'); NexusWriter's nanometre calibration comes
    from a *scan's* field of view (Phase 3), not anything diffraction-plane
    - this adapter is for camera recordings, so it should refuse rather
    than silently mislabel real-space nanometres as a diffraction-plane
    pixel count.
    """
    path = tmp_path / "scan.nxs"
    write_frames(path, [_frame(0, fov_nm=24.0)])

    with pytest.raises(ValueError, match="diffraction-plane"):
        load_as_diffraction_slice(path)


def test_reciprocal_nm_is_converted_to_inverse_angstrom_not_relabelled(tmp_path):
    """
    The factor of ten, asserted numerically because it fails silently.

    py4DSTEM's Q_pixel_units accepts only 'pixels', 'A^-1', or 'mrad' (a
    hard assert in its own code), and NeXus does not accept 'A^-1' as a
    NX_WAVENUMBER spelling - so a '1/nm' axis has to be converted. 1 A^-1
    is 10 nm^-1, so the magnitude divides by ten. Passing the nm^-1 number
    under the 'A^-1' label would make every downstream py4DSTEM number
    wrong by exactly 10x with nothing saying so, which is why this is
    checked against a literal rather than a round trip.
    """
    path = tmp_path / "reciprocal.nxs"
    scale_per_nm = 0.05
    write_frames(
        path,
        [_frame(1)],
        calibration=FrameCalibration.diffraction(scale_per_nm),
    )

    diffraction_slice = load_as_diffraction_slice(path)

    assert diffraction_slice.calibration.Q_pixel_units == "A^-1"
    assert diffraction_slice.calibration.Q_pixel_size == pytest.approx(0.005)
    assert diffraction_slice.calibration.Q_pixel_size == pytest.approx(
        scale_per_nm / 10.0
    )


def test_an_inverse_angstrom_recording_needs_no_conversion(tmp_path):
    """A file already written in 1/angstrom passes through unscaled."""
    path = tmp_path / "angstrom.nxs"
    write_frames(
        path,
        [_frame(1)],
        calibration=FrameCalibration.diffraction(0.005, units="1/angstrom"),
    )

    diffraction_slice = load_as_diffraction_slice(path)

    assert diffraction_slice.calibration.Q_pixel_units == "A^-1"
    assert diffraction_slice.calibration.Q_pixel_size == pytest.approx(0.005)


def test_an_angular_recording_passes_through_as_mrad(tmp_path):
    """
    An mrad axis is already one of py4DSTEM's three units, so it passes through.

    This is the second reason AxisKind keeps angles distinct from
    reciprocal space: turning a scattering angle into a scattering vector
    needs the electron wavelength, which nothing here knows.
    """
    path = tmp_path / "angles.nxs"
    write_frames(
        path,
        [_frame(1)],
        calibration=FrameCalibration.diffraction(0.4, units="mrad"),
    )

    diffraction_slice = load_as_diffraction_slice(path)

    assert diffraction_slice.calibration.Q_pixel_units == "mrad"
    assert diffraction_slice.calibration.Q_pixel_size == pytest.approx(0.4)


def test_a_radian_recording_is_converted_to_mrad(tmp_path):
    """A camera reporting radians (as the simulated one does) scales by 1000."""
    path = tmp_path / "radians.nxs"
    write_frames(
        path,
        [_frame(1)],
        calibration=FrameCalibration.diffraction(0.0004, units="rad"),
    )

    diffraction_slice = load_as_diffraction_slice(path)

    assert diffraction_slice.calibration.Q_pixel_units == "mrad"
    assert diffraction_slice.calibration.Q_pixel_size == pytest.approx(0.4)


def test_a_spectrum_recording_is_rejected_like_a_scan_one(tmp_path):
    """An energy axis is not a diffraction-plane quantity either."""
    path = tmp_path / "eels.nxs"
    write_frames(
        path,
        [_frame(0)],
        calibration=FrameCalibration.spectrum(0.5),
    )

    with pytest.raises(ValueError, match="diffraction-plane"):
        load_as_diffraction_slice(path)


def test_anisotropic_diffraction_axes_are_refused_not_halved(tmp_path):
    """
    Q_pixel_size is one number for both axes, so two scales cannot fit.

    Silently keeping one of them would distort every measured distance in
    the pattern along the other direction.
    """
    path = tmp_path / "anisotropic.nxs"
    write_frames(
        path,
        [_frame(0)],
        calibration=FrameCalibration(
            y=AxisCalibration(AxisKind.RECIPROCAL_SPACE, 0.05),
            x=AxisCalibration(AxisKind.RECIPROCAL_SPACE, 0.06),
        ),
    )

    with pytest.raises(ValueError, match="anisotropic"):
        load_as_diffraction_slice(path)


def test_mixed_axis_kinds_are_refused(tmp_path):
    """A frame with one energy and one reciprocal axis has no single Q scale."""
    path = tmp_path / "mixed.nxs"
    write_frames(
        path,
        [_frame(0)],
        calibration=FrameCalibration(
            y=AxisCalibration(AxisKind.RECIPROCAL_SPACE, 0.05),
            x=AxisCalibration(AxisKind.ENERGY, 0.5),
        ),
    )

    with pytest.raises(ValueError, match="diffraction-plane"):
        load_as_diffraction_slice(path)


def test_empty_recording_is_rejected_with_a_clear_error(tmp_path):
    """A zero-frame file has no /entry/data group and fails loudly, not silently."""
    path = tmp_path / "empty.nxs"
    write_frames(path, [])

    with pytest.raises(ValueError, match="no frames"):
        load_as_diffraction_slice(path)
