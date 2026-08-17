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

# Re-exported: this module used to define it, and the dock's top-level
# groups now fold with the same widget. See
# :mod:`miainwoodpecker.viewer.panels.sections`.
from miainwoodpecker.viewer.panels.sections import CollapsibleSection

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


def build_camera_group(
    widget: LiveInstrumentWidget,
    binding: object = None,
) -> QtWidgets.QGroupBox:
    """
    Build the Camera group: live control, recording and the analysis rows.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls; every
        ``_``-prefixed attribute set here belongs to it.
    binding : object
        The camera this group drives. Its ``button``, ``status``,
        ``count_spin``, ``save_button`` and ``record_button`` are filled
        in here, so each camera's controls act on **its own** device.

    Returns
    -------
    QtWidgets.QGroupBox
        The assembled group, for the caller to add to its layout.
    """
    camera_group = QtWidgets.QGroupBox("Camera", widget)
    camera_form = QtWidgets.QFormLayout(camera_group)
    name = binding.name
    binding.button = QtWidgets.QPushButton("Start camera", camera_group)
    camera_form.addRow(binding.button)
    binding.status = QtWidgets.QLabel("stopped", camera_group)
    camera_form.addRow("Status", binding.status)
    (
        binding.count_spin,
        binding.save_button,
        binding.record_button,
    ) = build_record_controls(camera_group, camera_form)

    # Each camera's controls name their own target, so the second
    # camera's Record button cannot start the first one's series.
    binding.button.clicked.connect(lambda *_, n=name: widget._toggle_camera(n))
    binding.save_button.clicked.connect(
        lambda *_, n=name: widget.save_camera_frame(n),
    )
    binding.record_button.clicked.connect(
        lambda *_, n=name: widget.record_camera_frames(n),
    )
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


def build_devices_panel(widget: LiveInstrumentWidget) -> QtWidgets.QGroupBox:
    """
    Build the Devices panel: one foldable section per device served.

    Built from what the instrument actually has rather than from a fixed
    list, which is the same rule ``_build_ui`` already followed for the
    scan group and is now worth stating: a detector-only instrument gets
    no Scan section, and gets no empty placeholder either.

    The first section opens and the rest fold, so a one-device
    instrument looks exactly as it did before this panel existed and a
    five-device one still fits on a screen.

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
    panel = QtWidgets.QGroupBox("Devices", widget)
    layout = QtWidgets.QVBoxLayout(panel)
    built: list[tuple[str, str, QtWidgets.QWidget]] = []
    if widget._scanner is not None:
        built.append(("scanner", "Scan", build_scan_group(widget)))
    titles = _camera_section_titles(widget._camera_bindings.values())
    for index, binding in enumerate(widget._camera_bindings.values()):
        group = build_camera_group(widget, binding)
        if index == 0:
            # The analysis buttons run against one camera - the first -
            # so they live in its section rather than being repeated in
            # every camera's, which would offer three buttons per camera
            # and no way to tell which burst you were about to take.
            build_analysis_rows(widget, group, group.layout())
        built.append((binding.name, titles[binding.name], group))

    widget._device_sections = {}
    for index, (key, title, content) in enumerate(built):
        # The section header names the device now, so the group box
        # inside it would otherwise say the same word twice.
        if isinstance(content, QtWidgets.QGroupBox):
            content.setTitle("")
        section = CollapsibleSection(title, content, panel, expanded=index == 0)
        widget._device_sections[key] = section
        layout.addWidget(section)
    return panel


def _camera_section_titles(bindings: typing.Iterable[object]) -> dict[str, str]:
    """
    Name each camera section after its device, keeping the names distinct.

    ``camera`` tells an operator nothing when there are two of them, so a
    section is titled with what the device calls itself. But an id is not
    guaranteed unique — two identical webcams, or two simulated cameras,
    report the same one — and two sections with the same header are worse
    than a slot number, because the operator cannot tell which is which.
    So a repeated id gets its target name appended, and a unique one is
    left clean.

    Parameters
    ----------
    bindings : typing.Iterable[object]
        The camera bindings to name.

    Returns
    -------
    dict[str, str]
        Target name to section title.
    """
    listed = list(bindings)
    identifiers = {
        binding.name: getattr(binding.camera, "camera_id", None) or binding.name
        for binding in listed
    }
    counts: dict[str, int] = {}
    for identifier in identifiers.values():
        counts[identifier] = counts.get(identifier, 0) + 1
    return {
        name: (
            f"Camera - {identifier} ({name})"
            if counts[identifier] > 1
            else f"Camera - {identifier}"
        )
        for name, identifier in identifiers.items()
    }
