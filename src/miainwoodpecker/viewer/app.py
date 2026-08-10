"""
Entry point: the live viewer against the simulated microscope.

Run with ``miainwoodpecker-viewer`` (or
``uv run --extra device --extra viewer miainwoodpecker-viewer``).
Requires both the ``device`` and ``viewer`` optional dependency groups;
real-hardware sources will join once Phase 1's hardware validation lands.

Deliberately imports ``miainwoodpecker.devices.remote``, not
``miainwoodpecker.devices.nion_server``: the running application talks to
the device server over IPC and never imports Nion's GPL-3.0 code directly
(see docs/migration-plan.md, §6).
"""

from __future__ import annotations

import napari

from miainwoodpecker.devices.remote import remote_simulated_instrument
from miainwoodpecker.viewer.live import LiveInstrumentWidget


def main() -> None:
    """Open a napari window with the live instrument dock widget."""
    with remote_simulated_instrument() as microscope:
        viewer = napari.Viewer(title="miainwoodpecker")
        widget = LiveInstrumentWidget(
            viewer,
            microscope.scanner,
            camera=microscope.ronchigram_camera,
        )
        viewer.window.add_dock_widget(widget, area="right", name="Instrument")
        # No explicit widget.shutdown() after this: closeEvent already calls
        # it once the window closes (part of Qt's app-quit teardown), and
        # calling it again here hits an already-destroyed Qt object.
        napari.run()


if __name__ == "__main__":
    main()
