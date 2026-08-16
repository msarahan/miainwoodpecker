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

import typing

import numpy as np
import pytest

from miainwoodpecker.acquisition import camera_series, record
from miainwoodpecker.devices import (
    IMAGE_READOUT,
    PROJECTED_READOUT,
    Camera,
    CameraParameters,
    Instrument,
    camera_server,
)
from miainwoodpecker.devices.camera_server import (
    CAMERA_TARGET,
    DISCOVERY_MAX_INDEX,
    NO_CAMERA_EXIT_STATUS,
    CameraOpenError,
    ServerInstrument,
    SimulatedCamera,
    _devices_to_serve,
    _parse_args,
    discover_devices,
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

if typing.TYPE_CHECKING:
    from collections.abc import Callable

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


# --------------------------------------------------------------------------
# Discovery: which cameras get served when nobody says.
# --------------------------------------------------------------------------


def _candidate(index: int, *, delivers: bool = True) -> dict[str, object]:
    """
    Return one probe result, in the shape ``probe_capture_devices`` returns.

    Parameters
    ----------
    index : int
        The capture index it was found at.
    delivers : bool
        Whether it managed to read a frame.

    Returns
    -------
    dict[str, object]
        One candidate.
    """
    return {
        "index": index,
        "width": 640,
        "height": 480,
        "backend": "TEST",
        "frame": (480, 640, 3) if delivers else None,
    }


def test_a_camera_that_opens_but_delivers_nothing_is_not_served(monkeypatch):
    """
    Discovery keeps only the devices that produced a frame.

    An open is not a working camera on this class of hardware — a
    microscope behind an underpowered hub opens and then delivers
    nothing — and serving it would put a section in the viewer that can
    never show an image, which is the failure this project refuses
    everywhere else.
    """
    monkeypatch.setattr(
        camera_server,
        "probe_capture_devices",
        lambda *_args, **_kwargs: [
            _candidate(0, delivers=False),
            _candidate(1),
        ],
    )

    assert discover_devices() == ["1"]


def test_naming_a_device_skips_discovery_entirely(monkeypatch):
    """
    ``--plugin`` is taken literally, and nothing is added to it.

    An operator who names a camera has answered the question. Adding a
    webcam they did not ask for would be worse than useless on an
    instrument, so this asserts discovery is not merely overridden but
    never runs.
    """
    def _explode(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        message = "discovery ran when a device was named"
        raise AssertionError(message)

    monkeypatch.setattr(camera_server, "probe_capture_devices", _explode)

    assert _devices_to_serve(HARDWARE_BACKEND, ["1"]) == ["1"]
    assert _devices_to_serve(HARDWARE_BACKEND, ["/dev/video2", "0"]) == [
        "/dev/video2",
        "0",
    ]


def test_the_simulated_backend_never_discovers(monkeypatch):
    """
    There is nothing to discover: the simulator synthesises frames.

    It also must not probe, because probing needs OpenCV and the whole
    point of the simulated backend is that it needs nothing installed —
    a probe here would make the CI path depend on the ``camera`` extra.
    """
    def _explode(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        message = "the simulated backend probed for hardware"
        raise AssertionError(message)

    monkeypatch.setattr(camera_server, "probe_capture_devices", _explode)

    assert _devices_to_serve(SIMULATED_BACKEND, []) == ["0"]


def test_discovering_nothing_says_so_rather_than_opening_index_zero(monkeypatch):
    """
    "No camera found" is its own diagnosis, not a failure to open "0".

    The distinction matters because the fix differs: there is nothing to
    correct in the command line, so the message says what was searched
    and offers the platform's likely reason rather than naming a device
    the operator never chose.
    """
    monkeypatch.setattr(
        camera_server, "probe_capture_devices", lambda *_a, **_k: [],
    )

    with pytest.raises(CameraOpenError) as raised:
        _devices_to_serve(HARDWARE_BACKEND, [])

    message = str(raised.value)
    assert "no camera was found" in message
    assert str(DISCOVERY_MAX_INDEX) in message
    assert "--plugin" in message


def test_every_discovered_camera_is_served_in_index_order(monkeypatch):
    """
    All of them, not the first: that is the whole point of discovering.

    Order is by index so the mapping from slot to device is at least
    stable between runs on an unchanged machine — which is not an
    identity claim, and the metadata still carries ``device_id``.
    """
    monkeypatch.setattr(
        camera_server,
        "probe_capture_devices",
        lambda *_a, **_k: [_candidate(0), _candidate(2), _candidate(3)],
    )

    assert _devices_to_serve(HARDWARE_BACKEND, []) == ["0", "2", "3"]


class _FakeCapture:
    """One capture device, standing in for ``cv2.VideoCapture``."""

    def __init__(self, index: int, present: frozenset[int]) -> None:
        self._index = index
        self._present = present
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802 - OpenCV's spelling
        """Return whether this index exists on the fake machine."""
        return self._index in self._present

    def read(self) -> tuple[bool, object]:
        """Return one frame, as a shaped stand-in."""
        return True, np.zeros((4, 4), dtype=np.uint8)

    def get(self, _property: int) -> float:
        """Return a plausible number for any property asked about."""
        return 640.0

    def getBackendName(self) -> str:  # noqa: N802 - OpenCV's spelling
        """Return a backend name."""
        return "FAKE"

    def release(self) -> None:
        """Record that this device was released."""
        self.released = True


class _FakeCv2:
    """Enough of ``cv2`` for the probe, and a record of what it opened."""

    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4

    def __init__(self, present: frozenset[int]) -> None:
        self._present = present
        self.opened: list[int] = []
        self.captures: list[_FakeCapture] = []

    def VideoCapture(self, index: int) -> _FakeCapture:  # noqa: N802 - OpenCV's
        """Open a fake capture device and remember the attempt."""
        self.opened.append(index)
        capture = _FakeCapture(index, self._present)
        self.captures.append(capture)
        return capture


@pytest.fixture
def fake_cv2(monkeypatch: pytest.MonkeyPatch) -> Callable[[set[int]], _FakeCv2]:
    """
    Install a stand-in ``cv2`` so the probe's own loop can be tested.

    The loop's stopping rule is the part with a real bug in it, and no
    machine in CI has the camera layout that would exercise it. A fake
    module is the only way to ask "what would this do on a laptop whose
    index 0 is taken?" — which is the ordinary case on macOS.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Used to install the module and restore it afterwards.

    Returns
    -------
    Callable[[set[int]], _FakeCv2]
        Call it with the indices that exist; it returns the fake.
    """
    import sys  # noqa: PLC0415 - only this fixture needs it

    def install(present: set[int]) -> _FakeCv2:
        module = _FakeCv2(frozenset(present))
        monkeypatch.setitem(sys.modules, "cv2", module)
        return module

    return install


def test_a_hole_at_index_zero_does_not_hide_the_camera_behind_it(fake_cv2):
    """
    A laptop whose built-in camera is disabled or held still finds the microscope.

    This is why the stopping rule tolerates a run of misses rather than
    one. Stopping at the first would report "no camera" on exactly the
    machine this feature exists for.
    """
    fake = fake_cv2({1})

    found = discover_devices()

    assert found == ["1"]
    assert 0 in fake.opened


def test_discovery_stops_after_a_run_of_misses_rather_than_scanning_to_the_end(
    fake_cv2,
):
    """
    The scan is bounded by consecutive misses, not only by the ceiling.

    Every probe costs a real open attempt, and the client's connect
    deadline is spent while it happens — so a machine with one camera
    must not pay for eight tries.
    """
    fake = fake_cv2({0})

    assert discover_devices() == ["0"]
    # 0 hits, then three misses end it: far short of DISCOVERY_MAX_INDEX.
    assert fake.opened == [0, 1, 2, 3]
    assert max(fake.opened) < DISCOVERY_MAX_INDEX


def test_probing_releases_every_device_it_opened(fake_cv2):
    """
    A probe must not leave a camera claimed for the server that follows.

    DirectShow admits one consumer at a time, so a device left open by
    discovery would be a device the server could not then serve — the
    probe would have caused the failure it exists to diagnose.
    """
    fake = fake_cv2({0, 1})

    discover_devices()

    assert fake.captures
    assert all(capture.released for capture in fake.captures)
