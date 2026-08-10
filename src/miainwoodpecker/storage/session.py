"""
Sessions: where an acquisition's data goes, and what it was.

Phase 3 gave this app the ability to *write* NeXus files
(:mod:`miainwoodpecker.storage.nexus`) and to stream a series into one
(:func:`miainwoodpecker.acquisition.sequence.record`), but nothing in the
running application ever chose a filename — so the live viewer displayed
frames and threw them away, and the Phase 4 analysis buttons wrote into
``tempfile.TemporaryDirectory()`` files that were deleted on the way out.
An operator could not press anything and keep their data, which blocks a
Phase 5 pilot for a wholly mundane reason.

This module is that missing piece and deliberately nothing more: a
directory, a collision-free naming rule, the session-level context that
makes a file interpretable six months later (who ran it, on what sample,
with what note), and a background job so a slow write does not freeze the
GUI. It is **not** a data-management layer — no database, no index, no
queries, no cataloguing. The filesystem is the index and NeXus files are
the records (§1: thin glue, reuse over reimplementation). Everything here
composes with the Phase 3 primitives rather than replacing them:
:meth:`Session.record` is a thin wrapper over
:func:`miainwoodpecker.acquisition.sequence.record`.

How session context reaches the file, and why it looks like this
----------------------------------------------------------------
``NexusWriter``'s constructor takes ``title``/``definition``/
``compression`` and persists the **first frame's** ``metadata`` mapping as
JSON at ``/entry/metadata/vendor_metadata_json``. It has no operator,
sample, or notes fields, and growing them was out of scope for the change
that added this module. So session context reaches the file through the
API that already exists, in two complementary places:

- **Injected into frame metadata** under a ``session_`` key prefix, so it
  lands inside the HDF5 file and travels with it when the file is copied
  off the instrument. Read it back with :func:`read_session_context`.
- **A ``session.json`` sidecar** at the session root, which records the
  context *once* per session rather than once per file, survives being
  edited by hand at the instrument, and lets a session be reopened later
  with its context intact. This is not duplicated bookkeeping: NeXus
  files have no concept of the session that produced them, and the
  sidecar is what makes reopening a directory mid-shift work.

The clean fix, deferred: ``nexus.py`` should accept the session context
directly and write it as real NeXus classes — operator into an ``NXuser``
group at ``/entry/user``, sample identifier into ``NXsample`` at
``/entry/sample``, notes into ``/entry/notes`` — which is where standard
NeXus tooling looks for exactly these three things. The ``session_``
prefixed keys are honest about being a stand-in: they are our own
convention inside a vendor-metadata blob, not schema-conformant NeXus,
and they conflate session context with vendor-reported acquisition
properties in one JSON object.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import json
import re
import threading
import typing
from pathlib import Path

import h5py

from miainwoodpecker.acquisition.sequence import record as record_frames

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Iterable, Iterator

    from miainwoodpecker.devices.interface import Frame

_SIDECAR_NAME = "session.json"
_SIDECAR_SCHEMA = "miainwoodpecker-session/1"
_SUFFIX = ".nxs"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_INDEX_DIGITS = 4
_CONTEXT_PREFIX = "session_"
_CONTEXT_FIELDS = ("operator", "sample", "notes")
_JOIN_TIMEOUT_S = 30.0

# 0007-scan-haadf-20260810T142530Z.nxs
_NAME_PATTERN = re.compile(
    rf"^(?P<index>\d{{{_INDEX_DIGITS},}})-(?P<label>.+)-(?P<stamp>\d{{8}}T\d{{6}}Z)"
    rf"{re.escape(_SUFFIX)}$"
)


@dataclasses.dataclass(frozen=True)
class Recording:
    """
    One acquisition kept on disk, as the session sees it.

    Attributes
    ----------
    path : Path
        The NeXus HDF5 file.
    index : int
        Per-session sequence number, from the filename.
    label : str
        The slug describing what was recorded (``"scan-haadf"``, ...).
    started_at : datetime.datetime
        When the name was minted, parsed back from the filename (UTC).
    frame_count : int
        Frames actually present in the file. ``0`` for an acquisition
        that produced nothing, and for a file too damaged to open.
    readable : bool
        Whether the file opens as HDF5 at all. ``False`` means the write
        was interrupted hard enough to lose the container — see
        :meth:`Session.record` for what does and does not survive.
    """

    path: Path
    index: int
    label: str
    started_at: datetime.datetime
    frame_count: int
    readable: bool


class Session:
    """
    A directory of recordings plus the context they were taken in.

    An existing directory is **reused, never cleared**: an operator who
    restarts the app mid-shift should land back in the same session and
    keep numbering where they left off, not clobber the morning's data.
    Context stored in a previous run's ``session.json`` is loaded, and
    only the fields passed here explicitly override it — omitting a field
    leaves what was already recorded rather than blanking it.

    Parameters
    ----------
    root : os.PathLike[str] | str
        Session directory. Created (with parents) if it does not exist.
    operator : str | None
        Who is running the instrument. None keeps any stored value.
    sample : str | None
        Sample identifier. None keeps any stored value.
    notes : str | None
        Free-text notes about the session. None keeps any stored value.

    Raises
    ------
    NotADirectoryError
        If ``root`` exists but is not a directory.
    """

    def __init__(
        self,
        root: os.PathLike[str] | str,
        *,
        operator: str | None = None,
        sample: str | None = None,
        notes: str | None = None,
    ) -> None:
        self._root = Path(root)
        if self._root.exists() and not self._root.is_dir():
            msg = f"session root {self._root} exists but is not a directory"
            raise NotADirectoryError(msg)
        self._root.mkdir(parents=True, exist_ok=True)
        self._context: dict[str, str] = dict.fromkeys(_CONTEXT_FIELDS, "")
        self._created = _now()
        self._load_sidecar()
        self.update_context(operator=operator, sample=sample, notes=notes)

    @property
    def root(self) -> Path:
        """Return the session directory."""
        return self._root

    @property
    def operator(self) -> str:
        """Return who is running the instrument."""
        return self._context["operator"]

    @property
    def sample(self) -> str:
        """Return the sample identifier."""
        return self._context["sample"]

    @property
    def notes(self) -> str:
        """Return the session's free-text notes."""
        return self._context["notes"]

    @property
    def context(self) -> dict[str, str]:
        """Return a copy of the session context (operator, sample, notes)."""
        return dict(self._context)

    def update_context(
        self,
        *,
        operator: str | None = None,
        sample: str | None = None,
        notes: str | None = None,
    ) -> None:
        """
        Change session context and persist it to the sidecar.

        Called whenever an operator edits a field mid-session (a new
        sample on the same holder, say), so later recordings carry the new
        context and earlier ones keep the old.

        Parameters
        ----------
        operator : str | None
            New operator, or None to leave unchanged.
        sample : str | None
            New sample identifier, or None to leave unchanged.
        notes : str | None
            New notes, or None to leave unchanged.
        """
        for field, value in (
            ("operator", operator),
            ("sample", sample),
            ("notes", notes),
        ):
            if value is not None:
                self._context[field] = value
        self._save_sidecar()

    def reserve_path(self, label: str) -> Path:
        """
        Mint and claim a collision-free filename for one acquisition.

        The name carries a per-session sequence number (so recordings
        sort in acquisition order), the label, and a UTC timestamp. The
        file is created empty and atomically here — ``O_EXCL``, via
        :meth:`pathlib.Path.touch` — rather than merely checked for
        absence, so two acquisitions cannot pick the same name even
        within the same second, from two threads, or from two processes
        pointed at one directory. ``NexusWriter`` truncates the
        placeholder when it opens it.

        Parameters
        ----------
        label : str
            What is being recorded; slugified into the filename.

        Returns
        -------
        Path
            The reserved path, inside :attr:`root`.
        """
        return self._reserve(label)[0]

    def _reserve(self, label: str) -> tuple[Path, int, str, datetime.datetime]:
        """Claim a filename, returning it with the parts it was built from."""
        slug = _slugify(label)
        # Truncated to whole seconds so this matches what reading the name
        # back off disk recovers.
        started_at = _now().replace(microsecond=0)
        stamp = started_at.strftime(_STAMP_FORMAT)
        index = self._next_index()
        while True:
            name = f"{index:0{_INDEX_DIGITS}d}-{slug}-{stamp}{_SUFFIX}"
            candidate = self._root / name
            try:
                candidate.touch(exist_ok=False)
            except FileExistsError:
                index += 1
                continue
            return candidate, index, slug, started_at

    def record(
        self,
        frames: Iterable[Frame],
        *,
        label: str = "acquisition",
        title: str | None = None,
    ) -> Recording:
        """
        Stream frames into a new file in this session.

        A thin wrapper over
        :func:`miainwoodpecker.acquisition.sequence.record`: it picks the
        name, injects the session context into each frame's metadata (so
        the writer persists it — see this module's docstring), and
        describes the result. Frames are streamed, not buffered.

        What an interruption leaves behind, measured rather than assumed:
        because ``record`` writes inside a ``with NexusWriter(...)``,
        anything that unwinds Python — an exception from the device, a
        ``KeyboardInterrupt``, or a cancelled :class:`RecordingJob`
        breaking out of the generator — still runs ``close()``, so the
        file is **complete and valid** for however many frames arrived,
        NXdata plotting hints and all. A hard process kill is different:
        HDF5 buffers its object headers, so a ``SIGKILL`` mid-acquisition
        leaves a file that does not open at all (not a short-but-valid
        one). See this module's notes in the migration plan for the
        ``nexus.py`` flush follow-up that would bound that loss.

        Parameters
        ----------
        frames : Iterable[Frame]
            Frames to record, typically one of the
            :mod:`miainwoodpecker.acquisition.sequence` generators.
        label : str
            What is being recorded; slugified into the filename.
        title : str | None
            ``/entry/title`` for the file. Defaults to a title composed
            from the label and session context.

        Returns
        -------
        Recording
            The file that was written, as read back from disk.
        """
        target, index, slug, started_at = self._reserve(label)
        record_frames(
            self._with_context(frames, label),
            target,
            title=title if title is not None else self._default_title(label),
        )
        frame_count, readable = _inspect(target)
        return Recording(
            path=target,
            index=index,
            label=slug,
            started_at=started_at,
            frame_count=frame_count,
            readable=readable,
        )

    def recordings(self) -> list[Recording]:
        """
        List what this session has recorded, in acquisition order.

        Reads the directory and each file's frame count — the filesystem
        is the index, so there is nothing to keep in sync. Files whose
        names this session did not mint are ignored.

        Returns
        -------
        list[Recording]
            One entry per recording, sorted by sequence number.
        """
        found = [
            described
            for candidate in self._root.glob(f"*{_SUFFIX}")
            if (described := _describe(candidate)) is not None
        ]
        return sorted(found, key=lambda recording: recording.index)

    def _next_index(self) -> int:
        """Return one past the highest sequence number already on disk."""
        indices = [
            int(match["index"])
            for candidate in self._root.glob(f"*{_SUFFIX}")
            if (match := _NAME_PATTERN.match(candidate.name)) is not None
        ]
        return max(indices, default=0) + 1

    def _default_title(self, label: str) -> str:
        """Compose ``/entry/title`` from the label and session context."""
        parts = [label]
        if self.sample:
            parts.append(f"sample: {self.sample}")
        if self.operator:
            parts.append(f"operator: {self.operator}")
        return " - ".join(parts)

    def _with_context(self, frames: Iterable[Frame], label: str) -> Iterator[Frame]:
        """Yield each frame with the session context added to its metadata."""
        extra = {
            f"{_CONTEXT_PREFIX}{key}": value for key, value in self._context.items()
        }
        extra[f"{_CONTEXT_PREFIX}label"] = label
        extra[f"{_CONTEXT_PREFIX}root"] = str(self._root)
        for frame in frames:
            yield dataclasses.replace(frame, metadata={**frame.metadata, **extra})

    @property
    def _sidecar_path(self) -> Path:
        """Return the path of the session's context sidecar."""
        return self._root / _SIDECAR_NAME

    def _load_sidecar(self) -> None:
        """Load context from a previous run's sidecar, ignoring a damaged one."""
        try:
            stored = json.loads(self._sidecar_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict):
            return
        for field in _CONTEXT_FIELDS:
            value = stored.get(field)
            if isinstance(value, str):
                self._context[field] = value
        created = stored.get("created")
        if isinstance(created, str):
            with contextlib.suppress(ValueError):
                self._created = datetime.datetime.fromisoformat(created)

    def _save_sidecar(self) -> None:
        """Write the session context sidecar."""
        payload = {
            "schema": _SIDECAR_SCHEMA,
            "created": self._created.isoformat(),
            **self._context,
        }
        self._sidecar_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


class RecordingJob:
    """
    Run one :meth:`Session.record` on a worker thread, cancellably.

    Recording is I/O-bound and can be slow (large frames, compression, a
    long series), so it must not run on the GUI thread. This deliberately
    mirrors :class:`~miainwoodpecker.acquisition.live.LiveAcquisition`:
    a daemon thread, state behind a lock, exceptions captured rather than
    raised into the caller, and **no Qt anywhere** — a caller polls
    :attr:`is_running`/:attr:`result`/:attr:`error` from its own event
    loop (migration plan, Phase 2's thread-safety contract).

    :meth:`cancel` is cooperative: it stops pulling frames from the
    generator, which unwinds ``record``'s ``with`` block normally and so
    leaves a complete, valid file containing the frames that did arrive.

    Parameters
    ----------
    session : Session
        Session that names and writes the file.
    frames : Iterable[Frame]
        Frames to record, consumed on the worker thread.
    label : str
        What is being recorded; slugified into the filename.
    title : str | None
        ``/entry/title``, or None for the session's default.
    """

    def __init__(
        self,
        session: Session,
        frames: Iterable[Frame],
        *,
        label: str = "acquisition",
        title: str | None = None,
    ) -> None:
        self._session = session
        self._frames = frames
        self._label = label
        self._title = title
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._thread: threading.Thread | None = None
        self._result: Recording | None = None
        self._error: Exception | None = None
        self._frames_seen = 0

    def start(self) -> None:
        """Start the worker thread; a no-op if it is already running."""
        if self.is_running:
            return
        self._thread = threading.Thread(target=self._run, name="recording", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """Ask the worker to stop after the current frame, keeping the file."""
        self._cancelled.set()

    def join(self, timeout: float = _JOIN_TIMEOUT_S) -> None:
        """
        Wait for the worker thread to finish.

        Parameters
        ----------
        timeout : float
            Seconds to wait before giving up.
        """
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @property
    def is_running(self) -> bool:
        """Return whether the worker thread is currently alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def is_cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._cancelled.is_set()

    @property
    def frames_recorded(self) -> int:
        """Return how many frames have been handed to the writer so far."""
        with self._lock:
            return self._frames_seen

    @property
    def result(self) -> Recording | None:
        """Return the finished recording, or None until the job completes."""
        with self._lock:
            return self._result

    @property
    def error(self) -> Exception | None:
        """Return the exception that ended the job, if any."""
        with self._lock:
            return self._error

    def _counted(self) -> Iterator[Frame]:
        """Yield frames until cancelled, counting them as they pass."""
        for frame in self._frames:
            if self._cancelled.is_set():
                return
            with self._lock:
                self._frames_seen += 1
            yield frame

    def _run(self) -> None:
        """Record on the worker thread, capturing any failure."""
        try:
            recording = self._session.record(
                self._counted(), label=self._label, title=self._title
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to callers via .error
            with self._lock:
                self._error = exc
            return
        with self._lock:
            self._result = recording


def read_session_context(path: os.PathLike[str] | str) -> dict[str, str]:
    """
    Read back the session context a recording was made in.

    Reads the ``session_``-prefixed keys out of
    ``/entry/metadata/vendor_metadata_json`` — the stand-in described in
    this module's docstring — and strips the prefix.

    Parameters
    ----------
    path : os.PathLike[str] | str
        A NeXus file written through :meth:`Session.record`.

    Returns
    -------
    dict[str, str]
        The context keys (``operator``, ``sample``, ``notes``, ``label``,
        ``root``). Empty for a file written without a session, or one
        whose acquisition produced no frames (the writer persists the
        first frame's metadata, so no frames means no metadata).
    """
    with h5py.File(path, "r") as handle:
        dataset = handle.get("entry/metadata/vendor_metadata_json")
        if dataset is None:
            return {}
        raw = dataset[()]
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    stored = json.loads(text)
    return {
        key.removeprefix(_CONTEXT_PREFIX): value
        for key, value in stored.items()
        if key.startswith(_CONTEXT_PREFIX) and isinstance(value, str)
    }


def default_root(base: os.PathLike[str] | str | None = None) -> Path:
    """
    Return a per-day session directory to use when nobody named one.

    One directory per calendar day under ``~/miainwoodpecker-data`` is
    the smallest default that keeps a pilot's files from piling into one
    heap, and matches how instrument time is actually booked.

    Parameters
    ----------
    base : os.PathLike[str] | str | None
        Parent directory for session folders. Defaults to
        ``~/miainwoodpecker-data``.

    Returns
    -------
    Path
        The session directory path; not created here.
    """
    parent = Path(base) if base is not None else Path.home() / "miainwoodpecker-data"
    return parent / _now().strftime("%Y-%m-%d")


def _now() -> datetime.datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.datetime.now(tz=datetime.UTC)


def _slugify(label: str) -> str:
    """Reduce a label to lowercase alphanumerics and hyphens for a filename."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return slug or "acquisition"


def _describe(path: Path) -> Recording | None:
    """Describe a session file, or return None if this session did not mint it."""
    match = _NAME_PATTERN.match(path.name)
    if match is None:
        return None
    frame_count, readable = _inspect(path)
    return Recording(
        path=path,
        index=int(match["index"]),
        label=match["label"],
        started_at=datetime.datetime.strptime(match["stamp"], _STAMP_FORMAT).replace(
            tzinfo=datetime.UTC
        ),
        frame_count=frame_count,
        readable=readable,
    )


def _inspect(path: Path) -> tuple[int, bool]:
    """Return a file's frame count and whether it opens as HDF5 at all."""
    try:
        with h5py.File(path, "r") as handle:
            dataset = handle.get("entry/instrument/detector/data")
            return (0 if dataset is None else int(dataset.shape[0])), True
    except (OSError, KeyError):
        # A reserved-but-unwritten placeholder, or a write killed hard
        # enough to lose the HDF5 container. Both are real states an
        # operator can produce, so report them instead of raising.
        return 0, False
