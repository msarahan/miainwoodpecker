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
from miainwoodpecker.devices import (
    IMAGE_READOUT,
    PROJECTED_READOUT,
    Camera,
    CameraParameters,
    Instrument,
)
from miainwoodpecker.devices.camera_server import (
    CAMERA_TARGET,
    NO_CAMERA_EXIT_STATUS,
    ServerInstrument,
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


def test_a_controlless_instrument_satisfies_the_instrument_protocol():
    """
    ``ServerInstrument`` passes the runtime check with zero controls.

    This adapter was one of the two that used to *fail* ``isinstance``
    while working perfectly: the old all-or-nothing check against the
    full controller demanded a stage, a defocus and a blanker no webcam
    has. The check now asks for the core every instrument target serves
    — identity, ``available_controls()``, ``park()`` — so an honest
    empty capability list and a passing type check are no longer in
    contradiction. The Gatan bridge's one-control instrument pins the
    same fix from the other direction.
    """
    import threading  # noqa: PLC0415 - one line, only this test needs it

    instrument = ServerInstrument({}, threading.Event(), SIMULATED_BACKEND)
    assert isinstance(instrument, Instrument)
    assert list(instrument.available_controls()) == []


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
    One port, for ``instrument``, and ``--plugin`` as configuration.

    The argv shape is the contract every adapter implements; asserting it
    here is what stops the servers drifting apart. One port rather than
    one per target is the whole of what let the target list stop being a
    fixed tuple: everything else binds where the OS says and is reported
    through ``describe()``.
    """
    arguments = _parse_args(
        ["--backend", HARDWARE_BACKEND, "--plugin", "2", "--instrument-port", "5000"],
    )
    assert arguments.backend == HARDWARE_BACKEND
    assert arguments.plugin == ["2"]
    assert arguments.instrument_port == 5000  # noqa: PLR2004 - the port passed above

    default = _parse_args(["--instrument-port", "5000"])
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
    status = main(
        ["--backend", HARDWARE_BACKEND, "--plugin", "/dev/definitely-not-a-camera",
         "--instrument-port", "5000"],
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


def test_a_projected_readout_is_refused_by_a_camera_with_no_dispersive_axis(
    camera_microscope,
):
    """
    A consumer sensor has nothing to project along, and refuses in a sentence.

    The same rule as the binning refusal above and for a sharper reason:
    silently imaging would hand back a 2D frame to a caller that asked
    for a spectrum, and the recording path — which dispatches on the
    frames' rank — would then write it as an image with nothing
    anywhere saying the request was ignored.
    """
    camera = camera_microscope.camera
    with pytest.raises(Exception, match="no dispersive axis to project along"):
        camera.configure(
            CameraParameters(exposure_ms=10.0, readout=PROJECTED_READOUT),
        )
    assert camera.parameters().readout == IMAGE_READOUT


@pytest.fixture
def two_camera_microscope():
    """Spawn the camera server with two simulated cameras and connect."""
    with remote_instrument(
        server_module=_SERVER_MODULE,
        plugin_names=["0", "1"],
    ) as devices:
        yield devices


def test_every_requested_device_gets_its_own_target(two_camera_microscope):
    """
    Two cameras plugged in are two targets, both live at once.

    The case that motivated this: a laptop with a built-in webcam and a
    USB microscope has two cameras, and an operator wants the microscope
    without unplugging anything. Serving one and dropping the other used
    to be the only option, because the target list was a fixed tuple.

    The first keeps the plain ``camera`` name every existing recording
    uses; the second is numbered from two.
    """
    served = two_camera_microscope.instrument.describe()["targets"]
    assert served == [CAMERA_TARGET, f"{CAMERA_TARGET}:2"]

    cameras = two_camera_microscope.cameras()
    assert list(cameras) == [CAMERA_TARGET, f"{CAMERA_TARGET}:2"]
    for camera in cameras.values():
        assert camera.acquire_frame().data.shape == (480, 640)


def test_the_named_camera_slot_still_holds_the_first(two_camera_microscope):
    """
    The extra camera does not displace the one the named field points at.

    ``.camera`` is what the viewer and every script reach for, so a
    second device must not change what a caller written before this
    existed already gets.
    """
    assert two_camera_microscope.camera is not None
    assert (
        two_camera_microscope.cameras()[CAMERA_TARGET]
        is two_camera_microscope.camera
    )
    assert set(two_camera_microscope.additional_cameras) == {f"{CAMERA_TARGET}:2"}


def test_targets_beyond_the_fixed_tuple_report_their_own_port(
    two_camera_microscope,
):
    """
    The server says where each target listens, because the client cannot know.

    The client allocates one port, for ``instrument``. Everything else
    binds where the OS says and is reported through ``describe()``,
    which is what makes ``camera:2`` — a name that is not in
    ``TARGET_NAMES`` and that no client could have allocated for —
    reachable at all.
    """
    endpoints = two_camera_microscope.instrument.describe()["endpoints"]
    extra = endpoints[f"{CAMERA_TARGET}:2"]

    assert extra["kind"] == "camera"
    assert isinstance(extra["port"], int)
    assert extra["port"] > 0
    # Every served target reports an endpoint, with no exception for the
    # one port the client did allocate, so there is only ever one source
    # of truth for where a target is.
    assert set(endpoints) == {CAMERA_TARGET, f"{CAMERA_TARGET}:2", "instrument"}
    assert f"{CAMERA_TARGET}:2" not in TARGET_NAMES
