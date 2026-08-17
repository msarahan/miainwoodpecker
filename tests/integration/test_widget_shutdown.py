"""
Integration tests: the widget shuts down cleanly, whatever the order.

There are two callers of ``shutdown`` that cannot see each other —
``closeEvent``, which Qt fires during app-quit teardown, and an entry
point tidying up after ``napari.run()`` returns — so it has to survive
being called twice, and being called after Qt has destroyed the widget.

Skipped without a display (see conftest.py).
"""

import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import napari

from miainwoodpecker.viewer.live import LiveInstrumentWidget
from miainwoodpecker.viewer.preview import build_preview_devices

_TWO_CAMERAS = 2


def _open(**kwargs: object) -> tuple[napari.Viewer, LiveInstrumentWidget, object]:
    """
    Open a docked widget over preview devices.

    Parameters
    ----------
    **kwargs : object
        Passed through to :func:`build_preview_devices`.

    Returns
    -------
    tuple[napari.Viewer, LiveInstrumentWidget, object]
        The viewer, the widget, and the devices behind it.
    """
    devices = build_preview_devices(**kwargs)
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(
        viewer,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    viewer.window.add_dock_widget(widget, area="right", name="Instrument")
    return viewer, widget, devices


def test_shutdown_survives_qt_destroying_the_widget_first():
    """
    The reported crash: a clean exit ended in a traceback.

    ``closeEvent`` runs during Qt's app-quit teardown and an entry point
    calling ``shutdown`` after ``napari.run()`` returns reaches a widget
    whose C++ side is already gone. That used to raise "Internal C++
    object already deleted" — and, worse than the traceback, it aborted
    before the device and thread teardown had run.
    """
    viewer, widget, _ = _open()
    viewer.close()
    widget.shutdown()


def test_shutdown_is_idempotent():
    """Called twice, it does the work once and returns quietly the second time."""
    viewer, widget, _ = _open()
    try:
        widget.shutdown()
        widget.shutdown()
    finally:
        viewer.close()


def test_shutdown_stops_every_camera_not_just_the_first():
    """
    A two-camera instrument used to leave its second camera running.

    ``stop_camera()`` with no argument acts on the first binding, so the
    bare call in ``shutdown`` stopped one of them and the process exited
    still holding the other. Asked through ``acquire_frame``, which is
    the camera's own contract for "you have not started me".
    """
    viewer, widget, devices = _open(
        scan=False, camera=True, camera_count=_TWO_CAMERAS,
    )
    try:
        for name in devices.cameras:
            widget.start_camera(name)
        for camera in devices.cameras.values():
            assert camera.acquire_frame() is not None

        widget.shutdown()

        for camera in devices.cameras.values():
            with pytest.raises(RuntimeError, match="start"):
                camera.acquire_frame()
    finally:
        viewer.close()


def test_shutdown_after_teardown_still_stops_the_devices():
    """
    The half that matters survives a dead widget.

    Each teardown step stops its machinery before it touches a label, so
    a step that dies on the Qt half has already done the real work — and
    one dead widget must not skip the steps after it.
    """
    viewer, widget, devices = _open()
    camera = next(iter(devices.cameras.values()))
    widget.start_camera()
    assert camera.acquire_frame() is not None

    viewer.close()
    widget.shutdown()

    with pytest.raises(RuntimeError, match="start"):
        camera.acquire_frame()
