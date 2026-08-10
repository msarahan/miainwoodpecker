"""
Entry point: the live viewer against the simulated microscope.

Run with ``miainwoodpecker-viewer`` (or
``uv run --extra device --extra viewer miainwoodpecker-viewer``).
Requires both the ``device`` and ``viewer`` optional dependency groups;
real-hardware sources will join once Phase 1's hardware validation lands.
"""

from __future__ import annotations

import napari

from miainwoodpecker.devices.nion_adapter import simulated_instrument
from miainwoodpecker.viewer.live import LiveInstrumentWidget


def main() -> None:
    """Open a napari window with the live instrument dock widget."""
    with simulated_instrument() as microscope:
        viewer = napari.Viewer(title="miainwoodpecker")
        widget = LiveInstrumentWidget(
            viewer,
            microscope.scanner,
            camera=microscope.ronchigram_camera,
        )
        viewer.window.add_dock_widget(widget, area="right", name="Instrument")
        napari.run()
        widget.shutdown()


if __name__ == "__main__":
    main()
