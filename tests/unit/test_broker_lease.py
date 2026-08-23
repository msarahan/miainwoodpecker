"""
The broker's contract, written as tests before either transport exists.

These are the spec. Every claim the protocol's docstrings make about
arbitration is asserted here against the in-process implementation, so a
remote broker joins by being added to the ``broker`` fixture rather than
by being trusted.

The properties worth protecting, in the order they matter:

* **A watcher cannot move the probe.** Reading the live view costs no
  device call and starts nothing.
* **A lease is granted whole or refused whole.** A refusal leaves the
  instrument exactly as it found it - in particular, it does not leave a
  scan stopped in exchange for a camera it did not get.
* **A paused loop always comes back**, on normal exit, on an exception,
  and on expiry. A stopped scan is a stationary probe; the tests below
  care about the restart more than about the pause.
* **The scanner is parked for the shortest interval the grant allows** -
  stopped last, restarted first.
"""

import datetime
import threading
import time
from collections.abc import Callable, Iterator, Sequence

import numpy as np
import pytest

from miainwoodpecker.acquisition import camera_series
from miainwoodpecker.broker import (
    DeviceBusyError,
    LeaseExpiredError,
    NotLiveError,
    lease_order,
)
from miainwoodpecker.broker.local import LocalBroker
from miainwoodpecker.broker.remote import RemoteLeasedDevices, connect_broker
from miainwoodpecker.broker.server import serve_broker
from miainwoodpecker.devices import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    CameraParameters,
    Frame,
    ScanParameters,
)
from miainwoodpecker.devices.rpc import SHARED_MEMORY_THRESHOLD_BYTES

_DEADLINE_S = 5.0
_JOIN_TIMEOUT_S = 0.05
_AUTHKEY = b"broker-conformance-tests"
_PARAMETERS = ScanParameters(height=4, width=4, pixel_time_us=1.0, fov_nm=10.0)


def _wait_until(
    condition: Callable[[], bool],
    deadline_s: float = _DEADLINE_S,
) -> bool:
    """
    Poll a condition until it is true or the deadline elapses.

    Parameters
    ----------
    condition : Callable[[], bool]
        Checked repeatedly.
    deadline_s : float
        Seconds to keep trying.

    Returns
    -------
    bool
        Whether the condition became true.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.002)
    return False


def _frame(label: str, index: int, shape: tuple[int, int] = (2, 2)) -> Frame:
    """
    Return a frame tagged with its source and sequence number.

    Parameters
    ----------
    label : str
        Which device produced it.
    index : int
        Its sequence number.
    shape : tuple[int, int]
        Frame size. The default is small enough to stay on the pickle
        path; a test wanting the shared-memory path asks for a bigger
        one.

    Returns
    -------
    Frame
        The frame.
    """
    return Frame(
        data=np.full(shape, float(index), dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={"source": label, "index": index},
    )


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        """
        Return the current time.

        Returns
        -------
        float
            Seconds, monotonic by construction.
        """
        return self.now

    def advance(self, seconds: float) -> None:
        """
        Move the clock forward.

        Parameters
        ----------
        seconds : float
            How far.
        """
        self.now += seconds


class _FakeCamera:
    """A camera that counts its calls and can be made to block."""

    def __init__(self, name: str = "camera") -> None:
        self.name = name
        self.shape = (2, 2)
        self.frames = 0
        self.starts = 0
        self.stops = 0
        self.block = threading.Event()
        self.entered = threading.Event()
        self._parameters = CameraParameters(exposure_ms=1.0, binning=1)
        self._lock = threading.Lock()

    @property
    def camera_id(self) -> str:
        """
        Return this camera's identifier.

        Returns
        -------
        str
            The name it was built with.
        """
        return self.name

    @property
    def binning_values(self) -> tuple[int, ...]:
        """
        Return the supported binning factors.

        Returns
        -------
        tuple[int, ...]
            Only unbinned, which is all these tests need.
        """
        return (1,)

    def parameters(self) -> CameraParameters:
        """
        Return the settings the next frame will use.

        Returns
        -------
        CameraParameters
            The current settings.
        """
        return self._parameters

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Apply new settings.

        Parameters
        ----------
        parameters : CameraParameters
            The requested settings.

        Returns
        -------
        CameraParameters
            The same settings, taken verbatim.
        """
        self._parameters = parameters
        return parameters

    def start(self) -> None:
        """Begin continuous acquisition."""
        self.starts += 1

    def stop(self) -> None:
        """Pause continuous acquisition."""
        self.stops += 1

    def acquire_frame(self) -> Frame:
        """
        Return the next frame, blocking first if the test asked for it.

        Returns
        -------
        Frame
            One frame.
        """
        if self.block.is_set():
            self.entered.set()
            while self.block.is_set():
                time.sleep(0.002)
        with self._lock:
            self.frames += 1
            index = self.frames
        return _frame(self.name, index, self.shape)

    def close(self) -> None:
        """Release the device."""


class _FakeScanner:
    """A scan unit that records its passes and can be made to block."""

    def __init__(self) -> None:
        self.passes = 0
        self.block = threading.Event()
        self.entered = threading.Event()
        self._lock = threading.Lock()

    @property
    def scanner_id(self) -> str:
        """
        Return this scanner's identifier.

        Returns
        -------
        str
            A fixed name.
        """
        return "scanner"

    @property
    def channel_names(self) -> tuple[str, ...]:
        """
        Return the detectors this scan unit reads out.

        Returns
        -------
        tuple[str, ...]
            Two, so the multichannel path can be exercised.
        """
        return ("HAADF", "MAADF")

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """
        Scan one pass and return one channel of it.

        Parameters
        ----------
        parameters : ScanParameters
            Ignored beyond being required.
        channel : int
            Which detector.

        Returns
        -------
        Frame
            The pass's frame.
        """
        return self.scan_frames(parameters, [channel])[0]

    def scan_frames(
        self,
        parameters: ScanParameters,
        channels: Sequence[int],
    ) -> list[Frame]:
        """
        Scan one pass and return every requested channel of it.

        Parameters
        ----------
        parameters : ScanParameters
            Ignored beyond being required.
        channels : Sequence[int]
            Which detectors.

        Returns
        -------
        list[Frame]
            One frame per channel, all from the one pass.
        """
        del parameters
        if self.block.is_set():
            self.entered.set()
            while self.block.is_set():
                time.sleep(0.002)
        with self._lock:
            self.passes += 1
            number = self.passes
        return [_frame(f"scan{channel}", number) for channel in channels]

    def close(self) -> None:
        """Release the device."""


class _FakeInstrument:
    """Instrument controls that count how often they are read."""

    def __init__(self) -> None:
        self.reads = 0
        self.parked = 0
        self.defocus = 0.0
        self.blanked = False

    def stage_size_nm(self) -> float:
        """
        Return the usable stage extent.

        Returns
        -------
        float
            A nominal extent.
        """
        return 1000.0

    def available_controls(self) -> tuple[str, ...]:
        """
        Return the controls this instrument implements.

        Returns
        -------
        tuple[str, ...]
            Defocus and the beam blanker.
        """
        return (DEFOCUS_CONTROL, BEAM_BLANKER_CONTROL)

    def park(self) -> None:
        """Put the instrument in a safe unattended state."""
        self.parked += 1
        self.blanked = True

    def defocus_nm(self) -> float:
        """
        Return the current defocus.

        Returns
        -------
        float
            Nanometres.
        """
        self.reads += 1
        return self.defocus

    def set_defocus_nm(self, defocus_nm: float) -> None:
        """
        Set the defocus.

        Parameters
        ----------
        defocus_nm : float
            Nanometres.
        """
        self.defocus = defocus_nm

    def is_beam_blanked(self) -> bool:
        """
        Return whether the beam is blanked.

        Returns
        -------
        bool
            The blanker state.
        """
        return self.blanked

    def set_beam_blanked(self, *, blanked: bool) -> None:
        """
        Blank or unblank the beam.

        Parameters
        ----------
        blanked : bool
            The state wanted.
        """
        self.blanked = blanked


@pytest.fixture
def clock() -> _Clock:
    """
    Return a hand-advanced monotonic clock.

    Returns
    -------
    _Clock
        The clock, at an arbitrary start.
    """
    return _Clock()


@pytest.fixture
def devices() -> dict:
    """
    Return one instrument's worth of fake device handles.

    Returns
    -------
    dict
        Keyed by target name, as a device server would serve them.
    """
    return {
        "instrument": _FakeInstrument(),
        "scanner": _FakeScanner(),
        "eels_camera": _FakeCamera("eels_camera"),
        "ronchigram_camera": _FakeCamera("ronchigram_camera"),
    }


@pytest.fixture(params=["local", "remote"])
def broker(
    request: pytest.FixtureRequest,
    devices: dict,
    clock: _Clock,
) -> Iterator[object]:
    """
    Return a broker over the fake instrument, in process and over a socket.

    Every test taking this fixture runs twice: once against
    :class:`LocalBroker` directly, and once against a
    :class:`RemoteBroker` talking to that same broker through a real
    socket. The arbitration is the same object either way - the point of
    the second pass is the *wire*, so it is a genuine connection rather
    than a stub, with the server on a thread of this process so the fake
    devices stay inspectable from the test.

    Parameters
    ----------
    request : pytest.FixtureRequest
        Carries the parameter naming which side to exercise.
    devices : dict
        The device handles.
    clock : _Clock
        The injected clock, shared by both sides because the broker
        being served is the one holding it.

    Yields
    ------
    object
        The broker under test, satisfying ``InstrumentBroker``.
    """
    made = LocalBroker(devices, holder="test-client", clock=clock)
    if request.param == "local":
        try:
            yield made
        finally:
            made.close()
        return
    server = serve_broker(made, authkey=_AUTHKEY)
    client = connect_broker(("localhost", server.port), authkey=_AUTHKEY)
    try:
        yield client
    finally:
        client.close()
        server.close()
        made.close()


# -- ordering -------------------------------------------------------------


def test_lease_order_puts_the_scanner_last_and_dedupes():
    """
    The scanner is acquired last, so the probe parks for the least time.

    Any total order would prevent the deadlock; this one is chosen so
    that a scanner-plus-camera lease stops scanning only once every
    other target is already in hand.
    """
    ordered = lease_order(
        ["scanner", "eels_camera", "instrument", "spectrum_detector", "scanner"],
    )
    assert ordered == (
        "instrument",
        "spectrum_detector",
        "eels_camera",
        "scanner",
    )


def test_lease_order_accepts_a_target_kind_it_has_never_heard_of():
    """
    An out-of-tree target is ordered, not refused.

    A broker that declined to arbitrate an unfamiliar name would hand
    the problem straight back to the race it exists to prevent.
    """
    assert lease_order(["scanner", "wobbulator"]) == ("wobbulator", "scanner")


# -- watching -------------------------------------------------------------


def test_watching_never_starts_a_loop(broker, devices):
    """
    Reading the live view of an idle target starts nothing.

    A caller asking what is on screen must not be able to move the probe
    by asking, so latest() answers None rather than acquiring.
    """
    assert broker.latest("eels_camera") is None
    assert broker.latest_frames("scanner") == ()
    assert devices["eels_camera"].frames == 0
    assert devices["scanner"].passes == 0


def test_watching_an_unknown_target_is_a_key_error(broker):
    """A name this instrument does not serve is refused, not answered."""
    with pytest.raises(KeyError):
        broker.latest("no_such_camera")


def test_stats_refuses_rather_than_reporting_a_stalled_looking_zero(broker):
    """
    Stats on an idle target raise, because zeros read as a stalled loop.

    A zeroed LiveStats is exactly what a loop that has wedged looks
    like, and "not running" must not be confusable with it.
    """
    with pytest.raises(NotLiveError):
        broker.stats("eels_camera")


def test_many_watchers_cost_the_device_nothing_extra(broker, devices):
    """
    Polling the latest frame makes no device call at all.

    Asserted while the camera is wedged inside one exposure, which
    freezes the only thing that legitimately drives it - the loop - so
    the count afterwards is exactly the count before. Comparing against
    a free-running loop instead would be timing, not arithmetic: it
    passed in process and failed over a socket purely because the wire
    is slower than the fake camera, which says nothing about whether
    watching costs anything.
    """
    camera = devices["eels_camera"]
    broker.start_live("eels_camera")
    assert _wait_until(lambda: broker.latest("eels_camera") is not None)

    camera.block.set()
    assert _wait_until(camera.entered.is_set)
    try:
        before = camera.frames
        for _ in range(100):
            assert broker.latest("eels_camera") is not None
        assert camera.frames == before
    finally:
        camera.block.clear()


def test_multichannel_latest_frames_returns_the_whole_pass(broker):
    """
    A two-channel scan loop publishes both channels of one pass.

    The frames share a pass by construction, which is what makes a
    per-pixel difference between them legitimate.
    """
    broker.start_live("scanner", _PARAMETERS, channels=(0, 1))
    assert _wait_until(lambda: len(broker.latest_frames("scanner")) == 2)  # noqa: PLR2004
    frames = broker.latest_frames("scanner")
    passes = {frame.metadata["index"] for frame in frames}
    assert passes == {frames[0].metadata["index"]}


# -- leasing: the happy path ----------------------------------------------


def test_a_lease_pauses_the_live_loop_and_restarts_it_on_release(broker):
    """
    The loop is stopped for the lease and running again after it.

    Restarting is unconditional. A stopped scan is not an idle state -
    the beam is on regardless, so a scan that is not scanning is a
    stationary probe concentrating dose on one spot.
    """
    broker.start_live("scanner", _PARAMETERS)
    assert _wait_until(lambda: broker.targets()["scanner"].is_live)

    with broker.lease("scanner", reason="one series") as leased:
        assert not broker.targets()["scanner"].is_live
        assert leased.scanner().scan_frame(_PARAMETERS) is not None

    assert _wait_until(lambda: broker.targets()["scanner"].is_live)


def test_a_loop_that_was_not_running_is_not_started_by_a_lease(broker):
    """
    Release restarts what the lease paused, not everything it held.

    The set is recorded at grant time, because by release time the loops
    are stopped and the answer is unknowable.
    """
    with broker.lease("scanner", reason="cold start"):
        pass
    assert not broker.targets()["scanner"].is_live


def test_the_loop_comes_back_even_when_the_block_raises(broker):
    """
    An exception inside the lease still releases and restarts.

    A script that fails mid-series must not leave the probe parked.
    """
    broker.start_live("scanner", _PARAMETERS)
    assert _wait_until(lambda: broker.targets()["scanner"].is_live)

    message = "deliberate"
    with pytest.raises(RuntimeError, match=message), broker.lease("scanner"):
        raise RuntimeError(message)

    assert _wait_until(lambda: broker.targets()["scanner"].is_live)


def test_the_scanner_is_stopped_last_and_restarted_first(broker, devices):
    """
    The probe stands parked only for the grant, not the negotiation.

    Stopping the scanner first would leave it parked while every other
    target in the lease is still being joined.
    """
    broker.start_live("scanner", _PARAMETERS)
    broker.start_live("eels_camera")
    assert _wait_until(lambda: broker.targets()["scanner"].is_live)
    assert _wait_until(lambda: broker.targets()["eels_camera"].is_live)

    with broker.lease("scanner", "eels_camera", reason="spectrum image") as leased:
        assert leased.lease.targets == ("eels_camera", "scanner")
        assert leased.lease.restarts == ("eels_camera", "scanner")

    # Released in reverse: the scan is the first thing back.
    assert _wait_until(lambda: broker.targets()["scanner"].is_live)
    assert devices["scanner"].passes > 0


def test_acquisition_generators_work_through_a_lease_unchanged(broker):
    """
    A lease yields the device-layer protocols, not broker substitutes.

    This is the property the whole design exists for: every generator in
    miainwoodpecker.acquisition, every Session recording and every
    analysis bridge works inside a lease without knowing one exists.
    """
    with broker.lease("eels_camera", reason="burst") as leased:
        frames = list(camera_series(leased.camera(), 3))
    expected = 3
    assert len(frames) == expected
    assert [frame.metadata["source"] for frame in frames] == ["eels_camera"] * expected


def test_a_lease_will_not_guess_between_two_cameras(broker):
    """
    Naming no camera when the lease holds two is refused.

    Guessing between a Ronchigram and an EELS camera is how a recording
    ends up labelled as the wrong detector.
    """
    with broker.lease("eels_camera", "ronchigram_camera") as leased:
        with pytest.raises(KeyError):
            leased.camera()
        assert leased.camera("eels_camera").camera_id == "eels_camera"


# -- leasing: refusal -----------------------------------------------------


def test_contention_is_refused_and_says_who_holds_it(devices, clock):
    """
    A second lease is refused, not queued, and names the holder.

    A queue invites two clients to each believe they are next, and a
    lease has no bounded duration for a queue to reason about. The
    honest answer is who holds it and why.
    """
    first = LocalBroker(devices, holder="operator", clock=clock)
    second = LocalBroker(devices, holder="notebook", clock=clock)
    # One instrument, two client views of it: the claim table is what
    # differs in a real broker, so share it here rather than pretend.
    second._claims = first._claims  # noqa: SLF001
    second._leases = first._leases  # noqa: SLF001

    with first.lease("eels_camera", reason="energy series"):  # noqa: SIM117 - the nesting is the scenario
        with pytest.raises(DeviceBusyError) as raised, second.lease("eels_camera"):
            pass
    assert raised.value.holder == "operator"
    assert raised.value.reason == "energy series"
    assert "energy series" in str(raised.value)


def test_a_refused_lease_leaves_the_instrument_exactly_as_it_found_it(broker, devices):
    """
    Whole or nothing: a camera that will not join does not stop the scan.

    Leaving the scan dark in exchange for a camera the lease did not get
    would park the probe for nothing, so every loop already stopped for
    the attempt is restarted before the refusal is raised.
    """
    broker.start_live("scanner", _PARAMETERS)
    broker.start_live("eels_camera")
    assert _wait_until(lambda: broker.targets()["eels_camera"].is_live)
    assert _wait_until(lambda: broker.targets()["scanner"].is_live)

    camera = devices["eels_camera"]
    camera.block.set()
    assert _wait_until(camera.entered.is_set)
    try:
        with pytest.raises(DeviceBusyError) as raised, broker.lease(
            "eels_camera",
            "scanner",
            timeout_s=_JOIN_TIMEOUT_S,
        ):
            pass
        assert raised.value.target == "eels_camera"
        assert raised.value.holder is None
        assert "try again" in str(raised.value)
        assert broker.targets()["scanner"].is_live
    finally:
        camera.block.clear()

    assert broker.targets()["eels_camera"].lease is None
    assert broker.targets()["scanner"].lease is None


def test_a_refusal_restarts_the_loops_it_had_already_stopped(broker, devices):
    """
    The undo path: a late failure gives back what the early ones took.

    The camera is stopped first and the scanner last, so a scanner that
    will not join is the case where the grant has already paused
    something. Without the undo the camera would stay dark on a lease
    nobody ever got - the failure the previous test cannot see, because
    there the first target is the one that fails and nothing has been
    stopped yet.
    """
    broker.start_live("eels_camera")
    broker.start_live("scanner", _PARAMETERS)
    assert _wait_until(lambda: broker.targets()["eels_camera"].is_live)
    assert _wait_until(lambda: broker.targets()["scanner"].is_live)

    scanner = devices["scanner"]
    scanner.block.set()
    assert _wait_until(scanner.entered.is_set)
    try:
        with pytest.raises(DeviceBusyError) as raised, broker.lease(
            "eels_camera",
            "scanner",
            timeout_s=_JOIN_TIMEOUT_S,
        ):
            pass
        assert raised.value.target == "scanner"
        assert broker.targets()["eels_camera"].is_live
    finally:
        scanner.block.clear()

    assert broker.targets()["eels_camera"].lease is None
    assert broker.targets()["scanner"].lease is None


def test_starting_or_stopping_a_display_is_refused_while_leased(broker):
    """
    A lease is exclusive against the display, not only against acquiring.

    Starting a live loop on a leased target would put a second driver on
    the device, which is the interleaving the broker exists to prevent.
    """
    with broker.lease("scanner", reason="series"):
        with pytest.raises(DeviceBusyError):
            broker.start_live("scanner", _PARAMETERS)
        with pytest.raises(DeviceBusyError):
            broker.stop_live("scanner")


# -- leasing: expiry ------------------------------------------------------


def test_an_expired_lease_is_reclaimed_and_its_loops_restarted(broker, clock):
    """
    A kernel that dies mid-lease does not hold the beam forever.

    On expiry the broker releases exactly as if the block had exited -
    restarting the loops it paused - because the alternative is a probe
    parked until somebody notices.
    """
    broker.start_live("scanner", _PARAMETERS)
    assert _wait_until(lambda: broker.targets()["scanner"].is_live)

    leased = broker.grant(("scanner",), reason="abandoned", timeout_s=1.0, ttl_s=30.0)
    assert not broker.targets()["scanner"].is_live

    clock.advance(31.0)
    assert _wait_until(lambda: broker.targets()["scanner"].is_live)
    assert broker.targets()["scanner"].lease is None
    with pytest.raises(LeaseExpiredError):
        broker.check_lease(leased.lease_id)


def test_a_call_through_an_expired_lease_is_refused_at_the_call_site(broker, clock):
    """
    The refusal lands where the probe would have moved.

    Handing out bare device handles would let a client whose lease
    expired keep driving a device the broker has given to somebody else,
    and the call would succeed silently.
    """
    with broker.lease("scanner", ttl_s=10.0) as leased:
        scanner = leased.scanner()
        assert scanner.scan_frame(_PARAMETERS) is not None
        clock.advance(11.0)
        with pytest.raises(LeaseExpiredError):
            scanner.scan_frame(_PARAMETERS)


def test_renewing_extends_the_deadline(broker, clock):
    """A lease renewed before expiry keeps working past its first deadline."""
    with broker.lease("scanner", ttl_s=10.0) as leased:
        clock.advance(9.0)
        renewed = leased.renew(ttl_s=10.0)
        assert renewed.expires_at == clock.now + 10.0
        clock.advance(5.0)
        assert leased.scanner().scan_frame(_PARAMETERS) is not None


def test_renewal_is_not_revival(broker, clock):
    """
    Renewing an already-reclaimed lease is refused rather than granted.

    The loops have restarted and the targets may be held by somebody
    else, so handing the deadline back would be handing back a device.
    """
    with broker.lease("scanner", ttl_s=10.0) as leased:
        clock.advance(11.0)
        with pytest.raises(LeaseExpiredError):
            leased.renew()


# -- instrument controls --------------------------------------------------


def test_controls_are_served_from_cache_while_the_instrument_is_leased(
    broker,
    devices,
):
    """
    A dashboard reading the defocus does not contend with a lease holder.

    The device protocol is one request at a time, so reading the
    instrument out from under a holder mid-sweep is the interleaving the
    broker exists to prevent. The last values stand instead.
    """
    devices["instrument"].defocus = 12.5
    assert broker.controls()[DEFOCUS_CONTROL] == 12.5  # noqa: PLR2004
    reads_before = devices["instrument"].reads

    with broker.lease("instrument", reason="focal series") as leased:
        leased.instrument.set_defocus_nm(40.0)
        assert broker.controls()[DEFOCUS_CONTROL] == 12.5  # noqa: PLR2004
        assert devices["instrument"].reads == reads_before

    assert broker.controls()[DEFOCUS_CONTROL] == 40.0  # noqa: PLR2004


def test_reaching_for_the_instrument_without_leasing_it_is_refused(broker):
    """
    A sweep that moves a control must have said so in its lease.

    focal_series moves the defocus and scans, so asking for the scanner
    alone and reaching for the instrument through it is a second driver
    arriving by the back door.
    """
    with broker.lease("scanner", reason="scan only") as leased, pytest.raises(KeyError):
        _ = leased.instrument


# -- over the wire, with more than one client -----------------------------


@pytest.fixture
def served(devices: dict, clock: _Clock) -> Iterator[tuple]:
    """
    Return a broker server and the address clients reach it on.

    Parameters
    ----------
    devices : dict
        The device handles.
    clock : _Clock
        The injected clock.

    Yields
    ------
    tuple
        The underlying LocalBroker, and its ``(host, port)``.
    """
    made = LocalBroker(devices, holder="owner", clock=clock)
    server = serve_broker(made, authkey=_AUTHKEY)
    try:
        yield made, ("localhost", server.port)
    finally:
        server.close()
        made.close()


def test_two_clients_contend_and_the_loser_is_told_who_holds_it(served):
    """
    The real contention case: two connections, one instrument.

    The holder is the *connection's* identity, assigned by the server -
    neither client chose it, and neither could have lied about it. The
    losing client learns who has it from the target's own state, which
    crosses as data; the exception carries the message.
    """
    _, address = served
    first = connect_broker(address, authkey=_AUTHKEY)
    second = connect_broker(address, authkey=_AUTHKEY)
    try:
        with first.lease("eels_camera", reason="energy series") as leased:
            holder = leased.lease.holder
            with pytest.raises(DeviceBusyError) as raised:
                second.grant(("eels_camera",))
            assert holder in str(raised.value)
            assert second.targets()["eels_camera"].lease.holder == holder
            assert second.targets()["eels_camera"].lease.reason == "energy series"
        # Released: the loser can have it now.
        with second.lease("eels_camera") as won:
            assert won.lease.holder != holder
    finally:
        first.close()
        second.close()


def test_a_dropped_connection_releases_its_leases_at_once(served):
    """
    A client that vanishes does not hold the beam until its lease expires.

    Waiting out the time to live would leave the probe parked for
    minutes over a socket that is demonstrably closed, so the server
    releases when it sees the disconnect - the same restart expiry would
    eventually do, at the moment the loss is actually observable.
    """
    made, address = served
    made.start_live("scanner", _PARAMETERS)
    assert _wait_until(lambda: made.targets()["scanner"].is_live)

    abandoning = connect_broker(address, authkey=_AUTHKEY)
    abandoning.grant(("scanner",), reason="never released")
    assert not made.targets()["scanner"].is_live

    abandoning.close()
    assert _wait_until(lambda: made.targets()["scanner"].is_live)
    assert made.targets()["scanner"].lease is None


def test_one_connection_serialises_two_leased_devices(served):
    """
    Two devices driven at once do not interleave on the one socket.

    A lease can hold a scanner and a camera, and a client is entitled to
    drive them from different threads - an acquisition on one, a status
    poll on the other. They share a connection, so they must share its
    lock: two private locks would let one device's send land between
    another's send and recv, and the replies would come back swapped or
    the pickle stream would desynchronise outright.

    Each device keeps its *own* frame lock, which guards its segment
    rather than the socket, so this does not serialise the copy-outs.

    Note what the bug looks like: a reply consumed by the wrong thread
    leaves the right one blocked in ``recv`` forever, so the symptom is
    a hang, not an exception. Everything about this test's shape follows
    from that - a test that reproduces a deadlock has to *fail*, not
    join it. The threads are daemons, the join has a deadline, the lease
    is taken by hand rather than with a ``with`` block, and release is
    skipped when a thread is stuck: releasing would take the same lock
    the stuck thread is holding, and the test would hang in its own
    teardown having already proved the point. Closing the connection is
    what frees the stuck thread, and the server releases the lease on
    seeing the disconnect anyway.
    """
    _, address = served
    client = connect_broker(address, authkey=_AUTHKEY)
    rounds = 30
    results: dict[str, list] = {"camera": [], "scanner": []}
    failures: list[Exception] = []

    def drive(name: str, grab: Callable[[], object]) -> None:
        """
        Call one device repeatedly, recording what came back.

        Parameters
        ----------
        name : str
            Which result list to append to.
        grab : Callable[[], object]
            The device call to repeat.
        """
        try:
            for _ in range(rounds):
                results[name].append(grab())
        except Exception as error:  # noqa: BLE001 - re-raised on the main thread
            failures.append(error)

    lease = client.grant(("eels_camera", "scanner"), reason="two at once")
    leased = RemoteLeasedDevices(client, lease)
    detector = leased.camera()
    scanner = leased.scanner()
    stuck: list[str] = []
    try:
        detector.start()
        threads = [
            threading.Thread(
                target=drive,
                args=("camera", detector.acquire_frame),
                daemon=True,
            ),
            threading.Thread(
                target=drive,
                args=("scanner", lambda: scanner.scan_frame(_PARAMETERS)),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_DEADLINE_S)
        stuck = [thread.name for thread in threads if thread.is_alive()]
        if not stuck:
            detector.stop()
            client.release(lease.lease_id)
    finally:
        client.close()

    assert not stuck, f"blocked waiting for a reply: {stuck}"
    assert not failures, failures[0]
    assert len(results["camera"]) == rounds
    assert len(results["scanner"]) == rounds
    # Every reply went to the caller that asked for it, not the other one.
    assert {frame.metadata["source"] for frame in results["camera"]} == {"eels_camera"}
    assert {frame.metadata["source"] for frame in results["scanner"]} == {"scan0"}


def test_a_large_leased_frame_crosses_through_shared_memory(served):
    """
    A frame past the threshold takes the segment, not the pickle channel.

    This is the reason leasing and watching differ. A leased target has
    one caller, so the reused segment is safe, and the frame arrives with
    its values intact rather than merely with its shape.
    """
    made, address = served
    camera = made.device("ronchigram_camera")
    camera.shape = (256, 256)  # 256KB float32, over the 64KB threshold
    client = connect_broker(address, authkey=_AUTHKEY)
    try:
        with client.lease("ronchigram_camera", reason="one big frame") as leased:
            detector = leased.camera()
            detector.start()
            frame = detector.acquire_frame()
            detector.stop()
    finally:
        client.close()
    assert frame.data.shape == (256, 256)
    assert frame.data.nbytes > SHARED_MEMORY_THRESHOLD_BYTES
    assert float(frame.data.max()) == float(frame.metadata["index"])


def test_close_parks_the_instrument(devices, clock):
    """
    Teardown leaves the instrument in a state somebody chose.

    Parking blanks the beam where one exists, which is what makes
    stopping the loops safe here when it would not be anywhere else.

    Local only, deliberately: a *client* closing its connection must not
    park the instrument for everybody else on it. Closing the broker is
    the owner's act, and a remote client is not the owner.
    """
    made = LocalBroker(devices, holder="test-client", clock=clock)
    made.start_live("scanner", _PARAMETERS)
    assert _wait_until(lambda: made.targets()["scanner"].is_live)
    made.close()
    assert devices["instrument"].parked >= 1
    assert not made.targets()["scanner"].is_live
