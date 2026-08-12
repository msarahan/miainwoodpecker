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

import psutil
from libertem.api import Context
from libertem.io.dataset.base import DataSetMeta
from libertem.io.dataset.hdf5 import H5DataSet
from libertem.udf.sum import SumUDF

from miainwoodpecker.analysis.libertem_bridge import (
    analysis_context,
    load_as_libertem_dataset,
)
from miainwoodpecker.analysis.threads import analysis_thread_count
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


def test_analysis_context_bounds_its_own_fine_grained_threads():
    """
    "Inline" bounds the executor; the thread count under it is separate.

    This is the reason `analysis_context` exists at all rather than the
    widget calling `Context.make_with("inline")`. An `InlineJobExecutor`
    built without `inline_threads` reports `psutil.cpu_count(logical=False)`
    threads per worker, and hands that number to numba, pyfftw and the
    BLAS pools around every partition it processes - i.e. every physical
    core, which is precisely what froze the viewer for four seconds in the
    Phase 2 benchmark. Asserted against LiberTEM's own `Environment`, the
    object it actually passes to a UDF, so this fails if LiberTEM ever
    stops honouring the parameter.
    """
    with analysis_context(cpu_count=8) as ctx:
        environment = ctx.executor._get_local_env()  # noqa: SLF001
    expected_on_eight_cores = 6
    assert environment.threads_per_worker == expected_on_eight_cores
    assert environment.threads_per_worker == analysis_thread_count(8)

    with Context.make_with("inline") as unbounded:
        # The default this replaces, named rather than assumed: whatever
        # the machine has, and never our capped number by coincidence.
        assert unbounded.executor._get_local_env().threads_per_worker == (  # noqa: SLF001
            psutil.cpu_count(logical=False)
        )


def test_analysis_context_still_runs_a_real_udf(tmp_path):
    """
    Capping the threads does not change the answer, only how many cores it costs.

    The same reduction as `test_sum_udf_runs_a_real_reduction_over_the_frame_axis`,
    through the bounded context the viewer actually uses, because a cap
    that quietly broke the UDF path would otherwise only show up on a
    button click.
    """
    path = tmp_path / "burst.nxs"
    write_frames(path, [_frame(i) for i in range(_FRAME_COUNT)])

    with analysis_context() as ctx:
        dataset = load_as_libertem_dataset(ctx, path)
        result = ctx.run_udf(dataset=dataset, udf=SumUDF())

    expected_sum = sum(range(_FRAME_COUNT))
    assert np.all(result["intensity"].data == expected_sum)


def test_the_bounded_context_degrades_to_one_thread_on_a_small_machine():
    """
    A two-core machine gets a single-threaded executor, not a zero-worker one.

    LiberTEM does not treat `inline_threads=0` as "one": it passes the
    number straight to `numba.set_num_threads`, which refuses anything
    below one. So the floor in `analysis_thread_count` is what keeps the
    LiberTEM button working on a small machine at all, and it is worth an
    assertion here rather than only in the policy's own unit tests.
    """
    with analysis_context(cpu_count=2) as ctx:
        assert ctx.executor._get_local_env().threads_per_worker == 1  # noqa: SLF001
