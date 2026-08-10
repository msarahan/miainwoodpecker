"""
Measure the IPC overhead of the process-isolated device server.

docs/migration-plan.md §6 isolates Nion's GPL-3.0 device layer behind a
subprocess boundary (see ``miainwoodpecker.devices.remote``) rather than
importing it in-process, for licensing reasons. The concern raised
against that: STEM frames are large (a Ronchigram frame here is
2048x2048 float32, ~16.8MB; real 4D-STEM data is worse), and the naive
transport - pickling each ``Call``/``Result`` over a
``multiprocessing.connection`` socket - copies the array through
serialization plus a kernel socket buffer, twice per round trip.

This compares that naive transport against calling the same underlying
device directly in-process (no IPC at all), for camera frames (large,
fixed size) and scan frames at a few sizes (large and controllable). If
IPC overhead is small relative to the acquisition cost it rides alongside,
the naive transport is good enough and a shared-memory transport is not
worth the added complexity yet; if it dominates, that is the empirical
case for one.

Run with: uv run --extra device python scripts/ipc_overhead_benchmark.py
"""

from __future__ import annotations

import statistics
import time

from miainwoodpecker.devices.interface import ScanParameters
from miainwoodpecker.devices.nion_server import simulated_instrument as local_instrument
from miainwoodpecker.devices.remote import remote_simulated_instrument


def _percentile(values: list[float], fraction: float) -> float:
    """Return a simple nearest-rank percentile of the given samples."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _report(label: str, samples_ms: list[float]) -> None:
    """Print median/p95/max for a set of timings."""
    median_ms = statistics.median(samples_ms)
    print(
        f"  {label}: n={len(samples_ms)} "
        f"median={median_ms:.2f}ms "
        f"p95={_percentile(samples_ms, 0.95):.2f}ms "
        f"max={max(samples_ms):.2f}ms",
    )


def _time_camera_acquire(camera: object, frames: int) -> list[float]:
    """Time repeated acquire_frame() calls; excludes the first (warm-up) call."""
    camera.start()
    try:
        camera.acquire_frame()  # warm up: first call includes extra setup cost
        samples_ms = []
        for _ in range(frames):
            started = time.perf_counter()
            camera.acquire_frame()
            samples_ms.append((time.perf_counter() - started) * 1000.0)
        return samples_ms
    finally:
        camera.stop()


def _time_scan(scanner: object, parameters: ScanParameters, frames: int) -> list[float]:
    """Time repeated scan_frame() calls; excludes the first (warm-up) call."""
    scanner.scan_frame(parameters, 0)  # warm up
    samples_ms = []
    for _ in range(frames):
        started = time.perf_counter()
        scanner.scan_frame(parameters, 0)
        samples_ms.append((time.perf_counter() - started) * 1000.0)
    return samples_ms


def main() -> None:
    """Run the direct vs. IPC comparison and print a verdict."""
    frames = 20
    scan_sizes = (128, 512, 1024, 1536, 2048)

    print("camera (Ronchigram, 2048x2048 float32 = ~16.8MB/frame):")
    with local_instrument() as local:
        direct_camera_ms = _time_camera_acquire(local.ronchigram_camera, frames)
    _report("direct (in-process)", direct_camera_ms)
    with remote_simulated_instrument() as remote:
        remote_camera_ms = _time_camera_acquire(remote.ronchigram_camera, frames)
    _report("IPC (subprocess)", remote_camera_ms)
    camera_overhead_ms = statistics.median(remote_camera_ms) - statistics.median(
        direct_camera_ms,
    )
    print(f"  IPC overhead: {camera_overhead_ms:+.2f}ms\n")

    for size in scan_sizes:
        megabytes = (size * size * 8) / 1e6  # scan data is float64
        print(f"scan ({size}x{size} float64 = ~{megabytes:.1f}MB/frame):")
        with local_instrument() as local:
            parameters = ScanParameters(
                height=size, width=size, pixel_time_us=1.0,
                fov_nm=local.stage_size_nm * 0.1,
            )
            direct_scan_ms = _time_scan(local.scanner, parameters, frames)
        _report("direct (in-process)", direct_scan_ms)
        with remote_simulated_instrument() as remote:
            parameters = ScanParameters(
                height=size, width=size, pixel_time_us=1.0,
                fov_nm=remote.stage_size_nm * 0.1,
            )
            remote_scan_ms = _time_scan(remote.scanner, parameters, frames)
        _report("IPC (subprocess)", remote_scan_ms)
        overhead_ms = statistics.median(remote_scan_ms) - statistics.median(
            direct_scan_ms,
        )
        print(f"  IPC overhead: {overhead_ms:+.2f}ms\n")


if __name__ == "__main__":
    main()
