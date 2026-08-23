"""
Integration tests: the device panels are a toolbar, not a stack of buttons.

The dock's device groups were a full-width labelled button per action and
a form row per setting, which on a two-camera instrument ran past a
screen. The actions are now one row of icons and the rarely-changed
settings are behind a dialog.

What these pin is the property that makes that safe rather than merely
smaller: **the settings widgets still exist, and are still reachable by
the names everything already reads them by.** They moved from the dock
into a dialog built alongside it, so nothing that acquires a frame has
to know where they live, and no test has to open a dialog to set one.

Skipped without a display (see conftest.py).
"""

import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import napari
from qtpy import QtWidgets

from miainwoodpecker.viewer.live import LiveInstrumentWidget
from miainwoodpecker.viewer.panels import toolbar
from miainwoodpecker.viewer.preview import build_preview_devices

#: Every action that used to be a full-width button in the Scan group.
_SCAN_ACTIONS = 7
#: And in a Camera group.
_CAMERA_ACTIONS = 5
#: A long exposure, distinct from any default, for the read-back test.
_AN_EXPOSURE_MS = 250.0
#: Loose ceiling on the dock's natural height, in pixels. The figure is
#: not the requirement; it fails if a settings row creeps back in.
_A_SHORTER_PANEL = 900


def _open(**kwargs: object) -> tuple:
    """
    Open a widget against the preview instrument.

    Parameters
    ----------
    **kwargs : object
        Passed to :func:`build_preview_devices`.

    Returns
    -------
    tuple
        The viewer and the widget.
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


def _buttons(row: QtWidgets.QWidget) -> list[QtWidgets.QToolButton]:
    """
    Return the icon buttons in a toolbar row.

    Parameters
    ----------
    row : QtWidgets.QWidget
        A widget containing the toolbar.

    Returns
    -------
    list[QtWidgets.QToolButton]
        Every tool button under it.
    """
    return row.findChildren(QtWidgets.QToolButton)


def test_the_scan_actions_are_one_row_of_icons():
    """Seven actions, seven icon buttons, one row."""
    viewer, widget = _open()
    try:
        group = widget._scan_button.parentWidget()  # noqa: SLF001
        row = widget._scan_button.parentWidget()  # noqa: SLF001
        assert isinstance(widget._scan_button, QtWidgets.QToolButton)  # noqa: SLF001
        assert len(_buttons(row)) == _SCAN_ACTIONS
        assert group is not None
    finally:
        widget.shutdown()
        viewer.close()


def test_every_icon_button_says_what_it_does():
    """
    A glyph with no tooltip is a guessing game.

    This is the whole cost of trading labelled buttons for icons, so it
    is the thing worth asserting rather than assuming: every button
    carries both a tooltip for the pointer and an accessible name for a
    screen reader.
    """
    viewer, widget = _open(camera_count=2)
    try:
        row = widget._scan_button.parentWidget()  # noqa: SLF001
        for button in _buttons(row):
            assert button.toolTip().strip(), button.text()
            assert button.accessibleName().strip(), button.text()
    finally:
        widget.shutdown()
        viewer.close()


def test_the_camera_actions_are_one_row_of_icons():
    """A camera gets start, acquire, save, record and settings."""
    viewer, widget = _open(scan=False, camera=True)
    try:
        binding = widget._binding(None)  # noqa: SLF001
        row = binding.button.parentWidget()
        assert len(_buttons(row)) == _CAMERA_ACTIONS
    finally:
        widget.shutdown()
        viewer.close()


def test_the_settings_still_exist_where_everything_reads_them():
    """
    Moving a control into a dialog must not move it out of reach.

    ``_image_parameters`` reads the exposure and binning at the moment of
    acquisition; it neither knows nor should know that they are now in a
    dialog. The dialog is built with the panel and kept, so they are
    readable whether or not it has ever been opened.
    """
    viewer, widget = _open(scan=False, camera=True, camera_count=2)
    try:
        binding = widget._binding("eels_camera")  # noqa: SLF001
        assert binding.settings_dialog is not None
        assert not binding.settings_dialog.isVisible()

        binding.exposure_spin.setValue(_AN_EXPOSURE_MS)
        binding.binning_combo.setCurrentText("10")
        binding.binning_across_combo.setCurrentText("1")

        taken = widget._image_parameters(binding)  # noqa: SLF001
        assert taken.exposure_ms == _AN_EXPOSURE_MS
        assert taken.binning_yx == (10, 1)
    finally:
        widget.shutdown()
        viewer.close()


def test_the_settings_button_opens_the_dialog():
    """The gear is the way in, and closing it leaves the panel intact."""
    viewer, widget = _open(scan=False, camera=True)
    try:
        binding = widget._binding(None)  # noqa: SLF001
        assert not binding.settings_dialog.isVisible()

        binding.settings_button.click()
        QtWidgets.QApplication.instance().processEvents()
        assert binding.settings_dialog.isVisible()

        binding.settings_dialog.close()
        QtWidgets.QApplication.instance().processEvents()
        assert not binding.settings_dialog.isVisible()
        assert binding.exposure_spin is not None
    finally:
        widget.shutdown()
        viewer.close()


def test_what_changes_often_stays_in_the_panel():
    """
    Detectors and field of view are not settings-dialog material.

    The rule the condensation follows is frequency, not tidiness: which
    detectors to read and where to look are changed while watching the
    image, so burying them behind a modal would cost more than the rows
    they occupy.
    """
    viewer, widget = _open()
    try:
        dialog = widget._scan_settings_dialog  # noqa: SLF001
        for check in widget._channel_checks.values():  # noqa: SLF001
            assert not dialog.isAncestorOf(check)
        assert not dialog.isAncestorOf(widget._fov_spin)  # noqa: SLF001
        # And what does not: dwell, resolution, the spectrum-image grid.
        assert dialog.isAncestorOf(widget._positions_spin)  # noqa: SLF001
        assert dialog.isAncestorOf(widget._scan_count_spin)  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_the_panel_is_shorter_than_it_was():
    """
    The point of the exercise, as a number.

    A scan unit and two cameras with every section open used to want far
    more than a screen. The assertion is deliberately loose — the exact
    figure is not the requirement — but it fails if a settings row
    creeps back into the dock.
    """
    viewer, widget = _open(camera_count=2)
    try:
        for section in widget._device_sections.values():  # noqa: SLF001
            section.set_expanded(True)
        QtWidgets.QApplication.instance().processEvents()
        assert widget.sizeHint().height() < _A_SHORTER_PANEL
    finally:
        widget.shutdown()
        viewer.close()


def test_a_running_source_still_shows_a_glyph_not_a_word():
    """
    Starting a source swaps the button's glyph, it does not label it.

    The start controls flip between starting and stopping, and did it by
    setting the button's *text*: on a labelled button that read "Stop
    scan", and on a 30-pixel icon button it read "..." — the glyph
    replaced by an elided word. A screenshot caught it; nothing in the
    suite did, so this is here now.
    """
    viewer, widget = _open(scan=False, camera=True)
    try:
        binding = widget._binding(None)  # noqa: SLF001
        assert binding.button.text() == toolbar.START

        widget.start_camera()
        assert binding.button.text() == toolbar.STOP
        assert "stop" in binding.button.toolTip().lower()

        widget.stop_camera()
        assert binding.button.text() == toolbar.START
        assert "start" in binding.button.toolTip().lower()
    finally:
        widget.shutdown()
        viewer.close()


def test_the_settings_glyph_is_not_the_gear():
    """
    U+2699 renders as a colour emoji on Windows, so it is not used.

    A lilac flower among seven black shapes reads as a rendering fault
    rather than a button, and U+FE0E does not reliably force the text
    form through Qt's font fallback.

    **This does not prove the glyphs render monochrome**, and no test
    here does. That is a property of the platform's font fallback, so
    asserting it would mean thresholding rendered pixels — and subpixel
    antialiasing tints the edges of *every* glyph by about 150 of 255,
    swamping the difference this would be looking for. Any threshold
    that separated them here would be tuned to this machine's font
    rendering and fail on the next one. So this pins the one fact that
    is stable and records the trap for whoever adds a glyph next: check
    it on screen.
    """
    assert toolbar.SETTINGS != "⚙"
    assert "️" not in toolbar.SETTINGS
