"""
Write acquired frames to NeXus-structured HDF5.

NeXus is a *convention over HDF5* — typed groups (``NX_class``), a
``signal``/``axes`` description of what to plot, and ``units`` on every
physical quantity — so writing it needs only ``h5py``, not a conversion
framework. That keeps the storage layer thin while producing files any
NeXus-aware tool can open, which is the whole point of not inventing
another bespoke project format (see docs/migration-plan.md, §3).

Writes stream frame-by-frame into a resizable, chunked dataset, so a long
acquisition is persisted as it happens rather than buffered in memory.

Scope of the compliance claim, now actually checked
---------------------------------------------------
Earlier versions of this module declared ``definition = "NXem"``
unconditionally, to state intent, while noting the claim was unverified.
It has now been verified with ``pynxtools`` (see
``scripts/validate_nexus_schema.py``, wired into CI), and the answer was
**no**: a plain recording is *not* valid NXem. The whole gap is one
required group, ``NXem``'s ``sampleID`` — an ``NXsample`` group carrying
``is_simulation``, ``preparation_date``, and ``atom_types``. Every other
required concept (``definition``, ``start_time``) and every class,
``signal``/``axes`` hint, and ``units`` attribute this writer produces
already satisfies the schema; adding just that group flips the same file
to valid.

Those three fields are facts about the physical specimen — which elements
it contains, when it was prepared, whether it is real. No device API can
supply them, and this writer will not invent them: a file that lies about
its provenance is worse than one that declines to claim a definition. So
the default is now to **claim nothing** rather than claim NXem falsely.
Pass ``sample=...`` together with ``definition="NXem"`` — the operator or
ELN knows those values — and the file is genuinely, verifiably NXem.
Without them the file is still a well-formed NeXus file conforming to the
base classes (``NXroot``/``NXentry``/``NXinstrument``/``NXdetector``/
``NXdata``), independently confirmed loadable and plottable by
``nexusformat``; it simply makes no application-definition claim.

``user`` and ``notes`` write the other two pieces of session context into
their real NeXus homes — ``NXuser`` at ``/entry/user``, ``NXnote`` at
``/entry/notes`` — instead of burying them in this module's
vendor-metadata JSON blob, which is where
:mod:`miainwoodpecker.storage.session` had to put them while the writer
had no fields for them. Both are *optional* in NXem (``userID``/``noteID``
are ``minOccurs="0"``, unlike ``sampleID``'s ``minOccurs="1"``), so
neither was ever part of the required-field gap; they are here for
honesty about where session context belongs, not to satisfy the schema.

Deliberately written with ``h5py`` alone, not ``pynxtools-em``: NeXus is a
*convention over HDF5*, and ``pynxtools-em`` is a vendor-format→NXem
reader/converter that pulls ~70 packages to supply, for our purposes, a
schema convention. ``pynxtools`` therefore appears only in the validation
environment, never in the runtime — which is where the migration plan's
Phase 3 note says this belongs.

Compression defaults are measured, not guessed
----------------------------------------------
``scripts/nexus_compression_benchmark.py`` measures ratio, write time, and
read-back time on real simulator frames across gzip, lzf, blosc2,
bitshuffle, and zstd, with and without HDF5's byte-shuffle filter. The
finding that sets the default here: **byte shuffle is what rescues float
data from a generic compressor, and it costs nothing.** Shuffling
transposes the array into byte planes, so the like-significance bytes of
adjacent floats sit together - the low-entropy sign/exponent plane
becomes compressible runs, and the noisy mantissa bytes are quarantined
where they can only fail to compress instead of poisoning every match
around them.

Adding ``shuffle=True`` to the gzip level 4 that was already the default
is **strictly better on all three axes, on every dataset measured** -
smaller, faster to write, faster to read:

===================  ==============  ===================
dataset              gzip-4 (old)    gzip-4 + shuffle
===================  ==============  ===================
scan 1024x1024 f64   0.957x          0.866x
EELS 256x1024 f32    0.336x          0.233x
Ronchigram 2048 f32  0.694x          0.532x
===================  ==============  ===================

...with Ronchigram write time dropping 747 -> 374 ms/frame and read-back
327 -> 192 ms, because deflate does less work on a stream that actually
has structure. There is no trade-off to weigh, so it is simply the
default; ``compression="gzip"`` keeps working and now means gzip+shuffle.

Blosc2 with zstd is a further step - marginally better ratios and much
faster (Ronchigram 84 ms/frame write, 82 ms read) - but it is a *plugin*
codec: a file written with it cannot be opened at all without
``hdf5plugin`` installed in the reading environment. That is a real
interoperability cost for a project whose entire storage argument (§3) is
"follow a documented format so other tools can read it," and the readers
in question include ``nexusformat``, HyperSpy, LiberTEM, and py4DSTEM.
So it stays opt-in - ``compression=hdf5plugin.Blosc2(cname="zstd")``,
with ``hdf5plugin`` an optional extra - rather than becoming the default
and quietly making every file plugin-dependent for a few percent.

Axis calibration is supplied per acquisition, never invented
------------------------------------------------------------
Frames used to get real axes in exactly one case — a scan reporting
``fov_nm`` — and an honest ``units = "pixel"`` otherwise, which meant every
camera frame this project wrote carried no physical axis at all
(docs/migration-plan.md, §7). :mod:`miainwoodpecker.storage.calibration`
now models that properly, **per axis** (an EELS frame's dispersive
direction is energy and the other direction is not) and **per
acquisition** (the same camera is reciprocal space in one microscope mode
and something else in another). Pass ``calibration=`` here, or attach it to
a frame's ``metadata`` under ``"calibration"`` — the same route ``fov_nm``
already travels. Both paths are optional and the ``fov_nm`` and
``"pixel"`` behaviours are unchanged, so nothing that already writes files
has to change.

What goes into the file is only what NeXus defines: ``units`` and
``long_name`` on each ``NXdata`` ``AXISNAME`` field. NeXus has no attribute
for "what kind of axis is this", so none is invented — the unit string
carries it (``nm`` -> ``NX_LENGTH``, ``1/nm`` -> ``NX_WAVENUMBER``, ``eV``
-> ``NX_ENERGY``, ``mrad`` -> ``NX_ANGLE``), which is exactly how
``NXimage`` and ``NXspectrum`` distinguish real-space, reciprocal-space,
and energy axes. See the calibration module for the measurements behind the
unit spellings, including the one that matters: ``pynxtools`` accepts
``"1/nm"`` for ``NX_WAVENUMBER`` and rejects ``"nm-1"``.

Why ``dtype`` exists, and why it is not the default
---------------------------------------------------
Storing ``float64`` scan frames as ``float32`` is a **2.3x** size win -
much bigger than any codec choice - because it halves the logical bytes
*and* compresses slightly better afterwards (0.822x vs 0.866x at
1024x1024): 32.1MB stored today becomes 13.8MB. And the same benchmark
shows the precision that buys it is not physically real. The simulator's
scan pipeline builds its sample array as ``float32`` and annotates
``generate_scan_data`` as returning ``NDArray[numpy.float32]``; the
``float64`` this project receives comes purely from numpy promoting
``float32 + numpy.random.randn(...)``, since ``randn`` returns
``float64``. A ``float32`` round trip of a real frame loses at most
1.19e-07 absolute, which is **1.55e-07 of the frame's own noise standard
deviation** - seven orders of magnitude below the signal's own
uncertainty.

That still does not make it this module's decision. Downcasting is lossy
and irreversible, the storage layer is handed an array rather than a
statement about its precision, and no real-hardware dtype has been
validated yet (§2's open item). A writer that quietly narrowed what it
was given would be asserting something about provenance it cannot know.
The correct fix is upstream - stop the accidental promotion where it
happens - so ``dtype`` is offered explicitly, documented, and off by
default.
"""

from __future__ import annotations

import datetime
import json
import typing

import h5py

from miainwoodpecker.devices.interface import HIGH_TENSION_V_KEY
from miainwoodpecker.storage import layout
from miainwoodpecker.storage.calibration import (
    AXIS_NAMES,
    AxisCalibration,
    FrameCalibration,
    axis_kind_for_units,
    resolve_frame_calibration,
)

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Iterable, Iterator, Mapping

    import numpy as np
    import numpy.typing as npt

    from miainwoodpecker.devices.interface import Frame

_PROGRAM_NAME = "miainwoodpecker"
_DEFAULT_COMPRESSION = "gzip"
_DEFAULT_COMPRESSION_LEVEL = 4
# HDF5's own byte-shuffle filter only makes sense in front of HDF5's own
# built-in compressors. Blosc2/bitshuffle shuffle internally, so stacking
# this filter ahead of them costs a pass over the data and buys nothing.
_SHUFFLE_BENEFITING_FILTERS = frozenset({"gzip", "lzf"})
# The NXdata layout this writer builds names exactly two frame axes, so a
# frame is 2D. Enforced at append() rather than discovered at close().
_FRAME_RANK = 2
# Flush after every frame by default. Measured across the whole codec sweep
# as within run-to-run noise, and it is what converts a SIGKILL mid-recording
# from "the file does not open at all" into "short but readable" - so the
# worst case an operator can hit is losing the frame in flight rather than
# the whole session. See NexusWriter.flush.
_DEFAULT_FLUSH_EVERY = 1

CompressionSpec = typing.Union[str, "Mapping[str, typing.Any]", None]
"""
What :class:`NexusWriter` accepts for ``compression``.

Either an h5py built-in filter name (``"gzip"``, ``"lzf"``), ``None`` for
no compression, or a mapping of ``create_dataset`` keyword arguments —
which is exactly what ``hdf5plugin``'s filter objects
(``hdf5plugin.Blosc2(...)``, ``hdf5plugin.Bitshuffle(...)``) are, so they
can be passed straight through.
"""


def _iso(timestamp: datetime.datetime) -> str:
    """Return an ISO 8601 string, as NeXus requires for date/time fields."""
    return timestamp.isoformat()


def _json_default(value: object) -> str:
    """Stringify values that are not natively JSON serializable."""
    return str(value)


class NexusWriter:
    """
    Stream frames into a NeXus-structured HDF5 file.

    Use as a context manager; the file is finalized (end time, axis
    calibration, plotting hints) on exit even if the acquisition raised.

    Parameters
    ----------
    path : os.PathLike[str] | str
        Destination HDF5 file. Overwritten if it exists.
    title : str
        Human-readable title stored at ``/entry/title``.
    definition : str | None
        NeXus application definition the file claims to conform to.
        ``None`` (the default) writes no ``/entry/definition`` field and so
        claims nothing — the honest default, since the definition this
        project targets, ``NXem``, additionally requires specimen metadata
        only the operator can supply. Pass ``"NXem"`` *together with*
        ``sample`` to make the claim truthfully; see the module docstring.
    sample : Mapping[str, object] | None
        Fields for an ``NXsample`` group at ``/entry/sample``, written
        verbatim. ``NXem`` requires ``is_simulation`` (bool),
        ``preparation_date`` (ISO 8601), and ``atom_types``
        (comma-separated element symbols).
    user : Mapping[str, object] | None
        Fields for an ``NXuser`` group at ``/entry/user``, written
        verbatim — ``name``, ``affiliation``, ``email``, ``orcid`` are the
        base class's own fields. Optional in ``NXem``, unlike ``sample``.
    instrument : str | None
        Which microscope this came from, written as
        ``/entry/instrument/name`` — the single field ``NXinstrument``
        defines, and the first question asked of a file a year later.
    notes : str | None
        Free-text session notes, written as ``description`` inside an
        ``NXnote`` group at ``/entry/notes``.
    calibration : FrameCalibration | None
        Per-axis calibration for the frames this writer will be given —
        real space, reciprocal space, energy, or the honest pixel fallback,
        independently per axis. ``None`` (the default) falls back to the
        frames' own ``metadata``: a ``"calibration"`` entry if present, then
        ``"fov_nm"`` as before, then bare pixel indices. See
        :func:`miainwoodpecker.storage.calibration.resolve_frame_calibration`
        for the precedence, and never expect this writer to guess one.
    compression : CompressionSpec
        h5py compression filter for the frame dataset: a built-in filter
        name (``"gzip"``, ``"lzf"``), ``None`` to disable, or a mapping of
        ``create_dataset`` keyword arguments — which is what
        ``hdf5plugin``'s filter objects are, so
        ``compression=hdf5plugin.Blosc2(cname="zstd")`` works. Note a
        plugin codec makes the file unreadable without that plugin
        installed; the default deliberately stays on HDF5 built-ins.
    compression_opts : object | None
        Filter options, e.g. the gzip level. ``None`` uses the gzip
        default level and is the correct value for filters that take no
        options.
    shuffle : bool | None
        Apply HDF5's byte-shuffle filter before compressing. ``None``
        (the default) enables it for the built-in compressors that
        benefit and skips it for codecs that shuffle internally. This is
        the single biggest ratio lever on float frames — see the module
        docstring.
    dtype : npt.DTypeLike | None
        Store frames as this dtype instead of the dtype they arrive in.
        **Lossy if narrower than the incoming data**; opt-in only, and
        never applied on the writer's own initiative. See the module
        docstring for the measured case this exists for.
    """

    def __init__(  # noqa: PLR0913 - keyword-only file-format options, not a call signature
        self,
        path: os.PathLike[str] | str,
        *,
        title: str = "miainwoodpecker acquisition",
        definition: str | None = None,
        sample: Mapping[str, object] | None = None,
        user: Mapping[str, object] | None = None,
        instrument: str | None = None,
        notes: str | None = None,
        calibration: FrameCalibration | None = None,
        compression: CompressionSpec = _DEFAULT_COMPRESSION,
        compression_opts: object | None = None,
        shuffle: bool | None = None,
        dtype: npt.DTypeLike | None = None,
    ) -> None:
        self._path = path
        self._title = title
        self._definition = definition
        self._sample = sample
        self._user = user
        self._instrument = instrument
        self._notes = notes
        self._calibration = calibration
        self._compression = compression
        self._compression_opts = compression_opts
        self._shuffle = shuffle
        self._dtype = dtype
        self._file: h5py.File | None = None
        self._data: h5py.Dataset | None = None
        self._times: h5py.Dataset | None = None
        self._frame_metadata: h5py.Dataset | None = None
        self._count = 0
        self._first_metadata: typing.Mapping[str, typing.Any] | None = None
        self._resolved_calibration: FrameCalibration | None = None
        self._start: datetime.datetime | None = None
        self._frame_zero: datetime.datetime | None = None

    def __enter__(self) -> typing.Self:
        """Create the file skeleton and return the writer."""
        self._start = datetime.datetime.now(tz=datetime.UTC)
        self._file = h5py.File(self._path, "w")
        root = self._file
        root.attrs["NX_class"] = "NXroot"
        # @default chains let NeXus viewers find the plottable data.
        root.attrs["default"] = "entry"

        entry = root.create_group("entry")
        entry.attrs["NX_class"] = "NXentry"
        entry.attrs["default"] = "data"
        if self._definition is not None:
            entry["definition"] = self._definition
        entry["title"] = self._title
        entry["start_time"] = _iso(self._start)
        program = entry.create_dataset("program_name", data=_PROGRAM_NAME)
        program.attrs["version"] = _version()

        # All three are written verbatim: specimen facts, who ran the
        # session, and what they wrote down are the operator's or an ELN's
        # to state, and the writer has no business editing or completing
        # them. NXem's own requirements are documented on the parameters
        # and enforced in CI, not here.
        for group_name, nx_class, fields in (
            ("sample", "NXsample", self._sample),
            ("user", "NXuser", self._user),
            (
                "notes",
                "NXnote",
                None if self._notes is None else {"description": self._notes},
            ),
        ):
            if fields is None:
                continue
            group = entry.create_group(group_name)
            group.attrs["NX_class"] = nx_class
            for name, value in fields.items():
                group[name] = value

        instrument = entry.create_group("instrument")
        instrument.attrs["NX_class"] = "NXinstrument"
        if self._instrument:
            # NXinstrument's single defined field. Written here rather than
            # left to the vendor-metadata blob because "which microscope
            # was this" is the first question asked of a file a year later,
            # and NeXus has a place for the answer.
            instrument["name"] = self._instrument
        detector = instrument.create_group("detector")
        detector.attrs["NX_class"] = "NXdetector"
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Finalize plotting hints and metadata, then close the file."""
        self.close()

    @property
    def frame_count(self) -> int:
        """Return how many frames have been appended so far."""
        return self._count

    def flush(self) -> None:
        """
        Push buffered HDF5 structures out so a killed process leaves a readable file.

        A sibling investigation measured what an interrupted acquisition
        actually leaves behind, and the two failure modes are not equally
        bad. A clean-but-abandoned exit already leaves every appended frame
        on disk, just without the ``NXdata`` group, ``end_time``, or
        metadata that :meth:`close` writes — recoverable, if unfinalized. A
        ``SIGKILL`` mid-acquisition is worse: the file will not open at all
        (``OSError: bad object header version number``), because HDF5's own
        superblock and chunk-index updates are still buffered. Calling this
        after each :meth:`append` was measured to convert the second case
        into the first.

        Deliberately a caller-controlled method rather than automatic
        per-frame behaviour: this writes whole chunks through the filter
        pipeline, so its cost scales with frame size and codec (measured in
        ``scripts/nexus_compression_benchmark.py``), and how much of that a
        given acquisition should pay to bound its worst-case loss is the
        caller's trade to make, not the writer's. No-ops when the writer is
        not open, so it is safe to call unconditionally.
        """
        if self._file is not None:
            self._file.flush()

    def _metadata_group(self) -> h5py.Group:
        """
        Return ``/entry/metadata``, creating the NXcollection if needed.

        Shared by the per-frame column (created at the first append) and
        the first frame's blob (written at close), so whichever runs
        first makes the group and the other joins it.

        Returns
        -------
        h5py.Group
            The ``NXcollection`` holding everything NeXus does not
            otherwise describe.
        """
        assert self._file is not None  # noqa: S101 - guarded by both callers
        entry = self._file["entry"]
        collection = entry.get("metadata")
        if collection is None:
            collection = entry.create_group("metadata")
            collection.attrs["NX_class"] = "NXcollection"
        return collection

    def _filter_kwargs(self) -> dict[str, typing.Any]:
        """
        Build the ``create_dataset`` filter-pipeline keyword arguments.

        Returns
        -------
        dict[str, typing.Any]
            ``compression``/``compression_opts``/``shuffle`` as h5py wants
            them, whether the configured spec is a built-in filter name,
            ``None``, or an ``hdf5plugin`` filter mapping.
        """
        compression = self._compression
        if compression is not None and not isinstance(compression, str):
            # An hdf5plugin filter object: already a mapping of the
            # create_dataset kwargs (compression id + compression_opts).
            kwargs: dict[str, typing.Any] = dict(compression)
        else:
            options = self._compression_opts
            if options is None and compression == _DEFAULT_COMPRESSION:
                options = _DEFAULT_COMPRESSION_LEVEL
            kwargs = {"compression": compression, "compression_opts": options}

        shuffle = self._shuffle
        if shuffle is None:
            shuffle = kwargs.get("compression") in _SHUFFLE_BENEFITING_FILTERS
        kwargs["shuffle"] = shuffle
        return kwargs

    def append(self, frame: Frame) -> None:
        """
        Append one frame, creating the dataset from the first frame's shape.

        Parameters
        ----------
        frame : Frame
            The frame to persist.

        Raises
        ------
        RuntimeError
            If the writer is not open.
        ValueError
            If the frame is not 2D, if its shape or dtype differs from the
            first frame's, or if the first frame's
            ``metadata["calibration"]`` is malformed (an outright wrong
            type there surfaces as a ``TypeError`` instead). These checks
            happen here, on append, rather than in :meth:`close`, so a
            mis-specified acquisition fails before it runs instead of
            after it.
        """
        if self._file is None:
            msg = "NexusWriter is not open; use it as a context manager."
            raise RuntimeError(msg)

        if frame.data.ndim != _FRAME_RANK:
            # Checked here rather than left to fail in close(): the NXdata
            # layout this writer builds is 2D-per-frame throughout (two
            # named axes, a rank-3 signal). A 1D frame used to reach
            # _write_nxdata and raise IndexError *after* the whole
            # acquisition had been appended, and a 3D one silently produced
            # a rank-4 signal described by two axes - a malformed file
            # rather than an error. Frame.data's docstring still allows 1D
            # for binned spectra; storing those needs a layout decision,
            # not a shape guess (docs/architecture-review.md, §1.6).
            msg = (
                f"NexusWriter stores 2D frames; got a {frame.data.ndim}D frame "
                f"of shape {frame.data.shape}"
            )
            raise ValueError(msg)

        if self._data is None:
            self._resolved_calibration = resolve_frame_calibration(
                typing.cast("tuple[int, int]", frame.data.shape),
                calibration=self._calibration,
                metadata=frame.metadata,
            )
            self._frame_zero = frame.timestamp
            detector = self._file["entry/instrument/detector"]
            self._data = detector.create_dataset(
                "data",
                shape=(0, *frame.data.shape),
                maxshape=(None, *frame.data.shape),
                dtype=self._dtype if self._dtype is not None else frame.data.dtype,
                chunks=(1, *frame.data.shape),
                **self._filter_kwargs(),
            )
            self._data.attrs["units"] = "counts"
            self._times = detector.create_dataset(
                "frame_time",
                shape=(0,),
                maxshape=(None,),
                dtype="float64",
            )
            self._times.attrs["units"] = "s"
            # In the NXcollection, not in NXdetector alongside the frames.
            # NXcollection is NeXus's own container for data that no base
            # class or application definition describes, which is exactly
            # what a JSON blob of vendor keys is; putting it in NXdetector
            # instead made files claiming NXem fail validation, since a
            # detector's contents *are* specified. Created here rather
            # than at close() because it has to grow per frame.
            collection = self._metadata_group()
            self._frame_metadata = collection.create_dataset(
                "frame_metadata_json",
                shape=(0,),
                maxshape=(None,),
                dtype=h5py.string_dtype(encoding="utf-8"),
            )
            # A copy, not the caller's mapping: this is held until close(),
            # and a caller reusing one dict across a series would otherwise
            # have its later edits appear in what was written for frame 0.
            self._first_metadata = dict(frame.metadata)
        else:
            if frame.data.shape != self._data.shape[1:]:
                msg = (
                    f"frame shape {frame.data.shape} does not match the first "
                    f"frame's shape {tuple(self._data.shape[1:])}"
                )
                raise ValueError(msg)
            if self._dtype is None and frame.data.dtype != self._data.dtype:
                # Only when this writer adopted the first frame's dtype. An
                # explicit dtype= means the caller asked for a cast, so a
                # differing input dtype is the point rather than an error.
                # Documented as raising since this method was written; h5py
                # would otherwise cast silently, which is exactly the kind of
                # quiet narrowing the dtype= parameter exists to keep opt-in.
                msg = (
                    f"frame dtype {frame.data.dtype} does not match the first "
                    f"frame's dtype {self._data.dtype}; pass dtype= to store "
                    f"a series as one dtype deliberately"
                )
                raise ValueError(msg)

        index = self._count
        self._data.resize(index + 1, axis=0)
        self._data[index] = frame.data
        assert self._times is not None  # noqa: S101 - created alongside _data
        self._times.resize(index + 1, axis=0)
        elapsed = frame.timestamp - typing.cast("datetime.datetime", self._frame_zero)
        self._times[index] = elapsed.total_seconds()
        # Per frame, not once: acquisition.sequence.focal_series varies
        # defocus_nm and requested_defocus_nm frame by frame precisely so a
        # recording says what the instrument did rather than what it was
        # asked to do. Keeping only the first frame's metadata threw that
        # away silently (docs/architecture-review.md, §1.4).
        assert self._frame_metadata is not None  # noqa: S101 - created alongside _data
        self._frame_metadata.resize(index + 1, axis=0)
        self._frame_metadata[index] = json.dumps(
            dict(frame.metadata),
            default=_json_default,
            sort_keys=True,
        )
        self._count += 1

    def close(self) -> None:
        """
        Write plotting hints, axis calibration, and metadata; close the file.

        Finalization runs under ``try``/``finally`` so the file handle is
        released even when a step fails. Without it, a raise partway
        through (a full disk writing ``end_time``, metadata that will not
        serialize) left ``self._file`` open *and* non-``None``, so the
        handle leaked, the file stayed locked, and ``__exit__`` propagated
        with the writer stuck half-finalized. The frames already on disk
        are worth more than the finalization that failed, and an
        unfinalized file is a state this project already handles
        (``Recording.finalized``).
        """
        if self._file is None:
            return
        try:
            entry = self._file["entry"]
            end_time = datetime.datetime.now(tz=datetime.UTC)
            entry["end_time"] = _iso(end_time)

            if self._data is not None:
                self._write_nxdata(entry)
            self._write_source(entry)
            if self._first_metadata:
                # Kept alongside the per-frame column, and kept at all
                # because read_session_context() reads it: the first
                # frame's metadata is where a session's own context lands.
                self._metadata_group()["vendor_metadata_json"] = json.dumps(
                    dict(self._first_metadata),
                    default=_json_default,
                    sort_keys=True,
                )
        finally:
            self._file.close()
            # Reset every piece of per-acquisition state, not just the
            # handles. Leaving _count behind made a reused writer resize to
            # count+1 and write at count, so frames 0..count-1 were HDF5
            # fill values indistinguishable from real data.
            self._file = None
            self._data = None
            self._times = None
            self._frame_metadata = None
            self._count = 0
            self._first_metadata = None
            self._resolved_calibration = None
            self._frame_zero = None

    def _write_source(self, entry: h5py.Group) -> None:
        """
        Record the accelerating voltage where a NeXus reader looks for it.

        Every acquired frame now carries the instrument state it was taken
        under, and all of it is preserved in the per-frame JSON column.
        That column is where this project puts what NeXus has *no* home
        for, and the accelerating voltage is not that: ``NXsource``
        defines ``voltage``, and ``NXinstrument`` documents an
        ``NXsource`` inside it, so leaving it in a JSON blob would hide a
        value from every reader that speaks NeXus rather than this
        project.

        ``NXem``'s own home for this is
        ``measurement/eventID/instrument/ebeam_column/electron_source``.
        Measured against ``pynxtools``: putting an ``NXebeam_column``
        inside this file's ``NXinstrument`` makes the file stop validating
        — ``NXinstrument`` documents no electron column, and NXem's entry
        has no ``instrument`` group at all to move to instead. Reaching
        NXem's path means restructuring the entry around its
        ``measurement``/``event`` hierarchy, which is a bigger change than
        one field justifies. ``NXsource`` is a documented home in both,
        and it validates.

        Written from the first frame, since a recording is one acquisition
        at one high tension; anything that varied is in the per-frame
        column. Omitted entirely when no frame reported a voltage, rather
        than written as zero.

        Parameters
        ----------
        entry : h5py.Group
            The file's ``NXentry`` group.
        """
        voltage = (self._first_metadata or {}).get(HIGH_TENSION_V_KEY)
        if not isinstance(voltage, (int, float)) or isinstance(voltage, bool):
            return
        source = entry["instrument"].create_group("source")
        source.attrs["NX_class"] = "NXsource"
        source["probe"] = "electron"
        dataset = source.create_dataset("voltage", data=float(voltage))
        dataset.attrs["units"] = "V"

    def _write_nxdata(self, entry: h5py.Group) -> None:
        """Create the NXdata group describing how to plot the frame stack."""
        assert self._data is not None  # noqa: S101 - guarded by caller
        height, width = self._data.shape[1], self._data.shape[2]

        data_group = entry.create_group("data")
        data_group.attrs["NX_class"] = "NXdata"
        # Hard-link rather than copy: one array, two access paths.
        data_group["data"] = self._data
        data_group["frame_time"] = self._file["entry/instrument/detector/frame_time"]  # type: ignore[index]

        # Resolved on the first append (so a bad calibration fails early);
        # falls back to the honest pixel model when nobody supplied one.
        calibration = self._resolved_calibration or FrameCalibration.uncalibrated()
        # `units` and `long_name` are the two attributes NXdata itself
        # defines on an AXISNAME field. The axis *kind* is not written -
        # NeXus has no attribute for it, and the unit string carries it.
        for name, length in zip(AXIS_NAMES, (height, width), strict=True):
            axis_calibration = calibration.axis(name)
            axis = data_group.create_dataset(
                name,
                data=axis_calibration.values(length),
            )
            axis.attrs["units"] = axis_calibration.units
            axis.attrs["long_name"] = axis_calibration.long_name

        data_group.attrs["signal"] = "data"
        data_group.attrs["axes"] = ["frame_time", "y", "x"]
        data_group.attrs["frame_time_indices"] = 0
        data_group.attrs["y_indices"] = 1
        data_group.attrs["x_indices"] = 2


def _version() -> str:
    """Return the installed package version, or 'unknown' if unavailable."""
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version(_PROGRAM_NAME)
    except Exception:  # noqa: BLE001 - metadata is best-effort provenance
        return "unknown"


def write_frames(
    path: os.PathLike[str] | str,
    frames: Iterable[Frame],
    *,
    flush_every: int = _DEFAULT_FLUSH_EVERY,
    **kwargs: object,
) -> int:
    """
    Write an iterable of frames to a NeXus HDF5 file.

    Parameters
    ----------
    path : os.PathLike[str] | str
        Destination HDF5 file.
    frames : Iterable[Frame]
        Frames to persist, in acquisition order.
    flush_every : int
        Call :meth:`NexusWriter.flush` after this many frames; ``0`` (or
        less) never flushes. The default of 1 bounds worst-case loss from
        a hard kill to the frame in flight, at a cost measured as within
        run-to-run noise — the writer's flush docstring explains why this
        is the difference between a file that opens and one that does not.
        Raise it for a detector fast enough that the flush shows up in a
        profile, which no measurement here has yet found.
    **kwargs : object
        Passed through to :class:`NexusWriter`.

    Returns
    -------
    int
        The number of frames written.
    """
    with NexusWriter(path, **kwargs) as writer:
        for frame in frames:
            writer.append(frame)
            if flush_every > 0 and writer.frame_count % flush_every == 0:
                writer.flush()
        return writer.frame_count


def _axis_calibration_from_values(
    values: npt.NDArray[np.float64],
    units: str,
) -> AxisCalibration:
    """
    Recover one axis's calibration from the values and units in a file.

    Parameters
    ----------
    values : npt.NDArray[np.float64]
        The axis dataset's values, in acquisition order.
    units : str
        The axis dataset's ``units`` attribute.

    Returns
    -------
    AxisCalibration
        The linear calibration those values encode. A single-sample axis
        carries no spacing information at all, so its scale falls back to
        1.0 — the same fallback the analysis adapters already made.
    """
    kind = axis_kind_for_units(units)
    offset = float(values[0]) if len(values) else 0.0
    scale = float(values[1] - values[0]) if len(values) > 1 else 1.0
    return AxisCalibration(kind, scale, offset, units)


class FrameStack(typing.NamedTuple):
    """
    A recording's frames, their times, and their axis calibration, together.

    What :func:`read_frames` has always returned, given a name so it can be
    *passed on* rather than only unpacked. The Phase 4 analysis adapters
    take one of these as their in-memory input (see
    :func:`miainwoodpecker.analysis.hyperspy_bridge.hyperspy_signal_from_frames`
    and its siblings), which is what lets the viewer analyze frames it has
    already read instead of making the adapter read the file a second time.

    A :class:`typing.NamedTuple` rather than a dataclass for one reason:
    ``data, frame_time, calibration = read_frames(path)`` is how every
    existing caller and test uses that function, and a named tuple keeps
    all of them working unchanged while giving the triple a type the rest
    of the codebase can name in a signature.

    The three fields travel together because *separating* them is the bug
    this exists to prevent: an array without its calibration produces a
    signal whose axes silently claim bare pixels, which is worse than the
    duplicated read it was meant to avoid.

    Attributes
    ----------
    data : np.ndarray
        The ``(frames, height, width)`` stack.
    frame_time : np.ndarray
        Each frame's elapsed time in seconds, in acquisition order.
    calibration : FrameCalibration
        The ``y``/``x`` axis calibration, kind recovered from the units the
        file records.
    """

    data: np.ndarray
    frame_time: np.ndarray
    calibration: FrameCalibration


def read_frames(path: os.PathLike[str] | str) -> FrameStack:
    """
    Read a whole recording — stack, times, and calibration — in one open.

    The reader the Phase 4 analysis adapters share. Each of them used to
    open the file itself for the arrays and then call
    :func:`read_calibration`, which opens it a *second* time; each also
    carried its own copy of the "this recording has no frames" check and
    its message. So the layout was known in four places, the message in
    four, and every analysis paid two opens of a file that can be tens of
    megabytes.

    Reads through :data:`~miainwoodpecker.storage.layout.NXDATA_GROUP`
    rather than the detector group, deliberately: that is the group
    ``close()`` creates, so a recording whose writer was abandoned is
    refused here rather than analyzed as if it were complete. The viewer
    already tells that story in words before it gets this far, and
    :func:`read_series` is the reader that *does* recover such a file.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`NexusWriter`.

    Returns
    -------
    FrameStack
        The ``(frames, height, width)`` stack, the per-frame elapsed
        times in seconds, and the ``y``/``x`` calibration. Unpacks as the
        plain triple it has always been.

    Raises
    ------
    layout.NoFramesError
        If the acquisition produced no frames, so there is no ``NXdata``
        group to read.
    """
    with h5py.File(path, "r") as handle:
        if layout.NXDATA_GROUP not in handle:
            raise layout.NoFramesError(path)
        data_group = handle[layout.NXDATA_GROUP]
        data = data_group["data"][()]
        frame_time = data_group["frame_time"][()]
        calibration = FrameCalibration(
            **{
                name: _axis_calibration_from_values(
                    data_group[name][()],
                    _decoded(data_group[name].attrs["units"]),
                )
                for name in AXIS_NAMES
            },
        )
    return FrameStack(data, frame_time, calibration)


def require_frames(path: os.PathLike[str] | str) -> None:
    """
    Raise unless a file holds an analyzable frame stack.

    For an adapter that hands the path straight to a library rather than
    reading the arrays itself — LiberTEM's ``Context.load`` opens the file
    with its own reader — so it can still refuse an unfinalized recording
    with this project's message instead of the library's much vaguer one.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`NexusWriter`.

    Raises
    ------
    layout.NoFramesError
        If the acquisition produced no frames.
    """
    with h5py.File(path, "r") as handle:
        if layout.NXDATA_GROUP not in handle:
            raise layout.NoFramesError(path)


def read_calibration(path: os.PathLike[str] | str) -> FrameCalibration:
    """
    Read back the per-axis calibration a written file carries.

    The counterpart to what :class:`NexusWriter` writes, and the single
    place the ``units``-to-kind inference lives, so the three Phase 4
    analysis adapters do not each re-derive it. It also gives the LiberTEM
    adapter something to point at: LiberTEM's ``DataSet`` has nowhere to
    put axis calibration, so a caller who needs it alongside a UDF result
    reads it from the file with this.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`NexusWriter`.

    Returns
    -------
    FrameCalibration
        The ``y`` and ``x`` axis calibrations, each with its kind recovered
        from its units.

    Raises
    ------
    layout.NoFramesError
        If the file has no ``NXdata`` group, i.e. it recorded no frames.
        A ``ValueError`` subclass, so existing callers catch it unchanged.
    """
    with h5py.File(path, "r") as handle:
        if layout.NXDATA_GROUP not in handle:
            raise layout.NoFramesError(path)
        data_group = handle[layout.NXDATA_GROUP]
        axes = {
            name: _axis_calibration_from_values(
                data_group[name][()],
                _decoded(data_group[name].attrs["units"]),
            )
            for name in AXIS_NAMES
        }
    return FrameCalibration(**axes)


def _decoded(value: object) -> str:
    """
    Return an HDF5 string attribute as ``str``, whether or not it is bytes.

    Parameters
    ----------
    value : object
        An attribute value read from h5py, which may be ``bytes`` or
        ``str`` depending on how it was written.

    Returns
    -------
    str
        The decoded value.
    """
    return value.decode() if isinstance(value, bytes) else str(value)


def read_series(path: os.PathLike[str] | str) -> Iterator[tuple[np.ndarray, float]]:
    """
    Yield ``(frame_data, elapsed_seconds)`` pairs from a written file.

    Provided so acquisitions can be replayed or verified without pulling
    in a full analysis stack.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`NexusWriter`.

    Yields
    ------
    tuple[np.ndarray, float]
        Each frame's array and its elapsed time in seconds. Yields nothing
        for a file written by an acquisition that produced no frames.
    """
    with h5py.File(path, "r") as handle:
        if layout.DETECTOR_DATA not in handle:
            return
        data = handle[layout.DETECTOR_DATA]
        times = handle[layout.DETECTOR_FRAME_TIME]
        for index in range(data.shape[0]):
            yield data[index], float(times[index])
