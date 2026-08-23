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

**Append-only, and that is a rule rather than an omission.** There is no
``remove``, no ``clear`` and no way to edit an entry. An acquisition
either happened or it did not, and a panel that could delete the record
of one is a panel that can make a session claim something false. The
refusals are kept for the same reason: "the scanner was leased by the
viewer at 14:32" is part of what happened, and a log that silently
dropped it would leave a gap an operator later reads as a quiet minute.

**Thread-safe, because the writer is not the reader.** Entries are
appended from the acquisition worker
(:class:`~miainwoodpecker.dashboard.acquisition.AcquisitionJob`), which
is where the lease is taken; they are read from whatever thread the
notebook's display cell runs on. One lock, the same contract
:class:`~miainwoodpecker.jobs.BackgroundJob` states for its own state.

**Thumbnails, not frames.** Each entry keeps a decimated PNG and the
first frame's metadata, never the pixels - see
:data:`~miainwoodpecker.dashboard.images.THUMBNAIL_MAX_EDGE` for the
arithmetic. What was acquired is on disk if a session was attached, and
in the entry's ``recording_path``; the log's job is to say what happened,
not to be a second copy of the data.
"""

from __future__ import annotations

import dataclasses
import threading
import typing

from miainwoodpecker.dashboard.images import THUMBNAIL_MAX_EDGE, png_data_uri

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
still on the entry's :attr:`SessionLogEntry.metadata` - this tuple
decides what is *promoted*, not what is kept.

Absent keys are omitted rather than shown blank, which is the rule the
frame vocabulary itself states: a missing key means the instrument did
not report it, and a zero would claim it reported zero.
"""


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
    frame_count : int
        Frames acquired. Zero for a refusal, and for an acquisition that
        was asked for none.
    shape : tuple[int, ...]
        The first frame's shape, empty when nothing was acquired.
    dtype : str
        The first frame's dtype, empty when nothing was acquired.
    metadata : Mapping[str, object]
        The first frame's metadata, whole. A series is recorded as one
        entry, and the frames of a series differ in the fields a
        *series* varies - a focal series' defocus, an index - so the
        first frame's values are the entry's provenance and the file is
        the per-frame record.
    thumbnail : str
        A ``data:`` URI of the first frame, decimated. Empty for a
        refusal.
    recording_path : str | None
        Where the frames were written, or None when no session was
        attached and they were held in memory only. The difference
        matters enough to be a field rather than an inference: an entry
        with no path is an acquisition whose data is gone when the
        kernel stops.
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
    frame_count: int = 0
    shape: tuple[int, ...] = ()
    dtype: str = ""
    metadata: Mapping[str, object] = dataclasses.field(default_factory=dict)
    thumbnail: str = ""
    recording_path: str | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether this acquisition produced data rather than a refusal."""
        return self.error is None


def highlights(entry: SessionLogEntry) -> dict[str, object]:
    """
    Pick out the metadata worth showing beside a log entry's thumbnail.

    Parameters
    ----------
    entry : SessionLogEntry
        The entry to summarise.

    Returns
    -------
    dict[str, object]
        The subset of :data:`METADATA_HIGHLIGHTS` this frame actually
        reported, in that tuple's order. Keys the instrument did not
        report are absent, not blank.
    """
    return {
        key: entry.metadata[key]
        for key in METADATA_HIGHLIGHTS
        if key in entry.metadata
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
        empty for an empty series.
    """
    if not frames:
        return ((), "", "")
    first = frames[0]
    return (
        tuple(first.data.shape),
        str(first.data.dtype),
        png_data_uri(first.data, max_edge=THUMBNAIL_MAX_EDGE),
    )


class SessionLog:
    """
    Every acquisition this dashboard has attempted, in order, for keeps.

    See the module docstring for why it is append-only and why it is
    locked. The only mutation is :meth:`append`.
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
