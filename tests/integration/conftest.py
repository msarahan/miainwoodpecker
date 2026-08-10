"""
Shared fixtures/guards for integration tests.

Napari's vispy canvas needs a real (or virtual) display: under
``QT_QPA_PLATFORM=offscreen`` Qt provides no ``QOpenGLWidget``, napari's
layer lifecycle breaks on teardown, and the GPU rendering path this
project chose napari *for* is never exercised. So viewer tests require a
display and are skipped with instructions otherwise.

Run them with: ``xvfb-run -a uv run --extra device --extra viewer
--extra tests pytest``
"""

import os

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config,  # noqa: ARG001
    items: list[pytest.Item],
) -> None:
    """Skip viewer tests when no usable display is available."""
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return
    skip_viewer = pytest.mark.skip(
        reason=(
            "no display available; napari needs a real GL canvas "
            "(run under 'xvfb-run -a', not QT_QPA_PLATFORM=offscreen)"
        ),
    )
    for item in items:
        if "test_live_widget" in item.nodeid:
            item.add_marker(skip_viewer)
