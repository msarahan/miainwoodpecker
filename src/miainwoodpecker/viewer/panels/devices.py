"""
The device panels: the scan unit, the cameras, and their recording controls.

See :mod:`miainwoodpecker.viewer.panels.recordings` for why these are
builders taking the widget rather than methods on it.
"""

from __future__ import annotations

import typing

from qtpy import QtCore, QtWidgets

from miainwoodpecker.viewer.panels.defaults import (
    _DEFAULT_DWELL_US,
    _DEFAULT_FOV_NM,
    _DEFAULT_RECORD_FRAME_COUNT,
    _DEFAULT_SCAN_SIZE_INDEX,
    _MAX_RECORD_FRAME_COUNT,
    _SCAN_SIZES,
)

if typing.TYPE_CHECKING:
    from miainwoodpecker.devices.interface import Scanner
    from miainwoodpecker.viewer.live import LiveInstrumentWidget


def build_scan_group(widget: LiveInstrumentWidget) -> QtWidgets.QGroupBox:
    """
    Build the Scan group: scan settings, live control and recording.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.

    Returns
    -------
    QtWidgets.QGroupBox
        The assembled group, for the caller to add to its layout.
    """
    # Only reached when there is a scanner: _build_ui skips this group
    # entirely for a detector-only instrument, so the widgets it
    # creates (_channel_combo, _scan_count_spin, _scan_status, ...) do
    # not exist in that case. Every method that touches them checks
    # _scanner rather than hasattr, because the scanner is the reason
    # they exist and is the honest thing to ask about.
    scanner = typing.cast("Scanner", widget._scanner)
    scan_group = QtWidgets.QGroupBox("Scan", widget)
    scan_form = QtWidgets.QFormLayout(scan_group)
    widget._channel_combo = QtWidgets.QComboBox(scan_group)
    widget._channel_combo.addItems(list(scanner.channel_names))
    scan_form.addRow("Channel", widget._channel_combo)
    widget._size_combo = QtWidgets.QComboBox(scan_group)
    widget._size_combo.addItems([str(size) for size in _SCAN_SIZES])
    widget._size_combo.setCurrentIndex(_DEFAULT_SCAN_SIZE_INDEX)
    scan_form.addRow("Size (px)", widget._size_combo)
    widget._dwell_spin = QtWidgets.QDoubleSpinBox(scan_group)
    widget._dwell_spin.setRange(0.1, 1000.0)
    widget._dwell_spin.setValue(_DEFAULT_DWELL_US)
    widget._dwell_spin.setSuffix(" µs")
    scan_form.addRow("Dwell", widget._dwell_spin)
    widget._fov_spin = QtWidgets.QDoubleSpinBox(scan_group)
    widget._fov_spin.setRange(0.1, 100000.0)
    widget._fov_spin.setValue(_DEFAULT_FOV_NM)
    widget._fov_spin.setSuffix(" nm")
    scan_form.addRow("FOV", widget._fov_spin)
    widget._scan_button = QtWidgets.QPushButton("Start scan", scan_group)
    scan_form.addRow(widget._scan_button)
    widget._scan_status = QtWidgets.QLabel("stopped", scan_group)
    scan_form.addRow("Status", widget._scan_status)
    (
        widget._scan_count_spin,
        widget._scan_save_button,
        widget._scan_record_button,
    ) = build_record_controls(scan_group, scan_form)

    widget._channel_combo.currentIndexChanged.connect(widget._on_scan_settings_changed)
    widget._size_combo.currentIndexChanged.connect(widget._on_scan_settings_changed)
    widget._dwell_spin.valueChanged.connect(widget._on_scan_settings_changed)
    widget._fov_spin.valueChanged.connect(widget._on_scan_settings_changed)
    widget._scan_button.clicked.connect(widget._toggle_scan)
    widget._scan_save_button.clicked.connect(widget.save_scan_frame)
    widget._scan_record_button.clicked.connect(widget.record_scan_frames)
    return scan_group

def build_record_controls(
    parent: QtWidgets.QWidget,
    form: QtWidgets.QFormLayout,
) -> tuple[QtWidgets.QSpinBox, QtWidgets.QPushButton, QtWidgets.QPushButton]:
    """
    Add "save displayed frame" and "record N frames" controls to a group.

    Shared by the scan and camera groups so both sources get the same
    two recording affordances without duplicating the widget setup.

    Parameters
    ----------
    parent : QtWidgets.QWidget
        The group box owning the new widgets.
    form : QtWidgets.QFormLayout
        The group's layout, appended to.

    Returns
    -------
    tuple[QtWidgets.QSpinBox, QtWidgets.QPushButton, QtWidgets.QPushButton]
        The frame-count spin box, the save button, and the record
        button, for the caller to connect.
    """
    save_button = QtWidgets.QPushButton("Save displayed frame", parent)
    form.addRow(save_button)
    count_spin = QtWidgets.QSpinBox(parent)
    count_spin.setRange(1, _MAX_RECORD_FRAME_COUNT)
    count_spin.setValue(_DEFAULT_RECORD_FRAME_COUNT)
    form.addRow("Frames", count_spin)
    record_button = QtWidgets.QPushButton("Record frames", parent)
    form.addRow(record_button)
    return count_spin, save_button, record_button


def build_camera_group(widget: LiveInstrumentWidget) -> QtWidgets.QGroupBox:
    """
    Build the Camera group: live control, recording and the analysis rows.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.

    Returns
    -------
    QtWidgets.QGroupBox
        The assembled group, for the caller to add to its layout.
    """
    camera_group = QtWidgets.QGroupBox("Camera", widget)
    camera_form = QtWidgets.QFormLayout(camera_group)
    widget._camera_button = QtWidgets.QPushButton("Start camera", camera_group)
    camera_form.addRow(widget._camera_button)
    widget._camera_status = QtWidgets.QLabel("stopped", camera_group)
    camera_form.addRow("Status", widget._camera_status)
    (
        widget._camera_count_spin,
        widget._camera_save_button,
        widget._camera_record_button,
    ) = build_record_controls(camera_group, camera_form)
    build_analysis_rows(widget, camera_group, camera_form)

    widget._camera_button.clicked.connect(widget._toggle_camera)
    widget._camera_save_button.clicked.connect(widget.save_camera_frame)
    widget._camera_record_button.clicked.connect(widget.record_camera_frames)
    return camera_group

def build_analysis_rows(
    widget: LiveInstrumentWidget,
    camera_group: QtWidgets.QGroupBox,
    camera_form: QtWidgets.QFormLayout,
) -> None:
    """
    Add a button per *installed* analysis extra, and name the rest.

    A button for a library that is not installed is a button that
    cannot work, and offering it teaches the operator that this
    application's buttons sometimes do nothing. So each one is built
    only when its extra is importable, and the extras that are not
    take a single summary row instead — which is more useful than
    three dead buttons, because it says what is installed as well as
    what is missing.

    The check is
    :func:`~miainwoodpecker.analysis.remote.target_available`, which
    resolves the module *spec* without executing it. Importing
    py4DSTEM to discover whether py4DSTEM is installed would stall
    building the window for seconds to answer a question with a cheap
    answer.

    This is a availability check, not a guarantee: a spec can resolve
    for a half-installed distribution whose import then fails. Each
    handler therefore keeps its own ``ImportError`` branch, and that
    branch is reachable rather than dead code.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.
    camera_group : QtWidgets.QGroupBox
        The group the widgets are parented to.
    camera_form : QtWidgets.QFormLayout
        The layout the rows are added to.
    """
    from miainwoodpecker.analysis.remote import target_available  # noqa: PLC0415
    from miainwoodpecker.analysis.transfer import (  # noqa: PLC0415
        ANALYSIS_TARGETS,
    )

    specifications = (
        ("hyperspy", "Analyze in HyperSpy", "Analysis"),
        ("libertem", "Sum in LiberTEM", "LiberTEM"),
        ("py4dstem", "Fit central disk (py4DSTEM)", "py4DSTEM"),
    )
    handlers = {
        "hyperspy": widget._analyze_camera_in_hyperspy,
        "libertem": widget._analyze_camera_in_libertem,
        "py4dstem": widget._fit_central_disk_in_py4dstem,
    }
    attributes = {
        "hyperspy": ("_analyze_button", "_analyze_status"),
        "libertem": ("_libertem_button", "_libertem_status"),
        "py4dstem": ("_py4dstem_button", "_py4dstem_status"),
    }
    for button_attribute, status_attribute in attributes.values():
        setattr(widget, button_attribute, None)
        setattr(widget, status_attribute, None)

    enabled: list[str] = []
    missing: list[str] = []
    for name, text, row_label in specifications:
        extra = ANALYSIS_TARGETS[name].extra
        if not target_available(name):
            missing.append(extra)
            continue
        enabled.append(extra)
        button_attribute, status_attribute = attributes[name]
        button = QtWidgets.QPushButton(text, camera_group)
        camera_form.addRow(button)
        status = QtWidgets.QLabel("", camera_group)
        camera_form.addRow(row_label, status)
        button.clicked.connect(handlers[name])
        setattr(widget, button_attribute, button)
        setattr(widget, status_attribute, status)

    if missing:
        camera_form.addRow(
            "Analysis extras",
            build_extras_summary(camera_group, enabled, missing),
        )

def build_extras_summary(
    camera_group: QtWidgets.QGroupBox,
    enabled: list[str],
    missing: list[str],
) -> QtWidgets.QLabel:
    """
    Describe which analysis extras are installed and which are not.

    Names both halves rather than only the missing one, because "no
    analysis buttons" and "analysis is installed but this build has
    no camera" look identical from the outside, and an operator
    deciding whether to install anything needs to see the whole set.

    Parameters
    ----------
    camera_group : QtWidgets.QGroupBox
        The group the label is parented to.
    enabled : list[str]
        Extras whose libraries are importable.
    missing : list[str]
        Extras whose libraries are not.

    Returns
    -------
    QtWidgets.QLabel
        A two-line summary, selectable so the install command can be
        copied out of it.
    """
    lines = [f"enabled: {', '.join(enabled)}" if enabled else "enabled: none"]
    lines.append(f"available: {', '.join(missing)}")
    lines.append(f"pip install \"miainwoodpecker[{','.join(missing)}]\"")
    label = QtWidgets.QLabel("\n".join(lines), camera_group)
    label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    label.setWordWrap(True)
    return label
