"""
Re-run the analysis isolation questions against a real 4D-STEM datacube.

``scripts/analysis_ipc_benchmark.py`` measures what a process boundary
costs, using synthetic noise frames. Noise is the right input for a
*transport* benchmark — the shared-memory path does no compression, so it
cannot care what the bytes mean — and the wrong input for anything about
the operations themselves. ``docs/analysis-isolation.md`` said so, and
recorded "no 4D-STEM dataset was analysed" as an open item for as long as
the sample datasets sat behind an egress block.

They no longer do. This script is the other half: the same three
operations, on real experimental diffraction patterns, answering two
questions the synthetic benchmark structurally cannot.

1. **Does real data change the transport answer?** Real and synthetic
   frames of identical shape and dtype, interleaved, at the same payload
   size the sibling benchmark reports. Expected: no. Measured anyway,
   because "expected" is how the claims this project keeps having to
   correct got written.
2. **Does ``fit_central_disk`` mean anything on synthetic input?** It
   does not, and that is the finding worth having. ``get_probe_size``
   thresholds and takes a centroid, so on any structureless field it
   returns a confident, centred, entirely fictitious disk — which means
   every synthetic-data exercise of the viewer's "Fit central disk"
   button could not have failed. The controls here demonstrate that, and
   the real patterns show both what a good fit looks like and the two
   ways real data breaks it.

The dataset is Zenodo record 8233585, "Mixed Phase Test Datasets for
py4dstem", CC-BY-4.0: a (254, 255, 384, 384) uint8 datacube, 153 MB on
disk and 9.55 GB raw. It is fetched over plain HTTPS from Zenodo's REST
API rather than through py4DSTEM's own downloader, which is Google
Drive-backed and still unreachable here (see
``analysis/py4dstem_bridge.py``).

Run with:
    uv run --extra analysis --extra libertem --extra py4dstem \
        python scripts/real_4dstem_benchmark.py --download

``--dataset`` points at an already-downloaded cube instead;
``--frames`` sets the stack depth for the transport comparison (142 x
384 x 384 float32 is 83.8 MB, matching the sibling benchmark's largest
in-memory row); ``--repeats`` sets the timed calls per arm.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
import typing
import urllib.request
from pathlib import Path

import h5py
import numpy as np

from miainwoodpecker.analysis import remote
from miainwoodpecker.analysis.operations import AnalysisInput
from miainwoodpecker.analysis.remote import InProcessRunner, WorkerRunner
from miainwoodpecker.analysis.threads import limit_analysis_threads
from miainwoodpecker.storage.calibration import (
    AxisCalibration,
    AxisKind,
    FrameCalibration,
)
from miainwoodpecker.storage.nexus import FrameStack

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

_ZENODO_URL = (
    "https://zenodo.org/api/records/8233585/files/20210306_084059.hdf5/content"
)
"""Zenodo 8233585, CC-BY-4.0. See the module docstring."""

_DATASET_PATH = "Experiments/__unnamed__/data"
"""Where the cube lives inside that file, which is HyperSpy's HDF5 layout."""

_DEFAULT_FRAMES = 142
"""142 x 384 x 384 float32 = 83.8 MB, the sibling benchmark's 84 MB row."""

_SCAN_ROW = 100
"""An arbitrary but fixed scan row, so runs are comparable."""

_BURST_FRAMES = 5
"""Frames per burst, matching the viewer's ``_ANALYSIS_BURST_FRAME_COUNT``."""

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


def _fetch(destination: Path) -> Path:
    """
    Download the Zenodo cube unless it is already there.

    Parameters
    ----------
    destination : Path
        Where to put it.

    Returns
    -------
    Path
        The same path, now populated.
    """
    if destination.exists():
        print(f"using cached {destination} ({destination.stat().st_size / 1e6:.0f}MB)")
        return destination
    print(f"downloading {_ZENODO_URL}\n  -> {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Zenodo over plain HTTPS; no credentials, no vendor downloader.
    with urllib.request.urlopen(_ZENODO_URL) as response:
        destination.write_bytes(response.read())
    print(f"  {destination.stat().st_size / 1e6:.0f}MB")
    return destination


def _diffraction_calibration() -> FrameCalibration:
    """
    Return a reciprocal-space calibration for the cube's detector axes.

    The scale is nominal: this dataset publishes no pixel size, and none
    of the three operations depends on one. It is set rather than left
    uncalibrated so the adapters take the same path they would on a real
    recording.

    Returns
    -------
    FrameCalibration
        The same calibration on both frame axes.
    """
    axis = AxisCalibration(AxisKind.RECIPROCAL_SPACE, scale=0.01, units="1/nm")
    return FrameCalibration(y=axis, x=axis)


def _as_stack(data: np.ndarray) -> FrameStack:
    """
    Wrap an ``(n, h, w)`` array as the triple the adapters take.

    Parameters
    ----------
    data : np.ndarray
        The frames.

    Returns
    -------
    FrameStack
        Frames, synthetic frame times, and a diffraction calibration.
    """
    return FrameStack(
        data=data,
        frame_time=np.arange(len(data), dtype=np.float64) * 0.01,
        calibration=_diffraction_calibration(),
    )


def _describe(label: str, frames: np.ndarray) -> None:
    """
    Print the sparsity of a frame stack, which is the point of using real data.

    Parameters
    ----------
    label : str
        What these frames are.
    frames : np.ndarray
        The stack.
    """
    print(
        f"  {label}: {frames.shape} {frames.dtype} "
        f"{frames.nbytes / 1e6:.1f}MB max={frames.max():.0f} "
        f"mean={frames.mean():.3f} "
        f"nonzero={np.count_nonzero(frames) / frames.size:.4f}",
    )


def _fit(pattern: np.ndarray) -> tuple[float, float, float]:
    """
    Fit one diffraction pattern's central disk, as the viewer's button does.

    Parameters
    ----------
    pattern : np.ndarray
        A single 2D pattern.

    Returns
    -------
    tuple[float, float, float]
        Radius and centre in pixels, as ``(radius, x0, y0)``.
    """
    from py4DSTEM.process.calibration import get_probe_size  # noqa: PLC0415

    radius, x0, y0 = get_probe_size(pattern)
    return float(radius), float(x0), float(y0)


def _report_fit(label: str, pattern: np.ndarray) -> None:
    """
    Fit one pattern and print the result on one line.

    Parameters
    ----------
    label : str
        What was fitted.
    pattern : np.ndarray
        The pattern.
    """
    radius, x0, y0 = _fit(pattern)
    print(f"    {label:<34} radius={radius:8.2f}px centre=({x0:6.1f}, {y0:6.1f})")


def _controls(edge: int) -> None:
    """
    Fit structureless inputs, to show what a synthetic-data check proves.

    Nothing here contains a disk. Every row that returns a plausible
    radius at the array centre is a row where the viewer's button would
    have looked like it worked.

    Parameters
    ----------
    edge : int
        Detector edge length, so the controls match the real patterns.
    """
    rng = np.random.default_rng(0)
    shape = (edge, edge)
    print("\n  controls -- structureless input, no disk present:")
    for label, pattern in (
        ("uniform noise U(0,1)", rng.random(shape, dtype=np.float32)),
        ("gaussian noise", rng.normal(0, 1, shape).astype(np.float32)),
        ("poisson lam=0.03", rng.poisson(0.03, shape).astype(np.float32)),
        ("all ones", np.ones(shape, np.float32)),
        ("all zeros", np.zeros(shape, np.float32)),
    ):
        _report_fit(label, pattern)


def _disk_fits(handle: h5py.File, burst: np.ndarray) -> None:
    """
    Fit real patterns at three dose levels, and the controls beside them.

    Parameters
    ----------
    handle : h5py.File
        The open cube.
    burst : np.ndarray
        The five-pattern burst already read, so it is not read twice.
    """
    cube = handle[_DATASET_PATH]
    print("\ncentral-disk fits")
    _controls(int(cube.shape[-1]))

    print("\n  real patterns, by how many electrons went into them:")
    row = np.asarray(cube[_SCAN_ROW], dtype=np.float32).sum(axis=0)
    _report_fit(f"{cube.shape[1]} patterns (one scan row)", row)
    sampled = np.asarray(cube[::16, ::16], dtype=np.float32)
    _report_fit(
        f"{sampled.shape[0] * sampled.shape[1]} patterns (across the scan)",
        sampled.reshape(-1, *sampled.shape[-2:]).sum(axis=0),
    )
    block = np.asarray(cube[120:136, 120:136], dtype=np.float32)
    _report_fit(
        f"{block.shape[0] * block.shape[1]} patterns (one mid-scan block)",
        block.reshape(-1, *block.shape[-2:]).sum(axis=0),
    )
    radii = [f"{_fit(burst[index])[0]:.2f}" for index in range(len(burst))]
    print(f"    {'single patterns, x5 consecutive':<34} radii={', '.join(radii)}")
    print(
        "    ^ the viewer's button fits ONE pattern. Compare that spread, and\n"
        "      the mid-scan block, against the two rows that worked.",
    )


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
    jobs: dict[str, AnalysisInput],
    repeats: int,
) -> dict[str, tuple[list[float], list[float]]]:
    """
    Time every job on both transports, round-robin, so machine drift cancels.

    The sibling benchmark interleaves the two *transports* for the reason
    its own ``_interleaved`` docstring gives. This interleaves the two
    *datasets* as well, and for the same reason found the same way: a
    first attempt that measured real frames to completion and then
    synthetic frames reported a 2337 ms real median against 46 ms
    synthetic, i.e. a 50x "real data penalty" on a transport that does
    not look at the data. Interleaved, the two are indistinguishable.

    The in-process arm runs inside the cap the viewer applies to it
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
    jobs : dict[str, AnalysisInput]
        The inputs to compare, keyed by label.
    repeats : int
        Timed calls per arm per job, after one warm-up each.

    Returns
    -------
    dict[str, tuple[list[float], list[float]]]
        In-process and isolated wall times per job, in milliseconds.
    """
    for job in jobs.values():
        with limit_analysis_threads():
            in_process.run(method, job)  # warm up: imports, JIT, page cache
        worker.run(method, job)
    samples: dict[str, tuple[list[float], list[float]]] = {
        label: ([], []) for label in jobs
    }
    for _ in range(repeats):
        for label, job in jobs.items():
            here, there = samples[label]
            with limit_analysis_threads():
                here.append(_time_one(in_process, method, job))
            there.append(_time_one(worker, method, job))
    return samples


def _transport(real: np.ndarray, repeats: int) -> None:
    """
    Compare both transports on real and synthetic frames of the same size.

    Parameters
    ----------
    real : np.ndarray
        The real frame stack.
    repeats : int
        Timed calls per arm per dataset.
    """
    synthetic = np.random.default_rng(0).random(real.shape, dtype=np.float32)
    print(f"\ntransport, {real.nbytes / 1e6:.1f}MB in memory, real vs synthetic")
    _describe("real     ", real)
    _describe("synthetic", synthetic)
    jobs = {
        "real ": AnalysisInput(frames=_as_stack(real), origin="real 4D-STEM burst"),
        "synth": AnalysisInput(
            frames=_as_stack(synthetic),
            origin="synthetic burst",
        ),
    }
    for name, method in _OPERATIONS:
        print(f"  {name}.{method}")
        if not remote.target_available(name):
            print(f"    {name}: not installed, skipped")
            continue
        in_process = InProcessRunner()
        worker = WorkerRunner(name)
        try:
            samples = _interleaved(in_process, worker, method, jobs, repeats)
            for label, (here, there) in samples.items():
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
        With ``dataset``, ``download``, ``frames`` and ``repeats``.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("20210306_084059.hdf5"),
        help="path to the cube; where --download puts it",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help=f"fetch it from Zenodo first ({_ZENODO_URL})",
    )
    parser.add_argument("--frames", type=int, default=_DEFAULT_FRAMES)
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
        Process exit status; 1 when the dataset is missing.
    """
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    dataset = (
        _fetch(arguments.dataset) if arguments.download else arguments.dataset
    )
    if not dataset.exists():
        print(f"no dataset at {dataset}; pass --download or --dataset", file=sys.stderr)
        return 1
    # The in-process runner must not be quietly turned into a worker by an
    # environment variable left over from a previous experiment.
    os.environ.pop(remote.ISOLATION_ENV_VAR, None)
    with h5py.File(dataset, "r") as handle:
        cube = handle[_DATASET_PATH]
        print(
            f"\ncube {cube.shape} {cube.dtype} "
            f"({cube.nbytes / 1e9:.2f}GB raw, {dataset.stat().st_size / 1e6:.0f}MB "
            f"on disk)",
        )
        burst = np.asarray(
            cube[_SCAN_ROW, _SCAN_ROW : _SCAN_ROW + _BURST_FRAMES],
            dtype=np.float32,
        )
        _describe(f"{_BURST_FRAMES}-pattern burst", burst)
        _disk_fits(handle, burst)
        real = np.asarray(cube[_SCAN_ROW, : arguments.frames], dtype=np.float32)
    _transport(real, arguments.repeats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
