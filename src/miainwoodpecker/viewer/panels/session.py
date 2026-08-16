"""
The Session panel: where data is kept, and the context stored beside it.

See :mod:`miainwoodpecker.viewer.panels.recordings` for why these are
builders taking the widget rather than methods on it.
"""

from __future__ import annotations

import typing

from qtpy import QtCore, QtWidgets

from miainwoodpecker.viewer.panels.defaults import (
    _CONTEXT_SAVE_DELAY_MS,
    _NOTES_HEIGHT_PX,
    _NO_SESSION_MESSAGE,
)

if typing.TYPE_CHECKING:
    from miainwoodpecker.viewer.live import LiveInstrumentWidget


def build_session_group(widget: LiveInstrumentWidget) -> QtWidgets.QGroupBox:
    """Build the group showing where data goes and the session context."""
    session_group = QtWidgets.QGroupBox("Session", widget)
    session_form = QtWidgets.QFormLayout(session_group)
    widget._session_path_label = QtWidgets.QLabel(_NO_SESSION_MESSAGE, session_group)
    widget._session_path_label.setWordWrap(True)
    session_form.addRow("Saving to", widget._session_path_label)
    widget._change_session_button = QtWidgets.QPushButton(
        "Change directory...", session_group
    )
    session_form.addRow(widget._change_session_button)
    build_session_context_rows(widget, session_group, session_form)
    widget._space_label = QtWidgets.QLabel("", session_group)
    widget._space_label.setWordWrap(True)
    session_form.addRow("Disk", widget._space_label)
    widget._recorded_label = QtWidgets.QLabel("nothing recorded yet", session_group)
    widget._recorded_label.setWordWrap(True)
    session_form.addRow("Recorded", widget._recorded_label)
    widget._recording_status = QtWidgets.QLabel("idle", session_group)
    widget._recording_status.setWordWrap(True)
    session_form.addRow("Recording", widget._recording_status)
    widget._cancel_record_button = QtWidgets.QPushButton(
        "Stop recording", session_group
    )
    widget._cancel_record_button.setEnabled(False)
    session_form.addRow(widget._cancel_record_button)

    widget._change_session_button.clicked.connect(widget.change_session_directory)
    widget._cancel_record_button.clicked.connect(widget.cancel_recording)
    return session_group

def build_session_context_rows(
    widget: LiveInstrumentWidget,
    parent: QtWidgets.QWidget,
    form: QtWidgets.QFormLayout,
) -> None:
    """
    Add the operator/sample/notes fields, and the next recording's note.

    Notes are multi-line: a single-line field was enough to prove the
    wiring and not enough for a shift's worth of observations. Two
    scopes, because they answer different questions — "Session notes"
    is the shift's standing context and is written to every subsequent
    recording, while "Note for next recording" describes the individual
    file (see :meth:`~miainwoodpecker.storage.session.Session.record`).

    ``QPlainTextEdit`` has no ``editingFinished`` signal, so a
    single-shot timer debounces ``textChanged`` instead: typing a
    paragraph should not rewrite ``session.json`` once per keystroke.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.
    parent : QtWidgets.QWidget
        The group box owning the new widgets.
    form : QtWidgets.QFormLayout
        The group's layout, appended to.
    """
    widget._operator_edit = QtWidgets.QLineEdit(parent)
    widget._operator_edit.setPlaceholderText("who is on the instrument")
    form.addRow("Operator", widget._operator_edit)
    widget._sample_edit = QtWidgets.QLineEdit(parent)
    widget._sample_edit.setPlaceholderText("sample identifier")
    form.addRow("Sample", widget._sample_edit)
    widget._notes_edit = QtWidgets.QPlainTextEdit(parent)
    widget._notes_edit.setPlaceholderText("notes for the whole session")
    widget._notes_edit.setMaximumHeight(_NOTES_HEIGHT_PX)
    form.addRow("Session notes", widget._notes_edit)
    widget._recording_note_edit = QtWidgets.QPlainTextEdit(parent)
    widget._recording_note_edit.setPlaceholderText(
        "what this next recording is - kept until you change it"
    )
    widget._recording_note_edit.setMaximumHeight(_NOTES_HEIGHT_PX)
    form.addRow("Note for next recording", widget._recording_note_edit)

    widget._context_save_timer = QtCore.QTimer(widget)
    widget._context_save_timer.setSingleShot(True)
    widget._context_save_timer.setInterval(_CONTEXT_SAVE_DELAY_MS)
    widget._context_save_timer.timeout.connect(widget._on_session_context_edited)
    for edit in (widget._operator_edit, widget._sample_edit):
        edit.editingFinished.connect(widget._on_session_context_edited)
    widget._notes_edit.textChanged.connect(widget._context_save_timer.start)
