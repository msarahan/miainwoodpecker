"""
Unit tests for the session abstraction: naming, context, partial writes, readback.

No GUI and no device extra needed — these run in the base test env against
in-memory fake frames and real files on disk, including reading written
files back so the metadata round trip is exercised for real rather than
mocked. The two degraded-file cases are produced rather than simulated: an
abandoned writer is really abandoned, and a hard-killed acquisition really
sends itself ``SIGKILL`` in a child process, because what those leave on
disk is the thing under test.
"""

import datetime
import json
import signal
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import h5py
import numpy as np
import pytest

from miainwoodpecker.devices import Frame
from miainwoodpecker.storage import layout
from miainwoodpecker.storage.nexus import NexusWriter, read_series
from miainwoodpecker.storage.session import (
    LoadJob,
    RecordingJob,
    RecordingReadError,
    Session,
    _is_locked,
    annotate,
    default_root,
    describe,
    estimate_size,
    find_recordings,
    format_bytes,
    free_space,
    load_recording,
    read_session_context,
)

_SIGKILL_RETURNCODE = -9


def make_frame(value: float = 1.0, size: int = 4) -> Frame:
    """Return a small constant frame with plausible vendor metadata."""
    return Frame(
        data=np.full((size, size), value, dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={"frame_number": int(value), "fov_nm": 10.0},
    )


def abandon_a_writer(path, frame_count: int = 3) -> None:
    """
    Leave the file an abandoned-but-cleanly-exited writer leaves.

    All frames present, no ``/entry/data``, no ``end_time``, no metadata —
    the second interruption mode in the migration plan's Phase 3 table.
    """
    writer = NexusWriter(path, title="abandoned")
    # Deliberately entered without ever being __exit__ed or closed.
    writer.__enter__()
    for value in range(frame_count):
        writer.append(make_frame(float(value)))
    writer.flush()


def kill_a_writer_mid_acquisition(path) -> None:
    """
    Leave the file a ``SIGKILL``-ed acquisition leaves, by really killing one.

    A child process appends frames and then sends itself ``SIGKILL`` without
    flushing, so HDF5's buffered object headers never reach disk. Done for
    real rather than by writing plausible garbage: that the container is
    unrecoverable is the claim under test.
    """
    script = textwrap.dedent(
        """
        import datetime, os, signal, sys
        import numpy as np
        from miainwoodpecker.devices.interface import Frame
        from miainwoodpecker.storage.nexus import NexusWriter

        writer = NexusWriter(sys.argv[1], title="killed")
        writer.__enter__()
        for value in range(3):
            writer.append(
                Frame(
                    data=np.full((64, 64), float(value), dtype=np.float32),
                    timestamp=datetime.datetime.now(tz=datetime.UTC),
                    metadata={},
                )
            )
        os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", script, str(path)],
        check=False,
        capture_output=True,
    )
    assert result.returncode == _SIGKILL_RETURNCODE, result.stderr.decode()


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
        "instrument": "",
        "site": "",
        "sample": "Au on C",
        "sample_area": "",
        "task": "",
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


def test_a_finished_recording_reports_itself_finalized(tmp_path):
    """A normal recording has the /entry/data group its writer's close() creates."""
    session = Session(tmp_path / "s")

    recording = session.record([make_frame(), make_frame()], label="scan")

    assert recording.readable
    assert recording.finalized


def test_an_abandoned_writer_keeps_every_frame_but_is_not_finalized(tmp_path):
    """
    The measured abandoned-writer case: frames intact, finalization missing.

    This is the state that matters for reading back, because it separates
    what the viewer can show from what the Phase 4 adapters can analyze.
    """
    session = Session(tmp_path / "s")
    target = session.reserve_path("scan")

    abandon_a_writer(target)

    (recording,) = session.recordings()
    expected_count = 3
    assert recording.readable
    assert recording.frame_count == expected_count
    assert not recording.finalized
    with h5py.File(target, "r") as handle:
        assert "data" not in handle["entry"]
        assert "end_time" not in handle["entry"]


def test_an_abandoned_recording_still_loads_every_frame(tmp_path):
    """
    load_recording reads the detector group, so an unfinalized file opens.

    The Phase 4 adapters read ``/entry/data`` and cannot; this path reads
    ``/entry/instrument/detector``, which is where the frames actually are.
    """
    session = Session(tmp_path / "s")
    target = session.reserve_path("scan")
    abandon_a_writer(target)

    loaded = load_recording(target)

    expected_shape = (3, 4, 4)
    assert loaded.data.shape == expected_shape
    assert not loaded.truncated
    assert not loaded.recording.finalized
    assert loaded.data[2][0, 0] == pytest.approx(2.0)


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="needs SIGKILL to leave a genuinely unflushed HDF5 file; Windows has none",
)
def test_a_hard_killed_recording_reports_a_reason_instead_of_an_h5py_error(tmp_path):
    """A SIGKILL-ed acquisition leaves a file that cannot be opened at all."""
    target = tmp_path / "0001-killed-20260810T120000Z.nxs"
    kill_a_writer_mid_acquisition(target)

    described = describe(target)
    assert not described.readable
    assert not described.finalized
    assert described.frame_count == 0

    with pytest.raises(RecordingReadError, match="does not open as an HDF5 file"):
        load_recording(target)


def test_load_recording_returns_a_stack_even_for_one_frame(tmp_path):
    """A recording is a series, so its data model stays three-dimensional."""
    session = Session(tmp_path / "s")
    recording = session.record([make_frame()], label="scan")

    loaded = load_recording(recording.path)

    expected_shape = (1, 4, 4)
    assert loaded.data.shape == expected_shape
    assert len(loaded.frame_times) == 1


def test_load_recording_reports_elapsed_times_from_the_file(tmp_path):
    """Frame times come back with the frames, not recomputed from the clock."""
    session = Session(tmp_path / "s")
    recording = session.record([make_frame(), make_frame(), make_frame()], label="scan")

    loaded = load_recording(recording.path)

    expected_count = 3
    assert len(loaded.frame_times) == expected_count
    assert loaded.frame_times[0] == pytest.approx(0.0)
    assert all(elapsed >= 0.0 for elapsed in loaded.frame_times)


def test_load_recording_stops_at_its_byte_budget_and_says_so(tmp_path):
    """A long recording loads as many frames as fit rather than exhausting memory."""
    session = Session(tmp_path / "s")
    recording = session.record([make_frame(float(i)) for i in range(5)], label="scan")
    one_frame_bytes = 4 * 4 * 4

    loaded = load_recording(recording.path, budget_bytes=2 * one_frame_bytes)

    expected_shown = 2
    assert loaded.truncated
    assert loaded.data.shape[0] == expected_shown
    assert loaded.recording.frame_count == 5  # noqa: PLR2004 - the file's real count


def test_load_recording_always_reads_at_least_one_frame(tmp_path):
    """A frame bigger than the whole budget still displays rather than failing."""
    session = Session(tmp_path / "s")
    recording = session.record([make_frame(), make_frame()], label="scan")

    loaded = load_recording(recording.path, budget_bytes=1)

    assert loaded.truncated
    assert loaded.data.shape[0] == 1


def test_load_recording_refuses_an_empty_recording_with_a_reason(tmp_path):
    """Opening a file whose acquisition produced nothing says that, not "no data"."""
    session = Session(tmp_path / "s")
    recording = session.record([], label="scan")

    with pytest.raises(RecordingReadError, match="holds no frames"):
        load_recording(recording.path)


def test_describe_handles_a_file_no_session_minted(tmp_path):
    """A file copied from elsewhere is described from its stem, not refused."""
    session = Session(tmp_path / "s")
    recording = session.record([make_frame()], label="scan")
    foreign = tmp_path / "from-the-other-microscope.nxs"
    foreign.write_bytes(recording.path.read_bytes())

    described = describe(foreign)

    assert described.index == 0
    assert described.label == "from-the-other-microscope"
    assert described.readable
    assert described.finalized
    assert described.frame_count == 1
    assert described.started_at.tzinfo is not None


def test_load_job_reads_off_the_calling_thread(tmp_path):
    """The load job mirrors RecordingJob: worker thread, polled result, no raising."""
    session = Session(tmp_path / "s")
    recording = session.record([make_frame() for _ in range(4)], label="scan")
    job = LoadJob(recording.path)

    job.start()
    job.join()

    assert not job.is_running
    assert job.error is None
    assert job.result is not None
    expected_count = 4
    assert job.result.data.shape[0] == expected_count
    assert job.frames_loaded == expected_count


def test_load_job_captures_an_unreadable_file_instead_of_raising(tmp_path):
    """A damaged file lands in .error for a caller to display, like LiveAcquisition."""
    damaged = tmp_path / "0001-damaged-20260810T120000Z.nxs"
    damaged.write_bytes(b"not hdf5")
    job = LoadJob(damaged)

    job.start()
    job.join()

    assert isinstance(job.error, RecordingReadError)
    assert job.result is None


def test_session_context_lands_in_real_nexus_groups(tmp_path):
    """Sample and operator go into NXsample/NXuser, not only a metadata blob."""
    session = Session(tmp_path / "s", operator="M. Sarahan", sample="Au on C")

    recording = session.record([make_frame()], label="scan")

    with h5py.File(recording.path, "r") as handle:
        assert handle["entry/sample"].attrs["NX_class"] == "NXsample"
        assert handle["entry/sample/name"][()].decode("utf-8") == "Au on C"
        assert handle["entry/user"].attrs["NX_class"] == "NXuser"
        assert handle["entry/user/name"][()].decode("utf-8") == "M. Sarahan"


def test_a_session_without_context_writes_no_empty_nexus_groups(tmp_path):
    """An unfilled field means no group, rather than a group claiming nothing."""
    session = Session(tmp_path / "s")

    recording = session.record([make_frame()], label="scan")

    with h5py.File(recording.path, "r") as handle:
        assert "sample" not in handle["entry"]
        assert "user" not in handle["entry"]
        assert "notes" not in handle["entry"]


def test_multi_line_session_notes_survive_to_the_file_and_the_sidecar(tmp_path):
    """A shift's worth of notes is many lines, and stays many lines."""
    notes = "09:10 grid loaded\n09:40 LN2 topped up\n10:15 drift settled"
    session = Session(tmp_path / "s", notes=notes)

    recording = session.record([make_frame()], label="scan")

    stored = json.loads((session.root / "session.json").read_text(encoding="utf-8"))
    assert stored["notes"] == notes
    with h5py.File(recording.path, "r") as handle:
        assert handle["entry/notes"].attrs["NX_class"] == "NXnote"
        description = handle["entry/notes/description"][()].decode("utf-8")
    assert description == f"session: {notes}"
    assert read_session_context(recording.path)["notes"] == notes


def test_a_per_recording_note_is_labelled_apart_from_the_session_notes(tmp_path):
    """
    Both note scopes reach the one NXnote, each saying which it is.

    A single description field mixing them would leave a reader unable to
    tell a shift-long observation from a statement about this one file.
    """
    session = Session(tmp_path / "s", notes="Au on C, 200kV")

    recording = session.record([make_frame()], label="scan", note="hole 4, after tilt")

    with h5py.File(recording.path, "r") as handle:
        description = handle["entry/notes/description"][()].decode("utf-8")
    assert description == "session: Au on C, 200kV\n\nrecording: hole 4, after tilt"
    assert read_session_context(recording.path)["note"] == "hole 4, after tilt"


def test_a_per_recording_note_applies_only_to_the_recording_given_it(tmp_path):
    """The next recording does not inherit the previous one's note."""
    session = Session(tmp_path / "s")

    first = session.record([make_frame()], label="scan", note="hole 4")
    second = session.record([make_frame()], label="scan")

    assert read_session_context(first.path)["note"] == "hole 4"
    assert "note" not in read_session_context(second.path)
    with h5py.File(second.path, "r") as handle:
        assert "notes" not in handle["entry"]


def test_recording_job_passes_a_per_recording_note_through(tmp_path):
    """The worker-thread path carries the note, not just the direct call."""
    session = Session(tmp_path / "s")
    job = RecordingJob(session, [make_frame()], label="scan", note="hole 4")

    job.start()
    job.join()

    assert job.result is not None
    assert read_session_context(job.result.path)["note"] == "hole 4"


def test_default_root_is_a_dated_directory(tmp_path):
    """The fallback session root is one directory per calendar day."""
    root = default_root(tmp_path)

    assert root.parent == tmp_path
    assert root.name == datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d")
    assert not root.exists()


def _note_of(path) -> str:
    """Read a recording's NXnote description back off disk."""
    with h5py.File(path, "r") as handle:
        raw = handle["entry/notes/description"][()]
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


def test_annotate_appends_without_losing_the_acquisition_note(tmp_path):
    """A note added afterwards is added, not substituted for the original."""
    session = Session(tmp_path / "shift", notes="session-wide observation")
    recording = session.record([make_frame()], label="scan", note="at acquisition")

    returned = annotate(recording.path, "  charged badly, discard  ")

    stored = _note_of(recording.path)
    assert stored == returned
    # Both original scopes survive, and the addition is labelled as one.
    assert "session: session-wide observation" in stored
    assert "recording: at acquisition" in stored
    assert "charged badly, discard" in stored
    assert stored.index("recording: at acquisition") < stored.index("charged badly")
    # Whitespace is trimmed, and the label carries when it was added.
    assert "added " in stored
    assert "  charged" not in stored


def test_annotate_accumulates_across_several_additions(tmp_path):
    """Annotating twice keeps both, in the order they were added."""
    session = Session(tmp_path / "shift")
    recording = session.record([make_frame()], label="scan")

    annotate(recording.path, "first thought")
    annotate(recording.path, "second thought")

    stored = _note_of(recording.path)
    assert stored.index("first thought") < stored.index("second thought")


def test_annotate_works_on_a_recording_that_had_no_note(tmp_path):
    """A recording written with no notes at all gains an NXnote group."""
    session = Session(tmp_path / "shift")
    recording = session.record([make_frame()], label="scan")
    with h5py.File(recording.path, "r") as handle:
        assert "entry/notes" not in handle

    annotate(recording.path, "added later")

    with h5py.File(recording.path, "r") as handle:
        assert handle["entry/notes"].attrs["NX_class"] == "NXnote"
    assert "added later" in _note_of(recording.path)


def test_annotate_refuses_blank_text_and_unreadable_files(tmp_path):
    """The two ways to fail both say why rather than writing something useless."""
    session = Session(tmp_path / "shift")
    recording = session.record([make_frame()], label="scan")

    with pytest.raises(RecordingReadError, match="empty note"):
        annotate(recording.path, "   ")

    broken = tmp_path / "not-hdf5.nxs"
    broken.write_bytes(b"not an HDF5 file at all")
    with pytest.raises(RecordingReadError, match="cannot annotate"):
        annotate(broken, "anything")


def test_find_recordings_spans_sessions_newest_first(tmp_path):
    """Cross-session enumeration answers "where is that scan from Tuesday"."""
    base = tmp_path / "data"
    monday = Session(base / "2026-08-10")
    tuesday = Session(base / "2026-08-11")
    monday.record([make_frame()], label="monday-scan")
    tuesday.record([make_frame()], label="tuesday-scan")
    # A loose recording directly in the base, not inside a session directory.
    loose = Session(base)
    loose.record([make_frame()], label="loose-scan")

    found = find_recordings(base)

    labels = [recording.label for recording in found]
    assert sorted(labels) == ["loose-scan", "monday-scan", "tuesday-scan"]
    # Newest first, so "what did I just take" is at the top.
    stamps = [recording.started_at for recording in found]
    assert stamps == sorted(stamps, reverse=True)


def test_find_recordings_is_empty_rather_than_raising_for_a_missing_base(tmp_path):
    """An operator who has recorded nothing yet gets a list, not an error."""
    assert find_recordings(tmp_path / "never-created") == []


def test_estimate_size_is_an_uncompressed_upper_bound():
    """The estimate errs high, which is the useful direction for a warning."""
    frames = 10
    shape = (256, 256)
    assert estimate_size(shape, frames) == 256 * 256 * frames * 4
    assert estimate_size(shape, frames, dtype=np.float64) == 256 * 256 * frames * 8


def test_free_space_answers_for_a_directory_not_yet_created(tmp_path):
    """A session directory chosen but not created still reports its filesystem."""
    assert free_space(tmp_path) > 0
    assert free_space(tmp_path / "does" / "not" / "exist") > 0


def test_format_bytes_uses_the_units_operators_compare_against():
    """Decimal units, matching what df -h and the file manager report."""
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(29_100_000) == "29.1 MB"
    assert format_bytes(2_500_000_000) == "2.5 GB"


def test_session_context_reads_back_from_the_nexus_groups(tmp_path):
    """
    NXsample and NXuser are read, not merely written.

    They are where standard tooling looks, and this project wrote them
    for three phases while reading only its own JSON blob - so the groups
    whose whole justification is interoperability were write-only for the
    code that produced them.
    """
    session = Session(tmp_path / "s", operator="M. Sarahan", sample="Au on C")
    recording = session.record([make_frame()], label="scan-haadf")

    # Remove the blob entirely: whatever comes back now came from the groups.
    with h5py.File(recording.path, "r+") as handle:
        del handle["entry/metadata/vendor_metadata_json"]

    context = read_session_context(recording.path)
    assert context["operator"] == "M. Sarahan"
    assert context["sample"] == "Au on C"


def test_notes_are_not_recovered_from_the_composed_nxnote(tmp_path):
    """
    NXnote/description renders two scopes, so it is not a source for either.

    ``_compose_notes`` prefixes the session's standing notes and the
    recording's own with their scope precisely so a reader can tell them
    apart. Reading ``notes`` back from it would hand the caller
    "session: grid 3" where the session holds "grid 3" - which is why the
    group reader deliberately covers sample and operator only.
    """
    session = Session(tmp_path / "s", notes="grid 3, hole 2")
    recording = session.record([make_frame()], label="scan", note="after tilting")

    assert read_session_context(recording.path)["notes"] == "grid 3, hole 2"
    with h5py.File(recording.path, "r") as handle:
        rendered = handle["entry/notes/description"][()]
    text = rendered.decode("utf-8") if isinstance(rendered, bytes) else str(rendered)
    assert "session: grid 3, hole 2" in text
    assert "recording: after tilting" in text


def test_the_sidecar_survives_a_failed_write(tmp_path):
    """
    An interrupted context save must not blank a shift's session.

    The sidecar was truncate-then-write, so a crash or a full disk left a
    truncated session.json - and _load_sidecar swallows the resulting
    ValueError and silently reverts to blank context. Simulated by making
    the temporary write fail, which is the step that used to be the
    destructive one.
    """
    root = tmp_path / "s"
    session = Session(root, operator="M. Sarahan")
    original = (root / "session.json").read_text(encoding="utf-8")

    def explode(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    with (
        mock.patch.object(Path, "write_text", explode),
        pytest.raises(OSError, match="disk full"),
    ):
        session.update_context(operator="Someone Else")

    # The old sidecar is intact, not a truncated fragment.
    assert (root / "session.json").read_text(encoding="utf-8") == original
    assert Session(root).operator == "M. Sarahan"
    assert not list(root.glob("*.tmp"))


def test_a_locked_recording_is_not_reported_as_damaged(tmp_path):
    """
    A file being written right now is in progress, not broken.

    Session.recordings() opens every file in the directory, including one
    a RecordingJob is actively writing. HDF5 refuses that read with an
    OSError naming a lock, which used to be reported as "damaged - does
    not open" - telling an operator their running acquisition had failed.

    The lock is injected rather than provoked, and that is deliberate:
    HDF5's file locking depends on the platform, the mount, and
    HDF5_USE_FILE_LOCKING, and it does *not* engage in this project's own
    test environment - a second open of a file held for writing simply
    succeeds here. A test that opened the file twice and asserted the
    happy answer would therefore pass without ever reaching the branch it
    claims to cover. Injecting the error the library raises elsewhere
    tests the classification, which is the part this project owns.
    """
    session = Session(tmp_path / "s")
    recording = session.record([make_frame()], label="scan")

    locked = OSError(
        f"Unable to synchronously open file (unable to lock file, errno = 11, "
        f"error message = 'Resource temporarily unavailable'): {recording.path}"
    )
    with mock.patch.object(h5py, "File", side_effect=locked):
        described = describe(recording.path)

    assert described.readable, "a locked file must not be reported as damaged"
    assert not described.finalized
    assert described.frame_count == 0


def test_a_genuinely_broken_file_is_still_reported_as_damaged(tmp_path):
    """The other side of the same branch: a lost container is not 'in use'."""
    session = Session(tmp_path / "s")
    recording = session.record([make_frame()], label="scan")
    recording.path.write_bytes(b"not hdf5 at all")

    described = describe(recording.path)
    assert not described.readable


@pytest.mark.parametrize(
    "message",
    [
        "unable to lock file, errno = 11",
        "Resource temporarily unavailable",
        "file is already open for write",
    ],
)
def test_lock_messages_are_recognized(message):
    """The strings HDF5 actually produces for a file in use."""
    assert _is_locked(OSError(message))


@pytest.mark.parametrize(
    "message",
    ["bad object header version number", "file signature not found", ""],
)
def test_corruption_messages_are_not_mistaken_for_locks(message):
    """A hard-killed writer's file must not be mistaken for a live one."""
    assert not _is_locked(OSError(message))


def test_join_reports_whether_the_worker_actually_finished(tmp_path):
    """
    A timed-out join must not look like a completed one.

    join() returned None either way, so a caller polling result/error
    afterwards saw (None, None) - the same state as "still running" -
    with no way to tell which had happened.
    """
    release = threading.Event()

    def blocking_frames() -> Iterator[Frame]:
        release.wait(10.0)
        yield make_frame()

    session = Session(tmp_path / "s")
    job = RecordingJob(session, blocking_frames(), label="scan")
    job.start()
    try:
        assert job.join(timeout=0.05) is False
        assert job.result is None
    finally:
        release.set()
    assert job.join(timeout=10.0) is True
    assert job.result is not None


def test_a_cancelled_job_can_be_started_again(tmp_path):
    """
    start() clears cancellation, so a restarted job is not born cancelled.

    _cancelled was never reset, so a cancelled-then-restarted job
    returned on its first frame and wrote an empty file while reporting
    success.
    """
    session = Session(tmp_path / "s")
    job = RecordingJob(session, iter([make_frame()]), label="scan")
    job.cancel()
    job.start()
    assert job.join(timeout=10.0)
    assert job.is_cancelled is False

    # And a genuine run after the restart records its frame.
    job = RecordingJob(session, iter([make_frame(), make_frame()]), label="scan")
    job.cancel()
    job._frames = iter([make_frame(), make_frame()])  # noqa: SLF001 - fresh generator
    job.start()
    assert job.join(timeout=10.0)
    assert job.result is not None
    assert job.result.frame_count == 2  # noqa: PLR2004


def test_joining_a_job_that_never_started_succeeds(tmp_path):
    """Nothing to wait for is a finished job, not a failure."""
    session = Session(tmp_path / "s")
    assert RecordingJob(session, iter([]), label="scan").join(timeout=0.01) is True


def test_the_full_session_vocabulary_reaches_its_nexus_groups(tmp_path):
    """
    Four of the seven context fields land in real NeXus fields.

    The vocabulary is Nion's, from its scripting guide; the point of
    adopting it rather than inventing one is that these are the facts
    that make a recording identifiable a year later. Each mapping is to a
    field that means what the value means — the sample area really is
    ``NXsample``'s ``description``, and the microscope really is
    ``NXinstrument``'s ``name``.
    """
    session = Session(
        tmp_path / "session",
        operator="M. Sarahan",
        instrument="Nion UltraSTEM 200",
        site="SuperSTEM",
        sample="Au on C",
        sample_area="hole 4",
        task="ADF survey",
        notes="grid 3",
    )
    recording = session.record([make_frame()], label="survey")

    with h5py.File(recording.path, "r") as handle:
        assert handle[f"{layout.SAMPLE_GROUP}/name"][()].decode() == "Au on C"
        assert handle[f"{layout.SAMPLE_GROUP}/description"][()].decode() == "hole 4"
        assert handle[f"{layout.USER_GROUP}/name"][()].decode() == "M. Sarahan"
        assert (
            handle[layout.INSTRUMENT_NAME][()].decode() == "Nion UltraSTEM 200"
        )


def test_site_and_task_are_not_written_into_an_approximate_nexus_field(tmp_path):
    """
    The three fields NeXus has no home for stay out of its groups.

    ``NXuser/affiliation`` is who the *operator* belongs to, not where the
    microscope is, so writing ``site`` there would be a confidently wrong
    claim in a standard field — worse, by this project's standing rule,
    than an honest absence. Both still round-trip through the JSON blob,
    which is what that blob is for.
    """
    session = Session(
        tmp_path / "session",
        site="SuperSTEM",
        task="ADF survey",
        operator="M. Sarahan",
    )
    recording = session.record([make_frame()], label="survey")

    with h5py.File(recording.path, "r") as handle:
        assert "affiliation" not in handle[layout.USER_GROUP]
        assert layout.INSTRUMENT_NAME not in handle

    context = read_session_context(recording.path)
    assert context["site"] == "SuperSTEM"
    assert context["task"] == "ADF survey"


def test_context_read_back_prefers_the_nexus_groups_for_what_they_hold(tmp_path):
    """
    A file carries its own context, readable without the session directory.

    The reason the groups are written at all: a recording that travels
    away from its session folder must still say what it was. All four
    mapped fields come back from the file's own groups.
    """
    session = Session(
        tmp_path / "session",
        operator="M. Sarahan",
        instrument="Nion UltraSTEM 200",
        sample="Au on C",
        sample_area="hole 4",
    )
    recording = session.record([make_frame()], label="survey")

    context = read_session_context(recording.path)
    assert context["operator"] == "M. Sarahan"
    assert context["instrument"] == "Nion UltraSTEM 200"
    assert context["sample"] == "Au on C"
    assert context["sample_area"] == "hole 4"
