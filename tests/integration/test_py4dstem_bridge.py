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
from miainwoodpecker.storage import write_frames

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


def test_empty_recording_is_rejected_with_a_clear_error(tmp_path):
    """A zero-frame file has no /entry/data group and fails loudly, not silently."""
    path = tmp_path / "empty.nxs"
    write_frames(path, [])

    with pytest.raises(ValueError, match="no frames"):
        load_as_diffraction_slice(path)
