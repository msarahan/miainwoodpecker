"""
The record of what this dashboard session actually acquired.

**Why a log rather than an output cell per acquisition.** A notebook's
natural instinct is to put each result under the cell that produced it,
and marimo forbids exactly that: a cell cannot write new cells, because
the dependency graph is the notebook and a program that rewrites its own
graph while running has no defined order. The log is the shape that is
left, and it turns out to be the better one anyway - a shift's
acquisitions in the order they happened, in one place, rather than
scattered through a document whose reading order is not its execution
order.

**An entry is an item, and an item is several signals.** The unit an
operator thinks in is not one array. It is "that spectrum image" - which
in practice means a HAADF survey with the mapped area marked on it, the
spectrum image itself, and the HAADF taken immediately afterwards in the
same place, because the interesting question about an SI is usually how
much the specimen moved and how much of it the beam removed while the
map ran. Or it is one scan pass read out on HAADF and BF at once. Or a
survey with a beam marker beside the spectrum taken at that point. So a
:class:`SessionLogEntry` carries a tuple of :class:`LoggedDataset`, and
nothing here decides how many there are or what they are called: the
acquisition names its own signals (see
:mod:`miainwoodpecker.dashboard.acquisition`) and the log records what
it named.

**Each signal is its own file, and the entry says where.** Separate
files rather than one combined NeXus entry, which the format would allow
- see :mod:`miainwoodpecker.storage.session` for the argument. What
matters here is the consequence: an entry does not have *a* path, it has
one per signal, and :attr:`LoggedDataset.path` is where that signal went.

**Data with nowhere to go is kept, not dropped.** An acquisition made
with no session attached has no file, and that is a legitimate choice -
looking at a Ronchigram to decide whether it is worth keeping should not
litter a session with files. But "no file" used to mean the frames were
released the moment the log entry was built, so a shot that turned out
to matter could not be rescued. Now an entry with no path keeps its
frames on :attr:`LoggedDataset.frames`, so the notebook can offer to
save it - one signal at a time, or a whole session's worth into a
directory at once (:mod:`miainwoodpecker.dashboard.saving`). The cost is
honest and bounded: it is the data the acquisition produced, held until
it is written or the kernel stops, and writing it releases it.

**Thumbnails, not frames, for anything already on disk.** A signal
written to a file keeps a decimated PNG and its first frame's metadata,
never the pixels - see
:data:`~miainwoodpecker.dashboard.images.THUMBNAIL_MAX_EDGE` for the
arithmetic. There is no second copy of what a file already holds.

**Append-only, and that is a rule rather than an omission.** There is no
``remove``, no ``clear`` and no way to edit an entry's account of what
happened. An acquisition either happened or it did not, and a panel that
could delete the record of one is a panel that can make a session claim
something false. The refusals are kept for the same reason: "the scanner
was leased by the viewer at 14:32" is part of what happened, and a log
that silently dropped it would leave a gap an operator later reads as a
quiet minute.

:meth:`SessionLog.mark_stored` is the one exception and is deliberately
narrow. It moves a signal from "in memory" to "at this path" and does
nothing else - it cannot change what was acquired, when, by whom, or
whether it succeeded, and it only ever runs in that direction, because
data that has been written to a file has not become unwritten. Where the
bytes live is not part of the account of the shift; it is a fact that
changes when somebody saves them, and a log that could not record the
change would go on telling an operator their data was unsaved after they
had saved it.

**Thread-safe, because the writer is not the reader.** Entries are
appended from the acquisition worker
(:class:`~miainwoodpecker.dashboard.acquisition.AcquisitionJob`), which
is where the lease is taken; they are read from whatever thread the
notebook's display cell runs on, and marked as stored from the save
worker. One lock, the same contract
:class:`~miainwoodpecker.jobs.BackgroundJob` states for its own state.
"""

from __future__ import annotations

import dataclasses
import threading
import typing

from miainwoodpecker.dashboard.images import (
    THUMBNAIL_MAX_EDGE,
    is_image,
    png_data_uri,
)

if typing.TYPE_CHECKING:
    import datetime
    from collections.abc import Mapping, Sequence

    from miainwoodpecker.devices.interface import Frame

METADATA_HIGHLIGHTS = (
    "device_id",
    "channel_name",
    "scan_pass_id",
    "simultaneous_channels",
    "fov_nm",
    "pixel_time_us",
    "rotation_rad",
    "exposure_ms",
    "binning",
    "readout",
    "projected_by",
    "defocus_nm",
    "high_tension_v",
    "beam_current_a",
)
"""
The metadata keys a log entry shows without being asked.

A subset, in reading order, of the vocabulary
:class:`~miainwoodpecker.devices.interface.Frame` defines: what produced
the frame, what geometry or exposure it was taken with, and what the
column was doing at the time. Everything else the vendor reported is
still on the signal's :attr:`LoggedDataset.metadata` - this tuple decides
what is *promoted*, not what is kept.

Absent keys are omitted rather than shown blank, which is the rule the
frame vocabulary itself states: a missing key means the instrument did
not report it, and a zero would claim it reported zero.
"""


@dataclasses.dataclass(frozen=True)
class LoggedDataset:
    """
    One signal of one acquisition, and where its data is.

    The thing an operator saves, opens, or sends to somebody: a HAADF
    image, a spectrum image, a survey. An entry has as many of these as
    the acquisition produced, and each has its own file.

    Attributes
    ----------
    name : str
        What this signal is, as the acquisition named it - a detector
        (``"HAADF"``), or a step of a recipe (``"survey"``, ``"SI"``,
        ``"followup"``). Slugified into the filename when it is written,
        and the caption in the log.
    frame_count : int
        Frames this signal received. Not the length of :attr:`frames`,
        which is empty for a signal already on disk.
    shape : tuple[int, ...]
        The first frame's shape, empty when nothing arrived.
    dtype : str
        The first frame's dtype, empty when nothing arrived.
    metadata : Mapping[str, object]
        The first frame's metadata, whole. A series is one signal, and
        the frames of a series differ in the fields a *series* varies - a
        focal series' defocus, an index - so the first frame's values are
        the signal's provenance and the file is the per-frame record.
    thumbnail : str
        A ``data:`` URI of the first frame, decimated. Empty for a signal
        that is not an image, which a projected spectrometer readout is
        not: a one-pixel-high strip is a picture of nothing, and a
        spectrum wants a plot.
    path : str | None
        The file this signal was written to, or None when it is held in
        memory only. The difference is a field rather than an inference
        because it decides what the log can offer: a path is a place to
        point at, and None is data that will be gone when the kernel
        stops unless somebody saves it.
    frames : tuple[Frame, ...]
        The frames themselves, kept **only** while :attr:`path` is None,
        because then nothing else has them. Emptied when the signal is
        written - see :meth:`SessionLog.mark_stored`.
    """

    name: str
    frame_count: int = 0
    shape: tuple[int, ...] = ()
    dtype: str = ""
    metadata: Mapping[str, object] = dataclasses.field(default_factory=dict)
    thumbnail: str = ""
    path: str | None = None
    frames: tuple[Frame, ...] = ()

    @property
    def in_memory(self) -> bool:
        """Return whether this signal's data exists only in this kernel."""
        return self.path is None


@dataclasses.dataclass(frozen=True)
class SessionLogEntry:
    """
    One acquisition, as the log records it.

    Attributes
    ----------
    index : int
        Position in the log, from 1. Assigned by :meth:`SessionLog.append`
        rather than by the caller, so two acquisitions racing to finish
        cannot claim the same number.
    label : str
        What was acquired, in the same words the session filename uses.
    reason : str
        What the lease was taken for, as it was shown to every other
        client of the instrument while it was held.
    targets : tuple[str, ...]
        The targets the lease held.
    holder : str
        Who the broker recorded as holding it - this client's identity,
        assigned from the connection. Empty for an acquisition that
        never got a lease.
    started_at : datetime.datetime
        When the acquisition began, timezone-aware, as
        :class:`~miainwoodpecker.devices.interface.Frame` timestamps are.
    duration_s : float
        How long it took, measured on a monotonic clock so a wall clock
        stepping mid-acquisition cannot produce a negative duration.
    datasets : tuple[LoggedDataset, ...]
        The signals this acquisition produced, in the order they first
        appeared - which for a multi-detector scan is the order the
        detectors were requested in. Empty for a refusal, and for an
        acquisition that produced nothing.
    error : str | None
        Why the acquisition did not happen, or None if it did. A refused
        lease lands here - the broker's own sentence, naming the holder
        and their reason.
    """

    index: int
    label: str
    reason: str
    targets: tuple[str, ...]
    holder: str
    started_at: datetime.datetime
    duration_s: float
    datasets: tuple[LoggedDataset, ...] = ()
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether this acquisition produced data rather than a refusal."""
        return self.error is None

    @property
    def frame_count(self) -> int:
        """Return how many frames this acquisition produced across every signal."""
        return sum(dataset.frame_count for dataset in self.datasets)

    @property
    def pending(self) -> tuple[LoggedDataset, ...]:
        """
        Return the signals still held in memory with no file behind them.

        What a "save everything" action has to write, and what a shift's
        worth of unsaved data amounts to - so the notebook can say how
        much is at stake before the kernel stops.

        Returns
        -------
        tuple[LoggedDataset, ...]
            The unwritten signals, in the entry's own order.
        """
        return tuple(dataset for dataset in self.datasets if dataset.in_memory)

    def dataset(self, name: str) -> LoggedDataset | None:
        """
        Return one signal of this acquisition by name.

        Parameters
        ----------
        name : str
            The signal's name, as the acquisition gave it.

        Returns
        -------
        LoggedDataset | None
            The signal, or None if this acquisition has none by that name.
        """
        return next(
            (dataset for dataset in self.datasets if dataset.name == name),
            None,
        )


def highlights(dataset: LoggedDataset) -> dict[str, object]:
    """
    Pick out the metadata worth showing beside a signal's thumbnail.

    Parameters
    ----------
    dataset : LoggedDataset
        The signal to summarise.

    Returns
    -------
    dict[str, object]
        The subset of :data:`METADATA_HIGHLIGHTS` this frame actually
        reported, in that tuple's order. Keys the instrument did not
        report are absent, not blank.
    """
    return {
        key: dataset.metadata[key]
        for key in METADATA_HIGHLIGHTS
        if key in dataset.metadata
    }


def describe_frames(frames: Sequence[Frame]) -> tuple[tuple[int, ...], str, str]:
    """
    Read a series' shape, dtype and thumbnail off its first frame.

    The first rather than the last, deliberately: it is the one frame a
    series is guaranteed to have if it has any, and for the parameter
    sweeps this project records - a focal series, an energy series - it
    is the step whose settings the operator chose before starting.

    Parameters
    ----------
    frames : Sequence[Frame]
        The acquired frames, possibly empty.

    Returns
    -------
    tuple[tuple[int, ...], str, str]
        Shape, dtype name, and a ``data:`` URI thumbnail. All three are
        empty for an empty series; the thumbnail alone is empty for a
        series of 1D frames, which a projected spectrometer readout
        produces and which no greyscale picture describes.
    """
    if not frames:
        return ((), "", "")
    first = frames[0]
    thumbnail = (
        png_data_uri(first.data, max_edge=THUMBNAIL_MAX_EDGE)
        if is_image(first.data)
        else ""
    )
    return (tuple(first.data.shape), str(first.data.dtype), thumbnail)


def describe_dataset(
    name: str,
    frames: Sequence[Frame],
    *,
    frame_count: int | None = None,
    path: str | None = None,
) -> LoggedDataset:
    """
    Summarise one signal for the log, keeping its frames if nothing else has them.

    The retention rule is the whole point and is decided here rather than
    at each call site: a signal with a ``path`` is on disk, so its frames
    are dropped; a signal without one exists only in this kernel, so they
    are kept and can still be saved.

    Parameters
    ----------
    name : str
        What this signal is called.
    frames : Sequence[Frame]
        The frames in hand. For a signal already streamed to a file this
        is just the first, retained for the thumbnail - which is why
        ``frame_count`` exists.
    frame_count : int | None
        How many frames the signal actually received. Defaults to the
        length of ``frames``, which is right only when they are all here.
    path : str | None
        Where the signal was written, or None for memory only.

    Returns
    -------
    LoggedDataset
        The signal as the log will hold it.
    """
    shape, dtype, thumbnail = describe_frames(frames)
    return LoggedDataset(
        name=name,
        frame_count=len(frames) if frame_count is None else frame_count,
        shape=shape,
        dtype=dtype,
        metadata=dict(frames[0].metadata) if frames else {},
        thumbnail=thumbnail,
        path=path,
        frames=() if path is not None else tuple(frames),
    )


class SessionLog:
    """
    Every acquisition this dashboard has attempted, in order, for keeps.

    See the module docstring for why it is append-only, why
    :meth:`mark_stored` is not an exception to that, and why it is
    locked.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[SessionLogEntry] = []

    def append(self, entry: SessionLogEntry) -> SessionLogEntry:
        """
        Add an entry, stamping it with its position in the log.

        The index is assigned here rather than taken from the entry
        because two acquisitions can finish in either order and neither
        knows how many came before it. The entry passed in is left
        untouched - :class:`SessionLogEntry` is frozen - and the stamped
        copy is what is stored and returned.

        Parameters
        ----------
        entry : SessionLogEntry
            The acquisition to record. Its ``index`` is ignored.

        Returns
        -------
        SessionLogEntry
            The entry as stored, with its index.
        """
        with self._lock:
            stamped = dataclasses.replace(entry, index=len(self._entries) + 1)
            self._entries.append(stamped)
            return stamped

    def mark_stored(self, index: int, name: str, path: str) -> SessionLogEntry | None:
        """
        Record that one in-memory signal has been written to a file.

        The only mutation besides :meth:`append`, and it says one thing:
        this signal's data is now at this path. It releases the frames as
        it does so, because the file has them and a second copy in kernel
        memory is what the save was for.

        Refuses to run backwards. A signal that already has a path is
        left alone rather than repointed: data that has been written has
        not become unwritten, and a log that accepted a second path would
        quietly disown the first file.

        Parameters
        ----------
        index : int
            The entry's position in the log, as :meth:`append` stamped it.
        name : str
            Which signal of that entry was written.
        path : str
            Where it went.

        Returns
        -------
        SessionLogEntry | None
            The updated entry, or None if the log has no such entry, no
            such signal in it, or that signal was already on disk.
        """
        with self._lock:
            if not 1 <= index <= len(self._entries):
                return None
            entry = self._entries[index - 1]
            updated = tuple(
                dataclasses.replace(dataset, path=path, frames=())
                if dataset.name == name and dataset.in_memory
                else dataset
                for dataset in entry.datasets
            )
            if updated == entry.datasets:
                return None
            stored = dataclasses.replace(entry, datasets=updated)
            self._entries[index - 1] = stored
            return stored

    @property
    def entries(self) -> tuple[SessionLogEntry, ...]:
        """
        Return every entry, oldest first.

        A tuple rather than the list itself, so a display cell holding
        the result cannot append to the log by accident, and so what it
        renders does not change underneath it while the worker thread
        adds another.

        Returns
        -------
        tuple[SessionLogEntry, ...]
            The log so far.
        """
        with self._lock:
            return tuple(self._entries)

    @property
    def latest(self) -> SessionLogEntry | None:
        """Return the most recent entry, or None if nothing has been logged."""
        with self._lock:
            return self._entries[-1] if self._entries else None

    @property
    def pending(self) -> tuple[tuple[SessionLogEntry, LoggedDataset], ...]:
        """
        Return every signal still held in memory, with the entry it belongs to.

        What "save everything acquired without a session" has to write,
        paired with the entry each signal came from, because saving it
        needs the entry's label and start time to name the file and its
        index to mark it stored afterwards.

        Returns
        -------
        tuple[tuple[SessionLogEntry, LoggedDataset], ...]
            Entry and signal, in acquisition order.
        """
        with self._lock:
            snapshot = tuple(self._entries)
        return tuple(
            (entry, dataset) for entry in snapshot for dataset in entry.pending
        )

    def __len__(self) -> int:
        """
        Return how many acquisitions have been logged.

        Returns
        -------
        int
            The entry count, refusals included.
        """
        with self._lock:
            return len(self._entries)
