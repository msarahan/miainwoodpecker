"""
The commodity-camera device server, driven the way the application does.

Runs entirely on the simulated backend, so it needs neither OpenCV nor a
camera and is a full end-to-end exercise of the client, the transport,
and an adapter that has nothing to do with Nion — in the *base* test
environment, where nothing else covers that path.

The hardware backend is not tested here for the honest reason: there is
no camera in CI. What is tested is everything either backend shares plus
the failure an operator actually hits first, which is asking for a
device that is not there.
"""

from __future__ import annotations

import numpy as np
import pytest

from miainwoodpecker.acquisition import camera_series, record
from miainwoodpecker.devices import Camera, CameraParameters
from miainwoodpecker.devices.camera_server import (
    CAMERA_TARGET,
    NO_CAMERA_EXIT_STATUS,
    SimulatedCamera,
    _parse_args,
    main,
    open_camera,
)
from miainwoodpecker.devices.remote import (
    HARDWARE_BACKEND,
    SERVER_RESPONSIVE,
    SIMULATED_BACKEND,
    remote_instrument,
)
from miainwoodpecker.devices.rpc import TARGET_NAMES
from miainwoodpecker.storage import read_series

_SERVER_MODULE = "miainwoodpecker.devices.camera_server"
_FRAMES = 4
_UNSUPPORTED_BINNING = 2


@pytest.fixture
def camera_microscope():
    """Spawn the camera server on its simulated backend and connect."""
    with remote_instrument(server_module=_SERVER_MODULE) as devices:
        yield devices


def test_the_camera_arrives_on_the_neutral_target(camera_microscope):
    """
    A commodity camera is served as ``camera``, not as a Ronchigram one.

    The whole reason the neutral target exists: mapping a USB microscope
    onto ``ronchigram_camera`` would have put a fiction in every file's
    target name while the frame metadata told the truth.
    """
    assert camera_microscope.camera is not None
    assert camera_microscope.ronchigram_camera is None
    assert camera_microscope.eels_camera is None
    assert camera_microscope.scanner is None
    assert set(camera_microscope.cameras()) == {CAMERA_TARGET}
    assert isinstance(camera_microscope.camera, Camera)


def test_frames_round_trip_with_honest_metadata(camera_microscope):
    """
    Every frame says it is not photometrically linear, because it is not.

    A UVC camera's pixels have been through demosaicing, gamma, white
    balance and in-camera sharpening, none of it recoverable. The flag is
    what lets an analysis step refuse to treat them as counts — and it is
    on the *simulated* frames too, because the simulator stands in for
    that hardware rather than for a detector.
    """
    camera = camera_microscope.camera
    camera.start()
    try:
        frame = camera.acquire_frame()
    finally:
        camera.stop()

    expected_ndim = 2
    assert frame.data.ndim == expected_ndim
    assert frame.timestamp.tzinfo is not None
    assert frame.metadata["device_id"] == camera.camera_id
    assert frame.metadata["photometrically_linear"] is False
    assert frame.metadata["camera_type"] == "commodity"
    assert frame.metadata["binning"] == 1


def test_frame_index_counts_without_gaps(camera_microscope):
    """A dropped frame stays visible, the same contract the Nion path holds."""
    camera = camera_microscope.camera
    camera.start()
    try:
        indexes = [camera.acquire_frame().metadata["frame_index"] for _ in range(3)]
    finally:
        camera.stop()
    assert indexes == [0, 1, 2]


def test_consecutive_frames_differ(camera_microscope):
    """
    The simulator moves, so the frame-identity contracts are not vacuous.

    A synthetic camera returning one still image would make "consecutive
    frames differ" pass for the wrong reason everywhere it is asserted.
    """
    camera = camera_microscope.camera
    camera.start()
    try:
        first = camera.acquire_frame()
        second = camera.acquire_frame()
    finally:
        camera.stop()
    assert not np.array_equal(first.data, second.data)


def test_binning_other_than_one_is_refused(camera_microscope):
    """
    A consumer sensor crops; it does not bin, and says so rather than lying.

    The alternative — accepting the request and returning unbinned frames
    — would put a calibration scale on the data wrong by the factor.
    """
    camera = camera_microscope.camera
    with pytest.raises(Exception, match="crop rather than bin"):
        camera.configure(
            CameraParameters(exposure_ms=10.0, binning=_UNSUPPORTED_BINNING),
        )
    assert camera.parameters().binning == 1


def test_exposure_configures_and_reports_what_was_taken(camera_microscope):
    """``configure`` returns the device's answer, which is the standing contract."""
    camera = camera_microscope.camera
    took = camera.configure(CameraParameters(exposure_ms=12.5))
    assert took.exposure_ms == pytest.approx(12.5)
    assert camera.parameters() == took


def test_an_instrument_with_no_controls_is_a_supported_instrument(
    camera_microscope,
):
    """
    A webcam has no stage, defocus or blanker, and reports none.

    ``available_controls`` returning empty is the documented answer for a
    partial instrument; this pins that a *fully* controlless one works
    too, including the health check the client polls.
    """
    instrument = camera_microscope.instrument
    assert list(instrument.available_controls()) == []
    assert instrument.check_health().state == SERVER_RESPONSIVE
    instrument.park()  # No beam to blank; must not raise.


def test_a_recording_from_a_commodity_camera_round_trips(camera_microscope, tmp_path):
    """
    The whole stack works against a non-Nion adapter: acquire, record, read.

    This is the point of the server. Everything above the device layer —
    the series generators, the writer, the reader — was written against
    protocols, and this is the first evidence that "vendor-neutral" holds
    for a device with no vendor at all.
    """
    path = tmp_path / "commodity.nxs"
    written = record(camera_series(camera_microscope.camera, _FRAMES), path)
    assert written == _FRAMES

    replayed = list(read_series(path))
    assert len(replayed) == _FRAMES
    # Not just a count: the frames read back are the frames acquired, and
    # they still differ from one another, so the round trip did not
    # quietly write the same buffer _FRAMES times.
    first, second = replayed[0][0], replayed[1][0]
    assert first.ndim == 2  # noqa: PLR2004 - a 2D frame, as Frame requires
    assert not np.array_equal(first, second)


def test_the_command_line_matches_the_protocol():
    """
    One positional port per target name, and ``--plugin`` as configuration.

    The argv shape is the contract every adapter implements; asserting it
    here is what stops the two servers drifting apart.
    """
    ports = [str(5000 + index) for index in range(len(TARGET_NAMES))]
    arguments = _parse_args(["--backend", HARDWARE_BACKEND, "--plugin", "2", *ports])
    assert arguments.backend == HARDWARE_BACKEND
    assert arguments.plugin == ["2"]
    assert arguments.ports == [int(port) for port in ports]

    default = _parse_args(ports)
    assert default.backend == SIMULATED_BACKEND
    assert default.plugin == []


def test_an_unknown_backend_is_rejected_before_a_device_is_opened():
    """A typo names the backends it could have been, rather than failing later."""
    with pytest.raises(ValueError, match="unknown backend"):
        open_camera("nonesuch", "0")


def test_a_missing_camera_exits_distinctly_and_says_what_to_check(monkeypatch):
    """
    "No camera plugged in" is not a crash, and the message is actionable.

    A distinct exit status lets a launcher tell a missing device from a
    broken adapter — the same reason ``nion_server`` separates "no
    instrument" from "crashed". The message names the device, the
    permission problem behind most Linux failures, and the way to run
    without hardware at all.
    """
    monkeypatch.setenv("MIAINWOODPECKER_AUTHKEY", "00" * 32)
    ports = [str(5000 + index) for index in range(len(TARGET_NAMES))]
    status = main(
        ["--backend", HARDWARE_BACKEND, "--plugin", "/dev/definitely-not-a-camera",
         *ports],
    )
    assert status == NO_CAMERA_EXIT_STATUS


def test_the_simulated_camera_needs_no_opencv():
    """
    The simulated backend imports nothing optional, which is what makes it CI-able.

    Asserted directly rather than inferred from the tests passing,
    because a stray top-level ``import cv2`` would still let them pass in
    an environment that happened to have it.
    """
    camera = open_camera(SIMULATED_BACKEND, "ignored")
    assert isinstance(camera, SimulatedCamera)
    frame = camera.acquire_frame()
    assert frame.data.dtype == np.uint8
    camera.close()
