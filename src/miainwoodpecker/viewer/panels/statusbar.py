"""
Where data is going, and whether there is room for it, in napari's status bar.

Two facts an operator needs continuously and should never have to hunt
for: the directory being written to, and the free space on it. Both used
to live in the Session group, several scrolls down a stack that did not
fit on the screen, so the answer to "am I actually recording anywhere?"
was off the bottom of the window.

They now sit in the main window's own status bar, to the left of
napari's "Ready" — the line a user already reads for status, rather than
a second status line of ours a few hundred pixels above it.

**Read-only, deliberately.** These are labels and not buttons: the path
is set in the Session settings dialog, and a status bar that quietly
doubled as a control would mean the same setting had two homes, one of
them invisible until you happened to click the text.

Inserting rather than appending, and why it is safe
---------------------------------------------------
``QStatusBar.showMessage`` hides widgets added with ``addWidget``, which
would normally make the left-hand area a poor place to put anything
lasting. napari does not use it: "Ready" is drawn by its own
``StatusBarWidget``, and ``currentMessage()`` stays empty. So
``insertWidget(0, ...)`` puts these to the left of it and they stay put.
"""

from __future__ import annotations

import typing

from qtpy import QtCore, QtWidgets

if typing.TYPE_CHECKING:
    import napari

    from miainwoodpecker.viewer.live import LiveInstrumentWidget

# Enough that the elided path keeps a useful tail ("...2026-08-16")
# rather than collapsing to an ellipsis on a narrow window.
_MIN_PATH_WIDTH_PX = 140

# Its own wording rather than panels.defaults._NO_SESSION_MESSAGE, which
# several *form rows* share. Here the string stands alone with its label
# hidden, so it is a sentence rather than the right-hand half of one and
# is capitalised to match.
_NO_SESSION_STATUS = "No session - data is not being kept"

# Semi-transparent white rather than a fixed grey: it reads as a divider
# on napari's dark theme and on its light one, without this module
# having to know which is in use.
_RULE_COLOUR = "rgba(255, 255, 255, 0.28)"
_RULE_WIDTH_PX = 1
_RULE_HEIGHT_PX = 14


class ElidingLabel(QtWidgets.QLabel):
    """
    A label showing a path, shortened from the left to fit its width.

    From the left because the informative end of a session directory is
    its tail: every session under one root shares the leading
    components, and eliding the other way would show an operator the
    part that is the same for every session they have ever recorded.

    Re-elides on resize rather than once at build time, since the window
    is resizable and a path fitted to the width it had at construction
    would be wrong the moment anyone dragged the corner.

    Parameters
    ----------
    parent : QtWidgets.QWidget | None
        Optional Qt parent widget.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(_MIN_PATH_WIDTH_PX)

    def full_text(self) -> str:
        """
        Return the unshortened text.

        Returns
        -------
        str
            What was last passed to :meth:`set_full_text`.
        """
        return self._full_text

    def set_full_text(self, text: str) -> None:
        """
        Set the text, keeping the unshortened version for the tooltip.

        Parameters
        ----------
        text : str
            The full string to display, elided to fit.
        """
        self._full_text = text
        self.setToolTip(text)
        self._apply_elision()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """
        Re-elide the text for the new width.

        Parameters
        ----------
        event : QtGui.QResizeEvent
            The resize event, passed to the base class.
        """
        super().resizeEvent(event)
        self._apply_elision()

    def _apply_elision(self) -> None:
        """Shorten the stored text to the current width."""
        metrics = self.fontMetrics()
        available = max(self.width() - metrics.height(), 0)
        super().setText(
            metrics.elidedText(self._full_text, QtCore.Qt.ElideLeft, available),
        )


def build_status_widgets(widget: LiveInstrumentWidget) -> list[QtWidgets.QWidget]:
    """
    Create the status labels, whether or not there is a bar to put them in.

    Built unconditionally and installed separately, because the rest of
    ``live.py`` writes to these labels on every session change: a widget
    constructed against a viewer with no reachable status bar still has
    to have somewhere for ``_refresh_space_label`` to write.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting labels; every ``_``-prefixed
        attribute set here belongs to it.

    Returns
    -------
    list[QtWidgets.QWidget]
        The labels and the rules between them, left to right.
    """
    widget._status_prefix_label = QtWidgets.QLabel("Saving to")
    widget._status_path_label = ElidingLabel()
    # Single line, unlike its old home in the Session form: this label
    # also carries the "not enough room for the recording you are about
    # to start" warning, and a status bar that grew a second line would
    # shove the canvas. The tooltip holds the untruncated text.
    widget._space_label = QtWidgets.QLabel()
    widget._status_space_rule = _separator()
    return [
        widget._status_prefix_label,
        widget._status_path_label,
        widget._status_space_rule,
        widget._space_label,
        # A rule on the right as well, marking where our fields stop and
        # napari's "Ready" begins - without it the two run together and
        # read as one sentence.
        _separator(),
    ]


def _separator() -> QtWidgets.QFrame:
    """
    Return a vertical rule for dividing one status field from the next.

    Returns
    -------
    QtWidgets.QFrame
        A one-pixel vertical rule, coloured explicitly.
    """
    rule = QtWidgets.QFrame()
    rule.setFrameShape(QtWidgets.QFrame.VLine)
    # Explicit width, height and colour rather than the frame defaults.
    # A plain sunken VLine draws itself from the palette's shadow
    # colours, which on napari's dark theme are within a few percent of
    # the bar behind it - the rule was there and invisible. It is also
    # given a height, because a zero-size-hint frame in a status bar
    # collapses to nothing.
    rule.setFixedWidth(_RULE_WIDTH_PX)
    rule.setMinimumHeight(_RULE_HEIGHT_PX)
    rule.setStyleSheet(f"QFrame {{ background-color: {_RULE_COLOUR}; border: none; }}")
    return rule


def set_destination(widget: LiveInstrumentWidget, path: str | None) -> None:
    """
    Show where recordings are going, or that they are going nowhere.

    With no session the "Saving to" prefix is hidden rather than left
    standing in front of an apology: "Saving to no session - data is not
    being kept" is a sentence that has to be read twice to find out it
    means nothing is being saved.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget whose status labels are updated.
    path : str | None
        The session directory, or None when there is no session.
    """
    if path is None:
        widget._status_prefix_label.hide()
        widget._status_path_label.set_full_text(_NO_SESSION_STATUS)
        return
    widget._status_prefix_label.show()
    widget._status_path_label.set_full_text(path)


def set_free_space(widget: LiveInstrumentWidget, text: str) -> None:
    """
    Show the free space, or take the field away when there is none to show.

    The rule in front of it goes with it. With no session there is no
    figure to report, and a divider standing next to an empty field
    reads as a value that failed to load rather than as one that does
    not apply.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget whose status labels are updated.
    text : str
        The free-space text, or an empty string for "not applicable".
    """
    widget._space_label.setText(text)
    widget._space_label.setToolTip(text)
    shown = bool(text)
    widget._space_label.setVisible(shown)
    widget._status_space_rule.setVisible(shown)


def install_status_bar(
    widget: LiveInstrumentWidget,
    viewer: napari.Viewer,
) -> bool:
    """
    Put the status labels into the main window's status bar.

    Reaches through ``viewer.window`` for the Qt main window, which is
    private napari API and the reason this is written to fail soft: a
    viewer with no window (or a napari that has moved the attribute)
    costs the status line, not the application. The labels still exist
    either way.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget whose labels are installed.
    viewer : napari.Viewer
        The viewer whose window supplies the status bar.

    Returns
    -------
    bool
        True if the labels were installed.
    """
    bar = _status_bar(viewer)
    if bar is None:
        return False
    # Reversed, because each insert at 0 pushes the previous one right;
    # inserting in display order would reverse them on screen.
    for item in reversed(widget._status_widgets):
        # The path gets the stretch, so it spends any spare width on
        # showing more of itself. Without it every label sits at its
        # minimum and the path elides to a stub while the bar beside it
        # is empty. The fixed-size labels either side keep stretch 0.
        stretch = 1 if item is widget._status_path_label else 0
        bar.insertWidget(0, item, stretch)
        item.show()
    # Last, and after the blanket show above: a freshly built widget has
    # no session, so this is what hides the "Saving to" prefix in front
    # of a message that says nothing is being saved, and the free-space
    # field that has no figure to report yet.
    set_destination(widget, None)
    set_free_space(widget, "")
    return True


def remove_status_bar(
    widget: LiveInstrumentWidget,
    viewer: napari.Viewer,
) -> None:
    """
    Take the status labels back out of the main window's status bar.

    Called from ``shutdown``. Without it a second widget docked into the
    same viewer would add a second copy of every label, and a closed
    widget would leave its own behind describing a session nothing is
    writing to any more.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget whose labels are removed.
    viewer : napari.Viewer
        The viewer whose window supplied the status bar.
    """
    bar = _status_bar(viewer)
    if bar is None:
        return
    for item in widget._status_widgets:
        # removeWidget takes the item out of the *layout* only: the
        # widget stays a child of the bar, still parented and still
        # shown. Hiding and reparenting are what actually remove it.
        # ``widget._status_widgets`` keeps a Python reference, so
        # dropping the Qt parent does not destroy them.
        bar.removeWidget(item)
        item.hide()
        item.setParent(None)


def _status_bar(viewer: napari.Viewer) -> QtWidgets.QStatusBar | None:
    """
    Return the viewer's Qt status bar, or None if it cannot be reached.

    Parameters
    ----------
    viewer : napari.Viewer
        The viewer to ask.

    Returns
    -------
    QtWidgets.QStatusBar | None
        The status bar, or None.
    """
    try:
        bar = viewer.window._qt_window.statusBar()
    except (AttributeError, RuntimeError):
        # RuntimeError: the C++ window is already gone, which is normal
        # during teardown after the viewer has been closed.
        return None
    return bar if isinstance(bar, QtWidgets.QStatusBar) else None
