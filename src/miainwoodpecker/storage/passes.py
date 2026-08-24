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

**Each signal is spelled in the vocabulary its own kind already has.**
An image channel and a diffraction cube use ``data``, as a frame
recording does; a spectrum image uses ``NXspectrum``'s names —
``intensity`` for the signal, ``axis_energy`` for the fastest axis,
``axis_j`` and ``axis_i`` for the spatial ones — exactly as
:mod:`miainwoodpecker.storage.spectra` writes a standalone one. Two
signal names in one file looks like an inconsistency and is the opposite:
a reader that knows how to find spectra finds them here under the name it
already looks for, and a spectrum image inside a pass is not a different
format from a spectrum image beside one. ``NXdata`` was designed for
precisely this — its ``signal`` attribute names the dataset — which is
why :func:`read_pass` asks the group rather than assuming a name.

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

from miainwoodpecker.storage.calibration import (
    METADATA_KEY,
    AxisKind,
    FrameCalibration,
    resolve_frame_calibration,
)

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Mapping

    from miainwoodpecker.devices.interface import ScanPass
    from miainwoodpecker.storage.calibration import AxisCalibration

_NAVIGATION_AXES = ("scan_y", "scan_x")
_DETECTOR_AXES = ("det_y", "det_x")
_IMAGE_AXES = ("y", "x")

# NXspectrum's own names, for the spectrum-image signals of a pass. Taken
# from storage.spectra rather than respelled, so the two writers cannot
# drift apart about what a spectrum image looks like on disk.
_SPECTRUM_SIGNAL = "intensity"
_SPECTRUM_ENERGY_AXIS = "axis_energy"
_SPECTRUM_SPATIAL_AXES = ("axis_j", "axis_i")
_IMAGE_SIGNAL = "data"
_ENERGY_UNITS = "eV"


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
    spectra : Mapping[str, int] | None
        Channel count per spectrometer target, the spectrum-image
        counterpart of ``cubes``. Separate rather than folded in with a
        rank check, because the two are allocated with different shapes,
        different axes and different signal names — and because a caller
        that asked for the wrong one should be told so before the
        acquisition runs rather than after.
    title : str | None
        Optional entry title.
    detector_calibration : FrameCalibration | None
        Reciprocal-space calibration for the detector axes. None leaves
        them uncalibrated, which is the honest state when the camera
        length is not known — a diffraction axis given a fabricated
        scale is one a strain measurement would happily use.
    """

    def __init__(  # noqa: PLR0913 - keyword-only file-format options, not a call signature
        self,
        path: os.PathLike[str] | str,
        parameters: object,
        *,
        cubes: Mapping[str, tuple[int, int]] | None = None,
        spectra: Mapping[str, int] | None = None,
        title: str | None = None,
        detector_calibration: FrameCalibration | None = None,
    ) -> None:
        self._parameters = parameters
        self._detector_calibration = detector_calibration
        self._handle = h5py.File(path, "w")
        self._cubes: dict[str, h5py.Dataset] = {}
        self._spectra: dict[str, h5py.Dataset] = {}
        self._entry = self._build_skeleton(title)
        for name, detector_shape in (cubes or {}).items():
            self._cubes[name] = self._allocate_cube(name, detector_shape)
        for name, channels in (spectra or {}).items():
            self._spectra[name] = self._allocate_spectrum_image(name, channels)

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
        Return the on-disk datasets, to be handed to ``scan_synchronised``.

        One mapping covering both kinds of target, because that is what
        ``scan_synchronised(into=...)`` takes: the device is handed a
        destination per target name and does not need to know which of
        them this writer thinks is a cube.

        Returns
        -------
        dict[str, h5py.Dataset]
            One dataset per camera or spectrometer target, already the
            acquisition's shape and chunked one beam position per chunk.

        Raises
        ------
        ValueError
            If a target was allocated as both a cube and a spectrum
            image, which would make one of the two silently unreachable.
        """
        clash = sorted(set(self._cubes) & set(self._spectra))
        if clash:
            msg = (
                f"{clash} was allocated both a diffraction cube and a "
                f"spectrum image; one target produces one signal per pass, "
                f"so only one of the two could ever be filled"
            )
            raise ValueError(msg)
        return {**self._cubes, **self._spectra}

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
            _IMAGE_SIGNAL,
            shape=(*navigation, *detector_shape),
            dtype="float32",
            chunks=(1, 1, *detector_shape),
        )

    def _allocate_spectrum_image(self, name: str, channels: int) -> h5py.Dataset:
        """
        Create one chunked rank-3 dataset for a spectrometer to fill.

        Chunked **one beam position per chunk**, which is deliberately
        not what :class:`~miainwoodpecker.storage.spectra.SpectrumWriter`
        does with a spectrum image — it chunks a whole row, because it
        receives the finished map and writes it in one assignment. Here
        the device writes position by position as it acquires, so a row
        chunk would turn each of those into a read-modify-write of a
        block spanning the rest of the row. Same data, different arrival
        pattern, different chunking.

        Parameters
        ----------
        name : str
            The spectrometer's target name.
        channels : int
            How many energy channels each spectrum has.

        Returns
        -------
        h5py.Dataset
            The allocated spectrum image.

        Raises
        ------
        ValueError
            If the channel count describes no spectrum.
        """
        if channels < 1:
            msg = (
                f"a spectrum image needs at least one energy channel; "
                f"{name} was allocated {channels}"
            )
            raise ValueError(msg)
        group = self._data_group(f"data_{name}")
        navigation = self._parameters.shape
        return group.create_dataset(
            _SPECTRUM_SIGNAL,
            shape=(*navigation, channels),
            dtype="float32",
            chunks=(1, 1, channels),
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

        Notes
        -----
        Propagates :class:`ValueError` from :meth:`_check_expected` when
        the pass names a target this writer allocated nothing for, which
        means the acquisition and the file disagree about what was
        collected — including the case where a target was allocated a
        cube and came back a spectrum image, which is the same
        disagreement wearing a rank.
        """
        self._check_expected(
            set(result.diffraction), self._cubes, self._spectra, "diffraction",
        )
        self._check_expected(
            set(result.spectra), self._spectra, self._cubes, "a spectrum image",
        )

        self._entry["pass_id"] = result.pass_id
        # A field rather than a JSON blob: which device was master is the
        # evidence for the whole file's claim that its signals share
        # probe positions, and a reader should not have to parse a string
        # to find it.
        self._entry["scan_sync"] = result.scan_sync
        for name in result.diffraction:
            self._describe_cube(name, result)
        for name in result.spectra:
            self._describe_spectrum_image(name, result)
        for index, frame in enumerate(result.images):
            self._write_image(index, frame)
        self._write_metadata(result)
        self._set_default(result)

    @staticmethod
    def _check_expected(
        carried: set[str],
        allocated: Mapping[str, h5py.Dataset],
        other_kind: Mapping[str, h5py.Dataset],
        description: str,
    ) -> None:
        """
        Refuse a pass carrying a signal this writer made no room for.

        The two failures are told apart because the fixes differ: a
        target nobody allocated means the acquisition asked for something
        the file was not opened for, while a target allocated as the
        *other* kind means the camera was in a different readout mode
        than the caller sized the file from — which is a real mistake,
        since a spectrometer left imaging produces a 4D cube where a
        spectrum image was expected.

        Parameters
        ----------
        carried : set[str]
            Target names the completed pass carries this kind of signal
            for.
        allocated : Mapping[str, h5py.Dataset]
            What this writer allocated of this kind.
        other_kind : Mapping[str, h5py.Dataset]
            What it allocated of the other kind.
        description : str
            How to name this kind of signal in the message.

        Raises
        ------
        ValueError
            If the pass carries a signal with nowhere to put it.
        """
        unexpected = carried - set(allocated)
        if not unexpected:
            return
        mistyped = sorted(unexpected & set(other_kind))
        if mistyped:
            msg = (
                f"the pass carries {description} for {mistyped}, but this "
                f"writer allocated the other kind of signal for it - the "
                f"camera's readout mode and the shape the file was opened "
                f"for disagree"
            )
            raise ValueError(msg)
        msg = (
            f"the pass carries {description} for {sorted(unexpected)}, which "
            f"this writer allocated nothing for (it has {sorted(allocated)})"
        )
        raise ValueError(msg)

    def _describe_cube(self, name: str, result: ScanPass) -> None:
        """
        Attach axes and calibration to an already-filled cube.

        Written after the acquisition rather than before, because the
        stack reports the settings every frame in it was taken with, and
        an adapter is free to have rounded them.

        **The detector's own axes win over this writer's default**, and
        the case that forces it is a spectrometer read out in 2D. What
        makes a detector a spectrometer is that one of its axes is
        calibrated in energy rather than in space; how many axes it has
        besides that one is device-specific, and keeping the whole
        dispersed image instead of summing it is a real experiment. Such
        a stack lands in the same 4D container a Ronchigram camera's
        does — at that point the two are the same shape of data — so if
        the detector's axes did not travel with it, the one fact
        distinguishing them would be gone from the file. A stack that
        publishes no calibration falls back to whatever the caller
        supplied, and then to honest pixels.

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
        detector = self._stack_calibration(stack)
        self._write_axis(group, _DETECTOR_AXES[0], stack.detector_shape[0],
                         detector.y)
        self._write_axis(group, _DETECTOR_AXES[1], stack.detector_shape[1],
                         detector.x)
        group.attrs["signal"] = _IMAGE_SIGNAL
        group.attrs["axes"] = [*_NAVIGATION_AXES, *_DETECTOR_AXES]
        for index, axis in enumerate([*_NAVIGATION_AXES, *_DETECTOR_AXES]):
            # Unsigned for the reason storage/nexus.py's _write_nxdata gives.
            group.attrs.create(f"{axis}_indices", index, dtype="uint32")
        group[_IMAGE_SIGNAL].attrs["units"] = "counts"
        group.attrs["camera_id"] = stack.camera_id

    def _stack_calibration(self, stack: object) -> FrameCalibration:
        """
        Decide the per-position axes of one 4D stack.

        An explicit ``detector_calibration`` wins, then what the detector
        published, then honest pixels — the same descending precedence
        :func:`~miainwoodpecker.storage.calibration.resolve_frame_calibration`
        applies to a stored frame, and resolved through that call so a
        malformed calibration produces the frame path's own sentences
        rather than a second set of them.

        **Only the calibration key is consulted**, not the whole
        metadata mapping. That function's last fallback reads
        ``fov_size_nm`` — the *scan's* field of view — and these are
        detector axes: a stack whose metadata happened to carry the scan
        geometry would come out with its detector labelled in nanometres
        of specimen, which is a wrong answer rather than a missing one.

        Parameters
        ----------
        stack : object
            The pass's ``DiffractionStack`` for this target.

        Returns
        -------
        FrameCalibration
            The calibration to write on the detector axes.
        """
        published = stack.metadata.get(METADATA_KEY)
        return resolve_frame_calibration(
            tuple(stack.detector_shape),
            calibration=self._detector_calibration,
            metadata=None if published is None else {METADATA_KEY: published},
        )

    def _describe_spectrum_image(self, name: str, result: ScanPass) -> None:
        """
        Attach ``NXspectrum``'s axes to an already-filled spectrum image.

        The energy axis comes from the
        :class:`~miainwoodpecker.devices.interface.Spectrum` itself
        rather than from anything this writer was told in advance, and
        that is not a convenience: a spectrum cannot exist without its
        energy axis, and the one that matters is the one the acquisition
        *actually ran at*, which a detector is free to have rounded from
        what was asked for.

        The spatial axes come from the pass's scan geometry through the
        same call a scanned frame's calibration travels, so a spectrum
        image and an image channel of one pass cannot disagree about the
        extent of the region they both cover.

        Parameters
        ----------
        name : str
            The spectrometer's target name.
        result : ScanPass
            The completed pass.
        """
        spectrum = result.spectra[name]
        group = self._data_group(f"data_{name}")
        navigation = FrameCalibration.from_field_size(
            self._parameters.fov_size_nm,
            self._parameters.shape,
        )
        rows, columns = spectrum.navigation_shape
        self._write_axis(group, _SPECTRUM_SPATIAL_AXES[0], rows, navigation.y)
        self._write_axis(group, _SPECTRUM_SPATIAL_AXES[1], columns, navigation.x)
        energy = group.create_dataset(
            _SPECTRUM_ENERGY_AXIS,
            data=(
                spectrum.energy_offset_ev
                + spectrum.energy_scale_ev
                * np.arange(spectrum.channel_count, dtype=np.float64)
            ),
        )
        energy.attrs["units"] = _ENERGY_UNITS
        energy.attrs["long_name"] = "Energy"
        axes = [*_SPECTRUM_SPATIAL_AXES, _SPECTRUM_ENERGY_AXIS]
        group.attrs["signal"] = _SPECTRUM_SIGNAL
        group.attrs["axes"] = axes
        for index, axis in enumerate(axes):
            # Unsigned for the reason storage/nexus.py's _write_nxdata gives.
            group.attrs.create(f"{axis}_indices", index, dtype="uint32")
        group[_SPECTRUM_SIGNAL].attrs["long_name"] = "Counts"
        identifier = spectrum.metadata.get("device_id")
        if identifier is not None:
            group.attrs["detector_id"] = str(identifier)

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
            # Unsigned for the reason storage/nexus.py's _write_nxdata gives.
            group.attrs.create(f"{axis}_indices", axis_index, dtype="uint32")

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
                "spectra": {
                    name: dict(spectrum.metadata)
                    for name, spectrum in result.spectra.items()
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

        The per-position signal when there is one — a diffraction cube or
        a spectrum image — since that is the reason a pass was taken at
        all; otherwise the first image channel, which is all a pass
        without one has. A pass carrying both kinds picks the cube, and
        the choice is arbitrary rather than principled: nothing in this
        project acquires both yet, and when something does, the operator
        should be choosing rather than this line. A hard link rather than
        a copy — the whole file exists to avoid writing these bytes
        twice.

        Parameters
        ----------
        result : ScanPass
            The completed pass.
        """
        if result.diffraction:
            primary = f"data_{next(iter(result.diffraction))}"
        elif result.spectra:
            primary = f"data_{next(iter(result.spectra))}"
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

    **Each group is asked what its signal is called** rather than assumed
    to call it ``data``. That is what ``NXdata``'s ``signal`` attribute
    is for, and it is what lets one pass hold an image channel spelled
    the frame writer's way beside a spectrum image spelled
    ``NXspectrum``'s. A group with no ``signal`` attribute is skipped:
    the writer sets it in :meth:`PassWriter.finish`, so its absence means
    an acquisition that was allocated and then never completed, and a
    half-written dataset is not a signal to report.

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
        signals = {}
        for name, group in entry.items():
            if (
                not isinstance(group, h5py.Group)
                or group.attrs.get("NX_class") != "NXdata"
                or name == "data"
            ):
                continue
            signal = group.attrs.get("signal")
            if signal is None:
                continue
            key = _text(signal)
            if key in group:
                signals[name] = tuple(group[key].shape)
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
