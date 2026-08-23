"""
Integration tests: acquiring a scan image and a camera image.

The everyday pair from the operator's workflow — find an area on the
live view, then keep one image of it — as distinct from "record N
frames", which is a time series and was until now the only kind of
recording the window could make.

Skipped without a display (see conftest.py).
"""

import json
import pathlib
import time

import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import h5py
import napari

from miainwoodpecker.storage.session import Session
from miainwoodpecker.viewer.live import LiveInstrumentWidget
from miainwoodpecker.viewer.preview import _EELS_CHANNELS, build_preview_devices

_DEADLINE_S = 10.0
_AN_IMAGE_EXPOSURE_MS = 250.0
_BOTH_CHANNELS = 2


def _open(tmp_path: pathlib.Path, **kwargs: object) -> tuple:
    """
    Open a widget with a session attached.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Where the session is written.
    **kwargs : object
        Passed through to :func:`build_preview_devices`.

    Returns
    -------
    tuple
        The viewer, the widget, and the devices behind it.
    """
    devices = build_preview_devices(**kwargs)
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(
        viewer,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    widget.set_session(Session(tmp_path / "shift"))
    return viewer, widget, devices


def _enable_all_channels(widget: LiveInstrumentWidget) -> None:
    """
    Tick every detector checkbox.

    An acquisition reads out what the operator enabled, and only the
    first detector is enabled on a fresh preferences file - so a test
    about "every channel" has to say which channels it means.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget whose detectors are enabled.
    """
    for check in widget._channel_checks.values():  # noqa: SLF001
        check.setChecked(True)


def _finish_recording(widget: LiveInstrumentWidget) -> None:
    """
    Drive the widget's poll path until its recording job completes.

    Calls ``refresh_display`` — what the display timer calls — rather
    than waiting on Qt timer timing, so the result is collected on the
    GUI thread exactly as it is in the running app.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget to drive.

    Raises
    ------
    AssertionError
        If the recording does not finish before the deadline.
    """
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        widget.refresh_display()
        if widget._recording_job is None:  # noqa: SLF001
            return
        time.sleep(0.005)
    msg = "recording did not finish"
    raise AssertionError(msg)


def _only_recording(widget: LiveInstrumentWidget) -> object:
    """
    Return the single recording in the widget's session.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget whose session is read.

    Returns
    -------
    Recording
        The one recording written.
    """
    recordings = widget.session.recordings()
    assert len(recordings) == 1
    return recordings[0]


def test_a_scan_image_reads_every_channel_out_of_one_pass(tmp_path):
    """
    One pass, both detectors, and the two are one acquisition.

    Every channel rather than the displayed one because the pass happens
    either way: the second detector costs no extra dose and no extra
    time, and the images are registered to each other by construction.
    """
    viewer, widget, _ = _open(tmp_path)
    try:
        _enable_all_channels(widget)
        widget.acquire_scan_image()
        _finish_recording(widget)

        recording = _only_recording(widget)
        assert recording.frame_count == _BOTH_CHANNELS
    finally:
        widget.shutdown()
        viewer.close()


def test_a_scan_images_channels_share_one_pass(tmp_path):
    """
    The stored frames say they came from the same pass, rather than implying it.

    ``scan_pass_id`` is what lets a reader do per-pixel arithmetic
    between the channels — DPC, ratios — without having to guess from
    timestamps whether the probe moved in between.
    """
    viewer, widget, _ = _open(tmp_path)
    try:
        _enable_all_channels(widget)
        widget.acquire_scan_image()
        _finish_recording(widget)

        with h5py.File(_only_recording(widget).path, "r") as handle:
            stored = [
                json.loads(entry)
                for entry in handle["entry/metadata/frame_metadata_json"][()]
            ]

        assert len(stored) == _BOTH_CHANNELS
        assert [item["channel_index"] for item in stored] == [0, 1]
        assert stored[0]["scan_pass_id"] == stored[1]["scan_pass_id"]
        assert all(item["simultaneous_channels"] == [0, 1] for item in stored)
    finally:
        widget.shutdown()
        viewer.close()


def test_a_scan_image_is_one_acquisition_not_a_burst(tmp_path):
    """
    Acquiring an image ignores the "Frames" count beside it.

    That spin box belongs to "record N frames"; a scan image is one
    pass whatever it says, or the two controls would silently interact.
    """
    viewer, widget, _ = _open(tmp_path)
    try:
        _enable_all_channels(widget)
        widget._scan_count_spin.setValue(7)  # noqa: SLF001
        widget.acquire_scan_image()
        _finish_recording(widget)

        assert _only_recording(widget).frame_count == _BOTH_CHANNELS
    finally:
        widget.shutdown()
        viewer.close()


def test_a_camera_image_is_a_single_exposure(tmp_path):
    """One image is one frame, whatever the record count says."""
    viewer, widget, _ = _open(tmp_path, scan=False, camera=True)
    try:
        widget.acquire_camera_image()
        _finish_recording(widget)

        assert _only_recording(widget).frame_count == 1
    finally:
        widget.shutdown()
        viewer.close()


def test_a_camera_image_uses_the_panels_exposure(tmp_path):
    """
    The acquisition exposure is the one on the panel, not the live one.

    Read at the moment of acquisition rather than cached, so what an
    operator typed is what the exposure uses.
    """
    viewer, widget, _ = _open(tmp_path, scan=False, camera=True)
    try:
        binding = widget._binding(None)  # noqa: SLF001
        binding.exposure_spin.setValue(_AN_IMAGE_EXPOSURE_MS)

        taken = widget._image_parameters(binding)  # noqa: SLF001

        assert taken.exposure_ms == _AN_IMAGE_EXPOSURE_MS
        assert taken.binning == int(binding.binning_combo.currentText())
    finally:
        widget.shutdown()
        viewer.close()


def test_a_camera_image_leaves_the_live_settings_alone(tmp_path):
    """
    Taking one long exposure must not leave the live feed crawling.

    The live view and the acquisition share a camera, so the settings
    have to come back afterwards.
    """
    viewer, widget, devices = _open(tmp_path, scan=False, camera=True)
    try:
        camera = next(iter(devices.cameras.values()))
        live = camera.parameters()
        widget._binding(None).exposure_spin.setValue(_AN_IMAGE_EXPOSURE_MS)  # noqa: SLF001

        widget.acquire_camera_image()
        _finish_recording(widget)

        assert camera.parameters() == live
    finally:
        widget.shutdown()
        viewer.close()


def test_the_binning_choices_come_from_the_camera(tmp_path):
    """
    A camera that only does 1x has no business offering a 4x it refuses.

    The list is the device's own ``binning_values``, not a fixed one
    this panel invents.
    """
    viewer, widget, devices = _open(tmp_path, scan=False, camera=True)
    try:
        camera = next(iter(devices.cameras.values()))
        combo = widget._binding(None).binning_combo  # noqa: SLF001
        offered = [int(combo.itemText(i)) for i in range(combo.count())]
        assert offered == list(camera.binning_values)
    finally:
        widget.shutdown()
        viewer.close()


def test_a_spectrometer_gets_a_binning_control_per_axis(tmp_path):
    """
    Two controls where the axes differ, one where they do not.

    Binning rows buys signal-to-noise; binning channels costs energy
    resolution. A single control would make the cheap one unreachable
    without paying the dear one, so a camera that reports its axes
    separately gets a control for each — and one that does not keeps the
    single control it always had, because for it there is only one thing
    to choose.
    """
    viewer, widget, _ = _open(tmp_path, scan=False, camera=True, camera_count=2)
    try:
        ronchigram = widget._binding("ronchigram_camera")  # noqa: SLF001
        eels = widget._binding("eels_camera")  # noqa: SLF001

        assert ronchigram.binning_across_combo is None
        assert eels.binning_across_combo is not None

        rows = [
            int(eels.binning_combo.itemText(i))
            for i in range(eels.binning_combo.count())
        ]
        channels = [
            int(eels.binning_across_combo.itemText(i))
            for i in range(eels.binning_across_combo.count())
        ]
        assert rows == [1, 2, 4, 5, 10, 20, 25, 50, 100]
        assert channels == [1, 2]
    finally:
        widget.shutdown()
        viewer.close()


def test_acquiring_a_spectrometer_image_sends_both_binning_factors(tmp_path):
    """
    The two controls reach the device as one ``(y, x)`` pair.

    Reading only the first would silently bin both axes by the row
    factor, which is exactly the spectral resolution this feature exists
    to protect.
    """
    viewer, widget, devices = _open(
        tmp_path, scan=False, camera=True, camera_count=2
    )
    try:
        eels = widget._binding("eels_camera")  # noqa: SLF001
        eels.binning_combo.setCurrentText("100")
        eels.binning_across_combo.setCurrentText("1")

        taken = widget._image_parameters(eels)  # noqa: SLF001
        assert taken.binning_yx == (100, 1)

        camera = devices.cameras["eels_camera"]
        camera.configure(taken)
        rows, channels = camera.readout_shape
        # Rows binned away, every channel kept.
        assert (rows, channels) == (1, _EELS_CHANNELS)
    finally:
        widget.shutdown()
        viewer.close()


def test_acquiring_with_no_session_is_refused(tmp_path):
    """Nothing is saved without somewhere to save it, and the panel says so."""
    viewer, widget, _ = _open(tmp_path)
    try:
        widget.set_session(None)
        widget.acquire_scan_image()

        assert "no session" in widget._recording_status.text()  # noqa: SLF001
        assert widget._recording_job is None  # noqa: SLF001
    finally:
        widget.shutdown()
        viewer.close()
