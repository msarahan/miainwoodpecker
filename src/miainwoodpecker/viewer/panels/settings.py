"""
A modal sheet for the settings a device does not change often.

Exposure, binning, readout mode and the scan profiles are set at the
start of a session and then largely left alone, while start, acquire and
record are pressed all day. Giving all of them the same permanent row in
the dock spent most of the panel on the ones used least — and on a
spectrometer with per-axis binning it had just grown another row.

So the rare ones move here, behind a **⚙** button, and the panel keeps
the frequent ones (see
:mod:`miainwoodpecker.viewer.panels.toolbar`). This is
DigitalMicrograph's arrangement, and the reason it works is that a
setting you change once a session is *cheaper* two clicks away than it is
occupying a row you read past a hundred times.

**The widgets live here but are still the widget's own.** The dialog is
built with the device group and kept, hidden, for the life of the
window — not created on demand — so ``binding.exposure_spin`` and the
rest exist and are readable from the moment the panel does, exactly as
when they sat in the dock. Nothing that reads a setting needs to know it
moved, and a test that sets one does not have to open a dialog first.

Modal, and closing is the only button. There is no OK/Cancel pair
because there is nothing to cancel: these controls apply the way they
always did — the readout combo configures the device as it changes, and
exposure and binning are read at the moment an acquisition is taken. A
Cancel that could not undo either would be a lie about what the dialog
does.
"""

from __future__ import annotations

from qtpy import QtWidgets


class SettingsDialog(QtWidgets.QDialog):
    """
    A modal form of one device's infrequent settings.

    Parameters
    ----------
    title : str
        The dialog's window title — the device it belongs to.
    parent : QtWidgets.QWidget
        The device's group box, so the dialog centres on the window and
        is destroyed with it.
    """

    def __init__(self, title: str, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        layout = QtWidgets.QVBoxLayout(self)
        self._form = QtWidgets.QFormLayout()
        layout.addLayout(self._form)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close,
            parent=self,
        )
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    @property
    def form(self) -> QtWidgets.QFormLayout:
        """
        The layout callers add their setting rows to.

        Returns
        -------
        QtWidgets.QFormLayout
            The dialog's form.
        """
        return self._form

    def show_modal(self) -> None:
        """
        Open the dialog over the window.

        Separate from ``exec`` so the button connection reads as an
        intention rather than a Qt call, and so a test can drive the
        settings without a nested event loop by touching the widgets
        directly — which is the point of them existing whether or not
        this has ever been opened.
        """
        self.show()
        self.raise_()
        self.activateWindow()
