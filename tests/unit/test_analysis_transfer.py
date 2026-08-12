"""
Unit tests for the analysis isolation layer's wire vocabulary and client.

Everything here runs in a base environment with no analysis extra
installed, which is the point: ``analysis/transfer.py`` and
``analysis/remote.py`` are the MIT halves of that boundary and must
import, dispatch and refuse without HyperSpy, LiberTEM or py4DSTEM being
present. The parts that need a real library are in
``tests/integration/test_analysis_worker.py``.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from miainwoodpecker.analysis import remote
from miainwoodpecker.analysis.operations import OPERATIONS, AnalysisInput
from miainwoodpecker.analysis.remote import (
    InProcessRunner,
    WorkerRunner,
    analysis_runner,
    isolation_enabled,
    open_runner,
    target_available,
)
from miainwoodpecker.analysis.threads import GUI_RESERVED_CORES
from miainwoodpecker.analysis.transfer import (
    ANALYSIS_TARGETS,
    AnalysisSource,
    publish_array,
    publish_source,
    read_array,
    read_source,
)
from miainwoodpecker.devices.rpc import SHARED_MEMORY_THRESHOLD_BYTES
from miainwoodpecker.devices.shared_frame import (
    SharedArrayRef,
    SharedFrameReader,
    SharedFrameWriter,
)
from miainwoodpecker.storage.calibration import FrameCalibration
from miainwoodpecker.storage.nexus import FrameStack

_SHM_DIR = pathlib.Path("/dev/shm")  # noqa: S108 - the POSIX shared-memory
# mount this module's transport manages, not a temp directory.


@pytest.fixture
def writer():
    """Yield a writer that always unlinks its segment, even on failure."""
    instance = SharedFrameWriter()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def reader():
    """Yield a reader that always detaches."""
    instance = SharedFrameReader()
    try:
        yield instance
    finally:
        instance.close()


def _stack(edge: int = 64, frames: int = 5) -> FrameStack:
    """Build a frame stack whose contents and axes identify it."""
    rng = np.random.default_rng(0)
    return FrameStack(
        data=rng.random((frames, edge, edge), dtype=np.float32),
        frame_time=np.arange(frames, dtype=np.float64) * 0.25,
        calibration=FrameCalibration.from_field_of_view(10.0, (edge, edge)),
    )


def test_a_large_array_goes_through_shared_memory(writer, reader):
    """Above the threshold the sender publishes a reference, not the array."""
    array = np.zeros(SHARED_MEMORY_THRESHOLD_BYTES, dtype=np.uint8)
    published = publish_array(writer, array)
    assert isinstance(published, SharedArrayRef)
    np.testing.assert_array_equal(read_array(reader, published), array)


def test_a_small_array_stays_on_the_pickle_path(writer, reader):
    """Below the threshold nothing is published, so no segment is created."""
    array = np.arange(8, dtype=np.uint8)
    published = publish_array(writer, array)
    assert isinstance(published, np.ndarray)
    np.testing.assert_array_equal(read_array(reader, published), array)


def test_a_frame_stack_round_trips_with_its_axes(writer, reader):
    """The transport carries the calibration, not just the array."""
    stack = _stack()
    source = publish_source(writer, AnalysisInput(frames=stack, origin="burst.nxs"))
    decoded = read_source(reader, source)

    assert decoded.path is None
    assert decoded.origin == "burst.nxs"
    np.testing.assert_array_equal(decoded.frames.data, stack.data)
    np.testing.assert_array_equal(decoded.frames.frame_time, stack.frame_time)
    assert decoded.frames.calibration == stack.calibration


def test_a_file_source_moves_no_array_at_all(writer, reader):
    """The fresh-burst case sends a path, so nothing large crosses."""
    before = set(_SHM_DIR.iterdir())
    source = publish_source(writer, AnalysisInput(path="/tmp/burst.nxs"))  # noqa: S108
    assert source.stack is None
    assert set(_SHM_DIR.iterdir()) == before

    decoded = read_source(reader, source)
    assert decoded.path == "/tmp/burst.nxs"  # noqa: S108
    assert decoded.frames is None


def test_a_source_with_neither_a_file_nor_frames_is_refused(writer):
    """A caller who forgot to say what to analyse is told so, not guessed at."""
    with pytest.raises(ValueError, match="either a file to open or frames"):
        publish_source(writer, AnalysisInput())


def test_frames_arriving_without_their_axes_are_refused(reader):
    """An array with no calibration would be analysed as bare pixels."""
    hand_built = AnalysisSource(stack=np.zeros((2, 2, 2)))
    with pytest.raises(ValueError, match="frame times and calibration"):
        read_source(reader, hand_built)


def test_an_empty_source_is_refused_on_the_worker_side(reader):
    """The worker names the problem rather than raising an AttributeError."""
    with pytest.raises(ValueError, match="neither a file nor frames"):
        read_source(reader, AnalysisSource())


def test_every_target_names_an_extra_and_a_probe_module():
    """The table the viewer's status messages and spawn argv both read."""
    assert set(ANALYSIS_TARGETS) == {"hyperspy", "libertem", "py4dstem"}
    for name, target in ANALYSIS_TARGETS.items():
        assert target.name == name
        assert target.extra
        assert target.probe_module
        assert target.eager_modules


def test_isolation_is_off_unless_the_environment_says_otherwise(monkeypatch):
    """The default is unchanged from before this layer existed."""
    monkeypatch.delenv(remote.ISOLATION_ENV_VAR, raising=False)
    assert isolation_enabled() is False
    assert isinstance(analysis_runner("hyperspy"), InProcessRunner)

    monkeypatch.setenv(remote.ISOLATION_ENV_VAR, "process")
    assert isolation_enabled() is True
    assert isinstance(analysis_runner("hyperspy"), WorkerRunner)


def test_an_unrecognised_isolation_setting_does_not_silently_isolate(monkeypatch):
    """Only the documented value turns it on; a typo leaves the default."""
    monkeypatch.setenv(remote.ISOLATION_ENV_VAR, "subprocess")
    assert isolation_enabled() is False


def test_availability_is_answered_without_importing_anything():
    """
    A probe must not pay the import it is asking about.

    ``sys.modules`` is checked directly rather than trusted: the whole
    reason ``target_available`` uses ``find_spec`` is that importing
    py4DSTEM to discover whether py4DSTEM is installed would freeze the
    GUI for five seconds, and a regression to a plain ``import`` would
    otherwise pass every other test in this file.
    """
    import sys  # noqa: PLC0415 - the thing under test

    for name, target in ANALYSIS_TARGETS.items():
        already = target.probe_module in sys.modules
        answer = target_available(name)
        assert isinstance(answer, bool)
        if not already:
            assert target.probe_module not in sys.modules


def test_a_missing_extra_is_an_import_error_in_both_modes(monkeypatch):
    """
    Both paths refuse the same way, so the viewer keeps one except clause.

    A target nobody has installed is invented rather than mocking the
    import machinery, because what is being checked is that
    ``open_runner`` refuses on a missing library and not on something
    about the three real names.
    """
    from miainwoodpecker.analysis.transfer import AnalysisTarget  # noqa: PLC0415

    monkeypatch.setitem(
        ANALYSIS_TARGETS,
        "nonesuch",
        AnalysisTarget(
            "nonesuch",
            "nonesuch",
            "miainwoodpecker_nonesuch",
            ("miainwoodpecker_nonesuch",),
        ),
    )
    monkeypatch.delenv(remote.ISOLATION_ENV_VAR, raising=False)
    with pytest.raises(ImportError):
        open_runner("nonesuch")

    monkeypatch.setenv(remote.ISOLATION_ENV_VAR, "process")
    with pytest.raises(ImportError, match="not installed"):
        open_runner("nonesuch")


def test_the_in_process_runner_dispatches_through_the_operations_table(monkeypatch):
    """
    One table, so the two transports cannot run different code.

    The operation is replaced rather than run, because running a real one
    would need an analysis extra this test deliberately does not have.
    """
    seen: list[AnalysisInput] = []
    monkeypatch.setitem(
        OPERATIONS,
        "mean_projection",
        lambda job: (seen.append(job), "computed")[1],
    )
    job = AnalysisInput(path="burst.nxs")
    assert InProcessRunner().run("mean_projection", job) == "computed"
    assert seen == [job]


def test_closing_an_in_process_runner_is_a_no_op():
    """It owns no process and no segment, and the viewer closes it anyway."""
    runner = InProcessRunner()
    runner.close()
    runner.close()


def test_a_worker_runner_that_never_started_closes_cleanly():
    """Teardown must not depend on a button ever having been clicked."""
    runner = WorkerRunner("hyperspy")
    runner.close()
    runner.close()


def test_a_worker_runner_reports_the_thread_budget_it_will_impose():
    """
    The budget is resolved client-side, before the worker exists.

    That ordering is the whole point of the isolated path's thread
    control: ``OMP_NUM_THREADS`` is read when the native library loads,
    so it has to be decided before there is a process to load it in.
    """
    assert WorkerRunner("hyperspy", cpu_count=8).thread_budget == (
        8 - GUI_RESERVED_CORES
    )
    assert WorkerRunner("hyperspy", cpu_count=1).thread_budget == 1
