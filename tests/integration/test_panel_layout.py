"""
Integration tests: the dock panel fits on a screen, and folds.

The bug these pin down: the panel was a fixed vertical stack of four
group boxes with no scroll area, and its *minimum* height was its
natural height. On a 1409-pixel-high screen the stack wanted 1499, so
Qt could not shrink it, nothing scrolled, and the lower sections were
not merely off screen but unreachable — no gesture existed that would
bring them back.

Skipped without a display (see conftest.py).
"""

import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import napari
from qtpy import QtWidgets

from miainwoodpecker.devices.interface import DEFOCUS_CONTROL
from miainwoodpecker.storage.session import Session
from miainwoodpecker.viewer.live import LiveInstrumentWidget
from miainwoodpecker.viewer.preview import build_preview_devices

# Shorter than any laptop this would run on, and far below the 1499 the
# panel used to demand. The point of the assertion is not the exact
# figure but that the panel's minimum no longer grows with its content.
_A_SMALL_SCREEN_HEIGHT = 600
_A_DEFOCUS_NM = 175.0
# Session context is a dialog, not a section: it is set once a shift and
# spent four form rows and two text boxes in the dock paying for that.
_EXPECTED_SECTIONS = {"instrument", "recordings", "devices"}
# One between the path and the free space, one between the free space
# and napari's "Ready".
_EXPECTED_RULES = 2


def _open(**kwargs: object) -> tuple[napari.Viewer, LiveInstrumentWidget]:
    """
    Open a widget over preview devices, returning it and its viewer.

    Parameters
    ----------
    **kwargs : object
        Passed through to :func:`build_preview_devices`.

    Returns
    -------
    tuple[napari.Viewer, LiveInstrumentWidget]
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


def test_the_panel_can_shrink_below_a_small_screen():
    """
    The regression itself: the panel's minimum must not track its content.

    Asserted on ``minimumSizeHint`` rather than on what the window
    happens to show, because the minimum is what actually made the old
    panel unreachable: Qt will not shrink a widget past it, so no amount
    of resizing the window could reveal the bottom.
    """
    viewer, widget = _open(scan=True, camera=True, camera_count=2)
    try:
        assert widget.minimumSizeHint().height() < _A_SMALL_SCREEN_HEIGHT
    finally:
        widget.shutdown()
        viewer.close()


def test_the_panel_scrolls():
    """
    Folding alone is not enough, so the stack lives in a scroll area.

    An operator watching a scan and two cameras at once has every
    section open on purpose, and that is the ordinary case rather than
    misuse. Collapsing is for tidiness; scrolling is the guarantee that
    the content is reachable whatever is open.
    """
    viewer, widget = _open(scan=True, camera=True, camera_count=2)
    try:
        scroll = widget.findChild(QtWidgets.QScrollArea)
        assert scroll is not None
        assert scroll.widgetResizable()
    finally:
        widget.shutdown()
        viewer.close()


def test_every_top_level_group_is_collapsible():
    """All four groups fold, not just the per-device ones inside Devices."""
    viewer, widget = _open()
    try:
        assert set(widget.panel_sections) == _EXPECTED_SECTIONS
    finally:
        widget.shutdown()
        viewer.close()


def test_groups_start_expanded():
    """
    Nothing is hidden on opening, so the panel looks as it always did.

    Folding is an operator's choice; a panel that opened half shut would
    be a different window rather than a fixed one.
    """
    viewer, widget = _open()
    try:
        assert all(
            section.is_expanded() for section in widget.panel_sections.values()
        )
    finally:
        widget.shutdown()
        viewer.close()


def test_collapsing_a_group_hides_its_controls():
    """
    Folding the Instrument group puts its controls away.

    Asked with ``isVisibleTo(widget)`` rather than ``isVisible()``:
    these tests never show the top-level window, so ``isVisible()`` is
    False for every widget either way and an assertion on it would pass
    whether or not folding did anything. ``isHidden()`` is no better —
    it reports a widget's *own* hide flag, and folding hides the group
    box, not the label inside it. ``isVisibleTo`` asks the question that
    matters: would this be on screen if the window were open.
    """
    viewer, widget = _open()
    try:
        label = widget._instrument_backend_label  # noqa: SLF001
        assert label.isVisibleTo(widget)

        widget.panel_sections["instrument"].set_expanded(False)

        assert not widget.panel_sections["instrument"].is_expanded()
        assert not label.isVisibleTo(widget)
    finally:
        widget.shutdown()
        viewer.close()


def test_a_folded_group_can_be_reopened():
    """Folding is reversible, which a one-way hide would not be."""
    viewer, widget = _open()
    try:
        section = widget.panel_sections["recordings"]
        section.set_expanded(False)
        section.set_expanded(True)
        assert section.is_expanded()
    finally:
        widget.shutdown()
        viewer.close()


def test_folding_every_group_shrinks_the_stack():
    """
    Folding actually reclaims height rather than only hiding text.

    Summed over the sections rather than read off their container: the
    container is inside a scroll area that is never shown here, and Qt
    does not recompute an unshown widget's cached ``sizeHint`` even
    after an explicit ``invalidate``. The sections are what the fold
    resizes, and they answer honestly.
    """
    viewer, widget = _open(scan=True, camera=True, camera_count=2)
    try:
        sections = widget.panel_sections.values()
        expanded = sum(section.sizeHint().height() for section in sections)
        for section in sections:
            section.set_expanded(False)
        folded = sum(section.sizeHint().height() for section in sections)
        assert folded < expanded
    finally:
        widget.shutdown()
        viewer.close()


def test_the_status_labels_go_into_the_main_window_status_bar():
    """
    The labels live beside napari's "Ready", not in the dock.

    A second status line of ours a few hundred pixels above the real one
    is two places to look; and inside the dock it could be scrolled out
    of view, which is what happened to the old Session group.
    """
    viewer, widget = _open()
    try:
        bar = viewer.window._qt_window.statusBar()  # noqa: SLF001
        assert bar.isAncestorOf(widget._status_path_label)  # noqa: SLF001
        assert bar.isAncestorOf(widget._space_label)  # noqa: SLF001
        scroll = widget.findChild(QtWidgets.QScrollArea)
        assert not scroll.isAncestorOf(widget._status_path_label)  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_napari_own_status_widget_survives_the_insert():
    """Adding ours must not displace the "Ready" line it sits beside."""
    viewer, widget = _open()
    try:
        bar = viewer.window._qt_window.statusBar()  # noqa: SLF001
        assert any(
            type(child).__name__ == "StatusBarWidget" for child in bar.children()
        )
    finally:
        widget.shutdown()
        viewer.close()


def test_the_status_labels_sit_left_of_ready():
    """
    Inserted at the front, which is where the operator asked for them.

    ``insertWidget(0, ...)`` rather than ``addWidget``: napari's "Ready"
    is drawn by its own ``StatusBarWidget``, already in the left-hand
    area, so appending would land to the right of it.

    This is the one test here that shows a real window, because it is
    the one asking a question only a laid-out window can answer.
    ``QStatusBar`` keeps inserted widgets in a private list rather than
    in its public layout — ``layout().indexOf`` returns -1 for every one
    of them, napari's included — so on-screen position is the only
    honest way to ask which is further left.
    """
    viewer = napari.Viewer()
    devices = build_preview_devices()
    widget = LiveInstrumentWidget(
        viewer,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    try:
        bar = viewer.window._qt_window.statusBar()  # noqa: SLF001
        QtWidgets.QApplication.instance().processEvents()
        ready = next(
            child
            for child in bar.children()
            if type(child).__name__ == "StatusBarWidget"
        )
        ours = widget._status_path_label  # noqa: SLF001
        assert ready.width() > 0, "the window must be laid out to mean anything"
        assert ours.mapTo(bar, ours.rect().topLeft()).x() < ready.mapTo(
            bar, ready.rect().topLeft(),
        ).x()
    finally:
        widget.shutdown()
        viewer.close()


def test_the_status_bar_is_read_only():
    """
    The path is a label, not a button: settings have one home.

    A status bar that quietly doubled as a control would mean the
    session directory could be changed from two places, one of them
    invisible until someone happened to click the text.
    """
    viewer, widget = _open()
    try:
        assert not isinstance(
            widget._status_path_label,  # noqa: SLF001
            QtWidgets.QAbstractButton,
        )
    finally:
        widget.shutdown()
        viewer.close()


def test_the_status_bar_names_the_session_directory(tmp_path):
    """Setting a session updates the line an operator actually looks at."""
    viewer, widget = _open()
    try:
        widget.set_session(Session(tmp_path / "shift"))
        assert widget._status_path_label.full_text() == str(  # noqa: SLF001
            (tmp_path / "shift"),
        )
        prefix = widget._status_prefix_label  # noqa: SLF001
        assert prefix.isVisibleTo(prefix.parentWidget())
    finally:
        widget.shutdown()
        viewer.close()


def test_with_no_session_the_saving_to_prefix_is_hidden(tmp_path):
    """
    The message stands alone rather than behind a label it contradicts.

    "Saving to no session - data is not being kept" is a sentence you
    have to read twice to learn that nothing is being saved.
    """
    viewer, widget = _open()
    try:
        widget.set_session(Session(tmp_path / "shift"))
        widget.set_session(None)

        prefix = widget._status_prefix_label  # noqa: SLF001
        assert not prefix.isVisibleTo(prefix.parentWidget())
        assert widget._status_path_label.full_text() == (  # noqa: SLF001
            "No session - data is not being kept"
        )
    finally:
        widget.shutdown()
        viewer.close()


def test_with_no_session_the_free_space_field_goes_too(tmp_path):
    """
    No figure to report means no field and no divider standing beside it.

    A rule next to an empty space reads as a value that failed to load,
    rather than one that does not apply.
    """
    viewer, widget = _open()
    try:
        widget.set_session(Session(tmp_path / "shift"))
        rule = widget._status_space_rule  # noqa: SLF001
        assert rule.isVisibleTo(rule.parentWidget())

        widget.set_session(None)

        assert not rule.isVisibleTo(rule.parentWidget())
        assert not widget._space_label.isVisibleTo(  # noqa: SLF001
            rule.parentWidget(),
        )
    finally:
        widget.shutdown()
        viewer.close()


def test_the_status_fields_are_divided_by_rules():
    """
    Separators, so the fields do not read as one run-on sentence.

    Including one on the right, marking where ours stop and napari's
    "Ready" begins.
    """
    viewer, widget = _open()
    try:
        rules = [
            item
            for item in widget._status_widgets  # noqa: SLF001
            if isinstance(item, QtWidgets.QFrame)
            and item.frameShape() == QtWidgets.QFrame.VLine
        ]
        assert len(rules) == _EXPECTED_RULES
        # The last thing in the bar is a rule, dividing us from "Ready".
        assert widget._status_widgets[-1] in rules  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_shutdown_takes_the_labels_back_out():
    """
    A closed widget leaves no stale status line behind.

    Without this, docking a second widget into one viewer would stack a
    second copy of every label, and a closed one would keep describing a
    session nothing is writing to.
    """
    viewer, widget = _open()
    try:
        bar = viewer.window._qt_window.statusBar()  # noqa: SLF001
        label = widget._status_path_label  # noqa: SLF001
        assert bar.isAncestorOf(label)
        widget.shutdown()
        assert not bar.isAncestorOf(label)
    finally:
        viewer.close()


def test_the_status_bar_reports_free_space(tmp_path):
    """
    Disk free is an absolute figure, shown without opening anything.

    Absolute rather than a percentage: "12 GB free" answers "can I
    record this burst", and "8% free" does not.
    """
    viewer, widget = _open()
    try:
        widget.set_session(Session(tmp_path / "shift"))
        assert "free" in widget._space_label.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_session_context_lives_in_the_dialog():
    """
    Operator, sample and standing notes moved out of the dock.

    Asserted through ``isVisibleTo`` on the dialog rather than on the
    widget: these fields exist from construction (the rest of live.py
    writes to them), so their mere existence proves nothing about where
    they ended up.
    """
    viewer, widget = _open()
    try:
        dialog = widget.session_dialog
        assert widget._operator_edit.isVisibleTo(dialog)  # noqa: SLF001
        assert widget._sample_edit.isVisibleTo(dialog)  # noqa: SLF001
        assert widget._notes_edit.isVisibleTo(dialog)  # noqa: SLF001
        assert not widget._operator_edit.isVisibleTo(widget)  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_the_next_recordings_note_stays_in_the_dock():
    """
    The per-recording note is answered per recording, so it stays out.

    It changes with every burst, unlike the session context around it,
    and a field you retype that often does not belong behind a dialog.
    """
    viewer, widget = _open()
    try:
        note = widget._recording_note_edit  # noqa: SLF001
        assert note.isVisibleTo(widget)
        assert not note.isVisibleTo(widget.session_dialog)
    finally:
        widget.shutdown()
        viewer.close()


def test_opening_the_settings_dialog_does_not_block():
    """
    ``open_session_settings`` returns, rather than running a nested loop.

    The test is that this call finishes at all: ``QDialog.exec`` would
    not return until the dialog was closed, and nothing here closes it.
    """
    viewer, widget = _open()
    try:
        widget.open_session_settings()
        assert widget.session_dialog.isVisible()
        widget.session_dialog.hide()
    finally:
        widget.shutdown()
        viewer.close()


def test_the_controls_still_reach_the_instrument():
    """
    The rearrangement did not unwire anything.

    The panels are built by the same functions as before and merely
    re-parented, so this is the cheap check that "merely" is true.
    """
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
