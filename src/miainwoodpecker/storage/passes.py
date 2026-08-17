"""
Storing a :class:`~miainwoodpecker.devices.interface.ScanPass`.

A pass is several correlated signals from one traversal of the probe —
image channels, 4D diffraction cubes, spectrum images — and storing it
raises one question the frame writer never had to answer: *where does a
second signal go?*

The layout, and why it is this one
----------------------------------
One ``NXentry`` per pass, holding one ``NXdata`` per signal.
``entry/data`` is the default and plottable one; the rest are
``entry/data_<name>``. NeXus allows an entry many ``NXdata`` groups and
uses the ``default`` attribute to name the one to plot, so this is the
vocabulary's own answer rather than a private convention.

The tempting alternative is ``NXem``'s ``measurement/eventID*``
hierarchy, which is *designed* for exactly this — several signals from
one event — and docs/adapters/spectrum-detectors.md §3 explicitly names
a pass as the thing that would justify adopting it. It is not adopted
here, and the reason is costed rather than lazy: reaching that path
additionally requires an ``NXem_instrument`` carrying ``ebeam_column``
and ``fabrication`` under a ``measurement`` group, i.e. restructuring
the entry that every reader in this package and every file already
written depends on. That is a migration, not a layout choice, and it
should be made once — when the ``measurement``/``event`` hierarchy is
needed for its own sake — rather than smuggled in behind the first
feature that could use it.

Streaming is structural, not an option
--------------------------------------
A 64x64 grid on a 512x512 detector is four gigabytes. :class:`PassWriter`
therefore creates the file and its datasets **first** and hands them out
through :meth:`PassWriter.destinations`, to be passed as
``scan_synchronised(..., into=...)``. The device then fills the final
on-disk datasets as it acquires, chunked one beam position per chunk so
each write lands as a single chunk write.

The alternative — acquire into memory, then copy into a file — is what
this shape exists to prevent, and it would be invisible in a test that
only compared values. So the cube is never held whole in memory here,
and a test asserts the pass's array *is* the file's dataset.
"""

from __future__ import annotations

import datetime
import json
import typing

import h5py
import numpy as np

from miainwoodpecker.storage.calibration import AxisKind, FrameCalibration

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Mapping

    from miainwoodpecker.devices.interface import ScanPass
    from miainwoodpecker.storage.calibration import AxisCalibration

_NAVIGATION_AXES = ("scan_y", "scan_x")
_DETECTOR_AXES = ("det_y", "det_x")
_IMAGE_AXES = ("y", "x")


def _version() -> str:
    """
    Return this package's version, for the ``program_name`` stamp.

    Returns
    -------
    str
        The installed version, or ``"unknown"`` if it cannot be read.
    """
    try:
        from miainwoodpecker import __version__  # noqa: PLC0415
    except ImportError:  # pragma: no cover - only when not installed
        return "unknown"
    return str(__version__)


def _json_default(value: object) -> str:
    """
    Render a value JSON cannot encode as a string.

    Parameters
    ----------
    value : object
        The value to render.

    Returns
    -------
    str
        Its string form.
    """
    return str(value)


class PassWriter:
    """
    Write one :class:`ScanPass` to a NeXus file, streaming its cubes.

    Used in two steps, because the whole point is that the cube is
    written where it will live rather than copied there afterwards::

        with PassWriter(path, parameters, cubes={"camera": (512, 512)}) as w:
            result = scanner.scan_synchronised(
                parameters, targets=["camera"], into=w.destinations(),
            )
            w.finish(result)

    Between those two calls the device is filling datasets inside this
    file. :meth:`finish` then writes everything that is only known once
    the pass is complete — the images, the metadata, the axes — and the
    identity that ties them together.

    Parameters
    ----------
    path : os.PathLike[str] | str
        Where to write.
    parameters : object
        The beam-position grid (a ``ScanParameters``), which calibrates
        every navigation axis.
    cubes : Mapping[str, tuple[int, int]] | None
        Detector shape per camera target, so the datasets can be
        allocated before the acquisition starts. Empty for a pass with
        no diffraction.
    title : str | None
        Optional entry title.
    detector_calibration : FrameCalibration | None
        Reciprocal-space calibration for the detector axes. None leaves
        them uncalibrated, which is the honest state when the camera
        length is not known — a diffraction axis given a fabricated
        scale is one a strain measurement would happily use.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str,
        parameters: object,
        *,
        cubes: Mapping[str, tuple[int, int]] | None = None,
        title: str | None = None,
        detector_calibration: FrameCalibration | None = None,
    ) -> None:
        self._parameters = parameters
        self._detector_calibration = detector_calibration
        self._handle = h5py.File(path, "w")
        self._cubes: dict[str, h5py.Dataset] = {}
        self._entry = self._build_skeleton(title)
        for name, detector_shape in (cubes or {}).items():
            self._cubes[name] = self._allocate_cube(name, detector_shape)

    def __enter__(self) -> typing.Self:
        """
        Enter the context, returning this writer.

        Returns
        -------
        PassWriter
            This writer.
        """
        return self

    def __exit__(self, *_: object) -> None:
        """Close the file, whatever happened inside the block."""
        self.close()

    def close(self) -> None:
        """Close the underlying file if it is still open."""
        if self._handle:
            self._handle.close()

    def destinations(self) -> dict[str, h5py.Dataset]:
        """
        Return the on-disk cubes, to be handed to ``scan_synchronised``.

        Returns
        -------
        dict[str, h5py.Dataset]
            One dataset per camera target, already the acquisition's
            shape and chunked one beam position per chunk.
        """
        return dict(self._cubes)

    def _build_skeleton(self, title: str | None) -> h5py.Group:
        """
        Create the NXroot/NXentry frame every recording here shares.

        Parameters
        ----------
        title : str | None
            Optional entry title.

        Returns
        -------
        h5py.Group
            The entry group.
        """
        root = self._handle
        root.attrs["NX_class"] = "NXroot"
        root.attrs["default"] = "entry"
        entry = root.create_group("entry")
        entry.attrs["NX_class"] = "NXentry"
        entry.attrs["default"] = "data"
        entry["start_time"] = datetime.datetime.now(tz=datetime.UTC).isoformat()
        program = entry.create_dataset("program_name", data="miainwoodpecker")
        program.attrs["version"] = _version()
        if title is not None:
            entry["title"] = title
        instrument = entry.create_group("instrument")
        instrument.attrs["NX_class"] = "NXinstrument"
        return entry

    def _allocate_cube(
        self,
        name: str,
        detector_shape: tuple[int, int],
    ) -> h5py.Dataset:
        """
        Create one chunked 4D dataset for a camera to fill as it acquires.

        Chunked one beam position per chunk, deliberately: the device
        writes a whole detector image at a time, so that chunking makes
        each write a single chunk write rather than a read-modify-write
        of a block spanning several positions.

        Parameters
        ----------
        name : str
            The camera's target name.
        detector_shape : tuple[int, int]
            The per-position detector image shape.

        Returns
        -------
        h5py.Dataset
            The allocated cube.
        """
        group = self._data_group(f"data_{name}")
        navigation = self._parameters.shape
        return group.create_dataset(
            "data",
            shape=(*navigation, *detector_shape),
            dtype="float32",
            chunks=(1, 1, *detector_shape),
        )

    def _data_group(self, name: str) -> h5py.Group:
        """
        Return a named ``NXdata`` group under the entry, creating it once.

        Parameters
        ----------
        name : str
            The group name.

        Returns
        -------
        h5py.Group
            The group.
        """
        if name in self._entry:
            return self._entry[name]
        group = self._entry.create_group(name)
        group.attrs["NX_class"] = "NXdata"
        return group

    def finish(self, result: ScanPass) -> None:
        """
        Write everything that is only known once the pass has run.

        Parameters
        ----------
        result : ScanPass
            The completed pass. Its diffraction data is expected to be
            the datasets :meth:`destinations` handed out; anything else
            is written as a fresh dataset, which is correct but copies.

        Raises
        ------
        ValueError
            If the pass names a camera this writer allocated no cube for,
            which means the acquisition and the file disagree about what
            was collected.
        """
        unexpected = set(result.diffraction) - set(self._cubes)
        if unexpected:
            msg = (
                f"the pass carries diffraction for {sorted(unexpected)}, which "
                f"this writer allocated no cube for (it has {sorted(self._cubes)})"
            )
            raise ValueError(msg)

        self._entry["pass_id"] = result.pass_id
        # A field rather than a JSON blob: which device was master is the
        # evidence for the whole file's claim that its signals share
        # probe positions, and a reader should not have to parse a string
        # to find it.
        self._entry["scan_sync"] = result.scan_sync
        for name in result.diffraction:
            self._describe_cube(name, result)
        for index, frame in enumerate(result.images):
            self._write_image(index, frame)
        self._write_metadata(result)
        self._set_default(result)

    def _describe_cube(self, name: str, result: ScanPass) -> None:
        """
        Attach axes and calibration to an already-filled cube.

        Written after the acquisition rather than before, because the
        stack reports the settings every frame in it was taken with, and
        an adapter is free to have rounded them.

        Parameters
        ----------
        name : str
            The camera's target name.
        result : ScanPass
            The completed pass.
        """
        stack = result.diffraction[name]
        group = self._data_group(f"data_{name}")
        navigation = FrameCalibration.from_field_size(
            self._parameters.fov_size_nm,
            self._parameters.shape,
        )
        self._write_axis(group, _NAVIGATION_AXES[0], stack.navigation_shape[0],
                         navigation.y)
        self._write_axis(group, _NAVIGATION_AXES[1], stack.navigation_shape[1],
                         navigation.x)
        detector = self._detector_calibration or FrameCalibration()
        self._write_axis(group, _DETECTOR_AXES[0], stack.detector_shape[0],
                         detector.y)
        self._write_axis(group, _DETECTOR_AXES[1], stack.detector_shape[1],
                         detector.x)
        group.attrs["signal"] = "data"
        group.attrs["axes"] = [*_NAVIGATION_AXES, *_DETECTOR_AXES]
        for index, axis in enumerate([*_NAVIGATION_AXES, *_DETECTOR_AXES]):
            group.attrs[f"{axis}_indices"] = index
        group["data"].attrs["units"] = "counts"
        group.attrs["camera_id"] = stack.camera_id

    def _write_image(self, index: int, frame: object) -> None:
        """
        Write one intensity channel as its own ``NXdata``.

        Named after the channel rather than numbered where the frame
        says so, because "which detector is this" is the question a
        reader actually has.

        Parameters
        ----------
        index : int
            Position in the pass's image list, the fallback name.
        frame : object
            The channel's ``Frame``.
        """
        channel = frame.metadata.get("channel_name") or f"channel{index}"
        group = self._data_group(f"data_{channel}")
        data = group.create_dataset("data", data=np.asarray(frame.data))
        data.attrs["units"] = "counts"
        calibration = FrameCalibration.from_field_size(
            self._parameters.fov_size_nm,
            self._parameters.shape,
        )
        self._write_axis(group, _IMAGE_AXES[0], data.shape[0], calibration.y)
        self._write_axis(group, _IMAGE_AXES[1], data.shape[1], calibration.x)
        group.attrs["signal"] = "data"
        group.attrs["axes"] = list(_IMAGE_AXES)
        for axis_index, axis in enumerate(_IMAGE_AXES):
            group.attrs[f"{axis}_indices"] = axis_index

    @staticmethod
    def _write_axis(
        group: h5py.Group,
        name: str,
        length: int,
        axis: AxisCalibration,
    ) -> None:
        """
        Write one axis of an ``NXdata`` group from its calibration.

        Parameters
        ----------
        group : h5py.Group
            The ``NXdata`` group.
        name : str
            The axis dataset's name.
        length : int
            How many points the axis has.
        axis : AxisCalibration
            The calibration to realise.
        """
        values = axis.offset + axis.scale * np.arange(length, dtype=np.float64)
        dataset = group.create_dataset(name, data=values)
        dataset.attrs["units"] = axis.units
        if axis.kind is not AxisKind.UNCALIBRATED:
            dataset.attrs["long_name"] = axis.long_name

    def _write_metadata(self, result: ScanPass) -> None:
        """
        Write the per-signal metadata NeXus has no field for, as JSON.

        Parameters
        ----------
        result : ScanPass
            The completed pass.
        """
        collection = self._entry.create_group("metadata")
        collection.attrs["NX_class"] = "NXcollection"
        collection["pass_json"] = json.dumps(
            {
                "pass_id": result.pass_id,
                "scan_sync": result.scan_sync,
                "fov_nm": self._parameters.fov_nm,
                "pixel_time_us": self._parameters.pixel_time_us,
                "diffraction": {
                    name: stack.metadata for name, stack in result.diffraction.items()
                },
            },
            default=_json_default,
        )
        if result.images:
            collection["frame_metadata_json"] = [
                json.dumps(frame.metadata, default=_json_default)
                for frame in result.images
            ]

    def _set_default(self, result: ScanPass) -> None:
        """
        Point ``entry/data`` at the signal a reader should plot first.

        The diffraction cube when there is one, since that is the reason
        a pass was taken; otherwise the first image channel. A hard link
        rather than a copy — the whole file exists to avoid writing these
        bytes twice.

        Parameters
        ----------
        result : ScanPass
            The completed pass.
        """
        if result.diffraction:
            primary = f"data_{next(iter(result.diffraction))}"
        elif result.images:
            channel = result.images[0].metadata.get("channel_name") or "channel0"
            primary = f"data_{channel}"
        else:  # pragma: no cover - ScanPass refuses to be empty
            return
        self._entry["data"] = self._entry[primary]
        self._entry.attrs["default"] = "data"


class PassRecording(typing.NamedTuple):
    """
    What :func:`read_pass` returns.

    Attributes
    ----------
    pass_id : str
        The traversal's identity.
    scan_sync : str
        Which device was master.
    signals : dict[str, tuple[int, ...]]
        Each stored signal's shape, by ``NXdata`` group name.
    """

    pass_id: str
    scan_sync: str
    signals: dict[str, tuple[int, ...]]


def read_pass(path: os.PathLike[str] | str) -> PassRecording:
    """
    Read back what a stored pass says about itself.

    Deliberately shallow: it reports the identity, the synchronisation
    and the shapes rather than loading gigabytes a caller may not want.
    Whole cubes are read through the analysis bridges, which know how to
    hand them to py4DSTEM and LiberTEM.

    Parameters
    ----------
    path : os.PathLike[str] | str
        The file to read.

    Returns
    -------
    PassRecording
        The pass's identity, synchronisation, and signal shapes.
    """
    with h5py.File(path, "r") as handle:
        entry = handle["entry"]
        signals = {
            name: tuple(group["data"].shape)
            for name, group in entry.items()
            if isinstance(group, h5py.Group)
            and group.attrs.get("NX_class") == "NXdata"
            and name != "data"
        }
        return PassRecording(
            pass_id=_text(entry["pass_id"][()]),
            scan_sync=_text(entry["scan_sync"][()]),
            signals=signals,
        )


def _text(value: object) -> str:
    """
    Return an HDF5 string value as ``str``.

    Parameters
    ----------
    value : object
        The stored value, ``bytes`` or ``str``.

    Returns
    -------
    str
        The decoded text.
    """
    return value.decode() if isinstance(value, bytes) else str(value)
