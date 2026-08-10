"""
Integration tests: the HyperSpy adapter against a real NeXus HDF5 file.

Skipped unless the ``analysis`` optional dependency group is installed
(``uv run --extra analysis --extra tests pytest tests/integration``).
"""

import datetime

import numpy as np
import pytest

pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")

from miainwoodpecker.analysis.hyperspy_bridge import load_as_hyperspy_signal
from miainwoodpecker.devices import Frame
from miainwoodpecker.storage import write_frames

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
    # fov_nm / width and fov_nm / height, sampled at pixel edges (see
    # storage/nexus.py's _write_nxdata).
    assert x_axis.scale == pytest.approx(fov_nm / _WIDTH)
    assert y_axis.scale == pytest.approx(fov_nm / _HEIGHT)
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


def test_empty_recording_is_rejected_with_a_clear_error(tmp_path):
    """A zero-frame file has no /entry/data group and fails loudly, not silently."""
    path = tmp_path / "empty.nxs"
    write_frames(path, [])

    with pytest.raises(ValueError, match="no frames"):
        load_as_hyperspy_signal(path)
