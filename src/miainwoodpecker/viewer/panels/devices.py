"""
The device panels: the scan unit, the cameras, and their recording controls.

See :mod:`miainwoodpecker.viewer.panels.recordings` for why these are
builders taking the widget rather than methods on it.
"""

from __future__ import annotations

import typing

from qtpy import QtCore, QtWidgets

from miainwoodpecker.devices.interface import (
    READOUT_MODES,
    CameraParameters,
    SynchronisedScanner,
    axis_binning_values,
)
from miainwoodpecker.viewer import preferences, profiles
from miainwoodpecker.viewer.panels import settings, toolbar
from miainwoodpecker.viewer.panels.defaults import (
    _DEFAULT_FOV_NM,
    _DEFAULT_POSITIONS,
    _DEFAULT_RECORD_FRAME_COUNT,
    _DEFAULT_SCAN_SIZE_INDEX,
    _EXPOSURE_DECIMALS,
    _MAX_EXPOSURE_MS,
    _MAX_POSITIONS,
    _MAX_RECORD_FRAME_COUNT,
    _MIN_EXPOSURE_MS,
    _MIN_POSITIONS,
    _SCAN_SIZES,
)

# Re-exported: this module used to define it, and the dock's top-level
# groups now fold with the same widget. See
# :mod:`miainwoodpecker.viewer.panels.sections`.
from miainwoodpecker.viewer.panels.sections import CollapsibleSection
from miainwoodpecker.viewer.profiles import (
    PREVIEW,
    PROFILE_LABELS,
    PROFILE_TOOLTIPS,
)

if typing.TYPE_CHECKING:
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
    scan_group = QtWidgets.QGroupBox("Scan", widget)
    scan_form = QtWidgets.QFormLayout(scan_group)
    widget._scan_settings_dialog = settings.SettingsDialog(
        "Scan settings", scan_group
    )
    settings_form = widget._scan_settings_dialog.form

    widget._scan_button = toolbar.action_button(
        scan_group, toolbar.START, "Start the live scan (View profile)"
    )
    widget._preview_button = toolbar.action_button(
        scan_group, toolbar.PREVIEW, PROFILE_TOOLTIPS[PREVIEW]
    )
    widget._scan_image_button = toolbar.action_button(
        scan_group,
        toolbar.ACQUIRE,
        "Acquire one scan image. One pass of the probe, with every "
        "detector channel read out of it - the channels are registered "
        "to each other by construction",
    )
    widget._spectrum_image_button = toolbar.action_button(
        scan_group,
        toolbar.SPECTRUM_IMAGE,
        "Acquire a spectrum image: the whole readout of one detector "
        "kept at every beam position",
    )
    widget._scan_save_button = toolbar.action_button(
        scan_group,
        toolbar.SAVE,
        "Save the frame currently on screen, without touching the instrument",
    )
    widget._scan_record_button = toolbar.action_button(
        scan_group, toolbar.RECORD, "Record a series of scan frames to the session"
    )
    widget._scan_settings_button = toolbar.action_button(
        scan_group,
        toolbar.SETTINGS,
        "Dwell, resolution and the spectrum-image grid",
        on_click=widget._scan_settings_dialog.show_modal,
    )
    scan_form.addRow(
        toolbar.toolbar(
            scan_group,
            (
                widget._scan_button,
                widget._preview_button,
                widget._scan_image_button,
                widget._spectrum_image_button,
                widget._scan_save_button,
                widget._scan_record_button,
                widget._scan_settings_button,
            ),
        )
    )
    widget._scan_status = QtWidgets.QLabel("stopped", scan_group)
    scan_form.addRow("Status", widget._scan_status)

    # These two stay in the panel while dwell and resolution go behind
    # the gear, because they are the ones an operator changes while
    # looking at the image: which detectors to read, and where to look.
    build_detector_checks(widget, scan_group, scan_form)
    widget._fov_spin = QtWidgets.QDoubleSpinBox(scan_group)
    widget._fov_spin.setRange(0.1, 100000.0)
    widget._fov_spin.setValue(_DEFAULT_FOV_NM)
    widget._fov_spin.setSuffix(" nm")
    # Outside the profiles, because it is the one setting they share:
    # switching from checking focus to taking the picture must not move
    # the region the operator navigated to.
    scan_form.addRow("FOV (shared)", widget._fov_spin)

    dialog = widget._scan_settings_dialog
    build_profile_controls(widget, dialog, settings_form)
    build_spectrum_image_controls(widget, dialog, settings_form)
    widget._scan_count_spin = build_record_controls(dialog, settings_form)

    widget._fov_spin.valueChanged.connect(widget._on_scan_settings_changed)
    widget._scan_button.clicked.connect(widget._toggle_scan)
    widget._preview_button.clicked.connect(widget.preview_scan)
    widget._scan_image_button.clicked.connect(widget.acquire_scan_image)
    widget._spectrum_image_button.clicked.connect(widget.acquire_spectrum_image)
    widget._scan_save_button.clicked.connect(widget.save_scan_frame)
    widget._scan_record_button.clicked.connect(widget.record_scan_frames)
    return scan_group

def build_detector_checks(
    widget: LiveInstrumentWidget,
    scan_group: QtWidgets.QWidget,
    scan_form: QtWidgets.QFormLayout,
) -> None:
    """
    Add one checkbox per detector, restored from the last launch.

    Checkboxes rather than a combo box because a scanned instrument
    reads **several** detectors out of one pass as a matter of course —
    HAADF and MAADF arrive together, and on an EDX-fitted column the
    X-ray spectra come with them. A control that offers a choice of one
    describes serial acquisition, which is the special case, and it
    disagreed with what ``acquire_scan_image`` already did.

    Which are enabled follows the operator and the instrument rather
    than the shift, so it is remembered in the platform config directory
    rather than in the session — see
    :mod:`miainwoodpecker.viewer.preferences`.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls.
    scan_group : QtWidgets.QWidget
        The group box owning the new widgets.
    scan_form : QtWidgets.QFormLayout
        The group's layout, appended to.
    """
    names = list(widget.channel_names())
    enabled = set(preferences.stored_channels(widget._preferences, names))
    row = QtWidgets.QWidget(scan_group)
    layout = QtWidgets.QVBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    widget._channel_checks = {}
    for name in names:
        check = QtWidgets.QCheckBox(name, row)
        check.setChecked(name in enabled)
        check.toggled.connect(
            lambda _checked, bound=name: widget._on_channel_toggled(bound),
        )
        widget._channel_checks[name] = check
        layout.addWidget(check)
    scan_form.addRow("Detectors", row)


def build_profile_controls(
    widget: LiveInstrumentWidget,
    scan_group: QtWidgets.QWidget,
    scan_form: QtWidgets.QFormLayout,
) -> None:
    """
    Add a dwell and size control for each of the three scan profiles.

    All three visible at once rather than behind a selector, because the
    operator's question is "what will Preview do" as often as it is
    "change Preview" — and with three profiles a selector costs a click
    to answer a question a row of numbers answers at a glance.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls.
    scan_group : QtWidgets.QWidget
        The group box owning the new widgets.
    scan_form : QtWidgets.QFormLayout
        The group's layout, appended to.
    """
    stored = profiles.stored_profiles(widget._preferences)
    widget._profile_controls = {}
    for name in profiles.PROFILE_NAMES:
        settings = stored[name]
        row = QtWidgets.QWidget(scan_group)
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        dwell = QtWidgets.QDoubleSpinBox(row)
        dwell.setRange(0.1, 1000.0)
        dwell.setValue(settings.dwell_us)
        dwell.setSuffix(" µs")
        layout.addWidget(dwell)
        size = QtWidgets.QComboBox(row)
        size.addItems([str(value) for value in _SCAN_SIZES])
        size.setCurrentText(str(settings.size_px))
        if size.currentText() != str(settings.size_px):
            # A remembered size this build no longer offers: keep the
            # default rather than silently scanning at something the
            # operator did not choose.
            size.setCurrentIndex(_DEFAULT_SCAN_SIZE_INDEX)
        layout.addWidget(size)
        row.setToolTip(PROFILE_TOOLTIPS[name])
        scan_form.addRow(PROFILE_LABELS[name], row)
        widget._profile_controls[name] = (dwell, size)
        dwell.valueChanged.connect(widget._on_scan_settings_changed)
        size.currentIndexChanged.connect(widget._on_scan_settings_changed)


def build_spectrum_image_controls(
    widget: LiveInstrumentWidget,
    scan_group: QtWidgets.QWidget,
    scan_form: QtWidgets.QFormLayout,
) -> None:
    """
    Add the beam-position count and the spectrum-image button.

    Built unconditionally, even on a backend that cannot synchronise —
    which today is every backend but the preview. A button that is
    absent teaches an operator the feature does not exist; a button that
    explains *why* this instrument cannot do it teaches them something
    true, and it is the same explanation whether they are on usim or on
    a column whose trigger is not wired.

    Positions are square, and the count is small. Both are placeholders
    for the target-area UI: the aspect ratio should come from a region
    drawn on the reference scan rather than be assumed, and the grid
    should be as large as the operator's patience rather than as small
    as a blocking call can afford.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget that owns the resulting controls.
    scan_group : QtWidgets.QWidget
        The group box owning the new widgets.
    scan_form : QtWidgets.QFormLayout
        The group's layout, appended to.
    """
    widget._positions_spin = QtWidgets.QSpinBox(scan_group)
    widget._positions_spin.setRange(_MIN_POSITIONS, _MAX_POSITIONS)
    widget._positions_spin.setValue(_DEFAULT_POSITIONS)
    widget._positions_spin.setPrefix("")
    widget._positions_spin.setToolTip(
        "Beam positions per side. A whole detector readout is kept at "
        "each, so the dataset grows with the square of this number",
    )
    scan_form.addRow("Positions", widget._positions_spin)

    # Which detector is read out at each beam position. A combo box and
    # not checkboxes, unlike the scan channels above, because those are
    # channels of *one* device read out together and these are separate
    # devices with no shared trigger between them - reading two out of
    # one pass is the cross-device claim `ScanPass` exists to make and
    # that no adapter here can yet honour for more than one target.
    widget._sync_target_combo = QtWidgets.QComboBox(scan_group)
    widget._sync_target_combo.addItems(_synchronisable_targets(widget))
    widget._sync_target_combo.setToolTip(
        "The detector read out at every beam position. What it "
        "contributes depends on the readout mode it is in: an imaging "
        "detector gives a 4D diffraction cube, a projecting one gives a "
        "spectrum image",
    )
    scan_form.addRow("Per-position detector", widget._sync_target_combo)


def _synchronisable_targets(widget: LiveInstrumentWidget) -> list[str]:
    """
    Return the targets this widget's scan unit can read out during a pass.

    Empty on every backend that cannot synchronise, which is all of them
    but the preview — and the combo is built empty rather than hidden for
    the reason the button is built at all: a control that explains why
    this instrument cannot do something teaches an operator more than a
    control that is not there.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget whose scanner is asked.

    Returns
    -------
    list[str]
        Target names, in the order the scanner reports them.
    """
    scanner = widget._scanner
    if scanner is None or not isinstance(scanner, SynchronisedScanner):
        return []
    return list(scanner.synchronised_targets())


def build_record_controls(
    parent: QtWidgets.QWidget,
    form: QtWidgets.QFormLayout,
) -> QtWidgets.QSpinBox:
    """
    Add the "how many frames to record" setting to a settings form.

    Only the count, now: saving and recording are actions and live on
    the group's icon toolbar, while *how many* is a setting and belongs
    with the exposure. Shared by the scan and camera groups so both
    sources describe a series the same way.

    Parameters
    ----------
    parent : QtWidgets.QWidget
        The dialog owning the new widget.
    form : QtWidgets.QFormLayout
        The settings form, appended to.

    Returns
    -------
    QtWidgets.QSpinBox
        The frame-count spin box.
    """
    count_spin = QtWidgets.QSpinBox(parent)
    count_spin.setRange(1, _MAX_RECORD_FRAME_COUNT)
    count_spin.setValue(_DEFAULT_RECORD_FRAME_COUNT)
    count_spin.setToolTip(
        "How many frames the record button on the toolbar captures",
    )
    form.addRow("Frames to record", count_spin)
    return count_spin


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
    binding.settings_dialog = settings.SettingsDialog(
        f"Camera settings - {name}", camera_group
    )
    settings_form = binding.settings_dialog.form

    # The row the operator actually uses all day. Everything that used to
    # be a full-width labelled button is one glyph here, and everything
    # that used to be a form row is behind the last one.
    binding.button = toolbar.action_button(
        camera_group, toolbar.START, "Start the live camera view"
    )
    binding.acquire_button = toolbar.action_button(
        camera_group,
        toolbar.ACQUIRE,
        "Acquire one image at the settings in this camera's settings dialog",
    )
    binding.save_button = toolbar.action_button(
        camera_group,
        toolbar.SAVE,
        "Save the frame currently on screen, without touching the instrument",
    )
    binding.record_button = toolbar.action_button(
        camera_group, toolbar.RECORD, "Record a series of frames to the session"
    )
    binding.settings_button = toolbar.action_button(
        camera_group,
        toolbar.SETTINGS,
        "Exposure, binning and detector readout",
        on_click=binding.settings_dialog.show_modal,
    )
    camera_form.addRow(
        toolbar.toolbar(
            camera_group,
            (
                binding.button,
                binding.acquire_button,
                binding.save_button,
                binding.record_button,
                binding.settings_button,
            ),
        )
    )
    binding.status = QtWidgets.QLabel("stopped", camera_group)
    camera_form.addRow("Status", binding.status)
    build_image_controls(widget, camera_group, settings_form, binding)
    binding.count_spin = build_record_controls(camera_group, settings_form)

    # Each camera's controls name their own target, so the second
    # camera's Record button cannot start the first one's series.
    binding.button.clicked.connect(lambda *_, n=name: widget._toggle_camera(n))
    binding.save_button.clicked.connect(
        lambda *_, n=name: widget.save_camera_frame(n),
    )
    binding.record_button.clicked.connect(
        lambda *_, n=name: widget.record_camera_frames(n),
    )
    binding.acquire_button.clicked.connect(
        lambda *_, n=name: widget.acquire_camera_image(n),
    )
    binding.readout_combo.currentTextChanged.connect(
        lambda mode, n=name: widget.set_camera_readout(n, mode),
    )
    return camera_group


def build_image_controls(
    widget: LiveInstrumentWidget,  # noqa: ARG001 - kept for builder symmetry
    camera_group: QtWidgets.QWidget,
    camera_form: QtWidgets.QFormLayout,
    binding: object,
) -> None:
    """
    Add the exposure, binning and "Acquire image" controls for one camera.

    Separate settings from the live view, deliberately. The feed and the
    kept image are different jobs: the feed runs short and often binned
    so it stays responsive at sixty frames a second, and the image an
    operator keeps is worth a long unbinned exposure. One shared pair of
    settings would force a choice between a usable live view and a
    usable acquisition.

    Seeded from what the camera currently reports, so the defaults are
    the device's own rather than a guess this module makes about it.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget these controls belong to.
    camera_group : QtWidgets.QWidget
        The group box owning the new widgets.
    camera_form : QtWidgets.QFormLayout
        The group's layout, appended to.
    binding : object
        The camera binding whose ``exposure_spin``, ``binning_combo``
        and ``acquire_button`` are filled in here.
    """
    current = binding.camera.parameters()
    binding.exposure_spin = QtWidgets.QDoubleSpinBox(camera_group)
    binding.exposure_spin.setRange(_MIN_EXPOSURE_MS, _MAX_EXPOSURE_MS)
    binding.exposure_spin.setDecimals(_EXPOSURE_DECIMALS)
    binding.exposure_spin.setValue(current.exposure_ms)
    binding.exposure_spin.setSuffix(" ms")
    camera_form.addRow("Image exposure", binding.exposure_spin)

    # Still read off the camera handle rather than from the broker's
    # description, and knowingly: this grew a *per-axis* answer
    # (`binning_values_yx`) that `TargetDescription` does not carry yet,
    # and inventing that mapping here - untested, on a detector whose two
    # axes cost different things to bin - would be worse than the honest
    # gap. It is the one capability read the window has left.
    _add_binning_controls(binding, camera_group, camera_form, current)

    binding.readout_combo = QtWidgets.QComboBox(camera_group)
    # Offered on every camera, and refused by the ones that cannot do it.
    # There is no capability question to ask here - `Camera` has no
    # `readout_modes`, and widening a runtime_checkable protocol would
    # make every existing adapter fail a check it passes today, which is
    # the trap `Instrument` already documents. So the modes are offered
    # and `configure` answers, exactly as it does for a binning factor
    # the camera does not have.
    binding.readout_combo.addItems(list(READOUT_MODES))
    if current.readout in READOUT_MODES:
        binding.readout_combo.setCurrentIndex(READOUT_MODES.index(current.readout))
    binding.readout_combo.setToolTip(
        "'image' keeps the sensor's 2D frame. 'projected' sums the whole "
        "non-dispersive direction into a 1D spectrum, which is what a "
        "spectrometer is set to for a spectrum image - a camera with no "
        "dispersive direction refuses it.\n\n"
        "Applied to the device as soon as it is changed, unlike the "
        "exposure and binning above: this is a mode the detector is in, "
        "not a setting one acquisition uses",
    )
    camera_form.addRow("Detector readout", binding.readout_combo)

def _add_binning_controls(
    binding: object,
    camera_group: QtWidgets.QWidget,
    camera_form: QtWidgets.QFormLayout,
    current: CameraParameters,
) -> None:
    """
    Add one binning control, or two when the detector's axes differ.

    Offered from the camera's own values rather than a fixed list: a
    camera that only does 1x has no business showing a 4x it will
    refuse. Per axis when the detector says its axes differ, which on a
    spectrometer they do — binning rows trades dynamic range for
    signal-to-noise and is the routine move, while binning channels
    spends the spectral resolution the instrument exists to provide. Two
    settings with opposite costs are not one setting, and a single
    control would make the cheap one unreachable without paying the dear
    one.

    A detector that does not distinguish them keeps the single control it
    always had, because for it there is only one thing to choose.

    Parameters
    ----------
    binding : object
        The camera binding whose ``binning_combo`` — and, for a detector
        with separate axes, ``binning_across_combo`` — are filled in.
    camera_group : QtWidgets.QWidget
        The group box owning the new widgets.
    camera_form : QtWidgets.QFormLayout
        The group's layout, appended to.
    current : CameraParameters
        What the camera reports it is set to now.
    """
    down_values, across_values = axis_binning_values(binding.camera)
    current_down, current_across = current.binning_yx
    binding.binning_combo = _binning_combo(camera_group, down_values, current_down)
    if getattr(binding.camera, "binning_values_yx", None) is None:
        camera_form.addRow("Image binning", binding.binning_combo)
        return
    binding.binning_across_combo = _binning_combo(
        camera_group, across_values, current_across
    )
    camera_form.addRow("Binning (rows)", binding.binning_combo)
    camera_form.addRow("Binning (channels)", binding.binning_across_combo)
    binding.binning_across_combo.setToolTip(
        "Binning rows trades dynamic range for signal-to-noise. Binning "
        "channels spends energy resolution, so it is offered sparingly - "
        "this detector reports what it will take on each axis",
    )


def _binning_combo(
    parent: QtWidgets.QWidget,
    values: typing.Sequence[int],
    current: int,
) -> QtWidgets.QComboBox:
    """
    Build one binning drop-down, selecting the factor already in force.

    Parameters
    ----------
    parent : QtWidgets.QWidget
        The camera's group box.
    values : typing.Sequence[int]
        The factors this axis offers, ascending.
    current : int
        The factor currently applied to it.

    Returns
    -------
    QtWidgets.QComboBox
        The populated drop-down.
    """
    offered = list(values) or [1]
    combo = QtWidgets.QComboBox(parent)
    combo.addItems([str(value) for value in offered])
    if current in offered:
        combo.setCurrentIndex(offered.index(current))
    return combo


def build_analysis_rows(
    widget: LiveInstrumentWidget,
    camera_group: QtWidgets.QWidget,
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
    camera_group : QtWidgets.QWidget
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
    camera_group: QtWidgets.QWidget,
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
    camera_group : QtWidgets.QWidget
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
