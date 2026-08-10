"""
Confirm a bare napari + PySide6 window can be constructed.

Phase 0 groundwork check (see docs/migration-plan.md) for the live-viewer
shell. Runs with QT_QPA_PLATFORM=offscreen so it also works in a headless
container/CI without a real display.

Run with: uv run --extra viewer python scripts/phase0_viewer_smoke_test.py
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import napari
import numpy as np


def main() -> None:
    """Construct a napari viewer, add a test image layer, and close it."""
    print("constructing napari viewer (offscreen Qt platform)...", flush=True)
    viewer = napari.Viewer(show=False)
    try:
        viewer.add_image(
            np.random.default_rng(0).random((256, 256)),
            name="smoke-test-frame",
        )
        print("layers:", [layer.name for layer in viewer.layers], flush=True)
    finally:
        viewer.close()
    print("OK: napari + PySide6 viewer constructed and closed headlessly", flush=True)


if __name__ == "__main__":
    main()
