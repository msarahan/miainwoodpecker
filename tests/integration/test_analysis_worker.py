"""
Integration tests for running an analysis in an isolated worker process.

The claim under test is the one docs/analysis-isolation.md rests on:
**the isolated path computes the same thing as the in-process path**,
because both call the same function in
``miainwoodpecker.analysis.operations``. Asserting the results are equal
is what makes that a fact rather than a design intention.

The rest covers the properties a subprocess boundary adds and the
in-process path never had: the worker actually holds the library (and the
client actually does not), a dead worker costs one call rather than the
session, and nothing is left in ``/dev/shm`` afterwards.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import subprocess
import sys

import numpy as np
import pytest

from miainwoodpecker.analysis.operations import AnalysisInput
from miainwoodpecker.analysis.remote import (
    InProcessRunner,
    WorkerRunner,
    target_available,
)
from miainwoodpecker.analysis.threads import GUI_RESERVED_CORES
from miainwoodpecker.devices.interface import Frame
from miainwoodpecker.devices.rpc import RemoteCallError
from miainwoodpecker.storage.nexus import read_frames, write_frames

_SHM_DIR = pathlib.Path("/dev/shm")  # noqa: S108 - the POSIX shared-memory
# mount the transport under test manages, not a temp directory.

_EDGE = 96
"""
Frame edge length, chosen so a five-frame burst clears the threshold.

96x96 float32 x5 is 177 KB against
``SHARED_MEMORY_THRESHOLD_BYTES``'s 64 KB, so the in-memory case really
does exercise the shared-memory branch rather than quietly staying on the
pickle path — while each individual result (36 KB) stays under it, which
exercises the other branch in the same test.
"""

_EIGHT_CORE_BUDGET = 8 - GUI_RESERVED_CORES
"""What a worker told to size itself against eight cores should reserve."""

_TARGETS = (
    ("hyperspy", "mean_projection", "analysis"),
    ("libertem", "sum_projection", "libertem"),
    ("py4dstem", "fit_central_disk", "py4dstem"),
)


@pytest.fixture
def recording(tmp_path):
    """Write a small burst and return its path and its frames."""
    rng = np.random.default_rng(0)
    now = datetime.datetime.now(tz=datetime.UTC)
    frames = [
        Frame(
            data=rng.random((_EDGE, _EDGE), dtype=np.float32),
            timestamp=now,
            metadata={},
        )
        for _ in range(5)
    ]
    path = tmp_path / "burst.nxs"
    write_frames(path, frames, title="analysis worker test")
    return path, read_frames(path)


def _same(left: object, right: object) -> None:
    """Assert two analysis results are identical, arrays and numbers alike."""
    if isinstance(left, tuple):
        assert isinstance(right, tuple)
        assert len(left) == len(right)
        for one, other in zip(left, right, strict=True):
            _same(one, other)
        return
    if isinstance(left, np.ndarray):
        np.testing.assert_array_equal(left, right)
        return
    assert left == right


@pytest.mark.parametrize(("name", "method", "extra"), _TARGETS)
def test_the_isolated_result_equals_the_in_process_one(
    name,
    method,
    extra,
    recording,
):
    """
    Same numbers from both transports, for a file and for frames in memory.

    Both branches in one test because they take different routes to the
    same answer: the file case sends a path and the worker opens it, the
    in-memory case pushes 177 KB through a shared segment. A transport
    that corrupted either would show up here as a mismatch rather than as
    a plausible-looking image.
    """
    if not target_available(name):
        pytest.skip(f"requires the {extra!r} extra")
    path, frames = recording
    in_process = InProcessRunner()
    worker = WorkerRunner(name)
    try:
        for job in (
            AnalysisInput(path=str(path)),
            AnalysisInput(path=str(path), frames=frames, origin="burst.nxs"),
        ):
            _same(in_process.run(method, job), worker.run(method, job))
    finally:
        worker.close()


def test_the_client_process_never_imports_the_analysis_library(recording):
    """
    The property the whole boundary exists for, asserted rather than assumed.

    Run in a fresh interpreter because this test session has almost
    certainly imported HyperSpy already — every other test in this
    directory does — so checking ``sys.modules`` here would prove nothing.
    """
    if not target_available("hyperspy"):
        pytest.skip("requires the 'analysis' extra")
    path, _frames = recording
    program = (
        "import sys\n"
        "from miainwoodpecker.analysis.operations import AnalysisInput\n"
        "from miainwoodpecker.analysis.remote import WorkerRunner\n"
        "runner = WorkerRunner('hyperspy')\n"
        f"result = runner.run('mean_projection', AnalysisInput(path={str(path)!r}))\n"
        "runner.close()\n"
        "print(result.shape, 'hyperspy' in sys.modules, 'exspy' in sys.modules)\n"
    )
    finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
    )
    assert finished.stdout.strip().endswith("False False")
    assert f"({_EDGE}, {_EDGE})" in finished.stdout


def test_the_worker_runs_under_the_thread_budget_it_was_given(recording):
    """
    The one thing the in-process cap documents that it cannot do.

    ``analysis/threads.py`` says plainly that ``OMP_NUM_THREADS`` and
    friends are read once when the native library loads, so a runtime cap
    has to be ``threadpoolctl`` scoped in time. A worker's environment is
    set before it starts, so the cap is a property of the process — which
    is checkable by asking the worker what it sees.
    """
    if not target_available("hyperspy"):
        pytest.skip("requires the 'analysis' extra")
    if not pathlib.Path("/proc/self/environ").exists():
        pytest.skip("reads /proc, which only Linux has")
    runner = WorkerRunner("hyperspy", cpu_count=8)
    try:
        runner.run("mean_projection", AnalysisInput(path=str(recording[0])))
        assert runner.thread_budget == _EIGHT_CORE_BUDGET
        worker_pid = runner._process.pid  # noqa: SLF001 - inspecting the subprocess
        recorded = pathlib.Path(f"/proc/{worker_pid}/environ").read_bytes()
        # The environment the process *started with*, which is the only
        # one these variables are ever read from.
        assert f"OMP_NUM_THREADS={_EIGHT_CORE_BUDGET}\x00".encode() in recorded
        assert f"OPENBLAS_NUM_THREADS={_EIGHT_CORE_BUDGET}\x00".encode() in recorded
        assert f"NUMBA_NUM_THREADS={_EIGHT_CORE_BUDGET}\x00".encode() in recorded
    finally:
        runner.close()


def test_a_worker_killed_between_calls_is_replaced_transparently(recording):
    """
    Restarting is the deliberate inversion of the device layer's rule.

    ``devices/remote.py`` refuses to reconnect because a fresh device
    server is a fresh instrument and a recording in progress would keep
    appending frames from differently-configured hardware. A fresh
    analysis worker is nothing of the kind: its input is still on disk,
    so the next call simply gets a new process. That is what makes a
    segfault in a native analysis dependency cost a result rather than
    the session — the crash-isolation argument, exercised rather than
    asserted in prose.
    """
    if not target_available("hyperspy"):
        pytest.skip("requires the 'analysis' extra")
    path, _frames = recording
    job = AnalysisInput(path=str(path))
    runner = WorkerRunner("hyperspy")
    try:
        runner.run("mean_projection", job)
        killed = runner._process  # noqa: SLF001 - simulating a crash
        killed.kill()
        killed.wait()
        assert runner.run("mean_projection", job).shape == (_EDGE, _EDGE)
        assert runner._process.pid != killed.pid  # noqa: SLF001
    finally:
        runner.close()


def test_a_failing_analysis_does_not_take_the_worker_with_it(recording):
    """
    One bad call is an error, not a dead process, exactly as on the device side.

    ``serving.invoke`` turns a raised exception into a ``Result`` carrying
    its message, which is why this works without the analysis layer
    writing any error handling of its own. The follow-up call is the
    assertion that matters: the client retires the connection on failure,
    so "the error was reported" and "the runner is still usable" are two
    separate claims.
    """
    if not target_available("hyperspy"):
        pytest.skip("requires the 'analysis' extra")
    path, _frames = recording
    runner = WorkerRunner("hyperspy")
    try:
        with pytest.raises(RemoteCallError):
            runner.run(
                "mean_projection",
                AnalysisInput(path=str(path.parent / "not-a-recording.nxs")),
            )
        assert runner.run(
            "mean_projection",
            AnalysisInput(path=str(path)),
        ).shape == (_EDGE, _EDGE)
    finally:
        runner.close()


def test_no_shared_memory_segments_survive_a_worker_session(recording):
    """
    The obligation reuse creates, checked the way the device layer checks it.

    A named POSIX segment is a tmpfs entry, not a process resource, so
    nothing reclaims it automatically. This path has an extra hazard the
    device path does not: the *reader* lives in the short-lived worker, so
    without ``SharedFrameReader(stop_tracking=True)`` that worker's
    ``resource_tracker`` unlinks the client's live segment on the way out.
    That failure was found by running this path, and this is the test that
    keeps it found.
    """
    if not target_available("hyperspy"):
        pytest.skip("requires the 'analysis' extra")
    path, frames = recording
    before = {entry.name for entry in _SHM_DIR.iterdir()}
    runner = WorkerRunner("hyperspy")
    try:
        job = AnalysisInput(path=str(path), frames=frames)
        assert runner.run("mean_projection", job).shape == (_EDGE, _EDGE)
        # The client's request really did use a segment, or this test would
        # pass without exercising anything.
        assert runner._writer._segment is not None  # noqa: SLF001
    finally:
        runner.close()
    assert {entry.name for entry in _SHM_DIR.iterdir()} - before == set()


def test_the_worker_refuses_an_unknown_target_on_its_command_line():
    """
    argparse, not a KeyError halfway through serving.

    The worker is a documented entry point (``python -m
    miainwoodpecker.analysis.worker``), so a typo in its argv is a thing a
    person does, and it should produce a usage message.
    """
    finished = subprocess.run(
        [sys.executable, "-m", "miainwoodpecker.analysis.worker", "hyperspx", "1"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "MIAINWOODPECKER_AUTHKEY": "00"},
    )
    assert finished.returncode != 0
    assert "invalid choice" in finished.stderr
