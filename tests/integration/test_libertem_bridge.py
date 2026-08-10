"""
Integration tests: the LiberTEM adapter against a real NeXus HDF5 file.

Skipped unless the ``libertem`` optional dependency group is installed
(``uv run --extra libertem --extra tests pytest tests/integration``).
"""

import datetime

import numpy as np
import pytest

pytest.importorskip("libertem", reason="requires the 'libertem' extra")

from libertem.api import Context
from libertem.udf.sum import SumUDF

from miainwoodpecker.analysis.libertem_bridge import load_as_libertem_dataset
from miainwoodpecker.devices import Frame
from miainwoodpecker.storage import write_frames

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


def test_empty_recording_is_rejected_with_a_clear_error(tmp_path):
    """A zero-frame file has no /entry/data group and fails loudly, not silently."""
    path = tmp_path / "empty.nxs"
    write_frames(path, [])

    with (
        Context.make_with("inline") as ctx,
        pytest.raises(ValueError, match="no frames"),
    ):
        load_as_libertem_dataset(ctx, path)
