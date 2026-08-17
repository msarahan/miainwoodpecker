"""
The folding section used by every panel that can be put away.

Lives here rather than in :mod:`miainwoodpecker.viewer.panels.devices`,
where it was written, because it is no longer a device idea: the dock's
four top-level groups (Instrument, Session, Recordings, Devices) fold
with the same widget as the per-device sections nested inside the last
of them. One implementation means one disclosure triangle, one keyboard
behaviour, and one place to change either.
"""

from __future__ import annotations

from qtpy import QtCore, QtWidgets


class CollapsibleSection(QtWidgets.QWidget):
    """
    A titled header that folds its content away.

    An instrument with a scan unit, three cameras and a spectrometer has
    more controls than fit on a screen, and an operator aligning one
    detector does not want the other four in the way. Folding is the
    cheapest answer that keeps *several* open at once, which tabs would
    not: watching a camera while a scan runs is the ordinary case.

    A disclosure triangle rather than ``QGroupBox.setCheckable``, which
    would put a checkbox in the title. On an instrument panel a checkbox
    beside a device name reads as "switch this device off", and a
    control that looks like it turns hardware off had better turn
    hardware off.

    Folding is a tidiness feature and **not** the answer to a panel
    taller than the screen; the dock scrolls for that. A section an
    operator has deliberately opened must never become unreachable, and
    an arrangement that relied on folding to fit would make it so.

    Parameters
    ----------
    title : str
        Header text, naming what folds away.
    content : QtWidgets.QWidget
        The widget to show and hide. Re-parented to this section.
    parent : QtWidgets.QWidget | None
        Optional Qt parent widget.
    expanded : bool
        Whether the section starts open.
    """

    def __init__(
        self,
        title: str,
        content: QtWidgets.QWidget,
        parent: QtWidgets.QWidget | None = None,
        *,
        expanded: bool = True,
    ) -> None:
        super().__init__(parent)
        self._toggle = QtWidgets.QToolButton(self)
        self._toggle.setText(title)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(expanded)
        self._toggle.setStyleSheet("QToolButton { border: none; font-weight: 600; }")
        self._toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(
            QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow,
        )
        self._content = content
        content.setParent(self)
        content.setVisible(expanded)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._toggle)
        layout.addWidget(content)
        self._toggle.toggled.connect(self.set_expanded)

    @property
    def title(self) -> str:
        """Return the header text, which names what folds away."""
        return self._toggle.text()

    def is_expanded(self) -> bool:
        """
        Report whether this section's content is showing.

        Returns
        -------
        bool
            True when the content is visible.
        """
        return self._toggle.isChecked()

    def set_expanded(self, expanded: bool) -> None:  # noqa: FBT001
        """
        Show or fold this section's content.

        Positionally typed ``bool`` because this is a Qt slot: it is
        connected to ``QToolButton.toggled``, which calls it with the
        new state as the one positional argument.

        Parameters
        ----------
        expanded : bool
            True to show the content, False to fold it away.
        """
        if self._toggle.isChecked() != expanded:
            self._toggle.setChecked(expanded)
        self._toggle.setArrowType(
            QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow,
        )
        self._content.setVisible(expanded)
