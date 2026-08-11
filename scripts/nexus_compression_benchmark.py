"""
Measure which HDF5 codec and storage dtype the NeXus writer should default to.

docs/migration-plan.md §5's Phase 3 "Revisit compression" item recorded a
bad result for the current default: gzip level 4 on noisy ``float64`` scan
data measured a *1.08x* ratio - the compressed file was **larger** than
raw - while ``float32`` camera frames reached 0.69x. The item names two
candidate fixes to evaluate: bitshuffle/blosc, and storing scan data as
``float32``. This script measures both, plus the cheaper thing the item's
phrasing implies but does not name: HDF5's own built-in ``shuffle``
byte-shuffle filter in front of the gzip that is already there.

Why shuffling is the specific technique to test: a generic compressor sees
an IEEE float array as an interleaved byte stream, where every 4th or 8th
byte is a near-constant sign/exponent and the rest is noisy mantissa, so
no useful run or match ever appears. Byte shuffle transposes the array
into byte planes, putting all the like-significance bytes of adjacent
floats next to each other - the exponent plane becomes long compressible
runs, and the noisy mantissa bytes are quarantined where they can only
fail to compress rather than poisoning everything around them.

**This is a different question from §6's resolved shared-memory
compression investigation, and that verdict does not transfer.** There,
compression lost because the shared-memory path is a same-host memcpy: no
wire, so there was no transport cost for a ratio win to amortize, and
zstd's per-byte cost is simply higher than a copy's. Disk bytes are not
free the same way - they cost storage capacity and, on every later
analysis read, read bandwidth. So ratio is worth paying *some* CPU for
here, and the question is how much.

Measured, for real frames acquired through the actual usim-backed device
server (:func:`~miainwoodpecker.devices.remote.remote_simulated_instrument`,
not synthesized arrays), on both data characters that behaved so
differently before - ``float64`` scan frames and ``float32`` camera frames
(EELS and Ronchigram):

- **ratio**: stored dataset bytes / raw logical bytes.
- **write time**: the whole streaming ``NexusWriter`` append loop *plus*
  close and ``fsync``, divided by frame count. Two measurement traps here,
  both of which this script avoids: timing ``append()`` alone measures
  when the write returned to userspace, not when it reached disk, which
  made compressed writes look up to 17x *faster* than uncompressed simply
  because they queue fewer dirty pages; and a single run's total is
  dominated by whatever writeback the kernel happened to schedule, so
  each configuration is written three times and the median taken.
- **write time with a per-frame ``flush()``**, as a separate column. A
  sibling investigation found that a ``SIGKILL`` mid-acquisition leaves a
  file that will not open at all, and that flushing after each append
  converts that into a merely-unfinalized file. Flushing pushes whole
  chunks through the filter pipeline, so what durability costs depends on
  the codec - which makes it this benchmark's business rather than a
  separate measurement.
- **read-back time**: reading the full frame stack into memory - what
  every analysis adapter in ``miainwoodpecker.analysis`` does. Measured
  warm (the file was just written, so it is in page cache) on purpose:
  that isolates the codec's *decompression* cost, which is the thing
  under comparison. A cold-cache read additionally pays for the bytes a
  worse ratio makes it fetch, so warm numbers understate the compressed
  options rather than flattering them. This matters as much as ratio: a
  codec that halves the file and doubles the read is a bad trade for an
  analysis workflow.

Everything is timed through the real :class:`~miainwoodpecker.storage.NexusWriter`
(resizable dataset, one chunk per frame, streaming appends), not a
separate h5py path, so the numbers describe the writer this project
ships.

Run with:
  uv run --extra device python scripts/nexus_compression_benchmark.py

``hdf5plugin`` supplies the blosc2/bitshuffle/zstd filters. It is a real
project dependency (unlike ``zstandard`` in the §6 benchmark, which was
installed ad hoc for a capability that did not ship), because the winner
this script picked is opt-in-able through it; see the writer's module
docstring for why the *default* deliberately stays on HDF5 built-ins.
"""

from __future__ import annotations

import os
import statistics
import tempfile
import time
import typing
from pathlib import Path

import h5py
import hdf5plugin
import numpy as np

from miainwoodpecker.devices.interface import Frame, ScanParameters
from miainwoodpecker.devices.remote import remote_simulated_instrument
from miainwoodpecker.storage import NexusWriter

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

_DATA_PATH = "entry/instrument/detector/data"
_SCAN_SIZES = (256, 512, 1024)
_SCAN_FRAMES = 4
_CAMERA_FRAMES = 3
_REPEATS = 3


def _codecs() -> tuple[tuple[str, dict[str, typing.Any]], ...]:
    """
    Return the codec configurations to sweep, labels included.

    The candidate set: no compression (the floor to beat on time), the
    current gzip default exactly as it ships today (the baseline to beat
    on ratio), the same gzip with HDF5's byte shuffle, a fast built-in
    alternative, and the plugin codecs the open item names. Kept in a
    function rather than a module constant because the ``hdf5plugin``
    filter objects are constructed, not literals.

    Returns
    -------
    tuple[tuple[str, dict[str, typing.Any]], ...]
        ``(label, NexusWriter keyword arguments)`` pairs.
    """
    return (
        ("none", {"compression": None, "shuffle": False}),
        ("gzip-4 (current default)", {"compression": "gzip", "shuffle": False}),
        ("gzip-4 +shuffle", {"compression": "gzip", "shuffle": True}),
        (
            "gzip-1 +shuffle",
            {"compression": "gzip", "compression_opts": 1, "shuffle": True},
        ),
        ("lzf +shuffle", {"compression": "lzf", "shuffle": True}),
        (
            "blosc2 zstd-5 shuffle",
            {
                "compression": hdf5plugin.Blosc2(
                    cname="zstd",
                    clevel=5,
                    filters=hdf5plugin.Blosc2.SHUFFLE,
                ),
            },
        ),
        (
            "blosc2 zstd-5 bitshuffle",
            {
                "compression": hdf5plugin.Blosc2(
                    cname="zstd",
                    clevel=5,
                    filters=hdf5plugin.Blosc2.BITSHUFFLE,
                ),
            },
        ),
        (
            "blosc2 lz4-5 bitshuffle",
            {
                "compression": hdf5plugin.Blosc2(
                    cname="lz4",
                    clevel=5,
                    filters=hdf5plugin.Blosc2.BITSHUFFLE,
                ),
            },
        ),
        ("bitshuffle +lz4", {"compression": hdf5plugin.Bitshuffle(cname="lz4")}),
        ("zstd-3 (no shuffle)", {"compression": hdf5plugin.Zstd(clevel=3)}),
    )


class Result(typing.NamedTuple):
    """
    One codec's measurement on one frame stack.

    Parameters
    ----------
    label : str
        The codec configuration's name.
    ratio : float
        Stored dataset bytes divided by raw logical bytes.
    write_ms : float
        Median wall time to stream one frame in, in milliseconds.
    flushed_write_ms : float
        The same, with a ``flush()`` after every append.
    read_ms : float
        Wall time to read the whole stack back, in milliseconds.
    stored_bytes : int
        HDF5's reported storage size for the frame dataset.
    """

    label: str
    ratio: float
    write_ms: float
    flushed_write_ms: float
    read_ms: float
    stored_bytes: int


def _write_once(
    path: Path,
    frames: Sequence[Frame],
    spec: dict[str, typing.Any],
    *,
    flush_each_frame: bool = False,
) -> float:
    """
    Stream one frame stack to disk and return the elapsed wall time.

    The timed region includes the writer's ``close()`` and an ``fsync``,
    so the number describes bytes actually reaching the device rather than
    dirty pages reaching the kernel.

    Parameters
    ----------
    path : Path
        Destination file, overwritten.
    frames : Sequence[Frame]
        Frames to persist.
    spec : dict[str, typing.Any]
        Keyword arguments for :class:`~miainwoodpecker.storage.NexusWriter`.
    flush_each_frame : bool
        Call :meth:`~miainwoodpecker.storage.NexusWriter.flush` after every
        append, which is what bounds worst-case loss if the process is
        killed mid-acquisition. Costed separately because it pushes whole
        chunks through the filter pipeline, so the price depends on the
        codec being compared.

    Returns
    -------
    float
        Elapsed time in seconds for the whole write.
    """
    started = time.perf_counter()
    with NexusWriter(path, **spec) as writer:
        for frame in frames:
            writer.append(frame)
            if flush_each_frame:
                writer.flush()
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return time.perf_counter() - started


def _measure(
    path: Path,
    frames: Sequence[Frame],
    spec: dict[str, typing.Any],
    label: str,
) -> Result:
    """
    Write one frame stack with one codec, then read it all back, timed.

    Parameters
    ----------
    path : Path
        Destination file, overwritten.
    frames : Sequence[Frame]
        The already-acquired frames to persist, so acquisition time never
        lands inside a timed region.
    spec : dict[str, typing.Any]
        Keyword arguments for :class:`~miainwoodpecker.storage.NexusWriter`.
    label : str
        The codec configuration's name, for the returned result.

    Returns
    -------
    Result
        Ratio and timings for this configuration.
    """
    write_seconds = [_write_once(path, frames, spec) for _ in range(_REPEATS)]
    flushed_seconds = [
        _write_once(path, frames, spec, flush_each_frame=True)
        for _ in range(_REPEATS)
    ]

    read_ms: list[float] = []
    with h5py.File(path, "r") as handle:
        dataset = handle[_DATA_PATH]
        stored = dataset.id.get_storage_size()
        logical = int(np.prod(dataset.shape)) * dataset.dtype.itemsize
        for _ in range(_REPEATS):
            started = time.perf_counter()
            dataset[()]
            read_ms.append((time.perf_counter() - started) * 1000.0)

    return Result(
        label=label,
        ratio=stored / logical,
        write_ms=statistics.median(write_seconds) * 1000.0 / len(frames),
        flushed_write_ms=statistics.median(flushed_seconds) * 1000.0 / len(frames),
        read_ms=statistics.median(read_ms),
        stored_bytes=stored,
    )


def _benchmark_stack(
    title: str,
    frames: Sequence[Frame],
    *,
    dtype: str | None = None,
) -> None:
    """
    Sweep every codec over one acquired frame stack and print the table.

    Parameters
    ----------
    title : str
        Heading for this stack's table.
    frames : Sequence[Frame]
        The acquired frames to persist.
    dtype : str | None
        Storage dtype override passed to the writer, or None to keep the
        frames' own dtype. Used to isolate the float32 question.
    """
    sample = frames[0].data
    stored_itemsize = np.dtype(dtype).itemsize if dtype else sample.dtype.itemsize
    logical_mb = sample.size * stored_itemsize * len(frames) / 1e6
    label_dtype = dtype or str(sample.dtype)
    print(f"\n{title}")
    print(
        f"  {len(frames)} x {sample.shape} {sample.dtype} "
        f"stored as {label_dtype} = {logical_mb:.2f}MB logical",
    )
    print(
        f"  {'codec':26s} {'ratio':>7s} {'write/frame':>12s} "
        f"{'+flush':>10s} {'read all':>10s}",
    )

    with tempfile.TemporaryDirectory() as directory:
        for label, spec in _codecs():
            full_spec = dict(spec)
            if dtype is not None:
                full_spec["dtype"] = dtype
            result = _measure(
                Path(directory) / "bench.nxs",
                frames,
                full_spec,
                label,
            )
            print(
                f"  {result.label:26s} {result.ratio:7.3f} "
                f"{result.write_ms:9.1f}ms {result.flushed_write_ms:7.1f}ms "
                f"{result.read_ms:8.1f}ms",
            )


def _report_precision(frames: Sequence[Frame]) -> None:
    """
    Report whether float64 scan frames actually carry >float32 information.

    The open item asks whether storing scan data as ``float32`` is
    acceptable. That is a precision question, not a size question, so it
    is answered by looking at the data rather than asserting either way.

    Parameters
    ----------
    frames : Sequence[Frame]
        Scan frames to inspect.
    """
    data = frames[0].data
    print("\n" + "-" * 70)
    print("PRECISION: is float64 scan data carrying real float64 information?")
    print("-" * 70)
    print(f"  dtype={data.dtype} min={data.min():.6g} max={data.max():.6g}")
    print(f"  mean={data.mean():.6g} noise std={data.std():.6g}")
    if data.dtype != np.float64:
        print("  not float64; nothing to check")
        return
    round_tripped = data.astype(np.float32).astype(np.float64)
    error = np.abs(round_tripped - data)
    print(f"  float32 round trip exact: {np.array_equal(round_tripped, data)}")
    print(f"  max abs error from float32: {error.max():.6g}")
    print(f"  ... as a fraction of the frame's own noise std: "
          f"{error.max() / data.std():.3g}")


def main() -> None:
    """Acquire real frames and sweep every codec/dtype configuration."""
    print(f"hdf5plugin {hdf5plugin.version}, h5py {h5py.version.version}, "
          f"HDF5 {h5py.version.hdf5_version}")

    with remote_simulated_instrument() as instrument:
        print("\n" + "=" * 70)
        print("SCAN FRAMES (float64 - the gzip 1.08x finding's data)")
        print("=" * 70)
        scan_stacks = {}
        for size in _SCAN_SIZES:
            parameters = ScanParameters(
                height=size,
                width=size,
                pixel_time_us=1.0,
                fov_nm=instrument.stage_size_nm * 0.1,
            )
            frames = [
                instrument.scanner.scan_frame(parameters, 0)
                for _ in range(_SCAN_FRAMES)
            ]
            scan_stacks[size] = frames
            _benchmark_stack(f"scan {size}x{size}, as acquired", frames)

        print("\n" + "=" * 70)
        print("SCAN FRAMES, DOWNCAST TO float32 (the open item's other suggestion)")
        print("=" * 70)
        for size, frames in scan_stacks.items():
            _benchmark_stack(
                f"scan {size}x{size}, stored as float32",
                frames,
                dtype="float32",
            )

        _report_precision(scan_stacks[_SCAN_SIZES[0]])

        print("\n" + "=" * 70)
        print("CAMERA FRAMES (float32 - the gzip 0.69x finding's data)")
        print("=" * 70)
        instrument.eels_camera.start()
        try:
            eels = [
                instrument.eels_camera.acquire_frame() for _ in range(_CAMERA_FRAMES)
            ]
        finally:
            instrument.eels_camera.stop()
        _benchmark_stack("EELS camera", eels)

        instrument.ronchigram_camera.start()
        try:
            ronchigram = [
                instrument.ronchigram_camera.acquire_frame()
                for _ in range(_CAMERA_FRAMES)
            ]
        finally:
            instrument.ronchigram_camera.stop()
        _benchmark_stack("Ronchigram camera", ronchigram)


if __name__ == "__main__":
    main()
