"""
Write acquired spectra to NeXus-structured HDF5, and read them back.

The spectrum-side counterpart to :mod:`miainwoodpecker.storage.nexus`,
and a separate writer rather than a mode of that one. The reason is that
the on-disk layouts are genuinely different and NeXus already says how:
a frame stack is ``NXdata`` with a ``(frames, y, x)`` signal called
``data``, and a set of spectra is ``NXspectrum``'s vocabulary, where the
signal is ``intensity``, the energy axis is ``axis_energy`` and is always
the fastest dimension, and the spatial axes of a map are ``axis_j`` (slow)
and ``axis_i`` (fast). Folding both into one class would mean one writer
whose every method branched on which of two formats it was writing.

What NeXus actually specifies, checked rather than guessed
-----------------------------------------------------------
``NXspectrum`` is a real base class shipped with ``pynxtools``, and it
defines exactly the cases this project needs:

- ``spectrum_0d`` — ``intensity(n_energy)`` plus ``axis_energy``. One
  spot spectrum.
- ``stack_0d`` — ``intensity(n_spc, n_energy)`` plus ``indices_group``,
  ``indices_spectrum`` and ``axis_energy``. Several spot spectra.
- ``spectrum_2d`` — ``intensity(n_j, n_i, n_energy)`` plus ``axis_j``,
  ``axis_i`` (both ``NX_LENGTH``) and ``axis_energy`` (``NX_ENERGY``).
  A spectrum image.

Its ``n_energy`` symbol reads "Number of energy bins (**always the
fastest dimension**)", which is where
:class:`~miainwoodpecker.devices.interface.Spectrum`'s energy-last
invariant comes from. It is also, independently, RosettaSciIO's Bruker
HyperMap layout (``height``, ``width``, ``Energy``) and Oxford's H5OINA
``Spectrum`` dataset shape (``size``, ``channels``) — so the format, the
readers, and this project agree, and none of them had to be talked into
it.

**Where it goes, and the measurement that decided it.** ``NXem``, the
application definition this project validates against, documents
``NXspectrum`` only deep inside ``measurement/eventID*/spectrumID*``.
Measured with ``pynxtools``: putting an ``NXspectrum`` group directly in
the ``NXentry`` makes the file **stop validating** — the same result this
project already got for ``NXebeam_column``, and for the same reason. So
the ``NXdata`` goes at ``entry/data``, which ``NXem`` does document and
where a frame recording's plottable data already lives, spelled
throughout in ``NXspectrum``'s field names. Measured again: that file
*does* validate as ``NXem``, with the detector geometry beside it in an
``NXdetector``. ``scripts/validate_nexus_schema.py`` checks both halves,
so neither claim can rot quietly.

Reaching ``NXem``'s own path is possible and was measured too — it needs
``measurement/instrument`` to be an ``NXem_instrument`` carrying
``ebeam_column`` and ``fabrication``, which is the entry restructuring
:mod:`miainwoodpecker.storage.nexus` already declined once for
``NXsource``. Recorded rather than done; see
docs/adapters/spectrum-detectors.md.

Where the detector's own facts go
----------------------------------
Not in a JSON blob. ``NXdetector`` already defines a home for every
number an EDS quantification needs, and using them is what lets a NeXus
reader find them without knowing anything about this project:

===========================  =========================  =================
spectrum metadata            ``NXdetector`` field       units
===========================  =========================  =================
``live_time_s``              ``count_time``             ``s``
``real_time_s``              ``real_time``              ``s``
(derived)                    ``dead_time``              ``s``
``azimuth_deg``              ``azimuthal_angle``        ``deg``
``elevation_deg``            ``polar_angle``            ``deg``
``solid_angle_sr``           ``solid_angle``            ``sr``
``detector_type``            ``type``                   —
``technique``                ``description``            —
===========================  =========================  =================

``count_time`` is NeXus's own name for live time ("elapsed actual
counting time"), which is worth stating because "live time" appears
nowhere in ``NXdetector`` and inventing a field for it would have been
easy. ``energy_resolution_ev`` is the one EDS number ``NXdetector`` has
no field for, so it stays in the per-spectrum JSON with everything else
NeXus does not describe — the same rule
:mod:`miainwoodpecker.storage.nexus` follows.

Compression, flushing and dtype
--------------------------------
Inherited wholesale from :class:`~miainwoodpecker.storage.nexus.NexusWriter`
rather than re-argued: gzip plus HDF5's byte shuffle, flush after every
appended spectrum, and no dtype narrowing on the writer's initiative. The
one thing worth noting is that a spectrum image is *larger* than the
frame stacks those defaults were measured on — a 256x256 map of 4096
uint32 channels is a gigabyte — so the chunking here is one spectrum per
chunk for a series and one row of spectra per chunk for a map, which
keeps a chunk in the same size range the benchmark covered.
"""

from __future__ import annotations

import datetime
import json
import typing

import h5py
import numpy as np

from miainwoodpecker.devices.interface import HIGH_TENSION_V_KEY, Spectrum
from miainwoodpecker.storage import layout
from miainwoodpecker.storage.calibration import (
    METADATA_KEY,
    AxisCalibration,
    AxisKind,
    FrameCalibration,
    resolve_axis_calibration,
)

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Iterable, Mapping

    import numpy.typing as npt

    from miainwoodpecker.devices.interface import Frame

_PROGRAM_NAME = "miainwoodpecker"
_DEFAULT_COMPRESSION = "gzip"
_DEFAULT_COMPRESSION_LEVEL = 4
_SHUFFLE_BENEFITING_FILTERS = frozenset({"gzip", "lzf"})
_DEFAULT_FLUSH_EVERY = 1

# NXspectrum's own group names, used as this writer's on-disk mode names so
# nothing has to be translated when reading the NXDL alongside the file.
SERIES_LAYOUT = "stack_0d"
"""
Spot spectra: ``intensity(n_spc, n_energy)``. Used for one as well as many.

``NXspectrum`` also defines ``spectrum_0d`` for exactly one spectrum, and
this writer deliberately does not produce it: making the signal's *rank*
depend on how many spectra an acquisition happened to yield would force
every reader to branch, and would make a recording that stopped after one
read back differently from the same recording that managed two. A stack
of one is honest — ``n_spc`` is 1 — and uniform.
"""

MAP_LAYOUT = "spectrum_2d"
"""One spectrum image: ``intensity(n_j, n_i, n_energy)``."""

# Metadata key to (NXdetector field, units). The one place the spelling of
# the Spectrum metadata vocabulary is known outside the device layer, which
# is the same arrangement `fov_nm`/`fov_size_nm`/`calibration` already have
# in storage.calibration - the alternative was a constant per key in
# interface.py, each with exactly one use.
TECHNIQUE_KEY = "technique"
"""
Spectrum-metadata key naming the kind of spectroscopy, e.g. ``"eds"``.

Named here, unlike the rest of the vocabulary, because two layers now
read it *by name* rather than persisting it whole: this module stamps it
onto a projected camera readout, and
:mod:`miainwoodpecker.analysis.hyperspy_bridge` keys its EELS/EDS
refusals on it. It is written into ``NXdetector``'s ``description``.
"""

EELS_TECHNIQUE = "eels"
"""
The :data:`TECHNIQUE_KEY` value for electron energy-loss spectroscopy.

The one value with a constant, for the same reason: once a camera's
projected readout lands in the same ``NXspectrum`` layout as EDX, this
string is the *only* thing distinguishing the two on disk, and the
writer and the loader agreeing on it letter for letter is what keeps
eXSpy from fitting X-ray lines to electron energy losses.
"""

_DETECTOR_FIELDS: tuple[tuple[str, str, str | None], ...] = (
    ("live_time_s", "count_time", "s"),
    ("real_time_s", "real_time", "s"),
    ("azimuth_deg", "azimuthal_angle", "deg"),
    ("elevation_deg", "polar_angle", "deg"),
    ("solid_angle_sr", "solid_angle", "sr"),
    ("detector_type", "type", None),
    (TECHNIQUE_KEY, "description", None),
)

_ENERGY_UNITS = "eV"
_NAVIGATION_UNITS = "nm"


def _iso(timestamp: datetime.datetime) -> str:
    """Return an ISO 8601 string, as NeXus requires for date/time fields."""
    return timestamp.isoformat()


def _json_default(value: object) -> str:
    """Stringify values that are not natively JSON serializable."""
    return str(value)


def _version() -> str:
    """Return the installed package version, or 'unknown' if unavailable."""
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version(_PROGRAM_NAME)
    except Exception:  # noqa: BLE001 - metadata is best-effort provenance
        return "unknown"


class SpectrumWriter:
    """
    Stream spectra into a NeXus-structured HDF5 file.

    Use as a context manager; the file is finalized (end time, axes,
    plotting hints, detector group) on exit even if the acquisition
    raised — the same contract
    :class:`~miainwoodpecker.storage.nexus.NexusWriter` offers, and worth
    keeping identical so an operator does not have to remember which kind
    of recording behaves which way.

    **What may be appended, and what is refused.** Several spot spectra,
    or exactly one spectrum image. Mixing the two, or appending a second
    map, raises — not from a shape check that happens to fail, but
    because ``NXspectrum`` has a *different* group for a series of maps
    (``stack_2d``) and writing one of those into ``spectrum_2d``'s shape
    would produce a file that describes something it is not. The error
    names the group a future version would need.

    Parameters
    ----------
    path : os.PathLike[str] | str
        Destination HDF5 file. Overwritten if it exists.
    title : str
        Human-readable title stored at ``/entry/title``.
    definition : str | None
        NeXus application definition the file claims to conform to.
        ``None`` (the default) claims nothing, for the reason
        :class:`~miainwoodpecker.storage.nexus.NexusWriter` documents at
        length: ``NXem`` additionally requires specimen metadata only the
        operator can supply, and a file that lies about its provenance is
        worse than one that declines to claim a definition.
    sample : Mapping[str, object] | None
        Fields for an ``NXsample`` group at ``/entry/sample``.
    user : Mapping[str, object] | None
        Fields for an ``NXuser`` group at ``/entry/user``.
    instrument : str | None
        Which microscope this came from, written as
        ``/entry/instrument/name``.
    notes : str | None
        Free-text session notes, written as ``description`` inside an
        ``NXnote`` group at ``/entry/notes``.
    navigation : FrameCalibration | None
        Explicit spatial calibration for a spectrum image's ``axis_j``
        and ``axis_i``. ``None`` (the default) reads the scan geometry
        from the first spectrum's ``metadata["fov_size_nm"]``, the same
        route a scanned frame's calibration travels, and falls back to
        bare pixel indices when nothing said. The energy axis is never
        taken from here — it is on the
        :class:`~miainwoodpecker.devices.interface.Spectrum` itself,
        because a spectrum cannot exist without one.
    compression : object
        h5py compression filter for the intensity dataset; see
        :class:`~miainwoodpecker.storage.nexus.NexusWriter` for the
        measurements behind the default and for why a plugin codec stays
        opt-in.
    compression_opts : object | None
        Filter options, e.g. the gzip level.
    shuffle : bool | None
        Apply HDF5's byte-shuffle filter before compressing. ``None``
        enables it for the built-in compressors that benefit.
    dtype : npt.DTypeLike | None
        Store counts as this dtype instead of the one they arrive in.
        **Lossy if narrower**; opt-in only. Counts are integers, so
        unlike the frame writer's float case there is rarely a reason.
    """

    def __init__(  # noqa: PLR0913 - keyword-only file-format options, not a call signature
        self,
        path: os.PathLike[str] | str,
        *,
        title: str = "miainwoodpecker spectra",
        definition: str | None = None,
        sample: Mapping[str, object] | None = None,
        user: Mapping[str, object] | None = None,
        instrument: str | None = None,
        notes: str | None = None,
        navigation: FrameCalibration | None = None,
        compression: object = _DEFAULT_COMPRESSION,
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
        self._navigation = navigation
        self._compression = compression
        self._compression_opts = compression_opts
        self._shuffle = shuffle
        self._dtype = dtype
        self._file: h5py.File | None = None
        self._data: h5py.Dataset | None = None
        self._metadata_column: h5py.Dataset | None = None
        self._count = 0
        self._layout: str | None = None
        self._energy: AxisCalibration | None = None
        self._first_metadata: dict[str, typing.Any] | None = None
        self._start: datetime.datetime | None = None

    def __enter__(self) -> typing.Self:
        """Create the file skeleton and return the writer."""
        self._start = datetime.datetime.now(tz=datetime.UTC)
        self._file = h5py.File(self._path, "w")
        root = self._file
        root.attrs["NX_class"] = "NXroot"
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
            instrument["name"] = self._instrument
        detector = instrument.create_group("spectrum_detector")
        detector.attrs["NX_class"] = "NXdetector"
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Finalize axes, plotting hints, and metadata, then close the file."""
        self.close()

    @property
    def spectrum_count(self) -> int:
        """Return how many spectra have been appended so far."""
        return self._count

    def flush(self) -> None:
        """
        Push buffered HDF5 structures out so a killed process leaves a readable file.

        Exactly :meth:`~miainwoodpecker.storage.nexus.NexusWriter.flush`'s
        contract and for the measured reason recorded there: without it a
        ``SIGKILL`` mid-acquisition leaves a file that will not open at
        all, and with it the worst case is losing the spectrum in flight.
        No-ops when the writer is not open.
        """
        if self._file is not None:
            self._file.flush()

    def append(self, spectrum: Spectrum) -> None:
        """
        Append one spectrum, creating the dataset from the first one's shape.

        Parameters
        ----------
        spectrum : Spectrum
            The spectrum to persist.

        Raises
        ------
        RuntimeError
            If the writer is not open.

        Notes
        -----
        Propagates :class:`ValueError` from :meth:`_create` and
        :meth:`_check_matches` when this spectrum's channel count, dtype,
        or energy calibration differs from the first one's, or when it
        would need an ``NXspectrum`` layout this writer does not produce
        — a second spectrum image, or a line scan. Those checks happen
        here, on append, rather than in :meth:`close`, so a mis-specified
        acquisition fails before it runs rather than after it.
        """
        if self._file is None:
            msg = "SpectrumWriter is not open; use it as a context manager."
            raise RuntimeError(msg)

        energy = AxisCalibration(
            AxisKind.ENERGY,
            spectrum.energy_scale_ev,
            spectrum.energy_offset_ev,
            _ENERGY_UNITS,
        )
        if self._data is None:
            self._create(spectrum, energy)
        else:
            self._check_matches(spectrum, energy)

        index = self._count
        assert self._data is not None  # noqa: S101 - created just above
        if self._layout == MAP_LAYOUT:
            # A map is written whole: the dataset already has the map's
            # shape, so there is no axis to grow along.
            self._data[...] = spectrum.data
        else:
            self._data.resize(index + 1, axis=0)
            self._data[index] = spectrum.data
        assert self._metadata_column is not None  # noqa: S101 - created alongside _data
        self._metadata_column.resize(index + 1, axis=0)
        self._metadata_column[index] = json.dumps(
            dict(spectrum.metadata),
            default=_json_default,
            sort_keys=True,
        )
        self._count += 1

    def _create(self, spectrum: Spectrum, energy: AxisCalibration) -> None:
        """
        Create the intensity dataset and the metadata column from the first spectrum.

        Parameters
        ----------
        spectrum : Spectrum
            The first appended spectrum, whose rank picks the layout.
        energy : AxisCalibration
            Its energy axis, held for :meth:`close` to write.

        Raises
        ------
        ValueError
            If the spectrum's rank needs an ``NXspectrum`` layout this
            writer does not produce.
        """
        assert self._file is not None  # noqa: S101 - guarded by append
        navigation_rank = spectrum.data.ndim - 1
        if navigation_rank == 0:
            # Which of spectrum_0d/stack_0d it is depends on how many
            # arrive, which is not known yet; close() decides, and the
            # dataset is grown along a leading axis either way.
            self._layout = SERIES_LAYOUT
            shape: tuple[int, ...] = (0, spectrum.channel_count)
            maxshape: tuple[int | None, ...] = (None, spectrum.channel_count)
            chunks: tuple[int, ...] = (1, spectrum.channel_count)
        elif navigation_rank == _MAP_NAVIGATION_RANK:
            self._layout = MAP_LAYOUT
            shape = spectrum.data.shape
            maxshape = spectrum.data.shape
            # One row of spectra per chunk, which keeps a chunk in the
            # size range NexusWriter's codec benchmark actually covered
            # rather than making it the whole gigabyte cube.
            chunks = (1, spectrum.data.shape[1], spectrum.channel_count)
        else:
            msg = (
                f"a {spectrum.data.ndim}D spectrum needs NXspectrum's "
                f"spectrum_1d layout (a line scan), which this writer does "
                f"not produce yet; it writes spectrum_0d, stack_0d and "
                f"spectrum_2d"
            )
            raise ValueError(msg)

        self._energy = energy
        data_group = self._file["entry"].create_group("data")
        data_group.attrs["NX_class"] = "NXdata"
        self._data = data_group.create_dataset(
            "intensity",
            shape=shape,
            maxshape=maxshape,
            dtype=self._dtype if self._dtype is not None else spectrum.data.dtype,
            chunks=chunks,
            **self._filter_kwargs(),
        )
        self._data.attrs["long_name"] = "Counts"
        collection = self._metadata_group()
        self._metadata_column = collection.create_dataset(
            "spectrum_metadata_json",
            shape=(0,),
            maxshape=(None,),
            dtype=h5py.string_dtype(encoding="utf-8"),
        )
        # A copy, not the caller's mapping, for the reason NexusWriter
        # takes one: this is held until close(), and a caller reusing one
        # dict across a series would otherwise have its later edits appear
        # in the detector group.
        self._first_metadata = dict(spectrum.metadata)

    def _check_matches(self, spectrum: Spectrum, energy: AxisCalibration) -> None:
        """
        Reject a spectrum that cannot join the ones already written.

        Parameters
        ----------
        spectrum : Spectrum
            The spectrum being appended.
        energy : AxisCalibration
            Its energy axis.

        Raises
        ------
        ValueError
            If it would change the recording's channel count, dtype,
            energy calibration, or layout.
        """
        assert self._data is not None  # noqa: S101 - guarded by append
        if self._layout == MAP_LAYOUT:
            msg = (
                "this recording holds a spectrum image, and a series of "
                "them is NXspectrum's stack_2d layout, which this writer "
                "does not produce; record each map to its own file"
            )
            raise ValueError(msg)
        if spectrum.data.ndim != 1:
            msg = (
                f"this recording holds spot spectra, so a "
                f"{spectrum.data.ndim}D spectrum cannot join it"
            )
            raise ValueError(msg)
        if spectrum.channel_count != self._data.shape[-1]:
            msg = (
                f"spectrum has {spectrum.channel_count} channels, but this "
                f"recording's are {self._data.shape[-1]}"
            )
            raise ValueError(msg)
        if self._dtype is None and spectrum.data.dtype != self._data.dtype:
            msg = (
                f"spectrum dtype {spectrum.data.dtype} does not match the "
                f"first spectrum's {self._data.dtype}; pass dtype= to store a "
                f"series as one dtype deliberately"
            )
            raise ValueError(msg)
        if energy != self._energy:
            # Not pedantry: the file carries one axis_energy for the whole
            # stack, exactly as NXspectrum's stack_0d does, so a spectrum
            # acquired at a different dispersion would be stored under an
            # energy axis that is not its own.
            msg = (
                f"spectrum's energy axis (scale={energy.scale} eV/channel, "
                f"offset={energy.offset} eV) differs from this recording's "
                f"(scale={self._energy.scale if self._energy else None}, "
                f"offset={self._energy.offset if self._energy else None}); one "
                f"file carries one energy axis for the whole stack"
            )
            raise ValueError(msg)

    def _metadata_group(self) -> h5py.Group:
        """
        Return ``/entry/metadata``, creating the ``NXcollection`` if needed.

        Returns
        -------
        h5py.Group
            The collection holding everything NeXus does not describe.
        """
        assert self._file is not None  # noqa: S101 - guarded by callers
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
            kwargs: dict[str, typing.Any] = dict(
                typing.cast("Mapping[str, typing.Any]", compression),
            )
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

    def close(self) -> None:
        """
        Write the axes, plotting hints, and detector group; close the file.

        Finalization runs under ``try``/``finally`` so the file handle is
        released even when a step fails, for the reason
        :meth:`~miainwoodpecker.storage.nexus.NexusWriter.close` records:
        the spectra already on disk are worth more than the finalization
        that failed.
        """
        if self._file is None:
            return
        try:
            entry = self._file["entry"]
            entry["end_time"] = _iso(datetime.datetime.now(tz=datetime.UTC))
            if self._data is not None:
                self._write_axes(entry)
            self._write_detector(entry)
            self._write_source(entry)
            if self._first_metadata:
                self._metadata_group()["vendor_metadata_json"] = json.dumps(
                    dict(self._first_metadata),
                    default=_json_default,
                    sort_keys=True,
                )
        finally:
            self._file.close()
            self._file = None
            self._data = None
            self._metadata_column = None
            self._count = 0
            self._layout = None
            self._energy = None
            self._first_metadata = None

    def _write_axes(self, entry: h5py.Group) -> None:
        """
        Write ``axis_energy``, any spatial axes, and the ``NXdata`` hints.

        Parameters
        ----------
        entry : h5py.Group
            The file's ``NXentry`` group.
        """
        assert self._data is not None  # noqa: S101 - guarded by caller
        assert self._energy is not None  # noqa: S101 - set alongside _data
        data_group = entry["data"]
        channels = int(self._data.shape[-1])

        energy_axis = data_group.create_dataset(
            "axis_energy",
            data=self._energy.values(channels),
        )
        energy_axis.attrs["units"] = self._energy.units
        energy_axis.attrs["long_name"] = "Energy"

        if self._layout == MAP_LAYOUT:
            axes = self._navigation_axes()
            height, width = int(self._data.shape[0]), int(self._data.shape[1])
            for name, calibration, length in (
                ("axis_j", axes.y, height),
                ("axis_i", axes.x, width),
            ):
                axis = data_group.create_dataset(
                    name,
                    data=calibration.values(length),
                )
                axis.attrs["units"] = calibration.units
                axis.attrs["long_name"] = calibration.long_name
            data_group.attrs["axes"] = ["axis_j", "axis_i", "axis_energy"]
            # Unsigned for the reason storage/nexus.py's _write_nxdata gives.
            data_group.attrs.create("axis_j_indices", 0, dtype="uint32")
            data_group.attrs.create("axis_i_indices", 1, dtype="uint32")
            data_group.attrs.create("axis_energy_indices", 2, dtype="uint32")
        else:
            # stack_0d, even for a single spot spectrum. NXspectrum does
            # define spectrum_0d for exactly one, and writing that instead
            # was considered and rejected: it would make the *rank* of the
            # signal depend on how many spectra an acquisition happened to
            # produce, so every reader would branch and a recording that
            # stopped after one would read back differently from the same
            # recording that managed two. A stack of one is honest -
            # n_spc is 1 - and uniform.
            for name, values, label in (
                ("indices_group", np.zeros(self._count, dtype="int32"), "Group"),
                (
                    "indices_spectrum",
                    np.arange(self._count, dtype="int32"),
                    "Spectrum",
                ),
            ):
                index_axis = data_group.create_dataset(name, data=values)
                index_axis.attrs["long_name"] = f"{label} identifier"
            data_group.attrs["axes"] = ["indices_spectrum", "axis_energy"]
            # Unsigned for the reason storage/nexus.py's _write_nxdata gives.
            data_group.attrs.create("indices_spectrum_indices", 0, dtype="uint32")
            data_group.attrs.create("axis_energy_indices", 1, dtype="uint32")

        # Why this exists and why it is written twice: see the matching
        # pair in :meth:`miainwoodpecker.storage.nexus.NexusWriter._write_nxdata`.
        # ``"spectrum"`` is right for every layout this writer produces,
        # because NXspectrum puts the energy axis last in all of them - so
        # a reader honouring it makes exactly ``axis_energy`` the signal
        # and leaves ``indices_spectrum``, or a map's ``axis_j``/
        # ``axis_i``, as navigation.
        #
        # Withheld from a file claiming an application definition for the
        # reason measured there: NXem's NXDL documents no such attribute
        # and pynxtools rejects the file that carries one.
        if self._definition is None:
            data_group.attrs["interpretation"] = "spectrum"
            self._data.attrs["interpretation"] = "spectrum"

        data_group.attrs["signal"] = "intensity"
        # Which NXspectrum layout this file's NXdata follows, written as
        # plain data in the collection rather than as an attribute NeXus
        # does not define. A reader recovers the same fact from the rank,
        # so this is a convenience and never the source of truth.
        self._metadata_group()["nxspectrum_layout"] = self._layout

    def _navigation_axes(self) -> FrameCalibration:
        """
        Decide a spectrum image's spatial calibration.

        Explicit ``navigation=`` wins; otherwise the first spectrum's
        ``fov_size_nm`` is read through exactly the path a scanned frame
        uses, so a map and an image of the same region cannot disagree
        about their own extent. Nothing said means bare pixel indices,
        which is the honest fallback everywhere else here too.

        Returns
        -------
        FrameCalibration
            The ``axis_j``/``axis_i`` calibration to write.
        """
        if self._navigation is not None:
            return self._navigation
        assert self._data is not None  # noqa: S101 - guarded by caller
        shape = (int(self._data.shape[0]), int(self._data.shape[1]))
        extent = (self._first_metadata or {}).get("fov_size_nm")
        if (
            isinstance(extent, (tuple, list))
            and len(extent) == 2  # noqa: PLR2004 - (y, x), as everywhere else here
            and all(isinstance(value, (int, float)) and value > 0 for value in extent)
        ):
            return FrameCalibration.from_field_size(
                (float(extent[0]), float(extent[1])),
                shape,
            )
        return FrameCalibration.uncalibrated()

    def _write_detector(self, entry: h5py.Group) -> None:
        """
        Write the detector's own facts into ``NXdetector``'s defined fields.

        Only fields the first spectrum actually reported are written; an
        absent key means "not reported", and a stored zero would not.
        ``dead_time`` is the one derived value, and only when both times
        are present and the arithmetic is meaningful.

        Parameters
        ----------
        entry : h5py.Group
            The file's ``NXentry`` group.
        """
        metadata = self._first_metadata or {}
        detector = entry["instrument/spectrum_detector"]
        for key, field, units in _DETECTOR_FIELDS:
            value = metadata.get(key)
            if value is None:
                continue
            if units is None:
                detector[field] = str(value)
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            dataset = detector.create_dataset(field, data=float(value))
            dataset.attrs["units"] = units
        live = metadata.get("live_time_s")
        real = metadata.get("real_time_s")
        if (
            isinstance(live, (int, float))
            and isinstance(real, (int, float))
            and not isinstance(live, bool)
            and not isinstance(real, bool)
            and real >= live
        ):
            dead = detector.create_dataset("dead_time", data=float(real) - float(live))
            dead.attrs["units"] = "s"
        identifier = metadata.get("device_id")
        if identifier is not None:
            detector["local_name"] = str(identifier)

    def _write_source(self, entry: h5py.Group) -> None:
        """
        Record the accelerating voltage in ``NXsource``, as the frame writer does.

        Same field, same reasoning, same key: the accelerating voltage is
        the one instrument value NeXus has a documented home for, and it
        matters more for an X-ray spectrum than anywhere else, since it
        sets which lines can be excited at all.

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


# A spectrum image has two navigation axes; anything else is a different
# NXspectrum group. Named so the branch reads as the rule it is.
_MAP_NAVIGATION_RANK = 2


def write_spectra(
    path: os.PathLike[str] | str,
    spectra: Iterable[Spectrum],
    *,
    flush_every: int = _DEFAULT_FLUSH_EVERY,
    **kwargs: object,
) -> int:
    """
    Write an iterable of spectra to a NeXus HDF5 file.

    Parameters
    ----------
    path : os.PathLike[str] | str
        Destination HDF5 file.
    spectra : Iterable[Spectrum]
        Spectra to persist, in acquisition order.
    flush_every : int
        Call :meth:`SpectrumWriter.flush` after this many spectra; ``0``
        (or less) never flushes. The default of 1 matches
        :func:`~miainwoodpecker.storage.nexus.write_frames`, and matters
        for the same measured reason.
    **kwargs : object
        Passed through to :class:`SpectrumWriter`.

    Returns
    -------
    int
        The number of spectra written.
    """
    with SpectrumWriter(path, **kwargs) as writer:  # type: ignore[arg-type]
        for spectrum in spectra:
            writer.append(spectrum)
            if flush_every > 0 and writer.spectrum_count % flush_every == 0:
                writer.flush()
        return writer.spectrum_count


def spectrum_from_projected_frame(frame: Frame) -> Spectrum:
    """
    Turn a projected (1D) camera frame into a :class:`Spectrum`.

    The bridge between the two device shapes: an EEL spectrometer camera
    under a projected readout
    (:data:`~miainwoodpecker.devices.interface.PROJECTED_READOUT`)
    produces a 1D :class:`~miainwoodpecker.devices.interface.Frame` whose
    surviving axis is the dispersive one, and this is what routes it into
    :class:`SpectrumWriter`'s ``NXspectrum`` layout — the same file shape
    EDX lands in, reaching
    :func:`~miainwoodpecker.analysis.hyperspy_bridge.load_as_hyperspy_spectrum`
    with no flattening. The §5 convergence of
    docs/adapters/spectrum-detectors.md, moved one layer down.

    The energy axis is taken **verbatim** from the frame's own
    ``metadata["calibration"]["x"]`` — the dispersive axis's calibration,
    which the projection keeps; the summed axis is gone — converted
    exactly within the energy kind to this layout's canonical eV. A frame
    whose surviving axis is not energy-calibrated is refused, because a
    :class:`~miainwoodpecker.devices.interface.Spectrum` cannot exist
    without its energy axis and inventing one here would put a fabricated
    axis on stored data.

    ``technique`` is stamped ``"eels"`` when the frame says it came from
    an EELS camera (``camera_type``), which is what lets
    :func:`~miainwoodpecker.analysis.hyperspy_bridge.load_as_eels_signal`
    recognise the recording — and
    :func:`~miainwoodpecker.analysis.hyperspy_bridge.load_as_eds_signal`
    refuse it — from what the file *says it is* rather than from its
    shape, which by then is identical to EDX's.

    Parameters
    ----------
    frame : Frame
        A 1D frame from a camera's projected readout, carrying an
        energy calibration for its surviving axis.

    Returns
    -------
    Spectrum
        The same counts and timestamp, with the energy axis as
        constructor arguments and the frame's metadata carried whole
        (plus ``technique`` where recoverable).

    Raises
    ------
    ValueError
        If the frame is not 1D, carries no ``calibration`` metadata for
        its axis, or its axis is calibrated as something other than
        energy.

    Notes
    -----
    Also propagates ``ValueError`` from
    :class:`~miainwoodpecker.devices.interface.Spectrum` when the
    dispersive axis's scale is negative — a spectrometer that disperses
    high-energy-first is a real arrangement, and reversing the counts to
    fit this layout would be a transformation of the data that this
    layer has no business performing silently.
    """
    if frame.data.ndim != 1:
        msg = (
            f"spectrum_from_projected_frame takes the 1D frame a projected "
            f"camera readout produces; got a {frame.data.ndim}D frame of "
            f"shape {frame.data.shape}, which belongs to NexusWriter"
        )
        raise ValueError(msg)
    calibration = frame.metadata.get(METADATA_KEY)
    axis_spec: object | None = None
    if isinstance(calibration, FrameCalibration):
        axis_spec = calibration.x
    elif isinstance(calibration, typing.Mapping):
        axis_spec = calibration.get("x")
    if axis_spec is None:
        msg = (
            f"a projected frame carries no energy calibration "
            f"(metadata[{METADATA_KEY!r}] names no 'x' axis), and a spectrum "
            f"cannot exist without its energy axis; record what the device "
            f"reported rather than assuming a dispersion"
        )
        raise ValueError(msg)
    axis = resolve_axis_calibration(axis_spec, int(frame.data.shape[0]), "x")
    if axis.kind is not AxisKind.ENERGY:
        msg = (
            f"a projected frame's surviving axis is calibrated as "
            f"{axis.kind.value!r} ({axis.units!r}), not energy, so its counts "
            f"are not a spectrum; only an energy-dispersive camera's "
            f"projected readout can be stored in the NXspectrum layout"
        )
        raise ValueError(msg)
    axis_ev = axis.converted_to(_ENERGY_UNITS)
    metadata = dict(frame.metadata)
    if str(metadata.get("camera_type", "")).lower() == EELS_TECHNIQUE:
        # What lets the analysis layer tell this recording from EDX once
        # both wear the same 1D shape. setdefault, so an adapter that
        # already said what it is is not overruled.
        metadata.setdefault(TECHNIQUE_KEY, EELS_TECHNIQUE)
    return Spectrum(
        data=frame.data,
        timestamp=frame.timestamp,
        energy_offset_ev=axis_ev.offset,
        energy_scale_ev=axis_ev.scale,
        metadata=metadata,
    )


class SpectrumRecording(typing.NamedTuple):
    """
    Everything one spectrum recording holds, read in a single open.

    A named tuple rather than a bare tuple because there are six things
    and four of them are numbers — the frame side's three-tuple is at the
    edge of what positional unpacking should carry, and this is past it.

    Attributes
    ----------
    data : npt.NDArray[typing.Any]
        Counts. ``(n_spectra, n_channels)`` for spot spectra, whether one
        or many, so a caller never has to branch on the count;
        ``(height, width, n_channels)`` for a spectrum image.
    energy_offset_ev : float
        Energy at channel 0, in electronvolts.
    energy_scale_ev : float
        Energy per channel, in electronvolts.
    navigation : FrameCalibration
        A map's spatial calibration; the uncalibrated pixel model for
        spot spectra, which have no spatial axes.
    metadata : list[dict[str, typing.Any]]
        One dictionary per appended spectrum, in acquisition order.
    is_map : bool
        Whether this is a spectrum image rather than spot spectra.
    """

    data: npt.NDArray[typing.Any]
    energy_offset_ev: float
    energy_scale_ev: float
    navigation: FrameCalibration
    metadata: list[dict[str, typing.Any]]
    is_map: bool

    @property
    def count(self) -> int:
        """Return how many spectra were recorded; 1 for a map."""
        return 1 if self.is_map else int(self.data.shape[0])

    @property
    def channel_count(self) -> int:
        """Return the number of energy channels."""
        return int(self.data.shape[-1])


def read_spectra(path: os.PathLike[str] | str) -> SpectrumRecording:
    """
    Read a whole spectrum recording — counts, axes, and metadata — in one open.

    The single reader, for the reason
    :func:`~miainwoodpecker.storage.nexus.read_frames` is one: every
    consumer that opened the file itself would encode the layout again,
    and the layout is meant to be known in one place
    (:mod:`miainwoodpecker.storage.layout`).

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`SpectrumWriter`.

    Returns
    -------
    SpectrumRecording
        The counts, the energy calibration, any spatial calibration, the
        per-spectrum metadata, and whether it is a map.

    Raises
    ------
    layout.NoSpectraError
        If the file holds no spectrum data — including when it is a
        perfectly good *frame* recording, which the message says rather
        than claiming the file is empty.
    """
    with h5py.File(path, "r") as handle:
        if layout.SPECTRUM_INTENSITY not in handle:
            raise layout.NoSpectraError(path)
        data = handle[layout.SPECTRUM_INTENSITY][()]
        energy_values = handle[layout.SPECTRUM_ENERGY_AXIS][()]
        is_map = data.ndim == _MAP_NAVIGATION_RANK + 1
        navigation = FrameCalibration.uncalibrated()
        if is_map:
            navigation = FrameCalibration(
                y=_axis_from_values(handle, layout.SPECTRUM_SLOW_AXIS),
                x=_axis_from_values(handle, layout.SPECTRUM_FAST_AXIS),
            )
        metadata: list[dict[str, typing.Any]] = []
        if layout.SPECTRUM_METADATA in handle:
            metadata = [
                json.loads(_decoded(entry))
                for entry in handle[layout.SPECTRUM_METADATA][()]
            ]
    offset = float(energy_values[0]) if len(energy_values) else 0.0
    scale = (
        float(energy_values[1] - energy_values[0]) if len(energy_values) > 1 else 1.0
    )
    return SpectrumRecording(
        data=data,
        energy_offset_ev=offset,
        energy_scale_ev=scale,
        navigation=navigation,
        metadata=metadata or [{}],
        is_map=is_map,
    )


def _axis_from_values(handle: h5py.File, path: str) -> AxisCalibration:
    """
    Recover one spatial axis's calibration from a written file.

    Parameters
    ----------
    handle : h5py.File
        The open recording.
    path : str
        The axis dataset's path.

    Returns
    -------
    AxisCalibration
        The linear calibration the values encode, or the uncalibrated
        pixel model when the axis was never written.
    """
    if path not in handle:
        return AxisCalibration()
    values = handle[path][()]
    units = _decoded(handle[path].attrs.get("units", _NAVIGATION_UNITS))
    if units == "pixel":
        return AxisCalibration()
    offset = float(values[0]) if len(values) else 0.0
    scale = float(values[1] - values[0]) if len(values) > 1 else 1.0
    return AxisCalibration(AxisKind.REAL_SPACE, scale, offset, units)


def _decoded(value: object) -> str:
    """
    Return an HDF5 string as ``str``, whether or not it is bytes.

    Parameters
    ----------
    value : object
        A value read from h5py, which may be ``bytes`` or ``str``.

    Returns
    -------
    str
        The decoded value.
    """
    return value.decode() if isinstance(value, bytes) else str(value)
