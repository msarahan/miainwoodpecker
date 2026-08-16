"""
The Recordings panel: reaching data that is already on disk.

Split out of ``live.py`` when that file passed 1900 lines. These are
builders taking the widget rather than methods on it, so the widget
stays the single owner of every ``_`` attribute the rest of the module
and the tests reach for — moving the *construction* out without moving
the *state* is what makes this a rearrangement rather than a redesign.
"""

from __future__ import annotations

import typing

from qtpy import QtWidgets

if typing.TYPE_CHECKING:
    from miainwoodpecker.viewer.live import LiveInstrumentWidget


def build_recordings_group(widget: LiveInstrumentWidget) -> QtWidgets.QGroupBox:
    """
    Build the group for looking at data already on disk.

    The combo lists the current session's recordings; "Open from
    disk..." reaches any file, including one recorded on another machine
    or in a session that has since been closed. The checkbox is how the
    three Phase 4 analysis buttons are pointed at a file instead of a
    fresh burst — one switch rather than three more buttons, since the
    choice is "what do I analyze", not "what analysis".

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.

    Returns
    -------
    QtWidgets.QGroupBox
        The assembled group.
    """
    group = QtWidgets.QGroupBox("Recordings", widget)
    form = QtWidgets.QFormLayout(group)
    widget._recording_combo = QtWidgets.QComboBox(group)
    widget._recording_combo.setPlaceholderText("no recordings in this session")
    form.addRow("File", widget._recording_combo)
    widget._all_sessions_check = QtWidgets.QCheckBox(
        "List every session in the parent directory", group
    )
    form.addRow(widget._all_sessions_check)
    widget._open_recording_button = QtWidgets.QPushButton("Open selected", group)
    form.addRow(widget._open_recording_button)
    widget._open_file_button = QtWidgets.QPushButton("Open from disk...", group)
    form.addRow(widget._open_file_button)
    widget._analyze_from_file_check = QtWidgets.QCheckBox(
        "Analysis buttons use the opened file, not a fresh burst", group
    )
    form.addRow(widget._analyze_from_file_check)
    widget._load_status = QtWidgets.QLabel("nothing opened yet", group)
    widget._load_status.setWordWrap(True)
    form.addRow("Opened", widget._load_status)
    widget._annotation_edit = QtWidgets.QLineEdit(group)
    widget._annotation_edit.setPlaceholderText("note to add to the opened recording")
    form.addRow("Add note", widget._annotation_edit)
    widget._annotate_button = QtWidgets.QPushButton("Annotate opened", group)
    form.addRow(widget._annotate_button)

    widget._open_recording_button.clicked.connect(widget.open_selected_recording)
    widget._open_file_button.clicked.connect(widget.choose_and_open_recording)
    widget._all_sessions_check.toggled.connect(widget._refresh_session_labels)
    widget._annotate_button.clicked.connect(widget.annotate_opened_recording)
    widget._annotation_edit.returnPressed.connect(widget.annotate_opened_recording)
    return group
