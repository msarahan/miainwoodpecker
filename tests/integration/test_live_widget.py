"""
Integration tests: the live viewer widget against in-memory fake devices.

Skipped unless the ``viewer`` extra is installed and a display is
available (see conftest.py — napari needs a real GL canvas, so run under
``xvfb-run -a``). Drives the display refresh directly rather than waiting
on timers, so the tests never depend on Qt event-loop timing.
"""

import datetime
import threading
import time
import typing

import h5py
import numpy as np
import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import napari
from qtpy import QtWidgets

from miainwoodpecker.devices import Frame, ScanParameters
from miainwoodpecker.storage.calibration import AxisKind, FrameCalibration
from miainwoodpecker.storage.nexus import (
    NexusWriter,
    read_calibration,
    read_series,
    write_frames,
)
from miainwoodpecker.storage.session import Session, read_session_context
from miainwoodpecker.viewer.live import LiveInstrumentWidget

_DEADLINE_S = 10.0


class _FakeScanner:
    """Fake scanner returning zero frames of the requested shape."""

    @property
    def scanner_id(self) -> str:
        """Return the fake scanner's id."""
        return "fake_scanner"

    @property
    def channel_names(self) -> typing.Sequence[str]:
        """Return the fake channel names."""
        return ["HAADF", "MAADF"]

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Return a zero-filled frame of the requested shape."""
        return Frame(
            data=np.zeros(parameters.shape, dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={"channel_index": channel},
        )

    def close(self) -> None:
        """Release nothing; the fake owns no resources."""


class _GradientScanner(_FakeScanner):
    """Fake scanner whose frames vary, so autocontrast actually computes."""

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Return a frame ramping 0..1 across the fast axis."""
        height, width = parameters.shape
        row = np.linspace(0.0, 1.0, width, dtype=np.float32)
        return Frame(
            data=np.tile(row, (height, 1)),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={"channel_index": channel},
        )


class _FakeCamera:
    """Fake camera returning constant 8x8 frames."""

    def __init__(self) -> None:
        self.started = False

    @property
    def camera_id(self) -> str:
        """Return the fake camera's id."""
        return "fake_camera"

    def start(self) -> None:
        """Mark acquisition as running."""
        self.started = True

    def stop(self) -> None:
        """Mark acquisition as paused."""
        self.started = False

    def acquire_frame(self) -> Frame:
        """Return a constant 8x8 frame."""
        return Frame(
            data=np.ones((8, 8), dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={"frame_number": 1},
        )

    def close(self) -> None:
        """Release nothing; the fake owns no resources."""


def _wait_until(condition, deadline_s: float = _DEADLINE_S) -> bool:
    """Poll a condition until it is true or the deadline elapses."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


def _finish_recording(widget: LiveInstrumentWidget) -> None:
    """
    Drive the widget's own poll path until its recording job completes.

    Calls ``refresh_display`` (what the display timer calls) rather than
    waiting on Qt timer timing, so the recording result is collected on
    the GUI thread exactly as it is in the running app.
    """

    def done() -> bool:
        widget.refresh_display()
        return widget._recording_job is None  # noqa: SLF001

    assert _wait_until(done)


def _finish_load(widget: LiveInstrumentWidget) -> None:
    """
    Drive the widget's own poll path until its load job completes.

    Same reasoning as :func:`_finish_recording`: reading runs on a worker
    thread and its result is collected by ``refresh_display``, so the test
    calls that rather than depending on Qt timer timing.
    """

    def done() -> bool:
        widget.refresh_display()
        return widget._load_job is None  # noqa: SLF001

    assert _wait_until(done)


def _finish_analysis(widget: LiveInstrumentWidget) -> None:
    """
    Drive the widget's own poll path until its analysis job completes.

    Same reasoning as :func:`_finish_recording`: the analysis buttons hand
    their work to a worker thread and only the poll path, on the GUI
    thread, adds the layers and writes the status label. A test that
    asserted straight after the click would be asserting on a job that had
    barely started.
    """

    def done() -> bool:
        widget.refresh_display()
        return widget._analysis_job is None  # noqa: SLF001

    assert _wait_until(done)


def _a_frame() -> Frame:
    """Return one small constant frame, for tests that just need a recording."""
    return Frame(
        data=np.ones((8, 8), dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={},
    )


def _abandon_a_writer(path, frame_count: int = 3) -> None:
    """
    Leave the file an abandoned-but-cleanly-exited writer leaves.

    Frames all present, no ``/entry/data``, no ``end_time`` — the second
    interruption mode in the migration plan's Phase 3 table. (The third,
    a real ``SIGKILL``, is produced for real in ``tests/unit/test_session.py``;
    here what matters is only what the widget says about it.)
    """
    writer = NexusWriter(path, title="abandoned")
    # Deliberately entered without ever being __exit__ed or closed.
    writer.__enter__()
    for value in range(frame_count):
        writer.append(
            Frame(
                data=np.full((8, 8), float(value), dtype=np.float32),
                timestamp=datetime.datetime.now(tz=datetime.UTC),
                metadata={},
            )
        )
    writer.flush()


def test_live_widget_updates_layers_from_both_sources():
    """Scan and camera loops feed napari layers through refresh_display."""
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        widget.start_scan()
        widget.start_camera()
        assert camera.started
        assert _wait_until(
            lambda: widget._scan_loop.latest() is not None  # noqa: SLF001
        )
        assert _wait_until(
            lambda: widget._camera_loop.latest() is not None  # noqa: SLF001
        )
        widget.refresh_display()

        assert "Scan (HAADF)" in viewer.layers
        default_scan_shape = (256, 256)
        assert viewer.layers["Scan (HAADF)"].data.shape == default_scan_shape
        assert "Camera" in viewer.layers
        camera_shape = (8, 8)
        assert viewer.layers["Camera"].data.shape == camera_shape
    finally:
        widget.shutdown()
        viewer.close()
    assert not camera.started


def test_analyze_camera_in_hyperspy_adds_a_projection_layer():
    """
    Clicking "Analyze in HyperSpy" round-trips a burst through the adapter.

    Exercises the actual wired-in Phase 4 entry point end to end: record a
    burst from the fake camera to a temporary NeXus file, load it back as
    a HyperSpy signal, average across frames, and land the result in
    napari as a new layer. Skipped if the ``analysis`` extra is not
    installed, since the widget itself does not require it.
    """
    pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        widget._analyze_camera_in_hyperspy()  # noqa: SLF001 - simulating a button click
        _finish_analysis(widget)

        layer_name = "HyperSpy mean projection (Camera)"
        assert layer_name in viewer.layers
        projection_shape = (8, 8)
        assert viewer.layers[layer_name].data.shape == projection_shape
        assert not camera.started  # the burst starts and stops the camera itself
        assert widget._analyze_status.text().startswith("done")  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_analysis_click_returns_before_the_work_is_done():
    """
    The handler hands off to a worker thread instead of doing the work inline.

    This is the property the ``AnalysisJob`` change exists for: an analysis
    used to run to completion inside the click handler, so the window did
    not repaint and "Stop scan" did not answer for its duration.

    The assertions are deliberately race-free rather than timing-based.
    Layers and the status text are only ever touched by ``_poll_analysis``,
    which only runs from ``refresh_display`` — so as long as the test does
    not call that, it does not matter whether the worker has already
    finished: the layer cannot exist yet and the label must still read
    "working...". A handler that did the work inline would fail both, every
    time, regardless of machine speed.
    """
    pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        widget._analyze_camera_in_hyperspy()  # noqa: SLF001 - simulating a button click

        # Returned to the caller with the work outstanding, and nothing
        # drawn: the GUI thread is free.
        assert widget._analysis_job is not None  # noqa: SLF001
        assert widget._analyze_status.text() == "working..."  # noqa: SLF001
        assert "HyperSpy mean projection (Camera)" not in viewer.layers

        _finish_analysis(widget)

        assert "HyperSpy mean projection (Camera)" in viewer.layers
        assert widget._analyze_status.text().startswith("done")  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_analyze_camera_in_libertem_adds_a_sum_projection_layer():
    """
    Clicking "Sum in LiberTEM" round-trips a burst through the adapter.

    Exercises the second Phase 4 entry point end to end: record a burst
    from the fake camera to a temporary NeXus file, load it back as a
    LiberTEM ``DataSet``, sum across the frame/navigation axis with the
    real ``SumUDF``, and land the result in napari as a new layer.
    Skipped if the ``libertem`` extra is not installed, since the widget
    itself does not require it.
    """
    pytest.importorskip("libertem", reason="requires the 'libertem' extra")
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        widget._analyze_camera_in_libertem()  # noqa: SLF001 - simulating a button click
        _finish_analysis(widget)

        layer_name = "LiberTEM sum projection (Camera)"
        assert layer_name in viewer.layers
        projection_shape = (8, 8)
        assert viewer.layers[layer_name].data.shape == projection_shape
        assert not camera.started  # the burst starts and stops the camera itself
        assert widget._libertem_status.text().startswith("done")  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_fit_central_disk_in_py4dstem_adds_a_frame_and_shapes_layer():
    """
    Clicking "Fit central disk (py4DSTEM)" round-trips one frame through the adapter.

    Exercises the py4DSTEM follow-up to Phase 4 end to end: acquire one
    frame from the fake camera, write it to a temporary NeXus file, load
    it back as a py4DSTEM ``DiffractionSlice``, run
    ``py4DSTEM.process.calibration.get_probe_size`` on it, and land both
    the analyzed frame and a ``Shapes`` ellipse at the fitted disk in
    napari. Skipped if the ``py4dstem`` extra is not installed, since the
    widget itself does not require it.
    """
    pytest.importorskip("py4DSTEM", reason="requires the 'py4dstem' extra")
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        widget._fit_central_disk_in_py4dstem()  # noqa: SLF001 - simulating a button click
        _finish_analysis(widget)

        frame_layer_name = "py4DSTEM disk fit (Camera)"
        assert frame_layer_name in viewer.layers
        frame_shape = (8, 8)
        assert viewer.layers[frame_layer_name].data.shape == frame_shape
        assert "py4DSTEM disk fit" in viewer.layers
        assert viewer.layers["py4DSTEM disk fit"].shape_type == ["ellipse"]
        assert not camera.started  # the acquisition starts and stops the camera itself
        assert widget._py4dstem_status.text().startswith("done")  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_the_buttons_work_with_the_analysis_libraries_in_a_worker_process(
    monkeypatch,
):
    """
    The same three buttons, with every analysis library out of this process.

    The isolated path is off by default (see docs/analysis-isolation.md
    for why that is the project owner's decision rather than this code's),
    so without a test that turns it on it would ship unexercised. This is
    that test: it drives the real handlers through the real
    ``AnalysisJob`` and asserts the same layers and the same "done" status
    the in-process tests assert, which is the whole claim — the transport
    changed and nothing else did.

    One test for all three rather than three, because each one costs a
    worker start (0.3 s for HyperSpy, 1.1 s for LiberTEM, 2.6 s for
    py4DSTEM in this environment) and they share every assertion but the
    layer name.
    """
    pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")
    pytest.importorskip("libertem", reason="requires the 'libertem' extra")
    pytest.importorskip("py4DSTEM", reason="requires the 'py4dstem' extra")
    from miainwoodpecker.analysis import remote  # noqa: PLC0415 - viewer-optional

    monkeypatch.setenv(remote.ISOLATION_ENV_VAR, "process")
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        for click, status, layer in (
            (
                widget._analyze_camera_in_hyperspy,  # noqa: SLF001
                widget._analyze_status,  # noqa: SLF001
                "HyperSpy mean projection (Camera)",
            ),
            (
                widget._analyze_camera_in_libertem,  # noqa: SLF001
                widget._libertem_status,  # noqa: SLF001
                "LiberTEM sum projection (Camera)",
            ),
            (
                widget._fit_central_disk_in_py4dstem,  # noqa: SLF001
                widget._py4dstem_status,  # noqa: SLF001
                "py4DSTEM disk fit (Camera)",
            ),
        ):
            click()
            _finish_analysis(widget)
            assert status.text().startswith("done"), status.text()
            assert layer in viewer.layers
            assert viewer.layers[layer].data.shape == (8, 8)
        # Every runner really was a worker, not the in-process fallback
        # quietly standing in for one.
        assert all(
            isinstance(runner, remote.WorkerRunner)
            for runner in widget._analysis_runners.values()  # noqa: SLF001
        )
    finally:
        widget.shutdown()
        viewer.close()
    # shutdown() reaped them, so nothing is left holding a library.
    assert widget._analysis_runners == {}  # noqa: SLF001


def test_scan_settings_change_takes_effect_on_next_frames():
    """Changing the size control changes the shape of subsequently scanned frames."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget._size_combo.setCurrentIndex(0)  # noqa: SLF001 - simulating user input
        widget.start_scan()
        assert _wait_until(
            lambda: widget._scan_loop.latest() is not None  # noqa: SLF001
        )
        widget.refresh_display()
        small_shape = (128, 128)
        assert viewer.layers["Scan (HAADF)"].data.shape == small_shape
    finally:
        widget.shutdown()
        viewer.close()


def test_recording_controls_report_honestly_without_a_session():
    """With no session attached, the widget says so instead of pretending to save."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        widget.record_scan_frames()

        assert "no session" in widget._recording_status.text()  # noqa: SLF001
        assert "no session" in widget._session_path_label.text()  # noqa: SLF001
        assert widget.session is None
    finally:
        widget.shutdown()
        viewer.close()


def test_set_session_shows_where_data_goes_and_loads_its_context(tmp_path):
    """Attaching a session fills the Session group from what is on disk."""
    session = Session(tmp_path / "shift", operator="M. Sarahan", sample="Au on C")
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget.set_session(session)

        assert widget._session_path_label.text() == str(session.root)  # noqa: SLF001
        assert widget._operator_edit.text() == "M. Sarahan"  # noqa: SLF001
        assert widget._sample_edit.text() == "Au on C"  # noqa: SLF001
        assert "nothing recorded yet" in widget._recorded_label.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_editing_session_context_persists_it_for_later_recordings(tmp_path):
    """Typing a sample into the Session group re-tags subsequent recordings."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        widget.set_session(Session(tmp_path / "shift"))
        widget._operator_edit.setText("M. Sarahan")  # noqa: SLF001 - simulating user input
        widget._sample_edit.setText("grid-2")  # noqa: SLF001 - simulating user input
        widget._notes_edit.setPlainText("hole 4")  # noqa: SLF001 - simulating user input
        widget._on_session_context_edited()  # noqa: SLF001 - the editingFinished slot

        widget._camera_count_spin.setValue(1)  # noqa: SLF001 - simulating user input
        widget.record_camera_frames()
        _finish_recording(widget)

        (recording,) = widget.session.recordings()
        context = read_session_context(recording.path)
        assert context["operator"] == "M. Sarahan"
        assert context["sample"] == "grid-2"
        assert context["notes"] == "hole 4"
    finally:
        widget.shutdown()
        viewer.close()


def test_save_displayed_scan_frame_keeps_the_frame_on_screen(tmp_path):
    """Saving the displayed frame writes it without stopping the live loop."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget.set_session(Session(tmp_path / "shift", sample="Au on C"))
        widget.start_scan()
        assert _wait_until(
            lambda: widget._scan_loop.latest() is not None  # noqa: SLF001
        )

        widget.save_scan_frame()
        _finish_recording(widget)

        # Saving a frame already in hand needs no device access, so the
        # live loop is deliberately left running.
        assert widget._scan_loop.is_running  # noqa: SLF001
        (recording,) = widget.session.recordings()
        assert recording.frame_count == 1
        assert recording.readable
        assert recording.label.startswith("scan-haadf")
        default_scan_shape = (256, 256)
        ((data, _),) = list(read_series(recording.path))
        assert data.shape == default_scan_shape
        assert read_session_context(recording.path)["sample"] == "Au on C"
    finally:
        widget.shutdown()
        viewer.close()


def test_record_scan_frames_writes_the_requested_count(tmp_path):
    """Recording frames streams a scan series to disk off the GUI thread."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget.set_session(Session(tmp_path / "shift"))
        widget.start_scan()
        assert _wait_until(
            lambda: widget._scan_loop.latest() is not None  # noqa: SLF001
        )

        expected_count = 3
        widget._scan_count_spin.setValue(expected_count)  # noqa: SLF001 - user input
        widget.record_scan_frames()
        # A recording drives the same device as the live loop, so the loop
        # must have been stopped: one driver per device.
        assert not widget._scan_loop.is_running  # noqa: SLF001
        _finish_recording(widget)

        (recording,) = widget.session.recordings()
        assert recording.frame_count == expected_count
        assert recording.readable
        assert str(recording.frame_count) in widget._recording_status.text()  # noqa: SLF001
        assert recording.path.name in widget._recorded_label.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_record_camera_frames_writes_the_requested_count(tmp_path):
    """The camera source records into the session too, not just the scan."""
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        widget.set_session(Session(tmp_path / "shift"))
        expected_count = 4
        widget._camera_count_spin.setValue(expected_count)  # noqa: SLF001 - user input
        widget.record_camera_frames()
        _finish_recording(widget)

        (recording,) = widget.session.recordings()
        assert recording.label == "camera"
        assert recording.frame_count == expected_count
        # camera_series starts and stops the camera around the series.
        assert not camera.started
    finally:
        widget.shutdown()
        viewer.close()


def test_recordings_from_both_sources_get_distinct_sequenced_files(tmp_path):
    """Two acquisitions in one session never collide on a filename."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        widget.set_session(Session(tmp_path / "shift"))
        widget._scan_count_spin.setValue(1)  # noqa: SLF001 - simulating user input
        widget._camera_count_spin.setValue(1)  # noqa: SLF001 - simulating user input

        widget.record_scan_frames()
        _finish_recording(widget)
        widget.record_camera_frames()
        _finish_recording(widget)

        recordings = widget.session.recordings()
        expected_files = 2
        assert len(recordings) == expected_files
        assert [recording.index for recording in recordings] == [1, 2]
        assert len({recording.path for recording in recordings}) == expected_files
    finally:
        widget.shutdown()
        viewer.close()


def test_opening_a_recording_from_the_session_shows_it_as_a_stack(tmp_path):
    """
    "Open selected" reads yesterday's file back into a napari layer.

    The table-stakes case for a parallel pilot: record something, then look
    at it again. A multi-frame recording goes in as a 3D stack, which napari
    navigates with its own frame slider.
    """
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget.set_session(Session(tmp_path / "shift"))
        expected_count = 3
        widget._scan_count_spin.setValue(expected_count)  # noqa: SLF001 - user input
        widget.record_scan_frames()
        _finish_recording(widget)
        (recording,) = widget.session.recordings()

        widget.open_selected_recording()
        _finish_load(widget)

        layer_name = f"File: {recording.path.name}"
        assert layer_name in viewer.layers
        assert viewer.layers[layer_name].data.shape == (expected_count, 256, 256)
        assert f"{expected_count} frames" in widget._load_status.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_opening_a_one_frame_recording_shows_a_plain_image(tmp_path):
    """A single frame is squeezed to 2D: a slider with one position is furniture."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        widget.set_session(Session(tmp_path / "shift"))
        widget._camera_count_spin.setValue(1)  # noqa: SLF001 - simulating user input
        widget.record_camera_frames()
        _finish_recording(widget)
        (recording,) = widget.session.recordings()

        widget.open_recording(recording.path)
        _finish_load(widget)

        camera_shape = (8, 8)
        assert viewer.layers[f"File: {recording.path.name}"].data.shape == camera_shape
    finally:
        widget.shutdown()
        viewer.close()


def test_opening_a_recording_from_an_arbitrary_path_works(tmp_path):
    """A file outside any session — copied off another machine — still opens."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        session = Session(tmp_path / "shift")
        widget.set_session(session)
        widget._scan_count_spin.setValue(1)  # noqa: SLF001 - simulating user input
        widget.record_scan_frames()
        _finish_recording(widget)
        (recording,) = session.recordings()
        elsewhere = tmp_path / "copied-from-the-other-scope.nxs"
        elsewhere.write_bytes(recording.path.read_bytes())

        widget.open_recording(elsewhere)
        _finish_load(widget)

        assert f"File: {elsewhere.name}" in viewer.layers
    finally:
        widget.shutdown()
        viewer.close()


def test_an_unfinalized_recording_displays_and_says_it_is_unfinalized(tmp_path):
    """
    A file whose writer was abandoned shows its frames and admits its state.

    Measured, not assumed: ``read_series`` reads
    ``/entry/instrument/detector``, which survives, so the frames display —
    while the Phase 4 adapters, which read ``/entry/data``, cannot touch it.
    The label says exactly that rather than leaving the operator to find out.
    """
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        session = Session(tmp_path / "shift")
        widget.set_session(session)
        target = session.reserve_path("scan")
        _abandon_a_writer(target)

        widget.open_recording(target)
        _finish_load(widget)

        assert f"File: {target.name}" in viewer.layers
        expected_shape = (3, 8, 8)
        assert viewer.layers[f"File: {target.name}"].data.shape == expected_shape
        status = widget._load_status.text()  # noqa: SLF001
        assert "unfinalized" in status
        assert "not analyzable" in status
    finally:
        widget.shutdown()
        viewer.close()


def test_a_damaged_recording_reports_a_sentence_and_adds_no_layer(tmp_path):
    """A file that will not open at all is a status message, not a traceback."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        damaged = tmp_path / "0001-killed-20260810T120000Z.nxs"
        damaged.write_bytes(b"not an HDF5 file at all")

        widget.open_recording(damaged)
        _finish_load(widget)

        assert f"File: {damaged.name}" not in viewer.layers
        status = widget._load_status.text()  # noqa: SLF001
        assert "cannot open" in status
        assert damaged.name in status
    finally:
        widget.shutdown()
        viewer.close()


def test_analysis_runs_against_a_file_on_disk_without_acquiring(tmp_path):
    """
    Ticking the Recordings checkbox points HyperSpy at the opened file.

    The fresh-burst path is unchanged and still tested above; this is the
    addition — analyzing something already recorded, which must not acquire
    anything, so the camera stays stopped and no second file appears.
    """
    pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        widget.set_session(Session(tmp_path / "shift"))
        recorded_frames = 2
        widget._camera_count_spin.setValue(recorded_frames)  # noqa: SLF001 - user input
        widget.record_camera_frames()
        _finish_recording(widget)
        (recording,) = widget.session.recordings()
        widget.open_recording(recording.path)
        _finish_load(widget)
        widget._analyze_from_file_check.setChecked(True)  # noqa: SLF001 - user input

        widget._analyze_camera_in_hyperspy()  # noqa: SLF001 - simulating a button click
        _finish_analysis(widget)

        assert "HyperSpy mean projection (Camera)" in viewer.layers
        status = widget._analyze_status.text()  # noqa: SLF001
        assert status.startswith("done")
        assert recording.path.name in status
        assert f"mean of {recorded_frames} frames" in status
        # Nothing was acquired: still one file, and the camera never started.
        assert len(widget.session.recordings()) == 1
        assert not camera.started
    finally:
        widget.shutdown()
        viewer.close()


def _count_frame_reads(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """
    Instrument both frame-stack readers and return the log they write to.

    Counting reads is the only way to assert the thing this path is for:
    the result of analyzing an opened recording is identical whether the
    file was read once or twice, so nothing about the *answer* can show
    which happened. Both readers are wrapped rather than one, so the log
    also proves the instrumentation works — a zero from the adapter is only
    meaningful next to the one the load itself records.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The fixture doing the patching, so it is undone with the test.

    Returns
    -------
    dict[str, list[str]]
        ``{"display": [...], "analysis": [...]}``, each a list of the paths
        that reader was asked for, in call order.
    """
    # Imported here, not at module scope: hyperspy_bridge needs the
    # 'analysis' extra, and this file also runs in a viewer-only env where
    # the tests that use this helper skip themselves.
    from miainwoodpecker.analysis import hyperspy_bridge  # noqa: PLC0415
    from miainwoodpecker.storage import session as session_module  # noqa: PLC0415

    log: dict[str, list[str]] = {"display": [], "analysis": []}
    read_series_real = session_module.read_series
    read_frames_real = hyperspy_bridge.read_frames

    def counted_read_series(path) -> typing.Iterator[tuple[np.ndarray, float]]:
        log["display"].append(str(path))
        return read_series_real(path)

    def counted_read_frames(path) -> object:
        log["analysis"].append(str(path))
        return read_frames_real(path)

    monkeypatch.setattr(session_module, "read_series", counted_read_series)
    monkeypatch.setattr(hyperspy_bridge, "read_frames", counted_read_frames)
    return log


def test_analyzing_an_opened_recording_reads_the_file_once_not_twice(
    tmp_path, monkeypatch
):
    """
    The load that displayed the recording is the only read of its frames.

    Opening a recording decompresses every frame to draw it; pointing an
    adapter at the same path made it decompress them all again. At the
    2048x2048 Ronchigram frames a pilot actually produces that is 16.8MB
    per frame paid twice for one operation. The frames are handed over
    instead, and this asserts the second read is gone rather than merely
    unlikely — while the first read, logged by the same instrumentation,
    shows the counter would have seen it.
    """
    pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        widget.set_session(Session(tmp_path / "shift"))
        recorded_frames = 2
        widget._camera_count_spin.setValue(recorded_frames)  # noqa: SLF001 - user input
        widget.record_camera_frames()
        _finish_recording(widget)
        (recording,) = widget.session.recordings()

        log = _count_frame_reads(monkeypatch)
        widget.open_recording(recording.path)
        _finish_load(widget)
        widget._analyze_from_file_check.setChecked(True)  # noqa: SLF001 - user input
        widget._analyze_camera_in_hyperspy()  # noqa: SLF001 - simulating a click
        _finish_analysis(widget)

        assert log["display"] == [str(recording.path)]
        assert log["analysis"] == []
        # ...and the answer is still the right one: the fake camera's
        # frames are all ones, so their mean is too.
        layer = viewer.layers["HyperSpy mean projection (Camera)"]
        assert layer.data.shape == (8, 8)
        assert np.allclose(layer.data, 1.0)
        assert widget._analyze_status.text().startswith("done")  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_a_fresh_burst_still_reads_the_file_it_just_wrote(tmp_path, monkeypatch):
    """
    The other half of the count, and a deliberate non-change.

    A burst's frames are in memory too, but their calibration is only
    decided when ``NexusWriter`` resolves it from the frame metadata — so
    short-circuiting this read would mean a second implementation of the
    rule that decides what a recording's axes are, to save one read of a
    file this app created a moment ago. It still reads, exactly once, and
    that is what makes the zero in the test above evidence rather than an
    artefact of the instrumentation.
    """
    pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        widget.set_session(Session(tmp_path / "shift"))
        log = _count_frame_reads(monkeypatch)

        widget._analyze_camera_in_hyperspy()  # noqa: SLF001 - simulating a click
        _finish_analysis(widget)

        (recording,) = widget.session.recordings()
        assert log["analysis"] == [str(recording.path)]
        assert widget._analyze_status.text().startswith("done")  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_the_frames_an_analysis_is_given_carry_the_files_calibration(tmp_path):
    """
    Reusing the frames must not cost the axes, or it trades a bug for a saving.

    An adapter handed arrays without their calibration produces a signal
    whose axes silently claim bare pixels — worse than the duplicated read
    this path removes, because nothing downstream says so. Asserted on the
    input the analysis actually receives, since the adapters' own tests
    cover what they do with it.
    """
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        session = Session(tmp_path / "shift")
        widget.set_session(session)
        target = session.reserve_path("camera")
        scale_per_nm = 0.05
        write_frames(
            target,
            [_a_frame(), _a_frame()],
            calibration=FrameCalibration.diffraction(scale_per_nm),
        )
        widget.open_recording(target)
        _finish_load(widget)
        widget._analyze_from_file_check.setChecked(True)  # noqa: SLF001 - user input

        existing, frames = widget._analysis_source()  # noqa: SLF001 - the GUI-thread half
        assert existing == target
        with widget._analysis_input(  # noqa: SLF001 - the worker-thread half
            frame_count=1,
            label="unused",
            title="unused",
            filename="unused.nxs",
            existing=existing,
            existing_frames=frames,
            note=None,
        ) as source:
            assert source.frames is not None
            assert source.frames.calibration.x.kind is AxisKind.RECIPROCAL_SPACE
            assert source.frames.calibration.x.scale == pytest.approx(scale_per_nm)
            assert source.frames.calibration == read_calibration(target)
            assert source.frames.data.shape == (2, 8, 8)
    finally:
        widget.shutdown()
        viewer.close()


def test_frames_that_no_longer_match_the_file_are_not_reused(tmp_path):
    """
    The file on disk is the authority on what is in it, not a copy in memory.

    The in-memory shortcut is only sound while the two agree; a recording
    that has grown since it was opened would otherwise be analyzed as the
    shorter thing that was displayed, and the status line would still say
    how many frames the *file* has. Simulated by shortening the copy,
    because what matters is the mismatch, not how it arose.
    """
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        session = Session(tmp_path / "shift")
        widget.set_session(session)
        target = session.reserve_path("camera")
        write_frames(target, [_a_frame(), _a_frame(), _a_frame()])
        widget.open_recording(target)
        _finish_load(widget)
        widget._analyze_from_file_check.setChecked(True)  # noqa: SLF001 - user input

        stale = widget._opened_frames  # noqa: SLF001 - what the load kept
        assert stale is not None
        widget._opened_frames = stale._replace(data=stale.data[:1])  # noqa: SLF001

        existing, frames = widget._analysis_source()  # noqa: SLF001 - GUI-thread half
        with widget._analysis_input(  # noqa: SLF001 - worker-thread half
            frame_count=1,
            label="unused",
            title="unused",
            filename="unused.nxs",
            existing=existing,
            existing_frames=frames,
            note=None,
        ) as source:
            # Falls back to the path: the adapter reads the file, which is
            # the honest answer, and costs only the read this avoids.
            assert source.frames is None
            assert source.path == target
            assert source.frame_count == 3  # noqa: PLR2004 - what the file holds
    finally:
        widget.shutdown()
        viewer.close()


def test_analysis_against_an_unfinalized_file_is_refused_with_the_reason(tmp_path):
    """
    The adapters' own "it recorded no frames" would be wrong here, so we pre-empt it.

    The frames *are* there; what is missing is ``/entry/data``. The status
    label says that and points at the button that does work.
    """
    pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=camera)
    try:
        session = Session(tmp_path / "shift")
        widget.set_session(session)
        target = session.reserve_path("camera")
        _abandon_a_writer(target)
        widget.open_recording(target)
        _finish_load(widget)
        widget._analyze_from_file_check.setChecked(True)  # noqa: SLF001 - user input

        widget._analyze_camera_in_hyperspy()  # noqa: SLF001 - simulating a button click
        _finish_analysis(widget)

        status = widget._analyze_status.text()  # noqa: SLF001
        assert "never finalized" in status
        assert "/entry/data" in status
        assert "HyperSpy mean projection (Camera)" not in viewer.layers
        assert not camera.started
    finally:
        widget.shutdown()
        viewer.close()


def test_changing_the_session_directory_redirects_later_recordings(tmp_path):
    """An operator switching samples after lunch no longer restarts the app."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget.set_session(Session(tmp_path / "morning", sample="grid-1"))
        widget._scan_count_spin.setValue(1)  # noqa: SLF001 - simulating user input
        widget.record_scan_frames()
        _finish_recording(widget)

        widget.open_session_directory(tmp_path / "afternoon")
        widget.record_scan_frames()
        _finish_recording(widget)

        assert widget.session.root == tmp_path / "afternoon"
        assert widget._session_path_label.text().endswith("afternoon")  # noqa: SLF001
        assert len(widget.session.recordings()) == 1
        assert len(Session(tmp_path / "morning").recordings()) == 1
    finally:
        widget.shutdown()
        viewer.close()


def test_switching_directory_does_not_overwrite_the_new_ones_stored_context(tmp_path):
    """Reopening a directory shows its own context, not the one being left behind."""
    Session(tmp_path / "yesterday", operator="A. Other", sample="grid-9")
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget.set_session(Session(tmp_path / "today", operator="M. Sarahan"))

        widget.open_session_directory(tmp_path / "yesterday")

        assert widget._operator_edit.text() == "A. Other"  # noqa: SLF001
        assert widget._sample_edit.text() == "grid-9"  # noqa: SLF001
        assert Session(tmp_path / "yesterday").operator == "A. Other"
    finally:
        widget.shutdown()
        viewer.close()


def test_changing_directory_is_refused_while_a_recording_is_in_flight(tmp_path):
    """
    A write in progress blocks the switch rather than being redirected or lost.

    The job holds its own session reference and would finish correctly into
    the old directory, but the Session group would then describe a directory
    not receiving the file — so the operator is told to stop it first.
    """
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    release = threading.Event()
    started = threading.Event()

    def blocking_frames() -> typing.Iterator[Frame]:
        yield Frame(
            data=np.zeros((8, 8), dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={},
        )
        started.set()
        release.wait(_DEADLINE_S)

    try:
        widget.set_session(Session(tmp_path / "morning"))
        widget._start_recording(blocking_frames(), "scan")  # noqa: SLF001
        assert started.wait(_DEADLINE_S)

        widget.open_session_directory(tmp_path / "afternoon")

        assert widget.session.root == tmp_path / "morning"
        assert "stop it before" in widget._recording_status.text()  # noqa: SLF001
    finally:
        release.set()
        widget.shutdown()
        viewer.close()


def test_multi_line_notes_and_a_per_recording_note_reach_the_file(tmp_path):
    """
    A shift's notes and a note about this one file both survive the widget.

    A single-line field proved the wiring; this is what an operator asked
    for next.
    """
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget.set_session(Session(tmp_path / "shift"))
        shift_notes = "09:10 grid loaded\n09:40 LN2 topped up"
        widget._notes_edit.setPlainText(shift_notes)  # noqa: SLF001 - user input
        widget._recording_note_edit.setPlainText("hole 4")  # noqa: SLF001 - user input
        widget._on_session_context_edited()  # noqa: SLF001 - the debounce timer's slot

        widget._scan_count_spin.setValue(1)  # noqa: SLF001 - simulating user input
        widget.record_scan_frames()
        _finish_recording(widget)

        (recording,) = widget.session.recordings()
        context = read_session_context(recording.path)
        assert context["notes"] == shift_notes
        assert context["note"] == "hole 4"
        with h5py.File(recording.path, "r") as handle:
            description = handle["entry/notes/description"][()].decode("utf-8")
        assert description == f"session: {shift_notes}\n\nrecording: hole 4"
    finally:
        widget.shutdown()
        viewer.close()


def test_analysis_burst_is_kept_in_the_session_when_one_is_attached(tmp_path):
    """
    The HyperSpy button keeps its burst instead of discarding it.

    With a session attached, the Phase 4 analysis buttons record into it,
    so an operator can analyze *and* keep the data — previously the burst
    went to a TemporaryDirectory that was deleted on the way out.
    """
    pytest.importorskip("hyperspy", reason="requires the 'analysis' extra")
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        widget.set_session(Session(tmp_path / "shift", operator="M. Sarahan"))
        widget._analyze_camera_in_hyperspy()  # noqa: SLF001 - simulating a button click
        _finish_analysis(widget)

        assert "HyperSpy mean projection (Camera)" in viewer.layers
        assert widget._analyze_status.text().startswith("done")  # noqa: SLF001
        (recording,) = widget.session.recordings()
        burst_frames = 5
        assert recording.label == "hyperspy-burst"
        assert recording.frame_count == burst_frames
        assert read_session_context(recording.path)["operator"] == "M. Sarahan"
    finally:
        widget.shutdown()
        viewer.close()


def test_annotating_the_opened_recording_writes_into_its_note(tmp_path):
    """The button annotates what is on screen, and clears the field on success."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        session = Session(tmp_path / "shift")
        widget.set_session(session)
        recording = session.record([_a_frame()], label="scan", note="at acquisition")
        widget.open_recording(recording.path)
        _finish_load(widget)

        widget._annotation_edit.setText("drifted badly")  # noqa: SLF001
        widget.annotate_opened_recording()

        assert widget._annotation_edit.text() == ""  # noqa: SLF001
        assert "note added" in widget._load_status.text()  # noqa: SLF001
        with h5py.File(recording.path, "r") as handle:
            stored = handle["entry/notes/description"][()]
        text = stored.decode("utf-8") if isinstance(stored, bytes) else str(stored)
        assert "recording: at acquisition" in text
        assert "drifted badly" in text
    finally:
        widget.shutdown()
        viewer.close()


def test_annotating_with_nothing_open_says_so_and_keeps_the_text(tmp_path):
    """A note typed with no recording open is not silently dropped."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        widget.set_session(Session(tmp_path / "shift"))
        widget._annotation_edit.setText("worth keeping")  # noqa: SLF001

        widget.annotate_opened_recording()

        assert "open a recording" in widget._load_status.text()  # noqa: SLF001
        assert widget._annotation_edit.text() == "worth keeping"  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_listing_every_session_spans_directories_and_disambiguates_names(tmp_path):
    """The all-sessions checkbox reaches other sessions, qualified by directory."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        base = tmp_path / "data"
        Session(base / "monday").record([_a_frame()], label="scan")
        tuesday = Session(base / "tuesday")
        tuesday.record([_a_frame()], label="scan")
        widget.set_session(tuesday)

        # This session only: one entry, and per-session numbering means the
        # name alone would not distinguish it from Monday's.
        assert widget._recording_combo.count() == 1  # noqa: SLF001

        widget._all_sessions_check.setChecked(True)  # noqa: SLF001

        combo = widget._recording_combo  # noqa: SLF001
        both = 2
        assert combo.count() == both
        shown = [combo.itemText(index) for index in range(combo.count())]
        assert any(text.startswith("monday/") for text in shown)
        assert any(text.startswith("tuesday/") for text in shown)
    finally:
        widget.shutdown()
        viewer.close()


def test_disk_label_reports_free_space_and_warns_when_a_plan_will_not_fit(
    tmp_path, monkeypatch
):
    """
    Free space is reported, and an oversized plan is flagged before it runs.

    Free space is stubbed rather than measured: whether the warning fires
    is the behaviour under test, and against a real filesystem that would
    depend on how much spare disk the machine running the tests happens to
    have — which is how a CI runner and a laptop end up disagreeing.
    """
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner(), camera=_FakeCamera())
    try:
        roomy = 10**15
        monkeypatch.setattr(
            "miainwoodpecker.viewer.live.free_space", lambda _path: roomy
        )
        widget.set_session(Session(tmp_path / "shift"))

        assert "free" in widget._space_label.text()  # noqa: SLF001
        assert "warning" not in widget._space_label.text()  # noqa: SLF001

        cramped = 1000
        monkeypatch.setattr(
            "miainwoodpecker.viewer.live.free_space", lambda _path: cramped
        )
        widget._refresh_session_labels()  # noqa: SLF001

        assert "warning" in widget._space_label.text()  # noqa: SLF001
        assert "scan frames need up to" in widget._space_label.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_an_unchanged_frame_is_not_redrawn():
    """
    A display tick that finds no new frame must not re-push the old one.

    The timer runs at a fixed 33 ms while acquisition runs at whatever the
    device manages, so most ticks see the frame they already drew.
    Reassigning ``layer.data`` schedules a GPU re-upload and the
    autocontrast pass walks the whole array twice - at Ronchigram size
    that was on the order of a gigabyte a second of pointless work on the
    GUI thread.

    Observed through the autocontrast, which is the visible half: with the
    loop stopped the frame cannot change, so a contrast limit set by hand
    survives a refresh only if the refresh genuinely skipped its work.
    """
    viewer = napari.Viewer(show=False)
    # A gradient, not the zero-filled default: autocontrast only sets a
    # limit when max > min, so a constant frame would make this test pass
    # whether or not the redundant work was skipped.
    widget = LiveInstrumentWidget(viewer, _GradientScanner())
    try:
        widget.start_scan()
        assert _wait_until(
            lambda: widget._scan_loop.latest() is not None  # noqa: SLF001
        )
        assert widget.stop_scan()
        widget.refresh_display()
        layer = viewer.layers["Scan (HAADF)"]
        assert tuple(layer.contrast_limits) != (-123.0, 456.0)

        sentinel = (-123.0, 456.0)
        layer.contrast_limits = sentinel
        widget.refresh_display()
        assert tuple(layer.contrast_limits) == sentinel
    finally:
        widget.shutdown()
        viewer.close()


def test_a_new_frame_is_still_drawn_after_a_skipped_tick():
    """The skip must not latch: the next genuinely new frame still lands."""
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget._size_combo.setCurrentIndex(0)  # noqa: SLF001 - simulating user input
        widget.start_scan()
        assert _wait_until(
            lambda: widget._scan_loop.latest() is not None  # noqa: SLF001
        )
        widget.refresh_display()
        widget.refresh_display()  # a skipped tick
        small_shape = (128, 128)
        assert viewer.layers["Scan (HAADF)"].data.shape == small_shape

        # Change the scan size; the next frames differ, so they must draw.
        widget._size_combo.setCurrentIndex(1)  # noqa: SLF001 - simulating user input
        larger = (256, 256)
        assert _wait_until(
            lambda: widget._scan_loop.latest().data.shape == larger  # noqa: SLF001
        )
        widget.refresh_display()
        assert viewer.layers["Scan (HAADF)"].data.shape == larger
    finally:
        widget.shutdown()
        viewer.close()


def test_a_deleted_layer_comes_back_on_the_next_refresh():
    """
    Skipping must key on the layer still existing, not only on the frame.

    An operator who deletes the layer while the source is idle would
    otherwise never get it back, because the frame has not changed.
    """
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _FakeScanner())
    try:
        widget.start_scan()
        assert _wait_until(
            lambda: widget._scan_loop.latest() is not None  # noqa: SLF001
        )
        assert widget.stop_scan()
        widget.refresh_display()
        del viewer.layers["Scan (HAADF)"]

        widget.refresh_display()
        assert "Scan (HAADF)" in viewer.layers
    finally:
        widget.shutdown()
        viewer.close()


def test_a_grab_error_stops_the_display_timer():
    """
    A dead source must not leave the timer spinning on the error path.

    Without this the 33 ms timer ran forever after a detector fell over,
    reformatting the same exception thirty times a second for the rest of
    the session.
    """

    class _BrokenScanner(_FakeScanner):
        def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:  # noqa: ARG002 - signature fixed by the Scanner protocol
            """Fail the way an unplugged detector does."""
            msg = "detector went away"
            raise RuntimeError(msg)

    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, _BrokenScanner())
    try:
        widget.start_scan()
        assert _wait_until(
            lambda: widget._scan_loop.error is not None  # noqa: SLF001
        )
        widget.refresh_display()
        assert not widget._timer.isActive()  # noqa: SLF001
        assert "detector went away" in widget._scan_status.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_camera_only_instrument_builds_a_window_with_no_scan_group():
    """
    A detector-only server gets a working window, minus the Scan group.

    The commodity camera server and every direct-detector adapter serve
    no scan unit (docs/vendor-support.md), so the widget has to be
    usable without one rather than refusing to open. The absent device
    is *missing* from the window rather than present and disabled: a
    greyed-out Scan group would invite an operator to look for the
    scanner that is not there.
    """
    viewer = napari.Viewer(show=False)
    camera = _FakeCamera()
    widget = LiveInstrumentWidget(viewer, None, camera=camera)
    try:
        titles = {box.title() for box in widget.findChildren(QtWidgets.QGroupBox)}
        assert "Camera" in titles
        assert "Scan" not in titles

        widget.start_camera()
        assert camera.started
        assert _wait_until(
            lambda: widget._camera_loop.latest() is not None  # noqa: SLF001
        )
        widget.refresh_display()
        camera_shape = (8, 8)
        assert viewer.layers["Camera"].data.shape == camera_shape
        assert not any(layer.name.startswith("Scan") for layer in viewer.layers)
    finally:
        widget.shutdown()
        viewer.close()
    assert not camera.started


def test_scan_controls_are_inert_rather_than_raising_without_a_scanner():
    """
    Every scan entry point is a no-op with no scanner, not an AttributeError.

    ``shutdown()`` calls ``stop_scan()`` unconditionally, and a caller
    driving the widget from a script has no reason to know which devices
    this server happens to serve. ``stop_scan()`` returning True is the
    load-bearing part: ``record_scan_frames`` and the analysis buttons
    treat False as "the scanner is still busy, do not proceed", and with
    no scanner nothing is holding the device.
    """
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, None, camera=_FakeCamera())
    try:
        widget.start_scan()
        assert widget._scan_loop is None  # noqa: SLF001
        assert widget.stop_scan() is True
        widget.save_scan_frame()
        widget.record_scan_frames()
        assert widget._recording_job is None  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()


def test_an_instrument_with_neither_device_is_refused():
    """
    No scanner and no camera is refused at construction, with a reason.

    There would be nothing to display, nothing to record, and every
    control would be dead. Failing here beats opening an empty window
    that looks like a bug in the viewer rather than a misconfigured
    server.
    """
    viewer = napari.Viewer(show=False)
    try:
        with pytest.raises(ValueError, match="scanner, a camera, or both"):
            LiveInstrumentWidget(viewer, None)
    finally:
        viewer.close()


def test_disk_warning_uses_the_camera_shape_when_there_is_no_scanner(
    tmp_path, monkeypatch
):
    """
    Without a scanner the size estimate waits for a real camera frame.

    A scan's frame shape is set by the operator before anything is
    acquired; a camera's is whatever the sensor gives, and on a
    commodity USB camera that is whatever the driver negotiated. So the
    warning stays silent until a frame has actually arrived and then
    uses its shape — rather than inventing one, which would put a number
    on screen that no acquisition would produce.
    """
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, None, camera=_FakeCamera())
    try:
        cramped = 10
        monkeypatch.setattr(
            "miainwoodpecker.viewer.live.free_space", lambda _path: cramped
        )
        widget.set_session(Session(tmp_path / "shift"))

        # No frame yet: free space is still reported, the plan is not.
        assert "free" in widget._space_label.text()  # noqa: SLF001
        assert "warning" not in widget._space_label.text()  # noqa: SLF001

        widget.start_camera()
        assert _wait_until(
            lambda: widget._camera_loop.latest() is not None  # noqa: SLF001
        )
        widget._refresh_session_labels()  # noqa: SLF001

        assert "warning" in widget._space_label.text()  # noqa: SLF001
        assert "camera frames need up to" in widget._space_label.text()  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()
