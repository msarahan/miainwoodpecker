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
Three places, each for a reason:

- **Real NeXus groups**, which is where standard tooling looks. When this
  module first landed, ``NexusWriter`` had no fields for operator, sample,
  or notes and the ``session_`` metadata prefix below was explicitly
  flagged as a stand-in for them. The writer has since grown
  ``sample=``/``user=``/``instrument=``/``notes=``, writing genuine
  ``NXsample``, ``NXuser``, ``NXinstrument`` and ``NXnote`` groups, so
  :meth:`Session.record` now passes them: the sample becomes
  ``/entry/sample/name``, the sample area ``/entry/sample/description``,
  the operator ``/entry/user/name``, the microscope
  ``/entry/instrument/name``, and the notes
  ``/entry/notes/description``. Note this does **not** make the file
  claim ``NXem`` — that additionally needs specimen facts
  (``is_simulation``, ``preparation_date``, ``atom_types``) no session
  knows, and ``nexus.py``'s own docstring explains why it refuses to
  invent them.

  **Four of the seven fields, not all seven.** ``site`` and ``task`` have
  no NeXus field that means what they mean — ``NXuser/affiliation`` is
  who the *operator* belongs to, not where the microscope is — and this
  project's standing rule is that a confidently wrong value in a standard
  field is worse than an honest absence. They live in the other two
  places below, which is where everything NeXus has no home for lives.
- **Injected into frame metadata** under a ``session_`` key prefix, kept
  rather than removed: it is the only place the *label* and session *root*
  land, it round-trips through one call
  (:func:`read_session_context`), and files already written this way stay
  readable by the same code. Deliberately a duplicate of the NeXus groups
  above for the two fields they share, not a replacement for them.
- **A ``session.json`` sidecar** at the session root, which records the
  context *once* per session rather than once per file, survives being
  edited by hand at the instrument, and lets a session be reopened later
  with its context intact. This is not duplicated bookkeeping: NeXus
  files have no concept of the session that produced them, and the
  sidecar is what makes reopening a directory mid-shift work.

Notes come in two scopes, because one was not enough for a shift
-----------------------------------------------------------------
:attr:`Session.notes` is the shift's standing context ("Au on C grid,
liquid nitrogen topped up 09:40") and is free to be many lines long; the
``note=`` argument to :meth:`Session.record` is about *this file* ("hole 4,
after tilting"). Both end up in the one ``NXnote`` the writer offers, each
prefixed with which scope it came from, because a single ``description``
field that silently mixed the two would leave a reader unable to tell a
standing observation from a per-frame one.

Reading a recording back
------------------------
:func:`load_recording` is the read path, and it deliberately goes through
:func:`miainwoodpecker.storage.nexus.read_series` rather than opening
``/entry/data`` itself: the second reader would be the start of a bespoke
format layer, and ``read_series`` reads
``/entry/instrument/detector``, which is exactly the group that survives
the interruption modes the migration plan's Phase 3 table measured. A
recording abandoned by its writer therefore *opens and displays* here even
though the Phase 4 analysis adapters (which read ``/entry/data``) cannot
touch it — :attr:`Recording.finalized` is that distinction, and callers
are expected to report it rather than discover it as a ``ValueError``.
:class:`LoadJob` runs the load on a worker thread for the same reason
:class:`RecordingJob` writes on one.

What comes back is enough to analyze, not only to display
----------------------------------------------------------
:class:`LoadedRecording` carries the file's axis calibration alongside its
frames, and :attr:`LoadedRecording.frames` hands both to the Phase 4
adapters in one object. That closes a genuinely wasteful path the migration
plan's Phase 5 list carried as open: analyzing a recording opened in the
viewer used to read it twice — once here to draw it, once in the adapter,
which took a path. The calibration is the reason it could not simply be an
array: an adapter handed frames without it produces a signal whose axes
silently claim bare pixels, which is a worse bug than the duplicated read.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import json
import os
import re
import shutil
import threading
import typing
from pathlib import Path

import h5py
import numpy as np

from miainwoodpecker.acquisition.sequence import record as record_frames
from miainwoodpecker.jobs import BackgroundJob
from miainwoodpecker.storage import layout
from miainwoodpecker.storage.nexus import FrameStack, read_calibration, read_series

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    import numpy.typing as npt

    from miainwoodpecker.devices.interface import Frame
    from miainwoodpecker.storage.calibration import FrameCalibration

_SIDECAR_NAME = "session.json"
_SIDECAR_SCHEMA = "miainwoodpecker-session/1"
_SUFFIX = ".nxs"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_INDEX_DIGITS = 4
_CONTEXT_PREFIX = "session_"
# Nion's own documented session vocabulary, from its scripting guide:
# stem.session.{instrument, microscopist, sample, sample_area, site, task}.
# Adopted rather than invented, because a vocabulary someone else
# maintains is easier to defend than one made up here, and because these
# are the facts that make a recording identifiable a year later.
#
# Two divergences. "microscopist" stays "operator": it is the same field
# under plainer English, it is what the viewer, the sidecar files already
# on disk, and this API all say, and renaming it would cost a migration
# for no gain. And "notes" is ours - Nion has no free-text session field,
# and NXnote is the obvious home for one.
_CONTEXT_FIELDS = (
    "operator",
    "instrument",
    "site",
    "sample",
    "sample_area",
    "task",
    "notes",
)
# 1000, not 1024: operators compare this against what the OS file manager
# and `df -h` report, and those use decimal units.
_BYTES_PER_UNIT = 1000.0
# How much frame data one "open this recording" reads into memory. A pilot
# day's 2048x2048 float32 Ronchigram frames are 16.8MB each, so an
# unbounded read of a long series would exhaust memory rather than display
# anything; this loads as many frames as fit and says it truncated.
_DEFAULT_LOAD_BUDGET_BYTES = 512 * 1024 * 1024

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
    finalized : bool
        Whether ``NexusWriter.close()`` ran, i.e. whether the file has its
        ``/entry/data`` group, ``end_time``, and metadata. ``False`` with
        ``frame_count`` above zero is the abandoned-writer case from the
        migration plan's Phase 3 interruption table: every frame is present
        and :func:`load_recording` displays them, but the Phase 4 analysis
        adapters read ``/entry/data`` and cannot.
    """

    path: Path
    index: int
    label: str
    started_at: datetime.datetime
    frame_count: int
    readable: bool
    finalized: bool


@dataclasses.dataclass(frozen=True)
class LoadedRecording:
    """
    A recording read back off disk, ready to display.

    Attributes
    ----------
    recording : Recording
        What the file is, including whether its writer finalized it.
    data : np.ndarray
        The frames as one ``(frames, height, width)`` stack, **always
        three-dimensional even for a single frame** — a recording is a
        series, and napari renders a 3D array with a frame slider natively,
        so nothing here needs to reimplement stack navigation. A caller
        displaying a one-frame recording may squeeze it; the data model does
        not, because "one frame" and "one frame so far" are the same file.
    frame_times : tuple[float, ...]
        Each loaded frame's elapsed time in seconds, from the file.
    truncated : bool
        Whether the byte budget stopped the read early, so ``data`` holds
        fewer frames than :attr:`Recording.frame_count`.
    calibration : FrameCalibration | None
        The per-axis calibration the file records, or ``None`` when it
        states none. ``None`` is not "uncalibrated" — an uncalibrated
        recording says so, in ``"pixel"`` units — it is "this file never
        got as far as writing its axes", which is the abandoned-writer case
        from the Phase 3 interruption table: the axis datasets live in
        ``/entry/data``, which ``NexusWriter.close()`` creates. Kept
        distinct because the two are answered differently: the first can be
        analyzed, the second cannot.
    """

    recording: Recording
    data: np.ndarray
    frame_times: tuple[float, ...]
    truncated: bool
    calibration: FrameCalibration | None = None

    @property
    def frames(self) -> FrameStack | None:
        """
        Return these frames in the form the analysis adapters take, if they qualify.

        The point of the whole read-once path: the viewer hands this
        straight to
        :func:`~miainwoodpecker.analysis.hyperspy_bridge.hyperspy_signal_from_frames`
        and its siblings instead of handing them the path and paying for a
        second decompression of the same tens of megabytes.

        ``None`` in the two cases where these frames are not the recording:

        - **No calibration**, i.e. an unfinalized file. Its frames display
          fine, but a signal built from them would claim pixel axes the
          file never asserted.
        - **Truncated**, i.e. the byte budget stopped the read early. The
          frames present are real, but "the mean of this recording"
          computed from the first few of them is not what it says it is,
          and the adapter reading the file itself is the honest answer.

        Both are silent narrowings if they were not checked here, which is
        why they are checked here — one place — rather than at each of the
        three call sites.

        Returns
        -------
        FrameStack | None
            The stack, times, and calibration together, or ``None`` when
            these frames should not stand in for the file.
        """
        if self.calibration is None or self.truncated:
            return None
        return FrameStack(
            self.data,
            np.asarray(self.frame_times, dtype="float64"),
            self.calibration,
        )


class RecordingReadError(RuntimeError):
    """Raised when a recording cannot be read, with a reason an operator can use."""


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
    instrument : str | None
        Which microscope. None keeps any stored value.
    site : str | None
        Where the instrument is — the facility or laboratory. None keeps
        any stored value.
    sample : str | None
        Sample identifier. None keeps any stored value.
    sample_area : str | None
        Which region of the sample is under the beam. None keeps any
        stored value.
    task : str | None
        What this session is trying to do. None keeps any stored value.
    notes : str | None
        Free-text notes about the session. None keeps any stored value.

    Raises
    ------
    NotADirectoryError
        If ``root`` exists but is not a directory.
    """

    def __init__(  # noqa: PLR0913 - one keyword per context field, by design
        self,
        root: os.PathLike[str] | str,
        *,
        operator: str | None = None,
        instrument: str | None = None,
        site: str | None = None,
        sample: str | None = None,
        sample_area: str | None = None,
        task: str | None = None,
        notes: str | None = None,
    ) -> None:
        self._root = Path(root)
        if self._root.exists() and not self._root.is_dir():
            msg = f"session root {self._root} exists but is not a directory"
            raise NotADirectoryError(msg)
        self._root.mkdir(parents=True, exist_ok=True)
        self._context: dict[str, str] = dict.fromkeys(_CONTEXT_FIELDS, "")
        # path -> ((mtime, size), description); see recordings().
        self._recording_cache: dict[Path, tuple[tuple[float, int], Recording]] = {}
        self._created = _now()
        self._load_sidecar()
        self.update_context(
            operator=operator,
            instrument=instrument,
            site=site,
            sample=sample,
            sample_area=sample_area,
            task=task,
            notes=notes,
        )

    @property
    def root(self) -> Path:
        """Return the session directory."""
        return self._root

    @property
    def operator(self) -> str:
        """Return who is running the instrument."""
        return self._context["operator"]

    @property
    def instrument(self) -> str:
        """Return which microscope this session is running on."""
        return self._context["instrument"]

    @property
    def site(self) -> str:
        """Return where the instrument is — the facility or laboratory."""
        return self._context["site"]

    @property
    def sample(self) -> str:
        """Return the sample identifier."""
        return self._context["sample"]

    @property
    def sample_area(self) -> str:
        """Return which region of the sample is under the beam."""
        return self._context["sample_area"]

    @property
    def task(self) -> str:
        """Return what this session is trying to do."""
        return self._context["task"]

    @property
    def notes(self) -> str:
        """Return the session's free-text notes."""
        return self._context["notes"]

    @property
    def context(self) -> dict[str, str]:
        """Return a copy of the session context (operator, sample, notes)."""
        return dict(self._context)

    def update_context(  # noqa: PLR0913 - one keyword per context field
        self,
        *,
        operator: str | None = None,
        instrument: str | None = None,
        site: str | None = None,
        sample: str | None = None,
        sample_area: str | None = None,
        task: str | None = None,
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
        instrument : str | None
            New microscope name, or None to leave unchanged.
        site : str | None
            New facility or laboratory, or None to leave unchanged.
        sample : str | None
            New sample identifier, or None to leave unchanged.
        sample_area : str | None
            New region of the sample, or None to leave unchanged.
        task : str | None
            New description of what the session is doing, or None to
            leave unchanged.
        notes : str | None
            New notes, or None to leave unchanged.
        """
        for field, value in (
            ("operator", operator),
            ("instrument", instrument),
            ("site", site),
            ("sample", sample),
            ("sample_area", sample_area),
            ("task", task),
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
        note: str | None = None,
    ) -> Recording:
        """
        Stream frames into a new file in this session.

        A thin wrapper over
        :func:`miainwoodpecker.acquisition.sequence.record`: it picks the
        name, hands the session context to the writer's ``NXsample``/
        ``NXuser``/``NXnote`` parameters *and* injects it into each frame's
        metadata (see this module's docstring for why both), and describes
        the result. Frames are streamed, not buffered.

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
        note : str | None
            A note about *this recording* specifically, as opposed to
            :attr:`notes`, which is the shift's standing context. Both land
            in the file's ``NXnote``, each labelled with its scope.

        Returns
        -------
        Recording
            The file that was written, as read back from disk.
        """
        target, index, slug, started_at = self._reserve(label)
        record_frames(
            self._with_context(frames, label, note),
            target,
            title=title if title is not None else self._default_title(label),
            **self._nexus_context(note),
        )
        return _describe_path(target, index=index, label=slug, started_at=started_at)

    def recordings(self) -> list[Recording]:
        """
        List what this session has recorded, in acquisition order.

        Reads the directory and each file's frame count — the filesystem
        is the index, so there is nothing to keep in sync. Files whose
        names this session did not mint are ignored.

        Descriptions are cached per file and invalidated by ``(mtime,
        size)``, because describing one means *opening* it as HDF5, and
        the viewer calls this on the GUI thread after every recording and
        every analysis run. Without the cache a shift's worth of
        recordings costs that many HDF5 opens per refresh — a freeze that
        grows through the day, and worst on the network-mounted session
        directory a real instrument is likeliest to use. The cache is
        keyed on the stat rather than merely on the path so an
        in-progress recording, whose file grows between refreshes, is
        still re-read.

        Returns
        -------
        list[Recording]
            One entry per recording, sorted by sequence number.
        """
        found = []
        fresh: dict[Path, tuple[tuple[float, int], Recording]] = {}
        for candidate in self._root.glob(f"*{_SUFFIX}"):
            if _NAME_PATTERN.match(candidate.name) is None:
                continue
            try:
                status = candidate.stat()
                stamp = (status.st_mtime, status.st_size)
            except OSError:
                # Vanished between the glob and the stat; nothing to list.
                continue
            cached = self._recording_cache.get(candidate)
            if cached is not None and cached[0] == stamp:
                described = cached[1]
            else:
                described = _describe_path(candidate)
            fresh[candidate] = (stamp, described)
            found.append(described)
        # Rebuilt rather than updated, so deleted files do not accumulate.
        self._recording_cache = fresh
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

    def _nexus_context(self, note: str | None) -> dict[str, object]:
        """
        Build the ``NexusWriter`` arguments carrying session context.

        Each is omitted when empty rather than written blank, so a file
        never claims an ``NXsample`` or ``NXuser`` that says nothing.

        Parameters
        ----------
        note : str | None
            This recording's own note, if the operator wrote one.

        Returns
        -------
        dict[str, object]
            ``sample``/``user``/``instrument``/``notes`` keyword arguments
            for the writer.
        """
        arguments: dict[str, object] = {}
        # NXsample's own fields; the specimen facts NXem additionally
        # requires are not the session's to state (see nexus.py's
        # docstring). `sample_area` is `description` because that is what
        # it is — which part of the specimen was under the beam.
        specimen = {
            name: value
            for name, value in (
                ("name", self.sample),
                ("description", self.sample_area),
            )
            if value
        }
        if specimen:
            arguments["sample"] = specimen
        if self.operator:
            arguments["user"] = {"name": self.operator}
        if self.instrument:
            arguments["instrument"] = self.instrument
        combined = _compose_notes(self.notes, note)
        if combined is not None:
            arguments["notes"] = combined
        return arguments

    def _with_context(
        self,
        frames: Iterable[Frame],
        label: str,
        note: str | None,
    ) -> Iterator[Frame]:
        """Yield each frame with the session context added to its metadata."""
        extra = {
            f"{_CONTEXT_PREFIX}{key}": value for key, value in self._context.items()
        }
        extra[f"{_CONTEXT_PREFIX}label"] = label
        extra[f"{_CONTEXT_PREFIX}root"] = str(self._root)
        if note:
            extra[f"{_CONTEXT_PREFIX}note"] = note
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
        """
        Write the session context sidecar atomically.

        Write-temp-then-``os.replace`` rather than truncate-then-write,
        for the same reason :meth:`reserve_path` claims a name with
        ``O_EXCL``: a crash or a full disk part-way through used to leave
        a truncated ``session.json``, and :meth:`_load_sidecar` swallows
        the resulting ``ValueError`` and silently reverts the session to
        blank context. Losing a shift's operator and sample that way is
        worse than the write failing outright. This runs on every context
        edit — including a debounced one while the operator is still
        typing — so it is not a rare path.

        ``os.replace`` is atomic within a filesystem, and the temporary
        file is created in the session directory itself so the rename
        never crosses one.
        """
        payload = {
            "schema": _SIDECAR_SCHEMA,
            "created": self._created.isoformat(),
            **self._context,
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        temporary = self._sidecar_path.with_name(
            f"{self._sidecar_path.name}.{os.getpid()}.tmp",
        )
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(self._sidecar_path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise


class RecordingJob(BackgroundJob):
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
    note : str | None
        A note about this recording specifically, passed through to
        :meth:`Session.record`.
    """

    def __init__(
        self,
        session: Session,
        frames: Iterable[Frame],
        *,
        label: str = "acquisition",
        title: str | None = None,
        note: str | None = None,
    ) -> None:
        super().__init__("recording")
        self._session = session
        self._frames = frames
        self._label = label
        self._title = title
        self._note = note
        self._cancelled = threading.Event()
        self._frames_seen = 0

    def _reset(self) -> None:
        """Clear cancellation and the frame count before a fresh run."""
        self._cancelled.clear()
        with self._lock:
            self._frames_seen = 0

    def cancel(self) -> None:
        """Ask the worker to stop after the current frame, keeping the file."""
        self._cancelled.set()

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
            return typing.cast("Recording | None", self._raw_result)

    def _counted(self) -> Iterator[Frame]:
        """Yield frames until cancelled, counting them as they pass."""
        for frame in self._frames:
            if self._cancelled.is_set():
                return
            with self._lock:
                self._frames_seen += 1
            yield frame

    def _work(self) -> object:
        """
        Record on the worker thread.

        Returns
        -------
        object
            The finished :class:`Recording`.
        """
        return self._session.record(
            self._counted(),
            label=self._label,
            title=self._title,
            note=self._note,
        )


class LoadJob(BackgroundJob):
    """
    Read one recording off disk on a worker thread.

    The write side already refuses to block the GUI thread
    (:class:`RecordingJob`); reading is the same problem in the other
    direction, and no smaller — the measured 23.3MB two-frame Ronchigram
    recording from the migration plan's Phase 3 notes took 5.5 seconds to
    write, and reading it back decompresses the same bytes. So this has
    :class:`RecordingJob`'s exact shape: a daemon thread, state behind a
    lock, exceptions captured rather than raised, and **no Qt anywhere**,
    polled by the caller from its own event loop.

    napari's own ``thread_worker`` was deliberately not used here: it
    reports completion through Qt signals, which would make the widget
    tests depend on driving a Qt event loop — the thing
    ``tests/integration/test_live_widget.py`` avoids by calling
    ``refresh_display`` directly.

    Parameters
    ----------
    path : os.PathLike[str] | str
        The NeXus file to read.
    budget_bytes : int
        Frame-data budget, as for :func:`load_recording`.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        budget_bytes: int = _DEFAULT_LOAD_BUDGET_BYTES,
    ) -> None:
        super().__init__("loading")
        self._path = Path(path)
        self._budget_bytes = budget_bytes
        self._frames_seen = 0

    def _reset(self) -> None:
        """Clear the frame count before a fresh run."""
        with self._lock:
            self._frames_seen = 0

    @property
    def path(self) -> Path:
        """Return the file being read."""
        return self._path

    @property
    def frames_loaded(self) -> int:
        """Return how many frames have been read so far."""
        with self._lock:
            return self._frames_seen

    @property
    def result(self) -> LoadedRecording | None:
        """Return the loaded recording, or None until the job completes."""
        with self._lock:
            return typing.cast("LoadedRecording | None", self._raw_result)

    def _count(self, loaded: int) -> None:
        """Record progress from the worker thread."""
        with self._lock:
            self._frames_seen = loaded

    def _work(self) -> object:
        """
        Read on the worker thread.

        Returns
        -------
        object
            The loaded recording.
        """
        return load_recording(
            self._path,
            budget_bytes=self._budget_bytes,
            progress=self._count,
        )


def describe(path: os.PathLike[str] | str) -> Recording:
    """
    Describe any NeXus file on disk, whether a session minted it or not.

    An operator opening yesterday's data through a file dialog may well
    point at a file this app named, a file copied off another machine, or
    something that is not a recording at all. All three are described here
    rather than only the first: a name matching this session's rule
    contributes its index, label, and start time, and anything else falls
    back to the file's stem and modification time.

    Parameters
    ----------
    path : os.PathLike[str] | str
        The file to describe. It need not exist, open, or be HDF5.

    Returns
    -------
    Recording
        What can be told about the file without reading its frames —
        including :attr:`Recording.readable` and
        :attr:`Recording.finalized`, which is the whole point of asking
        before loading.
    """
    return _describe_path(Path(path))


def load_recording(
    path: os.PathLike[str] | str,
    *,
    budget_bytes: int = _DEFAULT_LOAD_BUDGET_BYTES,
    progress: Callable[[int], None] | None = None,
) -> LoadedRecording:
    """
    Read a recording's frames back into memory as a stack.

    Reads through :func:`miainwoodpecker.storage.nexus.read_series`, not a
    second reader of its own — which is also why this works on a file whose
    writer was abandoned before it created ``/entry/data`` (see this
    module's docstring).

    The axis calibration is read too, from a finalized file, so what comes
    back is enough to *analyze* and not only to display — see
    :attr:`LoadedRecording.frames`, which is what lets the viewer analyze a
    file it has opened without reading it a second time. That is a second
    ``h5py`` open but not a second read of the frames: the axis datasets
    are ``height + width`` float64 values against the recording's tens of
    megabytes, and it is skipped entirely for an unfinalized file, which
    has no axes to read.

    Parameters
    ----------
    path : os.PathLike[str] | str
        The NeXus file to read.
    budget_bytes : int
        Stop after the frames read would exceed this many bytes, reporting
        :attr:`LoadedRecording.truncated`. At least one frame is always
        read, however large.
    progress : Callable[[int], None] | None
        Called with the running frame count as frames arrive, so a caller
        can show progress on a long read.

    Returns
    -------
    LoadedRecording
        The frame stack, its elapsed times, and what the file is.

    Raises
    ------
    RecordingReadError
        If the file does not open as HDF5 at all (a hard-killed
        acquisition), or opens but holds no frames. Both are states an
        operator can really produce, so both get a sentence they can act
        on rather than an h5py traceback.
    """
    target = Path(path)
    described = _describe_path(target)
    if not described.readable:
        msg = (
            f"{target.name} does not open as an HDF5 file. A recording whose "
            f"process was killed outright leaves exactly this (HDF5 buffers "
            f"its object headers, so the container never becomes valid); the "
            f"frames in it are not recoverable."
        )
        raise RecordingReadError(msg)

    frames: list[np.ndarray] = []
    times: list[float] = []
    used = 0
    truncated = False
    calibration: FrameCalibration | None = None
    try:
        if described.finalized:
            calibration = read_calibration(target)
        for data, elapsed in read_series(target):
            if frames and used + data.nbytes > budget_bytes:
                truncated = True
                break
            frames.append(np.asarray(data))
            times.append(elapsed)
            used += data.nbytes
            if progress is not None:
                progress(len(frames))
    except OSError as exc:
        msg = f"{target.name} could not be read: {exc}"
        raise RecordingReadError(msg) from exc

    if not frames:
        msg = (
            f"{target.name} opens but holds no frames — the acquisition that "
            f"reserved it produced none."
        )
        raise RecordingReadError(msg)

    return LoadedRecording(
        recording=described,
        data=np.stack(frames),
        frame_times=tuple(times),
        truncated=truncated,
        calibration=calibration,
    )


def read_session_context(path: os.PathLike[str] | str) -> dict[str, str]:
    """
    Read back the session context a recording was made in.

    Reads the real NeXus groups first — ``NXsample``, ``NXuser``,
    ``NXnote`` — and falls back to the ``session_``-prefixed keys in the
    vendor-metadata JSON blob.

    That order is the correction. This module writes its context to both,
    and it used to read back *only* the blob — so the groups whose whole
    justification is "this is where standard tooling looks" were
    write-only for the project that wrote them. Worse, the blob is the
    copy that disappears exactly when the file is damaged: it holds the
    first frame's metadata, written in ``close()``, so a recording with no
    frames or an unfinalized writer returned ``{}`` while the ``NXuser``
    group sitting in the same file had the answer. The blob is still read
    because it carries fields NeXus has no home for (``label``, ``root``,
    and the per-recording ``note``), and because files written before the
    writer grew ``sample=``/``user=``/``notes=`` have only that.

    Parameters
    ----------
    path : os.PathLike[str] | str
        A NeXus file written through :meth:`Session.record`.

    Returns
    -------
    dict[str, str]
        The context keys (``operator``, ``sample``, ``notes``, ``label``,
        ``root``, and ``note`` if the recording had one of its own). Empty
        only for a file written without a session at all.
    """
    with h5py.File(path, "r") as handle:
        context = _context_from_nexus_groups(handle)
        dataset = handle.get(layout.VENDOR_METADATA)
        raw = None if dataset is None else dataset[()]
    if raw is not None:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        stored = json.loads(text)
        for key, value in stored.items():
            if key.startswith(_CONTEXT_PREFIX) and isinstance(value, str):
                # The groups win where both have it: they are what the
                # file actually declares to a NeXus reader.
                context.setdefault(key.removeprefix(_CONTEXT_PREFIX), value)
    return context


def _read_text(group: object, field: str) -> str | None:
    """
    Return a NeXus text field as ``str``, or None if it is absent.

    Parameters
    ----------
    group : object
        An open ``h5py.File`` to read the field from.
    field : str
        Path to the field, relative to the file root.

    Returns
    -------
    str | None
        The decoded text, or None when the field is not there.
    """
    dataset = group.get(field)
    if dataset is None:
        return None
    raw = dataset[()]
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


def _context_from_nexus_groups(handle: object) -> dict[str, str]:
    """
    Read session context from the NXsample, NXuser, and NXinstrument groups.

    **Not ``NXnote``, and that turned out to be the interesting part.**
    ``sample`` and ``operator`` reach their groups verbatim
    (``/entry/sample/name``, ``/entry/user/name``), so reading them back
    is a genuine round trip. The note field is different by design:
    :func:`_compose_notes` merges the session's standing notes with this
    recording's own and prefixes each with its scope, precisely so a
    reader can tell them apart. That makes ``NXnote/description`` a
    *rendering* of two fields rather than either one of them, and
    recovering ``notes`` from it would hand back ``"session: grid 3"``
    where the session holds ``"grid 3"``.

    So the raw notes still come from the JSON blob, which stores the two
    scopes separately. Worth stating rather than leaving as a silent
    omission: it is the reason this function does not simply read every
    group the writer emits, and a future reader who "fixes" the apparent
    gap would reintroduce the bug.

    **Four of the seven context fields, for a related reason.** ``site``
    and ``task`` have no NeXus field that means what they mean —
    ``NXuser/affiliation`` is who the *operator* belongs to, not where the
    microscope is — and writing them into an approximate one would be a
    confidently wrong claim in a standard field, which this project
    already argues is worse than an admitted absence. They stay in the
    JSON blob, and the caller's fallback path reads them from there.

    Parameters
    ----------
    handle : object
        An open ``h5py.File``.

    Returns
    -------
    dict[str, str]
        Whichever of ``sample``, ``sample_area``, ``operator``, and
        ``instrument`` the file declares.
    """
    found: dict[str, str] = {}
    for key, field in (
        ("sample", f"{layout.SAMPLE_GROUP}/name"),
        ("sample_area", f"{layout.SAMPLE_GROUP}/description"),
        ("operator", f"{layout.USER_GROUP}/name"),
        ("instrument", layout.INSTRUMENT_NAME),
    ):
        value = _read_text(handle, field)
        if value:
            found[key] = value
    return found


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


def annotate(path: os.PathLike[str] | str, note: str) -> str:
    """
    Add a note to a recording that is already on disk.

    The gap this closes: notes could only be attached at acquisition time,
    from the session's standing notes and the per-recording field. But the
    thing worth writing down is often only known afterwards — that a scan
    drifted, that a sample charged, that this is the one to keep — and an
    operator who cannot record that goes back to a paper notebook, which is
    the habit this project exists to replace.

    Appended, never overwritten, and labelled with its own scope and the
    time it was added, for the same reason :func:`_compose_notes` labels
    the other two: a reader has to be able to tell an observation made
    during the shift from one added a week later. The original acquisition
    note is therefore always still there.

    Writes into the file's real ``NXnote`` at ``/entry/notes``, the same
    place :class:`~miainwoodpecker.storage.nexus.NexusWriter` puts the
    acquisition note, so nothing needs to know whether a note was written
    at acquisition time or added afterwards.

    Parameters
    ----------
    path : os.PathLike[str] | str
        The recording to annotate.
    note : str
        The text to add. Blank text is refused rather than writing an
        empty paragraph.

    Returns
    -------
    str
        The recording's full note text after the addition.

    Raises
    ------
    RecordingReadError
        If the note is blank, or the file cannot be opened for writing -
        which for a hard-killed recording is the expected answer, since
        that file has no readable container at all.
    """
    text = note.strip()
    if not text:
        msg = "an empty note adds nothing; type something to record it"
        raise RecordingReadError(msg)
    stamp = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    addition = f"added {stamp}: {text}"
    try:
        with h5py.File(path, "r+") as handle:
            entry = handle.require_group("entry")
            entry.attrs.setdefault("NX_class", "NXentry")
            notes = entry.require_group("notes")
            notes.attrs.setdefault("NX_class", "NXnote")
            existing = notes.get("description")
            previous = _as_text(existing[()]) if existing is not None else ""
            combined = f"{previous}\n\n{addition}" if previous else addition
            # Recreated rather than resized: h5py's variable-length string
            # datasets are fixed-shape scalars, so there is nothing to grow.
            if existing is not None:
                del notes["description"]
            notes["description"] = combined
    except OSError as exc:
        msg = f"cannot annotate {Path(path).name}: {exc}"
        raise RecordingReadError(msg) from exc
    return combined


def find_recordings(base: os.PathLike[str] | str) -> list[Recording]:
    """
    List recordings across every session directory under a base directory.

    :meth:`Session.recordings` answers "what is in *this* session"; this
    answers "where is that scan from last Tuesday", which previously meant
    leaving the application for a file browser.

    Deliberately a directory walk rather than an index or a database. The
    filesystem is already the index (see this module's docstring), and a
    catalogue would be a second source of truth to keep in sync with the
    thing it describes - for a directory of files an operator also moves,
    renames, and deletes by hand, that trade is not worth making. One
    level deep, matching how sessions are laid out by
    :func:`default_root`: a base holding one directory per session, plus
    any loose recordings in the base itself.

    Parameters
    ----------
    base : os.PathLike[str] | str
        The directory holding session directories.

    Returns
    -------
    list[Recording]
        Every recording found, newest first, so the answer to "what did I
        just take" is at the top. Empty if the base does not exist, rather
        than raising: an operator who has not recorded anything yet has an
        empty list, not an error.
    """
    root = Path(base)
    if not root.is_dir():
        return []
    directories = [root, *(child for child in root.iterdir() if child.is_dir())]
    found = [
        described
        for directory in directories
        for candidate in sorted(directory.glob(f"*{_SUFFIX}"))
        if (described := _describe(candidate)) is not None
    ]
    return sorted(found, key=lambda recording: recording.started_at, reverse=True)


def free_space(path: os.PathLike[str] | str) -> int:
    """
    Return the bytes free on the filesystem holding a path.

    Parameters
    ----------
    path : os.PathLike[str] | str
        Any path on the filesystem of interest. The nearest existing
        ancestor is used, so this answers for a session directory that has
        not been created yet.

    Returns
    -------
    int
        Free bytes, or ``0`` if the filesystem cannot be interrogated -
        reported to the operator as "unknown" rather than mistaken for
        "full".
    """
    candidate = Path(path).resolve()
    for target in (candidate, *candidate.parents):
        if target.exists():
            try:
                return shutil.disk_usage(target).free
            except OSError:  # pragma: no cover - platform dependent
                return 0
    return 0  # pragma: no cover - a resolved path always has an existing parent


def estimate_size(
    frame_shape: tuple[int, ...],
    frame_count: int,
    *,
    dtype: npt.DTypeLike = np.float32,
) -> int:
    """
    Estimate what a recording of this shape will occupy, before running it.

    An upper bound, deliberately: it is the uncompressed size, while the
    writer's default gzip+shuffle measured roughly a 10% saving on scan
    data (§5 Phase 3). Erring high is the useful direction for a
    free-space check - a warning that does not fire is worse than one that
    fires slightly early - and compression ratio depends on the data,
    which is not known until it has been acquired.

    Parameters
    ----------
    frame_shape : tuple[int, ...]
        Shape of one frame.
    frame_count : int
        Number of frames to be recorded.
    dtype : npt.DTypeLike
        Element type the frames will be stored as.

    Returns
    -------
    int
        Estimated bytes.
    """
    return int(np.prod(frame_shape)) * frame_count * np.dtype(dtype).itemsize


def format_bytes(count: int) -> str:
    """
    Render a byte count the way an operator reads it.

    Parameters
    ----------
    count : int
        Bytes.

    Returns
    -------
    str
        A short human-readable size, e.g. ``"29.1 MB"``.
    """
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < _BYTES_PER_UNIT or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= _BYTES_PER_UNIT
    return f"{size:.1f} TB"  # pragma: no cover - unreachable, GB returns first


def _as_text(raw: object) -> str:
    """Decode an HDF5 string value, which may come back as bytes."""
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


def _now() -> datetime.datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.datetime.now(tz=datetime.UTC)


def _slugify(label: str) -> str:
    """Reduce a label to lowercase alphanumerics and hyphens for a filename."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return slug or "acquisition"


def _compose_notes(session_notes: str, note: str | None) -> str | None:
    """
    Combine the session's standing notes and this recording's own note.

    ``NexusWriter`` offers one ``NXnote`` with one ``description`` field, so
    the two scopes share it and each is labelled with which it is. The
    labels are always written, even when only one scope has text: an
    unlabelled paragraph would leave a reader unable to tell a shift-long
    observation from a statement about this one file, which is the whole
    reason for having two fields.

    Parameters
    ----------
    session_notes : str
        The session's standing notes; may be many lines.
    note : str | None
        This recording's own note, if any.

    Returns
    -------
    str | None
        The combined note, or None if neither scope has anything to say (so
        the writer creates no empty ``NXnote`` group).
    """
    parts = [
        f"{scope}: {text.strip()}"
        for scope, text in (("session", session_notes), ("recording", note or ""))
        if text.strip()
    ]
    return "\n\n".join(parts) or None


def _describe(path: Path) -> Recording | None:
    """Describe a session file, or return None if this session did not mint it."""
    match = _NAME_PATTERN.match(path.name)
    if match is None:
        return None
    return _describe_path(path)


def _describe_path(
    path: Path,
    *,
    index: int | None = None,
    label: str | None = None,
    started_at: datetime.datetime | None = None,
) -> Recording:
    """
    Describe a file, taking its identity from its name unless told otherwise.

    Parameters
    ----------
    path : Path
        The file to describe.
    index : int | None
        Sequence number, when the caller already minted it.
    label : str | None
        Label slug, when the caller already minted it.
    started_at : datetime.datetime | None
        Start time, when the caller already minted it.

    Returns
    -------
    Recording
        The description, with frame count and readability read from disk.
    """
    match = _NAME_PATTERN.match(path.name)
    if match is not None:
        named_index = int(match["index"])
        named_label = match["label"]
        named_start = datetime.datetime.strptime(
            match["stamp"], _STAMP_FORMAT
        ).replace(tzinfo=datetime.UTC)
    else:
        # A file from somewhere else: index 0 says "not part of a session's
        # numbering" rather than pretending to a position in one.
        named_index = 0
        named_label = path.stem
        named_start = _modified_at(path)
    frame_count, readable, finalized = _inspect(path)
    return Recording(
        path=path,
        index=named_index if index is None else index,
        label=named_label if label is None else label,
        started_at=named_start if started_at is None else started_at,
        frame_count=frame_count,
        readable=readable,
        finalized=finalized,
    )


def _modified_at(path: Path) -> datetime.datetime:
    """Return a file's modification time as aware UTC, or now if it has none."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return _now()
    return datetime.datetime.fromtimestamp(stamp, tz=datetime.UTC)


def _is_locked(error: OSError) -> bool:
    """
    Return whether an open failed because the file is in use, not broken.

    HDF5 reports a file locked by a concurrent writer as a plain
    ``OSError`` with the reason in its message rather than as a distinct
    exception type or ``errno``, so this matches on the text the library
    actually produces. Matching text is unlovely; the alternative is
    calling a live acquisition damaged, which is worse. A false negative
    just restores the old behaviour, so the failure mode is the one that
    was already there.

    Parameters
    ----------
    error : OSError
        The exception ``h5py.File`` raised.

    Returns
    -------
    bool
        True when the message names a locking or concurrent-access
        problem.
    """
    text = str(error).lower()
    return any(
        marker in text
        for marker in ("unable to lock", "file is already open", "resource temporarily")
    )


def _inspect(path: Path) -> tuple[int, bool, bool]:
    """
    Return a file's frame count, whether it opens, and whether it was finalized.

    Reads the frame count from ``/entry/instrument/detector``, which is
    where the frames are appended, and takes the presence of ``/entry/data``
    as the finalized flag, because ``NexusWriter.close()`` is what creates
    it. Measured, not assumed: an abandoned writer's file reports
    ``(3, True, False)`` here and every one of those three frames reads back
    through ``read_series``.

    Parameters
    ----------
    path : Path
        The file to inspect.

    Returns
    -------
    tuple[int, bool, bool]
        Frame count, whether the file opens as HDF5, and whether its writer
        finalized it.
    """
    try:
        with h5py.File(path, "r") as handle:
            dataset = handle.get(layout.DETECTOR_DATA)
            count = 0 if dataset is None else int(dataset.shape[0])
            return count, True, layout.NXDATA_GROUP in handle
    except FileNotFoundError:
        # Vanished between the listing and the open.
        return 0, False, False
    except OSError as error:
        if _is_locked(error):
            # Being written right now, by a RecordingJob in this process or
            # another one pointed at the same directory. HDF5 refuses the
            # read rather than the file being broken, and reporting that as
            # "damaged - does not open" would tell an operator their
            # in-progress acquisition had failed. Treated as an
            # unfinalized-but-live recording, which is what it is.
            return 0, True, False
        # A reserved-but-unwritten placeholder, or a write killed hard
        # enough to lose the HDF5 container. Both are real states an
        # operator can produce, so report them instead of raising.
        return 0, False, False
    except KeyError:
        return 0, False, False
