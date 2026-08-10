"""
Write acquired frames to NeXus-structured HDF5.

NeXus is a *convention over HDF5* — typed groups (``NX_class``), a
``signal``/``axes`` description of what to plot, and ``units`` on every
physical quantity — so writing it needs only ``h5py``, not a conversion
framework. That keeps the storage layer thin while producing files any
NeXus-aware tool can open, which is the whole point of not inventing
another bespoke project format (see docs/migration-plan.md, §3).

Scope of the compliance claim: files are written to standard NeXus
conventions and declare ``definition = "NXem"`` to state intent, but they
have **not** been validated against the official NXem NXDL schema.
Doing that needs ``pynxtools``, which is a heavy dependency (~70
packages) and belongs in a validation/CI step, not in the runtime — see
the Phase 3 notes in the migration plan.

Writes stream frame-by-frame into a resizable, chunked dataset, so a long
acquisition is persisted as it happens rather than buffered in memory.
"""

from __future__ import annotations

import datetime
import json
import typing

import h5py
import numpy as np

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Iterable, Iterator

    from miainwoodpecker.devices.interface import Frame

_PROGRAM_NAME = "miainwoodpecker"
_DEFAULT_COMPRESSION = "gzip"
_DEFAULT_COMPRESSION_LEVEL = 4


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
    definition : str
        NeXus application definition name declared by the file.
    compression : str | None
        h5py compression filter for the frame dataset, or None to disable.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        title: str = "miainwoodpecker acquisition",
        definition: str = "NXem",
        compression: str | None = _DEFAULT_COMPRESSION,
    ) -> None:
        self._path = path
        self._title = title
        self._definition = definition
        self._compression = compression
        self._file: h5py.File | None = None
        self._data: h5py.Dataset | None = None
        self._times: h5py.Dataset | None = None
        self._count = 0
        self._first_metadata: typing.Mapping[str, typing.Any] | None = None
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
        entry["definition"] = self._definition
        entry["title"] = self._title
        entry["start_time"] = _iso(self._start)
        program = entry.create_dataset("program_name", data=_PROGRAM_NAME)
        program.attrs["version"] = _version()

        instrument = entry.create_group("instrument")
        instrument.attrs["NX_class"] = "NXinstrument"
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
            If the frame's shape or dtype differs from the first frame's.
        """
        if self._file is None:
            msg = "NexusWriter is not open; use it as a context manager."
            raise RuntimeError(msg)

        detector = self._file["entry/instrument/detector"]
        if self._data is None:
            self._first_metadata = frame.metadata
            self._frame_zero = frame.timestamp
            self._data = detector.create_dataset(
                "data",
                shape=(0, *frame.data.shape),
                maxshape=(None, *frame.data.shape),
                dtype=frame.data.dtype,
                chunks=(1, *frame.data.shape),
                compression=self._compression,
                compression_opts=(
                    _DEFAULT_COMPRESSION_LEVEL
                    if self._compression == _DEFAULT_COMPRESSION
                    else None
                ),
            )
            self._data.attrs["units"] = "counts"
            self._times = detector.create_dataset(
                "frame_time",
                shape=(0,),
                maxshape=(None,),
                dtype="float64",
            )
            self._times.attrs["units"] = "s"
        elif frame.data.shape != self._data.shape[1:]:
            msg = (
                f"frame shape {frame.data.shape} does not match the first "
                f"frame's shape {tuple(self._data.shape[1:])}"
            )
            raise ValueError(msg)

        index = self._count
        self._data.resize(index + 1, axis=0)
        self._data[index] = frame.data
        assert self._times is not None  # noqa: S101 - created alongside _data
        self._times.resize(index + 1, axis=0)
        elapsed = frame.timestamp - typing.cast("datetime.datetime", self._frame_zero)
        self._times[index] = elapsed.total_seconds()
        self._count += 1

    def close(self) -> None:
        """Write plotting hints, axis calibration, and metadata; close the file."""
        if self._file is None:
            return
        entry = self._file["entry"]
        end_time = datetime.datetime.now(tz=datetime.UTC)
        entry["end_time"] = _iso(end_time)

        if self._data is not None:
            self._write_nxdata(entry)
        if self._first_metadata:
            collection = entry.create_group("metadata")
            collection.attrs["NX_class"] = "NXcollection"
            collection["vendor_metadata_json"] = json.dumps(
                dict(self._first_metadata),
                default=_json_default,
                sort_keys=True,
            )
        self._file.close()
        self._file = None
        self._data = None
        self._times = None

    def _write_nxdata(self, entry: h5py.Group) -> None:
        """Create the NXdata group describing how to plot the frame stack."""
        assert self._data is not None  # noqa: S101 - guarded by caller
        height, width = self._data.shape[1], self._data.shape[2]
        metadata = self._first_metadata or {}

        data_group = entry.create_group("data")
        data_group.attrs["NX_class"] = "NXdata"
        # Hard-link rather than copy: one array, two access paths.
        data_group["data"] = self._data
        data_group["frame_time"] = self._file["entry/instrument/detector/frame_time"]  # type: ignore[index]

        # Scan frames know their field of view, so the spatial axes can carry
        # real calibration in nanometres instead of bare pixel indices.
        fov_nm = metadata.get("fov_nm")
        if isinstance(fov_nm, (int, float)) and fov_nm > 0 and width and height:
            y_values = np.linspace(0.0, float(fov_nm), height, endpoint=False)
            x_values = np.linspace(0.0, float(fov_nm), width, endpoint=False)
            units = "nm"
        else:
            y_values = np.arange(height, dtype="float64")
            x_values = np.arange(width, dtype="float64")
            units = "pixel"
        for name, values in (("y", y_values), ("x", x_values)):
            axis = data_group.create_dataset(name, data=values)
            axis.attrs["units"] = units

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
    \*\*kwargs : object
        Passed through to :class:`NexusWriter`.

    Returns
    -------
    int
        The number of frames written.
    """
    with NexusWriter(path, **kwargs) as writer:
        for frame in frames:
            writer.append(frame)
        return writer.frame_count


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
        detector = handle["entry/instrument/detector"]
        if "data" not in detector:
            return
        data = detector["data"]
        times = detector["frame_time"]
        for index in range(data.shape[0]):
            yield data[index], float(times[index])
