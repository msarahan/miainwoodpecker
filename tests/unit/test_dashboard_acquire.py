"""
Acquiring from a dashboard: off the display thread, and on the record.

Four properties, and the first is the one the whole design turns on.

* **The lease is taken on a worker, never on the caller's thread.**
  Taking one means waiting out the pass already in flight, which is
  ``height x width x dwell`` - up to minutes on a big slow scan. A
  notebook that took it inline would stop refreshing its tiles for that
  long. The test below blocks the lease outright and asserts the caller
  came back anyway.
* **Every attempt reaches the log, refusals included.** A lease the
  broker refused is part of what happened during the shift.
* **One acquisition is several signals, and each gets its own file.** A
  pass read out on two detectors is one item with two datasets, written
  to two files that share a sequence number - which is what makes "send
  me the HAADF" a copy rather than an extraction.
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
import itertools
import threading
import time
from collections.abc import Iterator, Sequence

import numpy as np
import pytest

from miainwoodpecker.acquisition.sequence import multichannel_scan_series
from miainwoodpecker.broker.interface import DeviceBusyError
from miainwoodpecker.broker.local import LocalBroker
from miainwoodpecker.dashboard.acquisition import (
    AcquisitionJob,
    AcquisitionRequest,
    camera_request,
    named,
    scan_request,
)
from miainwoodpecker.dashboard.session_log import (
    SessionLog,
    SessionLogEntry,
    describe_dataset,
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
_TWO_PASSES = 2
_TWO_SIGNALS = 2
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


def test_a_successful_acquisition_lands_in_the_log_with_a_signal_per_detector(broker):
    """
    One entry per acquisition, one dataset per detector, each with a picture.

    The two detectors of a pass are one *item* - they came from one
    traversal of the probe - so they are one entry; but they are separate
    signals, so each has its own thumbnail, its own metadata and, when
    there is a session, its own file.
    """
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
    assert entry.label == "scan"
    assert entry.frame_count == _TWO_PASSES_TWO_DETECTORS
    assert [dataset.name for dataset in entry.datasets] == ["HAADF", "MAADF"]
    for dataset in entry.datasets:
        assert dataset.frame_count == _TWO_PASSES
        assert dataset.shape == _PARAMETERS.shape
        assert dataset.thumbnail.startswith("data:image/png;base64,")
        # No session was attached, so the frames have nowhere else to be
        # and the entry keeps them - which is what makes Save possible.
        assert dataset.in_memory
        assert len(dataset.frames) == _TWO_PASSES
    haadf = entry.dataset("HAADF")
    assert haadf is not None
    assert highlights(haadf)["channel_name"] == "HAADF"
    # Not a credential, whatever the name looks like: the pass identity
    # is what makes per-pixel arithmetic between two channels legitimate.
    assert highlights(haadf)["scan_pass_id"] == "pass-1"  # noqa: S105
    maadf = entry.dataset("MAADF")
    assert maadf is not None
    assert highlights(maadf)["channel_name"] == "MAADF"
    # Same pass as the HAADF above: one traversal, both detectors.
    assert highlights(maadf)["scan_pass_id"] == "pass-1"  # noqa: S105


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
    # No signals at all, rather than one empty one: nothing was acquired,
    # and a dataset row with no picture would suggest something was.
    assert entry.datasets == ()
    assert entry.pending == ()


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
    (dataset,) = entry.datasets
    assert dataset.path is not None
    assert not dataset.in_memory
    # The file has them, so the kernel does not keep a second copy.
    assert dataset.frames == ()
    assert dataset.frame_count == _CAMERA_FRAMES
    assert entry.frame_count == _CAMERA_FRAMES
    assert entry.pending == ()
    recorded = session.recordings()
    assert [record.frame_count for record in recorded] == [_CAMERA_FRAMES]
    assert str(recorded[0].path) == dataset.path


def test_each_detector_of_a_pass_gets_its_own_file_under_one_number(
    broker,
    tmp_path,
):
    """
    Two detectors, two files, one sequence number.

    Separate files because every external tool an operator reaches for -
    HyperSpy, a NeXus viewer, a file manager - takes a file and gives
    back a signal. The shared number is what still says the two came out
    of one pass.
    """
    log = SessionLog()
    session = Session(tmp_path / "shift")
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
        session=session,
    )
    job.start()
    assert _finished(job)
    assert job.error is None
    (entry,) = log.entries
    paths = {dataset.name: dataset.path for dataset in entry.datasets}
    assert set(paths) == {"HAADF", "MAADF"}
    assert all(path is not None for path in paths.values())
    assert paths["HAADF"] != paths["MAADF"]
    recorded = session.recordings()
    assert len(recorded) == _TWO_SIGNALS
    assert {record.index for record in recorded} == {1}
    assert {record.label for record in recorded} == {"scan-haadf", "scan-maadf"}
    # Each file holds only its own detector's frames, not the interleaved
    # stream both came out of.
    assert [record.frame_count for record in recorded] == [_TWO_PASSES] * _TWO_SIGNALS


def test_a_multi_step_recipe_becomes_one_item_with_a_signal_per_step(broker):
    """
    ``named`` is the whole extension mechanism for a composite acquisition.

    A survey, then the thing you came for, then the same survey again to
    see what the beam did - one lease, one item, three signals. Nothing
    in the log or the storage layer had to learn about the recipe; the
    request labelled its own steps.
    """
    log = SessionLog()
    request = AcquisitionRequest(
        targets=("scanner",),
        label="survey-and-followup",
        reason="a survey, and the same area again afterwards",
        build=lambda leased: itertools.chain(
            named(
                "survey",
                multichannel_scan_series(
                    leased.scanner("scanner"),
                    _PARAMETERS,
                    1,
                    channels=[0],
                ),
            ),
            named(
                "followup",
                multichannel_scan_series(
                    leased.scanner("scanner"),
                    _PARAMETERS,
                    1,
                    channels=[0],
                ),
            ),
        ),
    )
    job = AcquisitionJob(broker, request, log)
    job.start()
    assert _finished(job)
    assert job.error is None
    (entry,) = log.entries
    # In step order, which is first-appearance order - not the order the
    # detector happens to name itself, which is "HAADF" for both.
    assert [dataset.name for dataset in entry.datasets] == ["survey", "followup"]


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


def _one_signal_entry() -> SessionLogEntry:
    """
    Return an entry holding a single in-memory signal.

    Returns
    -------
    SessionLogEntry
        The entry, unstamped.
    """
    frame = Frame(
        data=np.zeros((4, 4), dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={"device_id": "scan-unit", "channel_name": "HAADF"},
    )
    return SessionLogEntry(
        index=0,
        label="scan",
        reason="one pass",
        targets=("scanner",),
        holder="me",
        started_at=datetime.datetime.now(tz=datetime.UTC),
        duration_s=0.1,
        datasets=(describe_dataset("HAADF", [frame]),),
    )


def test_marking_a_signal_stored_moves_it_and_changes_nothing_else():
    """
    Where the bytes live is not part of the account of the shift.

    The one mutation besides append, and it says exactly one thing:
    this signal is now at this path. It releases the frames as it does
    so, because the file has them.
    """
    log = SessionLog()
    original = log.append(_one_signal_entry())

    updated = log.mark_stored(original.index, "HAADF", "/data/0001-scan-haadf.nxs")

    assert updated is not None
    (dataset,) = updated.datasets
    assert dataset.path == "/data/0001-scan-haadf.nxs"
    assert dataset.frames == ()
    assert not dataset.in_memory
    # Everything the entry claims about what happened is untouched.
    assert (updated.label, updated.reason, updated.holder) == (
        original.label,
        original.reason,
        original.holder,
    )
    assert updated.started_at == original.started_at
    assert updated.frame_count == original.frame_count
    assert log.pending == ()


def test_a_signal_that_is_already_stored_is_not_repointed():
    """
    Data that has been written has not become unwritten.

    A second path would quietly disown the first file, so it is refused
    rather than applied - and refused by saying nothing changed, which
    is what a caller retrying a flush needs to hear.
    """
    log = SessionLog()
    entry = log.append(_one_signal_entry())
    log.mark_stored(entry.index, "HAADF", "/data/first.nxs")

    assert log.mark_stored(entry.index, "HAADF", "/data/second.nxs") is None

    (dataset,) = log.entries[0].datasets
    assert dataset.path == "/data/first.nxs"


def test_marking_a_signal_the_log_does_not_have_changes_nothing():
    """An index or a name from somewhere else must not invent an entry."""
    log = SessionLog()
    entry = log.append(_one_signal_entry())

    assert log.mark_stored(entry.index, "MAADF", "/data/wrong.nxs") is None
    assert log.mark_stored(entry.index + 1, "HAADF", "/data/wrong.nxs") is None
    assert log.mark_stored(0, "HAADF", "/data/wrong.nxs") is None

    assert len(log.pending) == 1


def test_an_empty_series_describes_itself_as_empty_rather_than_raising():
    """An acquisition of nothing is a real outcome; the log renders it."""
    assert describe_frames([]) == ((), "", "")


def test_a_projected_readout_is_described_without_a_thumbnail():
    """
    A spectrum is not a picture, and asking for one used to raise.

    A camera in the projected readout delivers a 1D frame, which the PNG
    encoder refuses - correctly, since a one-pixel-high strip is a
    picture of nothing. Describing it has to say so rather than take
    that refusal through the log.
    """
    spectrum = Frame(
        data=np.arange(16, dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={"device_id": "eels_camera", "readout": "projected"},
    )

    shape, dtype, thumbnail = describe_frames([spectrum])

    assert shape == (16,)
    assert dtype == "float32"
    assert thumbnail == ""
    # And it is still a signal the log holds, with its metadata intact.
    dataset = describe_dataset("EELS", [spectrum])
    assert dataset.frame_count == 1
    assert dataset.in_memory
    assert highlights(dataset)["readout"] == "projected"
