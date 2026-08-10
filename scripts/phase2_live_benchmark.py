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

Needs a real GL canvas, so run under a virtual display:

    xvfb-run -a -s "-screen 0 1920x1080x24" \
        uv run --extra device --extra viewer \
        python scripts/phase2_live_benchmark.py
"""

from __future__ import annotations

import argparse
import statistics
import time
import typing

import napari
from qtpy import QtWidgets

from miainwoodpecker.devices.interface import ScanParameters
from miainwoodpecker.devices.nion_adapter import simulated_instrument
from miainwoodpecker.viewer.live import LiveInstrumentWidget

if typing.TYPE_CHECKING:
    from miainwoodpecker.acquisition import LiveAcquisition
    from miainwoodpecker.devices.interface import Scanner

_SOFTWARE_RENDERERS = ("llvmpipe", "softpipe", "swrast", "software")


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


def main() -> None:
    """Run the acquire and display benchmarks and print a verdict."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=512, help="scan size in pixels")
    parser.add_argument("--frames", type=int, default=50, help="frames per benchmark")
    parser.add_argument("--dwell-us", type=float, default=1.0, help="pixel dwell time")
    args = parser.parse_args()

    print(f"scan {args.size}x{args.size} px, dwell {args.dwell_us} us\n")

    with simulated_instrument() as microscope:
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
        widget = LiveInstrumentWidget(viewer, microscope.scanner)
        try:
            widget._size_combo.setCurrentText(str(args.size))  # noqa: SLF001
            widget._dwell_spin.setValue(args.dwell_us)  # noqa: SLF001
            widget.start_scan()
            app.processEvents()
            renderer = _gl_renderer(viewer)
            display_ms = benchmark_display(
                widget,
                widget._scan_loop,  # noqa: SLF001
                app,
                args.frames,
            )
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
    print(f"\ndisplay is {display_median / acquire_median:.2f}x the acquire cost")
    if display_median < acquire_median:
        print("verdict: napari keeps up with this source.")
    elif is_software:
        print(
            "verdict: display dominates under software rendering. Re-run on a "
            "GPU workstation before concluding anything about napari; only a "
            "hardware-accelerated result justifies moving to ndv."
        )
    else:
        print(
            "verdict: display dominates on real GPU hardware - this is the "
            "empirical argument for ndv or a custom VisPy canvas."
        )


if __name__ == "__main__":
    main()
