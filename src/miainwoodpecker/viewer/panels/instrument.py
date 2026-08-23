"""
The Instrument panel: what this instrument is, and the controls it offers.

Two halves. The top is **read-only** and answers "what am I connected
to?" — backend, served targets, and which controls the instrument
publishes. That question had no answer in the window at all before this
panel: an operator who launched against the wrong backend found out from
the images.

The bottom **writes to the instrument**. Defocus, energy offset and the
stage move optics on a real column, and the beam blanker turns the beam
off. That is the point of them, and it is why this module is careful
about three things.

**Only controls the instrument reports get a row.** The control set comes
from :meth:`~miainwoodpecker.devices.interface.Instrument.available_controls`,
not from a fixed list, for the same reason the Devices panel is built
from served targets: a control that is not there must be *absent* rather
than present and dead. A microscope with no blanker gets no blanker
checkbox.

**No range limits are applied here**, and that is a decision rather than
an omission — see
:class:`~miainwoodpecker.devices.interface.InstrumentController`, where
it is recorded. A client that clamped would be a second source of truth
for a limit only the hardware knows, and a client whose idea of the
limit had drifted would send a *different* value than the one on screen.
The spin boxes carry a deliberately enormous numeric range: Qt requires
one, and a narrow one would be a clamp wearing a different hat. The
instrument refuses what it will not do, and the refusal is shown.

**Values are read on demand, not polled.** The display timer runs at
16 ms; asking the instrument for four controls at that rate would put
traffic on the wire to answer a question nobody asked. They are read
when the panel is built and when **Refresh** is pressed.
"""

from __future__ import annotations

import typing

from qtpy import QtWidgets

from miainwoodpecker.devices.interface import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    STAGE_POSITION_CONTROL,
)
from miainwoodpecker.devices.rpc import INSTRUMENT_TARGET

if typing.TYPE_CHECKING:
    from miainwoodpecker.viewer.live import LiveInstrumentWidget

# Qt needs a range; this one exists to be wider than any instrument's, so
# that it never becomes the limit. See the module docstring.
_UNBOUNDED = 1e12
_DECIMALS = 3


def build_instrument_panel(widget: LiveInstrumentWidget) -> QtWidgets.QGroupBox:
    """
    Build the Instrument panel: identity, then the controls it publishes.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.

    Returns
    -------
    QtWidgets.QGroupBox
        The assembled panel, for the caller to add to its layout.
    """
    panel = QtWidgets.QGroupBox("Instrument", widget)
    form = QtWidgets.QFormLayout(panel)
    instrument = widget._instrument

    widget._instrument_backend_label = QtWidgets.QLabel("not connected", panel)
    form.addRow("Backend", widget._instrument_backend_label)
    widget._instrument_targets_label = QtWidgets.QLabel("-", panel)
    widget._instrument_targets_label.setWordWrap(True)
    form.addRow("Serves", widget._instrument_targets_label)
    widget._instrument_status = QtWidgets.QLabel("", panel)
    widget._instrument_status.setWordWrap(True)
    form.addRow("Status", widget._instrument_status)

    widget._instrument_controls = {}
    if instrument is None:
        return panel

    controls = set(widget._description(INSTRUMENT_TARGET).controls)
    if DEFOCUS_CONTROL in controls:
        widget._instrument_controls[DEFOCUS_CONTROL] = _add_number_row(
            widget, panel, form, "Defocus (nm)", DEFOCUS_CONTROL,
        )
    if ENERGY_OFFSET_CONTROL in controls:
        widget._instrument_controls[ENERGY_OFFSET_CONTROL] = _add_number_row(
            widget, panel, form, "Energy offset (eV)", ENERGY_OFFSET_CONTROL,
        )
    if STAGE_POSITION_CONTROL in controls:
        _add_stage_row(widget, panel, form)
    if BEAM_BLANKER_CONTROL in controls:
        _add_blanker_row(widget, panel, form)

    refresh = QtWidgets.QPushButton("Refresh", panel)
    refresh.clicked.connect(widget.refresh_instrument)
    form.addRow(refresh)
    widget.refresh_instrument()
    return panel


def _spin(parent: QtWidgets.QWidget) -> QtWidgets.QDoubleSpinBox:
    """
    Return a spin box whose range is not a safety limit.

    Parameters
    ----------
    parent : QtWidgets.QWidget
        The parent widget.

    Returns
    -------
    QtWidgets.QDoubleSpinBox
        A spin box spanning far more than any instrument's real range.
    """
    spin = QtWidgets.QDoubleSpinBox(parent)
    spin.setRange(-_UNBOUNDED, _UNBOUNDED)
    spin.setDecimals(_DECIMALS)
    spin.setKeyboardTracking(False)
    return spin


def _add_number_row(
    widget: LiveInstrumentWidget,
    panel: QtWidgets.QGroupBox,
    form: QtWidgets.QFormLayout,
    label: str,
    control: str,
) -> QtWidgets.QDoubleSpinBox:
    """
    Add one editable scalar control with its own Set button.

    A **Set** button rather than writing on every edit: a spin box's
    arrows would drive the optics once per click on the way to a value,
    and typing "150" into an empty box would pass through 1 and 15.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls.
    panel : QtWidgets.QGroupBox
        The panel the widgets are parented to.
    form : QtWidgets.QFormLayout
        The layout the row is added to.
    label : str
        The row label, including its unit.
    control : str
        The control name, as ``available_controls`` reports it.

    Returns
    -------
    QtWidgets.QDoubleSpinBox
        The spin box holding the value.
    """
    row = QtWidgets.QWidget(panel)
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    spin = _spin(row)
    apply_button = QtWidgets.QPushButton("Set", row)
    layout.addWidget(spin)
    layout.addWidget(apply_button)
    apply_button.clicked.connect(
        lambda *_, name=control: widget.apply_instrument_control(name),
    )
    form.addRow(label, row)
    return spin


def _add_stage_row(
    widget: LiveInstrumentWidget,
    panel: QtWidgets.QGroupBox,
    form: QtWidgets.QFormLayout,
) -> None:
    """
    Add the stage position as a ``(y, x)`` pair with one Set button.

    One button for both axes because the instrument takes them together:
    two buttons would move the stage twice to reach one position.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls.
    panel : QtWidgets.QGroupBox
        The panel the widgets are parented to.
    form : QtWidgets.QFormLayout
        The layout the row is added to.
    """
    row = QtWidgets.QWidget(panel)
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    widget._instrument_stage_y = _spin(row)
    widget._instrument_stage_x = _spin(row)
    apply_button = QtWidgets.QPushButton("Set", row)
    layout.addWidget(QtWidgets.QLabel("y", row))
    layout.addWidget(widget._instrument_stage_y)
    layout.addWidget(QtWidgets.QLabel("x", row))
    layout.addWidget(widget._instrument_stage_x)
    layout.addWidget(apply_button)
    apply_button.clicked.connect(
        lambda *_: widget.apply_instrument_control(STAGE_POSITION_CONTROL),
    )
    form.addRow("Stage (nm)", row)


def _add_blanker_row(
    widget: LiveInstrumentWidget,
    panel: QtWidgets.QGroupBox,
    form: QtWidgets.QFormLayout,
) -> None:
    """
    Add the beam blanker as a checkbox that writes when you click it.

    No Set button, unlike the scalars: a blanker is one bit and the
    click *is* the decision. It is also the one control here that turns
    the beam off, which is an operator action with its own control
    rather than something another limit should ever do as a side effect.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls.
    panel : QtWidgets.QGroupBox
        The panel the widgets are parented to.
    form : QtWidgets.QFormLayout
        The layout the row is added to.
    """
    widget._instrument_blanker = QtWidgets.QCheckBox("blanked", panel)
    widget._instrument_blanker.clicked.connect(
        lambda checked: widget.apply_beam_blanker(blanked=checked),
    )
    form.addRow("Beam", widget._instrument_blanker)
