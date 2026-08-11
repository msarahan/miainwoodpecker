"""
Integration tests: the LiberTEM adapter against a real NeXus HDF5 file.

Skipped unless the ``libertem`` optional dependency group is installed
(``uv run --extra libertem --extra tests pytest tests/integration``).
"""

import datetime

import numpy as np
import pytest

pytest.importorskip("libertem", reason="requires the 'libertem' extra")

import inspect

from libertem.api import Context
from libertem.io.dataset.base import DataSetMeta
from libertem.io.dataset.hdf5 import H5DataSet
from libertem.udf.sum import SumUDF

from miainwoodpecker.analysis.libertem_bridge import load_as_libertem_dataset
from miainwoodpecker.devices import Frame
from miainwoodpecker.storage import (
    AxisKind,
    FrameCalibration,
    read_calibration,
    write_frames,
)

_FRAME_COUNT = 3
_HEIGHT, _WIDTH = 4, 6


def _frame(index: int) -> Frame:
    """Return a frame whose pixels all equal its index, one second apart."""
    return Frame(
        data=np.full((_HEIGHT, _WIDTH), index, dtype=np.float32),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        + datetime.timedelta(seconds=index),
        metadata={"index": index},
    )


def test_dataset_has_a_one_dimensional_navigation_axis(tmp_path):
    """
    LiberTEM infers a genuine 1D nav shape from a plain frame stack.

    This is the core finding this adapter exists to exploit: unlike
    py4DSTEM, LiberTEM's ``DataSet`` does not require a 2D (scan_y,
    scan_x) navigation grid - a 1D stack of frames, exactly what this
    app's ``camera_series``/``scan_series`` produce, is a valid
    navigation shape in its own right.
    """
    path = tmp_path / "burst.nxs"
    write_frames(path, [_frame(i) for i in range(_FRAME_COUNT)])

    with Context.make_with("inline") as ctx:
        dataset = load_as_libertem_dataset(ctx, path)
        assert tuple(dataset.shape.nav) == (_FRAME_COUNT,)
        assert tuple(dataset.shape.sig) == (_HEIGHT, _WIDTH)


def test_sum_udf_runs_a_real_reduction_over_the_frame_axis(tmp_path):
    """A real, built-in LiberTEM UDF (SumUDF) produces the expected sum."""
    path = tmp_path / "burst.nxs"
    write_frames(path, [_frame(i) for i in range(_FRAME_COUNT)])

    with Context.make_with("inline") as ctx:
        dataset = load_as_libertem_dataset(ctx, path)
        result = ctx.run_udf(dataset=dataset, udf=SumUDF())

    expected_sum = sum(range(_FRAME_COUNT))  # each frame is filled with its index
    assert result["intensity"].data.shape == (_HEIGHT, _WIDTH)
    assert np.all(result["intensity"].data == expected_sum)


def test_libertem_still_has_nowhere_to_put_axis_calibration(tmp_path):
    """
    The honest negative, asserted as a canary rather than left as prose.

    Unlike HyperSpy's AxesManager and py4DSTEM's Calibration, LiberTEM's
    DataSetMeta models no per-axis scale/offset/units - its shape is plain
    integer extents. Its one free-form `metadata` passthrough is not even
    reachable through the HDF5 loader, which takes no such parameter. If a
    future LiberTEM grows a real per-axis calibration field, this test
    fails and the adapter can start using it, instead of the module
    docstring quietly going stale.
    """
    meta_params = set(inspect.signature(DataSetMeta.__init__).parameters)
    assert not [
        name
        for name in meta_params
        if any(word in name for word in ("scale", "units", "calibration"))
    ]
    # `sync_offset` is the one offset-shaped parameter, and it is a
    # frame-index alignment for a detector that started early or late, not
    # an axis origin - so it is named here rather than pattern-matched away.
    assert {name for name in meta_params if "offset" in name} == {"sync_offset"}
    loader_params = set(inspect.signature(H5DataSet.__init__).parameters)
    assert "metadata" not in loader_params

    # The DataSet a real, calibrated recording produces carries integer
    # extents and nothing else, so the calibration has to be read from the
    # file alongside it.
    path = tmp_path / "diffraction.nxs"
    write_frames(
        path,
        [_frame(i) for i in range(_FRAME_COUNT)],
        calibration=FrameCalibration.diffraction(0.05),
    )
    with Context.make_with("inline") as ctx:
        dataset = load_as_libertem_dataset(ctx, path)
        assert tuple(dataset.shape.sig) == (_HEIGHT, _WIDTH)
        assert not hasattr(dataset.meta, "scale")

    recovered = read_calibration(path)
    assert recovered.x.kind is AxisKind.RECIPROCAL_SPACE
    assert recovered.x.units == "1/nm"


def test_empty_recording_is_rejected_with_a_clear_error(tmp_path):
    """A zero-frame file has no /entry/data group and fails loudly, not silently."""
    path = tmp_path / "empty.nxs"
    write_frames(path, [])

    with (
        Context.make_with("inline") as ctx,
        pytest.raises(ValueError, match="no frames"),
    ):
        load_as_libertem_dataset(ctx, path)
