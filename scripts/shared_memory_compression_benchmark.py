"""
Measure whether zstd compression helps the shared-memory frame path.

docs/migration-plan.md §6 already replaced one-shot shared-memory segments
with persistent, reused ones (:mod:`miainwoodpecker.devices.shared_frame`),
so the transfer cost per frame is now essentially one memcpy in
(``SharedFrameWriter.publish``) and one memcpy out
(``SharedFrameReader.read``). This script asks a follow-on question: can
transparent zstd compression - using idle CPU cores in parallel, via
``zstandard.ZstdCompressor(threads=N)`` - reduce end-to-end latency
further, by shrinking the bytes that cross the memcpy boundary?

The Phase 3 storage finding (§5's "Revisit compression" item) is the
reason to suspect not: gzip level 4 on noisy float64 scan data measured a
*1.08x* ratio (bigger than raw), while float32 camera frames compressed
to 0.69x. That is an HDF5-storage measurement, not this IPC path, but it
is a real prior data point about how this project's actual detector data
(photon/thermal noise, not smooth natural images) responds to a generic
compressor - worth checking whether zstd does any better before writing
compression off, or assuming it helps.

This measures, for real frames acquired through the actual usim-backed
device server (:func:`~miainwoodpecker.devices.remote.remote_simulated_instrument`,
not synthesized arrays):

- compression ratio at a fast zstd level (1), a mid level (3), and two
  balanced levels (9, 12), single-threaded and with
  ``threads=os.cpu_count()`` (standing in for "idle cores" - the whole
  machine, since this container has none reserved elsewhere).
- compress+decompress wall time for each of those configurations.
- the actual :class:`~miainwoodpecker.devices.shared_frame.SharedFrameWriter`/
  :class:`~miainwoodpecker.devices.shared_frame.SharedFrameReader` raw
  memcpy path, timed the same way, as the baseline compression would have
  to beat.

The compressed-path numbers are measured through the *same* writer/reader
classes as the raw path (the compressed bytes are published as a 1-D
``uint8`` "frame" and read back), not a separate mock - so the comparison
includes the real memcpy cost of whatever's actually being moved through
shared memory in each case, not just an isolated compression benchmark.

Run with: uv run --extra device python scripts/shared_memory_compression_benchmark.py
(or: install zstandard into that environment first, e.g.
``uv pip install zstandard``, since it is not yet a project dependency -
see this investigation's conclusion in docs/migration-plan.md §6 for why).
"""

from __future__ import annotations

import os
import statistics
import time

import numpy as np
import zstandard

from miainwoodpecker.devices.interface import Frame, ScanParameters
from miainwoodpecker.devices.remote import remote_simulated_instrument
from miainwoodpecker.devices.shared_frame import SharedFrameReader, SharedFrameWriter

_LEVELS = (1, 3, 9, 12)
_REPEATS = 8


def _percentile(values: list[float], fraction: float) -> float:
    """Return a simple nearest-rank percentile of the given samples."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _report(label: str, samples_ms: list[float]) -> float:
    """Print median/p95 for a set of timings and return the median."""
    median_ms = statistics.median(samples_ms)
    p95_ms = _percentile(samples_ms, 0.95)
    print(f"    {label}: median={median_ms:.2f}ms p95={p95_ms:.2f}ms")
    return median_ms


def _time_raw_shared_memory(frame: Frame, repeats: int) -> list[float]:
    """Time the actual publish()+read() memcpy round trip, warmed up first."""
    writer = SharedFrameWriter()
    reader = SharedFrameReader()
    try:
        reader.read(writer.publish(frame))  # warm up: first call resizes
        samples_ms = []
        for _ in range(repeats):
            started = time.perf_counter()
            reader.read(writer.publish(frame))
            samples_ms.append((time.perf_counter() - started) * 1000.0)
        return samples_ms
    finally:
        writer.close()
        reader.close()


def _time_compressed_shared_memory(
    frame: Frame,
    level: int,
    threads: int,
    repeats: int,
) -> tuple[list[float], float]:
    """
    Time compress -> publish -> read -> decompress through the real classes.

    Returns
    -------
    tuple[list[float], float]
        Per-iteration wall times in ms, and the compression ratio
        (compressed bytes / raw bytes).
    """
    raw = np.ascontiguousarray(frame.data)
    raw_bytes = raw.tobytes()
    compressor = zstandard.ZstdCompressor(level=level, threads=threads)
    decompressor = zstandard.ZstdDecompressor()
    writer = SharedFrameWriter()
    reader = SharedFrameReader()

    def _round_trip() -> tuple[float, int]:
        started = time.perf_counter()
        compressed = compressor.compress(raw_bytes)
        compressed_frame = Frame(
            data=np.frombuffer(compressed, dtype=np.uint8),
            timestamp=frame.timestamp,
            metadata=frame.metadata,
        )
        received = reader.read(writer.publish(compressed_frame))
        decompressor.decompress(received.data.tobytes())
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return elapsed_ms, len(compressed)

    try:
        _round_trip()  # warm up
        samples_ms = []
        compressed_size = 0
        for _ in range(repeats):
            elapsed_ms, compressed_size = _round_trip()
            samples_ms.append(elapsed_ms)
        ratio = compressed_size / len(raw_bytes)
        return samples_ms, ratio
    finally:
        writer.close()
        reader.close()


def _benchmark_frame(label: str, frame: Frame, threads: int) -> None:
    """Run the raw-vs-compressed comparison for one acquired frame."""
    megabytes = frame.data.nbytes / 1e6
    print(f"\n{label}: {frame.data.shape} {frame.data.dtype} = ~{megabytes:.2f}MB")

    raw_samples = _time_raw_shared_memory(frame, _REPEATS)
    raw_median = _report("raw shared memory (current design)", raw_samples)

    for level in _LEVELS:
        for thread_count, thread_label in (
            (1, "threads=1"),
            (threads, f"threads={threads}"),
        ):
            samples, ratio = _time_compressed_shared_memory(
                frame,
                level,
                thread_count,
                _REPEATS,
            )
            median = _report(
                f"zstd level={level} {thread_label}: ratio={ratio:.3f}",
                samples,
            )
            verdict = "FASTER" if median < raw_median else "slower"
            slowdown = median / raw_median
            print(f"      -> {verdict} than raw memcpy ({slowdown:.1f}x)")


def main() -> None:
    """Acquire real frames and run the raw-vs-compressed comparison on each."""
    cpu_count = os.cpu_count() or 1
    idle_threads = max(1, cpu_count)
    print(f"machine has {cpu_count} CPUs; using threads={idle_threads} for the MT case")

    scan_sizes = (64, 90, 128, 256, 512, 1024, 1536, 2048)

    with remote_simulated_instrument() as instrument:
        print("\n" + "=" * 70)
        print("SCAN FRAMES (float64, noisy - the gzip 1.08x finding's data)")
        print("=" * 70)
        for size in scan_sizes:
            parameters = ScanParameters(
                height=size,
                width=size,
                pixel_time_us=1.0,
                fov_nm=instrument.stage_size_nm * 0.1,
            )
            frame = instrument.scanner.scan_frame(parameters, 0)
            _benchmark_frame(f"scan {size}x{size}", frame, idle_threads)

        print("\n" + "=" * 70)
        print("CAMERA FRAMES (float32 - the gzip 0.69x finding's data)")
        print("=" * 70)
        instrument.eels_camera.start()
        try:
            eels_frame = instrument.eels_camera.acquire_frame()
        finally:
            instrument.eels_camera.stop()
        _benchmark_frame("EELS camera", eels_frame, idle_threads)

        instrument.ronchigram_camera.start()
        try:
            ronchigram_frame = instrument.ronchigram_camera.acquire_frame()
        finally:
            instrument.ronchigram_camera.stop()
        _benchmark_frame("Ronchigram camera", ronchigram_frame, idle_threads)


if __name__ == "__main__":
    main()
