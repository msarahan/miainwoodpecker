"""
Measure what isolating the analysis libraries in a worker process costs.

docs/analysis-isolation.md asks whether the analysis layer should get a
process boundary like the one docs/migration-plan.md §6 built for the
device layer, and this is the measurement half of the answer. Same
structure and the same reporting as
``scripts/ipc_overhead_benchmark.py``, which measured the device side:
run the identical operation both ways and print the difference, rather
than reasoning about it.

Three things are timed, because they are three different decisions:

1. **Worker startup**, once per target. A cold ``import py4DSTEM`` is
   seconds, so this is what decides whether a worker can be spawned per
   job or has to be long-lived.
2. **A call whose input is a file** — the fresh-burst case, and the one
   the viewer takes for every analysis of data it just acquired. Nothing
   crosses the boundary but a path, so this isolates the protocol's own
   overhead from any array transport.
3. **A call whose input is a frame stack already in memory** — the
   opened-recording case. This is where the boundary has to move real
   data, up to five 2048x2048 float32 frames (84 MB), and where the
   shared-memory path earns or fails to earn its place.

Run with:
    uv run --extra analysis --extra libertem --extra py4dstem \
        python scripts/analysis_ipc_benchmark.py

``--sizes`` takes frame edge lengths (default ``256 512 1024 2048``) and
``--repeats`` the number of timed calls per configuration.
"""

from __future__ import annotations

import argparse
import datetime
import os
import statistics
import sys
import tempfile
import time
import typing
from pathlib import Path

import numpy as np

from miainwoodpecker.analysis import remote
from miainwoodpecker.analysis.operations import AnalysisInput
from miainwoodpecker.analysis.remote import InProcessRunner, WorkerRunner
from miainwoodpecker.analysis.threads import limit_analysis_threads
from miainwoodpecker.devices.interface import Frame
from miainwoodpecker.storage.nexus import read_frames, write_frames

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

    from miainwoodpecker.storage.nexus import FrameStack

_BURST_FRAMES = 5
"""Frames per burst, matching the viewer's own ``_ANALYSIS_BURST_FRAME_COUNT``."""

_OPERATIONS = (
    ("hyperspy", "mean_projection"),
    ("libertem", "sum_projection"),
    ("py4dstem", "fit_central_disk"),
)


def _percentile(values: list[float], fraction: float) -> float:
    """
    Return a simple nearest-rank percentile of the given samples.

    Parameters
    ----------
    values : list[float]
        The samples.
    fraction : float
        The percentile, as a fraction between 0 and 1.

    Returns
    -------
    float
        The sample at that rank, or NaN when there are no samples.
    """
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _report(label: str, samples_ms: list[float], baseline_ms: float | None) -> float:
    """
    Print median/p95 for a set of timings, with the delta from a baseline.

    Parameters
    ----------
    label : str
        What was timed.
    samples_ms : list[float]
        The timings, in milliseconds.
    baseline_ms : float | None
        The in-process median to compare against, or None when this *is*
        the baseline.

    Returns
    -------
    float
        This set's median, so the caller can pass it back as the next
        baseline.
    """
    median_ms = statistics.median(samples_ms)
    delta = ""
    if baseline_ms is not None:
        delta = f" ({median_ms - baseline_ms:+.1f}ms, {median_ms / baseline_ms:.2f}x)"
    print(
        f"    {label}: n={len(samples_ms)} median={median_ms:.1f}ms "
        f"p95={_percentile(samples_ms, 0.95):.1f}ms{delta}",
    )
    return median_ms


def _make_recording(directory: Path, edge: int) -> tuple[Path, FrameStack]:
    """
    Write a burst of noise frames and read it back.

    Noise rather than a smooth pattern on purpose: it is what a detector
    actually produces, and it is the input the device-side benchmarks
    already established compresses and copies like real data.

    Parameters
    ----------
    directory : Path
        Where to write the file.
    edge : int
        Frame edge length in pixels.

    Returns
    -------
    tuple[Path, FrameStack]
        The written file and its frames, read back so the in-memory case
        starts from exactly what the viewer would hold.
    """
    rng = np.random.default_rng(0)
    now = datetime.datetime.now(tz=datetime.UTC)
    frames = [
        Frame(
            data=rng.random((edge, edge), dtype=np.float32),
            timestamp=now,
            metadata={},
        )
        for _ in range(_BURST_FRAMES)
    ]
    path = directory / f"burst_{edge}.nxs"
    write_frames(path, frames, title="analysis ipc benchmark")
    return path, read_frames(path)


def _time_one(
    runner: InProcessRunner | WorkerRunner,
    method: str,
    job: AnalysisInput,
) -> float:
    """
    Time one analysis call.

    Parameters
    ----------
    runner : InProcessRunner | WorkerRunner
        The runner to drive.
    method : str
        The operation name.
    job : AnalysisInput
        The input to analyse.

    Returns
    -------
    float
        Wall time in milliseconds.
    """
    started = time.perf_counter()
    runner.run(method, job)
    return (time.perf_counter() - started) * 1000.0


def _interleaved(
    in_process: InProcessRunner,
    worker: WorkerRunner,
    method: str,
    job: AnalysisInput,
    repeats: int,
) -> tuple[list[float], list[float]]:
    """
    Time both transports alternately, so machine drift cancels.

    Not a refinement: measuring one arm to completion and then the other
    produced a *reproducible* 400-560 ms "isolation overhead" for
    py4DSTEM at 2048x2048 that vanished when the isolated arm was run on
    its own, and that turned out to be the container getting slower over
    the run rather than anything the boundary did. Alternating is the
    standard fix, and the difference between the two orderings is the
    reason this function exists rather than a simpler loop.

    The in-process arm runs inside the same cap the viewer applies to it
    (``viewer/jobs.py``'s ``AnalysisJob._work``), because the worker runs
    under the thread budget its environment was spawned with and giving
    only one of them every core would not be a comparison.

    Parameters
    ----------
    in_process : InProcessRunner
        The in-process arm.
    worker : WorkerRunner
        The isolated arm, already started.
    method : str
        The operation name.
    job : AnalysisInput
        The input to analyse.
    repeats : int
        Timed calls per arm, after one warm-up each.

    Returns
    -------
    tuple[list[float], list[float]]
        In-process and isolated wall times, in milliseconds.
    """
    with limit_analysis_threads():
        in_process.run(method, job)  # warm up: imports, JIT, page cache
    worker.run(method, job)
    here: list[float] = []
    there: list[float] = []
    for _ in range(repeats):
        with limit_analysis_threads():
            here.append(_time_one(in_process, method, job))
        there.append(_time_one(worker, method, job))
    return here, there


def _run_target(
    name: str,
    method: str,
    path: Path,
    frames: FrameStack,
    repeats: int,
) -> None:
    """
    Compare both transports for one analysis target at one frame size.

    Parameters
    ----------
    name : str
        The analysis target.
    method : str
        The operation to run.
    path : Path
        The recording to analyse from disk.
    frames : FrameStack
        The same recording's frames, already in memory.
    repeats : int
        Timed calls per configuration.
    """
    if not remote.target_available(name):
        print(f"  {name}: not installed, skipped")
        return
    in_process = InProcessRunner()
    worker = WorkerRunner(name)
    started = time.perf_counter()
    worker.run(method, AnalysisInput(path=str(path)))
    print(
        f"    worker startup + first call: "
        f"{(time.perf_counter() - started) * 1000.0:.0f}ms "
        f"(thread budget {worker.thread_budget})",
    )
    try:
        for label, job in (
            ("from file", AnalysisInput(path=str(path))),
            ("from memory", AnalysisInput(path=str(path), frames=frames)),
        ):
            here, there = _interleaved(in_process, worker, method, job, repeats)
            baseline = _report(f"in-process, {label}", here, None)
            _report(f"isolated,   {label}", there, baseline)
    finally:
        worker.close()


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """
    Parse the script's command line.

    Parameters
    ----------
    argv : Sequence[str]
        Arguments after the program name.

    Returns
    -------
    argparse.Namespace
        With ``sizes`` and ``repeats``.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[256, 512, 1024, 2048])
    parser.add_argument("--repeats", type=int, default=5)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the benchmark.

    Parameters
    ----------
    argv : Sequence[str] | None
        Arguments after the program name, or None to read ``sys.argv``.

    Returns
    -------
    int
        Process exit status.
    """
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    # The in-process runner must not be quietly turned into a worker by an
    # environment variable left over from a previous experiment.
    os.environ.pop(remote.ISOLATION_ENV_VAR, None)
    with tempfile.TemporaryDirectory() as tmp_dir:
        directory = Path(tmp_dir)
        for edge in arguments.sizes:
            path, frames = _make_recording(directory, edge)
            megabytes = frames.data.nbytes / 1e6
            print(
                f"\n{edge}x{edge} float32 x{_BURST_FRAMES} frames "
                f"({megabytes:.1f}MB stack)",
            )
            for name, method in _OPERATIONS:
                print(f"  {name}.{method}")
                _run_target(name, method, path, frames, arguments.repeats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
