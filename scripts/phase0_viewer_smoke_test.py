"""
Confirm a bare napari + PySide6 window can be constructed.

Phase 0 groundwork check (see docs/migration-plan.md) for the live-viewer
shell.

Requires a real (or virtual) display. ``QT_QPA_PLATFORM=offscreen`` is
deliberately *not* offered as a fallback: under offscreen Qt provides no
``QOpenGLWidget``, so napari's GPU canvas is never exercised and its
layer lifecycle raises a KeyError on teardown. An offscreen run would
therefore either crash or, worse, pass while proving nothing about the
rendering path this project chose napari for.

Run with:

    xvfb-run -a -s "-screen 0 1920x1080x24" \
        uv run --extra viewer python scripts/phase0_viewer_smoke_test.py
"""

from __future__ import annotations

import os
import sys

import napari
import numpy as np


def main() -> int:
    """
    Construct a napari viewer, add a test image layer, and close it.

    Returns
    -------
    int
        Process exit status: 0 on success, 1 if no display is available.
    """
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print(
            "FAIL: no display available. napari needs a real GL canvas; run "
            'this under \'xvfb-run -a -s "-screen 0 1920x1080x24"\'. '
            "QT_QPA_PLATFORM=offscreen is not a valid substitute - it gives "
            "no QOpenGLWidget and breaks napari's layer teardown.",
            file=sys.stderr,
        )
        return 1

    print("constructing napari viewer...", flush=True)
    viewer = napari.Viewer(show=False)
    try:
        viewer.add_image(
            np.random.default_rng(0).random((256, 256)),
            name="smoke-test-frame",
        )
        print("layers:", [layer.name for layer in viewer.layers], flush=True)
    finally:
        viewer.close()
    print("OK: napari + PySide6 viewer constructed and closed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
