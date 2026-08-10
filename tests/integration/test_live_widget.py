"""
Integration tests: the live viewer widget against in-memory fake devices.

Skipped unless the ``viewer`` extra is installed and a display is
available (see conftest.py — napari needs a real GL canvas, so run under
``xvfb-run -a``). Drives the display refresh directly rather than waiting
on timers, so the tests never depend on Qt event-loop timing.
"""

import datetime
import time
import typing

import numpy as np
import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import napari

from miainwoodpecker.devices import Frame, ScanParameters
from miainwoodpecker.storage.nexus import read_series
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

        layer_name = "HyperSpy mean projection (Camera)"
        assert layer_name in viewer.layers
        projection_shape = (8, 8)
        assert viewer.layers[layer_name].data.shape == projection_shape
        assert not camera.started  # the burst starts and stops the camera itself
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
        widget._notes_edit.setText("hole 4")  # noqa: SLF001 - simulating user input
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
    """"Save displayed frame" writes the live frame without stopping the loop."""
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
    """"Record frames" streams a scan series into the session off the GUI thread."""
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
