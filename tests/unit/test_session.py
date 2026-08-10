"""
Unit tests for the session abstraction: naming, context, and partial writes.

No GUI and no device extra needed — these run in the base test env against
in-memory fake frames and real files on disk, including reading written
files back so the metadata round trip is exercised for real rather than
mocked.
"""

import datetime
import json
import threading
from collections.abc import Iterator

import h5py
import numpy as np
import pytest

from miainwoodpecker.devices import Frame
from miainwoodpecker.storage.nexus import read_series
from miainwoodpecker.storage.session import (
    RecordingJob,
    Session,
    default_root,
    read_session_context,
)


def make_frame(value: float = 1.0, size: int = 4) -> Frame:
    """Return a small constant frame with plausible vendor metadata."""
    return Frame(
        data=np.full((size, size), value, dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={"frame_number": int(value), "fov_nm": 10.0},
    )


def test_session_creates_its_root_and_sidecar(tmp_path):
    """A new session makes its directory and records its context on disk."""
    root = tmp_path / "monday"
    session = Session(root, operator="M. Sarahan", sample="Au on C", notes="test grid")

    assert root.is_dir()
    stored = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert stored["operator"] == "M. Sarahan"
    assert stored["sample"] == "Au on C"
    assert stored["notes"] == "test grid"
    assert session.context == {
        "operator": "M. Sarahan",
        "sample": "Au on C",
        "notes": "test grid",
    }


def test_reopening_a_session_reuses_the_directory_and_its_context(tmp_path):
    """An existing session directory is reused, not cleared, and keeps context."""
    first = Session(tmp_path / "shift", operator="A", sample="grid-1")
    first.record([make_frame()], label="scan")

    second = Session(tmp_path / "shift")

    assert second.operator == "A"
    assert second.sample == "grid-1"
    assert len(second.recordings()) == 1


def test_reopening_only_overrides_context_fields_passed_explicitly(tmp_path):
    """Omitting a field keeps the stored value instead of blanking it."""
    Session(tmp_path / "shift", operator="A", sample="grid-1", notes="morning")

    reopened = Session(tmp_path / "shift", sample="grid-2")

    assert reopened.operator == "A"
    assert reopened.sample == "grid-2"
    assert reopened.notes == "morning"


def test_session_root_that_is_a_file_is_refused(tmp_path):
    """A path that exists as a file is not silently treated as a session."""
    not_a_dir = tmp_path / "oops.txt"
    not_a_dir.write_text("data", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        Session(not_a_dir)


def test_recording_names_are_sequenced_labelled_and_timestamped(tmp_path):
    """Filenames carry an incrementing index, the label slug, and a UTC stamp."""
    session = Session(tmp_path / "s")

    first = session.record([make_frame()], label="Scan HAADF")
    second = session.record([make_frame()], label="Scan HAADF")

    assert first.index == 1
    second_index = 2
    assert second.index == second_index
    assert first.label == "scan-haadf"
    assert first.path.name.startswith("0001-scan-haadf-")
    assert first.path.suffix == ".nxs"
    assert first.started_at.tzinfo is not None


def test_reserved_paths_never_collide_even_within_one_second(tmp_path):
    """Reserving is atomic, so same-second reservations get distinct names."""
    session = Session(tmp_path / "s")

    paths = [session.reserve_path("burst") for _ in range(25)]

    assert len({path.name for path in paths}) == len(paths)
    assert all(path.exists() for path in paths)


def test_reserved_paths_never_collide_across_threads(tmp_path):
    """Two threads reserving at once cannot claim the same filename."""
    session = Session(tmp_path / "s")
    claimed: list = []
    lock = threading.Lock()

    def reserve() -> None:
        for _ in range(20):
            path = session.reserve_path("burst")
            with lock:
                claimed.append(path)

    threads = [threading.Thread(target=reserve) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert len({path.name for path in claimed}) == len(claimed)


def test_numbering_continues_after_reopening_a_session(tmp_path):
    """A restarted session keeps numbering where the previous run stopped."""
    Session(tmp_path / "s").record([make_frame()], label="scan")

    resumed = Session(tmp_path / "s").record([make_frame()], label="scan")

    expected_index = 2
    assert resumed.index == expected_index


def test_record_streams_frames_and_reads_back(tmp_path):
    """Recorded frames land in a readable NeXus file with the right count."""
    session = Session(tmp_path / "s")

    recording = session.record(
        [make_frame(1.0), make_frame(2.0), make_frame(3.0)], label="series"
    )

    expected_count = 3
    assert recording.frame_count == expected_count
    assert recording.readable
    replayed = list(read_series(recording.path))
    assert len(replayed) == expected_count
    assert replayed[0][0][0, 0] == pytest.approx(1.0)


def test_session_context_round_trips_through_a_written_file(tmp_path):
    """Operator, sample, and notes survive the trip into HDF5 and back out."""
    session = Session(
        tmp_path / "s", operator="M. Sarahan", sample="Au on C", notes="grid 3, hole 2"
    )

    recording = session.record([make_frame()], label="scan-haadf")

    context = read_session_context(recording.path)
    assert context["operator"] == "M. Sarahan"
    assert context["sample"] == "Au on C"
    assert context["notes"] == "grid 3, hole 2"
    assert context["label"] == "scan-haadf"
    assert context["root"] == str(session.root)


def test_recorded_file_keeps_vendor_metadata_alongside_session_context(tmp_path):
    """Injecting context does not displace the frame's own vendor metadata."""
    session = Session(tmp_path / "s", operator="A")

    recording = session.record([make_frame()], label="scan")

    with h5py.File(recording.path, "r") as handle:
        stored = json.loads(handle["entry/metadata/vendor_metadata_json"][()])
    expected_fov_nm = 10.0
    assert stored["fov_nm"] == expected_fov_nm
    assert stored["session_operator"] == "A"


def test_title_defaults_to_label_plus_session_context(tmp_path):
    """The NeXus title carries sample and operator, since the writer has no fields."""
    session = Session(tmp_path / "s", operator="A", sample="Au on C")

    recording = session.record([make_frame()], label="scan")

    with h5py.File(recording.path, "r") as handle:
        title = handle["entry/title"][()].decode("utf-8")
    assert "scan" in title
    assert "Au on C" in title
    assert "A" in title


def test_updating_context_mid_session_affects_later_recordings_only(tmp_path):
    """Changing the sample re-tags new files and leaves earlier ones alone."""
    session = Session(tmp_path / "s", sample="grid-1")
    first = session.record([make_frame()], label="scan")

    session.update_context(sample="grid-2")
    second = session.record([make_frame()], label="scan")

    assert read_session_context(first.path)["sample"] == "grid-1"
    assert read_session_context(second.path)["sample"] == "grid-2"


def test_recordings_enumerates_in_acquisition_order(tmp_path):
    """Enumeration reports each recording once, sorted by sequence number."""
    session = Session(tmp_path / "s")
    session.record([make_frame()], label="scan")
    session.record([make_frame(), make_frame()], label="camera")

    found = session.recordings()

    assert [recording.index for recording in found] == [1, 2]
    assert [recording.label for recording in found] == ["scan", "camera"]
    assert [recording.frame_count for recording in found] == [1, 2]


def test_recordings_ignores_foreign_files(tmp_path):
    """Files this session did not mint are not reported as recordings."""
    session = Session(tmp_path / "s")
    session.record([make_frame()], label="scan")
    (session.root / "notes.txt").write_text("hello", encoding="utf-8")
    (session.root / "someone-elses.nxs").write_bytes(b"not hdf5")

    assert len(session.recordings()) == 1


def test_an_acquisition_that_fails_midway_still_leaves_a_valid_file(tmp_path):
    """
    A device error unwinds Python, so NexusWriter still finalizes the file.

    This is the interruption mode that matters most in a pilot: the file
    is short but complete and plottable, not corrupt.
    """
    session = Session(tmp_path / "s")

    def failing_frames() -> Iterator[Frame]:
        yield make_frame(1.0)
        yield make_frame(2.0)
        msg = "detector dropped out"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="detector dropped out"):
        session.record(failing_frames(), label="scan")

    (recording,) = session.recordings()
    assert recording.readable
    expected_count = 2
    assert recording.frame_count == expected_count
    with h5py.File(recording.path, "r") as handle:
        # The NXdata plotting hints are written in close(), so their
        # presence is what proves the file was really finalized.
        assert handle["entry/data"].attrs["signal"] == "data"
        assert "entry/end_time" in handle


def test_an_empty_acquisition_produces_a_readable_but_frameless_file(tmp_path):
    """Recording nothing is reported honestly rather than raising."""
    session = Session(tmp_path / "s")

    recording = session.record([], label="scan")

    assert recording.readable
    assert recording.frame_count == 0
    assert read_session_context(recording.path) == {}


def test_a_reserved_but_unwritten_name_is_reported_as_unreadable(tmp_path):
    """A placeholder from a reservation that never got written is not a data file."""
    session = Session(tmp_path / "s")

    session.reserve_path("scan")

    (recording,) = session.recordings()
    assert not recording.readable
    assert recording.frame_count == 0


def test_recording_job_records_off_the_calling_thread(tmp_path):
    """The job runs a real recording on a worker thread and reports the result."""
    session = Session(tmp_path / "s")
    job = RecordingJob(session, [make_frame() for _ in range(4)], label="scan")

    job.start()
    job.join()

    assert not job.is_running
    assert job.error is None
    assert job.result is not None
    expected_count = 4
    assert job.result.frame_count == expected_count
    assert job.frames_recorded == expected_count


def test_cancelling_a_recording_job_keeps_a_valid_partial_file(tmp_path):
    """Cancellation stops pulling frames, so the file is short but complete."""
    session = Session(tmp_path / "s")
    started = threading.Event()
    release = threading.Event()

    def blocking_frames() -> Iterator[Frame]:
        yield make_frame(1.0)
        started.set()
        release.wait(30)
        for value in range(2, 100):
            yield make_frame(float(value))

    job = RecordingJob(session, blocking_frames(), label="scan")
    job.start()
    assert started.wait(30)
    job.cancel()
    release.set()
    job.join()

    assert job.is_cancelled
    assert job.error is None
    assert job.result is not None
    assert job.result.readable
    assert job.result.frame_count == 1
    with h5py.File(job.result.path, "r") as handle:
        assert handle["entry/data"].attrs["signal"] == "data"


def test_recording_job_captures_a_failure_instead_of_raising(tmp_path):
    """A device error on the worker thread lands in .error, like LiveAcquisition."""
    session = Session(tmp_path / "s")

    def failing_frames() -> Iterator[Frame]:
        yield make_frame()
        msg = "detector dropped out"
        raise RuntimeError(msg)

    job = RecordingJob(session, failing_frames(), label="scan")
    job.start()
    job.join()

    assert isinstance(job.error, RuntimeError)
    assert job.result is None
    (recording,) = session.recordings()
    assert recording.readable
    assert recording.frame_count == 1


def test_default_root_is_a_dated_directory(tmp_path):
    """The fallback session root is one directory per calendar day."""
    root = default_root(tmp_path)

    assert root.parent == tmp_path
    assert root.name == datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d")
    assert not root.exists()
