"""
Integration tests: the live viewer widget against in-memory fake devices.

Skipped unless the ``viewer`` extra is installed and a display is
available (see conftest.py — napari needs a real GL canvas, so run under
``xvfb-run -a``). Drives the display refresh directly rather than waiting
on timers, so the tests never depend on Qt event-loop timing.
"""

import datetime
import time
import typing

import numpy as np
import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import napari

from miainwoodpecker.devices import Frame, ScanParameters
from miainwoodpecker.viewer.live import LiveInstrumentWidget

_DEADLINE_S = 10.0


class _FakeScanner:
    """Fake scanner returning zero frames of the requested shape."""

    @property
    def scanner_id(self) -> str:
        """Return the fake scanner's id."""
        return "fake_scanner"

    @property
    def channel_names(self) -> typing.Sequence[str]:
        """Return the fake channel names."""
        return ["HAADF", "MAADF"]

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Return a zero-filled frame of the requested shape."""
        return Frame(
            data=np.zeros(parameters.shape, dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={"channel_index": channel},
        )

    def close(self) -> None:
        """Release nothing; the fake owns no resources."""


class _FakeCamera:
    """Fake camera returning constant 8x8 frames."""

    def __init__(self) -> None:
        self.started = False

    @property
    def camera_id(self) -> str:
        """Return the fake camera's id."""
        return "fake_camera"

    def start(self) -> None:
        """Mark acquisition as running."""
        self.started = True

    def stop(self) -> None:
        """Mark acquisition as paused."""
        self.started = False

    def acquire_frame(self) -> Frame:
        """Return a constant 8x8 frame."""
        return Frame(
            data=np.ones((8, 8), dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={"frame_number": 1},
        )

    def close(self) -> None:
        """Release nothing; the fake owns no resources."""


def _wait_until(condition, deadline_s: float = _DEADLINE_S) -> bool:
    """Poll a condition until it is true or the deadline elapses."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


def test_live_widget_updates_layers_from_both_sources():
    """Scan and camera loops feed napari layers through refresh_display."""
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        widget.start_scan()
        widget.start_camera()
        assert camera.started
        assert _wait_until(
            lambda: widget._scan_loop.latest() is not None  # noqa: SLF001
        )
        assert _wait_until(
            lambda: widget._camera_loop.latest() is not None  # noqa: SLF001
        )
        widget.refresh_display()

        assert "Scan (HAADF)" in viewer.layers
        default_scan_shape = (256, 256)
        assert viewer.layers["Scan (HAADF)"].data.shape == default_scan_shape
        assert "Camera" in viewer.layers
        camera_shape = (8, 8)
        assert viewer.layers["Camera"].data.shape == camera_shape
    finally:
        widget.shutdown()
        viewer.close()
    assert not camera.started


def test_analyze_camera_in_hyperspy_adds_a_projection_layer():
    """
    Clicking "Analyze in HyperSpy" round-trips a burst through the adapter.

    Exercises the actual wired-in Phase 4 entry point end to end: record a
    burst from the fake camera to a temporary NeXus file, load it back as
    a HyperSpy signal, average across frames, and land the result in
    napari as a new layer. Skipped if the ``analysis`` extra is not
    installed, since the widget itself does not require it.
    """
    pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        widget._analyze_camera_in_hyperspy()  # noqa: SLF001 - simulating a button click

        layer_name = "HyperSpy mean projection (Camera)"
        assert layer_name in viewer.layers
        projection_shape = (8, 8)
        assert viewer.layers[layer_name].data.shape == projection_shape
        assert not camera.started  # the burst starts and stops the camera itself
        assert widget._analyze_status.text().startswith("done")  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_scan_settings_change_takes_effect_on_next_frames():
    """Changing the size control changes the shape of subsequently scanned frames."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget._size_combo.setCurrentIndex(0)  # noqa: SLF001 - simulating user input
        widget.start_scan()
        assert _wait_until(
            lambda: widget._scan_loop.latest() is not None  # noqa: SLF001
        )
        widget.refresh_display()
        small_shape = (128, 128)
        assert viewer.layers["Scan (HAADF)"].data.shape == small_shape
    finally:
        widget.shutdown()
        viewer.close()
