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
from miainwoodpecker.viewer.panels import devices as devices_panel
from miainwoodpecker.viewer.panels import instrument as instrument_panel
from miainwoodpecker.viewer.panels import recordings as recordings_panel
from miainwoodpecker.viewer.panels import session as session_panel
from miainwoodpecker.viewer.panels.defaults import (
    _DEFAULT_DWELL_US,
    _DEFAULT_FOV_NM,
    _DEFAULT_SCAN_SIZE_INDEX,
    _NO_SESSION_MESSAGE,
    _SCAN_SIZES,
)

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    import napari

    from miainwoodpecker.analysis.operations import AnalysisInput
    from miainwoodpecker.analysis.remote import AnalysisRunner
    from miainwoodpecker.devices.interface import (
        Camera,
        Frame,
        Instrument,
        Scanner,
    )
    from miainwoodpecker.storage.nexus import FrameStack
    from miainwoodpecker.storage.session import LoadedRecording, Recording

_DEFAULT_DISPLAY_INTERVAL_MS = 33
_ANALYSIS_BURST_FRAME_COUNT = 5
# Shown when a live loop would not release the device in time. Refusing is
# deliberate: driving a device from two threads corrupts frames silently
# rather than raising (docs/architecture-review.md, §1.2).
_SCANNER_BUSY_MESSAGE = "scanner still busy - live scan did not stop, try again"
_CAMERA_BUSY_MESSAGE = "camera still busy - live loop did not stop, try again"
# Long enough that typing a sentence writes the sidecar once, short enough
# that an operator who types and immediately clicks Record has their note.
_NEXUS_FILE_FILTER = "NeXus recordings (*.nxs *.h5 *.hdf5);;All files (*)"


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
        The target name the server serves this camera on.
    camera : Camera
        The device itself.
    layer_name : str
        The napari layer its frames are pushed into. One layer per
        camera, so two live cameras do not overwrite each other.
    loop : LiveAcquisition | None
        Its live-acquisition loop while running, None otherwise.
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
    """

    name: str
    camera: Camera
    layer_name: str
    loop: LiveAcquisition | None = None
    button: QtWidgets.QPushButton | None = None
    status: QtWidgets.QLabel | None = None
    count_spin: QtWidgets.QSpinBox | None = None
    save_button: QtWidgets.QPushButton | None = None
    record_button: QtWidgets.QPushButton | None = None


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
        The scan device to drive, or None for a detector-only instrument.
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
    display_interval_ms : int
        How often the display polls for new frames.
    parent : QtWidgets.QWidget | None
        Optional Qt parent widget.

    Raises
    ------
    ValueError
        If neither a scanner nor a camera is given. There would be
        nothing to show and nothing to record, and every control would
        be disabled — a window worth refusing to build rather than
        opening empty.
    """

    def __init__(  # noqa: PLR0913 - all but viewer/scanner are keyword-only
        self,
        viewer: napari.Viewer,
        scanner: Scanner | None,
        *,
        camera: Camera | None = None,
        cameras: typing.Mapping[str, Camera] | None = None,
        instrument: Instrument | None = None,
        display_interval_ms: int = _DEFAULT_DISPLAY_INTERVAL_MS,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        served = dict(cameras) if cameras else {}
        if camera is not None and camera not in served.values():
            # The single-camera keyword stays, because the viewer, the
            # scripts and every existing test use it. It is the one-entry
            # case of the mapping rather than a separate path.
            served = {"camera": camera, **served}
        if scanner is None and not served:
            msg = (
                "LiveInstrumentWidget needs a scanner, a camera, or both - "
                "an instrument with neither has nothing to display"
            )
            raise ValueError(msg)
        super().__init__(parent)
        self._viewer = viewer
        self._scanner = scanner
        self._instrument = instrument
        self._instrument_controls: dict[str, QtWidgets.QDoubleSpinBox] = {}
        self._instrument_stage_y: QtWidgets.QDoubleSpinBox | None = None
        self._instrument_stage_x: QtWidgets.QDoubleSpinBox | None = None
        self._instrument_blanker: QtWidgets.QCheckBox | None = None
        self._camera_bindings: dict[str, _CameraBinding] = {
            name: _CameraBinding(
                name=name,
                camera=device,
                layer_name="Camera" if index == 0 else f"Camera ({name})",
            )
            for index, (name, device) in enumerate(served.items())
        }
        self._scan_loop: LiveAcquisition | None = None
        # Newest frame already pushed into each napari layer, so a display
        # tick that finds nothing new can skip the upload entirely. Holds
        # a reference for identity comparison only; the array itself is
        # the layer's, not a second copy.
        self._displayed: dict[str, Frame] = {}
        # Section title to section, so a caller - and a test - can ask
        # which devices the window offered and whether each is folded.
        self._device_sections: dict[str, devices_panel.CollapsibleSection] = {}
        self._session: Session | None = None
        self._recording_job: RecordingJob | None = None
        self._load_job: LoadJob | None = None
        self._analysis_job: AnalysisJob | None = None
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
        self._scan_request: tuple[ScanParameters, int, str] = (
            ScanParameters(
                height=_SCAN_SIZES[_DEFAULT_SCAN_SIZE_INDEX],
                width=_SCAN_SIZES[_DEFAULT_SCAN_SIZE_INDEX],
                pixel_time_us=_DEFAULT_DWELL_US,
                fov_nm=_DEFAULT_FOV_NM,
            ),
            0,
            scanner.channel_names[0] if scanner is not None else "",
        )
        self._build_ui()
        if scanner is not None:
            self._on_scan_settings_changed()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(display_interval_ms)
        self._timer.timeout.connect(self.refresh_display)


    @property
    def _camera(self) -> Camera | None:
        """The first camera served, which is what ``camera=`` used to mean."""
        binding = self._first_binding()
        return binding.camera if binding is not None else None

    @property
    def _camera_loop(self) -> LiveAcquisition | None:
        """The first camera's live loop, kept for callers written before N."""
        binding = self._first_binding()
        return binding.loop if binding is not None else None

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
        every 33 ms would put traffic on the wire to answer a question
        nobody asked. Called once when the panel is built, and whenever
        **Refresh** is pressed.

        A control that refuses to be read is reported in the status line
        and leaves its field alone, because a stale number an operator
        can see beats a zero they might act on.
        """
        from miainwoodpecker.devices.interface import (  # noqa: PLC0415
            BEAM_BLANKER_CONTROL,
            DEFOCUS_CONTROL,
            ENERGY_OFFSET_CONTROL,
            STAGE_POSITION_CONTROL,
        )

        if self._instrument is None:
            return
        try:
            description = self._instrument.describe()
        except Exception as error:  # noqa: BLE001 - any failure is a status line
            self._instrument_status.setText(f"could not describe: {error}")
            return
        self._instrument_backend_label.setText(
            str(description.get("backend", "unknown")),
        )
        targets = description.get("targets") or []
        self._instrument_targets_label.setText(
            ", ".join(str(name) for name in targets) or "no devices",
        )

        readers = {
            DEFOCUS_CONTROL: self._instrument.defocus_nm,
            ENERGY_OFFSET_CONTROL: self._instrument.energy_offset_ev,
        }
        failures: list[str] = []
        for name, spin in self._instrument_controls.items():
            try:
                spin.setValue(float(readers[name]()))
            except Exception as error:  # noqa: BLE001 - reported, not raised
                failures.append(f"{name}: {error}")
        if self._instrument_stage_y is not None:
            try:
                y_nm, x_nm = self._instrument.stage_position_nm()
                self._instrument_stage_y.setValue(float(y_nm))
                self._instrument_stage_x.setValue(float(x_nm))
            except Exception as error:  # noqa: BLE001 - reported, not raised
                failures.append(f"{STAGE_POSITION_CONTROL}: {error}")
        if self._instrument_blanker is not None:
            try:
                self._instrument_blanker.setChecked(
                    bool(self._instrument.is_beam_blanked()),
                )
            except Exception as error:  # noqa: BLE001 - reported, not raised
                failures.append(f"{BEAM_BLANKER_CONTROL}: {error}")
        self._instrument_status.setText(
            "; ".join(failures) if failures else "read",
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

        if self._instrument is None:
            return
        try:
            if name == STAGE_POSITION_CONTROL:
                self._instrument.set_stage_position_nm(
                    self._instrument_stage_y.value(),
                    self._instrument_stage_x.value(),
                )
            elif name == DEFOCUS_CONTROL:
                self._instrument.set_defocus_nm(
                    self._instrument_controls[name].value(),
                )
            elif name == ENERGY_OFFSET_CONTROL:
                self._instrument.set_energy_offset_ev(
                    self._instrument_controls[name].value(),
                )
            else:  # pragma: no cover - only built controls have buttons
                return
        except Exception as error:  # noqa: BLE001 - the refusal is the message
            self._instrument_status.setText(f"{name} refused: {error}")
            return
        self._instrument_status.setText(f"{name} set")

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
        if self._instrument is None:
            return
        try:
            self._instrument.set_beam_blanked(blanked=blanked)
        except Exception as error:  # noqa: BLE001 - the refusal is the message
            self._instrument_status.setText(f"beam blanker refused: {error}")
            # Put the box back where the hardware actually is.
            self.refresh_instrument()
            return
        self._instrument_status.setText("beam blanked" if blanked else "beam unblanked")

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(instrument_panel.build_instrument_panel(self))
        layout.addWidget(session_panel.build_session_group(self))
        layout.addWidget(recordings_panel.build_recordings_group(self))
        layout.addWidget(devices_panel.build_devices_panel(self))
        layout.addStretch(1)

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
        scanner = typing.cast("Scanner", self._scanner)
        return scanner.scan_frame(parameters, channel_index)

    def _toggle_scan(self) -> None:
        if self._scan_loop is not None and self._scan_loop.is_running:
            self.stop_scan()
        else:
            self.start_scan()

    def _toggle_camera(self, name: str | None = None) -> None:
        binding = self._binding(name)
        if binding is None:
            return
        if binding.loop is not None and binding.loop.is_running:
            self.stop_camera(binding.name)
        else:
            self.start_camera(binding.name)

    def start_scan(self) -> None:
        """Start the live scan loop and the display timer. No-op with no scanner."""
        if self._scanner is None:
            return
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
            about to drive the scanner themselves must not proceed. True
            with no scanner at all: nothing is holding the device, which
            is what the callers are asking about.
        """
        if self._scanner is None:
            return True
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
        if binding.loop is not None and binding.loop.is_running:
            return
        binding.camera.start()
        binding.loop = LiveAcquisition(binding.camera.acquire_frame)
        binding.loop.start()
        if binding.button is not None:
            binding.button.setText("Stop camera")
        if binding.status is not None:
            binding.status.setText("running")
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
        stopped = True
        if binding.loop is not None:
            stopped = binding.loop.stop()
        if not stopped:
            if binding.status is not None:
                binding.status.setText("still finishing an exposure - try again")
            return False
        binding.camera.stop()
        if binding.button is not None:
            binding.button.setText("Start camera")
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
        self._space_label.setText(text)

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
        if self._scanner is not None:
            frames = self._scan_count_spin.value()
            return frames, estimate_size(self._scan_request[0].shape, frames), "scan"
        if self._camera is None:  # pragma: no cover - one device is required
            return None
        frame = self._camera_loop.latest() if self._camera_loop is not None else None
        if frame is None:
            return None
        binding = self._first_binding()
        if binding is None or binding.count_spin is None:
            return None
        frames = binding.count_spin.value()
        return frames, estimate_size(frame.data.shape, frames), "camera"

    def save_scan_frame(self) -> None:
        """Save the scan frame currently on screen into the session."""
        if self._scanner is None:
            return
        label = f"scan-{self._scan_request[2]}-frame"
        self._save_displayed_frame(self._scan_loop, label)

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
        self._save_displayed_frame(binding.loop, "camera-frame")

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
        if self._scanner is None:
            return
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
                typing.cast("Scanner", self._scanner),
                parameters,
                self._scan_count_spin.value(),
                channel=channel_index,
            ),
            f"scan-{channel_name}",
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
        # Same one-driver-per-device rule as record_scan_frames; camera_series
        # starts and stops the camera around the series itself.
        if not self.stop_camera(binding.name):
            self._recording_status.setText(_CAMERA_BUSY_MESSAGE)
            return
        self._start_recording(
            camera_series(binding.camera, count), "camera"
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
        # Kept alongside the path, from the same load: this is what the
        # analysis buttons hand the adapters instead of making them read
        # the file a second time. None when these frames are not the whole
        # recording - LoadedRecording.frames decides that, not this widget.
        self._opened_frames = loaded.frames
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
        binding = self._first_binding()
        if (
            binding is not None
            and binding.loop is not None
            and binding.loop.is_running
            and not self.stop_camera(binding.name)
        ):
            # Refusing is the point rather than pessimism: an exposure
            # still in flight means the live loop has not released the
            # camera, and starting the burst anyway would drive one device
            # from two threads - which tears a frame across the reused
            # shared-memory segment instead of raising
            # (docs/architecture-review.md, §1.2).
            status.setText(_CAMERA_BUSY_MESSAGE)
            return

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
        if self._camera is None or status is None:
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
        if self._camera is None or status is None:
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
        if self._camera is None or status is None:
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
            status=status,
            compute=compute,
            display=display,
            frame_count=1,
            label="py4dstem-frame",
            title="py4DSTEM analysis frame",
            filename="py4dstem_analysis_frame.nxs",
        )

    def _maybe_stop_timer(self) -> None:
        scan_running = self._scan_loop is not None and self._scan_loop.is_running
        camera_running = any(
            binding.loop is not None and binding.loop.is_running
            for binding in self._camera_bindings.values()
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
        for binding in self._camera_bindings.values():
            if binding.loop is not None and binding.status is not None:
                self._refresh_source(
                    binding.loop,
                    layer_name=binding.layer_name,
                    status_label=binding.status,
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
        # After the devices, and deliberately: a worker holds no hardware,
        # so nothing about the column depends on it going first, while
        # stopping the camera can take an exposure's worth of time and an
        # idle worker costs nothing to leave running for it.
        self._close_analysis_runners()

    def closeEvent(self, event: typing.Any) -> None:  # noqa: N802, ANN401 - Qt override
        """Shut down cleanly when the widget is closed."""
        self.shutdown()
        super().closeEvent(event)
