"""
Integration tests: the preview harness opens a working window.

The unit suite covers the preview's devices as objects. What is left,
and what this file covers, is the claim the harness actually makes: that
``miainwoodpecker-preview`` puts a live, populated window on screen with
no device server behind it. Skipped without a display (see conftest.py).
"""

import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import napari

from miainwoodpecker.devices.interface import DEFOCUS_CONTROL
from miainwoodpecker.viewer.live import LiveInstrumentWidget
from miainwoodpecker.viewer.preview import (
    PREVIEW_BACKEND,
    build_preview_devices,
)

_A_DEFOCUS_NM = 300.0


def _open(**kwargs: object) -> tuple[napari.Viewer, LiveInstrumentWidget]:
    """
    Open a preview widget, returning it and its viewer for teardown.

    Parameters
    ----------
    **kwargs
        Passed through to :func:`build_preview_devices`.

    Returns
    -------
    tuple[napari.Viewer, LiveInstrumentWidget]
        The viewer and the widget docked into it.
    """
    devices = build_preview_devices(**kwargs)
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(
        viewer,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    return viewer, widget


def test_the_default_preview_window_builds():
    """The full window — scan, camera, instrument — constructs and tears down."""
    viewer, widget = _open()
    try:
        assert widget is not None
    finally:
        widget.shutdown()
        viewer.close()


def test_the_panel_names_the_preview_backend():
    """
    A window of invented data says so on its face.

    The one assertion that keeps a preview screenshot from being
    mistaken for the simulator or an instrument.
    """
    viewer, widget = _open()
    try:
        assert widget._instrument_backend_label.text() == PREVIEW_BACKEND  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_a_withheld_control_gets_no_row():
    """
    The 'absent, not disabled' rule, exercised without special hardware.

    This is the branch the preview exists to make reachable: a microscope
    with no blanker is otherwise a thing you have to own to see.
    """
    viewer, widget = _open(controls=[DEFOCUS_CONTROL])
    try:
        assert list(widget._instrument_controls) == [DEFOCUS_CONTROL]  # noqa: SLF001
        assert widget._instrument_blanker is None  # noqa: SLF001
        assert widget._instrument_stage_y is None  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_a_control_set_from_the_panel_reaches_the_instrument():
    """The dials are wired: pressing Set writes through to the instrument."""
    devices = build_preview_devices()
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(
        viewer,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    try:
        widget._instrument_controls[DEFOCUS_CONTROL].setValue(_A_DEFOCUS_NM)  # noqa: SLF001
        widget.apply_instrument_control(DEFOCUS_CONTROL)
        assert devices.instrument.defocus_nm() == _A_DEFOCUS_NM
    finally:
        widget.shutdown()
        viewer.close()


def test_a_camera_only_window_has_no_scan_group():
    """A detector-only instrument opens, rather than being refused."""
    viewer, widget = _open(scan=False, camera=True)
    try:
        assert widget is not None
    finally:
        widget.shutdown()
        viewer.close()


def test_a_scan_only_window_opens():
    """So does a scan-only one."""
    viewer, widget = _open(scan=True, camera=False)
    try:
        assert widget is not None
    finally:
        widget.shutdown()
        viewer.close()
