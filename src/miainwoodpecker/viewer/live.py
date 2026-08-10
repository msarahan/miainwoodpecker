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
import tempfile
import typing
from pathlib import Path

from qtpy import QtCore, QtWidgets

from miainwoodpecker.acquisition.live import LiveAcquisition
from miainwoodpecker.acquisition.sequence import camera_series, scan_series
from miainwoodpecker.devices.interface import ScanParameters
from miainwoodpecker.storage.nexus import write_frames
from miainwoodpecker.storage.session import RecordingJob

if typing.TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    import napari

    from miainwoodpecker.devices.interface import Camera, Frame, Scanner
    from miainwoodpecker.storage.session import Session

_DEFAULT_DISPLAY_INTERVAL_MS = 33
_SCAN_SIZES = (128, 256, 512)
_DEFAULT_SCAN_SIZE_INDEX = 1  # 256
_DEFAULT_DWELL_US = 1.0
_DEFAULT_FOV_NM = 15.0
_ANALYSIS_BURST_FRAME_COUNT = 5
_DEFAULT_RECORD_FRAME_COUNT = 10
_MAX_RECORD_FRAME_COUNT = 100000
_NO_SESSION_MESSAGE = "no session - data is not being kept"


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
        self._session: Session | None = None
        self._recording_job: RecordingJob | None = None
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
        layout.addWidget(self._build_scan_group())
        if self._camera is not None:
            layout.addWidget(self._build_camera_group())
        layout.addStretch(1)

    def _build_session_group(self) -> QtWidgets.QGroupBox:
        """Build the group showing where data goes and the session context."""
        session_group = QtWidgets.QGroupBox("Session", self)
        session_form = QtWidgets.QFormLayout(session_group)
        self._session_path_label = QtWidgets.QLabel(_NO_SESSION_MESSAGE, session_group)
        self._session_path_label.setWordWrap(True)
        session_form.addRow("Saving to", self._session_path_label)
        self._operator_edit = QtWidgets.QLineEdit(session_group)
        self._operator_edit.setPlaceholderText("who is on the instrument")
        session_form.addRow("Operator", self._operator_edit)
        self._sample_edit = QtWidgets.QLineEdit(session_group)
        self._sample_edit.setPlaceholderText("sample identifier")
        session_form.addRow("Sample", self._sample_edit)
        self._notes_edit = QtWidgets.QLineEdit(session_group)
        self._notes_edit.setPlaceholderText("free-text notes")
        session_form.addRow("Notes", self._notes_edit)
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

        for edit in (self._operator_edit, self._sample_edit, self._notes_edit):
            edit.editingFinished.connect(self._on_session_context_edited)
        self._cancel_record_button.clicked.connect(self.cancel_recording)
        return session_group

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

    def stop_scan(self) -> None:
        """Stop the live scan loop."""
        if self._scan_loop is not None:
            self._scan_loop.stop()
        self._scan_button.setText("Start scan")
        self._scan_status.setText("stopped")
        self._maybe_stop_timer()

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

    def stop_camera(self) -> None:
        """Stop the camera's live loop and pause the camera."""
        if self._camera_loop is not None:
            self._camera_loop.stop()
        if self._camera is not None:
            self._camera.stop()
        if self._camera is not None:
            self._camera_button.setText("Start camera")
            self._camera_status.setText("stopped")
        self._maybe_stop_timer()

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
            self._notes_edit.setText(session.notes)
        self._refresh_session_labels()

    @property
    def session(self) -> Session | None:
        """Return the session recordings are written into, if any."""
        return self._session

    def _on_session_context_edited(self) -> None:
        """Persist edited operator/sample/notes into the session."""
        if self._session is None:
            return
        self._session.update_context(
            operator=self._operator_edit.text(),
            sample=self._sample_edit.text(),
            notes=self._notes_edit.text(),
        )

    def _refresh_session_labels(self) -> None:
        """Show where data is going and what has been recorded so far."""
        if self._session is None:
            self._session_path_label.setText(_NO_SESSION_MESSAGE)
            self._recorded_label.setText("nothing recorded yet")
            return
        self._session_path_label.setText(str(self._session.root))
        recordings = self._session.recordings()
        if not recordings:
            self._recorded_label.setText("nothing recorded yet")
            return
        latest = recordings[-1]
        self._recorded_label.setText(
            f"{len(recordings)} file(s) - latest: {latest.path.name} "
            f"({latest.frame_count} frames)"
        )

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
        # over a single connection (migration plan, §6).
        self.stop_scan()
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
        self.stop_camera()
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
        self._recording_job = RecordingJob(self._session, frames, label=label)
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

    @contextlib.contextmanager
    def _burst_file(
        self,
        frames: list[Frame],
        *,
        label: str,
        title: str,
        filename: str,
    ) -> Iterator[Path]:
        """
        Write an analysis burst somewhere the operator can still get at it.

        With a session attached the burst becomes a real recording in it,
        so clicking an analysis button both analyzes *and* keeps the data.
        The Phase 4 buttons previously always wrote into a
        ``TemporaryDirectory`` that was deleted on the way out — exactly
        the "an operator cannot keep anything" problem sessions exist to
        fix. Without a session that original behaviour stands unchanged,
        so the widget still works as a pure live display and the analysis
        buttons still work without one.

        Parameters
        ----------
        frames : list[Frame]
            The already-acquired burst to persist.
        label : str
            Session label for the recording; unused without a session.
        title : str
            ``/entry/title`` for the written file.
        filename : str
            Temporary filename used when there is no session.

        Yields
        ------
        Path
            The written NeXus file, for the adapter to read back.
        """
        if self._session is not None:
            recording = self._session.record(frames, label=label, title=title)
            try:
                yield recording.path
            finally:
                self._refresh_session_labels()
        else:
            with tempfile.TemporaryDirectory() as tmp_dir:
                path = Path(tmp_dir) / filename
                write_frames(path, frames, title=title)
                yield path

    def _analyze_camera_in_hyperspy(self) -> None:
        """
        Round-trip a short camera burst through the HyperSpy adapter.

        Demonstrates the Phase 4 analysis-integration path end to end:
        stop the live camera loop (so this button and the loop never
        drive the same device at once), record a short burst straight to
        a temporary NeXus file with
        :func:`~miainwoodpecker.storage.nexus.write_frames`, read it back
        as a HyperSpy signal with
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

        if self._camera_loop is not None and self._camera_loop.is_running:
            self.stop_camera()

        self._analyze_status.setText("recording...")
        try:
            frames = list(camera_series(self._camera, _ANALYSIS_BURST_FRAME_COUNT))
            with self._burst_file(
                frames,
                label="hyperspy-burst",
                title="hyperspy analysis burst",
                filename="hyperspy_analysis_burst.nxs",
            ) as burst_path:
                signal = load_as_hyperspy_signal(burst_path)
                projection = signal.mean(axis=signal.axes_manager.navigation_axes[0])
        except Exception as exc:  # noqa: BLE001 - surfaced in the status label
            self._analyze_status.setText(f"error: {exc}")
            return

        self._viewer.add_image(
            projection.data,
            name="HyperSpy mean projection (Camera)",
            colormap="viridis",
        )
        self._analyze_status.setText(f"done - mean of {len(frames)} frames")

    def _analyze_camera_in_libertem(self) -> None:
        """
        Round-trip a short camera burst through the LiberTEM adapter.

        The second half of the Phase 4 analysis-integration path: stop the
        live camera loop if running, record a short burst to a temporary
        NeXus file with
        :func:`~miainwoodpecker.storage.nexus.write_frames`, read it back
        as a LiberTEM ``DataSet`` with
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

        if self._camera_loop is not None and self._camera_loop.is_running:
            self.stop_camera()

        self._libertem_status.setText("recording...")
        try:
            frames = list(camera_series(self._camera, _ANALYSIS_BURST_FRAME_COUNT))
            # Inline executor: this is a single UDF run over one small,
            # already-in-memory burst, not the large-dataset workload
            # LiberTEM's default dask executor is built for - spinning
            # up a local cluster per button click would be pure
            # overhead here.
            with (
                self._burst_file(
                    frames,
                    label="libertem-burst",
                    title="libertem analysis burst",
                    filename="libertem_analysis_burst.nxs",
                ) as burst_path,
                Context.make_with("inline") as ctx,
            ):
                dataset = load_as_libertem_dataset(ctx, burst_path)
                result = ctx.run_udf(dataset=dataset, udf=SumUDF())
                sum_projection = result["intensity"].data
        except Exception as exc:  # noqa: BLE001 - surfaced in the status label
            self._libertem_status.setText(f"error: {exc}")
            return

        self._viewer.add_image(
            sum_projection,
            name="LiberTEM sum projection (Camera)",
            colormap="viridis",
        )
        self._libertem_status.setText(f"done - sum of {len(frames)} frames")

    def _fit_central_disk_in_py4dstem(self) -> None:
        """
        Round-trip one real camera frame through the py4DSTEM adapter.

        Demonstrates the py4DSTEM follow-up to Phase 4 (migration plan,
        §5) end to end: stop the live camera loop if running, acquire one
        real frame via :func:`~miainwoodpecker.acquisition.sequence.camera_series`,
        write it to a temporary NeXus file with
        :func:`~miainwoodpecker.storage.nexus.write_frames`, read it back
        as a py4DSTEM ``DiffractionSlice`` with
        :func:`~miainwoodpecker.analysis.py4dstem_bridge.load_as_diffraction_slice`,
        run one real py4DSTEM operation on that single diffraction pattern
        (``py4DSTEM.process.calibration.get_probe_size``, the same
        central-disk fit py4DSTEM runs per-pattern inside a full
        datacube), and push both the analyzed frame and a napari ``Shapes``
        ellipse at the fitted disk into the viewer. Only a single frame is
        used, not a scan-position-indexed cube - see
        :mod:`miainwoodpecker.analysis.py4dstem_bridge` for why that cube
        isn't available yet. Requires the ``py4dstem`` optional dependency
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

        if self._camera_loop is not None and self._camera_loop.is_running:
            self.stop_camera()

        self._py4dstem_status.setText("acquiring...")
        try:
            (frame,) = camera_series(self._camera, 1)
            with self._burst_file(
                [frame],
                label="py4dstem-frame",
                title="py4DSTEM analysis frame",
                filename="py4dstem_analysis_frame.nxs",
            ) as frame_path:
                diffraction_slice = load_as_diffraction_slice(frame_path)
                radius, x0, y0 = get_probe_size(diffraction_slice.data)
        except Exception as exc:  # noqa: BLE001 - surfaced in the status label
            self._py4dstem_status.setText(f"error: {exc}")
            return

        self._viewer.add_image(
            diffraction_slice.data,
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
        self._py4dstem_status.setText(
            f"done - r={radius:.1f}px center=({x0:.1f}, {y0:.1f})"
        )

    def _maybe_stop_timer(self) -> None:
        scan_running = self._scan_loop is not None and self._scan_loop.is_running
        camera_running = (
            self._camera_loop is not None and self._camera_loop.is_running
        )
        recording = self._recording_job is not None and self._recording_job.is_running
        if not scan_running and not camera_running and not recording:
            self._timer.stop()

    def refresh_display(self) -> None:
        """Push the newest frames into napari layers; called by the display timer."""
        self._poll_recording()
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
            return
        frame = loop.latest()
        if frame is None:
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
        if loop.is_running:
            status_label.setText(f"running - {loop.stats.fps:.1f} fps")

    def shutdown(self) -> None:
        """Stop all loops, the camera, the display timer, and any recording."""
        self._timer.stop()
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
