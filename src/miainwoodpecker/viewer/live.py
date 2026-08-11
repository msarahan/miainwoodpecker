"""
Napari dock widget for the live "look at the sample, adjust settings" loop.

One :class:`~miainwoodpecker.acquisition.live.LiveAcquisition` per source
(scan, camera) grabs frames on worker threads; a single QTimer on the GUI
thread polls their latest frames at display rate and pushes them into
napari image layers. Acquisition rate and display rate are fully
decoupled — a slow display skips frames, and a fast source never floods
the UI with events.

Thread-safety contract: Qt widgets are only touched from the GUI thread.
Scan settings are written to a plain tuple attribute by the GUI thread
whenever a control changes, and the worker thread only reads that
attribute — no Qt access from workers. Recording obeys the same contract
from the other direction: a
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

Importing this module requires the ``viewer`` optional dependency group.
The camera group's "Analyze in HyperSpy", "Sum in LiberTEM", and "Fit
central disk (py4DSTEM)" buttons additionally need the ``analysis``,
``libertem``, and ``py4dstem`` groups respectively (migration plan,
Phase 4); all three libraries are imported lazily so this module still
imports and the buttons still render without them, only reporting the
missing extra in the status label if clicked.
"""

from __future__ import annotations

import contextlib
import dataclasses
import tempfile
import typing
from pathlib import Path

from qtpy import QtCore, QtWidgets

from miainwoodpecker.acquisition.live import LiveAcquisition
from miainwoodpecker.acquisition.sequence import camera_series, scan_series
from miainwoodpecker.devices.interface import ScanParameters
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
from miainwoodpecker.viewer.jobs import AnalysisJob

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    import napari

    from miainwoodpecker.devices.interface import Camera, Frame, Scanner
    from miainwoodpecker.storage.session import LoadedRecording, Recording

_DEFAULT_DISPLAY_INTERVAL_MS = 33
_SCAN_SIZES = (128, 256, 512)
_DEFAULT_SCAN_SIZE_INDEX = 1  # 256
_DEFAULT_DWELL_US = 1.0
_DEFAULT_FOV_NM = 15.0
_ANALYSIS_BURST_FRAME_COUNT = 5
_DEFAULT_RECORD_FRAME_COUNT = 10
_MAX_RECORD_FRAME_COUNT = 100000
_NO_SESSION_MESSAGE = "no session - data is not being kept"
# Shown when a live loop would not release the device in time. Refusing is
# deliberate: driving a device from two threads corrupts frames silently
# rather than raising (docs/architecture-review.md, §1.2).
_SCANNER_BUSY_MESSAGE = "scanner still busy - live scan did not stop, try again"
_CAMERA_BUSY_MESSAGE = "camera still busy - live loop did not stop, try again"
_NOTES_HEIGHT_PX = 64
# Long enough that typing a sentence writes the sidecar once, short enough
# that an operator who types and immediately clicks Record has their note.
_CONTEXT_SAVE_DELAY_MS = 750
_NEXUS_FILE_FILTER = "NeXus recordings (*.nxs *.h5 *.hdf5);;All files (*)"
_SINGLE_PATTERN_NDIM = 2


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
    """

    path: Path
    frame_count: int
    origin: str


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
    Dock widget with live scan (and optionally camera) view controls.

    A session is attached with :meth:`set_session` rather than passed
    here: where data goes is operator-editable state that can change
    during a run (a new directory for the afternoon's sample), not a
    construction-time dependency like the devices. Without a session the
    widget still works as a live display, and the recording controls say
    so instead of pretending to save anything.

    Parameters
    ----------
    viewer : napari.Viewer
        The napari viewer whose layers display the live frames.
    scanner : Scanner
        The scan device to drive.
    camera : Camera | None
        An optional camera to offer a live view for (e.g. Ronchigram).
    display_interval_ms : int
        How often the display polls for new frames.
    parent : QtWidgets.QWidget | None
        Optional Qt parent widget.
    """

    def __init__(
        self,
        viewer: napari.Viewer,
        scanner: Scanner,
        *,
        camera: Camera | None = None,
        display_interval_ms: int = _DEFAULT_DISPLAY_INTERVAL_MS,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewer = viewer
        self._scanner = scanner
        self._camera = camera
        self._scan_loop: LiveAcquisition | None = None
        self._camera_loop: LiveAcquisition | None = None
        # Newest frame already pushed into each napari layer, so a display
        # tick that finds nothing new can skip the upload entirely. Holds
        # a reference for identity comparison only; the array itself is
        # the layer's, not a second copy.
        self._displayed: dict[str, Frame] = {}
        self._session: Session | None = None
        self._recording_job: RecordingJob | None = None
        self._load_job: LoadJob | None = None
        self._analysis_job: AnalysisJob | None = None
        # Set together with the job, and only read by _poll_analysis, so the
        # result of whichever button started it lands in that button's own
        # status label and layers. One job at a time is enforced in
        # _start_analysis; all three buttons share the device.
        self._analysis_status: QtWidgets.QLabel | None = None
        self._analysis_display: (
            Callable[[object, _AnalysisInput], str] | None
        ) = None
        self._opened_file: Path | None = None
        self._scan_request: tuple[ScanParameters, int, str] = (
            ScanParameters(
                height=_SCAN_SIZES[_DEFAULT_SCAN_SIZE_INDEX],
                width=_SCAN_SIZES[_DEFAULT_SCAN_SIZE_INDEX],
                pixel_time_us=_DEFAULT_DWELL_US,
                fov_nm=_DEFAULT_FOV_NM,
            ),
            0,
            scanner.channel_names[0],
        )
        self._build_ui()
        self._on_scan_settings_changed()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(display_interval_ms)
        self._timer.timeout.connect(self.refresh_display)

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._build_session_group())
        layout.addWidget(self._build_recordings_group())
        layout.addWidget(self._build_scan_group())
        if self._camera is not None:
            layout.addWidget(self._build_camera_group())
        layout.addStretch(1)

    def _build_recordings_group(self) -> QtWidgets.QGroupBox:
        """
        Build the group for looking at data already on disk.

        The combo lists the current session's recordings; "Open from
        disk..." reaches any file, including one recorded on another machine
        or in a session that has since been closed. The checkbox is how the
        three Phase 4 analysis buttons are pointed at a file instead of a
        fresh burst — one switch rather than three more buttons, since the
        choice is "what do I analyze", not "what analysis".

        Returns
        -------
        QtWidgets.QGroupBox
            The assembled group.
        """
        group = QtWidgets.QGroupBox("Recordings", self)
        form = QtWidgets.QFormLayout(group)
        self._recording_combo = QtWidgets.QComboBox(group)
        self._recording_combo.setPlaceholderText("no recordings in this session")
        form.addRow("File", self._recording_combo)
        self._all_sessions_check = QtWidgets.QCheckBox(
            "List every session in the parent directory", group
        )
        form.addRow(self._all_sessions_check)
        self._open_recording_button = QtWidgets.QPushButton("Open selected", group)
        form.addRow(self._open_recording_button)
        self._open_file_button = QtWidgets.QPushButton("Open from disk...", group)
        form.addRow(self._open_file_button)
        self._analyze_from_file_check = QtWidgets.QCheckBox(
            "Analysis buttons use the opened file, not a fresh burst", group
        )
        form.addRow(self._analyze_from_file_check)
        self._load_status = QtWidgets.QLabel("nothing opened yet", group)
        self._load_status.setWordWrap(True)
        form.addRow("Opened", self._load_status)
        self._annotation_edit = QtWidgets.QLineEdit(group)
        self._annotation_edit.setPlaceholderText("note to add to the opened recording")
        form.addRow("Add note", self._annotation_edit)
        self._annotate_button = QtWidgets.QPushButton("Annotate opened", group)
        form.addRow(self._annotate_button)

        self._open_recording_button.clicked.connect(self.open_selected_recording)
        self._open_file_button.clicked.connect(self.choose_and_open_recording)
        self._all_sessions_check.toggled.connect(self._refresh_session_labels)
        self._annotate_button.clicked.connect(self.annotate_opened_recording)
        self._annotation_edit.returnPressed.connect(self.annotate_opened_recording)
        return group

    def _build_session_group(self) -> QtWidgets.QGroupBox:
        """Build the group showing where data goes and the session context."""
        session_group = QtWidgets.QGroupBox("Session", self)
        session_form = QtWidgets.QFormLayout(session_group)
        self._session_path_label = QtWidgets.QLabel(_NO_SESSION_MESSAGE, session_group)
        self._session_path_label.setWordWrap(True)
        session_form.addRow("Saving to", self._session_path_label)
        self._change_session_button = QtWidgets.QPushButton(
            "Change directory...", session_group
        )
        session_form.addRow(self._change_session_button)
        self._build_session_context_rows(session_group, session_form)
        self._space_label = QtWidgets.QLabel("", session_group)
        self._space_label.setWordWrap(True)
        session_form.addRow("Disk", self._space_label)
        self._recorded_label = QtWidgets.QLabel("nothing recorded yet", session_group)
        self._recorded_label.setWordWrap(True)
        session_form.addRow("Recorded", self._recorded_label)
        self._recording_status = QtWidgets.QLabel("idle", session_group)
        self._recording_status.setWordWrap(True)
        session_form.addRow("Recording", self._recording_status)
        self._cancel_record_button = QtWidgets.QPushButton(
            "Stop recording", session_group
        )
        self._cancel_record_button.setEnabled(False)
        session_form.addRow(self._cancel_record_button)

        self._change_session_button.clicked.connect(self.change_session_directory)
        self._cancel_record_button.clicked.connect(self.cancel_recording)
        return session_group

    def _build_session_context_rows(
        self,
        parent: QtWidgets.QWidget,
        form: QtWidgets.QFormLayout,
    ) -> None:
        """
        Add the operator/sample/notes fields, and the next recording's note.

        Notes are multi-line: a single-line field was enough to prove the
        wiring and not enough for a shift's worth of observations. Two
        scopes, because they answer different questions — "Session notes"
        is the shift's standing context and is written to every subsequent
        recording, while "Note for next recording" describes the individual
        file (see :meth:`~miainwoodpecker.storage.session.Session.record`).

        ``QPlainTextEdit`` has no ``editingFinished`` signal, so a
        single-shot timer debounces ``textChanged`` instead: typing a
        paragraph should not rewrite ``session.json`` once per keystroke.

        Parameters
        ----------
        parent : QtWidgets.QWidget
            The group box owning the new widgets.
        form : QtWidgets.QFormLayout
            The group's layout, appended to.
        """
        self._operator_edit = QtWidgets.QLineEdit(parent)
        self._operator_edit.setPlaceholderText("who is on the instrument")
        form.addRow("Operator", self._operator_edit)
        self._sample_edit = QtWidgets.QLineEdit(parent)
        self._sample_edit.setPlaceholderText("sample identifier")
        form.addRow("Sample", self._sample_edit)
        self._notes_edit = QtWidgets.QPlainTextEdit(parent)
        self._notes_edit.setPlaceholderText("notes for the whole session")
        self._notes_edit.setMaximumHeight(_NOTES_HEIGHT_PX)
        form.addRow("Session notes", self._notes_edit)
        self._recording_note_edit = QtWidgets.QPlainTextEdit(parent)
        self._recording_note_edit.setPlaceholderText(
            "what this next recording is - kept until you change it"
        )
        self._recording_note_edit.setMaximumHeight(_NOTES_HEIGHT_PX)
        form.addRow("Note for next recording", self._recording_note_edit)

        self._context_save_timer = QtCore.QTimer(self)
        self._context_save_timer.setSingleShot(True)
        self._context_save_timer.setInterval(_CONTEXT_SAVE_DELAY_MS)
        self._context_save_timer.timeout.connect(self._on_session_context_edited)
        for edit in (self._operator_edit, self._sample_edit):
            edit.editingFinished.connect(self._on_session_context_edited)
        self._notes_edit.textChanged.connect(self._context_save_timer.start)

    def _build_scan_group(self) -> QtWidgets.QGroupBox:
        scan_group = QtWidgets.QGroupBox("Scan", self)
        scan_form = QtWidgets.QFormLayout(scan_group)
        self._channel_combo = QtWidgets.QComboBox(scan_group)
        self._channel_combo.addItems(list(self._scanner.channel_names))
        scan_form.addRow("Channel", self._channel_combo)
        self._size_combo = QtWidgets.QComboBox(scan_group)
        self._size_combo.addItems([str(size) for size in _SCAN_SIZES])
        self._size_combo.setCurrentIndex(_DEFAULT_SCAN_SIZE_INDEX)
        scan_form.addRow("Size (px)", self._size_combo)
        self._dwell_spin = QtWidgets.QDoubleSpinBox(scan_group)
        self._dwell_spin.setRange(0.1, 1000.0)
        self._dwell_spin.setValue(_DEFAULT_DWELL_US)
        self._dwell_spin.setSuffix(" µs")
        scan_form.addRow("Dwell", self._dwell_spin)
        self._fov_spin = QtWidgets.QDoubleSpinBox(scan_group)
        self._fov_spin.setRange(0.1, 100000.0)
        self._fov_spin.setValue(_DEFAULT_FOV_NM)
        self._fov_spin.setSuffix(" nm")
        scan_form.addRow("FOV", self._fov_spin)
        self._scan_button = QtWidgets.QPushButton("Start scan", scan_group)
        scan_form.addRow(self._scan_button)
        self._scan_status = QtWidgets.QLabel("stopped", scan_group)
        scan_form.addRow("Status", self._scan_status)
        (
            self._scan_count_spin,
            self._scan_save_button,
            self._scan_record_button,
        ) = self._build_record_controls(scan_group, scan_form)

        self._channel_combo.currentIndexChanged.connect(self._on_scan_settings_changed)
        self._size_combo.currentIndexChanged.connect(self._on_scan_settings_changed)
        self._dwell_spin.valueChanged.connect(self._on_scan_settings_changed)
        self._fov_spin.valueChanged.connect(self._on_scan_settings_changed)
        self._scan_button.clicked.connect(self._toggle_scan)
        self._scan_save_button.clicked.connect(self.save_scan_frame)
        self._scan_record_button.clicked.connect(self.record_scan_frames)
        return scan_group

    def _build_record_controls(
        self,
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

    def _build_camera_group(self) -> QtWidgets.QGroupBox:
        camera_group = QtWidgets.QGroupBox("Camera", self)
        camera_form = QtWidgets.QFormLayout(camera_group)
        self._camera_button = QtWidgets.QPushButton("Start camera", camera_group)
        camera_form.addRow(self._camera_button)
        self._camera_status = QtWidgets.QLabel("stopped", camera_group)
        camera_form.addRow("Status", self._camera_status)
        (
            self._camera_count_spin,
            self._camera_save_button,
            self._camera_record_button,
        ) = self._build_record_controls(camera_group, camera_form)
        self._analyze_button = QtWidgets.QPushButton(
            "Analyze in HyperSpy", camera_group
        )
        camera_form.addRow(self._analyze_button)
        self._analyze_status = QtWidgets.QLabel("", camera_group)
        camera_form.addRow("Analysis", self._analyze_status)
        self._libertem_button = QtWidgets.QPushButton("Sum in LiberTEM", camera_group)
        camera_form.addRow(self._libertem_button)
        self._libertem_status = QtWidgets.QLabel("", camera_group)
        camera_form.addRow("LiberTEM", self._libertem_status)
        self._py4dstem_button = QtWidgets.QPushButton(
            "Fit central disk (py4DSTEM)", camera_group
        )
        camera_form.addRow(self._py4dstem_button)
        self._py4dstem_status = QtWidgets.QLabel("", camera_group)
        camera_form.addRow("py4DSTEM", self._py4dstem_status)

        self._camera_button.clicked.connect(self._toggle_camera)
        self._camera_save_button.clicked.connect(self.save_camera_frame)
        self._camera_record_button.clicked.connect(self.record_camera_frames)
        self._analyze_button.clicked.connect(self._analyze_camera_in_hyperspy)
        self._libertem_button.clicked.connect(self._analyze_camera_in_libertem)
        self._py4dstem_button.clicked.connect(self._fit_central_disk_in_py4dstem)
        return camera_group

    def _on_scan_settings_changed(self) -> None:
        size = int(self._size_combo.currentText())
        channel_index = self._channel_combo.currentIndex()
        self._scan_request = (
            ScanParameters(
                height=size,
                width=size,
                pixel_time_us=self._dwell_spin.value(),
                fov_nm=self._fov_spin.value(),
            ),
            channel_index,
            self._channel_combo.currentText(),
        )

    def _grab_scan(self) -> Frame:
        # Runs on the worker thread: reads the request tuple, never Qt state.
        parameters, channel_index, _ = self._scan_request
        return self._scanner.scan_frame(parameters, channel_index)

    def _toggle_scan(self) -> None:
        if self._scan_loop is not None and self._scan_loop.is_running:
            self.stop_scan()
        else:
            self.start_scan()

    def _toggle_camera(self) -> None:
        if self._camera_loop is not None and self._camera_loop.is_running:
            self.stop_camera()
        else:
            self.start_camera()

    def start_scan(self) -> None:
        """Start the live scan loop and the display timer."""
        if self._scan_loop is not None and self._scan_loop.is_running:
            return
        self._scan_loop = LiveAcquisition(self._grab_scan)
        self._scan_loop.start()
        self._scan_button.setText("Stop scan")
        self._scan_status.setText("running")
        self._timer.start()

    def stop_scan(self) -> bool:
        """
        Stop the live scan loop.

        Returns
        -------
        bool
            True if the worker actually finished. False means a grab is
            still in flight and the scanner is still in use — callers
            about to drive the scanner themselves must not proceed.
        """
        stopped = True
        if self._scan_loop is not None:
            stopped = self._scan_loop.stop()
        if stopped:
            self._scan_button.setText("Start scan")
            self._scan_status.setText("stopped")
            self._maybe_stop_timer()
        else:
            self._scan_status.setText("still finishing a scan - try again")
        return stopped

    def start_camera(self) -> None:
        """Start the camera and its live loop and the display timer."""
        if self._camera is None:
            return
        if self._camera_loop is not None and self._camera_loop.is_running:
            return
        self._camera.start()
        self._camera_loop = LiveAcquisition(self._camera.acquire_frame)
        self._camera_loop.start()
        self._camera_button.setText("Stop camera")
        self._camera_status.setText("running")
        self._timer.start()

    def stop_camera(self) -> bool:
        """
        Stop the camera's live loop and pause the camera.

        Returns
        -------
        bool
            True if the worker actually finished. False means an exposure
            is still in flight and the camera is still in use; the camera
            is left running rather than stopped underneath it.
        """
        stopped = True
        if self._camera_loop is not None:
            stopped = self._camera_loop.stop()
        if not stopped:
            self._camera_status.setText("still finishing an exposure - try again")
            return False
        if self._camera is not None:
            self._camera.stop()
            self._camera_button.setText("Start camera")
            self._camera_status.setText("stopped")
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
        """Show where data is going and what has been recorded so far."""
        if self._session is None:
            self._session_path_label.setText(_NO_SESSION_MESSAGE)
            self._recorded_label.setText("nothing recorded yet")
            self._refresh_recording_choices([])
            return
        self._session_path_label.setText(str(self._session.root))
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
            self._space_label.setText("")
            return
        free = free_space(self._session.root)
        frames = self._scan_count_spin.value()
        planned = estimate_size(self._scan_request[0].shape, frames)
        text = f"{format_bytes(free)} free"
        if free and planned > free:
            # An upper bound compared against the real number: erring high
            # here means warning slightly early, which is the right way to
            # be wrong about running out of disk mid-acquisition.
            text += (
                f" - warning: {frames} scan frames need up to "
                f"{format_bytes(planned)}"
            )
        self._space_label.setText(text)

    def save_scan_frame(self) -> None:
        """Save the scan frame currently on screen into the session."""
        label = f"scan-{self._scan_request[2]}-frame"
        self._save_displayed_frame(self._scan_loop, label)

    def save_camera_frame(self) -> None:
        """Save the camera frame currently on screen into the session."""
        if self._camera is None:
            return
        self._save_displayed_frame(self._camera_loop, "camera-frame")

    def _save_displayed_frame(self, loop: LiveAcquisition | None, label: str) -> None:
        """
        Record the newest frame from a live loop as a one-frame file.

        This needs no device access — the frame is already in hand from
        the live loop — so unlike :meth:`record_scan_frames` it leaves the
        loop running and the operator keeps looking at the sample.
        """
        frame = loop.latest() if loop is not None else None
        if frame is None:
            self._recording_status.setText("no frame on screen yet")
            return
        self._start_recording([frame], label)

    def record_scan_frames(self) -> None:
        """Record the requested number of scan frames into the session."""
        if self._session is None:
            self._recording_status.setText(_NO_SESSION_MESSAGE)
            return
        # One driver per device: the live loop and the recording would
        # otherwise call scan_frame from two threads at once, and the
        # device RPC protocol is strictly synchronous request/response
        # over a single connection (migration plan, §6). Refusing when the
        # loop did not actually stop is the point of checking: starting
        # anyway is what tears a frame in half across the reused
        # shared-memory segment.
        if not self.stop_scan():
            self._recording_status.setText(_SCANNER_BUSY_MESSAGE)
            return
        parameters, channel_index, channel_name = self._scan_request
        self._start_recording(
            scan_series(
                self._scanner,
                parameters,
                self._scan_count_spin.value(),
                channel=channel_index,
            ),
            f"scan-{channel_name}",
        )

    def record_camera_frames(self) -> None:
        """Record the requested number of camera frames into the session."""
        if self._camera is None:
            return
        if self._session is None:
            self._recording_status.setText(_NO_SESSION_MESSAGE)
            return
        # Same one-driver-per-device rule as record_scan_frames; camera_series
        # starts and stops the camera around the series itself.
        if not self.stop_camera():
            self._recording_status.setText(_CAMERA_BUSY_MESSAGE)
            return
        self._start_recording(
            camera_series(self._camera, self._camera_count_spin.value()), "camera"
        )

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
        data = loaded.data[0] if loaded.data.shape[0] == 1 else loaded.data
        name = f"File: {recording.path.name}"
        if name in self._viewer.layers:
            del self._viewer.layers[name]
        self._viewer.add_image(data, name=name, colormap="gray")
        parts = [_condition(recording)]
        if loaded.truncated:
            parts.append(
                f"showing the first {loaded.data.shape[0]} - the rest exceeded "
                f"this app's in-memory read budget"
            )
        self._load_status.setText(f"{recording.path.name}: {' - '.join(parts)}")

    def _analysis_file(self) -> Path | None:
        """Return the on-disk file the analysis buttons should use, if any."""
        if not self._analyze_from_file_check.isChecked():
            return None
        return self._opened_file

    @contextlib.contextmanager
    def _analysis_input(  # noqa: PLR0913 - keyword-only inputs, not a call signature
        self,
        *,
        frame_count: int,
        label: str,
        title: str,
        filename: str,
        existing: Path | None,
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
            yield _AnalysisInput(
                path=existing,
                frame_count=described.frame_count,
                origin=existing.name,
            )
            return

        if self._camera is None:  # pragma: no cover - callers check first
            msg = "no camera to acquire from and no file opened"
            raise RecordingReadError(msg)
        frames = list(camera_series(self._camera, frame_count))
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
        Only ``compute`` crosses over, and it is handed plain data.

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
        if (
            self._camera_loop is not None
            and self._camera_loop.is_running
            and not self.stop_camera()
        ):
            # Refusing is the point rather than pessimism: an exposure
            # still in flight means the live loop has not released the
            # camera, and starting the burst anyway would drive one device
            # from two threads - which tears a frame across the reused
            # shared-memory segment instead of raising
            # (docs/architecture-review.md, §1.2).
            status.setText(_CAMERA_BUSY_MESSAGE)
            return

        existing = self._analysis_file()
        note = self._note_for_next_recording()

        def work() -> _AnalysisOutcome:
            with self._analysis_input(
                frame_count=frame_count,
                label=label,
                title=title,
                filename=filename,
                existing=existing,
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

    def _analyze_camera_in_hyperspy(self) -> None:
        """
        Round-trip a short camera burst through the HyperSpy adapter.

        Demonstrates the Phase 4 analysis-integration path end to end:
        stop the live camera loop (so this button and the loop never
        drive the same device at once), get a NeXus file to analyze from
        :meth:`_analysis_input` — a fresh burst, or a recording already on
        disk if the operator opened one and ticked the Recordings
        checkbox — read it back as a HyperSpy signal with
        :func:`~miainwoodpecker.analysis.hyperspy_bridge.load_as_hyperspy_signal`,
        run one real HyperSpy operation
        (:meth:`hyperspy.signals.Signal2D.mean` over the frame axis), and
        push the result into napari as a new image layer. Requires the
        ``analysis`` optional dependency group; reports that in the
        status label rather than crashing the widget if it is missing.
        """
        if self._camera is None:
            return
        try:
            from miainwoodpecker.analysis.hyperspy_bridge import (  # noqa: PLC0415
                load_as_hyperspy_signal,
            )
        except ImportError:
            self._analyze_status.setText("install the 'analysis' extra")
            return

        def compute(source: _AnalysisInput) -> object:
            signal = load_as_hyperspy_signal(source.path)
            return signal.mean(axis=signal.axes_manager.navigation_axes[0]).data

        def display(payload: object, source: _AnalysisInput) -> str:
            self._viewer.add_image(
                payload,
                name="HyperSpy mean projection (Camera)",
                colormap="viridis",
            )
            return f"done - mean of {source.frame_count} frames from {source.origin}"

        self._start_analysis(
            status=self._analyze_status,
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
        checkbox), read it back as a LiberTEM ``DataSet`` with
        :func:`~miainwoodpecker.analysis.libertem_bridge.load_as_libertem_dataset`,
        run one real LiberTEM UDF (``libertem.udf.sum.SumUDF``, summing
        across the frame/navigation axis) with an inline ``Context``, and
        push the result into napari as a new image layer. Requires the
        ``libertem`` optional dependency group; reports that in the status
        label rather than crashing the widget if it is missing.
        """
        if self._camera is None:
            return
        try:
            from libertem.api import Context  # noqa: PLC0415
            from libertem.udf.sum import SumUDF  # noqa: PLC0415

            from miainwoodpecker.analysis.libertem_bridge import (  # noqa: PLC0415
                load_as_libertem_dataset,
            )
        except ImportError:
            self._libertem_status.setText("install the 'libertem' extra")
            return

        def compute(source: _AnalysisInput) -> object:
            # Inline executor: this is a single UDF run over one small,
            # already-in-memory burst, not the large-dataset workload
            # LiberTEM's default dask executor is built for - spinning
            # up a local cluster per button click would be pure
            # overhead here.
            with Context.make_with("inline") as ctx:
                dataset = load_as_libertem_dataset(ctx, source.path)
                result = ctx.run_udf(dataset=dataset, udf=SumUDF())
                return result["intensity"].data

        def display(payload: object, source: _AnalysisInput) -> str:
            self._viewer.add_image(
                payload,
                name="LiberTEM sum projection (Camera)",
                colormap="viridis",
            )
            return f"done - sum of {source.frame_count} frames from {source.origin}"

        self._start_analysis(
            status=self._libertem_status,
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
        the Recordings checkbox — read it back
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
        if self._camera is None:
            return
        try:
            from py4DSTEM.process.calibration import get_probe_size  # noqa: PLC0415

            from miainwoodpecker.analysis.py4dstem_bridge import (  # noqa: PLC0415
                load_as_diffraction_slice,
            )
        except ImportError:
            self._py4dstem_status.setText("install the 'py4dstem' extra")
            return

        def compute(source: _AnalysisInput) -> object:
            diffraction_slice = load_as_diffraction_slice(source.path)
            stack = diffraction_slice.data
            pattern = stack if stack.ndim == _SINGLE_PATTERN_NDIM else stack[0]
            return (pattern, *get_probe_size(pattern))

        def display(payload: object, source: _AnalysisInput) -> str:
            pattern, radius, x0, y0 = typing.cast(
                "tuple[typing.Any, float, float, float]", payload
            )
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
            )
            return (
                f"done - r={radius:.1f}px center=({x0:.1f}, {y0:.1f}) "
                f"from {source.origin}"
            )

        self._start_analysis(
            status=self._py4dstem_status,
            compute=compute,
            display=display,
            frame_count=1,
            label="py4dstem-frame",
            title="py4DSTEM analysis frame",
            filename="py4dstem_analysis_frame.nxs",
        )

    def _maybe_stop_timer(self) -> None:
        scan_running = self._scan_loop is not None and self._scan_loop.is_running
        camera_running = (
            self._camera_loop is not None and self._camera_loop.is_running
        )
        recording = self._recording_job is not None and self._recording_job.is_running
        loading = self._load_job is not None and self._load_job.is_running
        analyzing = self._analysis_job is not None
        if (
            not scan_running
            and not camera_running
            and not recording
            and not loading
            and not analyzing
        ):
            self._timer.stop()

    def refresh_display(self) -> None:
        """Push the newest frames into napari layers; called by the display timer."""
        self._poll_recording()
        self._poll_load()
        self._poll_analysis()
        if self._scan_loop is not None:
            self._refresh_source(
                self._scan_loop,
                layer_name=f"Scan ({self._scan_request[2]})",
                status_label=self._scan_status,
                autocontrast_every_frame=True,
            )
        if self._camera_loop is not None:
            self._refresh_source(
                self._camera_loop,
                layer_name="Camera",
                status_label=self._camera_status,
                autocontrast_every_frame=False,
            )

    def _refresh_source(
        self,
        loop: LiveAcquisition,
        *,
        layer_name: str,
        status_label: QtWidgets.QLabel,
        autocontrast_every_frame: bool,
    ) -> None:
        if loop.error is not None:
            status_label.setText(f"error: {loop.error}")
            # A failed grab stops the worker but sets no stop event, so
            # without this the timer would run forever, reformatting the
            # same exception 30 times a second at full display cost.
            self._maybe_stop_timer()
            return
        frame = loop.latest()
        if frame is None:
            return
        if frame is self._displayed.get(layer_name) and layer_name in (
            self._viewer.layers
        ):
            # Nothing new since the last tick. The display timer is fixed
            # at 33 ms while acquisition runs at whatever the device
            # manages, so most ticks see the frame they already drew:
            # assigning layer.data schedules a GPU re-upload and the
            # autocontrast pass walks the whole array twice, both for
            # pixels that did not change. Identity is the right test -
            # the loop hands out the same object until it grabs another.
            self._refresh_rate_label(loop, status_label)
            return
        if layer_name in self._viewer.layers:
            layer = self._viewer.layers[layer_name]
            layer.data = frame.data
            if autocontrast_every_frame:
                low = float(frame.data.min())
                high = float(frame.data.max())
                if high > low:
                    layer.contrast_limits = (low, high)
        else:
            self._viewer.add_image(frame.data, name=layer_name, colormap="gray")
        self._displayed[layer_name] = frame
        self._refresh_rate_label(loop, status_label)

    def _refresh_rate_label(
        self,
        loop: LiveAcquisition,
        status_label: QtWidgets.QLabel,
    ) -> None:
        """
        Show the acquisition rate, rewriting the label only when it changes.

        ``setText`` with identical text still costs a Qt repaint, and this
        runs for every source on every tick; the rate only moves in the
        first decimal a few times a second.
        """
        if not loop.is_running:
            return
        text = f"running - {loop.stats.fps:.1f} fps"
        if status_label.text() != text:
            status_label.setText(text)

    def shutdown(self) -> None:
        """Stop all loops, the camera, the display timer, and any recording."""
        self._timer.stop()
        self._context_save_timer.stop()
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
        self.stop_scan()
        self.stop_camera()

    def closeEvent(self, event: typing.Any) -> None:  # noqa: N802, ANN401 - Qt override
        """Shut down cleanly when the widget is closed."""
        self.shutdown()
        super().closeEvent(event)
