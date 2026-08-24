"""
Rescuing data that was acquired without a session, one way or both.

Acquiring with no session attached is a legitimate choice - looking at a
Ronchigram to decide whether it is worth keeping should not litter a
session with files - and it used to be irreversible: the frames went
into a thumbnail and were released. These are the two routes back.

* **Save as**, one signal at a time, through the browser.
  :func:`~miainwoodpecker.dashboard.saving.nexus_bytes` renders the
  signal as a real NeXus file in memory, which is what makes it
  something the operator's own machine can save even when the notebook
  is running on the microscope's.
* **Write everything here**, into a directory chosen at the end of a
  shift. What comes out is indistinguishable from data acquired into
  that session in the first place - same naming, same numbering, one
  file per signal - and the frames stop being held once they are on
  disk.

The properties worth pinning, because each of them is a way for an
operator to lose data quietly:

* a saved signal is *readable*, not merely written;
* the acquisition's own start time is in the filename, not the save's,
  because a directory in which every name claims 18:00 has thrown away
  the only ordering the data had;
* an item's signals share a sequence number, so a HAADF and the spectrum
  image taken with it stay adjacent;
* saving releases the frames, which is the whole reason a flush is worth
  offering;
* and a failure is per signal rather than per shift - a disk that fills
  half way through must not lose the half that would have fitted.

No devices here. What is being tested is what happens to frames after
the instrument has finished with them, so the entries are built by hand:
a fake device would only produce the same arrays more slowly.
"""

import datetime

import numpy as np
import pytest

from miainwoodpecker.dashboard.saving import (
    SaveError,
    SaveJob,
    download_name,
    nexus_bytes,
    save_pending,
)
from miainwoodpecker.dashboard.session_log import (
    SessionLog,
    SessionLogEntry,
    describe_dataset,
)
from miainwoodpecker.devices.interface import Frame
from miainwoodpecker.storage.nexus import read_frames
from miainwoodpecker.storage.session import Session, read_session_context

_ACQUIRED_AT = datetime.datetime(2026, 8, 10, 14, 25, 30, tzinfo=datetime.UTC)
_FRAMES_PER_SIGNAL = 2
_TWO_SIGNALS = 2
_DEADLINE_S = 10.0


def _frame(value: float, name: str) -> Frame:
    """
    Return a small frame tagged with the detector that produced it.

    Parameters
    ----------
    value : float
        Fills the array, so a reader can tell one frame from another.
    name : str
        The detector's name.

    Returns
    -------
    Frame
        The frame.
    """
    return Frame(
        data=np.full((4, 4), value, dtype=np.float32),
        timestamp=_ACQUIRED_AT,
        metadata={"device_id": "scan-unit", "channel_name": name},
    )


def _entry(*names: str, label: str = "scan", index: int = 1) -> SessionLogEntry:
    """
    Build one in-memory log entry with a signal per name.

    Parameters
    ----------
    *names : str
        The signals the acquisition produced.
    label : str
        What the acquisition was called.
    index : int
        Its position in the log.

    Returns
    -------
    SessionLogEntry
        The entry, with every signal held in memory.
    """
    return SessionLogEntry(
        index=index,
        label=label,
        reason="a test acquisition",
        targets=("scanner",),
        holder="dashboard-tests",
        started_at=_ACQUIRED_AT,
        duration_s=0.5,
        datasets=tuple(
            describe_dataset(
                name,
                [
                    _frame(float(order * 10 + step), name)
                    for step in range(_FRAMES_PER_SIGNAL)
                ],
            )
            for order, name in enumerate(names)
        ),
    )


def _logged(*entries: SessionLogEntry) -> SessionLog:
    """
    Return a log holding these entries, numbered as the log numbers them.

    Parameters
    ----------
    *entries : SessionLogEntry
        The acquisitions to record.

    Returns
    -------
    SessionLog
        The log.
    """
    log = SessionLog()
    for entry in entries:
        log.append(entry)
    return log


def test_a_saved_signal_is_a_readable_nexus_file_of_its_own_frames():
    """Save as produces the file, not a promise of one."""
    entry = _entry("HAADF")
    (dataset,) = entry.datasets
    payload = nexus_bytes(entry, dataset)
    assert payload.startswith(b"\x89HDF\r\n\x1a\n")


def test_a_saved_signal_reads_back_frame_for_frame(tmp_path):
    """
    The bytes are a file HyperSpy or a NeXus viewer will open.

    Written out here and read with this project's own reader, which is
    the same code path any of them takes to the frame stack.
    """
    entry = _entry("HAADF")
    (dataset,) = entry.datasets
    target = tmp_path / download_name(entry, dataset)
    target.write_bytes(nexus_bytes(entry, dataset))
    stack = read_frames(target)
    assert stack.data.shape == (_FRAMES_PER_SIGNAL, 4, 4)
    assert stack.data[0][0][0] == pytest.approx(0.0)
    assert stack.data[1][0][0] == pytest.approx(1.0)


def test_a_download_is_named_as_the_session_would_have_named_it():
    """
    One naming rule, so both routes out produce files that sort together.

    The stamp is the acquisition's, not now's, and the number is the
    entry's place in the shift.
    """
    entry = _entry("HAADF", index=7)
    (dataset,) = entry.datasets
    assert download_name(entry, dataset) == "0007-scan-haadf-20260810T142530Z.nxs"


def test_saving_a_signal_that_is_already_on_disk_is_refused():
    """
    A Save button on stored data would write a second copy and mean nothing.

    Refused with a sentence naming where the data already is, rather
    than silently producing a duplicate the operator then has to
    reconcile.
    """
    entry = _entry("HAADF")
    (dataset,) = entry.datasets
    stored = describe_dataset(
        "HAADF",
        list(dataset.frames),
        path="0001-scan-haadf-20260810T142530Z.nxs",
    )
    with pytest.raises(SaveError, match="already stored"):
        nexus_bytes(entry, stored)


def test_writing_everything_puts_each_signal_in_its_own_file(tmp_path):
    """An item's signals share a number and differ only in the signal's name."""
    log = _logged(_entry("HAADF", "MAADF"))
    report = save_pending(log, tmp_path / "rescued")
    assert report.complete
    assert [signal.name for signal in report.saved] == ["HAADF", "MAADF"]
    names = sorted(path.name for path in (tmp_path / "rescued").glob("*.nxs"))
    assert names == [
        "0001-scan-haadf-20260810T142530Z.nxs",
        "0001-scan-maadf-20260810T142530Z.nxs",
    ]


def test_writing_everything_keeps_the_acquisition_time_not_the_save_time(tmp_path):
    """
    A flush at the end of a shift is saving work from all afternoon.

    Naming every file after the moment the operator pressed the button
    would throw away the only ordering the data had.
    """
    log = _logged(_entry("HAADF"))
    save_pending(log, tmp_path / "rescued")
    session = Session(tmp_path / "rescued")
    (recording,) = session.recordings()
    assert recording.started_at == _ACQUIRED_AT


def test_writing_everything_releases_the_frames_it_wrote(tmp_path):
    """
    The point of the flush: a shift's held data stops costing kernel memory.

    And the entry now points at the file, so the panel stops offering to
    save what is already saved.
    """
    log = _logged(_entry("HAADF", "MAADF"))
    save_pending(log, tmp_path / "rescued")
    (entry,) = log.entries
    assert entry.pending == ()
    for dataset in entry.datasets:
        assert dataset.path is not None
        assert dataset.frames == ()
        assert not dataset.in_memory
    assert log.pending == ()


def test_signals_already_on_disk_are_not_written_again(tmp_path):
    """
    A second flush is a no-op, not a second copy of the shift.

    An operator who presses the button twice - or who saved one signal
    through the browser and then flushed - must not end up with
    duplicates.
    """
    log = _logged(_entry("HAADF", "MAADF"))
    first = save_pending(log, tmp_path / "rescued")
    second = save_pending(log, tmp_path / "rescued")
    assert len(first.saved) == _TWO_SIGNALS
    assert second.saved == ()
    assert second.failed == ()
    assert len(list((tmp_path / "rescued").glob("*.nxs"))) == _TWO_SIGNALS


def test_a_flush_carries_the_session_context_the_directory_already_has(tmp_path):
    """
    Rescued data is not second-class data.

    Pointed at a directory that has been used as a session, the flush
    picks up its operator and sample rather than writing files that
    claim nothing.
    """
    root = tmp_path / "shift"
    Session(root, operator="M. Sarahan", sample="Au on C")
    log = _logged(_entry("HAADF"))
    report = save_pending(log, root)
    (signal,) = report.saved
    context = read_session_context(signal.path)
    assert context["operator"] == "M. Sarahan"
    assert context["sample"] == "Au on C"
    assert context["dataset"] == "HAADF"


def test_each_acquisition_keeps_its_own_number_across_a_flush(tmp_path):
    """Two items, two numbers, in the order they were acquired."""
    log = _logged(_entry("HAADF"), _entry("MAADF", label="camera"))
    report = save_pending(log, tmp_path / "rescued")
    assert [signal.entry_index for signal in report.saved] == [1, 2]
    session = Session(tmp_path / "rescued")
    assert [record.index for record in session.recordings()] == [1, 2]


def test_a_refusal_leaves_nothing_to_save_and_says_so(tmp_path):
    """An entry that never acquired anything is skipped, not failed."""
    log = _logged(
        SessionLogEntry(
            index=0,
            label="scan",
            reason="a lease somebody else held",
            targets=("scanner",),
            holder="",
            started_at=_ACQUIRED_AT,
            duration_s=0.0,
            error="DeviceBusyError: leased by the viewer",
        ),
    )
    report = save_pending(log, tmp_path / "rescued")
    assert report.saved == ()
    assert report.failed == ()
    assert "Nothing was held in memory" in report.summary()


def test_a_destination_that_cannot_be_a_directory_is_refused_outright(tmp_path):
    """
    There is no partial outcome to report, so this raises rather than returns.

    An empty report would read as "there was nothing to save", which is
    the opposite of what happened.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this is a file", encoding="utf-8")
    log = _logged(_entry("HAADF"))
    with pytest.raises((SaveError, NotADirectoryError)):
        save_pending(log, blocker)
    # Nothing was marked stored, so the data is still there to try again.
    assert len(log.pending) == 1


def test_one_unwritable_signal_does_not_take_its_item_down_with_it(tmp_path):
    """
    A survey and a spectrum are one item and two files, and two fates.

    The spectrum here is a projected readout with no energy calibration,
    which is a real state a camera can produce and which cannot be
    stored as a spectrum at all - there is no axis to write. Losing the
    HAADF acquired in the same second because of it would be the flush
    doing more damage than the failure.
    """
    uncalibrated = Frame(
        data=np.arange(8, dtype=np.float32),
        timestamp=_ACQUIRED_AT,
        metadata={"device_id": "eels_camera", "readout": "projected"},
    )
    entry = SessionLogEntry(
        index=1,
        label="si",
        reason="a survey and a spectrum",
        targets=("scanner", "eels_camera"),
        holder="dashboard-tests",
        started_at=_ACQUIRED_AT,
        duration_s=1.0,
        datasets=(
            describe_dataset("survey", [_frame(1.0, "HAADF")]),
            describe_dataset("spectrum", [uncalibrated]),
        ),
    )
    log = _logged(entry)

    report = save_pending(log, tmp_path / "rescued")

    assert [signal.name for signal in report.saved] == ["survey"]
    assert [signal.name for signal in report.failed] == ["spectrum"]
    # Same item, so the survey still carries the acquisition's number.
    assert report.saved[0].path.name == "0001-si-survey-20260810T142530Z.nxs"
    assert read_frames(report.saved[0].path).data.shape == (1, 4, 4)
    # And the spectrum is still held, to try somewhere else.
    assert [dataset.name for _, dataset in log.pending] == ["spectrum"]


def test_one_item_failing_does_not_stop_the_rest(tmp_path, monkeypatch):
    """
    A disk that fills half way through must not lose the half that fitted.

    The second item is made to fail at the writer; the first is already
    on disk and stays there, and the failure names the signals that did
    not make it so they can be tried somewhere else.
    """
    log = _logged(_entry("HAADF"), _entry("MAADF", label="camera"))
    real = Session.record_datasets
    calls = {"n": 0}

    def flaky(
        session: Session,
        datasets: object,
        **kwargs: object,
    ) -> dict[str, object]:
        """
        Stand in for the writer, failing the second call.

        Parameters
        ----------
        session : Session
            The session the method was called on.
        datasets : object
            The signal-and-frame pairs, passed straight through.
        **kwargs : object
            The rest of the call, passed straight through.

        Returns
        -------
        dict[str, object]
            Whatever the real method returned.

        Raises
        ------
        OSError
            On the second call, standing in for a disk that filled up.
        """
        calls["n"] += 1
        if calls["n"] == _TWO_SIGNALS:
            message = "No space left on device"
            raise OSError(message)
        return real(session, datasets, **kwargs)

    monkeypatch.setattr(Session, "record_datasets", flaky)
    report = save_pending(log, tmp_path / "rescued")
    assert not report.complete
    assert [signal.name for signal in report.saved] == ["HAADF"]
    assert [signal.name for signal in report.failed] == ["MAADF"]
    assert "No space left" in report.failed[0].reason
    # Still held, so a second attempt at another directory finds it.
    assert [dataset.name for _, dataset in log.pending] == ["MAADF"]


def test_the_save_runs_off_the_display_thread(tmp_path):
    """
    Writing a shift's data is slow and must not freeze the tiles.

    The same shape every other slow thing in this project has: a daemon
    thread, a result read under a lock, and a caller that polls.
    """
    log = _logged(_entry("HAADF"))
    job = SaveJob(log, tmp_path / "rescued")
    job.start()
    assert job.join(_DEADLINE_S)
    assert job.error is None
    assert job.result is not None
    assert job.result.complete
    assert log.pending == ()
