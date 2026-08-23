"""
The analysis controls: a button per installed extra, and what is installed.

Two different things live here, and they are deliberately built into two
different places in the dock.

The **buttons** run against a camera, so they are added to a camera's
section by :func:`build_analysis_rows` - the same section that holds that
camera's exposure and its start control.

The **extras summary** is not about a camera at all. It answers "what can
this installation do?", which has the same answer for every device served
and for none: ``pip install`` decided it, not the hardware. It used to be
a row inside the first camera's section, where it read as a property of
that camera - a panel headed "Camera - usim_ronchigram_camera" with
"Analysis extras: enabled: none" printed under it says, to anyone reading
it, that this camera has no analysis. So :func:`build_analysis_panel`
gives it a section of its own, beside Instrument, Recordings and Devices
rather than inside one of them.

Moving it out also gave it an answer in a case where it had none: an
instrument serving no camera builds no camera section, so the summary had
nowhere to appear and an operator on a scanner-only instrument could not
find out what was installed.
"""

from __future__ import annotations

import typing

from qtpy import QtCore, QtWidgets

if typing.TYPE_CHECKING:
    from miainwoodpecker.viewer.live import LiveInstrumentWidget

# Wire name, button text, and the label of the status row beneath it.
_SPECIFICATIONS = (
    ("hyperspy", "Analyze in HyperSpy", "Analysis"),
    ("libertem", "Sum in LiberTEM", "LiberTEM"),
    ("py4dstem", "Fit central disk (py4DSTEM)", "py4DSTEM"),
)
# The widget attributes each target's controls are stored under.
_ATTRIBUTES = {
    "hyperspy": ("_analyze_button", "_analyze_status"),
    "libertem": ("_libertem_button", "_libertem_status"),
    "py4dstem": ("_py4dstem_button", "_py4dstem_status"),
}


def installed_extras() -> tuple[list[str], list[str]]:
    """
    Report which analysis extras are importable and which are not.

    The check is
    :func:`~miainwoodpecker.analysis.remote.target_available`, which
    resolves the module *spec* without executing it. Importing py4DSTEM
    to discover whether py4DSTEM is installed would stall building the
    window for seconds to answer a question with a cheap answer.

    This is an availability check, not a guarantee: a spec can resolve
    for a half-installed distribution whose import then fails. Each
    handler therefore keeps its own ``ImportError`` branch, and that
    branch is reachable rather than dead code.

    Returns
    -------
    tuple[list[str], list[str]]
        The extras that are installed and those that are not, both named
        as ``pyproject.toml`` names them.
    """
    from miainwoodpecker.analysis.remote import target_available  # noqa: PLC0415
    from miainwoodpecker.analysis.transfer import (  # noqa: PLC0415
        ANALYSIS_TARGETS,
    )

    enabled: list[str] = []
    missing: list[str] = []
    for name, _text, _row_label in _SPECIFICATIONS:
        extra = ANALYSIS_TARGETS[name].extra
        (enabled if target_available(name) else missing).append(extra)
    return enabled, missing


def reset_analysis_controls(widget: LiveInstrumentWidget) -> None:
    """
    Declare every analysis button and status label absent.

    Called before any of them is built, and called *whether or not* any
    will be - an instrument with no camera builds none. The handlers in
    ``live.py`` read these attributes and return when they are ``None``,
    so an attribute that was never set would turn "nothing to analyze"
    into an ``AttributeError``.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the controls.
    """
    for button_attribute, status_attribute in _ATTRIBUTES.values():
        setattr(widget, button_attribute, None)
        setattr(widget, status_attribute, None)


def build_analysis_rows(
    widget: LiveInstrumentWidget,
    camera_group: QtWidgets.QWidget,
    camera_form: QtWidgets.QFormLayout,
) -> None:
    """
    Add a button per *installed* analysis extra to a camera's section.

    A button for a library that is not installed is a button that cannot
    work, and offering it teaches the operator that this application's
    buttons sometimes do nothing. So each one is built only when its
    extra is importable, and the extras that are not are named by the
    Analysis extras section instead - see :func:`build_analysis_panel`.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.
    camera_group : QtWidgets.QWidget
        The group the widgets are parented to.
    camera_form : QtWidgets.QFormLayout
        The layout the rows are added to.
    """
    from miainwoodpecker.analysis.remote import target_available  # noqa: PLC0415

    handlers = {
        "hyperspy": widget._analyze_camera_in_hyperspy,
        "libertem": widget._analyze_camera_in_libertem,
        "py4dstem": widget._fit_central_disk_in_py4dstem,
    }
    for name, text, row_label in _SPECIFICATIONS:
        if not target_available(name):
            continue
        button_attribute, status_attribute = _ATTRIBUTES[name]
        button = QtWidgets.QPushButton(text, camera_group)
        camera_form.addRow(button)
        status = QtWidgets.QLabel("", camera_group)
        camera_form.addRow(row_label, status)
        button.clicked.connect(handlers[name])
        setattr(widget, button_attribute, button)
        setattr(widget, status_attribute, status)


def build_analysis_panel(
    widget: LiveInstrumentWidget,
) -> QtWidgets.QGroupBox | None:
    """
    Build the Analysis extras panel, or nothing when there is nothing to say.

    ``None`` when all three extras are installed: the summary stands in
    for buttons that are not there, and with none missing it would be a
    whole section restating what the buttons already say.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget the panel is parented to.

    Returns
    -------
    QtWidgets.QGroupBox | None
        The assembled panel, or None when every extra is installed.
    """
    enabled, missing = installed_extras()
    if not missing:
        return None
    panel = QtWidgets.QGroupBox("Analysis extras", widget)
    layout = QtWidgets.QVBoxLayout(panel)
    layout.addWidget(build_extras_summary(panel, enabled, missing))
    return panel


def build_extras_summary(
    parent: QtWidgets.QWidget,
    enabled: list[str],
    missing: list[str],
) -> QtWidgets.QLabel:
    """
    Describe which analysis extras are installed and which are not.

    Names both halves rather than only the missing one, because "no
    analysis buttons" and "analysis is installed but this build has no
    camera" look identical from the outside, and an operator deciding
    whether to install anything needs to see the whole set.

    Parameters
    ----------
    parent : QtWidgets.QWidget
        The widget the label is parented to.
    enabled : list[str]
        Extras whose libraries are importable.
    missing : list[str]
        Extras whose libraries are not.

    Returns
    -------
    QtWidgets.QLabel
        A three-line summary, selectable so the install command can be
        copied out of it.
    """
    extras = ",".join(missing)
    lines = [f"enabled: {', '.join(enabled)}" if enabled else "enabled: none"]
    lines.append(f"available: {', '.join(missing)}")
    lines.append(f'pip install "miainwoodpecker[{extras}]"')
    label = QtWidgets.QLabel("\n".join(lines), parent)
    label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    label.setWordWrap(True)
    return label
