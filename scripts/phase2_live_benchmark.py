"""
Measure live-display latency to decide whether napari is fast enough.

Phase 2 of docs/migration-plan.md calls for benchmarking the live update
rate before committing to napari as the viewer shell, because the
pymmcore-plus team moved off napari toward ``ndv`` for exactly this
reason. This separates the two costs:

* **acquire** - how fast the device layer produces frames, with no
  display attached at all (the ceiling any viewer must keep up with).
* **display** - how long it takes to push a frame into a napari layer
  *and have the canvas actually repaint it*.

Two measurement traps this script avoids, both of which silently make
napari look about twice as fast as it is:

1. The viewer window must be **shown**. With ``show=False`` the canvas
   widget is hidden, Qt never issues paint events, and the GPU draw
   never happens - you measure only napari's CPU-side upload.
2. The Qt event loop must be **flushed inside the timed region**.
   Assigning ``layer.data`` only schedules a repaint; without a flush
   the paint lands outside your timer.

Numbers are only meaningful next to the GL renderer that produced them,
which the report prints: under software rasterization (llvmpipe, typical
in CI/containers) display cost is a pessimistic floor, not a verdict for
real instrument workstations with a GPU.

The **acquire** figure is not comparable with the one first recorded for
Phase 2. This script originally drove ``devices.nion_adapter``, an
in-process wrapper around usim that no longer exists: §6's license work
split it into ``nion_server`` (GPL-3.0, subprocess-only) plus ``remote``
(MIT, IPC client), and the import here went stale rather than wrong. It
is now fixed to ``remote_simulated_instrument()`` — deliberately the
client, not ``nion_server.simulated_instrument()``, because that is what
``viewer/app.py`` and every other benchmark script use, so the number
measured is the one the shipped application actually pays. The cost of
that honesty is that "acquire" now includes the IPC round trip; at
512x512 a float64 scan frame is 2.1MB, well over
``_SHARED_MEMORY_THRESHOLD_BYTES``, so it travels through shared memory
and §6's measurements put that overhead at roughly +3ms.

Needs a real GL canvas. On a machine with a display - which is the case
worth measuring, since only hardware-accelerated numbers settle
anything - run it directly:

    uv run --extra device --extra viewer \
        python scripts/phase2_live_benchmark.py

On a headless Linux box or in CI, wrap it in a virtual display. That
gives llvmpipe software rasterization, which the report detects and
labels as a floor rather than a verdict:

    xvfb-run -a -s "-screen 0 1920x1080x24" \
        uv run --extra device --extra viewer \
        python scripts/phase2_live_benchmark.py

macOS needs neither wrapper and reports its own renderer (Apple's Metal
-backed GL). Its unified memory removes the host-to-device texture copy
a discrete card pays per frame, so it is a genuinely interesting data
point rather than merely an available one - but the number is what
decides that, not the architecture.
"""

from __future__ import annotations

import argparse
import contextlib
import statistics
import threading
import time
import typing

import napari
import numpy as np
from qtpy import QtWidgets

from miainwoodpecker.devices.interface import ScanParameters
from miainwoodpecker.devices.remote import remote_simulated_instrument
from miainwoodpecker.viewer.live import LiveInstrumentWidget

if typing.TYPE_CHECKING:
    from miainwoodpecker.acquisition import LiveAcquisition
    from miainwoodpecker.devices.interface import Scanner

_SOFTWARE_RENDERERS = ("llvmpipe", "softpipe", "swrast", "software")
# Below this share of a frame's acquisition time, the display cost cannot
# be what limits a live view - a tenth leaves an order of magnitude of
# headroom, which is past arguing about.
_NEGLIGIBLE_FRACTION = 0.1


def _gl_renderer(viewer: napari.Viewer) -> str:
    """Return the OpenGL renderer string, or a placeholder if unavailable."""
    try:
        from vispy.gloo import gl  # noqa: PLC0415

        viewer.window._qt_viewer.canvas.native.makeCurrent()  # noqa: SLF001
        return str(gl.glGetParameter(gl.GL_RENDERER))
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return f"unknown ({type(exc).__name__}: {exc})"


def _percentile(values: list[float], fraction: float) -> float:
    """Return a simple nearest-rank percentile of the given samples."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _report(label: str, samples_ms: list[float]) -> None:
    """Print median/p95/max and the implied rate for a set of timings."""
    if not samples_ms:
        print(f"{label}: no samples")
        return
    median_ms = statistics.median(samples_ms)
    implied_fps = 1000.0 / median_ms if median_ms > 0 else float("inf")
    print(
        f"{label}: n={len(samples_ms)} "
        f"median={median_ms:.1f}ms "
        f"p95={_percentile(samples_ms, 0.95):.1f}ms "
        f"max={max(samples_ms):.1f}ms "
        f"({implied_fps:.1f} fps equivalent)"
    )


def benchmark_acquire(
    scanner: Scanner,
    parameters: ScanParameters,
    frames: int,
) -> list[float]:
    """Time bare ``scan_frame`` calls with no display attached."""
    samples_ms: list[float] = []
    scanner.scan_frame(parameters, 0)  # warm up
    for _ in range(frames):
        started = time.perf_counter()
        scanner.scan_frame(parameters, 0)
        samples_ms.append((time.perf_counter() - started) * 1000.0)
    return samples_ms


def benchmark_display(
    widget: LiveInstrumentWidget,
    loop: LiveAcquisition,
    app: QtWidgets.QApplication,
    frames: int,
) -> list[float]:
    """
    Time a full display update per genuinely new frame.

    Each sample covers ``refresh_display()`` plus an event-loop flush, so
    the GPU repaint is inside the measurement rather than after it.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The dock widget whose ``refresh_display`` is being timed.
    loop : LiveAcquisition
        The live loop supplying frames, polled for genuinely new ones.
    app : QtWidgets.QApplication
        The running application, flushed inside each timed region.
    frames : int
        How many samples to collect.

    Returns
    -------
    list[float]
        One elapsed time per frame, in milliseconds.
    """
    samples_ms: list[float] = []
    seen: object = None
    deadline = time.monotonic() + 120.0
    # Warm up: first frame also creates the layer, which is far slower.
    widget.refresh_display()
    app.processEvents()
    while len(samples_ms) < frames and time.monotonic() < deadline:
        frame = loop.latest()
        if frame is None or frame is seen:
            time.sleep(0.001)
            continue
        seen = frame
        started = time.perf_counter()
        widget.refresh_display()
        app.processEvents()
        samples_ms.append((time.perf_counter() - started) * 1000.0)
    return samples_ms


@contextlib.contextmanager
def _background_load(threads: int) -> typing.Iterator[None]:
    """
    Saturate ``threads`` CPUs for the duration, or do nothing for zero.

    Stands in for analysis running on the same machine, which is not a
    hypothetical: napari's per-update cost is CPU-side, and the spread on
    this measurement widened from a few percent to 42% under the CPU
    contention of software rendering. Whether a HyperSpy or LiberTEM job
    degrades a live view is therefore a real question, and a measurable
    one.

    The load is numpy work rather than a Python spin loop, deliberately -
    numpy releases the GIL, so this competes for *cores* the way real
    analysis does rather than for the interpreter, which would be a
    harsher and less representative test.

    Parameters
    ----------
    threads : int
        How many workers to run. Zero yields immediately.

    Yields
    ------
    None
        For the duration of the load.
    """
    if threads <= 0:
        yield
        return
    stop = threading.Event()

    def burn() -> None:
        block = np.random.default_rng().standard_normal((512, 512))
        while not stop.is_set():
            block @ block

    workers = [
        threading.Thread(target=burn, name=f"load-{index}", daemon=True)
        for index in range(threads)
    ]
    for worker in workers:
        worker.start()
    try:
        yield
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=5.0)


def main() -> None:
    """Run the acquire and display benchmarks and print a verdict."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=512, help="scan size in pixels")
    parser.add_argument("--frames", type=int, default=50, help="frames per benchmark")
    parser.add_argument("--dwell-us", type=float, default=1.0, help="pixel dwell time")
    parser.add_argument(
        "--source",
        choices=("scan", "camera"),
        default="scan",
        help=(
            "which live view to time. 'scan' also pays a per-frame "
            "autocontrast pass (two full array walks); 'camera' does not, "
            "so it is the cheaper path and the one a Ronchigram or "
            "spectrometer live view uses"
        ),
    )
    parser.add_argument(
        "--load",
        type=int,
        default=0,
        help=(
            "CPU-saturating threads to run during the display measurement, "
            "standing in for analysis on the same machine. napari's "
            "per-update cost is CPU-side, so this is the question 'how "
            "responsive is the viewer while something else is running'"
        ),
    )
    args = parser.parse_args()

    print(f"scan {args.size}x{args.size} px, dwell {args.dwell_us} us")
    print(f"display source: {args.source}")
    if args.load:
        print(f"background load: {args.load} CPU-saturating thread(s)")
    print()

    with remote_simulated_instrument() as microscope:
        parameters = ScanParameters(
            height=args.size,
            width=args.size,
            pixel_time_us=args.dwell_us,
            fov_nm=microscope.stage_size_nm * 0.1,
        )

        acquire_ms = benchmark_acquire(microscope.scanner, parameters, args.frames)

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        # show=True is required: a hidden canvas never receives paint events.
        viewer = napari.Viewer(show=True)
        widget = LiveInstrumentWidget(
            viewer,
            microscope.scanner,
            camera=microscope.ronchigram_camera,
        )
        try:
            widget._size_combo.setCurrentText(str(args.size))  # noqa: SLF001
            widget._dwell_spin.setValue(args.dwell_us)  # noqa: SLF001
            if args.source == "camera":
                widget.start_camera()
                loop = widget._camera_loop  # noqa: SLF001
            else:
                widget.start_scan()
                loop = widget._scan_loop  # noqa: SLF001
            app.processEvents()
            renderer = _gl_renderer(viewer)
            with _background_load(args.load):
                display_ms = benchmark_display(widget, loop, app, args.frames)
        finally:
            widget.shutdown()
            viewer.close()

    print(f"GL renderer: {renderer}")
    is_software = any(name in renderer.lower() for name in _SOFTWARE_RENDERERS)
    print(
        "rendering: software rasterization - treat display cost as a "
        "pessimistic floor\n"
        if is_software
        else "rendering: hardware accelerated\n"
    )
    _report("acquire (no display)", acquire_ms)
    _report("display (refresh + repaint)", display_ms)

    if not acquire_ms or not display_ms:
        return
    acquire_median = statistics.median(acquire_ms)
    display_median = statistics.median(display_ms)
    scan_ms = args.size * args.size * args.dwell_us / 1000.0
    _verdict(display_median, acquire_median, scan_ms, is_software=is_software)


def _verdict(
    display_median: float,
    acquire_median: float,
    scan_ms: float,
    *,
    is_software: bool,
) -> None:
    """
    Say what the display cost means, against the comparison that matters.

    **Not against ``acquire``, which is what this printed at first and
    which is the wrong denominator.** ``acquire`` is the simulator going
    as fast as it can with no beam involved; on hardware the same frame
    takes ``size^2 x dwell``, which for the 512x512 default at 1us is
    262ms rather than the simulator's 5ms. Dividing by the simulator made
    napari look like the bottleneck in a pipeline where it accounts for a
    few percent, and the first hardware-accelerated run duly printed
    "display dominates ... the empirical argument for ndv" off a 2.05x
    ratio that meant nothing of the sort.

    What a live viewer has to beat is the rate frames actually *arrive*
    at. So both are reported: the scan's own physical duration for a
    scanned source, and the sustainable frame rate for a camera, which is
    the case where this cost is a real ceiling.

    Parameters
    ----------
    display_median : float
        Median refresh-and-repaint time, in milliseconds.
    acquire_median : float
        Median device-layer frame time with no display, in milliseconds.
    scan_ms : float
        How long the requested scan physically takes on an instrument.
    is_software : bool
        Whether the renderer was a software rasterizer.
    """
    print(
        f"\ndisplay is {display_median / acquire_median:.2f}x this source's "
        f"acquire cost, which is the *simulator* going flat out - not the "
        f"comparison that decides anything",
    )
    print(
        f"against the scan it was asked for ({scan_ms:.0f}ms of real beam "
        f"time): display is {100 * display_median / scan_ms:.1f}% of a frame",
    )
    print(
        f"as a ceiling for a camera-rate source: "
        f"{1000 / display_median:.0f} fps sustained",
    )
    if is_software:
        print(
            "verdict: software rasterization - this is a floor, not a "
            "measurement. Re-run where there is a GPU.",
        )
        return
    if display_median < scan_ms * _NEGLIGIBLE_FRACTION:
        print(
            "verdict: negligible for scanned imaging. Whether it is also "
            "fine for a fast camera depends on that camera's rate against "
            "the sustained figure above.",
        )
        return
    print(
        "verdict: display is a real share of the frame time for this "
        "source. Before concluding anything about napari, re-run with "
        "--size varied: a cost that barely moves is napari's fixed "
        "per-update overhead (the thing ndv exists to remove), while one "
        "that scales with pixels is upload and draw, which changing "
        "viewer will not fix.",
    )


if __name__ == "__main__":
    main()
