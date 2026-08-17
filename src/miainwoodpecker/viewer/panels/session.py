"""
The Session settings dialog, and the per-recording note that stays behind.

Session context — where data goes, who is on the instrument, what the
sample is, the shift's standing notes — is set-and-forget: typed once at
the start of a session and then left alone. It spent four form rows and
two text boxes in the dock paying for that once-a-shift use, on a panel
that did not fit on the screen.

So it moved into a dialog, and the two facts that *are* continuously
useful (the directory, the free space) moved into the status bar at the
foot of the dock — see :mod:`miainwoodpecker.viewer.panels.statusbar`.
What did not move is the note for the next recording: that changes per
recording rather than per session, so it belongs where the recording
controls are, a click away rather than behind a dialog.

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


def build_session_dialog(widget: LiveInstrumentWidget) -> QtWidgets.QDialog:
    """
    Build the Session settings dialog.

    Built once, with the widget, and shown and hidden thereafter rather
    than constructed on demand. Two reasons, and the second is the real
    one: the fields are ``widget``-owned attributes that the rest of
    ``live.py`` writes to and reads from (``_operator_edit`` and friends
    are saved to ``session.json`` on a debounce timer), so they have to
    exist from construction whether or not anyone has opened the dialog
    — a lazily built dialog would leave every one of those call sites
    reaching for an attribute that was not there yet.

    Application-modal, but shown rather than ``exec``'d. Modal because
    this is a settings dialog and there is nothing sensible to do to the
    instrument halfway through editing it; shown because ``exec`` runs a
    nested event loop that does not return until the dialog closes,
    which would make :meth:`LiveInstrumentWidget.open_session_settings`
    block its caller and, with it, every test that opens the dialog.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.

    Returns
    -------
    QtWidgets.QDialog
        The assembled dialog, hidden.
    """
    dialog = QtWidgets.QDialog(widget)
    dialog.setWindowTitle("Session settings")
    dialog.setWindowModality(QtCore.Qt.ApplicationModal)
    form = QtWidgets.QFormLayout(dialog)

    widget._session_path_label = QtWidgets.QLabel(_NO_SESSION_MESSAGE, dialog)
    widget._session_path_label.setWordWrap(True)
    # Selectable: a path an operator can copy is a path they can paste
    # into a terminal or a file manager, which is most of what anyone
    # wants from seeing one.
    widget._session_path_label.setTextInteractionFlags(
        QtCore.Qt.TextSelectableByMouse,
    )
    form.addRow("Saving to", widget._session_path_label)
    widget._change_session_button = QtWidgets.QPushButton(
        "Change directory...", dialog
    )
    form.addRow(widget._change_session_button)

    build_session_context_rows(widget, dialog, form)

    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close, dialog)
    form.addRow(buttons)

    widget._change_session_button.clicked.connect(widget.change_session_directory)
    buttons.rejected.connect(dialog.hide)
    return dialog


def build_session_context_rows(
    widget: LiveInstrumentWidget,
    parent: QtWidgets.QWidget,
    form: QtWidgets.QFormLayout,
) -> None:
    """
    Add the operator/sample/notes fields to a form.

    Notes are multi-line: a single-line field was enough to prove the
    wiring and not enough for a shift's worth of observations. This is
    the shift's standing context, written to every subsequent recording;
    the individual file's note is :func:`build_recording_note_row`,
    which lives elsewhere because it is answered at a different rate
    (see :meth:`~miainwoodpecker.storage.session.Session.record`).

    ``QPlainTextEdit`` has no ``editingFinished`` signal, so a
    single-shot timer debounces ``textChanged`` instead: typing a
    paragraph should not rewrite ``session.json`` once per keystroke.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.
    parent : QtWidgets.QWidget
        The widget owning the new controls.
    form : QtWidgets.QFormLayout
        The layout, appended to.
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

    widget._context_save_timer = QtCore.QTimer(widget)
    widget._context_save_timer.setSingleShot(True)
    widget._context_save_timer.setInterval(_CONTEXT_SAVE_DELAY_MS)
    widget._context_save_timer.timeout.connect(widget._on_session_context_edited)
    for edit in (widget._operator_edit, widget._sample_edit):
        edit.editingFinished.connect(widget._on_session_context_edited)
    widget._notes_edit.textChanged.connect(widget._context_save_timer.start)


def build_recording_note_row(
    widget: LiveInstrumentWidget,
    parent: QtWidgets.QWidget,
    form: QtWidgets.QFormLayout,
) -> None:
    """
    Add the note that describes the *next* recording.

    Kept out of the settings dialog on purpose. This one is answered per
    recording — a focal series, then a different feature — so it has to
    be visible and editable next to the button that starts a recording,
    not two clicks away behind a dialog that is meant to be opened once
    a shift.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.
    parent : QtWidgets.QWidget
        The widget owning the new control.
    form : QtWidgets.QFormLayout
        The layout, appended to.
    """
    widget._recording_note_edit = QtWidgets.QPlainTextEdit(parent)
    widget._recording_note_edit.setPlaceholderText(
        "what this next recording is - kept until you change it"
    )
    widget._recording_note_edit.setMaximumHeight(_NOTES_HEIGHT_PX)
    form.addRow("Note for next recording", widget._recording_note_edit)
