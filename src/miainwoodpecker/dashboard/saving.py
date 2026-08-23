"""
Getting data out of the kernel and onto a disk somebody chose.

Acquiring with no session attached is a real choice rather than a
degraded one - looking at a Ronchigram to decide whether it is worth
keeping should not litter a session with files - but it used to be a
one-way one. The frames went into the log entry's thumbnail and were
released, so the shot that turned out to matter could not be rescued.
This module is the way back: every signal an entry still holds in memory
can be written, one at a time through the browser or all at once into a
directory.

Two routes, because operators want two different things
--------------------------------------------------------
**Save as**, one signal at a time. :func:`nexus_bytes` renders a signal
as a complete NeXus file in memory and the notebook hands it to
``mo.download``, which is the browser's own Save-as dialog. This is what
you want when the browser is not on the instrument's machine - the file
arrives wherever that browser saves things, with no share to configure.
It is passed to ``mo.download`` as a **callable**, so the bytes are
produced when somebody clicks rather than on every tick of the display
timer - which the dashboard will run at ten a second if the operator
asks it to.

**Write everything here**, at the end of a shift or before shutting the
kernel down. :func:`save_pending` takes a directory, opens it as a
:class:`~miainwoodpecker.storage.session.Session`, and writes every
unsaved signal into it with the naming and the numbering a session
gives anything else. That is the difference between the two routes worth
knowing: the browser download is one loose file with no session context
in it, because there was no session; the directory flush produces files
that are indistinguishable from ones acquired into that session in the
first place, including its ``session.json`` context and its ``NXsample``
and ``NXuser`` groups. If the directory has been used as a session
before, that context is picked up rather than blanked.

What is written, and what stops being held
------------------------------------------
One file per signal, exactly as
:meth:`~miainwoodpecker.storage.session.Session.record_datasets` writes
an acquisition made *into* a session - so an item's HAADF and its
spectrum image land in two files sharing a sequence number, and either
opens on its own in HyperSpy or a NeXus viewer.

The files carry the acquisition's own start time, not the time of the
save. An operator flushing at 18:00 is saving work from all afternoon,
and a directory in which every filename claimed 18:00 would have thrown
away the only ordering the data had.

A signal that is written is marked stored in the log
(:meth:`~miainwoodpecker.dashboard.session_log.SessionLog.mark_stored`),
which releases its frames. That is the point at which a shift's worth of
held data stops costing kernel memory, and it is why the flush is worth
offering rather than leaving to "copy it out of the notebook somehow".

Failures are per signal, not per shift
---------------------------------------
A disk that fills up half way through a flush must not lose the half
that would have fitted, and one unwritable signal must not take the item
it belongs to down with it - a projected frame that carries no energy
calibration cannot be stored as a spectrum, and that is no reason to
lose the HAADF acquired beside it. So each signal is written on its own,
into a shared item so the numbering still says which acquisition it came
from, and :func:`save_pending` records what each one did and carries on.
It returns a :class:`SaveReport` naming both halves. Nothing that failed
is marked stored, so it is still in memory and a second attempt at
another directory will find it.

Nothing here imports marimo, for the reason the package docstring gives.
"""

from __future__ import annotations

import dataclasses
import tempfile
import typing
from pathlib import Path

from miainwoodpecker.acquisition.sequence import record
from miainwoodpecker.jobs import BackgroundJob
from miainwoodpecker.storage.session import Session, recording_filename

if typing.TYPE_CHECKING:
    import os

    from miainwoodpecker.dashboard.session_log import (
        LoggedDataset,
        SessionLog,
        SessionLogEntry,
    )

NEXUS_MIMETYPE = "application/x-hdf5"
"""
The mimetype a downloaded recording is offered under.

Registered for HDF5, which is what a NeXus file is; there is no
NeXus-specific type. It matters only in that a browser given no type at
all may decide a ``.nxs`` file is text and mangle it.
"""


class SaveError(RuntimeError):
    """Raised when data held in memory could not be written, with the reason."""


@dataclasses.dataclass(frozen=True)
class SavedSignal:
    """
    One signal that was written out of memory, and where it went.

    Attributes
    ----------
    entry_index : int
        The log entry the signal belongs to.
    name : str
        The signal's name within that entry.
    path : Path
        The file it was written to.
    """

    entry_index: int
    name: str
    path: Path


@dataclasses.dataclass(frozen=True)
class FailedSignal:
    """
    One signal that could not be written, and why.

    Attributes
    ----------
    entry_index : int
        The log entry the signal belongs to.
    name : str
        The signal's name within that entry.
    reason : str
        What went wrong, as a sentence an operator can act on. The data
        is still in memory and still in the log.
    """

    entry_index: int
    name: str
    reason: str


@dataclasses.dataclass(frozen=True)
class SaveReport:
    """
    What one "write everything here" actually did.

    Attributes
    ----------
    root : Path
        The session directory written into.
    saved : tuple[SavedSignal, ...]
        The signals that were written, in acquisition order.
    failed : tuple[FailedSignal, ...]
        The signals that were not, each with its reason. Their data is
        untouched and still held.
    """

    root: Path
    saved: tuple[SavedSignal, ...] = ()
    failed: tuple[FailedSignal, ...] = ()

    @property
    def complete(self) -> bool:
        """Return whether everything that was pending is now on disk."""
        return not self.failed

    def summary(self) -> str:
        """
        Describe the outcome in one sentence, for a status line.

        Returns
        -------
        str
            What was written and what was not.
        """
        if not self.saved and not self.failed:
            return f"Nothing was held in memory; {self.root} is unchanged."
        written = f"Wrote {len(self.saved)} signal(s) to {self.root}"
        if not self.failed:
            return f"{written}."
        return f"{written}; {len(self.failed)} could not be written."


def download_name(entry: SessionLogEntry, dataset: LoggedDataset) -> str:
    """
    Return the filename one signal should be downloaded under.

    The same name a session would have given the file, built by the same
    function (:func:`~miainwoodpecker.storage.session.recording_filename`)
    - so a signal saved through the browser and one written into a
    session directory are named alike, and an operator who does both ends
    up with files that sort together rather than with two conventions.
    The log entry's own position stands in for the session's sequence
    number, which is what it is: this acquisition's place in the shift.

    Parameters
    ----------
    entry : SessionLogEntry
        The acquisition the signal came from.
    dataset : LoggedDataset
        The signal.

    Returns
    -------
    str
        The filename, extension included.
    """
    return recording_filename(
        f"{entry.label}-{dataset.name}",
        index=entry.index,
        started_at=entry.started_at,
    )


def nexus_bytes(entry: SessionLogEntry, dataset: LoggedDataset) -> bytes:
    """
    Render one in-memory signal as a complete NeXus file, in memory.

    Written to a temporary file and read back rather than built in a
    buffer, because HDF5 writes through a file driver and the writers
    this project uses take a path. That is one extra copy of data that is
    already in the kernel, on a path the operator triggered by clicking
    Save - not on the acquisition path, and not on the display tick,
    which is why ``mo.download`` is given this as a callable to run on
    demand.

    Parameters
    ----------
    entry : SessionLogEntry
        The acquisition the signal came from; names the file and its
        ``/entry/title``.
    dataset : LoggedDataset
        The signal to render. Must still be held in memory.

    Returns
    -------
    bytes
        A complete NeXus HDF5 file.

    Raises
    ------
    SaveError
        If the signal is already on disk, or holds no frames. Both would
        otherwise produce a file that is empty or a second copy of one
        that exists, and neither is what a Save button meant.
    """
    if not dataset.in_memory:
        message = (
            f"{dataset.name} of entry {entry.index} is already stored at "
            f"{dataset.path}; there is nothing held in memory to save"
        )
        raise SaveError(message)
    if not dataset.frames:
        message = (
            f"{dataset.name} of entry {entry.index} holds no frames, so "
            f"there is nothing to write"
        )
        raise SaveError(message)
    name = download_name(entry, dataset)
    with tempfile.TemporaryDirectory(prefix="miainwoodpecker-save-") as directory:
        target = Path(directory) / name
        record(
            dataset.frames,
            target,
            title=f"{entry.label} {dataset.name}",
        )
        return target.read_bytes()


def save_pending(
    log: SessionLog,
    destination: os.PathLike[str] | str,
) -> SaveReport:
    """
    Write every signal the log still holds in memory into one directory.

    The end-of-shift action. The directory is opened as a
    :class:`~miainwoodpecker.storage.session.Session`, so what comes out
    is numbered, named, and carries session context exactly as data
    acquired into that session would - including any context already
    recorded there by an earlier run, which is reused rather than
    blanked.

    One acquisition's signals are written as one item, sharing a sequence
    number, so a HAADF and the spectrum image taken with it stay
    adjacent in the directory.

    Parameters
    ----------
    log : SessionLog
        The log to drain. Signals that are written are marked stored in
        it, which releases their frames; everything else is untouched.
    destination : os.PathLike[str] | str
        The session directory to write into. Created if it does not
        exist.

    Returns
    -------
    SaveReport
        What was written and what was not.

    Raises
    ------
    SaveError
        If the destination cannot be opened as a session directory at
        all - there is no partial outcome to report in that case, and
        returning an empty report would read as "there was nothing to
        save".
    """
    root = Path(destination)
    try:
        session = Session(root)
    except OSError as error:
        message = f"cannot write to {root}: {error}"
        raise SaveError(message) from error
    saved: list[SavedSignal] = []
    failed: list[FailedSignal] = []
    for entry in log.entries:
        if not entry.pending:
            continue
        written, missed = _save_entry(session, log, entry)
        saved.extend(written)
        failed.extend(missed)
    return SaveReport(root=session.root, saved=tuple(saved), failed=tuple(failed))


def _save_entry(
    session: Session,
    log: SessionLog,
    entry: SessionLogEntry,
) -> tuple[list[SavedSignal], list[FailedSignal]]:
    """
    Write one acquisition's unsaved signals, recording how each went.

    Parameters
    ----------
    session : Session
        Where the files go.
    log : SessionLog
        Marked as each signal lands, which releases its frames.
    entry : SessionLogEntry
        The acquisition being written.

    Returns
    -------
    tuple[list[SavedSignal], list[FailedSignal]]
        What was written and what was not, in the entry's own order.
    """
    saved: list[SavedSignal] = []
    failed: list[FailedSignal] = []
    # One item, several calls: the signals share the item's number and
    # timestamp, and each is attempted on its own. Written one at a time
    # rather than as one interleaved stream because these frames are
    # already in memory - there is no acquisition to keep up with, and
    # nothing to gain by letting one unwritable signal take its siblings
    # down with it.
    item = session.open_item(entry.label, started_at=entry.started_at)
    for dataset in entry.pending:
        try:
            recordings = session.record_datasets(
                ((dataset.name, frame) for frame in dataset.frames),
                item=item,
            )
        except (OSError, ValueError) as error:
            # The reserved file is left where it is, part-written or
            # empty. Deleting it would be this code destroying data on a
            # path it has just discovered it does not understand, and
            # the session already describes an unreadable recording
            # honestly. The frames stay in memory, so another directory
            # can be tried.
            failed.append(
                FailedSignal(
                    entry_index=entry.index,
                    name=dataset.name,
                    reason=f"{type(error).__name__}: {error}",
                ),
            )
            continue
        for name, recording in recordings.items():
            log.mark_stored(entry.index, name, str(recording.path))
            saved.append(
                SavedSignal(entry_index=entry.index, name=name, path=recording.path),
            )
    return (saved, failed)


class SaveJob(BackgroundJob):
    """
    Run one :func:`save_pending` on a worker thread.

    Writing a shift's held data is the same problem
    :class:`~miainwoodpecker.storage.session.RecordingJob` exists for and
    is generally worse: it is every unsaved acquisition at once, gzipped,
    quite possibly onto a network mount. A notebook cell that did it
    inline would stop refreshing its tiles for the duration - at exactly
    the moment somebody is trying to leave - so it goes where every other
    slow thing in this project goes, and the display learns how it went
    from the same poll that draws the tiles.

    Parameters
    ----------
    log : SessionLog
        The log to drain.
    destination : os.PathLike[str] | str
        The session directory to write into.
    """

    def __init__(
        self,
        log: SessionLog,
        destination: os.PathLike[str] | str,
    ) -> None:
        super().__init__("save-pending")
        self._log = log
        self._destination = destination

    @property
    def result(self) -> SaveReport | None:
        """Return what the save did, or None until it has finished."""
        with self._lock:
            return typing.cast("SaveReport | None", self._raw_result)

    def _work(self) -> SaveReport:
        """
        Write on the worker thread.

        Returns
        -------
        SaveReport
            What was written and what was not.
        """
        return save_pending(self._log, self._destination)
