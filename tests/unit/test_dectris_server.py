"""
The DECTRIS device server, driven the way the application drives it.

Runs entirely on the simulated backend, which for this adapter means
something stronger than it does for the others: the simulated backend is a
mock *control unit*, serving the documented SIMPLON resources over real
HTTP, and the tests below drive the same client code the hardware backend
uses. So "the simulated path passes" here means URL construction, JSON
shapes, HTTP statuses, the armed-state rules and TIFF decoding were all
exercised — not that a stub returned an array.

What is deliberately **not** claimed: none of this has met a detector. The
protocol is implemented from DECTRIS's published SIMPLON reference and the
open-source clients that speak it, and the entries in
docs/adapters/dectris.md say which parts a real ELA still has to confirm.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import socket
import threading

import numpy as np
import pytest

from miainwoodpecker.acquisition import camera_series, record
from miainwoodpecker.devices import Camera, CameraParameters
from miainwoodpecker.devices.dectris_server import (
    CAMERA_TARGET,
    NO_DETECTOR_EXIT_STATUS,
    SIMPLON_API_VERSION,
    DectrisCamera,
    DetectorOpenError,
    SimplonClient,
    SimplonError,
    SimulatedControlUnit,
    _decode_baseline_tiff,
    _open_failure_hint,
    _parse_args,
    decode_tiff,
    encode_tiff,
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

_SERVER_MODULE = "miainwoodpecker.devices.dectris_server"
_FRAMES = 3
_UNSUPPORTED_BINNING = 2
_ELA_PIXEL_PITCH_M = 75e-6
_LONGER_EXPOSURE_MS = 25.0
_SHORTER_EXPOSURE_MS = 4.0
_NOT_FOUND = 404


_TWO_DIMENSIONS = 2


@pytest.fixture
def detector():
    """Open a camera against a mock control unit, in this process."""
    camera = open_camera(SIMULATED_BACKEND, "ignored")
    try:
        yield camera
    finally:
        camera.close()


@pytest.fixture
def dectris_microscope():
    """Spawn the DECTRIS server on its simulated backend and connect."""
    with remote_instrument(server_module=_SERVER_MODULE) as devices:
        yield devices


# ---------------------------------------------------------------------------
# What the frames say about themselves
# ---------------------------------------------------------------------------


def test_the_detector_arrives_on_the_neutral_target(dectris_microscope):
    """
    A direct detector is a camera and nothing else, and no scanner is missing.

    This is the detector-only instrument shape the vendor-support audit
    made work, now with a real adapter behind it rather than a test
    fixture: no scan unit, no Nion-shaped camera slots, and the neutral
    ``camera`` target — because an ELA is not a Ronchigram camera and
    calling it one would put a fiction in every recording's target name.
    """
    assert dectris_microscope.camera is not None
    assert dectris_microscope.scanner is None
    assert dectris_microscope.ronchigram_camera is None
    assert dectris_microscope.eels_camera is None
    assert set(dectris_microscope.cameras()) == {CAMERA_TARGET}
    assert isinstance(dectris_microscope.camera, Camera)


def test_frames_claim_photometric_linearity_and_are_entitled_to(dectris_microscope):
    """
    The flag is True here, and that is a statement about the physics.

    ``camera_server`` sets ``photometrically_linear: False`` because a UVC
    camera's pixels have been through demosaicing, gamma and white balance
    that cannot be undone. A hybrid-pixel detector has none of that: it
    discriminates each electron against a threshold and increments an
    integer, so the stored value *is* a count. Asserting both the flag and
    ``counts_per_electron`` together is the point — the second is why the
    first is allowed to be True, and a future adapter that set the flag
    without meaning it would fail here.
    """
    camera = dectris_microscope.camera
    camera.start()
    try:
        frame = camera.acquire_frame()
    finally:
        camera.stop()

    expected_ndim = 2
    assert frame.data.ndim == expected_ndim
    assert frame.data.dtype.kind == "u"  # counts, not a float rescaling
    assert frame.timestamp.tzinfo is not None
    assert frame.metadata["photometrically_linear"] is True
    assert frame.metadata["counts_per_electron"] == 1.0
    assert frame.metadata["camera_type"] == "hybrid_pixel_counting"
    assert frame.metadata["device_id"] == camera.camera_id
    # Recorded next to the flag rather than instead of it: count-rate
    # paralysis is the one thing that makes a counting detector nonlinear,
    # and whether the DCU corrected for it is a property of the frame.
    assert "countrate_correction_applied" in frame.metadata


def test_the_beam_energy_is_not_borrowed_from_the_detector(detector):
    """
    ``high_tension_v`` is absent, because the detector does not measure it.

    A DCU publishes ``incident_energy``, which is tempting: on a 100 kV
    column it will read 100 keV. But it is the energy the detector was
    *configured for* — it sets the discriminator threshold — not a reading
    from the column, and nothing stops it being stale or simply wrong. The
    vocabulary's rule is to omit rather than substitute, so it is recorded
    under its own name and the instrument key stays empty for an adapter
    that can actually measure it.
    """
    detector.start()
    try:
        frame = detector.acquire_frame()
    finally:
        detector.stop()
    assert "high_tension_v" not in frame.metadata
    assert frame.metadata["incident_energy_ev"] == pytest.approx(100_000.0)
    assert frame.metadata["threshold_energy_ev"] == pytest.approx(50_000.0)


def test_no_calibration_is_published_but_the_pixel_pitch_is(detector):
    """
    An uncalibrated axis stays uncalibrated; the detector constant is kept.

    The DCU knows exactly one length: its own pixel pitch, 75 µm. Turning
    that into a sample-plane, reciprocal or energy axis needs the camera
    length or the spectrometer dispersion, and both live in the microscope
    rather than in the detector — so publishing a ``calibration`` here
    would be inventing the optics. The pitch is recorded so a caller who
    *does* know them can finish the job, which is what
    ``FrameCalibration``'s explicit constructors are for.
    """
    detector.start()
    try:
        frame = detector.acquire_frame()
    finally:
        detector.stop()
    assert "calibration" not in frame.metadata
    assert frame.metadata["x_pixel_size_m"] == pytest.approx(_ELA_PIXEL_PITCH_M)
    assert frame.metadata["y_pixel_size_m"] == pytest.approx(_ELA_PIXEL_PITCH_M)


def test_frame_index_counts_without_gaps(dectris_microscope):
    """A dropped frame stays visible, the same contract every adapter holds."""
    camera = dectris_microscope.camera
    camera.start()
    try:
        indexes = [camera.acquire_frame().metadata["frame_index"] for _ in range(3)]
    finally:
        camera.stop()
    assert indexes == [0, 1, 2]


def test_consecutive_frames_differ(dectris_microscope):
    """
    The simulated detector has counting statistics, so nothing is vacuous.

    Poisson noise rather than Gaussian, deliberately: the whole reason to
    buy a counting detector is that its noise *is* counting statistics, and
    a simulator with the wrong noise model would make the frame-identity
    contracts pass for the wrong reason and any statistical assertion
    written against it meaningless.
    """
    camera = dectris_microscope.camera
    camera.start()
    try:
        first = camera.acquire_frame()
        second = camera.acquire_frame()
    finally:
        camera.stop()
    assert not np.array_equal(first.data, second.data)


def test_the_simulated_frame_looks_like_an_eels_spectrum_image(detector):
    """
    The mock produces a zero-loss peak, because that is the target workload.

    The ELA this adapter was written for sits on a Nion IRIS spectrometer
    on a monochromated 100 kV column, where it is an *EELS* detector: one
    axis disperses energy and the other does not. A simulator producing
    uniform noise would let a dispersive-axis mistake pass unnoticed, so
    the synthetic frame has the shape the real one does — an intense,
    narrow peak near one end of the fast axis, and a falling tail.
    """
    detector.start()
    try:
        frame = detector.acquire_frame()
    finally:
        detector.stop()
    column_totals = frame.data.sum(axis=0)
    peak_column = int(np.argmax(column_totals))
    # The peak is near the low-loss end and is far brighter than the tail.
    assert peak_column < frame.data.shape[1] // 4
    assert column_totals[peak_column] > 10 * np.median(column_totals)


# ---------------------------------------------------------------------------
# Configuration, and the SIMPLON rules that shape it
# ---------------------------------------------------------------------------


def test_binning_other_than_one_is_refused_for_a_physical_reason(dectris_microscope):
    """
    A counting pixel array cannot bin, which is not the same as declining to.

    A consumer sensor crops instead of binning. This is a stronger claim:
    each pixel owns its discriminator and counter, so there is no charge to
    sum anywhere. Summing neighbours on the host would return a smaller
    array whose pixel size no longer matches the sensor — a calibration
    wrong by the factor, with nothing saying so.
    """
    camera = dectris_microscope.camera
    assert list(camera.binning_values) == [1]
    with pytest.raises(Exception, match="no charge to sum"):
        camera.configure(
            CameraParameters(exposure_ms=10.0, binning=_UNSUPPORTED_BINNING),
        )
    assert camera.parameters().binning == 1


def test_exposure_reaches_the_detector_and_is_read_back(detector):
    """
    ``configure`` reports the device's answer, and here it is the DCU's.

    Checked at both ends: the value the client returns, and the
    ``count_time`` the control unit is actually holding. A ``configure``
    that updated only a local field would pass the first assertion alone,
    which is exactly the bug an in-process stub could not catch.
    """
    took = detector.configure(CameraParameters(exposure_ms=_LONGER_EXPOSURE_MS))
    assert took.exposure_ms == pytest.approx(_LONGER_EXPOSURE_MS)
    assert detector.parameters() == took

    config = detector.control_unit.model.config
    assert config["count_time"] == pytest.approx(_LONGER_EXPOSURE_MS / 1000.0)


def test_frame_time_never_falls_below_count_time(detector):
    """
    The two are written in whichever order keeps the invariant true.

    SIMPLON rejects a ``count_time`` longer than the ``frame_time`` that
    bounds it, so a fixed write order is wrong in one direction or the
    other: writing ``count_time`` first breaks when the exposure grows,
    writing ``frame_time`` first breaks when it shrinks. Both directions
    are exercised here because only testing one would leave a bug that
    appears the first time an operator turns the exposure down.
    """
    config = detector.control_unit.model.config
    for exposure_ms in (_LONGER_EXPOSURE_MS, _SHORTER_EXPOSURE_MS):
        detector.configure(CameraParameters(exposure_ms=exposure_ms))
        assert config["frame_time"] >= config["count_time"]
        assert config["count_time"] == pytest.approx(exposure_ms / 1000.0)


def test_configuring_while_armed_disarms_and_rearms(detector):
    """
    The detector's configuration is read-only while armed, so this dances.

    The ``Camera`` contract allows ``configure`` at any point, including
    while started — "which frame first shows the change is the device's
    business". SIMPLON's business, it turns out, is to refuse the write
    outright, so the adapter disarms, writes and re-arms. Both halves are
    asserted: that the mock really does refuse a direct write while armed
    (otherwise this test would pass against an adapter that never
    disarmed), and that the camera is still armed and producing frames
    afterwards.
    """
    model = detector.control_unit.model
    detector.start()
    try:
        assert model.state == "ready"
        client = SimplonClient(detector.control_unit.address)
        with pytest.raises(SimplonError, match="read-only while armed"):
            client.put_config("count_time", 0.5)

        detector.configure(CameraParameters(exposure_ms=_SHORTER_EXPOSURE_MS))
        assert model.state == "ready"
        assert model.config["count_time"] == pytest.approx(
            _SHORTER_EXPOSURE_MS / 1000.0,
        )
        frame = detector.acquire_frame()
    finally:
        detector.stop()
    assert frame.metadata["exposure_ms"] == pytest.approx(_SHORTER_EXPOSURE_MS)
    assert model.state == "idle"


def test_start_arms_the_detector_and_stop_disarms_it(detector):
    """
    ``start``/``stop`` are arm/disarm, which is what makes the interlock work.

    Nothing else can hold an armed series while this one does, so leaving
    the detector armed after ``stop()`` would quietly deny it to Gatan's
    GMS, Nion Swift or LiberTEM-live. Asserting the *detector's* state
    rather than the client's flag is the point.
    """
    model = detector.control_unit.model
    assert model.state == "idle"
    detector.start()
    assert model.state == "ready"
    assert detector.series_id is not None
    detector.stop()
    assert model.state == "idle"
    assert detector.series_id is None


def test_acquiring_before_start_says_so(detector):
    """The protocol's ordering contract, refused by name rather than hanging."""
    with pytest.raises(RuntimeError, match="not armed"):
        detector.acquire_frame()


def test_the_control_unit_refuses_a_trigger_before_arming(detector):
    """
    The mock is a state machine, not a yes-machine.

    Worth asserting directly: the value of a protocol mock is that it can
    say no, and a test suite that never sees it say no has not established
    that it would.
    """
    client = SimplonClient(detector.control_unit.address)
    with pytest.raises(SimplonError, match="arm first"):
        client.command("trigger")


def test_a_long_series_is_rearmed_transparently(detector):
    """
    Running out of triggers is the adapter's problem, not the caller's.

    A pull-per-frame caller has no idea a SIMPLON series exists or how
    many triggers it was armed for, so the boundary has to be invisible.
    Forced here by shortening the series to one trigger rather than
    waiting for 65 536.
    """
    model = detector.control_unit.model
    detector.start()
    try:
        first = detector.acquire_frame()
        model.triggers_left = 0  # the series is spent
        second = detector.acquire_frame()
    finally:
        detector.stop()
    assert second.metadata["frame_index"] == first.metadata["frame_index"] + 1
    assert second.metadata["series_id"] != first.metadata["series_id"]


# ---------------------------------------------------------------------------
# Failure diagnostics
# ---------------------------------------------------------------------------


def test_an_already_armed_detector_names_who_might_have_it():
    """
    The other half of the ownership interlock, and the realistic failure.

    An ELA is usually *also* reachable from Gatan's GMS (where it is sold
    as the Stela camera) and from LiberTEM-live. SIMPLON lets anyone read
    and write configuration but only one client can hold an armed series,
    so "someone else has it" is the failure this adapter will hit most
    often on a working instrument — and it must not present as a mystery.
    """
    camera = open_camera(SIMULATED_BACKEND, "ignored")
    unit = camera.control_unit
    try:
        camera.start()  # stands in for the other application
        with pytest.raises(DetectorOpenError, match="another client"):
            DectrisCamera(unit.address)
    finally:
        camera.close()


def test_an_unreachable_control_unit_names_the_address_and_a_way_forward():
    """
    A dead address produces a diagnosis, not a bare traceback.

    Deliberately does *not* assert which transport branch fires. Whether
    connecting to a free loopback port is refused or times out is the
    platform's choice, not this adapter's: Linux sends an immediate RST
    and produces ``ConnectionRefusedError``, while macOS and Windows were
    both observed in CI taking the timeout path instead. Asserting
    "Nothing is listening" here made a real cross-platform difference
    look like a bug in the hint.

    What is genuinely this adapter's contract, and is asserted: the
    message names the address that failed, and tells the operator how to
    proceed without a detector. Which sentence each transport cause gets
    is pinned separately and platform-independently in
    :func:`test_each_transport_failure_gets_its_own_sentence`, by handing
    the hint function the causes directly rather than trying to provoke
    them through a real socket.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        address = f"127.0.0.1:{port}"
    with pytest.raises(DetectorOpenError) as raised:
        DectrisCamera(address, timeout_s=2.0)
    message = str(raised.value)
    assert str(port) in message
    assert "--backend simulated" in message


def test_something_that_is_not_a_control_unit_names_the_api_version():
    """
    A box that answers HTTP but not SIMPLON is a distinct, diagnosable case.

    The API version is part of the URL path, so a DCU on different
    firmware fails exactly like a wrong address does — with a 404 — and
    the difference matters because one is fixed by re-cabling and the
    other by checking the firmware.
    """
    with _always_404_server() as address, pytest.raises(DetectorOpenError) as raised:
        DectrisCamera(address, timeout_s=2.0)
    assert SIMPLON_API_VERSION in str(raised.value)
    assert "not a SIMPLON control unit" in str(raised.value)


def test_each_transport_failure_gets_its_own_sentence():
    """
    The hint function branches on cause, and every branch says something.

    Asserted directly as well as end to end, because the cases that matter
    most are the ones hardest to provoke in a test: a name that does not
    resolve, and a connection that times out rather than being refused —
    which is what a wrong address on a reachable subnet looks like, and
    the one that wastes an afternoon.
    """
    refused = SimplonError("refused", status=None)
    refused.__cause__ = OSError(ConnectionRefusedError())
    refused.__cause__.reason = ConnectionRefusedError()
    assert "Nothing is listening" in _open_failure_hint(refused)

    timed_out = SimplonError("timeout", status=None)
    timed_out.__cause__ = OSError()
    timed_out.__cause__.reason = TimeoutError()
    assert "timed out" in _open_failure_hint(timed_out)

    unresolved = SimplonError("dns", status=None)
    unresolved.__cause__ = OSError()
    unresolved.__cause__.reason = socket.gaierror()
    assert "did not resolve" in _open_failure_hint(unresolved)

    assert "read-only" in _open_failure_hint(SimplonError("busy", status=403))


def test_a_missing_control_unit_exits_distinctly(monkeypatch):
    """
    "No detector there" is not a crash, and the exit status says which.

    The same separation ``camera_server`` and ``nion_server`` make: a
    launcher can tell "wrong address" from "the adapter is broken" without
    parsing a traceback.
    """
    monkeypatch.setenv("MIAINWOODPECKER_AUTHKEY", "00" * 32)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{probe.getsockname()[1]}"
    ports = [str(5000 + index) for index in range(len(TARGET_NAMES))]
    status = main(["--backend", HARDWARE_BACKEND, "--plugin", address, *ports])
    assert status == NO_DETECTOR_EXIT_STATUS


# ---------------------------------------------------------------------------
# The monitor image is a TIFF, which is part of the protocol
# ---------------------------------------------------------------------------


def test_a_monitor_image_round_trips_through_tiff():
    """
    Counts survive the encoding, which is the only thing that matters here.

    SIMPLON's monitor endpoint returns TIFF rather than a raw array, so
    the encoding is on the path between the detector and every number
    computed downstream. A silent misread of bit depth or byte order would
    scale every count.
    """
    generator = np.random.default_rng(seed=1)
    original = generator.integers(0, 60000, size=(7, 11), dtype=np.uint16)
    decoded = decode_tiff(encode_tiff(original))
    assert decoded.dtype == np.uint16
    assert np.array_equal(decoded, original)


def test_the_builtin_decoder_refuses_rather_than_guesses():
    """
    An unreadable variant names the fix instead of returning wrong numbers.

    The narrow reader exists so the simulated backend needs nothing
    installed; it is not a claim to read every TIFF a control unit might
    send. Guessing at a compression it does not implement would reinterpret
    counts silently, which is worse than failing, so it fails and says
    which extra to install.
    """
    payload = bytearray(encode_tiff(np.ones((4, 4), dtype=np.uint16)))
    # Rewrite the Compression tag's inline value from 1 (none) to 5 (LZW).
    index = payload.find(bytes([3, 1, 3, 0]))  # tag 259, type SHORT
    assert index > 0
    payload[index + 8] = 5
    with pytest.raises(ValueError, match="dectris"):
        _decode_baseline_tiff(bytes(payload))


def test_the_two_decoders_agree_where_both_can_run():
    """
    The fallback is a real decoder, not a different one wearing the name.

    ``decode_tiff`` prefers ``tifffile`` when it is installed, so the base
    test environment and an environment with the ``dectris`` extra take
    different paths through the *same* function. If they disagreed, every
    count would change depending on what happened to be installed — which
    is precisely the class of bug that only shows up on one machine.
    """
    tifffile = pytest.importorskip(
        "tifffile",
        reason="only the built-in decoder is available here",
    )
    assert tifffile is not None
    generator = np.random.default_rng(seed=2)
    original = generator.integers(0, 5000, size=(5, 9), dtype=np.uint16)
    payload = encode_tiff(original)
    assert np.array_equal(decode_tiff(payload), _decode_baseline_tiff(payload))


def test_the_hardware_backend_asks_for_its_extra_before_a_detector():
    """
    A missing optional dependency is reported as itself, not as "no detector".

    The distinction is the whole reason the check is up front: an operator
    who installed the package without the extra would otherwise be told
    their detector was unreachable.
    """
    if importlib.util.find_spec("tifffile") is not None:
        pytest.skip("tifffile is installed, so this failure cannot occur here")
    with pytest.raises(DetectorOpenError, match="'dectris' extra"):
        open_camera(HARDWARE_BACKEND, "127.0.0.1:1")


# ---------------------------------------------------------------------------
# The rest of the adapter contract
# ---------------------------------------------------------------------------


def test_an_instrument_with_no_controls_is_a_supported_instrument(dectris_microscope):
    """
    A detector on someone else's column controls none of it, and says so.

    ``available_controls()`` returning empty is the documented answer, and
    it is the honest one here: the ELA has no stage, no defocus and no
    blanker of its own. The column's adapter, if there is one, is a
    separate server.
    """
    instrument = dectris_microscope.instrument
    assert list(instrument.available_controls()) == []
    assert instrument.check_health().state == SERVER_RESPONSIVE
    instrument.park()  # No beam to blank; must not raise.


def test_a_recording_from_a_detector_round_trips(dectris_microscope, tmp_path):
    """
    Acquire, record, read — against an adapter with no microscope behind it.

    The same end-to-end proof ``camera_server`` gets, on the other end of
    the market: everything above the device layer was written against
    protocols, and a scientific detector reached through a REST API
    exercises that claim as well as a webcam does.
    """
    path = tmp_path / "dectris.nxs"
    written = record(camera_series(dectris_microscope.camera, _FRAMES), path)
    assert written == _FRAMES

    replayed = list(read_series(path))
    assert len(replayed) == _FRAMES
    first, second = replayed[0][0], replayed[1][0]
    assert first.ndim == 2  # noqa: PLR2004 - a 2D frame, as Frame requires
    assert not np.array_equal(first, second)


def test_the_command_line_matches_the_protocol():
    """
    One positional port per target name, and ``--plugin`` as configuration.

    Here ``--plugin`` reads as the control unit's address, which is what
    the protocol's server-specific configuration channel is for: the Nion
    server reads it as plug-in module names and nothing requires that.
    """
    ports = [str(5000 + index) for index in range(len(TARGET_NAMES))]
    arguments = _parse_args(
        ["--backend", HARDWARE_BACKEND, "--plugin", "10.42.0.10", *ports],
    )
    assert arguments.backend == HARDWARE_BACKEND
    assert arguments.plugin == ["10.42.0.10"]
    assert arguments.ports == [int(port) for port in ports]

    default = _parse_args(ports)
    assert default.backend == SIMULATED_BACKEND
    assert default.plugin == []


def test_an_unknown_backend_is_rejected_before_anything_is_opened():
    """A typo names the backends it could have been, rather than failing later."""
    with pytest.raises(ValueError, match="unknown backend"):
        open_camera("nonesuch", "localhost")


class _Always404Handler(http.server.BaseHTTPRequestHandler):
    """An HTTP server that is emphatically not a SIMPLON control unit."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # http.server's own dispatch name
        """Answer every request with 404, as a foreign web service would."""
        payload = json.dumps({"message": "no"}).encode("utf-8")
        self.send_response(_NOT_FOUND)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Stay quiet; the test does not want a request log."""


class _always_404_server:  # noqa: N801 - a context manager used as one
    """Serve 404s on localhost for the duration of a ``with`` block."""

    def __enter__(self) -> str:
        """
        Start the server.

        Returns
        -------
        str
            The ``host:port`` it is listening on.
        """
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _Always404Handler,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def __exit__(self, *exception: object) -> None:
        """
        Stop the server.

        Parameters
        ----------
        *exception : object
            The exception triple, unused.
        """
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


def test_a_control_unit_is_reached_directly_rather_than_through_a_proxy(
    monkeypatch,
):
    """
    An HTTP proxy in the environment must not be used to reach a detector.

    ``urllib``'s default opener consults ``$HTTP_PROXY`` and, on macOS,
    the system proxy configuration. A DECTRIS control unit is precisely
    the host that must never be reached that way: it sits on a private
    instrument network, so a proxy either cannot route to it — the
    request hangs until the timeout and the failure gets attributed to
    the detector — or resolves the address to something else entirely.

    This is not a hypothetical. Without an explicit empty
    ``ProxyHandler``, whether the *simulated* backend works at all
    depends on whether the machine happens to list ``127.0.0.1`` in
    ``$no_proxy``, because it talks to its own mock DCU over real HTTP on
    loopback. CI is not a machine you control: this was found when macOS
    runners failed every test that spawns a device server, while Linux
    passed, because the two disagreed about proxy defaults.

    The proxy here points at port 9 (discard), so anything actually
    routed through it fails rather than quietly succeeding.
    """
    for variable in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(variable, "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")

    camera = open_camera(SIMULATED_BACKEND, "ignored")
    try:
        camera.start()
        frame = camera.acquire_frame()
    finally:
        camera.close()
    assert frame.data.ndim == _TWO_DIMENSIONS
    assert frame.data.size > 0


def test_the_mock_control_unit_binds_without_a_reverse_dns_lookup(monkeypatch):
    """
    Starting the simulated backend must not depend on name resolution.

    ``http.server.HTTPServer.server_bind`` calls ``socket.getfqdn`` on
    the bind address purely to populate ``server_name``, which nothing
    here reads. On Linux that answers instantly from ``/etc/hosts``; on a
    machine with no resolver for the loopback range it blocks, and it
    blocks inside ``__init__`` — before the mock DCU is listening, and
    long before the device server binds its own RPC ports.

    macOS CI failed every test that spawns a DECTRIS device server for
    exactly this reason: ``ConnectionRefusedError`` against the RPC
    listeners, because the server was still in the lookup when the
    client's connect deadline passed. The macOS job took 167 seconds
    against Linux's 40.

    Asserted by making the lookup fail loudly rather than by timing it,
    so this test means the same thing on every platform and in every
    network environment — a timing assertion would pass on the machines
    that were never affected.
    """

    def _refuse(host: str) -> str:
        msg = f"getfqdn({host!r}) must not be called while binding"
        raise AssertionError(msg)

    monkeypatch.setattr("socket.getfqdn", _refuse)

    unit = SimulatedControlUnit()
    try:
        assert ":" in unit.address
    finally:
        unit.shutdown()
