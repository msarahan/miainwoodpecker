"""
Integration tests: a spectrum image over a region drawn on the survey scan.

The workflow, from the window: take a survey scan, mark a region on it,
choose how many positions it gets, and acquire — watching the scan
detector and the spectrum build while the probe goes. Everything below
runs against the preview instrument, the one backend that can
synchronise.

Skipped without a display (see conftest.py).
"""

from __future__ import annotations

import json
import time
import typing

import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import h5py
import napari
import numpy as np

from miainwoodpecker.devices.interface import PROJECTED_READOUT, ScanParameters
from miainwoodpecker.storage.passes import read_pass
from miainwoodpecker.storage.session import Session
from miainwoodpecker.viewer import documents
from miainwoodpecker.viewer.live import _REGION_LAYER, LiveInstrumentWidget
from miainwoodpecker.viewer.preview import _EELS_TARGET, build_preview_devices

if typing.TYPE_CHECKING:
    import pathlib

_POSITIONS = 8
_TWO_CAMERAS = 2
_DEADLINE_S = 30.0
_HALF = 0.5
_TALL_REGION_ROWS = 40.0
_TALL_REGION_COLUMNS = 20.0


def _finish_pass(widget: LiveInstrumentWidget) -> None:
    """
    Drive the display poll until the synchronised pass has finished.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget whose pass to wait for.

    Raises
    ------
    AssertionError
        If the pass does not finish before the deadline.
    """
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        widget.refresh_display()
        if widget._pass_job is None:  # noqa: SLF001
            return
        time.sleep(0.005)
    msg = "the spectrum image did not finish"
    raise AssertionError(msg)


class _RecordingScanner:
    """The preview's scanner, remembering what geometry it was asked for."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.requested: ScanParameters | None = None

    def __getattr__(self, name: str) -> object:
        """Forward everything else to the wrapped scanner."""
        return getattr(self._inner, name)

    def scan_synchronised(self, parameters: ScanParameters, **kwargs: object) -> object:
        """Record the grid, then acquire it."""
        self.requested = parameters
        return self._inner.scan_synchronised(parameters, **kwargs)


def _open(tmp_path: pathlib.Path, *, board: bool = False) -> tuple:
    """
    Open a widget over a preview with a spectrometer, in projected readout.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Where the session is written.
    board : bool
        Whether to display on a document board rather than a bare viewer.

    Returns
    -------
    tuple
        The viewer or window, the widget, the devices and the scanner.
    """
    devices = build_preview_devices(scan=True, camera=True, camera_count=_TWO_CAMERAS)
    scanner = _RecordingScanner(devices.scanner)
    if board:
        display = documents.open_window("region test")
        viewer = display.board
    else:
        display = napari.Viewer(show=False)
        viewer = display
    widget = LiveInstrumentWidget(
        viewer,
        scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    widget.set_session(Session(tmp_path / "shift"))
    widget._positions_spin.setValue(_POSITIONS)  # noqa: SLF001
    widget._sync_target_combo.setCurrentText(_EELS_TARGET)  # noqa: SLF001
    widget.set_camera_readout(_EELS_TARGET, PROJECTED_READOUT)
    return display, widget, devices, scanner


def _survey_layer(widget: LiveInstrumentWidget) -> str:
    """
    Take a survey scan and return the layer it was drawn in.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget to drive.

    Returns
    -------
    str
        The scan layer's name.
    """
    widget.preview_scan()
    return widget._scan_layer_name(widget.enabled_channel_names()[0])  # noqa: SLF001


def test_marking_a_region_needs_a_survey_scan(tmp_path):
    """
    With nothing on screen there is nothing to draw on, and it says so.

    A rectangle on an empty panel would be a region of nothing; the
    status line names the step the operator skipped instead.
    """
    viewer, widget, _, _ = _open(tmp_path)
    try:
        widget.mark_spectrum_image_region()

        assert "survey scan first" in widget._scan_status.text()  # noqa: SLF001
        assert _REGION_LAYER not in viewer.layers
    finally:
        widget.shutdown()
        viewer.close()


def test_the_region_is_drawn_on_the_survey_scan(tmp_path):
    """
    The rectangle lands in the survey scan's own panel, in its units.

    Attached to that layer rather than opened in a window of its own,
    because a rectangle marking a region of an image has to be on the
    image to mark anything.
    """
    window, widget, _, _ = _open(tmp_path, board=True)
    try:
        layer_name = _survey_layer(widget)
        widget.mark_spectrum_image_region()

        assert _REGION_LAYER in window.board.layers
        home = window.area.document(layer_name)
        assert home is not None
        assert _REGION_LAYER in [layer.name for layer in home.viewer.layers]
        assert "drag its corners" in widget._scan_status.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        window.close()


def test_the_first_region_is_the_middle_of_the_field_of_view(tmp_path):
    """The Grid row says half the field of view, square, at the centre."""
    viewer, widget, _, _ = _open(tmp_path)
    try:
        _survey_layer(widget)
        widget.mark_spectrum_image_region()

        geometry = widget._pass_parameters()  # noqa: SLF001
        fov = widget._fov_spin.value()  # noqa: SLF001
        assert geometry.shape == (_POSITIONS, _POSITIONS)
        assert geometry.fov_nm == pytest.approx(fov * _HALF)
        assert geometry.center_nm == pytest.approx((0.0, 0.0))
        assert "marked region" in widget._region_label.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_a_dragged_region_sets_the_grid_and_where_the_probe_goes(tmp_path):
    """
    The rectangle's aspect ratio and position become the pass's geometry.

    Positions count along the longer side; the shorter side follows from
    the rectangle so the pixels stay square, and the centre is where the
    rectangle is - which is the whole point of drawing it.
    """
    viewer, widget, _, scanner = _open(tmp_path)
    try:
        layer_name = _survey_layer(widget)
        widget.mark_spectrum_image_region()
        survey = viewer.layers[layer_name]
        pixel_nm = float(survey.scale[-1])
        # A tall rectangle in the top-left quarter, in the panel's world
        # units - which is what an operator dragging the handles produces.
        top, left = 0.0, 0.0
        bottom, right = _TALL_REGION_ROWS * pixel_nm, _TALL_REGION_COLUMNS * pixel_nm
        viewer.layers[_REGION_LAYER].data = [
            np.array([[top, left], [top, right], [bottom, right], [bottom, left]]),
        ]

        geometry = widget._pass_parameters()  # noqa: SLF001
        assert geometry.shape == (_POSITIONS, _POSITIONS // 2)
        assert geometry.fov_nm == pytest.approx(_TALL_REGION_ROWS * pixel_nm)
        rows, columns = survey.data.shape
        expected_centre = (
            (_TALL_REGION_ROWS / 2 - rows / 2) * pixel_nm,
            (_TALL_REGION_COLUMNS / 2 - columns / 2) * pixel_nm,
        )
        assert geometry.center_nm == pytest.approx(expected_centre)

        widget.acquire_spectrum_image()
        _finish_pass(widget)

        assert scanner.requested == geometry
        assert "spectrum image saved" in widget._recording_status.text()  # noqa: SLF001
        path = next((tmp_path / "shift").glob("*spectrum-image*"))
        assert read_pass(path).signals[f"data_{_EELS_TARGET}"][:2] == geometry.shape
        with h5py.File(path, "r") as handle:
            recorded = json.loads(handle["entry/metadata/pass_json"][()])
        assert recorded["center_nm"] == pytest.approx(list(expected_centre))
    finally:
        widget.shutdown()
        viewer.close()


def test_pressing_again_removes_the_region(tmp_path):
    """The next pass then covers the whole field of view, and the row says so."""
    viewer, widget, _, _ = _open(tmp_path)
    try:
        _survey_layer(widget)
        widget.mark_spectrum_image_region()
        widget.mark_spectrum_image_region()

        assert _REGION_LAYER not in viewer.layers
        assert "whole field of view" in widget._region_label.text()  # noqa: SLF001
        geometry = widget._pass_parameters()  # noqa: SLF001
        assert geometry.fov_nm == widget._fov_spin.value()  # noqa: SLF001
        assert geometry.center_nm == (0.0, 0.0)
    finally:
        widget.shutdown()
        viewer.close()


def test_the_scan_detector_builds_beside_the_spectrum(tmp_path):
    """
    Both halves of the live view: the HAADF of the pass, and the spectrum.

    The scan channels are written through one position at a time, so
    the survey image of the pass is on screen as it fills in rather
    than when the file closes - the same tee the spectrum image's map
    comes through, over a bare array.
    """
    viewer, widget, _, _ = _open(tmp_path)
    try:
        _survey_layer(widget)
        widget.mark_spectrum_image_region()
        widget.acquire_spectrum_image()
        _finish_pass(widget)

        assert "Acquiring (HAADF)" in viewer.layers
        haadf = viewer.layers["Acquiring (HAADF)"]
        geometry = widget._pass_parameters()  # noqa: SLF001
        assert haadf.data.shape == geometry.shape
        assert haadf.data.any()
        # Drawn to the region's scale, so the panel says nanometres.
        assert float(haadf.scale[-1]) == pytest.approx(geometry.pixel_size_nm)
        assert f"Acquiring ({_EELS_TARGET})" in viewer.layers
        latest = widget._pass_preview[_EELS_TARGET].latest_spectrum  # noqa: SLF001
        assert latest is not None
    finally:
        widget.shutdown()
        viewer.close()
