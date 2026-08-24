"""
Shared fixtures/guards for integration tests.

Napari's vispy canvas needs a real (or virtual) display: under
``QT_QPA_PLATFORM=offscreen`` Qt provides no ``QOpenGLWidget``, napari's
layer lifecycle breaks on teardown, and the GPU rendering path this
project chose napari *for* is never exercised. So viewer tests require a
display and are skipped with instructions otherwise.

Run them on Linux with: ``xvfb-run -a uv run --extra device --extra
viewer --extra tests pytest``. On macOS and Windows they just run — see
:func:`_has_display` for why that needs saying.
"""

import os
import sys

import pytest

# Qt platform plugins that provide their own window server, so a display
# exists without any X11/Wayland environment variable to advertise it.
# Naming them is the whole point of this constant: guarding on $DISPLAY
# alone silently skipped every viewer test on macOS, where the cocoa
# plugin never sets it. The tests were not failing there and were not
# passing there - they were not running, which is the one outcome a
# guard should never produce quietly.
_NATIVE_WINDOW_SERVER_PLATFORMS = ("darwin", "win32")

# Test modules that construct a napari canvas, and so need the display
# the guard below checks for. Listed rather than detected: a module that
# forgets to add itself fails loudly on a headless machine, which is the
# safe direction for a guard whose other failure mode is silence.
#
# ``test_tray`` is in the list for a different reason and skips the same
# way: it builds no canvas, but a QApplication cannot be constructed at
# all without a display, so the guard has to run before the test's own
# "is there a notification area?" check gets a chance to skip politely.
_VIEWER_TEST_MODULES = (
    "test_live_widget",
    "test_preview_window",
    "test_panel_layout",
    "test_widget_shutdown",
    "test_acquire_images",
    "test_spectrum_image",
    "test_scan_panel",
    "test_panel_density",
    "test_documents",
    "test_window_on_a_remote_broker",
    "test_spectrum_plot",
    "test_tray",
)


def _has_display() -> bool:
    """
    Report whether a usable display is available for napari's canvas.

    Returns
    -------
    bool
        True if Qt can open a real GL canvas here.
    """
    if sys.platform in _NATIVE_WINDOW_SERVER_PLATFORMS:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def pytest_collection_modifyitems(
    config: pytest.Config,  # noqa: ARG001
    items: list[pytest.Item],
) -> None:
    """Skip viewer tests when no usable display is available."""
    if _has_display():
        return
    skip_viewer = pytest.mark.skip(
        reason=(
            "no display available; napari needs a real GL canvas "
            "(run under 'xvfb-run -a', not QT_QPA_PLATFORM=offscreen)"
        ),
    )
    for item in items:
        if any(module in item.nodeid for module in _VIEWER_TEST_MODULES):
            item.add_marker(skip_viewer)
