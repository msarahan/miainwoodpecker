"""
Acquiring from a dashboard: off the display thread, and on the record.

Three properties, and the first is the one the whole design turns on.

* **The lease is taken on a worker, never on the caller's thread.**
  Taking one means waiting out the pass already in flight, which is
  ``height x width x dwell`` - up to minutes on a big slow scan. A
  notebook that took it inline would stop refreshing its tiles for that
  long. The test below blocks the lease outright and asserts the caller
  came back anyway.
* **Every attempt reaches the log, refusals included.** A lease the
  broker refused is part of what happened during the shift.
* **No new acquisition verbs.** What runs inside the lease is one of
  :mod:`miainwoodpecker.acquisition`'s own generators, so the fakes here
  are ordinary device protocols rather than dashboard-shaped ones.

The broker is a real :class:`~miainwoodpecker.broker.local.LocalBroker`
over fake devices rather than a stub of the protocol, because the thing
under test is behaviour at the boundary - a lease being granted, a live
loop being stopped and restarted around it - and a stub would assert
that the dashboard calls the methods it calls.
"""

import datetime
import threading
import time
from collections.abc import Iterator, Sequence

import numpy as np
import pytest

from miainwoodpecker.broker.interface import DeviceBusyError
from miainwoodpecker.broker.local import LocalBroker
from miainwoodpecker.dashboard.acquisition import (
    AcquisitionJob,
    camera_request,
    scan_request,
)
from miainwoodpecker.dashboard.session_log import (
    SessionLog,
    SessionLogEntry,
    describe_frames,
    highlights,
)
from miainwoodpecker.devices.interface import CameraParameters, Frame, ScanParameters
from miainwoodpecker.storage.session import Session

_PARAMETERS = ScanParameters(height=4, width=4, pixel_time_us=1.0, fov_nm=10.0)
_DEADLINE_S = 5.0
# Two passes of two detectors each, which is the number the multichannel
# path exists to produce: one traversal of the probe, every enabled
# detector read out of it.
_TWO_PASSES_TWO_DETECTORS = 4
_CAMERA_FRAMES = 3
_LOGGED_ENTRIES = 3


def _frame(label: str, index: int) -> Frame:
    """
    Return a small frame tagged with its source and sequence number.

    Parameters
    ----------
    label : str
        Which device produced it.
    index : int
        Its sequence number.

    Returns
    -------
    Frame
        The frame.
    """
    return Frame(
        data=np.full((4, 4), float(index), dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={"device_id": label, "frame_index": index, "exposure_ms": 5.0},
    )


class _FakeCamera:
    """A camera that counts frames and can be held mid-exposure."""

    def __init__(self) -> None:
        self.frames = 0
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
            A fixed name.
        """
        return "ronchigram"

    @property
    def binning_values(self) -> tuple[int, ...]:
        """
        Return the supported binning factors.

        Returns
        -------
        tuple[int, ...]
            Two, so a binning menu has something to choose between.
        """
        return (1, 2)

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
            The settings, taken verbatim.
        """
        self._parameters = parameters
        return parameters

    def start(self) -> None:
        """Begin continuous acquisition."""

    def stop(self) -> None:
        """Pause continuous acquisition."""

    def acquire_frame(self) -> Frame:
        """
        Return the next frame, waiting first if the test asked it to.

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
        return _frame("ronchigram", index)

    def close(self) -> None:
        """Release the device."""


class _FakeScanner:
    """A scan unit with two detectors that records the geometry it ran."""

    def __init__(self) -> None:
        self.passes = 0
        self.requested: list[tuple[ScanParameters, tuple[int, ...]]] = []
        self._lock = threading.Lock()

    @property
    def scanner_id(self) -> str:
        """
        Return this scan unit's identifier.

        Returns
        -------
        str
            A fixed name.
        """
        return "scan-unit"

    @property
    def channel_names(self) -> tuple[str, ...]:
        """
        Return the detectors this scan unit reads out.

        Returns
        -------
        tuple[str, ...]
            Two, so the multichannel path is exercised.
        """
        return ("HAADF", "MAADF")

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """
        Scan one pass and return one channel of it.

        Parameters
        ----------
        parameters : ScanParameters
            The geometry to run.
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
            The geometry to run.
        channels : Sequence[int]
            Which detectors.

        Returns
        -------
        list[Frame]
            One frame per requested channel, all from the one pass.
        """
        with self._lock:
            self.passes += 1
            number = self.passes
            self.requested.append((parameters, tuple(channels)))
        return [
            Frame(
                data=np.full(parameters.shape, float(number), dtype=np.float32),
                timestamp=datetime.datetime.now(tz=datetime.UTC),
                metadata={
                    "device_id": "scan-unit",
                    "channel_index": channel,
                    "channel_name": self.channel_names[channel],
                    "scan_pass_id": f"pass-{number}",
                },
            )
            for channel in channels
        ]

    def close(self) -> None:
        """Release the device."""


@pytest.fixture
def devices() -> tuple[_FakeScanner, _FakeCamera]:
    """
    Return the scan unit and camera the broker is built over.

    Returns
    -------
    tuple[_FakeScanner, _FakeCamera]
        The two fakes, so a test can inspect what the acquisition asked
        of them.
    """
    return (_FakeScanner(), _FakeCamera())


@pytest.fixture
def broker(devices: tuple[_FakeScanner, _FakeCamera]) -> Iterator[LocalBroker]:
    """
    Return a broker over the fake devices, closed when the test ends.

    Closed, and this is not boilerplate. Several tests here start a live
    loop deliberately - it is what an acquisition has to pause and
    restart - and a loop left running does not stop when the test does:
    it keeps grabbing from a fake device flat out for the rest of the
    session. Measured here rather than reasoned about: without this
    teardown the whole unit suite slowed to a crawl and one pytest
    process reached 14 GB, because the leaked loop kept appending to
    ``_FakeScanner.requested``. It reads as "the suite got slow" rather
    than as a failure, which is exactly why it is worth a fixture.

    Parameters
    ----------
    devices : tuple[_FakeScanner, _FakeCamera]
        The devices to serve.

    Yields
    ------
    LocalBroker
        The broker, holding as "dashboard-tests".
    """
    scanner, camera = devices
    built = LocalBroker(
        {"scanner": scanner, "ronchigram_camera": camera},
        holder="dashboard-tests",
    )
    try:
        yield built
    finally:
        built.close()


def _finished(job: AcquisitionJob) -> bool:
    """
    Wait for a job to finish, within the suite's deadline.

    Parameters
    ----------
    job : AcquisitionJob
        The job to wait for.

    Returns
    -------
    bool
        Whether it finished.
    """
    return job.join(_DEADLINE_S)


def test_the_lease_is_taken_on_the_worker_not_the_caller(broker, devices):
    """
    Starting an acquisition returns immediately even while the lease blocks.

    The property the whole module exists for. The camera is held
    mid-exposure so that stopping its live loop - which is what taking a
    lease does first - cannot complete; if the lease were taken on this
    thread, ``start`` would not return until the block was lifted.
    """
    _, camera = devices
    broker.start_live("ronchigram_camera")
    camera.block.set()
    assert camera.entered.wait(_DEADLINE_S)
    job = AcquisitionJob(
        broker,
        camera_request("ronchigram_camera", count=1, parameters=None),
        SessionLog(),
    )
    started = time.monotonic()
    job.start()
    # The assertion is that this line is reached at all: the lease is
    # still waiting for the exposure the block is holding.
    assert time.monotonic() - started < 1.0
    assert job.is_running
    camera.block.clear()
    assert _finished(job)


def test_a_successful_acquisition_lands_in_the_log_with_a_thumbnail(broker):
    """Each acquisition adds one entry carrying a picture and its provenance."""
    log = SessionLog()
    job = AcquisitionJob(
        broker,
        scan_request(
            "scanner",
            parameters=_PARAMETERS,
            channels=(0, 1),
            channel_names=("HAADF", "MAADF"),
            count=2,
        ),
        log,
    )
    job.start()
    assert _finished(job)
    assert job.error is None
    (entry,) = log.entries
    assert entry.index == 1
    assert entry.label == "scan-HAADF-MAADF"
    assert entry.frame_count == _TWO_PASSES_TWO_DETECTORS
    assert entry.shape == _PARAMETERS.shape
    assert entry.thumbnail.startswith("data:image/png;base64,")
    assert entry.recording_path is None
    assert highlights(entry)["channel_name"] == "HAADF"
    # Not a credential, whatever the name looks like: the pass identity
    # is what makes per-pixel arithmetic between two channels legitimate.
    assert highlights(entry)["scan_pass_id"] == "pass-1"  # noqa: S105


def test_the_holder_the_broker_assigned_is_recorded(broker):
    """A client cannot know its own name until the broker has told it one."""
    log = SessionLog()
    job = AcquisitionJob(broker, camera_request("ronchigram_camera", count=1), log)
    job.start()
    assert _finished(job)
    assert job.holder == "dashboard-tests"
    assert log.entries[0].holder == "dashboard-tests"


def test_a_refusal_is_logged_as_well_as_raised(broker):
    """A lease somebody else holds is part of what happened, not a silent gap."""
    log = SessionLog()
    with broker.lease("scanner", reason="somebody else's series"):
        job = AcquisitionJob(
            broker,
            scan_request(
                "scanner",
                parameters=_PARAMETERS,
                channels=(0,),
                channel_names=("HAADF",),
            ),
            log,
            # The broker refuses contention rather than queueing it, so
            # this does not wait for the other lease to end.
        )
        job.start()
        assert _finished(job)
    assert isinstance(job.error, DeviceBusyError)
    (entry,) = log.entries
    assert not entry.succeeded
    assert "DeviceBusyError" in (entry.error or "")
    assert entry.frame_count == 0
    assert entry.thumbnail == ""


def test_acquiring_into_a_session_writes_the_frames_and_names_the_file(
    broker,
    tmp_path,
):
    """With a session attached the entry points at a file, not at memory only."""
    log = SessionLog()
    session = Session(tmp_path / "shift", operator="M. Sarahan")
    job = AcquisitionJob(
        broker,
        camera_request("ronchigram_camera", count=3),
        log,
        session=session,
        note="hole 4",
    )
    job.start()
    assert _finished(job)
    assert job.error is None
    (entry,) = log.entries
    assert entry.recording_path is not None
    assert entry.frame_count == _CAMERA_FRAMES
    recorded = session.recordings()
    assert [record.frame_count for record in recorded] == [_CAMERA_FRAMES]
    assert str(recorded[0].path) == entry.recording_path


def test_a_paused_live_loop_comes_back_after_an_acquisition(broker, devices):
    """The broker restarts what it stopped, and the dashboard changes nothing."""
    broker.start_live("scanner", _PARAMETERS)
    job = AcquisitionJob(
        broker,
        scan_request(
            "scanner",
            parameters=_PARAMETERS,
            channels=(0,),
            channel_names=("HAADF",),
        ),
        SessionLog(),
    )
    job.start()
    assert _finished(job)
    assert broker.targets()["scanner"].is_live
    del devices


def test_a_scan_request_asks_for_every_enabled_detector_in_one_pass(broker, devices):
    """Two detectors out of one pass, not two passes - dose and drift both."""
    scanner, _ = devices
    job = AcquisitionJob(
        broker,
        scan_request(
            "scanner",
            parameters=_PARAMETERS,
            channels=(0, 1),
            channel_names=("HAADF", "MAADF"),
            count=1,
        ),
        SessionLog(),
    )
    job.start()
    assert _finished(job)
    assert scanner.passes == 1
    assert scanner.requested == [(_PARAMETERS, (0, 1))]


def test_a_single_camera_image_uses_its_own_settings_and_restores_the_live_ones(
    broker,
    devices,
):
    """
    An acquired image is worth a longer exposure; the feed is not.

    ``camera_image`` puts the live settings back on the way out, which
    is the reason a single image goes through it rather than through a
    one-frame series.
    """
    _, camera = devices
    live = camera.parameters()
    job = AcquisitionJob(
        broker,
        camera_request(
            "ronchigram_camera",
            parameters=CameraParameters(exposure_ms=250.0, binning=2),
            count=1,
        ),
        SessionLog(),
    )
    job.start()
    assert _finished(job)
    assert camera.parameters() == live


def test_settings_for_a_multi_frame_camera_acquisition_are_refused():
    """Ignoring them quietly would leave the operator's exposure unapplied."""
    with pytest.raises(ValueError, match="camera_series does not apply"):
        camera_request(
            "ronchigram_camera",
            parameters=CameraParameters(exposure_ms=10.0),
            count=5,
        )


def test_the_log_numbers_entries_itself_and_hands_out_copies():
    """Two acquisitions racing to finish cannot claim the same number."""
    log = SessionLog()
    made = [
        SessionLogEntry(
            index=99,
            label=f"acquisition-{number}",
            reason="",
            targets=("scanner",),
            holder="me",
            started_at=datetime.datetime.now(tz=datetime.UTC),
            duration_s=0.0,
        )
        for number in range(_LOGGED_ENTRIES)
    ]
    stamped = [log.append(entry) for entry in made]
    assert [entry.index for entry in stamped] == [1, 2, 3]
    assert len(log) == _LOGGED_ENTRIES
    assert isinstance(log.entries, tuple)
    assert log.latest is not None
    assert log.latest.label == "acquisition-2"


def test_the_log_has_no_way_to_forget_an_acquisition():
    """
    Append-only is a rule, not an omission.

    A panel that could delete the record of an acquisition is a panel
    that can make a session claim something false, so the type simply
    has no verb for it.
    """
    log = SessionLog()
    for name in ("remove", "clear", "pop", "__delitem__", "__setitem__"):
        assert not hasattr(log, name)


def test_an_empty_series_describes_itself_as_empty_rather_than_raising():
    """An acquisition of nothing is a real outcome; the log renders it."""
    assert describe_frames([]) == ((), "", "")
