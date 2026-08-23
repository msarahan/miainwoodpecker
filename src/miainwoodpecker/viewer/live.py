"""
Napari dock widget for the live "look at the sample, adjust settings" loop.

**This window is a client of a broker, not the owner of the instrument.**
:mod:`miainwoodpecker.broker` runs the live loops and decides who may
drive what; a single QTimer on the GUI thread asks it for the latest
frames at display rate and pushes them into napari image layers.
Acquisition rate and display rate are fully decoupled — a slow display
skips frames, and a fast source never floods the UI with events.

That the arbitration lives elsewhere is the point rather than a detail.
This file used to enforce "one driver per device" itself, in pieces: a
``stop_scan`` whose return value callers had to check, a stop dance
before every acquisition, and two "still busy - try again" strings. It
worked precisely as long as this window was the only program touching
the instrument, which stopped being true the moment a notebook or a
dashboard wanted the same microscope. The rule now lives in one place
that all of them share, and what is left here is a window.

**And the instrument may be in another process entirely.** Given a
broker and no devices, every control this window offers is built from
what the broker *describes* — detector names, binning factors per axis,
which controls the column implements, which cameras the scan unit can
read out, whether the scan unit has a fixed grid — and every value it
displays comes from a watch call. Nothing here reads a device to decide
what to offer, which is what makes the window on an operator's laptop
the same window as the one on the microscope PC rather than a cut-down
one. The visible consequence is that driving can be *refused*: a
control write takes a lease like everything else, and somebody else may
be holding it.

**A lease can take as long as a scan pass, so no lease is taken on the
GUI thread.** Stopping a live loop means waiting out the pass in flight,
and a pass is ``height x width x dwell`` — a quarter of a second at
512x512 and one microsecond, but 42 seconds at 2048x2048 and ten. Every
acquisition here therefore takes its lease *inside* the generator the
worker consumes, where waiting costs nothing but the wait. The two
paths that still block (Preview, and a spectrum image) blocked before
this change for the same reason and are marked as such.

The one deliberate exception is a control write, which leases the
``instrument`` target on the GUI thread — and can, because that target
runs no live loop, so there is no pass for the grant to wait out. It
waits half a second and then says who is driving; see
``_drive_instrument``.

Thread-safety contract: Qt widgets are only touched from the GUI thread.
Scan settings are handed to the broker, which publishes them to its own
worker — no Qt access from workers, and no shared mutable state between
this file and a running loop. Recording obeys the same contract from the
other direction: a
:class:`~miainwoodpecker.storage.session.RecordingJob` streams frames to
disk on its own thread and touches no Qt, and the GUI thread learns how
it is going by polling it from the same display timer that drives the
live view.

Recording is what makes this a viewer an operator can actually work
with rather than a live display: with a session attached
(:meth:`LiveInstrumentWidget.set_session`), the scan and camera groups
can keep the frame on screen or record a series of frames into the
session, and the three Phase 4 analysis buttons write their bursts into
the session too instead of a temporary file that is deleted on the way
out.

Reading back is the other half of that, and the half a parallel pilot
against Swift needs first: the Recordings group opens a file already on
disk — from this session or any path — into a napari layer, and can point
the three analysis buttons at that file instead of a fresh burst. Loading
runs on a :class:`~miainwoodpecker.storage.session.LoadJob` for the same
reason recording runs on a ``RecordingJob``: decompressing tens of
megabytes must not freeze the window. The two degraded files the migration
plan's Phase 3 interruption table measured are reported in words rather
than discovered as a traceback — an unfinalized recording displays but
will not analyze, and a hard-killed one does neither.

Analyzing an opened file reads it once, not twice. The load that displayed
it already decompressed every frame, so those frames — with the axis
calibration they were recorded with — are handed to the adapter directly
instead of the adapter being pointed at the path. At 2048x2048 that is the
difference between one 16.8MB-per-frame read and two.

Importing this module requires the ``viewer`` optional dependency group.
The camera group's "Analyze in HyperSpy", "Sum in LiberTEM", and "Fit
central disk (py4DSTEM)" buttons additionally need the ``analysis``,
``libertem``, and ``py4dstem`` groups respectively (migration plan,
Phase 4). All three libraries are imported lazily, so this module still
imports without them — and **each button is built only when its own
extra is installed**, with a single row naming the enabled and available
extras standing in for the ones that are not. A button that cannot work
is worse than an absent one: it teaches the operator that this
application's buttons sometimes do nothing.

Where the panels live
---------------------
This module assembles the window; each panel is built in its own module
under :mod:`miainwoodpecker.viewer.panels`, which is where they moved
when this file passed 1900 lines. They are **builders taking the
widget**, not methods on it and not widget subclasses, and that is the
whole point of the arrangement: the construction moved out, the *state*
did not. Every control is still an attribute of this class, so every
method here, and every test, reaches for it exactly where it always
did. A panel that owned its own children would have moved
``_analyze_status`` to ``_camera_panel._analyze_status`` and made a file
split into an API change.
"""

from __future__ import annotations

import contextlib
import dataclasses
import tempfile
import typing
from pathlib import Path

import numpy as np
from qtpy import QtCore, QtWidgets

from miainwoodpecker.acquisition.sequence import (
    camera_image,
    camera_series,
    multichannel_scan_series,
)
from miainwoodpecker.broker.interface import BrokerError, TargetDescription
from miainwoodpecker.broker.local import LocalBroker
from miainwoodpecker.devices.interface import (
    IMAGE_READOUT,
    CameraParameters,
    ScanParameters,
    ScanPass,
)
from miainwoodpecker.devices.rpc import (
    INSTRUMENT_TARGET,
    SCANNER_TARGET,
    target_kind,
)
from miainwoodpecker.storage.calibration import FrameCalibration
from miainwoodpecker.storage.nexus import write_frames
from miainwoodpecker.storage.session import (
    LoadJob,
    RecordingJob,
    RecordingReadError,
    Session,
    annotate,
    describe,
    estimate_size,
    find_recordings,
    format_bytes,
    free_space,
)
from miainwoodpecker.viewer import axes, preferences, profiles, progress
from miainwoodpecker.viewer import jobs as jobs_module
from miainwoodpecker.viewer.documents import ATTACHED_TO
from miainwoodpecker.viewer.jobs import AnalysisJob
from miainwoodpecker.viewer.panels import devices as devices_panel
from miainwoodpecker.viewer.panels import instrument as instrument_panel
from miainwoodpecker.viewer.panels import recordings as recordings_panel
from miainwoodpecker.viewer.panels import sections as sections_panel
from miainwoodpecker.viewer.panels import session as session_panel
from miainwoodpecker.viewer.panels import statusbar as statusbar_panel
from miainwoodpecker.viewer.panels import toolbar
from miainwoodpecker.viewer.panels.defaults import (
    _DEFAULT_DWELL_US,
    _DEFAULT_FOV_NM,
    _DEFAULT_SCAN_SIZE_INDEX,
    _NO_SESSION_MESSAGE,
    _SCAN_SIZES,
)

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    import napari

    from miainwoodpecker.analysis.operations import AnalysisInput
    from miainwoodpecker.analysis.remote import AnalysisRunner
    from miainwoodpecker.broker.interface import (
        InstrumentBroker,
        LeasedDevices,
        TargetState,
        TargetView,
    )
    from miainwoodpecker.devices.interface import (
        Camera,
        Frame,
        Instrument,
        InstrumentController,
        Scanner,
        SynchronisedScanner,
    )
    from miainwoodpecker.storage.nexus import FrameStack
    from miainwoodpecker.storage.session import LoadedRecording, Recording

# 60 fps, not the 30 this was. Measured end to end through the real
# display path - refresh_display, the document board, and napari - the
# whole thing costs 9.4 ms at 512^2, 9.7 ms at 1024^2 and 10.2 ms at
# 2048^2, so it already sustained ~100 fps and the old 33 ms timer was
# discarding two thirds of that.
#
# **Raising it is close to free when nothing is arriving**, which is the
# property that makes it safe on a slower machine than the one measured:
# a tick that finds the frame it already drew costs 4.4 microseconds
# (0.026% of a core at 60 Hz), because the identity check skips before
# any upload or contrast pass. The cost tracks frames actually produced,
# not ticks. Overridable per instance for a machine where that does not
# hold.
_DEFAULT_DISPLAY_INTERVAL_MS = 16
_ANALYSIS_BURST_FRAME_COUNT = 5
# Long enough that typing a sentence writes the sidecar once, short enough
# that an operator who types and immediately clicks Record has their note.
_NEXUS_FILE_FILTER = "NeXus recordings (*.nxs *.h5 *.hdf5);;All files (*)"
# What a detector's per-position readout may be, as ranks. One axis of
# counts is a spectrum; two are an image. Named rather than written as 1
# and 2 in the branch, because "len(shape) == 1" does not say which of
# the two kinds of dataset a pass is about to allocate.
_SPECTRUM_READOUT_RANK = 1
_IMAGE_READOUT_RANK = 2

# The two keys a stage position arrives under in the broker's control
# map. One control on the instrument, two numbers on the wire, because
# a mapping of scalars is what crosses a process boundary without a
# type of its own - see LocalBroker._refresh_controls.
_STAGE_Y_CONTROL = "stage_position_y_nm"
_STAGE_X_CONTROL = "stage_position_x_nm"

# How long a control write waits for the instrument target. Short,
# unlike the default: this is the one lease taken on the GUI thread, and
# the instrument runs no live loop for the wait to be about - so a wait
# here means another client is holding it, and saying so beats a window
# that stops repainting for five seconds every time somebody clicks
# Apply.
_CONTROL_LEASE_TIMEOUT_S = 0.5

# The kind of target this window builds a camera section for, as
# target_kind spells it. Cameras only, which is exactly what
# RemoteInstrumentDevices.cameras() yields for the in-process path -
# the two ways of finding detectors have to agree or a window would
# gain a section by being pointed at a broker.
_CAMERA_KIND = "camera"


@dataclasses.dataclass
class _CameraBinding:
    """
    One camera, with the controls and the live loop that drive it.

    An instrument can serve several cameras — a webcam and a USB
    microscope, a Ronchigram camera and an EELS camera — and each needs
    its own start/stop state, its own frame counter and its own napari
    layer. Grouping them here rather than in parallel dictionaries keeps
    "which loop belongs to which button" impossible to get wrong.

    Attributes
    ----------
    name : str
        The target name the server serves this camera on, which is how
        the broker is asked about it and how a lease names it. There is
        deliberately **no device handle here**: what the panel shows and
        what its buttons drive both go through the broker, so a binding
        holding one would be the one path by which a window could reach
        past the arbitration - and the one thing that cannot exist at
        all when the instrument is in another process.
    layer_name : str
        The napari layer its frames are pushed into. One layer per
        camera, so two live cameras do not overwrite each other.
    button : QtWidgets.QPushButton | None
        Start/stop, or None before the panel is built.
    status : QtWidgets.QLabel | None
        Its status line.
    count_spin : QtWidgets.QSpinBox | None
        How many frames its Record button records.
    save_button : QtWidgets.QPushButton | None
        Save the displayed frame.
    record_button : QtWidgets.QPushButton | None
        Record a series.
    exposure_spin : QtWidgets.QDoubleSpinBox | None
        Exposure for an *acquired image*, kept apart from whatever the
        live view is running at. The two are different jobs: the feed
        stays short to be responsive, and the image an operator keeps is
        worth waiting for.
    binning_combo : QtWidgets.QComboBox | None
        Binning for an acquired image, same reasoning as the exposure —
        a live view can afford to be binned where a kept image cannot.
        On a detector that bins both directions alike this is the whole
        setting; on one that does not, it is the *slow* axis.
    binning_across_combo : QtWidgets.QComboBox | None
        The fast axis's binning, built only for a detector that reports
        its axes separately — a spectrometer, where binning rows buys
        signal-to-noise and binning channels costs energy resolution.
        None means the camera has one binning setting, not two.
    readout_combo : QtWidgets.QComboBox | None
        Which readout mode the *device* is in — and unlike the two
        above, changing it configures the camera immediately. Readout is
        not a setting one acquisition uses; it decides the rank of every
        frame the detector produces, so a camera whose live view is 2D
        and whose acquisition is 1D would be a camera in two states at
        once.
    acquire_button : QtWidgets.QPushButton | None
        Take one image with those settings.
    """

    name: str
    layer_name: str
    button: QtWidgets.QPushButton | None = None
    status: QtWidgets.QLabel | None = None
    count_spin: QtWidgets.QSpinBox | None = None
    save_button: QtWidgets.QPushButton | None = None
    record_button: QtWidgets.QPushButton | None = None
    exposure_spin: QtWidgets.QDoubleSpinBox | None = None
    binning_combo: QtWidgets.QComboBox | None = None
    binning_across_combo: QtWidgets.QComboBox | None = None
    readout_combo: QtWidgets.QComboBox | None = None
    acquire_button: QtWidgets.QPushButton | None = None


@dataclasses.dataclass(frozen=True)
class _AnalysisInput:
    """
    The NeXus file an analysis button is about to run against.

    Attributes
    ----------
    path : Path
        The file to hand to the adapter.
    frame_count : int
        Frames in it, for the status message.
    origin : str
        Where it came from, in words, so the status line distinguishes a
        result computed from a fresh burst from one computed from a file.
    frames : FrameStack | None
        The same frames already in memory, when the operator is analyzing a
        recording this widget has just opened and displayed — in which case
        the adapters take these and the file is read once for the whole
        operation instead of twice. ``None`` means the adapter reads
        ``path`` itself, which is the fresh-burst case and any case where
        the in-memory copy is not demonstrably the whole file (see
        :meth:`_analysis_input`).
    """

    path: Path
    frame_count: int
    origin: str
    frames: FrameStack | None = None


@dataclasses.dataclass(frozen=True)
class _AnalysisOutcome:
    """
    What an analysis produced, carried back to the GUI thread for display.

    Attributes
    ----------
    payload : object
        Whatever the analysis computed. Opaque here on purpose: each button
        supplies its own display callable, because a mean projection, a sum
        projection, and a fitted disk do not render the same way.
    source : _AnalysisInput
        The file it was computed from, so the status line can say how many
        frames and where they came from.
    """

    payload: object
    source: _AnalysisInput


def _analysis_job_input(source: _AnalysisInput) -> AnalysisInput:
    """
    Restate what this widget resolved as what an analysis runner takes.

    A three-field copy rather than a shared type, because the two
    dataclasses answer different questions: ``_AnalysisInput`` also
    carries the frame count the *status line* needs, which is a display
    concern with no business crossing a process boundary. The conversion
    is one line and it is here rather than in each button so the three
    cannot drift.

    Parameters
    ----------
    source : _AnalysisInput
        What :meth:`LiveInstrumentWidget._analysis_input` produced.

    Returns
    -------
    AnalysisInput
        The same file-or-frames pair, plus the name a refusal should use.
    """
    from miainwoodpecker.analysis.operations import AnalysisInput  # noqa: PLC0415

    return AnalysisInput(
        path=str(source.path),
        frames=source.frames,
        origin=source.origin,
    )


def _condition(recording: Recording) -> str:
    """
    Describe a recording's state in the words an operator needs.

    The three states come straight from the migration plan's Phase 3
    interruption table, measured rather than reasoned about: a finalized
    file, a file whose writer was abandoned (all frames present, no
    ``/entry/data``), and a file whose process was killed outright (does not
    open at all).

    Parameters
    ----------
    recording : Recording
        The recording to describe.

    Returns
    -------
    str
        A short phrase for a combo entry or status label.
    """
    if not recording.readable:
        return "damaged - does not open"
    if recording.frame_count == 0:
        return "empty - no frames"
    if not recording.finalized:
        return f"{recording.frame_count} frames, unfinalized - viewable, not analyzable"
    return f"{recording.frame_count} frames"


def _analysis_refusal(recording: Recording) -> str | None:
    """
    Return why an analysis cannot run against this file, or None if it can.

    Parameters
    ----------
    recording : Recording
        The file the operator pointed the analysis buttons at.

    Returns
    -------
    str | None
        A sentence naming the problem, or None when the file is analyzable.
    """
    if not recording.readable:
        return (
            f"{recording.path.name} does not open as HDF5 at all - a "
            f"hard-killed acquisition leaves this, and its frames are gone"
        )
    if recording.frame_count == 0:
        return f"{recording.path.name} holds no frames"
    if not recording.finalized:
        return (
            f"{recording.path.name} was never finalized by its writer, so it "
            f"has no /entry/data group and the analysis adapters cannot read "
            f"it. Its {recording.frame_count} frames are intact - "
            f"'Open selected' displays them"
        )
    return None


class LiveInstrumentWidget(QtWidgets.QWidget):
    """
    Dock widget with live scan and/or camera view controls.

    A session is attached with :meth:`set_session` rather than passed
    here: where data goes is operator-editable state that can change
    during a run (a new directory for the afternoon's sample), not a
    construction-time dependency like the devices. Without a session the
    widget still works as a live display, and the recording controls say
    so instead of pretending to save anything.

    **Both devices are optional, and at least one is required.** A
    detector-only device server — a Direct Electron, DECTRIS or Hamamatsu
    camera driven through its own SDK, or the commodity USB camera server
    — has no scan unit, and a scan-only server has no camera. The Scan
    group is built only when there is a scanner to drive, so the absent
    device is missing from the window rather than present and broken.
    Everything downstream of the two groups (session, recordings,
    analysis) is shared and unconditional, because none of it is
    scan-specific: the analysis buttons already acquire from the camera.

    Parameters
    ----------
    viewer : napari.Viewer
        The napari viewer whose layers display the live frames.
    scanner : Scanner | None
        The scan device to drive, or None for a detector-only
        instrument — and None, too, when ``broker`` is one in another
        process, which is the case this window has no handles for at
        all. What it offers then comes from
        :meth:`~miainwoodpecker.broker.interface.InstrumentBroker.describe`
        rather than from the devices, and everything it drives goes
        through the broker either way.
    camera : Camera | None
        An optional camera to offer a live view for (e.g. Ronchigram, or
        a commodity USB camera). The one-entry case of ``cameras``, kept
        because the viewer, the scripts and every existing test use it.
    cameras : typing.Mapping[str, Camera] | None
        Every camera the instrument serves, by target name — a webcam
        *and* a USB microscope, or a Ronchigram *and* an EELS camera.
        Each gets its own section, its own live loop and its own napari
        layer, so two can run at once. The first is what a call that
        names no camera acts on.
    instrument : Instrument | None
        The instrument itself, for the Instrument panel: what this is
        connected to, and the controls it publishes. None gives a panel
        that says what it knows and offers no controls, which is what a
        caller constructing the widget without one has always had.
    broker : InstrumentBroker | None
        The arbitration to drive the instrument through, shared with
        whatever else is connected to it. None builds a private
        :class:`~miainwoodpecker.broker.local.LocalBroker` over the
        devices above, which is what a window that is the only program
        on the instrument wants and what every existing caller gets
        without changing a line.

        Passing one is how this window stops being the only client: a
        notebook, a dashboard and this dock can hold the same broker and
        take turns at the hardware instead of corrupting each other's
        frames. Pass one *without* devices - a
        :class:`~miainwoodpecker.broker.remote.RemoteBroker` - and the
        instrument is in another process entirely: the window is then
        made of what ``describe``, ``controls`` and ``camera_parameters``
        report, which is the whole of what it needs.
    display_interval_ms : int
        How often the display polls for new frames.
    parent : QtWidgets.QWidget | None
        Optional Qt parent widget.

    Raises
    ------
    ValueError
        If there is neither a scan unit nor a detector — given as
        handles, or described by the broker. There would be nothing to
        show and nothing to record, and every control would be disabled
        — a window worth refusing to build rather than opening empty.
    """

    def __init__(  # noqa: PLR0913 - all but viewer/scanner are keyword-only
        self,
        viewer: napari.Viewer,
        scanner: Scanner | None = None,
        *,
        camera: Camera | None = None,
        cameras: typing.Mapping[str, Camera] | None = None,
        instrument: Instrument | None = None,
        broker: InstrumentBroker | None = None,
        display_interval_ms: int = _DEFAULT_DISPLAY_INTERVAL_MS,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        served = dict(cameras) if cameras else {}
        if camera is not None and camera not in served.values():
            # The single-camera keyword stays, because the viewer, the
            # scripts and every existing test use it. It is the one-entry
            # case of the mapping rather than a separate path.
            served = {"camera": camera, **served}
        if broker is None and scanner is None and not served:
            msg = (
                "LiveInstrumentWidget needs a scanner, a camera, or both - "
                "an instrument with neither has nothing to display"
            )
            raise ValueError(msg)
        super().__init__(parent)
        self._viewer = viewer
        self._instrument_controls: dict[str, QtWidgets.QDoubleSpinBox] = {}
        self._instrument_stage_y: QtWidgets.QDoubleSpinBox | None = None
        self._instrument_stage_x: QtWidgets.QDoubleSpinBox | None = None
        self._instrument_blanker: QtWidgets.QCheckBox | None = None
        self._owns_broker = broker is None
        self._broker: InstrumentBroker = broker if broker is not None else LocalBroker(
            {
                **({SCANNER_TARGET: scanner} if scanner is not None else {}),
                **({INSTRUMENT_TARGET: instrument} if instrument is not None else {}),
                **served,
            },
            holder="viewer",
        )
        # What this instrument *has*, asked of the broker rather than of
        # the handles - because with a broker in another process there
        # are no handles, and asking the same question two ways would be
        # two answers to keep in step.
        #
        # **The handles are not kept.** Whatever a caller passed went
        # into the broker above and is reached from here only through a
        # lease; this object holds no device, which is what makes the
        # in-process window and the one across a socket the same window
        # rather than two that resemble each other.
        described = self._broker.describe()
        self._has_scanner = SCANNER_TARGET in described
        self._camera_bindings = self._bind_cameras(described, served)
        # Newest frame already pushed into each napari layer, so a display
        # tick that finds nothing new can skip the upload entirely. Holds
        # a reference for identity comparison only; the array itself is
        # the layer's, not a second copy.
        self._displayed: dict[str, Frame] = {}
        # Calibration each layer was last given, so a per-frame check can
        # tell "the field of view changed" from the far commoner "it did
        # not" without reassigning evented properties either way.
        self._calibrated: dict[str, FrameCalibration] = {}
        # Section title to section, so a caller - and a test - can ask
        # which devices the window offered and whether each is folded.
        self._device_sections: dict[str, devices_panel.CollapsibleSection] = {}
        # The dock's four top-level groups, by key, so a caller - and a
        # test - can ask which are folded. Same idea one level up.
        self._panel_sections: dict[str, sections_panel.CollapsibleSection] = {}
        self._session: Session | None = None
        self._recording_job: RecordingJob | None = None
        self._load_job: LoadJob | None = None
        self._analysis_job: AnalysisJob | None = None
        self._pass_job: jobs_module.PassJob | None = None
        self._pass_preview: dict[str, progress.PassPreview] = {}
        self._pass_target: str | None = None
        self._pass_path: Path | None = None
        self._pass_positions = ""
        # Set together with the job, and only read by _poll_analysis, so the
        # result of whichever button started it lands in that button's own
        # status label and layers. One job at a time is enforced in
        # _start_analysis; all three buttons share the device.
        self._analysis_status: QtWidgets.QLabel | None = None
        self._analysis_display: Callable[[object, _AnalysisInput], str] | None = None
        # One runner per analysis target, kept for the life of the widget.
        # Only the isolated runner has anything to keep - a worker process
        # whose 2.6-5.2s library import must not be paid per click - but
        # both are cached the same way so the two paths differ in nothing
        # but transport. Closed in shutdown().
        self._analysis_runners: dict[str, AnalysisRunner] = {}
        self._opened_file: Path | None = None
        # The frames of _opened_file, kept from the load that displayed
        # them so an analysis of that file does not read it again. Set and
        # read on the GUI thread only, and handed to the worker as plain
        # data by _start_analysis, exactly as _opened_file already is.
        self._opened_frames: FrameStack | None = None
        # Parameters, the enabled channel indices, and their names. A
        # tuple of channels rather than one, because a scanned
        # instrument reads several detectors out of a single pass and
        # the panel now says so.
        self._scan_request: tuple[ScanParameters, tuple[int, ...], tuple[str, ...]] = (
            ScanParameters(
                height=_SCAN_SIZES[_DEFAULT_SCAN_SIZE_INDEX],
                width=_SCAN_SIZES[_DEFAULT_SCAN_SIZE_INDEX],
                pixel_time_us=_DEFAULT_DWELL_US,
                fov_nm=_DEFAULT_FOV_NM,
            ),
            (0,),
            # Replaced by _on_scan_settings_changed below, before the
            # window is shown; the fallback is for the moment between.
            (self.channel_names() or ("",))[0],
        )
        # Before _build_ui: a failure part-way through building the UI
        # still leaves a widget whose shutdown() may be called.
        self._shutdown_done = False
        # Read once, before the panel is built, so the controls come
        # up already showing what the operator last chose.
        self._preferences = preferences.load()
        self._channel_checks: dict[str, QtWidgets.QCheckBox] = {}
        self._profile_controls: dict[
            str, tuple[QtWidgets.QDoubleSpinBox, QtWidgets.QComboBox]
        ] = {}
        self._build_ui()
        if scanner is not None:
            self._on_scan_settings_changed()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(display_interval_ms)
        self._timer.timeout.connect(self.refresh_display)

    @property
    def _camera_status(self) -> QtWidgets.QLabel | None:
        """The first camera's status line, kept for callers written before N."""
        binding = self._first_binding()
        return binding.status if binding is not None else None

    @property
    def _camera_button(self) -> QtWidgets.QPushButton | None:
        """The first camera's start/stop button, kept for pre-N callers."""
        binding = self._first_binding()
        return binding.button if binding is not None else None

    @property
    def _camera_count_spin(self) -> QtWidgets.QSpinBox | None:
        """The first camera's frame count, kept for pre-N callers."""
        binding = self._first_binding()
        return binding.count_spin if binding is not None else None

    @property
    def _camera_save_button(self) -> QtWidgets.QPushButton | None:
        """The first camera's save button, kept for pre-N callers."""
        binding = self._first_binding()
        return binding.save_button if binding is not None else None

    @property
    def _camera_record_button(self) -> QtWidgets.QPushButton | None:
        """The first camera's record button, kept for pre-N callers."""
        binding = self._first_binding()
        return binding.record_button if binding is not None else None

    def _bind_cameras(
        self,
        described: typing.Mapping[str, TargetDescription],
        served: typing.Mapping[str, Camera],
    ) -> dict[str, _CameraBinding]:
        """
        Build one binding per detector, from handles or from names alone.

        Parameters
        ----------
        described : typing.Mapping[str, TargetDescription]
            What the broker says each target is.
        served : typing.Mapping[str, Camera]
            The camera handles the caller passed, possibly none.

        Returns
        -------
        dict[str, _CameraBinding]
            Keyed by target name, in display order.

        Raises
        ------
        ValueError
            If this instrument has neither a scan unit nor a detector.
            An empty window with every control disabled is worse than a
            message saying why there is none.
        """
        names = self._camera_names(described, served)
        if not self._has_scanner and not names:
            msg = (
                "LiveInstrumentWidget needs a scan unit or a detector - the "
                "broker describes neither, so there is nothing to display"
            )
            raise ValueError(msg)
        return {
            name: _CameraBinding(
                name=name,
                layer_name="Camera" if index == 0 else f"Camera ({name})",
            )
            for index, name in enumerate(names)
        }

    @staticmethod
    def _camera_names(
        described: typing.Mapping[str, TargetDescription],
        served: typing.Mapping[str, Camera],
    ) -> tuple[str, ...]:
        """
        Return the detectors to build a section for, in display order.

        The handles' own order when a caller passed handles, because
        that order is a choice the caller made — the viewer puts the
        Ronchigram camera first, which is the one a call naming no
        camera acts on. Otherwise the broker's, which is the device
        server's, which is the same order for the same instrument.

        Parameters
        ----------
        described : typing.Mapping[str, TargetDescription]
            What the broker says each target is.
        served : typing.Mapping[str, Camera]
            The camera handles the caller passed, possibly none.

        Returns
        -------
        tuple[str, ...]
            Target names, empty on a scan-only instrument.
        """
        if served:
            return tuple(served)
        return tuple(
            name
            for name, description in described.items()
            if description.kind == _CAMERA_KIND
        )

    def _first_binding(self) -> _CameraBinding | None:
        """
        Return the camera a call that names none should act on.

        The first served, which on a one-camera instrument is the only
        one and is exactly what every existing caller meant.

        Returns
        -------
        _CameraBinding | None
            The first binding, or None on a scanner-only instrument.
        """
        return next(iter(self._camera_bindings.values()), None)

    def _binding(self, name: str | None) -> _CameraBinding | None:
        """
        Resolve a camera by target name, defaulting to the first.

        Parameters
        ----------
        name : str | None
            The target name, or None for the first camera.

        Returns
        -------
        _CameraBinding | None
            The binding, or None when there is no such camera.
        """
        if name is None:
            return self._first_binding()
        return self._camera_bindings.get(name)

    def refresh_instrument(self) -> None:
        """
        Re-read the instrument's identity and control values.

        On demand rather than on the display timer: four control reads
        at the display rate would put traffic on the wire to answer a question
        nobody asked. Called once when the panel is built, and whenever
        **Refresh** is pressed.

        A control that refuses to be read is reported in the status line
        and leaves its field alone, because a stale number an operator
        can see beats a zero they might act on.

        Read through the broker rather than off the instrument handle,
        which is what lets this panel exist in a window whose instrument
        is in another process. The broker reads the whole set in one go
        and only while nothing holds a lease, so a refusal now costs the
        whole reading rather than one field of it - the status line says
        which reading failed instead of which control did.
        """
        from miainwoodpecker.devices.interface import (  # noqa: PLC0415
            BEAM_BLANKER_CONTROL,
            STAGE_POSITION_CONTROL,
        )

        described = self._description(INSTRUMENT_TARGET)
        self._instrument_backend_label.setText(described.backend or "unknown")
        self._instrument_targets_label.setText(
            ", ".join(self._broker.describe()) or "no devices",
        )
        try:
            values = self._broker.controls()
        except Exception as error:  # noqa: BLE001 - any failure is a status line
            self._instrument_status.setText(f"could not read the controls: {error}")
            return
        missing: list[str] = []
        # The number rows are keyed by the control's own name, which is
        # the key the broker reports it under. The stage is the one that
        # is not - one control, two numbers - and is read below.
        for name, spin in self._instrument_controls.items():
            if name not in values:
                missing.append(name)
                continue
            spin.setValue(float(typing.cast("float", values[name])))
        if self._instrument_stage_y is not None:
            y_nm = values.get(_STAGE_Y_CONTROL)
            x_nm = values.get(_STAGE_X_CONTROL)
            if y_nm is None or x_nm is None:
                missing.append(STAGE_POSITION_CONTROL)
            else:
                self._instrument_stage_y.setValue(float(y_nm))
                typing.cast(
                    "QtWidgets.QDoubleSpinBox",
                    self._instrument_stage_x,
                ).setValue(float(x_nm))
        if self._instrument_blanker is not None:
            if BEAM_BLANKER_CONTROL not in values:
                missing.append(BEAM_BLANKER_CONTROL)
            else:
                self._instrument_blanker.setChecked(bool(values[BEAM_BLANKER_CONTROL]))
        self._instrument_status.setText(
            # A control the instrument said it had and then did not
            # report. Named rather than passed over: the field beside it
            # is showing a number nothing refreshed.
            "; ".join(f"{name}: not reported" for name in missing)
            if missing
            else "read",
        )

    def apply_instrument_control(self, name: str) -> None:
        """
        Send one control's field value to the instrument.

        No range check happens here, deliberately: limits belong behind
        the setters, where the hardware knows them
        (:class:`~miainwoodpecker.devices.interface.InstrumentController`).
        A refusal is shown rather than pre-empted, and the field keeps
        what the operator typed so it can be corrected rather than
        retyped.

        Parameters
        ----------
        name : str
            The control to apply, as ``available_controls`` reports it.
        """
        from miainwoodpecker.devices.interface import (  # noqa: PLC0415
            DEFOCUS_CONTROL,
            ENERGY_OFFSET_CONTROL,
            STAGE_POSITION_CONTROL,
        )

        def write(instrument: InstrumentController) -> None:
            """
            Send the field's value to one control.

            Parameters
            ----------
            instrument : InstrumentController
                The leased instrument.
            """
            if name == STAGE_POSITION_CONTROL:
                instrument.set_stage_position_nm(
                    self._instrument_stage_y.value(),
                    self._instrument_stage_x.value(),
                )
            elif name == DEFOCUS_CONTROL:
                instrument.set_defocus_nm(self._instrument_controls[name].value())
            elif name == ENERGY_OFFSET_CONTROL:
                instrument.set_energy_offset_ev(self._instrument_controls[name].value())

        if name not in self._description(INSTRUMENT_TARGET).controls:
            return  # pragma: no cover - only built controls have buttons
        if self._drive_instrument(write, refusal=f"{name} refused"):
            self._instrument_status.setText(f"{name} set")

    def _drive_instrument(
        self,
        write: Callable[[InstrumentController], None],
        *,
        refusal: str,
    ) -> bool:
        """
        Run one control write inside a lease on the instrument target.

        A lease, because writing a control *is* driving the instrument
        and this window is no longer the only thing that might be: a
        defocus set from here while a notebook sweeps the same control
        is the interleaving the broker exists to prevent, and the honest
        outcome of the collision is a refusal naming who holds it.

        Taken on the GUI thread, which everything else in this file is
        careful not to do, and the difference is the instrument target:
        it runs no live loop, so granting a lease on it stops nothing
        and waits for no pass. The wait this file's docstring warns
        about is a scan pass finishing, and there is none here.

        Parameters
        ----------
        write : Callable[[InstrumentController], None]
            What to do with the leased instrument.
        refusal : str
            How to introduce a failure in the status line - "defocus
            refused", "beam blanker refused".

        Returns
        -------
        bool
            True if the write went through. False means the status line
            already says why it did not.
        """
        if INSTRUMENT_TARGET not in self._broker.describe():
            return False
        try:
            with self._broker.lease(
                INSTRUMENT_TARGET,
                reason="setting a control",
                timeout_s=_CONTROL_LEASE_TIMEOUT_S,
            ) as leased:
                write(leased.instrument)
        # Both kinds of refusal read the same way in the status line and
        # want the same response from the operator: a BrokerError says
        # somebody else is driving, a device exception says the hardware
        # said no, and either way the value stays in the field to be
        # tried again.
        except Exception as error:  # noqa: BLE001 - the refusal is the message
            self._instrument_status.setText(f"{refusal}: {error}")
            return False
        return True

    def apply_beam_blanker(self, *, blanked: bool) -> None:
        """
        Blank or unblank the beam.

        The one control here that turns the beam off, which is why it is
        an explicit operator action with its own checkbox rather than
        something a limit elsewhere does as a side effect.

        Parameters
        ----------
        blanked : bool
            True to blank the beam, False to unblank it.
        """

        def write(instrument: InstrumentController) -> None:
            """
            Set the blanker on the leased instrument.

            Parameters
            ----------
            instrument : InstrumentController
                The leased instrument.
            """
            instrument.set_beam_blanked(blanked=blanked)

        if not self._drive_instrument(write, refusal="beam blanker refused"):
            # Put the box back where the hardware actually is - which
            # matters more here than for a number in a field, because
            # the checkbox is the operator's picture of whether the beam
            # is on the specimen.
            self.refresh_instrument()
            return
        self._instrument_status.setText("beam blanked" if blanked else "beam unblanked")

    @property
    def panel_sections(self) -> dict[str, sections_panel.CollapsibleSection]:
        """
        Return the dock's top-level folding sections, by key.

        Returns
        -------
        dict[str, sections_panel.CollapsibleSection]
            ``instrument``, ``recordings`` and ``devices``. Session
            context is a dialog rather than a section - see
            :meth:`open_session_settings`.
        """
        return self._panel_sections

    @property
    def session_dialog(self) -> QtWidgets.QDialog:
        """
        Return the Session settings dialog, whether or not it is showing.

        Returns
        -------
        QtWidgets.QDialog
            The dialog holding the session directory, operator, sample
            and standing notes.
        """
        return self._session_dialog

    def open_session_settings(self) -> None:
        """
        Show the Session settings dialog, raising it if already open.

        ``show`` rather than ``exec``: the dialog is application-modal
        either way, but ``exec`` would run a nested event loop and not
        return until it closed, blocking this method's caller.
        """
        self._session_dialog.show()
        self._session_dialog.raise_()
        self._session_dialog.activateWindow()

    def _build_ui(self) -> None:
        """
        Build the dock: four folding groups in a scroll area.

        Both halves of that are load-bearing, and neither substitutes
        for the other.

        The **scroll area** is the fix for a panel that could not be
        reached. The groups were a plain vertical stack whose minimum
        height was its natural height, so a stack wanting 1499 pixels on
        a 1409-pixel screen could not be shrunk, had nothing to scroll,
        and simply ran off the bottom - the lower sections were not just
        out of view but unreachable by any gesture.

        The **folding** is what an operator asked for and is a different
        job: putting away the groups they are not using. It is
        deliberately not relied on to make the panel fit, because
        several groups open at once is the ordinary case (watching a
        camera while a scan runs), and a layout that only fits when
        folded would put the same content out of reach again the moment
        someone opened it.
        """
        container = QtWidgets.QWidget(self)
        stack = QtWidgets.QVBoxLayout(container)
        stack.setContentsMargins(0, 0, 0, 0)
        # The session dialog is built first and kept hidden: the
        # recordings group's note field and half of live.py reach for
        # widgets it owns, so they have to exist before anything else
        # is assembled. See panels/session.py.
        self._session_dialog = session_panel.build_session_dialog(self)
        built = (
            ("instrument", instrument_panel.build_instrument_panel(self)),
            ("recordings", recordings_panel.build_recordings_group(self)),
            ("devices", devices_panel.build_devices_panel(self)),
        )
        for key, content in built:
            # The section header carries the title now, so the box
            # inside it would otherwise say the same word twice - the
            # same trick the per-device sections already use.
            title = content.title() if isinstance(content, QtWidgets.QGroupBox) else key
            if isinstance(content, QtWidgets.QGroupBox):
                content.setTitle("")
            section = sections_panel.CollapsibleSection(title, content, container)
            self._panel_sections[key] = section
            stack.addWidget(section)
        stack.addStretch(1)

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        # No frame: inside a dock the border reads as a second panel
        # edge a few pixels in from the real one.
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        # Not in this layout at all: where data is going and how much
        # room is left go into the main window's own status bar, beside
        # napari's "Ready", rather than into a second status line of
        # ours a few hundred pixels above it.
        self._status_widgets = statusbar_panel.build_status_widgets(self)
        self._status_bar_installed = statusbar_panel.install_status_bar(
            self, self._viewer,
        )

    def _on_scan_settings_changed(self) -> None:
        """
        Rebuild the live request from the View profile and the checkboxes.

        The *View* profile specifically: this is what the continuous
        loop runs at. Preview and Acquire are read at the moment their
        own action is taken, so changing them never disturbs a running
        live view.

        The broker is told immediately rather than at the next start, so
        a size or detector change reaches the scan that is *already*
        running - and reaches it without a stop, because stopping a scan
        to re-parameterise it parks the probe for as long as that takes.
        A change made while somebody holds a lease is reported rather
        than applied: the settings on screen would otherwise disagree
        with the acquisition in flight.
        """
        self._scan_request = (
            self.scan_parameters(profiles.VIEW),
            tuple(self.enabled_channels()),
            tuple(self.enabled_channel_names()),
        )
        if self._has_scanner:
            parameters, channels, _ = self._scan_request
            try:
                self._broker.reconfigure_live(
                    SCANNER_TARGET,
                    parameters,
                    channels=channels,
                )
            except BrokerError as error:
                self._scan_status.setText(str(error))
        self._save_scan_preferences()

    def scan_parameters(self, profile: str) -> ScanParameters:
        """
        Return the scan geometry for one profile over the shared field of view.

        The field of view is read from its own control rather than from
        the profile, which is the point of profiles: switching from
        checking focus to taking the picture must not move the region
        the operator navigated to.

        Parameters
        ----------
        profile : str
            One of :data:`~miainwoodpecker.viewer.profiles.PROFILE_NAMES`.

        Returns
        -------
        ScanParameters
            Geometry for that profile.
        """
        settings = self._profile_settings(profile)
        return ScanParameters(
            height=settings.size_px,
            width=settings.size_px,
            pixel_time_us=settings.dwell_us,
            fov_nm=self._fov_spin.value(),
        )

    def _profile_settings(self, profile: str) -> profiles.ScanProfile:
        """
        Read one profile's dwell and size off the panel.

        Parameters
        ----------
        profile : str
            The profile name.

        Returns
        -------
        profiles.ScanProfile
            What the panel currently says.
        """
        dwell_spin, size_combo = self._profile_controls[profile]
        return profiles.ScanProfile(
            dwell_us=dwell_spin.value(),
            size_px=int(size_combo.currentText()),
        )

    def enabled_channels(self) -> list[int]:
        """
        Return the indices of every detector the operator has enabled.

        Returns
        -------
        list[int]
            Channel indices, in the scanner's own order. Never empty
            while a scanner exists - see :meth:`_on_channel_toggled`.
        """
        names = list(self.channel_names())
        return [
            index
            for index, name in enumerate(names)
            if self._channel_checks[name].isChecked()
        ]

    def enabled_channel_names(self) -> list[str]:
        """
        Return the names of every detector the operator has enabled.

        Returns
        -------
        list[str]
            Channel names, in the scanner's own order.
        """
        names = list(self.channel_names())
        return [names[index] for index in self.enabled_channels()]

    def _on_channel_toggled(self, name: str) -> None:
        """
        Keep at least one detector enabled, then rebuild the request.

        Unchecking the last box is refused rather than allowed: a scan
        with no detector produces no data at all, so the state is not a
        preference an operator could mean. The box goes back on and the
        status line says why, which is more use than a silently dead
        Start button.

        Parameters
        ----------
        name : str
            The channel whose checkbox changed.
        """
        if not self.enabled_channels():
            self._channel_checks[name].setChecked(True)
            self._scan_status.setText(
                "at least one detector has to stay enabled - a scan with "
                "none reads nothing out",
            )
            return
        self._on_scan_settings_changed()
        self._rename_scan_layers()

    def _save_scan_preferences(self) -> None:
        """Remember the detector selection and profiles for the next launch."""
        if not self._has_scanner:
            return
        stored = dict(self._preferences)
        stored["scan_channels"] = self.enabled_channel_names()
        stored["scan_profiles"] = profiles.as_stored(
            {name: self._profile_settings(name) for name in profiles.PROFILE_NAMES},
        )
        self._preferences = stored
        preferences.save(stored)

    def _description(self, target: str) -> TargetDescription:
        """
        Return what a target is, or an empty description if it is absent.

        Empty rather than raising, because every caller is asking in
        order to decide what to offer, and "this instrument has no scan
        unit" is an ordinary answer rather than a fault.

        Parameters
        ----------
        target : str
            The target name.

        Returns
        -------
        TargetDescription
            Its description.
        """
        described = self._broker.describe().get(target)
        if described is not None:
            return described
        return TargetDescription(name=target, kind=target_kind(target), label=target)

    def channel_names(self) -> tuple[str, ...]:
        """
        Return the detectors the scan unit reads out, in channel order.

        From the broker's description rather than the scan unit itself.
        The answer is identical; where it comes from is not, and that is
        the point: a description crosses a process boundary and a device
        handle does not, so this is one of the reads that decides whether
        this window can be pointed at an instrument somewhere else.

        Returns
        -------
        tuple[str, ...]
            Channel names, empty for an instrument with no scan unit.
        """
        described = self._broker.describe().get(SCANNER_TARGET)
        return described.channel_names if described is not None else ()

    def _is_live(self, target: str) -> bool:
        """
        Return whether the broker is running a live loop on a target.

        Parameters
        ----------
        target : str
            The target name.

        Returns
        -------
        bool
            False for a target this instrument does not serve, so a
            caller can ask about a scanner that is not there.
        """
        state = self._broker.targets().get(target)
        return state is not None and state.is_live

    def _toggle_scan(self) -> None:
        if self._is_live(SCANNER_TARGET):
            self.stop_scan()
        else:
            self.start_scan()

    def _toggle_camera(self, name: str | None = None) -> None:
        binding = self._binding(name)
        if binding is None:
            return
        if self._is_live(binding.name):
            self.stop_camera(binding.name)
        else:
            self.start_camera(binding.name)

    def start_scan(self) -> None:
        """Start the live scan loop and the display timer. No-op with no scanner."""
        if not self._has_scanner:
            return
        parameters, channels, _ = self._scan_request
        try:
            self._broker.start_live(SCANNER_TARGET, parameters, channels=channels)
        except BrokerError as error:
            self._scan_status.setText(str(error))
            return
        toolbar.set_action(
            self._scan_button, toolbar.STOP, "Stop the live scan"
        )
        self._scan_status.setText("running")
        # Reversed, so the first enabled detector ends up on top of the
        # pile rather than under the ones raised after it.
        for channel in reversed(self.enabled_channel_names()):
            self._bring_to_front(self._scan_layer_name(channel))
        self._timer.start()

    def stop_scan(self) -> bool:
        """
        Stop the live scan loop.

        Rarely what a caller wants for its own sake: a stopped scan is a
        stationary probe. Acquisition paths do **not** call this — they
        take a lease, and the broker stops and restarts the loop around
        them.

        Returns
        -------
        bool
            True if the worker actually finished, False if a pass is
            still in flight. True with no scanner at all: nothing is
            holding the device.
        """
        if not self._has_scanner:
            return True
        try:
            stopped = self._broker.stop_live(SCANNER_TARGET)
        except BrokerError as error:
            self._scan_status.setText(str(error))
            return False
        if stopped:
            toolbar.set_action(
                self._scan_button,
                toolbar.START,
                "Start the live scan (View profile)",
            )
            self._scan_status.setText("stopped")
            self._maybe_stop_timer()
        else:
            self._scan_status.setText("still finishing a scan - try again")
        return stopped

    def start_camera(self, name: str | None = None) -> None:
        """
        Start a camera, its live loop, and the display timer.

        Parameters
        ----------
        name : str | None
            Which camera, by target name. None means the first served,
            which is what a one-camera instrument has always meant.
        """
        binding = self._binding(name)
        if binding is None:
            return
        try:
            self._broker.start_live(binding.name)
        except BrokerError as error:
            if binding.status is not None:
                binding.status.setText(str(error))
            return
        if binding.button is not None:
            toolbar.set_action(
                binding.button, toolbar.STOP, "Stop the live camera view"
            )
        if binding.status is not None:
            binding.status.setText("running")
        self._bring_to_front(binding.layer_name)
        self._timer.start()

    def stop_camera(self, name: str | None = None) -> bool:
        """
        Stop a camera's live loop and pause the camera.

        Parameters
        ----------
        name : str | None
            Which camera, by target name. None means the first served.

        Returns
        -------
        bool
            True if the worker actually finished. False means an exposure
            is still in flight and the camera is still in use; the camera
            is left running rather than stopped underneath it.
        """
        binding = self._binding(name)
        if binding is None:
            return True
        try:
            stopped = self._broker.stop_live(binding.name)
        except BrokerError as error:
            if binding.status is not None:
                binding.status.setText(str(error))
            return False
        if not stopped:
            if binding.status is not None:
                binding.status.setText("still finishing an exposure - try again")
            return False
        if binding.button is not None:
            toolbar.set_action(
                binding.button, toolbar.START, "Start the live camera view"
            )
        if binding.status is not None:
            binding.status.setText("stopped")
        self._maybe_stop_timer()
        return True

    def set_session(self, session: Session | None) -> None:
        """
        Attach (or detach) the session that recordings are written into.

        Parameters
        ----------
        session : Session | None
            The session to record into, or None to keep nothing.
        """
        self._session = session
        if session is not None:
            self._operator_edit.setText(session.operator)
            self._sample_edit.setText(session.sample)
            self._notes_edit.setPlainText(session.notes)
        self._refresh_session_labels()

    @property
    def session(self) -> Session | None:
        """Return the session recordings are written into, if any."""
        return self._session

    def change_session_directory(self) -> None:
        """Ask for a new session directory and switch to it (the button handler)."""
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Session directory", str(self._session.root) if self._session else ""
        )
        if chosen:
            self.open_session_directory(chosen)

    def open_session_directory(self, root: str | Path) -> None:
        """
        Point subsequent recordings at another directory mid-shift.

        An operator switching samples after lunch previously had to restart
        the app. This goes through :meth:`set_session`, the single path such
        a change has, so a switched-to directory behaves exactly like one
        named at launch: reused if it already exists, numbering resumed, and
        context loaded from its own ``session.json``.

        Deliberately no context is carried across: the new session shows
        whatever its own sidecar says, blank for a fresh directory. Carrying
        the current operator or sample over would *overwrite* the stored
        context of a directory being reopened, which is the one thing the
        session layer's "reuse, never clear" rule exists to prevent.

        A recording in flight blocks the switch rather than being cancelled
        or silently redirected. The job holds its own session reference and
        would keep writing correctly into the old directory, but the
        operator would then be watching a Session group describing a
        directory that is not receiving their file — and cancelling a live
        acquisition to change a setting is worse than being told to finish
        it. "Stop recording" is one button away.

        Parameters
        ----------
        root : str | Path
            The session directory to switch to; created if it does not exist.
        """
        if self._recording_job is not None and self._recording_job.is_running:
            self._recording_status.setText(
                "still recording - stop it before changing directory"
            )
            return
        try:
            session = Session(root)
        except OSError as exc:
            self._recording_status.setText(f"cannot use that directory: {exc}")
            return
        self.set_session(session)

    def _on_session_context_edited(self) -> None:
        """Persist edited operator/sample/notes into the session."""
        if self._session is None:
            return
        self._session.update_context(
            operator=self._operator_edit.text(),
            sample=self._sample_edit.text(),
            notes=self._notes_edit.toPlainText(),
        )

    def _note_for_next_recording(self) -> str | None:
        """
        Return the per-recording note to attach, or None if the field is empty.

        Deliberately not cleared after use: a focal series or a burst of
        repeats at one feature all want the same note, and the field is on
        screen the whole time, so a stale note is only possible for an
        operator ignoring the box they are looking at. Clearing it would
        instead silently drop the note off every recording after the first.
        """
        note = self._recording_note_edit.toPlainText().strip()
        return note or None

    def _refresh_session_labels(self) -> None:
        """
        Show where data is going and what has been recorded so far.

        The destination is written twice, to the dialog's label and to
        the status bar's button, because the two are read at different
        moments: the button is the answer to "am I recording anywhere?"
        at a glance, and the label is what someone reads when they have
        opened the settings to change it.
        """
        if self._session is None:
            self._session_path_label.setText(_NO_SESSION_MESSAGE)
            statusbar_panel.set_destination(self, None)
            self._recorded_label.setText("nothing recorded yet")
            self._refresh_recording_choices([])
            self._refresh_space_label()
            return
        self._session_path_label.setText(str(self._session.root))
        statusbar_panel.set_destination(self, str(self._session.root))
        recordings = self._session.recordings()
        # The combo can show either scope; the labels below always describe
        # this session, because that is where the next recording goes.
        if self._all_sessions_check.isChecked():
            # find_recordings answers newest-first; the combo wants
            # acquisition order, because it preselects the last entry.
            self._refresh_recording_choices(
                list(reversed(find_recordings(self._session.root.parent))),
                qualified=True,
            )
        else:
            self._refresh_recording_choices(recordings)
        self._refresh_space_label()
        if not recordings:
            self._recorded_label.setText("nothing recorded yet")
            return
        latest = recordings[-1]
        self._recorded_label.setText(
            f"{len(recordings)} file(s) - latest: {latest.path.name} "
            f"({latest.frame_count} frames)"
        )

    def _refresh_recording_choices(
        self, recordings: Sequence[Recording], *, qualified: bool = False
    ) -> None:
        """
        Repopulate the Recordings combo, keeping the operator's choice if it survives.

        Each entry says what the file is *now*, including the two degraded
        states the migration plan's Phase 3 interruption table measured, so
        an operator sees why a file will not analyze before clicking rather
        than after.

        The newest recording is preselected when the operator has not chosen
        otherwise, because "open the file I just took" is the request this
        exists for. Note a combo with placeholder text keeps
        ``currentIndex == -1`` after the first ``addItem``, so the selection
        has to be set rather than assumed.

        Parameters
        ----------
        recordings : Sequence[Recording]
            The recordings to offer, in acquisition order.
        qualified : bool
            Whether to prefix each entry with its session directory. True
            when listing across sessions, where the filename alone is
            ambiguous: per-session numbering restarts at ``0001``, so two
            sessions each hold a ``0001-scan-haadf-...`` and only the
            directory tells them apart.
        """
        previous = self._recording_combo.currentData()
        self._recording_combo.clear()
        for recording in recordings:
            name = (
                f"{recording.path.parent.name}/{recording.path.name}"
                if qualified
                else recording.path.name
            )
            self._recording_combo.addItem(
                f"{name} - {_condition(recording)}",
                str(recording.path),
            )
        restored = -1 if previous is None else self._recording_combo.findData(previous)
        if restored < 0:
            restored = self._recording_combo.count() - 1
        self._recording_combo.setCurrentIndex(restored)

    def annotate_opened_recording(self) -> None:
        """
        Add a note to the recording currently open (the button handler).

        Deliberately acts on the *opened* recording rather than the combo
        selection: an operator annotates what they are looking at, and
        making the target the thing on screen removes the way to annotate
        the wrong file by leaving the combo on something else.

        The field is cleared only on success, so a note refused for being
        blank, or lost to an unwritable file, is still on screen to retry
        or copy out.
        """
        if self._opened_file is None:
            self._load_status.setText("open a recording before annotating it")
            return
        try:
            annotate(self._opened_file, self._annotation_edit.text())
        except RecordingReadError as exc:
            self._load_status.setText(str(exc))
            return
        self._annotation_edit.clear()
        self._load_status.setText(f"note added to {self._opened_file.name}")

    def _refresh_space_label(self) -> None:
        """Report free space where recordings are being written."""
        if self._session is None:
            statusbar_panel.set_free_space(self, "")
            return
        free = free_space(self._session.root)
        text = f"{format_bytes(free)} free"
        plan = self._planned_recording_size()
        if free and plan is not None:
            frames, planned, source = plan
            if planned > free:
                # An upper bound compared against the real number: erring
                # high here means warning slightly early, which is the
                # right way to be wrong about running out of disk
                # mid-acquisition.
                text += (
                    f" - warning: {frames} {source} frames need up to "
                    f"{format_bytes(planned)}"
                )
        statusbar_panel.set_free_space(self, text)

    def _planned_recording_size(self) -> tuple[int, int, str] | None:
        """
        Estimate the largest recording the current controls would write.

        Returns
        -------
        tuple[int, int, str] | None
            Frame count, upper-bound byte size, and what to call the
            source, or None when no size can be estimated honestly.

        Notes
        -----
        A scan's size is known before anything is acquired, because the
        operator sets it: the Size combo *is* the frame shape. A camera's
        is not — the sensor decides, and a commodity USB camera's
        resolution is whatever the driver negotiated. So with no scanner
        the estimate waits for a frame to arrive and then uses its real
        shape, and stays silent until then. Inventing a shape to warn
        about would put a number on screen that no acquisition would
        produce, which is worse than saying nothing: the free-space
        figure beside it is still true.
        """
        if self._has_scanner:
            frames = self._scan_count_spin.value()
            return frames, estimate_size(self._scan_request[0].shape, frames), "scan"
        first = self._first_binding()
        if first is None:  # pragma: no cover - one device is required
            return None
        frame = self._broker.latest(first.name)
        if frame is None:
            return None
        binding = self._first_binding()
        if binding is None or binding.count_spin is None:
            return None
        frames = binding.count_spin.value()
        return frames, estimate_size(frame.data.shape, frames), "camera"

    def save_scan_frame(self) -> None:
        """Save the scan frame currently on screen into the session."""
        if not self._has_scanner:
            return
        label = f"scan-{self._scan_request[2][0]}-frame"
        self._save_displayed_frame(SCANNER_TARGET, label)

    def save_camera_frame(self, name: str | None = None) -> None:
        """
        Save the camera frame currently on screen into the session.

        Parameters
        ----------
        name : str | None
            Which camera, by target name. None means the first served.
        """
        binding = self._binding(name)
        if binding is None:
            return
        self._save_displayed_frame(binding.name, "camera-frame")

    def _save_displayed_frame(self, target: str, label: str) -> None:
        """
        Record the newest frame from a live loop as a one-frame file.

        Takes no lease, deliberately, and this is the one acquisition
        button that should not: the frame is already in hand from the
        broker's own loop, so there is no device to claim. The operator
        keeps looking at the sample while it is written.

        Parameters
        ----------
        target : str
            The target whose displayed frame to keep.
        label : str
            Session label for the one-frame recording.
        """
        frame = self._broker.latest(target)
        if frame is None:
            self._recording_status.setText("no frame on screen yet")
            return
        self._start_recording([frame], label)

    def record_scan_frames(self) -> None:
        """Record the requested number of scan frames into the session."""
        if not self._has_scanner:
            return
        if self._session is None:
            self._recording_status.setText(_NO_SESSION_MESSAGE)
            return
        # Acquire settings, not the live view's: a kept series is worth
        # the dwell of a kept image. Every enabled channel, one pass per
        # repeat, so the series is multichannel for the same reason a
        # scan image is.
        parameters = self.scan_parameters(profiles.ACQUIRE)
        channels = list(self._scan_request[1])
        names = self._scan_request[2]
        count = self._scan_count_spin.value()
        self._start_recording(
            self._leased_frames(
                [SCANNER_TARGET],
                f"recording {count} scan frames",
                lambda leased: multichannel_scan_series(
                    leased.scanner(),
                    parameters,
                    count,
                    channels=channels,
                ),
            ),
            f"scan-{'-'.join(names)}",
        )

    def record_camera_frames(self, name: str | None = None) -> None:
        """
        Record the requested number of camera frames into the session.

        Parameters
        ----------
        name : str | None
            Which camera, by target name. None means the first served.
        """
        binding = self._binding(name)
        if binding is None or binding.count_spin is None:
            return
        if self._session is None:
            self._recording_status.setText(_NO_SESSION_MESSAGE)
            return
        count = binding.count_spin.value()
        self._start_recording(
            self._leased_frames(
                [binding.name],
                f"recording {count} camera frames",
                lambda leased: camera_series(leased.camera(binding.name), count),
            ),
            "camera",
        )

    def preview_scan(self) -> None:
        """
        Take one scan at the Preview profile and show it, saving nothing.

        The focus check. Preview sits between the live view and an
        acquisition precisely so an operator can judge focus and
        astigmatism by eye at a signal-to-noise the live view cannot
        reach, without paying for a kept image — and it records nothing
        on purpose, because a focus check that littered the session with
        files would stop being used.

        Every enabled detector, from one pass, like everything else here.
        """
        if not self._has_scanner:
            return
        parameters = self.scan_parameters(profiles.PREVIEW)
        channels = list(self._scan_request[1])
        names = self._scan_request[2]
        self._scan_status.setText("previewing...")
        try:
            with self._broker.lease(SCANNER_TARGET, reason="preview") as leased:
                frames = leased.scanner().scan_frames(parameters, channels)
        except Exception as error:  # noqa: BLE001 - the refusal is the message
            self._scan_status.setText(f"preview failed: {error}")
            return
        for frame, name in zip(frames, names, strict=False):
            self._show_frame(
                frame,
                layer_name=self._scan_layer_name(name),
                autocontrast_every_frame=True,
            )
        self._scan_status.setText(
            f"preview: {parameters.width} px at {parameters.pixel_time_us:g} us "
            "(not saved)",
        )

    def acquire_scan_image(self) -> None:
        """
        Acquire one scan image: a single pass, every channel at once.

        The everyday scan acquisition, and a different thing from
        "record N frames" beside it. That one is a *time* series — the
        same channel scanned repeatedly, for drift or for averaging.
        This is one pass of the probe, with every detector the scanner
        has read out of it.

        Every channel rather than the one on display, because the pass
        happens either way: reading ADF while you were going to scan for
        HAADF anyway costs no extra dose and no extra time, and the two
        images are then registered to each other by construction. The
        cost of *not* doing it is a second pass over the same area to
        get the channel you wish you had kept.
        """
        if not self._has_scanner:
            return
        if self._session is None:
            self._recording_status.setText(_NO_SESSION_MESSAGE)
            return
        # The Acquire profile, not the live view's settings: this is the
        # image that gets kept, and it is worth the longer dwell.
        parameters = self.scan_parameters(profiles.ACQUIRE)
        channels = list(self._scan_request[1])
        self._start_recording(
            self._leased_frames(
                [SCANNER_TARGET],
                "acquiring a scan image",
                lambda leased: multichannel_scan_series(
                    leased.scanner(),
                    parameters,
                    1,
                    channels=channels,
                ),
            ),
            "scan-image",
        )

    def acquire_camera_image(self, name: str | None = None) -> None:
        """
        Acquire one camera image, with its own exposure and binning.

        The snapshot an operator keeps — a Ronchigram for the record, a
        diffraction pattern to measure — taken at settings chosen for
        the image rather than the ones the live view happens to be
        running. The live settings are restored afterwards by
        :func:`~miainwoodpecker.acquisition.camera_image`, so taking one
        long exposure does not leave the feed crawling.

        Parameters
        ----------
        name : str | None
            Which camera, by target name. None means the first served.
        """
        binding = self._binding(name)
        if binding is None or binding.exposure_spin is None:
            return
        if self._session is None:
            self._recording_status.setText(_NO_SESSION_MESSAGE)
            return
        parameters = self._image_parameters(binding)
        self._start_recording(
            self._leased_frames(
                [binding.name],
                "acquiring a camera image",
                lambda leased: camera_image(
                    leased.camera(binding.name),
                    parameters,
                ),
            ),
            "camera-image",
        )

    def _image_parameters(self, binding: _CameraBinding) -> CameraParameters:
        """
        Return the settings an acquired image should be taken with.

        Read off the panel at the moment of acquisition rather than
        cached, so what an operator typed is what the exposure uses.

        Parameters
        ----------
        binding : _CameraBinding
            The camera and its controls.

        Returns
        -------
        CameraParameters
            Exposure and binning for one acquired image, keeping the
            camera's current readout mode.
        """
        exposure_spin = typing.cast("QtWidgets.QDoubleSpinBox", binding.exposure_spin)
        binning_combo = typing.cast("QtWidgets.QComboBox", binding.binning_combo)
        down = int(binning_combo.currentText())
        across_combo = binding.binning_across_combo
        # A scalar when the camera has one binning control, so a detector
        # that never learned about per-axis binning is still handed the
        # spelling it validates - see interface.validate_binning, which
        # refuses an asymmetric pair to any camera that has not said it
        # can tell its axes apart.
        binning: int | tuple[int, int] = (
            down
            if across_combo is None
            else (down, int(across_combo.currentText()))
        )
        return CameraParameters(
            exposure_ms=exposure_spin.value(),
            binning=binning,
            # Not from the panel's readout row: that row configures the
            # device the moment it changes (see set_camera_readout), so
            # the camera's own answer is already what the operator chose
            # — and an image acquisition is not the place to switch a
            # spectrometer between imaging and projecting. Asked of the
            # broker, which reads it from the detector, so the answer is
            # the device's whichever process it is in.
            readout=self._current_readout(binding),
        )

    def _current_readout(self, binding: _CameraBinding) -> str:
        """
        Return the readout mode a detector is in right now.

        Parameters
        ----------
        binding : _CameraBinding
            The camera to ask about.

        Returns
        -------
        str
            One of :data:`~miainwoodpecker.devices.interface.READOUT_MODES`.
            The default for a detector that reports no settings at all,
            which is what a camera with nothing to say about itself has
            always been treated as.
        """
        current = self._broker.camera_parameters(binding.name)
        return current.readout if current is not None else IMAGE_READOUT

    def set_camera_readout(self, name: str, readout: str) -> None:
        """
        Put one camera into a readout mode, or say why it will not go.

        Applied immediately, unlike the exposure and binning beside it in
        the panel. Those describe *an acquisition*; this describes the
        **device**, and it decides the rank of every frame the detector
        produces — so a camera whose live view was imaging while its next
        acquisition projected would be a camera in two states at once.

        Refused while the camera is running, and that is about the
        display rather than the device: the interface explicitly allows
        ``configure`` on a started camera, but the frame after this one
        would arrive with a different number of axes than the napari
        layer showing it, and there is no rank a single image layer can
        hold both ways. Stopping first is one click and leaves nothing
        ambiguous.

        A camera with no dispersive direction refuses a projected readout
        outright (its ``configure`` raises), which is the case this
        control exists to make reachable: the refusal is the honest
        answer, and an operator who never sees it learns nothing about
        why their Ronchigram camera is not a spectrometer.

        Parameters
        ----------
        name : str
            Which camera, by target name.
        readout : str
            The requested mode, from
            :data:`~miainwoodpecker.devices.interface.READOUT_MODES`.
        """
        binding = self._binding(name)
        if binding is None or binding.readout_combo is None:
            return
        current = self._broker.camera_parameters(binding.name)
        if current is None or readout == current.readout:
            return
        status = binding.status
        if self._is_live(binding.name):
            self._show_camera_readout(binding, current.readout)
            if status is not None:
                status.setText(
                    "stop the camera before changing its readout - the live "
                    "layer cannot change rank underneath itself",
                )
            return
        try:
            # Under a lease, because configuring a detector is driving
            # it. The wait this costs is bounded by the refusal above:
            # the loop is already stopped, so there is no pass to finish
            # and nothing for the grant to wait out.
            with self._broker.lease(
                binding.name,
                reason="setting the readout mode",
                timeout_s=_CONTROL_LEASE_TIMEOUT_S,
            ) as leased:
                leased.camera(binding.name).configure(
                    dataclasses.replace(current, readout=readout),
                )
        except Exception as error:  # noqa: BLE001 - the refusal is the message
            self._show_camera_readout(binding, current.readout)
            if status is not None:
                status.setText(f"readout refused: {error}")
            return
        # Shown from what the device answered, not from what was asked
        # for, and done on success as well as on refusal: this method is
        # also how a script drives the mode, and then the combo has not
        # moved at all. A panel showing a mode the detector is not in is
        # the same lie whichever direction it drifted.
        taken = self._current_readout(binding)
        self._show_camera_readout(binding, taken)
        if status is not None:
            status.setText(f"readout: {taken}")

    @staticmethod
    def _show_camera_readout(binding: _CameraBinding, readout: str) -> None:
        """
        Put the combo back to what the device actually took.

        Without the signal blocked this would re-enter
        :meth:`set_camera_readout` with the old value, which is harmless
        but writes a second status line over the message explaining the
        refusal — so the operator would see the combo revert and no
        reason for it.

        Parameters
        ----------
        binding : _CameraBinding
            The camera whose combo is being corrected.
        readout : str
            The mode the device is in.
        """
        combo = binding.readout_combo
        if combo is None:  # pragma: no cover - guarded by the caller
            return
        blocked = combo.blockSignals(True)  # noqa: FBT003 - Qt's own signature
        try:
            combo.setCurrentText(readout)
        finally:
            combo.blockSignals(blocked)

    def acquire_spectrum_image(self) -> None:
        """
        Acquire one synchronised pass over the current field of view.

        One traversal of the probe over a grid of beam positions, with
        the selected detector's whole readout kept at each and every scan
        channel read out alongside it — see
        :class:`~miainwoodpecker.devices.interface.ScanPass`.

        **What lands on disk is decided by the detector's readout mode,
        not by this button.** A detector left imaging contributes a 4D
        diffraction cube (the 4D-STEM case); a spectrometer projecting
        its non-dispersive direction contributes a rank-3 spectrum image
        (the EELS case). That is the same thing real hardware does — a
        spectrum image is acquired with the spectrometer summing — and it
        is why one action covers both rather than two actions differing
        in which device they name.

        **Most of this method is the refusal, and that is the point.**
        Synchronised acquisition is a hardware fact, not a software
        feature: the column has to drive the detector's trigger or the
        detector has to advance the scan. A backend without that wiring
        cannot do it, and the nionswift-usim simulator is exactly such a
        backend — measured, in
        :mod:`miainwoodpecker.analysis.py4dstem_bridge`, which found that
        moving the simulator's own probe position changes nothing beyond
        shot noise. Producing a plausible cube anyway would be the worst
        available outcome: it is the same shape as a real one, and every
        number computed per pixel from it would be computed against a
        position nothing established.

        Blocking, for now. The whole pass runs on the GUI thread, which
        is tolerable for the preview's small grids and is not what a real
        acquisition needs; moving it behind a job like
        :class:`~miainwoodpecker.storage.session.RecordingJob` is the
        next step, and is why the grid offered here is deliberately small.
        """
        from miainwoodpecker.storage.passes import PassWriter  # noqa: PLC0415

        if not self._has_scanner:
            return
        target = self._spectrum_image_target()
        if target is None:
            return
        # No stopping either device here any more. The pass takes a lease
        # on its own worker thread (see _run_spectrum_image), where
        # waiting out a scan already in flight costs nothing but the
        # wait - rather than refusing on the GUI thread because one was.
        self._run_spectrum_image(PassWriter, target)

    def _spectrum_image_target(self) -> str | None:
        """
        Return the detector to read out per beam position, or refuse.

        **This method is the refusals**, which is why it is one: on every
        backend but the preview, declining to acquire *is* what happens
        when the button is pressed, and each of the four reasons wants
        its own sentence because each has a different fix — attach a
        session, use a different instrument, wire a detector to the
        column, or choose one that is wired.

        The panel's choice is honoured or refused, never quietly replaced
        by the first available target: acquiring against a detector the
        operator did not choose would store it under that detector's name,
        producing a file that is wrong in a way nothing about it looks
        wrong.

        Returns
        -------
        str | None
            The chosen target, or None when the acquisition was refused —
            in which case the status line already says why.
        """
        if self._session is None:
            self._recording_status.setText(_NO_SESSION_MESSAGE)
            return None
        # Both refusals come from the description now, where they were
        # an ``isinstance`` against the scan unit and a list read off
        # it. They stay two messages because the operator does two
        # different things about them: use a different instrument, or
        # wire a detector to this one.
        described = self._description(SCANNER_TARGET)
        if not described.synchronises:
            self._recording_status.setText(
                "this backend cannot acquire a spectrum image: it has no "
                "synchronised scan/camera mode, so there is no way to tie a "
                "camera frame to a probe position",
            )
            return None
        targets = list(described.synchronised_targets)
        if not targets:
            self._recording_status.setText(
                "no camera is wired to the scan unit, so nothing can be read "
                "out at each beam position",
            )
            return None
        combo = getattr(self, "_sync_target_combo", None)
        # No panel: an instrument with no scan group has no combo to ask,
        # and a caller driving this widget from a script has not made a
        # choice to honour.
        chosen = targets[0] if combo is None else combo.currentText()
        if chosen not in targets:
            self._recording_status.setText(
                f"the selected detector is not one this scan unit can "
                f"synchronise; it can read out {targets}",
            )
            return None
        return chosen

    def _run_spectrum_image(self, writer_class: type, target: str) -> None:
        """
        Drive one synchronised pass into a file in the session.

        Split out so :meth:`acquire_spectrum_image` reads as the list of
        refusals it mostly is.

        Parameters
        ----------
        writer_class : type
            The pass writer to use, imported by the caller.
        target : str
            The camera target to read out at each beam position.
        """
        parameters = self._pass_parameters()
        positions = f"{parameters.height}x{parameters.width}"
        path, index, slug, started_at = self._session.reserve("spectrum-image")
        self._recording_status.setText(f"acquiring {positions} pass...")
        channels = list(range(len(self.channel_names())))
        # Built on the GUI thread and read from it, so the worker only
        # ever writes through them. See viewer/progress.py.
        watched: dict[str, progress.PassPreview] = {}

        def run() -> object:
            # The lease is taken here, on the job's own thread, for the
            # reason every other acquisition takes it there: granting one
            # means waiting out the pass already in flight, and a pass is
            # height x width x dwell - up to minutes on a large scan.
            # Both targets in one lease, so the broker takes them in its
            # own order: a scanner and a camera claimed separately are how
            # two clients asking in opposite orders deadlock, and the
            # scanner goes last so the probe stands still for the shortest
            # interval the grant allows.
            with self._broker.lease(
                SCANNER_TARGET,
                target,
                reason="spectrum image",
            ) as leased:
                # Sizing the file is an *acquisition* - one frame out of
                # the detector at the settings the pass will use - so it
                # belongs inside the lease with the pass it sizes. It ran
                # on the GUI thread against a device handle before, which
                # was a second driver on a shared instrument and is not
                # possible at all when the instrument is elsewhere. A
                # failure here now reaches the operator through the job,
                # in the same words: see _poll_pass.
                allocation = self._pass_allocation(target, leased.camera(target))
                with writer_class(path, parameters, **allocation) as writer:
                    watched.update(progress.previews(writer.destinations()))
                    # Cast rather than checked: whether the scan unit has
                    # a synchronised mode was settled before this job was
                    # started, by the description listing a camera it can
                    # read out - see _spectrum_image_target.
                    synchronised = typing.cast(
                        "SynchronisedScanner",
                        leased.scanner(),
                    )
                    result = synchronised.scan_synchronised(
                        parameters,
                        channels=channels,
                        targets=[target],
                        into=watched,
                    )
                    writer.finish(result)
                    return result

        self._pass_job = jobs_module.PassJob(run)
        self._pass_preview = watched
        self._pass_target = target
        self._pass_path = path
        self._pass_positions = positions
        self._pass_job.start()
        self._timer.start()
        del index, slug, started_at

    def _poll_pass(self) -> None:
        """
        Show a synchronised pass building, and report it when it lands.

        The sampled half of the arrangement the live view already uses:
        the pass writes flat out on its own thread, and this — called
        from the display timer — draws whatever has arrived. What it
        draws is a virtual-detector image formed from the signal at each
        beam position, which is the map an operator is watching for
        anyway: it shows drift, contamination, or a probe scanning
        vacuum, minutes before the file exists.
        """
        job = self._pass_job
        if job is None:
            return
        for name, preview in self._pass_preview.items():
            self._show_pass_preview(name, preview)
        if job.is_running:
            done = sum(preview.positions for preview in self._pass_preview.values())
            total = sum(preview.total for preview in self._pass_preview.values())
            if total:
                self._recording_status.setText(
                    f"acquiring {self._pass_positions} pass - "
                    f"{done}/{total} positions ({100 * done // total}%)",
                )
            return
        self._pass_job = None
        if job.error is not None:
            self._recording_status.setText(f"spectrum image failed: {job.error}")
            self._maybe_stop_timer()
            return
        result = typing.cast("ScanPass", job.result)
        # Named from what actually landed rather than from the button:
        # an operator who left the spectrometer imaging has a 4D stack,
        # and a status line calling it a spectrum image would be the
        # first place they could have noticed and did not.
        kind = "spectrum image" if result.spectra else "4D stack"
        self._recording_status.setText(
            f"{kind} saved: {self._pass_path.name} "
            f"({self._pass_positions} positions, sync={result.scan_sync})",
        )
        self._refresh_session_labels()
        self._maybe_stop_timer()

    def _show_pass_preview(
        self,
        target: str,
        preview: progress.PassPreview,
    ) -> None:
        """
        Draw one pass destination's progress map.

        Parameters
        ----------
        target : str
            The detector being read out at each beam position.
        preview : progress.PassPreview
            Its live map.
        """
        if not preview.positions:
            return
        layer_name = f"Acquiring ({target})"
        if layer_name not in self._viewer.layers:
            self._bring_to_front(layer_name)
            self._viewer.add_image(preview.map, name=layer_name, colormap="gray")
        layer = self._viewer.layers[layer_name]
        # The same array every tick, updated in place, so this is a
        # redraw rather than a new upload of a new object.
        layer.data = preview.map
        # Restretched every tick, over the positions visited so far
        # rather than over the whole map: the part the probe has not
        # reached is zero, and including it would put every real value at
        # the top of the range and show a white rectangle growing.
        limits = preview.limits
        if limits is not None:
            layer.contrast_limits = limits

    def _pass_parameters(self) -> ScanParameters:
        """
        Return the geometry the next synchronised pass will use.

        The panel's, normally: a square grid of **Positions** beam
        positions over the field of view, at the Acquire profile's dwell.

        **Unless the device has only one geometry it can acquire**, which
        is the case for a replay device: it holds the grid the probe
        actually visited, and no request makes it another. Asking such a
        device for the panel's numbers would be refused every time, and
        an operator would have to guess a shape they cannot see - a
        recording is 22x25, which the square spin box cannot even
        express. So the description is read first, and carries None
        unless the device genuinely has a fixed grid - read from the
        device once when the broker was built, which is where every
        other fact about what a target *is* now comes from.

        This is not the panel being overridden lightly. It is the same
        rule the readout control follows: what the *device* is set to do
        wins over what a control would like it to do, because the device
        is the one that has to do it.

        Returns
        -------
        ScanParameters
            The beam-position grid, dwell and field of view.
        """
        native = self._description(SCANNER_TARGET).native_scan
        if native is not None:
            return native
        positions = self._positions_spin.value()
        return ScanParameters(
            height=positions,
            width=positions,
            pixel_time_us=self._profile_settings(profiles.ACQUIRE).dwell_us,
            fov_nm=self._fov_spin.value(),
        )

    @staticmethod
    def _pass_allocation(target: str, camera: Camera) -> dict[str, dict]:
        """
        Return the ``PassWriter`` allocation this detector's readout needs.

        The file has to be created, and its datasets sized, **before**
        the acquisition starts — that is what lets the device write
        through to disk rather than into memory that is then copied. So
        the shape has to be known in advance, and it is asked of the
        detector rather than assumed: one frame is taken at the settings
        the pass will use, and its rank decides everything else.

        A 2D readout is allocated as a 4D stack of whole detector images.
        A 1D readout is allocated as a spectrum image, because a detector
        delivering one axis of counts is delivering spectra — its
        surviving axis is the dispersive one, and the pass will carry
        them as a :class:`~miainwoodpecker.devices.interface.Spectrum`.
        Nothing here inspects what *kind* of detector it is, which is
        the point: the rank is a fact and the label would be a guess.

        Parameters
        ----------
        target : str
            The target name, for the allocation's key and the message.
        camera : Camera
            The **leased** camera that will be read out. Leased because
            this takes a frame from it, which is driving the detector
            rather than describing it, and the pass it is sizing holds
            the lease already.

        Returns
        -------
        dict[str, dict]
            Keyword arguments for :class:`PassWriter` — ``cubes`` or
            ``spectra``, with one entry.

        Raises
        ------
        RuntimeError
            If the detector produces a frame of a rank a pass has no
            home for.
        """
        camera.start()
        try:
            shape = tuple(np.asarray(camera.acquire_frame().data).shape)
        finally:
            camera.stop()
        if len(shape) == _SPECTRUM_READOUT_RANK:
            return {"spectra": {target: shape[0]}}
        if len(shape) == _IMAGE_READOUT_RANK:
            return {"cubes": {target: shape}}
        msg = (
            f"{target} produces a {len(shape)}D readout of shape {shape}; a "
            f"pass keeps a 1D spectrum or a 2D image at each beam position, "
            f"and has nowhere to put anything else"
        )
        raise RuntimeError(msg)

    def _leased_frames(
        self,
        targets: Sequence[str],
        reason: str,
        produce: Callable[[LeasedDevices], Iterable[Frame]],
    ) -> Iterator[Frame]:
        """
        Yield a series from inside a lease, taken on the consumer's thread.

        The generator body does not run until the first ``next``, and
        every acquisition here is consumed by a worker
        (:class:`~miainwoodpecker.storage.session.RecordingJob`,
        :class:`~miainwoodpecker.viewer.jobs.AnalysisJob`) — so the lease
        is taken *there*, not in the click handler that built this.

        That placement is the whole reason acquisition no longer freezes
        the window. Taking a lease means waiting out the pass already in
        flight, which is ``height x width x dwell``: a quarter of a
        second on a small fast scan and 42 seconds at 2048x2048 and ten
        microseconds. The old code called ``stop_scan`` on the GUI
        thread and refused if it did not return in time, which is how a
        long scan became "still busy - try again" rather than a wait.

        The lease is renewed per frame rather than granted for a guessed
        duration. A recording of a thousand frames outlives any fixed
        time to live, and renewing as frames arrive means a job that
        wedges stops renewing and lets the broker take the instrument
        back — which is the behaviour a time to live is for.

        Parameters
        ----------
        targets : Sequence[str]
            The targets the series drives. Named together so the broker
            takes them in its own order rather than this file's.
        reason : str
            What to show other clients while it is held.
        produce : Callable[[LeasedDevices], Iterable[Frame]]
            Builds the series from the leased devices.

        Yields
        ------
        Frame
            Each frame of the series.
        """
        with self._broker.lease(*targets, reason=reason) as leased:
            for frame in produce(leased):
                leased.renew()
                yield frame

    def _start_recording(self, frames: Iterable[Frame], label: str) -> None:
        """
        Hand a frame series to a worker thread that streams it to disk.

        The generator is *built* here on the GUI thread but *consumed* on
        the worker, which touches only the device and the file — never Qt.
        The GUI thread learns how it is going by polling the job from the
        display timer (:meth:`_poll_recording`).
        """
        if self._session is None:
            self._recording_status.setText(_NO_SESSION_MESSAGE)
            return
        if self._recording_job is not None and self._recording_job.is_running:
            self._recording_status.setText("already recording - stop it first")
            return
        self._recording_job = RecordingJob(
            self._session,
            frames,
            label=label,
            note=self._note_for_next_recording(),
        )
        self._recording_job.start()
        self._recording_status.setText(f"recording {label}...")
        self._cancel_record_button.setEnabled(True)
        self._timer.start()

    def cancel_recording(self) -> None:
        """
        Stop a running recording, keeping the frames already written.

        Cancellation is cooperative: the worker stops pulling frames,
        which unwinds ``record``'s ``with`` block normally, so the file is
        finalized and valid rather than truncated.
        """
        job = self._recording_job
        if job is None or not job.is_running:
            return
        job.cancel()
        self._recording_status.setText("stopping...")

    def _poll_recording(self) -> None:
        """Report a recording job's progress and result on the GUI thread."""
        job = self._recording_job
        if job is None:
            return
        if job.is_running:
            if not job.is_cancelled:
                self._recording_status.setText(
                    f"recording - {job.frames_recorded} frames"
                )
            return
        self._recording_job = None
        self._cancel_record_button.setEnabled(False)
        if job.error is not None:
            self._recording_status.setText(f"error: {job.error}")
        elif job.result is not None:
            verb = "cancelled after" if job.is_cancelled else "saved"
            self._recording_status.setText(
                f"{verb} {job.result.frame_count} frames -> {job.result.path.name}"
            )
        self._refresh_session_labels()
        self._maybe_stop_timer()

    def open_selected_recording(self) -> None:
        """Open the recording chosen in the Recordings combo."""
        chosen = self._recording_combo.currentData()
        if chosen is None:
            self._load_status.setText("no recording selected")
            return
        self.open_recording(chosen)

    def choose_and_open_recording(self) -> None:
        """Ask for any file on disk and open it (the button handler)."""
        start_in = str(self._session.root) if self._session is not None else ""
        chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open recording", start_in, _NEXUS_FILE_FILTER
        )
        if chosen:
            self.open_recording(chosen)

    def open_recording(self, path: str | Path) -> None:
        """
        Read a recording off disk and display it, without blocking the GUI.

        This is the "open the file I just took and look at it again" path a
        parallel pilot against Swift needs. Reading is slow I/O — a real
        two-frame 2048x2048 Ronchigram recording is 23.3MB of compressed
        HDF5 — so the read runs on a
        :class:`~miainwoodpecker.storage.session.LoadJob` worker and the
        result is collected by the display timer, exactly as recording
        already works in the other direction (Phase 2's thread-safety
        contract).

        A file that will not open at all is reported as a sentence, not a
        traceback: the load job captures the failure and
        :meth:`_poll_load` puts its message in the status label.

        Parameters
        ----------
        path : str | Path
            The NeXus file to open.
        """
        if self._load_job is not None and self._load_job.is_running:
            self._load_status.setText("still opening the last one")
            return
        target = Path(path)
        self._load_job = LoadJob(target)
        self._load_job.start()
        self._load_status.setText(f"opening {target.name}...")
        self._timer.start()

    def _poll_load(self) -> None:
        """Report a load job's progress and display its result on the GUI thread."""
        job = self._load_job
        if job is None:
            return
        if job.is_running:
            self._load_status.setText(
                f"opening {job.path.name} - {job.frames_loaded} frames"
            )
            return
        self._load_job = None
        if job.error is not None:
            self._load_status.setText(f"cannot open {job.path.name}: {job.error}")
        elif job.result is not None:
            self._display_loaded(job.result)
        self._maybe_stop_timer()

    def _display_loaded(self, loaded: LoadedRecording) -> None:
        """
        Push a loaded recording into napari and say what it is.

        A multi-frame recording goes in as the ``(frames, height, width)``
        stack it is, because napari renders a 3D array with a frame slider
        natively — reimplementing stack navigation to show one frame at a
        time would be exactly the kind of bespoke UI §3 adopts napari to
        delete. A single-frame recording is squeezed to 2D, since a slider
        with one position is furniture, not information.

        Parameters
        ----------
        loaded : LoadedRecording
            The frames and the description of where they came from.
        """
        recording = loaded.recording
        self._opened_file = recording.path
        # Kept alongside the path, from the same load: this is what the
        # analysis buttons hand the adapters instead of making them read
        # the file a second time. None when these frames are not the whole
        # recording - LoadedRecording.frames decides that, not this widget.
        self._opened_frames = loaded.frames
        data = loaded.data[0] if loaded.data.shape[0] == 1 else loaded.data
        name = f"File: {recording.path.name}"
        # Before the layer, not after: this both reopens a panel the
        # operator had closed and queues the raise, and removing the old
        # layer below closes that panel's window in between.
        self._bring_to_front(name)
        if name in self._viewer.layers:
            del self._viewer.layers[name]
        self._viewer.add_image(
            data,
            name=name,
            colormap="gray",
            # The calibration the file itself carries, read back with the
            # frames rather than re-derived here. A multi-frame recording
            # goes in as a stack, so the axes are built for its extra
            # leading dimension - napari's frame slider, not a drawn axis.
            **axes.layer_axes(
                loaded.calibration or FrameCalibration.uncalibrated(),
                ndim=data.ndim,
            ),
        )
        parts = [_condition(recording)]
        if loaded.truncated:
            parts.append(
                f"showing the first {loaded.data.shape[0]} - the rest exceeded "
                f"this app's in-memory read budget"
            )
        self._load_status.setText(f"{recording.path.name}: {' - '.join(parts)}")

    def _analysis_source(self) -> tuple[Path | None, FrameStack | None]:
        """
        Return the opened file the analysis buttons should use, and its frames.

        Both halves are read here, on the GUI thread, and passed into the
        worker as plain data — the checkbox because it is a widget, and the
        frames because they belong to whichever load last succeeded and
        must not be picked up mid-flight from another thread.

        Returns
        -------
        tuple[Path | None, FrameStack | None]
            The file to analyze (``None`` to acquire a fresh burst
            instead), and the frames already read from it, if they are the
            whole recording.
        """
        if not self._analyze_from_file_check.isChecked():
            return None, None
        return self._opened_file, self._opened_frames

    @contextlib.contextmanager
    def _analysis_input(  # noqa: PLR0913 - keyword-only inputs, not a call signature
        self,
        *,
        frame_count: int,
        label: str,
        title: str,
        filename: str,
        existing: Path | None,
        existing_frames: FrameStack | None,
        note: str | None,
    ) -> Iterator[_AnalysisInput]:
        """
        Yield the NeXus file an analysis button should run against.

        Three cases, in order of precedence:

        1. **A file already on disk**, when the operator has opened one and
           ticked the Recordings checkbox. Nothing is acquired and the
           camera is never touched — the point being that re-running an
           analysis on yesterday's data should not cost a fresh exposure on
           the sample, or wait for one.
        2. **A fresh burst into the session**, so clicking an analysis
           button both analyzes and keeps the data.
        3. **A fresh burst into a temporary file** when there is no
           session — the original Phase 4 behaviour, unchanged, so the
           widget still works as a pure live display.

        For case 1 the file is checked *before* the adapter sees it. The
        Phase 4 adapters read ``/entry/data`` and raise "it recorded no
        frames" when it is absent, which is precisely wrong for the
        abandoned-writer file from the Phase 3 interruption table: every
        frame is present and readable, only the finalization is missing. So
        that case is refused here with a sentence saying so.

        Case 1 is also where the file gets read *once* rather than twice.
        Opening it already read every frame to display them, so those
        frames are passed on to the adapter rather than the adapter being
        pointed at the path and decompressing the same tens of megabytes
        again. Two conditions have to hold, and both are checked rather
        than assumed:

        - :attr:`~miainwoodpecker.storage.session.LoadedRecording.frames`
          has to have offered them at all, which it declines to do for a
          truncated read or a file that states no calibration;
        - the file's own frame count, read here, has to match how many are
          in hand — if the recording on disk has grown since it was opened,
          the in-memory copy is stale and the file is the authority on what
          it contains.

        Either way the analysis is the same analysis; the fallback costs
        the second read this exists to avoid, and nothing else.

        The fresh-burst cases (2 and 3) deliberately keep reading the file
        they just wrote. The frames are in memory there too, but their
        calibration is only resolved when ``NexusWriter`` writes them (from
        the frame metadata, by
        :func:`~miainwoodpecker.storage.calibration.resolve_frame_calibration`),
        so short-circuiting the read would mean re-deriving that here — a
        second implementation of the rule that decides what a recording's
        axes are, to save one read of a file this app just created.

        Parameters
        ----------
        frame_count : int
            Frames to acquire, for the fresh-burst cases.
        label : str
            Session label for a fresh burst; unused without a session.
        title : str
            ``/entry/title`` for a written file.
        filename : str
            Temporary filename used for a fresh burst with no session.
        existing : Path | None
            The already-open recording to analyze, or None to acquire.
            Passed in rather than read from the checkbox here because this
            runs on a worker thread (see
            :class:`~miainwoodpecker.viewer.jobs.AnalysisJob`), and reading
            a widget off the GUI thread is exactly what that split exists to
            prevent. :meth:`_start_analysis` resolves it beforehand.
        existing_frames : FrameStack | None
            That file's frames, if the load that displayed them kept them.
            Resolved on the GUI thread with ``existing``, for the same
            reason, and used only when the checks above pass.
        note : str | None
            Per-recording note for a fresh burst into a session, resolved on
            the GUI thread for the same reason.

        Yields
        ------
        _AnalysisInput
            The file to analyze and how to describe it afterwards.

        Raises
        ------
        RecordingReadError
            If the operator pointed the analysis at a file that cannot be
            analyzed, with the reason.
        """
        if existing is not None:
            described = describe(existing)
            refusal = _analysis_refusal(described)
            if refusal is not None:
                raise RecordingReadError(refusal)
            in_hand = (
                existing_frames
                if existing_frames is not None
                and len(existing_frames.data) == described.frame_count
                else None
            )
            yield _AnalysisInput(
                path=existing,
                frame_count=described.frame_count,
                origin=existing.name,
                frames=in_hand,
            )
            return

        binding = self._first_binding()
        if binding is None:  # pragma: no cover - callers check first
            msg = "no camera to acquire from and no file opened"
            raise RecordingReadError(msg)
        # On the analysis worker's thread, which is why the lease can be
        # taken here at all: it waits out whatever exposure the live loop
        # is in rather than refusing because there is one.
        frames = list(
            self._leased_frames(
                [binding.name],
                "analysis burst",
                lambda leased: camera_series(
                    leased.camera(binding.name),
                    frame_count,
                ),
            ),
        )
        acquired = _AnalysisInput(
            path=Path(), frame_count=len(frames), origin="a fresh burst"
        )
        if self._session is not None:
            recording = self._session.record(
                frames, label=label, title=title, note=note
            )
            # The session labels this recording changed are refreshed by
            # _poll_analysis on the GUI thread, not here: this runs on a
            # worker thread.
            yield dataclasses.replace(acquired, path=recording.path)
        else:
            with tempfile.TemporaryDirectory() as tmp_dir:
                path = Path(tmp_dir) / filename
                write_frames(path, frames, title=title)
                yield dataclasses.replace(acquired, path=path)

    def _start_analysis(  # noqa: PLR0913 - keyword-only, and each one differs per button
        self,
        *,
        status: QtWidgets.QLabel,
        compute: Callable[[_AnalysisInput], object],
        display: Callable[[object, _AnalysisInput], str],
        frame_count: int,
        label: str,
        title: str,
        filename: str,
    ) -> None:
        """
        Run one analysis on a worker thread and return immediately.

        The three analysis buttons differ in what they compute and how they
        draw it, and are identical in everything else: stop the live camera
        so the button and the loop never drive the same device at once,
        acquire or open a file, do the work, draw the result, say what
        happened. That shared part lives here, and the two callables are the
        difference.

        The GUI/worker split is the point. Everything that touches a widget
        happens on this side of the call — resolving the operator's
        Recordings checkbox and note field *before* the thread starts, and
        deferring every layer and label update to :meth:`_poll_analysis`.
        Only ``compute`` crosses over, and it is handed plain data. The
        frames of an already-opened recording are resolved here for the
        same reason and travel the same way: arrays and a calibration are
        plain data, so handing them to the worker changes nothing about the
        contract — what would break it is the worker reaching back for
        ``self._opened_frames`` itself.

        One analysis at a time: a second click while one is running is
        refused rather than queued, because all three share the camera and
        the status labels, and two bursts interleaved on one device is not a
        thing an operator ever wants.

        Parameters
        ----------
        status : QtWidgets.QLabel
            The clicked button's own status label.
        compute : Callable[[_AnalysisInput], object]
            The analysis, run on the worker thread. Must not touch Qt.
        display : Callable[[object, _AnalysisInput], str]
            Draws the result on the GUI thread and returns the status text.
        frame_count : int
            Frames to acquire, when acquiring.
        label : str
            Session label for a fresh burst.
        title : str
            ``/entry/title`` for a written file.
        filename : str
            Temporary filename for a fresh burst with no session.
        """
        if self._analysis_job is not None and self._analysis_job.is_running:
            status.setText("another analysis is running")
            return
        # No stopping the camera here any more. The burst takes a lease
        # on the worker thread (see _analysis_input), where waiting out
        # an exposure costs nothing but the wait - rather than refusing
        # on the GUI thread because the camera was mid-frame.
        existing, existing_frames = self._analysis_source()
        note = self._note_for_next_recording()

        def work() -> _AnalysisOutcome:
            with self._analysis_input(
                frame_count=frame_count,
                label=label,
                title=title,
                filename=filename,
                existing=existing,
                existing_frames=existing_frames,
                note=note,
            ) as source:
                return _AnalysisOutcome(payload=compute(source), source=source)

        self._analysis_status = status
        self._analysis_display = display
        self._analysis_job = AnalysisJob(work)
        self._analysis_job.start()
        status.setText("working...")
        self._timer.start()

    def _poll_analysis(self) -> None:
        """Draw a finished analysis and report it, on the GUI thread."""
        job = self._analysis_job
        if job is None or job.is_running:
            return
        self._analysis_job = None
        status = self._analysis_status
        display = self._analysis_display
        self._analysis_status = None
        self._analysis_display = None
        if status is None or display is None:  # pragma: no cover - set together
            return
        if job.error is not None:
            status.setText(f"error: {job.error}")
        else:
            outcome = typing.cast("_AnalysisOutcome", job.result)
            status.setText(display(outcome.payload, outcome.source))
        # A fresh burst into a session wrote a file; the worker thread could
        # not say so, so the labels catch up here.
        self._refresh_session_labels()
        self._maybe_stop_timer()

    def _analysis_runner(self, name: str) -> AnalysisRunner:
        """
        Return this widget's runner for one analysis target, making it once.

        Cached for the widget's lifetime because an isolated runner owns a
        worker process whose library import costs seconds, and paying that
        per click would turn a demonstrated capability into an annoyance.
        The in-process runner has nothing to cache and is cached anyway,
        so the two paths differ in transport and in nothing else.

        Called on the GUI thread, before the analysis job starts, for the
        same reason every other pre-flight check is: it can raise, and it
        can talk to a status label.

        Parameters
        ----------
        name : str
            The analysis target, as
            :data:`~miainwoodpecker.analysis.transfer.ANALYSIS_TARGETS`
            names it.

        Returns
        -------
        AnalysisRunner
            The runner to hand to the button's ``compute`` closure.

        Notes
        -----
        Propagates ``ImportError`` from
        :func:`~miainwoodpecker.analysis.remote.open_runner` when the
        target's optional extra is not installed — which each caller turns
        into its own status message, exactly as the handler's own
        ``try``/``except`` did before.
        """
        existing = self._analysis_runners.get(name)
        if existing is not None:
            return existing
        from miainwoodpecker.analysis.remote import open_runner  # noqa: PLC0415

        runner = open_runner(name)
        self._analysis_runners[name] = runner
        return runner

    def _close_analysis_runners(self) -> None:
        """
        Shut down every analysis worker this widget started.

        Guarded individually: a worker that has already died should not
        stop the next one being reaped, and an application on its way out
        has nothing to gain from a traceback here.
        """
        for runner in self._analysis_runners.values():
            with contextlib.suppress(Exception):
                runner.close()
        self._analysis_runners.clear()

    def _analyze_camera_in_hyperspy(self) -> None:
        """
        Round-trip a short camera burst through the HyperSpy adapter.

        Demonstrates the Phase 4 analysis-integration path end to end:
        stop the live camera loop (so this button and the loop never
        drive the same device at once), get a NeXus file to analyze from
        :meth:`_analysis_input` — a fresh burst, or a recording already on
        disk if the operator opened one and ticked the Recordings
        checkbox, whose frames are then handed over directly rather than
        read again — read it back as a HyperSpy signal with
        :func:`~miainwoodpecker.analysis.hyperspy_bridge.load_as_hyperspy_signal`,
        run one real HyperSpy operation
        (:meth:`hyperspy.signals.Signal2D.mean` over the frame axis), and
        push the result into napari as a new image layer. Requires the
        ``analysis`` optional dependency group; reports that in the
        status label rather than crashing the widget if it is missing.

        Whether the HyperSpy call happens in this process or in an
        isolated worker is
        :func:`~miainwoodpecker.analysis.remote.analysis_runner`'s
        decision, not this handler's, and in-process remains the default.
        See docs/analysis-isolation.md.
        """
        status = self._analyze_status
        if self._first_binding() is None or status is None:
            return
        try:
            runner = self._analysis_runner("hyperspy")
        except ImportError:
            # target_available said yes and the import still failed: a
            # half-installed distribution whose spec resolves.
            status.setText("the 'analysis' extra is installed but broken")
            return

        def compute(source: _AnalysisInput) -> object:
            return runner.run("mean_projection", _analysis_job_input(source))

        def display(payload: object, source: _AnalysisInput) -> str:
            self._bring_to_front("HyperSpy mean projection (Camera)")
            self._viewer.add_image(
                payload,
                name="HyperSpy mean projection (Camera)",
                colormap="viridis",
            )
            return f"done - mean of {source.frame_count} frames from {source.origin}"

        self._start_analysis(
            status=status,
            compute=compute,
            display=display,
            frame_count=_ANALYSIS_BURST_FRAME_COUNT,
            label="hyperspy-burst",
            title="hyperspy analysis burst",
            filename="hyperspy_analysis_burst.nxs",
        )

    def _analyze_camera_in_libertem(self) -> None:
        """
        Round-trip a short camera burst through the LiberTEM adapter.

        The second half of the Phase 4 analysis-integration path: stop the
        live camera loop if running, get a NeXus file to analyze from
        :meth:`_analysis_input` (a fresh burst, or a recording already on
        disk if the operator opened one and ticked the Recordings
        checkbox, in which case its frames go straight into a
        ``MemoryDataSet`` rather than being read off disk again), read it
        back as a LiberTEM ``DataSet`` with
        :func:`~miainwoodpecker.analysis.libertem_bridge.load_as_libertem_dataset`,
        run one real LiberTEM UDF (``libertem.udf.sum.SumUDF``, summing
        across the frame/navigation axis) on the thread-bounded inline
        ``Context`` from
        :func:`~miainwoodpecker.analysis.libertem_bridge.analysis_context`,
        and push the result into napari as a new image layer. Requires the
        ``libertem`` optional dependency group; reports that in the status
        label rather than crashing the widget if it is missing.

        The executor stays inline and thread-bounded, unchanged — see
        :func:`~miainwoodpecker.analysis.operations.sum_projection`, which
        is where that closure's body now lives so the isolated worker can
        run the same code rather than a copy of it.
        """
        status = self._libertem_status
        if self._first_binding() is None or status is None:
            return
        try:
            runner = self._analysis_runner("libertem")
        except ImportError:
            # target_available said yes and the import still failed: a
            # half-installed distribution whose spec resolves.
            status.setText("the 'libertem' extra is installed but broken")
            return

        def compute(source: _AnalysisInput) -> object:
            return runner.run("sum_projection", _analysis_job_input(source))

        def display(payload: object, source: _AnalysisInput) -> str:
            self._bring_to_front("LiberTEM sum projection (Camera)")
            self._viewer.add_image(
                payload,
                name="LiberTEM sum projection (Camera)",
                colormap="viridis",
            )
            return f"done - sum of {source.frame_count} frames from {source.origin}"

        self._start_analysis(
            status=status,
            compute=compute,
            display=display,
            frame_count=_ANALYSIS_BURST_FRAME_COUNT,
            label="libertem-burst",
            title="libertem analysis burst",
            filename="libertem_analysis_burst.nxs",
        )

    def _fit_central_disk_in_py4dstem(self) -> None:
        """
        Round-trip one real camera frame through the py4DSTEM adapter.

        Demonstrates the py4DSTEM follow-up to Phase 4 (migration plan,
        §5) end to end: stop the live camera loop if running, get a NeXus
        file from :meth:`_analysis_input` — one freshly acquired frame, or
        a recording already on disk if the operator opened one and ticked
        the Recordings checkbox, in which case the frames already read to
        display it are used rather than read again — read it back
        as a py4DSTEM ``DiffractionSlice`` with
        :func:`~miainwoodpecker.analysis.py4dstem_bridge.load_as_diffraction_slice`,
        run one real py4DSTEM operation on that single diffraction pattern
        (``py4DSTEM.process.calibration.get_probe_size``, the same
        central-disk fit py4DSTEM runs per-pattern inside a full
        datacube), and push both the analyzed frame and a napari ``Shapes``
        ellipse at the fitted disk into the viewer. Only a single frame is
        used, not a scan-position-indexed cube - see
        :mod:`miainwoodpecker.analysis.py4dstem_bridge` for why that cube
        isn't available yet; a *multi-frame* file opened from disk therefore
        has its first pattern fitted, since ``get_probe_size`` fits one
        pattern and averaging several would fit something that was never
        acquired. Requires the ``py4dstem`` optional dependency
        group; reports that in the status label rather than crashing the
        widget if it is missing.
        """
        status = self._py4dstem_status
        if self._first_binding() is None or status is None:
            return
        try:
            runner = self._analysis_runner("py4dstem")
        except ImportError:
            # target_available said yes and the import still failed: a
            # half-installed distribution whose spec resolves.
            status.setText("the 'py4dstem' extra is installed but broken")
            return

        def compute(source: _AnalysisInput) -> object:
            return runner.run("fit_central_disk", _analysis_job_input(source))

        def display(payload: object, source: _AnalysisInput) -> str:
            pattern, radius, x0, y0 = typing.cast(
                "tuple[typing.Any, float, float, float]", payload
            )
            self._bring_to_front("py4DSTEM disk fit (Camera)")
            self._viewer.add_image(
                pattern,
                name="py4DSTEM disk fit (Camera)",
                colormap="gray",
            )
            self._viewer.add_shapes(
                [
                    [y0 - radius, x0 - radius],
                    [y0 - radius, x0 + radius],
                    [y0 + radius, x0 + radius],
                    [y0 + radius, x0 - radius],
                ],
                shape_type="ellipse",
                name="py4DSTEM disk fit",
                edge_color="red",
                face_color="transparent",
                # The ellipse is in the *pattern's* pixel coordinates, so
                # it belongs in that image's window rather than one of
                # its own, where it would be a red circle marking nothing.
                metadata={ATTACHED_TO: "py4DSTEM disk fit (Camera)"},
            )
            return (
                f"done - r={radius:.1f}px center=({x0:.1f}, {y0:.1f}) "
                f"from {source.origin}"
            )

        self._start_analysis(
            status=status,
            compute=compute,
            display=display,
            frame_count=1,
            label="py4dstem-frame",
            title="py4DSTEM analysis frame",
            filename="py4dstem_analysis_frame.nxs",
        )

    def _maybe_stop_timer(
        self,
        states: Mapping[str, TargetState] | None = None,
    ) -> None:
        # Reuses the tick's own answer when it has one. Asking again is
        # another entry to the broker and another loop lock, and this is
        # called from every poller on every tick.
        if states is None:
            states = {
                name: view.state for name, view in self._broker.snapshot().items()
            }
        scan_running = SCANNER_TARGET in states and states[SCANNER_TARGET].is_live
        camera_running = any(
            name in states and states[name].is_live
            for name in self._camera_bindings
        )
        recording = self._recording_job is not None and self._recording_job.is_running
        loading = self._load_job is not None and self._load_job.is_running
        analyzing = self._analysis_job is not None
        passing = self._pass_job is not None
        if (
            not scan_running
            and not camera_running
            and not recording
            and not loading
            and not analyzing
            and not passing
        ):
            self._timer.stop()

    def refresh_display(self) -> None:
        """Push the newest frames into napari layers; called by the display timer."""
        self._poll_recording()
        self._poll_load()
        self._poll_analysis()
        self._poll_pass()
        # One question per tick, not one per source plus one for the
        # chrome. Each entry to the broker re-takes a loop lock that its
        # acquisition worker is reacquiring on every grab, so polling in
        # pieces means queueing behind the thread being watched.
        views = self._broker.snapshot()
        if SCANNER_TARGET in views:
            self._refresh_scan(views[SCANNER_TARGET])
        for binding in self._camera_bindings.values():
            view = views.get(binding.name)
            if view is not None and binding.status is not None:
                self._refresh_source(
                    view,
                    layer_name=binding.layer_name,
                    status_label=binding.status,
                    autocontrast_every_frame=False,
                )

    def _refresh_scan(self, view: TargetView) -> None:
        """
        Push the newest pass into one napari layer per enabled detector.

        Every layer updated from the *same* ``latest_frames`` snapshot,
        so the images on screen are always from one traversal. Refreshing
        them from separate reads would let HAADF advance a pass ahead of
        MAADF, and an operator differencing what they see would be
        differencing two probe positions.

        Parameters
        ----------
        view : TargetView
            The scanner's state and latest pass, read once per display
            tick with every other target's, so a tick sees one
            consistent answer.
        """
        state = view.state
        if state.error is not None:
            self._scan_status.setText(f"error: {state.error}")
            self._maybe_stop_timer()
            return
        if state.lease is not None:
            # Paused for an acquisition rather than idle, and saying so
            # beats a status line that reads as if the scan had stopped
            # on its own. The broker restarts it when the lease ends.
            self._scan_status.setText(f"held: {state.lease.reason or 'acquiring'}")
            return
        frames = view.frames
        if not frames:
            return
        names = self._scan_request[2]
        for frame, name in zip(frames, names, strict=False):
            self._show_frame(
                frame,
                layer_name=self._scan_layer_name(name),
                autocontrast_every_frame=True,
            )
        if state.stats is not None:
            self._scan_status.setText(f"running - {state.stats.fps:.1f} fps")

    @staticmethod
    def _scan_layer_name(channel: str) -> str:
        """
        Return the napari layer name for one detector channel.

        Parameters
        ----------
        channel : str
            The detector's name.

        Returns
        -------
        str
            The layer name.
        """
        return f"Scan ({channel})"

    def _rename_scan_layers(self) -> None:
        """
        Drop layers for detectors that are no longer enabled.

        A layer left behind after its checkbox is cleared keeps showing
        the last image that detector produced, which reads as a live
        feed that has silently stopped.
        """
        wanted = {self._scan_layer_name(name) for name in self._scan_request[2]}
        for layer_name in list(self._displayed):
            if layer_name.startswith("Scan (") and layer_name not in wanted:
                self._displayed.pop(layer_name, None)
                if layer_name in self._viewer.layers:
                    del self._viewer.layers[layer_name]

    def _refresh_source(
        self,
        view: TargetView,
        *,
        layer_name: str,
        status_label: QtWidgets.QLabel,
        autocontrast_every_frame: bool,
    ) -> None:
        state = view.state
        if state.error is not None:
            status_label.setText(f"error: {state.error}")
            # A failed grab stops the worker but sets no stop event, so
            # without this the timer would run forever, reformatting the
            # same exception 30 times a second at full display cost.
            self._maybe_stop_timer()
            return
        if state.lease is not None:
            status_label.setText(f"held: {state.lease.reason or 'acquiring'}")
            return
        if not view.frames:
            return
        frame = view.frames[0]
        self._show_frame(
            frame,
            layer_name=layer_name,
            autocontrast_every_frame=autocontrast_every_frame,
        )
        self._refresh_rate_label(state, status_label)

    def _bring_to_front(self, layer_name: str) -> None:
        """
        Bring a layer's window out from under whatever is covering it.

        Called when a detector or camera is started, because starting a
        source again is a request to see it. A panel is free to be
        covered while it runs — that is the operator's arrangement to
        make — but it should not stay buried once they ask for it back.

        Nothing happens when the display is a single shared canvas
        rather than a set of documents: there is no window to raise, and
        every layer is already on screen. That is why this asks the
        display whether it can do it rather than assuming, which is the
        same reason ``documents.DocumentBoard`` is duck-typed to
        ``napari.Viewer`` at all.

        Parameters
        ----------
        layer_name : str
            The layer whose window should come to the front.
        """
        raise_document = getattr(self._viewer, "raise_document", None)
        if raise_document is not None:
            raise_document(layer_name)

    def _show_frame(
        self,
        frame: Frame,
        *,
        layer_name: str,
        autocontrast_every_frame: bool,
    ) -> None:
        """
        Put one frame into its napari layer, skipping an unchanged redraw.

        Split out of :meth:`_refresh_source` so the scan can drive it
        once per enabled detector from a single pass, rather than each
        detector polling its own loop.

        Parameters
        ----------
        frame : Frame
            The frame to display.
        layer_name : str
            The layer it belongs in.
        autocontrast_every_frame : bool
            Whether to restretch the contrast limits to this frame.
        """
        if frame is self._displayed.get(layer_name) and layer_name in (
            self._viewer.layers
        ):
            # Nothing new since the last tick. The display timer is fixed
            # at 16 ms while acquisition runs at whatever the device
            # manages, so most ticks see the frame they already drew:
            # assigning layer.data schedules a GPU re-upload and the
            # autocontrast pass walks the whole array twice, both for
            # pixels that did not change. Identity is the right test -
            # the loop hands out the same object until it grabs another.
            return
        calibration = axes.frame_calibration(frame.data, frame.metadata)
        if layer_name in self._viewer.layers:
            layer = self._viewer.layers[layer_name]
            layer.data = frame.data
            self._recalibrate(layer, layer_name, calibration)
            if autocontrast_every_frame:
                low = float(frame.data.min())
                high = float(frame.data.max())
                if high > low:
                    layer.contrast_limits = (low, high)
        else:
            self._viewer.add_image(
                frame.data,
                name=layer_name,
                colormap="gray",
                **axes.layer_axes(calibration),
            )
            self._calibrated[layer_name] = calibration
        self._displayed[layer_name] = frame

    def _recalibrate(
        self,
        layer: typing.Any,  # noqa: ANN401 - a napari layer, imported only for typing
        layer_name: str,
        calibration: FrameCalibration,
    ) -> None:
        """
        Update a live layer's axes when its calibration has changed.

        Changing the field of view mid-scan rescales the picture without
        replacing the layer, so the calibration has to follow the frames
        rather than being set once when the panel opens — otherwise the
        scale bar would go on stating the field of view the operator
        navigated away from, which is worse than showing none.

        Written as a comparison rather than an unconditional assignment
        because this runs on every displayed frame, and both properties
        are evented: reassigning an unchanged calibration would refresh
        the scale bar of every panel sixty times a second.

        Parameters
        ----------
        layer : typing.Any
            The napari layer showing this source.
        layer_name : str
            Its name, the key the last calibration is cached under.
        calibration : FrameCalibration
            The calibration the newest frame reports.
        """
        if self._calibrated.get(layer_name) == calibration:
            return
        self._calibrated[layer_name] = calibration
        for name, value in axes.layer_axes(calibration).items():
            if name == "metadata":
                layer.metadata.update(typing.cast("dict", value))
            else:
                setattr(layer, name, value)

    def _refresh_rate_label(
        self,
        state: TargetState,
        status_label: QtWidgets.QLabel,
    ) -> None:
        """
        Show the acquisition rate, rewriting the label only when it changes.

        ``setText`` with identical text still costs a Qt repaint, and this
        runs for every source on every tick; the rate only moves in the
        first decimal a few times a second.
        """
        if not state.is_live or state.stats is None:
            return
        text = f"running - {state.stats.fps:.1f} fps"
        if status_label.text() != text:
            status_label.setText(text)

    def shutdown(self) -> None:
        """
        Stop all loops, the camera, the display timer, and any recording.

        Safe to call more than once, and safe to call after Qt has
        already destroyed this widget. Both matter because there are two
        callers that cannot see each other: ``closeEvent``, which Qt
        fires during app-quit teardown, and any entry point that tidies
        up after ``napari.run()`` returns. The second one runs *after*
        the widget tree has been destroyed, so it used to die with
        "Internal C++ object already deleted" — an ugly traceback on a
        clean exit, and one that skipped the device and thread teardown
        that had not run yet.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._shutdown_qt_parts()
        load_job = self._load_job
        if load_job is not None and load_job.is_running:
            # Reading holds no device and writes nothing, so waiting for it
            # is only about not leaving a thread reading into a file handle
            # while the process tears down.
            load_job.join()
        job = self._recording_job
        if job is not None and job.is_running:
            # Cancel and wait rather than abandon: unwinding the generator
            # lets NexusWriter finalize, so a closing app leaves the
            # operator a valid file. A write abandoned to a dying process
            # is what produces an unreadable one.
            job.cancel()
            job.join()
        if self._owns_broker:
            # This window built the broker, so this window closes it -
            # which stops every loop and parks the instrument, exactly
            # as closing the only program on an instrument should.
            self._quietly(typing.cast("LocalBroker", self._broker).close)
        # A broker that was *handed* to this window belongs to whoever
        # made it, and closing this window touches none of it - not the
        # loops, and certainly not the instrument. Stopping a shared
        # scan on the way out would park the probe on one spot for
        # whoever else is connected, which is the failure this project
        # spends the most words avoiding; and a notebook watching the
        # feed would have it go dark because somebody closed a window
        # they were not using. The display timer stops with the Qt
        # parts above, which is the whole of what leaving costs.
        # After the devices, and deliberately: a worker holds no hardware,
        # so nothing about the column depends on it going first, while
        # stopping the camera can take an exposure's worth of time and an
        # idle worker costs nothing to leave running for it.
        self._quietly(self._close_analysis_runners)

    @staticmethod
    def _quietly(step: Callable[[], object]) -> None:
        """
        Run one teardown step, tolerating widgets Qt has already destroyed.

        Per step rather than around the whole of ``shutdown`` so that one
        dead widget cannot skip the steps after it. That is safe to do
        here because each of these stops its machinery *before* it
        touches a label or a button — ``stop_camera`` pauses the camera
        and then renames the button — so a step that dies on the Qt half
        has already done the half that matters.

        Only the "already deleted" flavour of ``RuntimeError`` is
        swallowed. A device refusing to stop raises the same class, and
        that is worth hearing about even on the way out.

        Parameters
        ----------
        step : Callable[[], object]
            The teardown step to run.

        Raises
        ------
        RuntimeError
            Re-raised when it is not the "already deleted" kind - a
            device refusing to stop is worth hearing about even here.
        """
        try:
            step()
        except RuntimeError as error:
            if "deleted" not in str(error):
                raise

    def _shutdown_qt_parts(self) -> None:
        """
        Stop the timers and take back the status bar, tolerating teardown.

        Separated from the rest of ``shutdown`` so that one dead Qt
        object cannot skip the device and thread teardown that follows
        it. That part is pure Python: the acquisition loops, the worker
        threads and the recording writers do not care that a widget has
        been destroyed, and they are the half that matters — an
        abandoned writer is what leaves an operator an unreadable file.
        """
        try:
            self._timer.stop()
            self._context_save_timer.stop()
            if self._status_bar_installed:
                # These labels live in a window this widget does not
                # own, so leaving them behind would describe a session
                # nothing is writing to any more, and docking a second
                # widget would stack a second copy of each.
                statusbar_panel.remove_status_bar(self, self._viewer)
        except RuntimeError:
            # "Internal C++ object already deleted": Qt destroyed the
            # widget tree before this ran. The timers went with it, so
            # there is nothing left to stop.
            pass
        finally:
            self._status_bar_installed = False

    def closeEvent(self, event: typing.Any) -> None:  # noqa: N802, ANN401 - Qt override
        """Shut down cleanly when the widget is closed."""
        self.shutdown()
        super().closeEvent(event)
