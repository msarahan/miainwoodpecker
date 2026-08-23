"""
A device that replays a recorded acquisition, at the speed it was taken.

The fourth in-tree adapter, and the first whose data is *real*. The
preview instrument (:mod:`miainwoodpecker.viewer.preview`) synthesises a
specimen that has a right answer by construction; this one opens a
session someone actually recorded on a microscope and hands it back
through the same protocols, one beam position at a time, waiting the
dwell the instrument waited.

Why a replay device rather than a file reader
----------------------------------------------
This project can already *read* vendor files — RosettaSciIO does it, and
:mod:`miainwoodpecker.analysis.hyperspy_bridge` is where analysis meets
them. A reader answers "what is in this file". It does not answer the
question this module exists for, which is **"does the acquisition path
work"**: the scan/detector synchronisation, the pass concept, the
streaming writer, the viewer's refusals, the status line, the timing an
operator actually sits through. Those are exercised only by something
that behaves like a device, and until now the only such thing was
synthetic.

So this is deliberately a device and not a loader. It implements
:class:`~miainwoodpecker.devices.interface.Scanner`,
:class:`~miainwoodpecker.devices.interface.SynchronisedScanner` and
:class:`~miainwoodpecker.devices.interface.Camera`, and everything above
the device layer — the viewer, ``PassWriter``, the NeXus layout — runs
against it unchanged. That is also the test: a bug that only shows up
against real geometry, real energy axes and real timing shows up here.

The same idea is already in the tree twice, in smaller form:
``camera_server``'s ``replay`` backend treats a media file as a camera,
and ``spectrum_server``'s opens a spectrum recording this project wrote.
This one is the pass-shaped version, and it reads the *vendor's* files
rather than our own — which is what makes a real session recorded at an
instrument into a fixture anyone can run without the instrument.

What it replays
---------------
A DigitalMicrograph spectrum-image session: a Gatan spectrometer driven
by GMS, with the HAADF channel collected during the same traversal.
A recording set is discovered by index (see :func:`find_recordings`):

``<n>_EELS-SI*.dm3``
    The spectrum image. Becomes the pass's
    :attr:`~miainwoodpecker.devices.interface.ScanPass.spectra`.
``<n>_HAADF*during-SI*.dm3``
    The image channel read out **during** that acquisition, on the same
    beam-position grid. This is the whole reason the session is worth
    replaying: it is a cross-device pass that a real instrument really
    performed, so the correlation this project's ``ScanPass`` asserts is
    a fact about the data rather than a claim about the adapter.
``<n>_HAADF*SI-survey*.dm3``
    The fine survey scan of the same region, taken before the SI. Used
    for the live scan view, so the window shows a real image while
    nothing is being acquired.

**Honesty rules, which are the same three every backend here follows.**
Every frame and spectrum carries ``backend: "replay"`` and the file it
came from, so a recording made from this can never be mistaken for one
made at an instrument. Nothing is resampled: a grid this recording does
not have is refused rather than interpolated to. And the timing is the
recording's own ``Pixel time (s)`` unless a caller explicitly asks for a
speed-up, which is then recorded in the metadata too.

What it cannot do, and says so
-------------------------------
The geometry is fixed. A recording is 22x25 beam positions over the
region the operator chose in 2011, and no argument makes it 32x32 — so
:meth:`ReplayScanner.scan_synchronised` refuses parameters that are not
the recording's own, naming the ones it has. Resampling would produce a
dataset of the requested shape whose every pixel was invented, which is
precisely the "plausible cube" failure the whole synchronised-acquisition
path is written to avoid.

Reading needs RosettaSciIO (the ``replay`` extra). It is imported lazily
and the failure names the extra, because a device that cannot open its
own data should say which package is missing rather than raise
``ModuleNotFoundError`` from three frames down.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import re
import time
import typing
from collections.abc import Mapping

import numpy as np

from miainwoodpecker.devices.interface import (
    ENERGY_OFFSET_CONTROL,
    HIGH_TENSION_V_KEY,
    PROJECTED_READOUT,
    SCAN_SYNC_DETECTOR,
    CameraParameters,
    Frame,
    ScanParameters,
    ScanPass,
    Spectrum,
)
from miainwoodpecker.storage.calibration import (
    METADATA_KEY,
    AxisCalibration,
    AxisKind,
    FrameCalibration,
)
from miainwoodpecker.storage.spectra import EELS_TECHNIQUE, TECHNIQUE_KEY

if typing.TYPE_CHECKING:
    import datetime
    from collections.abc import Sequence

REPLAY_BACKEND = "replay"
"""
What every frame and spectrum from this adapter reports as its backend.

The same string ``camera_server`` and ``spectrum_server`` use for their
file-playback backends, and it is load-bearing rather than decorative:
:mod:`miainwoodpecker.viewer.app` records that the two failures a backend
name exists to prevent are driving a microscope you meant to simulate,
and believing you are on hardware when you are not. Data replayed from a
file is the second one unless it says so on every object it produces.
"""

EELS_TARGET = "eels_camera"
"""The target name the spectrometer is served under, as the client spells it."""

SCANNER_ID = "replay_scanner"
HAADF_CHANNEL = "HAADF"

# The energy axis is found by its *units*, never by its position in the
# file. Measured, not assumed: this reader returns an SI as
# (energy, y, x) with `navigate=True` on the energy axis and False on the
# spatial ones - the opposite of what that flag means once HyperSpy has
# post-processed the same dictionary - while a line scan from the same
# session comes back as (x, energy). An adapter keying on either the axis
# order or the navigate flag would silently transpose one of the two.
# Reading the units is what docs/pre-hardware-work.md already concluded
# for the camera path: "the dispersive axis is the one whose units the
# device reports as eV".
_ENERGY_UNITS = ("ev", "kev", "mev")

# Where GMS keeps what this adapter needs, inside the DM tag tree.
_TAGS = ("ImageList", "TagGroup0", "ImageTags")
_DEFAULT_PIXEL_TIME_S = 0.2
_DEFAULT_EXPOSURE_MS = 200.0
_DEFAULT_HIGH_TENSION_V = 100_000.0
_MAP_RANK = 3
_LINE_RANK = 2


class ReplayDataError(RuntimeError):
    """
    Raised when a recording cannot be opened, or is not what it claims.

    Its own class rather than a bare ``RuntimeError`` because callers do
    branch on it: the viewer turns it into a status line, and the CLI
    turns it into an exit status with the sentence attached. Both want
    "this data is unusable" told apart from "this adapter is broken".
    """


def _read_dm(path: pathlib.Path) -> dict[str, typing.Any]:
    """
    Read one DigitalMicrograph file into RosettaSciIO's plain dictionary.

    Imported here rather than at module scope so that importing this
    adapter — which the viewer does to offer the backend at all — does
    not require the reader to be installed. A missing reader is then a
    sentence naming the extra, at the moment someone actually opens a
    file.

    Parameters
    ----------
    path : pathlib.Path
        The file to read.

    Returns
    -------
    dict[str, typing.Any]
        The first signal dictionary in the file.

    Raises
    ------
    ReplayDataError
        If RosettaSciIO is not installed, the file cannot be read, or it
        holds no signal.
    """
    try:
        from rsciio.digitalmicrograph import file_reader  # noqa: PLC0415
    except ImportError as error:  # pragma: no cover - depends on the install
        msg = (
            "reading DigitalMicrograph files needs RosettaSciIO, which is "
            "the 'replay' extra: pip install 'miainwoodpecker[replay]', or "
            "`pixi run -e replay ...`"
        )
        raise ReplayDataError(msg) from error
    try:
        signals = file_reader(str(path))
    except Exception as error:
        msg = f"{path.name} could not be read as a DigitalMicrograph file: {error}"
        raise ReplayDataError(msg) from error
    if not signals:
        msg = f"{path.name} holds no signal"
        raise ReplayDataError(msg)
    return signals[0]


# ANN401: a vendor tag tree holds whatever the vendor chose to put in it,
# and narrowing the return type here would only move the cast to the
# callers, which already each decide what to do with an absent tag.
def _tag(signal: Mapping[str, typing.Any], *path: str) -> typing.Any:  # noqa: ANN401
    """
    Return one value from the DM tag tree, or None if it is not there.

    Every tag this adapter reads is optional, and that is deliberate: an
    absent tag means the instrument did not report it, and substituting a
    default would put a number in a recording that nothing measured. The
    callers below each decide what to do with None, and what they mostly
    do is omit the key.

    Parameters
    ----------
    signal : Mapping[str, typing.Any]
        A signal dictionary from the reader.
    *path : str
        The tag path below ``ImageList/TagGroup0/ImageTags``.

    Returns
    -------
    typing.Any
        The tag's value, or None.
    """
    node: typing.Any = signal.get("original_metadata", {})
    for key in (*_TAGS, *path):
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _number(value: object) -> float | None:
    """
    Return a tag as a float, or None if it is not a usable number.

    Parameters
    ----------
    value : object
        A tag value.

    Returns
    -------
    float | None
        The number, or None.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _energy_axis_index(axes: Sequence[Mapping[str, typing.Any]]) -> int:
    """
    Return which axis is the energy one, by its units.

    Parameters
    ----------
    axes : Sequence[Mapping[str, typing.Any]]
        The reader's axis dictionaries, in array order.

    Returns
    -------
    int
        The energy axis's position in the array.

    Raises
    ------
    ReplayDataError
        If no axis is calibrated in energy — in which case the data is
        not a spectrum, whatever the file is called, and a
        :class:`~miainwoodpecker.devices.interface.Spectrum` built from
        it would carry an invented axis.
    """
    for index, axis in enumerate(axes):
        if str(axis.get("units", "")).strip().lower() in _ENERGY_UNITS:
            return index
    units = [axis.get("units") for axis in axes]
    msg = (
        f"no axis is calibrated in energy (units are {units}), so this is "
        f"not a spectrum image; a spectrum cannot exist without its energy "
        f"axis and one will not be invented here"
    )
    raise ReplayDataError(msg)


def _to_energy_last(
    data: np.ndarray,
    axes: Sequence[Mapping[str, typing.Any]],
) -> tuple[np.ndarray, Mapping[str, typing.Any]]:
    """
    Move the energy axis last, which is the invariant everything downstream has.

    ``NXspectrum`` says energy is "always the fastest dimension", and
    :class:`~miainwoodpecker.devices.interface.Spectrum` holds callers to
    it. This file does not: an SI from this session arrives as
    ``(energy, y, x)`` and a line scan from the *same* session as
    ``(x, energy)``. Transposing here is the adapter doing its job —
    converting the vendor's layout once, at the boundary, rather than
    leaving every reader to discover it.

    Parameters
    ----------
    data : np.ndarray
        The array as read.
    axes : Sequence[Mapping[str, typing.Any]]
        Its axis dictionaries, in array order.

    Returns
    -------
    tuple[np.ndarray, Mapping[str, typing.Any]]
        The array with energy last, and the energy axis's dictionary.
    """
    index = _energy_axis_index(axes)
    energy = axes[index]
    if index == data.ndim - 1:
        return data, energy
    order = [axis for axis in range(data.ndim) if axis != index] + [index]
    # ascontiguousarray, not just transpose: this array is then read one
    # beam position at a time by the acquisition loop, and a transposed
    # view makes every one of those a strided gather across the whole
    # cube. Paid once here, at load.
    return np.ascontiguousarray(np.transpose(data, order)), energy


@dataclasses.dataclass(frozen=True)
class ReplayRecording:
    """
    One recorded acquisition, in this project's units and conventions.

    Everything the vendor's file said, converted once at load: energy
    last, nanometres, microseconds, volts. Frozen because a replay device
    that could edit its own recording between passes would stop being a
    replay of anything.

    Attributes
    ----------
    label : str
        How the recording names itself, from its file stem.
    spectra : np.ndarray
        Counts with energy on the **last** axis: ``(y, x, energy)`` for a
        map, ``(x, energy)`` for a line, ``(energy,)`` for one spectrum.
    energy_offset_ev : float
        Energy at channel 0, in electronvolts, as actually acquired.
    energy_scale_ev : float
        Energy per channel, in electronvolts, as actually acquired.
    pixel_size_nm : float
        The beam-position spacing, from the file's own spatial axis.
    pixel_time_s : float
        What the instrument dwelled at each beam position. The number
        this device waits, and the reason it is a device.
    exposure_ms : float
        The spectrometer's exposure per position.
    binning : int
        Dispersive binning, recovered by comparing the acquisition's
        dispersion with the spectrometer's own.
    high_tension_v : float | None
        The accelerating voltage, or None if the file did not report it.
    energy_offset_v : float | None
        The spectrometer's drift-tube voltage, or None if not reported.
    detector_name : str | None
        The spectrometer's own name, e.g. ``"Enfina"``.
    image : np.ndarray | None
        The intensity channel read out during the same traversal, on the
        same grid, or None if the set has none.
    survey : np.ndarray | None
        The fine survey scan of the region, for the live view.
    survey_pixel_size_nm : float | None
        Its own pixel size, which is not the acquisition's.
    source : pathlib.Path
        The spectrum image's path, recorded on everything produced.
    """

    label: str
    spectra: np.ndarray
    energy_offset_ev: float
    energy_scale_ev: float
    pixel_size_nm: float
    pixel_time_s: float
    exposure_ms: float
    binning: int
    high_tension_v: float | None
    energy_offset_v: float | None
    detector_name: str | None
    image: np.ndarray | None
    survey: np.ndarray | None
    survey_pixel_size_nm: float | None
    source: pathlib.Path

    @property
    def navigation_shape(self) -> tuple[int, ...]:
        """
        Return the beam-position grid, ``()`` for a single spectrum.

        Returns
        -------
        tuple[int, ...]
            Every axis but energy.
        """
        return tuple(int(size) for size in self.spectra.shape[:-1])

    @property
    def channel_count(self) -> int:
        """
        Return how many energy channels each spectrum has.

        Returns
        -------
        int
            The last axis's length.
        """
        return int(self.spectra.shape[-1])

    @property
    def is_map(self) -> bool:
        """
        Return whether this is a spectrum image rather than a line or a spot.

        Returns
        -------
        bool
            True for a rank-3 recording.
        """
        return self.spectra.ndim == _MAP_RANK

    def scan_parameters(self) -> ScanParameters:
        """
        Return the acquisition's geometry, in the vendor-neutral type.

        The grid is the recording's and cannot be anything else, which is
        why this exists as a *statement* rather than as a default a
        caller may override: a replay device is asked what it has, and
        then that is acquired.

        ``fov_nm`` spans the longer axis, matching
        :class:`~miainwoodpecker.devices.interface.ScanParameters`'s own
        convention, so its ``pixel_size_nm`` comes back out equal to the
        one the file reported rather than merely close to it.

        Returns
        -------
        ScanParameters
            The beam-position grid, dwell and field of view.

        Raises
        ------
        ReplayDataError
            If the recording is not a map, and so has no 2D grid to
            describe. A line scan and a spot spectrum are real and
            loadable; they are simply not passes.
        """
        if not self.is_map:
            msg = (
                f"{self.label} is a {self.spectra.ndim}D recording with "
                f"navigation shape {self.navigation_shape}, not a spectrum "
                f"image, so it has no beam-position grid to scan"
            )
            raise ReplayDataError(msg)
        height, width = self.navigation_shape
        return ScanParameters(
            height=height,
            width=width,
            pixel_time_us=self.pixel_time_s * 1e6,
            fov_nm=self.pixel_size_nm * max(height, width),
        )

    def energy_calibration(self) -> FrameCalibration:
        """
        Return the spectrometer's axes: energy fast, nothing slow.

        Returns
        -------
        FrameCalibration
            An energy ``x`` axis and an uncalibrated ``y`` axis, which is
            what a projected readout leaves.
        """
        return FrameCalibration(
            x=AxisCalibration(
                AxisKind.ENERGY,
                self.energy_scale_ev,
                self.energy_offset_ev,
                "eV",
            ),
        )


@dataclasses.dataclass(frozen=True)
class RecordingSet:
    """
    The files of one numbered acquisition, before any of them is read.

    Separated from :class:`ReplayRecording` so that listing what a
    directory holds costs a directory scan rather than reading a
    gigabyte of spectra: the CLI prints the set list, and the viewer
    opens one of them.

    Attributes
    ----------
    index : str
        The leading number the session names its acquisitions by.
    spectrum_image : pathlib.Path
        The EELS spectrum-image file.
    image : pathlib.Path | None
        The intensity channel from the same traversal, if there is one.
    survey : pathlib.Path | None
        The survey scan of the region, if there is one.
    """

    index: str
    spectrum_image: pathlib.Path
    image: pathlib.Path | None
    survey: pathlib.Path | None


# The session's own naming, and the reason this adapter can pair the
# files at all: a numeric prefix per acquisition, then what the file is.
# Matched case-insensitively and loosely, because the suffixes carry the
# operator's notes too ("_FeMap", "_cone_tube_rotated") and a pattern
# demanding an exact tail would find nothing.
_INDEX = re.compile(r"^(\d+)_")
_SPECTRUM_IMAGE = re.compile(r"eels[-_]si", re.IGNORECASE)
_DURING = re.compile(r"during[-_]si", re.IGNORECASE)
_SURVEY = re.compile(r"si[-_]survey", re.IGNORECASE)


def find_recordings(directory: pathlib.Path | str) -> dict[str, RecordingSet]:
    """
    Group a session directory's files into acquisitions, by their index.

    Pairing is by the numeric prefix the operator's own naming already
    carries, and by what the rest of the name says the file is. That is a
    convention rather than a standard, so it is matched loosely and a
    file that fits nothing is ignored rather than guessed at: a session
    holds plenty of images belonging to no spectrum image, and a
    "helpful" match would attach an unrelated scan to a pass and thereby
    claim the two shared probe positions.

    Parameters
    ----------
    directory : pathlib.Path | str
        A directory of DigitalMicrograph files.

    Returns
    -------
    dict[str, RecordingSet]
        One entry per acquisition that has a spectrum image, keyed by
        index, in index order.

    Raises
    ------
    ReplayDataError
        If the directory does not exist.
    """
    root = pathlib.Path(directory)
    if not root.is_dir():
        msg = f"{root} is not a directory of recordings"
        raise ReplayDataError(msg)

    grouped: dict[str, dict[str, list[pathlib.Path]]] = {}
    for path in sorted(root.glob("*.dm3")) + sorted(root.glob("*.dm4")):
        match = _INDEX.match(path.name)
        if match is None:
            continue
        slot = grouped.setdefault(match.group(1), {})
        for role, pattern in (
            ("spectrum_image", _SPECTRUM_IMAGE),
            ("image", _DURING),
            ("survey", _SURVEY),
        ):
            if pattern.search(path.name):
                slot.setdefault(role, []).append(path)
                break

    return {
        index: RecordingSet(
            index=index,
            spectrum_image=_preferred(slot["spectrum_image"]),
            image=_preferred(slot.get("image")),
            survey=_preferred(slot.get("survey")),
        )
        for index, slot in sorted(grouped.items())
        if slot.get("spectrum_image")
    }


def _preferred(candidates: list[pathlib.Path] | None) -> pathlib.Path | None:
    """
    Choose one file when a session holds several for the same role.

    Sessions accumulate duplicates - this one has both a
    "Copy of 005_EELS-SI.dm3" and the original - and which one is opened
    must not depend on how the filesystem happens to order them. The
    shortest name wins, because a duplicate is the original's name with
    something added to it, whatever the something is: a " copy" suffix, a
    " (1)", a "Copy of " prefix. Ties break alphabetically so the answer
    is the same on every machine.

    This deliberately does not try to *detect* copies. A session may hold
    two genuinely different spectrum images under one index, and there is
    no rule that tells them apart from a duplicate - so the rule here is
    only "be deterministic", and a caller who wanted the other one names
    its index.

    Parameters
    ----------
    candidates : list[pathlib.Path] | None
        Every file found for one role, or None if there were none.

    Returns
    -------
    pathlib.Path | None
        The one to open, or None.
    """
    if not candidates:
        return None
    return min(candidates, key=lambda path: (len(path.name), path.name))


def load_recording(recordings: RecordingSet) -> ReplayRecording:
    """
    Read one acquisition set into this project's units and conventions.

    Parameters
    ----------
    recordings : RecordingSet
        The files to read.

    Returns
    -------
    ReplayRecording
        The acquisition, energy last.

    Notes
    -----
    Propagates :class:`ReplayDataError` from :func:`_read_dm` when a file
    cannot be read, and from :func:`_energy_axis_index` when the spectrum
    image has no energy axis.
    """
    signal = _read_dm(recordings.spectrum_image)
    axes = signal["axes"]
    spectra, energy = _to_energy_last(np.asarray(signal["data"]), axes)

    spatial = [
        axis
        for axis in axes
        if str(axis.get("units", "")).strip().lower() not in _ENERGY_UNITS
    ]
    pixel_size_nm = _number(spatial[0].get("scale")) if spatial else None

    scale_ev = _number(energy.get("scale")) or 1.0
    dispersion = _number(
        _tag(signal, "EELS", "Acquisition", "Spectrometer", "Dispersion (eV/ch)"),
    )
    # Binning recovered by comparing what the acquisition's axis says a
    # channel is worth against what the spectrometer says one channel is.
    # The same rule the Nion camera path follows, for the same reason:
    # binning is recovered from the data rather than echoed from a
    # setting, because a setting can have been changed after the frame
    # and an axis cannot.
    binning = 1
    if dispersion and dispersion > 0:
        binning = max(1, round(scale_ev / dispersion))

    image = None
    if recordings.image is not None:
        image = np.asarray(_read_dm(recordings.image)["data"])
    survey = None
    survey_pixel_size_nm = None
    if recordings.survey is not None:
        survey_signal = _read_dm(recordings.survey)
        survey = np.asarray(survey_signal["data"])
        survey_axes = survey_signal["axes"]
        if survey_axes:
            survey_pixel_size_nm = _number(survey_axes[0].get("scale"))

    exposure_s = _number(_tag(signal, "EELS", "Acquisition", "Exposure (s)"))
    pixel_time_s = _number(_tag(signal, "SI", "Acquisition", "Pixel time (s)"))
    return ReplayRecording(
        label=recordings.spectrum_image.stem,
        spectra=spectra,
        energy_offset_ev=_number(energy.get("offset")) or 0.0,
        energy_scale_ev=scale_ev,
        pixel_size_nm=pixel_size_nm or 1.0,
        # Falling back to a default rather than refusing, and this is the
        # one place that trade is made: the dwell only sets how long this
        # device waits, so a recording that did not report one is still
        # entirely replayable, and refusing to open it would be a worse
        # answer than replaying it at a plausible rate.
        pixel_time_s=pixel_time_s or _DEFAULT_PIXEL_TIME_S,
        exposure_ms=exposure_s * 1e3 if exposure_s else _DEFAULT_EXPOSURE_MS,
        binning=binning,
        high_tension_v=_number(_tag(signal, "Microscope Info", "Voltage")),
        energy_offset_v=_number(
            _tag(signal, "EELS", "Acquisition", "Spectrometer",
                 "Drift tube voltage (V)"),
        ),
        detector_name=_tag(
            signal, "EELS", "Acquisition", "Spectrometer", "Instrument name",
        ),
        image=image,
        survey=survey,
        survey_pixel_size_nm=survey_pixel_size_nm,
        source=recordings.spectrum_image,
    )


def load(directory: pathlib.Path | str, index: str | None = None) -> ReplayRecording:
    """
    Open one acquisition from a session directory, by index.

    Parameters
    ----------
    directory : pathlib.Path | str
        A directory of DigitalMicrograph files.
    index : str | None
        Which acquisition, e.g. ``"004"``. None takes the first that is
        a spectrum image.

    Returns
    -------
    ReplayRecording
        The acquisition.

    Raises
    ------
    ReplayDataError
        If the directory holds no spectrum image, or not the one asked
        for. The message names what it does hold, since the answer to
        "which index" is otherwise a directory listing away.
    """
    sets = find_recordings(directory)
    if not sets:
        msg = (
            f"{directory} holds no spectrum-image recording: no file is "
            f"named like '004_EELS-SI.dm3'"
        )
        raise ReplayDataError(msg)
    if index is None:
        return load_recording(next(iter(sets.values())))
    if index not in sets:
        msg = f"{directory} has no recording {index!r}; it has {sorted(sets)}"
        raise ReplayDataError(msg)
    return load_recording(sets[index])


def _now() -> datetime.datetime:
    """
    Return the current UTC time, for a frame's timestamp.

    The *replay's* time, not the recording's, and deliberately so: this
    acquisition is happening now. When the original was taken is a
    property of the file, and it travels in the metadata under
    ``recorded_source`` rather than being backdated onto a frame, because
    a timestamp that lies about when data arrived breaks every ordering a
    session depends on.

    Returns
    -------
    datetime.datetime
        The current time, timezone-aware.
    """
    import datetime as _datetime  # noqa: PLC0415 - shadowed by the annotation import

    return _datetime.datetime.now(tz=_datetime.UTC)


class ReplayInstrument:
    """
    The instrument state a recording reported, held read-only.

    Implements :class:`~miainwoodpecker.devices.interface.Instrument`,
    and publishes exactly one control:
    :data:`~miainwoodpecker.devices.interface.ENERGY_OFFSET_CONTROL`,
    because the spectrometer's drift-tube voltage is the one instrument
    setting this session actually recorded. Publishing the other three
    would put rows in the viewer's Instrument panel for dials that cannot
    move anything, which is the failure
    :meth:`~miainwoodpecker.devices.interface.Instrument.available_controls`
    exists to prevent.

    **Setting the offset is refused rather than accepted and ignored.**
    A replay cannot change what was recorded, and this project has been
    bitten precisely once by a control that accepted a value, echoed it
    back and did nothing (``probe_position``; see the migration plan).
    An operator who drives this dial gets a sentence saying the data is
    fixed, which is a true statement about a replay and a useful one.

    Parameters
    ----------
    recording : ReplayRecording
        The acquisition whose state this reports.
    """

    def __init__(self, recording: ReplayRecording) -> None:
        self._recording = recording

    def describe(self) -> dict[str, object]:
        """
        Report the backend and the devices served.

        Returns
        -------
        dict[str, object]
            The shape a device server's ``describe`` returns, so the
            Instrument panel needs no replay-specific branch. The backend
            is :data:`REPLAY_BACKEND`, which is what stops a window full
            of 2011 data reading as a live one.
        """
        return {
            "backend": REPLAY_BACKEND,
            "targets": ["scanner", EELS_TARGET],
            "controls": list(self.available_controls()),
            "recording": self._recording.label,
        }

    def stage_size_nm(self) -> float:
        """
        Return the usable stage extent, in nanometres.

        Returns
        -------
        float
            The recording's own field of view. A replay has no stage to
            move, so what this reports is the region it holds: a caller
            sizing a scan against it gets the only field of view there
            is any data for.
        """
        height, width = (
            self._recording.navigation_shape
            if self._recording.is_map
            else (1, 1)
        )
        return self._recording.pixel_size_nm * max(height, width)

    def available_controls(self) -> Sequence[str]:
        """
        Return the control names this instrument publishes.

        Returns
        -------
        typing.Sequence[str]
            The energy offset when the recording reported one, and
            nothing otherwise.
        """
        return [ENERGY_OFFSET_CONTROL] if self._recording.energy_offset_v else []

    def park(self) -> None:
        """Do nothing, honestly: a replay has no beam to blank."""

    def energy_offset_ev(self) -> float:
        """
        Return the spectrometer's energy offset as it was recorded.

        Returns
        -------
        float
            The drift-tube voltage, in volts, which for a drift tube is
            numerically the energy offset in electronvolts.
        """
        return float(self._recording.energy_offset_v or 0.0)

    def set_energy_offset_ev(self, offset_ev: float) -> None:
        """
        Refuse to move the spectrometer, and say why.

        Parameters
        ----------
        offset_ev : float
            The requested offset, which cannot be honoured.

        Raises
        ------
        ReplayDataError
            Always. The spectra in this device were dispersed at the
            offset the operator used, and re-dispersing them is not
            something a replay can do or fake.
        """
        msg = (
            f"{self._recording.label} was recorded at an energy offset of "
            f"{self.energy_offset_ev()} eV and cannot be moved to "
            f"{offset_ev} eV; a replay device hands back what was acquired"
        )
        raise ReplayDataError(msg)


class ReplaySpectrometer:
    """
    The EEL spectrometer of a recorded session, replayed.

    Implements :class:`~miainwoodpecker.devices.interface.Camera`. What
    it produces is a **projected** readout: the instrument summed its
    non-dispersive direction on the sensor before reading out, which the
    file says plainly enough to be read rather than assumed. The Enfina
    behind this session has a 1340x100 sensor, and the stored spectra are
    670 channels of a 0.4 eV axis against the spectrometer's own 0.2 eV
    dispersion, so the acquisition binned the dispersive direction by two
    and summed the other one entirely.

    The readout mode is therefore **fixed**, unlike the preview's
    spectrometer, which can be switched. That is not a limitation being
    papered over: the non-dispersive direction is gone from the file, and
    an imaging readout would have to invent 100 rows.

    Parameters
    ----------
    recording : ReplayRecording
        The acquisition to serve.
    speed : float
        How many times faster than the original to replay. See
        :class:`ReplayScanner`, which documents why this exists and why
        it is recorded in the metadata.
    """

    def __init__(self, recording: ReplayRecording, speed: float = 1.0) -> None:
        self._recording = recording
        self._speed = speed
        self._started = False
        self._index = 0
        self._parameters = CameraParameters(
            exposure_ms=recording.exposure_ms,
            binning=recording.binning,
            readout=PROJECTED_READOUT,
        )

    @property
    def camera_id(self) -> str:
        """
        Return the stable identifier for this spectrometer.

        Returns
        -------
        str
            The vendor's own name for it where the file gave one, so a
            recording says which spectrometer it came off.
        """
        return self._recording.detector_name or EELS_TARGET

    @property
    def camera_type(self) -> str:
        """
        Return what kind of detector this is.

        Returns
        -------
        str
            Always EELS: this adapter reads spectrum images from an
            energy-dispersive spectrometer and nothing else.
        """
        return EELS_TECHNIQUE

    @property
    def binning_values(self) -> Sequence[int]:
        """
        Return the binning factors this device offers, which is one.

        Returns
        -------
        typing.Sequence[int]
            Only the binning the acquisition actually used. A replay
            cannot re-bin: summing channels would change the energy axis
            of data already dispersed, and offering a factor that would
            then be refused teaches an operator nothing.
        """
        return [self._recording.binning]

    @property
    def channel_count(self) -> int:
        """
        Return the length of the energy-dispersive axis.

        Returns
        -------
        int
            How many energy channels each spectrum has.
        """
        return self._recording.channel_count

    @property
    def readout_shape(self) -> tuple[int, ...]:
        """
        Return the shape of one readout.

        Returns
        -------
        tuple[int, ...]
            One dimension of counts, since this device only projects.
        """
        return (self.channel_count,)

    def parameters(self) -> CameraParameters:
        """
        Return the settings the recording was acquired with.

        Returns
        -------
        CameraParameters
            The exposure, binning and readout mode the instrument used.
        """
        return self._parameters

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Accept only the settings the recording was actually taken at.

        A device that quietly accepted a new exposure would be claiming
        the spectra it then handed back were acquired at it. They were
        not, and no acquisition can make them so — so a request that
        differs is refused, and one that matches is a no-op that returns
        what the device is already set to.

        Parameters
        ----------
        parameters : CameraParameters
            The requested settings.

        Returns
        -------
        CameraParameters
            The settings in force, unchanged.

        Raises
        ------
        ReplayDataError
            If the request differs from what was recorded.
        """
        current = self._parameters
        if parameters == current:
            return current
        msg = (
            f"{self.camera_id} is replaying {self._recording.label}, which "
            f"was acquired at {current.exposure_ms:g} ms, binning "
            f"{current.binning}, {current.readout} readout; it cannot be "
            f"reconfigured to {parameters.exposure_ms:g} ms, binning "
            f"{parameters.binning}, {parameters.readout}"
        )
        raise ReplayDataError(msg)

    def frame_calibration(self) -> FrameCalibration:
        """
        Return the energy axis these spectra were dispersed onto.

        Returns
        -------
        FrameCalibration
            The recording's own axis, verbatim.
        """
        return self._recording.energy_calibration()

    def start(self) -> None:
        """Begin acquisition, which for a replay is rewinding to the start."""
        self._started = True

    def stop(self) -> None:
        """Pause acquisition; ``start`` may be called again."""
        self._started = False

    def close(self) -> None:
        """Release nothing; this device owns no resources."""

    def spectrum_at(self, position: int) -> np.ndarray:
        """
        Return the spectrum recorded at one beam position.

        Parameters
        ----------
        position : int
            The position's index in acquisition order, wrapped so a live
            view can run indefinitely over a finite recording.

        Returns
        -------
        numpy.ndarray
            Counts per channel.
        """
        flat = self._recording.spectra.reshape(-1, self.channel_count)
        return np.asarray(flat[position % len(flat)])

    def acquire_frame(self) -> Frame:
        """
        Return the next spectrum, waiting the exposure the instrument waited.

        Successive calls walk the recording's beam positions in
        acquisition order and wrap at the end, so a live view shows the
        specimen changing under the probe exactly as it did. It is a
        *view* of the recording rather than an acquisition from it, and
        the wrap is the giveaway that would be dishonest to hide: the
        metadata says which position each frame came from.

        Returns
        -------
        Frame
            One projected spectrum, calibrated in energy.

        Raises
        ------
        RuntimeError
            If called before ``start``, which is the interface's contract
            and is worth honouring even here: a viewer that never called
            ``start`` would otherwise work against this device and stall
            against every real one.
        """
        if not self._started:
            msg = (
                f"{self.camera_id} has not been started; "
                "call start() before acquire_frame()"
            )
            raise RuntimeError(msg)
        position = self._index
        self._index += 1
        _wait(self._recording.exposure_ms / 1e3, self._speed)
        return Frame(
            data=self.spectrum_at(position),
            timestamp=_now(),
            metadata={
                **self._provenance(),
                "device_id": self.camera_id,
                "camera_name": self.camera_id,
                "camera_type": self.camera_type,
                "frame_index": position,
                "beam_position_index": position % max(
                    1, self._recording.spectra.size // self.channel_count,
                ),
                "exposure_ms": self._parameters.exposure_ms,
                "binning": self._parameters.binning,
                "readout": PROJECTED_READOUT,
                # The sensor summed its non-dispersive direction before
                # readout, so this frame carries one readout's noise
                # rather than a hundred rows' worth. The distinction is
                # recorded because the noise statistics differ and
                # nothing downstream could recover which it was.
                "projected_by": "sensor",
                METADATA_KEY: _calibration_metadata(self.frame_calibration()),
            },
        )

    def _provenance(self) -> dict[str, object]:
        """
        Return the keys that stop replayed data passing for live data.

        Returns
        -------
        dict[str, object]
            The backend, the file, and the speed-up if there was one.
        """
        return _provenance(self._recording, self._speed)


def _provenance(recording: ReplayRecording, speed: float) -> dict[str, object]:
    """
    Return the keys that stop replayed data passing for live data.

    Attached to every frame, every spectrum and every pass this module
    produces. The rule is the one ``viewer/app.py`` states: the second of
    the two failures a backend name exists to prevent is believing you
    are on hardware when you are not, and by the time anyone reads the
    metadata the session has already happened. So the marking is on the
    object rather than in a log.

    Parameters
    ----------
    recording : ReplayRecording
        The acquisition being replayed.
    speed : float
        The replay speed multiplier.

    Returns
    -------
    dict[str, object]
        Backend, source file, and the speed-up when there was one.
    """
    provenance: dict[str, object] = {
        "backend": REPLAY_BACKEND,
        "recorded_source": str(recording.source),
        "recorded_label": recording.label,
    }
    if speed != 1.0:
        # Present only when time was compressed, for the same reason
        # `projected_by` is present only on a projected frame: a key
        # asserting "1x" about data that was never slowed or hurried is a
        # claim nothing needed to make. When it *is* present, it says the
        # dwell in this recording's metadata is not the interval anything
        # actually waited.
        provenance["replay_speed"] = speed
    return provenance


def _calibration_metadata(calibration: FrameCalibration) -> dict[str, object]:
    """
    Render a frame calibration as the plain data a frame's metadata holds.

    Plain mappings rather than the object, matching every other adapter:
    this is what crosses a device-server boundary, and what survives
    being written into a stored pass as JSON.

    Parameters
    ----------
    calibration : FrameCalibration
        The calibration to render.

    Returns
    -------
    dict[str, object]
        One mapping per axis, keyed "y" and "x".
    """
    return {
        name: {
            "kind": axis.kind.value,
            "scale": axis.scale,
            "offset": axis.offset,
            "units": axis.units,
        }
        for name, axis in (("y", calibration.y), ("x", calibration.x))
    }


def _wait(seconds: float, speed: float) -> None:
    """
    Wait as long as the instrument did, divided by the replay speed.

    **The waiting is the point of this module**, not an affectation. An
    acquisition that returns instantly cannot show that the viewer
    freezes for the duration of a pass, that a two-minute spectrum image
    is two minutes of an operator's attention, or that a progress
    indication is missing. Those are properties of the application that
    only a device with real timing can demonstrate, and this project has
    a known limitation - the pass runs on the GUI thread - whose severity
    is invisible against a synthetic device that answers in microseconds.

    Parameters
    ----------
    seconds : float
        What the instrument waited.
    speed : float
        How many times faster to replay. Values above 1 compress time and
        are recorded in the metadata wherever they are used.
    """
    interval = seconds / speed if speed > 0 else 0.0
    if interval > 0:
        time.sleep(interval)


class ReplayScanner:
    """
    The scan unit of a recorded session, replaying one traversal.

    Implements :class:`~miainwoodpecker.devices.interface.Scanner` and
    :class:`~miainwoodpecker.devices.interface.SynchronisedScanner`, and
    it is the second implementation of the latter in this project and the
    first whose data was not invented. That matters more than it sounds:
    the preview instrument proves the *code path* works, and this proves
    it works against a real grid, a real energy axis, a real dwell, and a
    real image channel collected during the same pass.

    **The synchronisation is genuine and historical.** The HAADF channel
    this hands back was read out by the instrument during the same
    traversal that collected the spectra, so
    :data:`~miainwoodpecker.devices.interface.SCAN_SYNC_DETECTOR` is a
    statement about what happened in 2011 rather than about this adapter:
    GMS drove the spectrometer, and the scan advanced behind it. A pass
    from here therefore carries the one thing a pass exists to assert,
    and carries it on evidence.

    **The geometry cannot be argued with.** A recording is the grid the
    operator chose, and :meth:`scan_synchronised` refuses any other
    rather than resampling to it. See the module docstring.

    Parameters
    ----------
    recording : ReplayRecording
        The acquisition to replay.
    spectrometer : ReplaySpectrometer
        The detector read out at each beam position.
    speed : float
        How many times faster than the original to replay. 1.0, the
        default, is the recording's own timing; anything else compresses
        it and is stamped into the metadata of everything produced.

    Notes
    -----
    Propagates :class:`ReplayDataError` from
    :meth:`ReplayRecording.scan_parameters` when the recording is not a
    spectrum image, since a line scan or a spot spectrum is not a
    traversal this can replay.
    """

    def __init__(
        self,
        recording: ReplayRecording,
        spectrometer: ReplaySpectrometer,
        speed: float = 1.0,
    ) -> None:
        # Asked for eagerly, so a recording that cannot be scanned fails
        # when the device is built rather than when someone presses
        # Acquire two minutes into a session.
        self._parameters = recording.scan_parameters()
        self._recording = recording
        self._spectrometer = spectrometer
        self._speed = speed
        self._pass_index = 0

    @property
    def scanner_id(self) -> str:
        """
        Return the stable identifier for this scanner.

        Returns
        -------
        str
            The scanner's id.
        """
        return SCANNER_ID

    @property
    def channel_names(self) -> Sequence[str]:
        """
        Return the detector channel names.

        Returns
        -------
        typing.Sequence[str]
            One channel when the set has an image from the traversal, and
            none when it does not. A scan unit with no fitted intensity
            detector is unusual but real, and claiming a channel that
            would then hand back nothing is worse than reporting none.
        """
        return [HAADF_CHANNEL] if self._recording.image is not None else []

    def native_parameters(self) -> ScanParameters:
        """
        Return the only geometry this device can acquire.

        Not part of :class:`~miainwoodpecker.devices.interface.Scanner`,
        and asked for by name: a caller that wants to acquire from this
        device needs to know what it holds, because a replay's grid is a
        fact about the file rather than a request. The viewer asks
        through :func:`native_scan_parameters`, which returns None for
        every device that has no such constraint.

        Returns
        -------
        ScanParameters
            The recording's grid, dwell and field of view.
        """
        return self._parameters

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """
        Return the survey image of the recorded region.

        A live scan of a replayed session is the survey the operator took
        of that region: it is a real image of the same specimen at the
        same place, which is what a live view is for. It is deliberately
        **not** the image channel from the spectrum image, which belongs
        to the pass and would imply an acquisition that is not happening.

        Parameters
        ----------
        parameters : ScanParameters
            The requested geometry. Its shape cannot be honoured - a
            replay has one survey at one sampling - but its dwell is
            honoured as the time this call takes, so a live loop against
            this device runs at the rate the numbers say.
        channel : int
            Which detector channel; only channel 0 exists.

        Returns
        -------
        Frame
            The survey image, calibrated in nanometres.

        Raises
        ------
        IndexError
            If a channel this scanner does not have is requested.
        ReplayDataError
            If the set has no survey image to show.
        """
        if not 0 <= channel < max(1, len(self.channel_names)):
            msg = (
                f"channel {channel} does not exist on {self.scanner_id}; "
                f"it has {len(self.channel_names)}"
            )
            raise IndexError(msg)
        if self._recording.survey is None:
            msg = (
                f"{self._recording.label} has no survey image, so there is "
                f"nothing to show as a live scan; the spectrum image itself "
                f"is acquired with Acquire spectrum image"
            )
            raise ReplayDataError(msg)
        data = np.asarray(self._recording.survey)
        pixel_nm = self._recording.survey_pixel_size_nm or 1.0
        _wait(
            parameters.height * parameters.width * parameters.pixel_time_us / 1e6,
            self._speed,
        )
        return Frame(
            data=data,
            timestamp=_now(),
            metadata={
                **_provenance(self._recording, self._speed),
                "device_id": self.scanner_id,
                "channel_index": channel,
                "channel_name": HAADF_CHANNEL,
                "frame_index": self._pass_index,
                "fov_size_nm": [
                    data.shape[0] * pixel_nm,
                    data.shape[1] * pixel_nm,
                ],
                **_instrument_metadata(self._recording),
            },
        )

    def scan_frames(
        self,
        parameters: ScanParameters,
        channels: Sequence[int],
    ) -> Sequence[Frame]:
        """
        Scan once and return one frame per requested channel.

        This device has at most one intensity channel, so the simultaneous
        case is the single-channel case: one pass with one readout is
        still one pass, which is what the interface says a single-channel
        request always is.

        Parameters
        ----------
        parameters : ScanParameters
            Scan geometry and timing.
        channels : Sequence[int]
            Detector channel indices to read out.

        Returns
        -------
        typing.Sequence[Frame]
            One frame per requested channel, in request order.

        Raises
        ------
        ValueError
            If no channels are requested, or one is requested twice.

        Notes
        -----
        Propagates :class:`IndexError` from :meth:`scan_frame` for a
        channel this scanner does not have, which is the exception the
        single-channel path already raises for that mistake.
        """
        requested = list(channels)
        if not requested:
            msg = "a scan pass must read at least one channel"
            raise ValueError(msg)
        if len(set(requested)) != len(requested):
            msg = (
                f"channels {requested} names a detector twice; "
                "one detector cannot be read out twice in one pass"
            )
            raise ValueError(msg)
        return [self.scan_frame(parameters, index) for index in requested]

    def synchronised_targets(self) -> Sequence[str]:
        """
        Return the detectors this scanner can read out during a pass.

        Returns
        -------
        typing.Sequence[str]
            The spectrometer, always: a recording exists because it was
            read out during a traversal.
        """
        return [EELS_TARGET]

    def scan_synchronised(
        self,
        parameters: ScanParameters,
        *,
        channels: Sequence[int] = (),
        targets: Sequence[str] = (),
        into: Mapping[str, typing.Any] | None = None,
    ) -> ScanPass:
        """
        Replay the traversal, one beam position at a time, at its own speed.

        The loop is the point. It writes each position into the caller's
        destination as it goes and waits the instrument's dwell between
        them, so a pass from this device takes as long as it took, fills
        an on-disk dataset chunk by chunk as it runs, and exercises every
        part of the acquisition path that a device answering instantly
        cannot reach.

        Parameters
        ----------
        parameters : ScanParameters
            The beam-position grid. Must be the recording's own; see
            Raises.
        channels : Sequence[int]
            Intensity channels to read out. May be empty.
        targets : Sequence[str]
            Detector targets to read out at each position.
        into : Mapping[str, typing.Any] | None
            Pre-allocated destinations by target name, filled in place.
            None allocates.

        Returns
        -------
        ScanPass
            The traversal and everything read out of it.

        Raises
        ------
        ValueError
            If nothing was asked for, a channel is named twice, or a
            target is not one this scanner can synchronise.
        IndexError
            If a channel this scanner does not have is requested.

        Notes
        -----
        Propagates :class:`ReplayDataError` from :meth:`_check_geometry`
        when the requested geometry is not the recording's. Refused
        rather than resampled to: a cube of the requested shape whose
        every pixel was interpolated is the one outcome this whole path
        exists to prevent, and it looks exactly like a real one.
        """
        requested = list(channels)
        wanted = list(targets)
        if not requested and not wanted:
            msg = (
                "a synchronised pass must read something out - name at "
                "least one channel or one target"
            )
            raise ValueError(msg)
        if len(set(requested)) != len(requested):
            msg = f"channels {requested} names a detector twice"
            raise ValueError(msg)
        for index in requested:
            if not 0 <= index < len(self.channel_names):
                msg = (
                    f"channel {index} does not exist on {self.scanner_id}; "
                    f"it has {len(self.channel_names)}"
                )
                raise IndexError(msg)
        unknown = [name for name in wanted if name != EELS_TARGET]
        if unknown:
            msg = (
                f"cannot synchronise {unknown}; {self.scanner_id} can "
                f"synchronise {self.synchronised_targets()}"
            )
            raise ValueError(msg)
        self._check_geometry(parameters)

        pass_id = f"{self._recording.label}-replay-{self._pass_index}"
        self._pass_index += 1
        timestamp = _now()
        spectra = {}
        if wanted:
            spectra[EELS_TARGET] = self._replay_spectra(
                pass_id, timestamp, (into or {}).get(EELS_TARGET),
            )
        else:
            # No detector asked for, so nothing waits per position on its
            # behalf - but the probe still traversed the grid, and a pass
            # that returned instantly would misreport what it cost.
            _wait(
                self._parameters.height
                * self._parameters.width
                * self._recording.pixel_time_s,
                self._speed,
            )
        images = [self._image_channel(index, pass_id, timestamp, requested)
                  for index in requested]
        return ScanPass(
            pass_id=pass_id,
            parameters=self._parameters,
            # Not this adapter's claim: the instrument read the image
            # channel out during the spectrum image's own acquisition,
            # with GMS driving the spectrometer and the scan following.
            scan_sync=SCAN_SYNC_DETECTOR,
            images=images,
            spectra=spectra,
        )

    def _check_geometry(self, parameters: ScanParameters) -> None:
        """
        Refuse a grid this recording does not have.

        Only the *shape* is checked, and deliberately not the dwell or
        the field of view: those describe how the acquisition was taken,
        which a caller cannot change but also cannot get wrong in a way
        that corrupts anything - the pass reports the recording's own
        values regardless. The shape is different, because it decides how
        much data there is.

        Parameters
        ----------
        parameters : ScanParameters
            The requested geometry.

        Raises
        ------
        ReplayDataError
            If the requested shape is not the recording's.
        """
        if parameters.shape == self._parameters.shape:
            return
        msg = (
            f"{self._recording.label} was acquired over "
            f"{self._parameters.height}x{self._parameters.width} beam "
            f"positions and cannot be replayed over "
            f"{parameters.height}x{parameters.width}; a replay hands back "
            f"the positions the probe actually visited, and resampling to "
            f"another grid would invent every one of them"
        )
        raise ReplayDataError(msg)

    def _replay_spectra(
        self,
        pass_id: str,
        timestamp: datetime.datetime,
        # ANN401: `into` is deliberately any object with a shape that can
        # be assigned at a beam position - an ndarray or an h5py dataset.
        destination: typing.Any | None,  # noqa: ANN401
    ) -> Spectrum:
        """
        Walk the grid, writing one recorded spectrum per beam position.

        Parameters
        ----------
        pass_id : str
            Identifier shared by every output of this pass.
        timestamp : datetime.datetime
            When the replay started.
        destination : typing.Any | None
            A pre-allocated cube to fill, or None to allocate one.

        Returns
        -------
        Spectrum
            The rank-3 spectrum image, energy on the last axis.

        Raises
        ------
        ValueError
            If the destination does not match what the pass produces.
        """
        height, width = self._parameters.shape
        channels = self._recording.channel_count
        expected = (height, width, channels)
        cube = destination
        if cube is None:
            cube = np.empty(expected, dtype=np.float32)
        elif tuple(cube.shape) != expected:
            msg = (
                f"destination for {EELS_TARGET} has shape "
                f"{tuple(cube.shape)}, but the pass produces {expected} "
                f"({height}x{width} beam positions of {channels} channels)"
            )
            raise ValueError(msg)
        source = self._recording.spectra
        dwell_s = self._recording.pixel_time_s
        for row in range(height):
            for column in range(width):
                _wait(dwell_s, self._speed)
                # Written through as it goes rather than assigned whole at
                # the end: the destination may be an h5py dataset chunked
                # one beam position per chunk, and then this is a single
                # chunk write that overlaps the next position's dwell.
                # Assigning the recording's array in one statement would
                # be faster and would make the streaming untested.
                cube[row, column] = source[row, column]
        return Spectrum(
            data=cube,
            timestamp=timestamp,
            energy_offset_ev=self._recording.energy_offset_ev,
            energy_scale_ev=self._recording.energy_scale_ev,
            metadata={
                **_provenance(self._recording, self._speed),
                "device_id": self._spectrometer.camera_id,
                "camera_type": EELS_TECHNIQUE,
                TECHNIQUE_KEY: EELS_TECHNIQUE,
                "scan_pass_id": pass_id,
                "scan_sync": SCAN_SYNC_DETECTOR,
                "simultaneous_with": [
                    self._spectrometer.camera_id,
                    *([self.scanner_id] if self.channel_names else []),
                ],
                "fov_size_nm": list(self._parameters.fov_size_nm),
                "pixel_time_us": self._parameters.pixel_time_us,
                "exposure_ms": self._recording.exposure_ms,
                "binning": self._recording.binning,
                **_instrument_metadata(self._recording),
            },
        )

    def _image_channel(
        self,
        channel: int,
        pass_id: str,
        timestamp: datetime.datetime,
        simultaneous: Sequence[int],
    ) -> Frame:
        """
        Return the intensity channel the instrument read out during the pass.

        Parameters
        ----------
        channel : int
            Which channel; only 0 exists.
        pass_id : str
            Identifier shared by every output of this pass.
        timestamp : datetime.datetime
            When the replay started.
        simultaneous : Sequence[int]
            Every channel read out during this pass.

        Returns
        -------
        Frame
            The image channel, on the pass's own grid.
        """
        return Frame(
            data=np.asarray(self._recording.image),
            timestamp=timestamp,
            metadata={
                **_provenance(self._recording, self._speed),
                "device_id": self.scanner_id,
                "frame_index": channel,
                "channel_index": channel,
                "channel_name": HAADF_CHANNEL,
                "scan_pass_id": pass_id,
                "simultaneous_channels": list(simultaneous),
                "fov_nm": self._parameters.fov_nm,
                "fov_size_nm": list(self._parameters.fov_size_nm),
                "pixel_time_us": self._parameters.pixel_time_us,
                **_instrument_metadata(self._recording),
            },
        )

    def close(self) -> None:
        """Release nothing; this scanner owns no resources."""


def _instrument_metadata(recording: ReplayRecording) -> dict[str, object]:
    """
    Return the instrument state the recording reported, omitting what it did not.

    An absent key means "not reported", which a stored zero would not -
    the rule every adapter here follows. This session recorded its
    accelerating voltage and its drift-tube setting and left probe
    current at zero, which is why the beam current is not passed through:
    a literal 0 A is what the tag says and is not what the instrument was
    doing.

    Parameters
    ----------
    recording : ReplayRecording
        The acquisition.

    Returns
    -------
    dict[str, object]
        Whatever of the vocabulary this recording can honestly fill.
    """
    metadata: dict[str, object] = {}
    if recording.high_tension_v:
        metadata[HIGH_TENSION_V_KEY] = recording.high_tension_v
    if recording.energy_offset_v:
        metadata["energy_offset_ev"] = recording.energy_offset_v
    return metadata
@dataclasses.dataclass(frozen=True)
class ReplayDevices:
    """
    The devices of one replayed acquisition.

    Deliberately the same shape as
    :class:`~miainwoodpecker.viewer.preview.PreviewDevices` and
    :class:`~miainwoodpecker.devices.remote.RemoteInstrumentDevices`
    where they overlap, so code that opens a window against one opens a
    window against this unchanged. That is the whole claim of the device
    layer, and a third implementation is what tests it.

    Attributes
    ----------
    scanner : ReplayScanner
        The scan unit.
    cameras : Mapping[str, ReplaySpectrometer]
        The spectrometer, by target name.
    instrument : ReplayInstrument
        The instrument state the recording reported.
    recording : ReplayRecording
        What is being replayed.
    stage_size_nm : float
        The extent of the recorded region.
    """

    scanner: ReplayScanner
    cameras: Mapping[str, ReplaySpectrometer]
    instrument: ReplayInstrument
    recording: ReplayRecording
    stage_size_nm: float


def build_replay_devices(
    directory: pathlib.Path | str,
    index: str | None = None,
    speed: float = 1.0,
) -> ReplayDevices:
    """
    Open a recorded acquisition as a set of devices.

    Parameters
    ----------
    directory : pathlib.Path | str
        A session directory of DigitalMicrograph files.
    index : str | None
        Which acquisition, e.g. ``"004"``. None takes the first.
    speed : float
        How many times faster than the original to replay. Must be
        positive. 1.0 is the recording's own timing, which is the
        default because a device that silently ran faster than the
        instrument would be misreporting the one thing this adapter adds
        over a file reader.

    Returns
    -------
    ReplayDevices
        The assembled devices, sharing one recording.

    Raises
    ------
    ValueError
        If ``speed`` is not positive.

    Notes
    -----
    Propagates :class:`ReplayDataError` from :func:`load` when the
    directory holds no such recording, and from :class:`ReplayScanner`
    when the recording is not a spectrum image.
    """
    if not speed > 0:
        msg = f"speed must be positive, got {speed!r}"
        raise ValueError(msg)
    recording = load(directory, index)
    spectrometer = ReplaySpectrometer(recording, speed=speed)
    scanner = ReplayScanner(recording, spectrometer, speed=speed)
    instrument = ReplayInstrument(recording)
    return ReplayDevices(
        scanner=scanner,
        cameras={EELS_TARGET: spectrometer},
        instrument=instrument,
        recording=recording,
        stage_size_nm=instrument.stage_size_nm(),
    )


def describe_recordings(directory: pathlib.Path | str) -> str:
    """
    Return a human-readable listing of what a session directory holds.

    Reads only the headers, so listing a session is cheap even when the
    spectra behind it are gigabytes.

    Parameters
    ----------
    directory : pathlib.Path | str
        A session directory.

    Returns
    -------
    str
        One line per acquisition, naming what each set contains.

    Notes
    -----
    Propagates :class:`ReplayDataError` when the directory does not
    exist.
    """
    lines = []
    for index, found in find_recordings(directory).items():
        parts = [found.spectrum_image.name]
        if found.image is not None:
            parts.append("+image")
        if found.survey is not None:
            parts.append("+survey")
        lines.append(f"  {index}  {' '.join(parts)}")
    if not lines:
        return f"{directory} holds no spectrum-image recording"
    return "\n".join(lines)


def parse_replay_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse the replay viewer's command-line arguments.

    Parameters
    ----------
    argv : list[str] | None
        Argument list, or None to read ``sys.argv``.

    Returns
    -------
    argparse.Namespace
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Open the live viewer against a recorded acquisition, replayed "
            "at the speed it was taken. The data is real and was acquired "
            "elsewhere; every frame says so."
        ),
    )
    parser.add_argument(
        "directory",
        help="a session directory of DigitalMicrograph files",
    )
    parser.add_argument(
        "--index",
        default=None,
        help="which acquisition to replay, e.g. 004 (default: the first)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        metavar="N",
        help=(
            "replay N times faster than the instrument acquired. The "
            "default of 1 is the recording's own timing; anything else is "
            "recorded in the metadata of everything produced"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the acquisitions in the directory and exit",
    )
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "session directory for recordings made from the replay. These "
            "are real NeXus files holding real data acquired elsewhere; "
            "point this somewhere that will not be mistaken for original "
            "work"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    Open the live viewer against a recorded acquisition.

    Parameters
    ----------
    argv : list[str] | None
        Argument list, or None to read ``sys.argv``.

    Returns
    -------
    int
        Process exit status; 2 when the recording could not be opened,
        which is the status ``camera_server`` and ``spectrum_server``
        already use for "there was nothing to open" as distinct from a
        crash.
    """
    args = parse_replay_args(argv)
    if args.list:
        try:
            print(describe_recordings(args.directory))  # noqa: T201 - a CLI listing
        except ReplayDataError as error:
            print(f"error: {error}")  # noqa: T201 - a CLI diagnostic
            return NO_RECORDING_EXIT_STATUS
        return 0

    try:
        devices = build_replay_devices(args.directory, args.index, args.speed)
    except (ReplayDataError, ValueError) as error:
        print(f"error: {error}")  # noqa: T201 - a CLI diagnostic
        return NO_RECORDING_EXIT_STATUS

    import napari  # noqa: PLC0415 - the CLI needs the viewer extra; the devices do not

    from miainwoodpecker.storage.session import Session  # noqa: PLC0415
    from miainwoodpecker.viewer.live import LiveInstrumentWidget  # noqa: PLC0415

    recording = devices.recording
    height, width = recording.navigation_shape
    low_ev = recording.energy_offset_ev
    high_ev = low_ev + recording.energy_scale_ev * recording.channel_count
    seconds = height * width * recording.pixel_time_s / args.speed
    faster = f" at {args.speed:g}x" if args.speed != 1.0 else ""
    # Printed before the window opens because it is what an operator needs
    # in order to decide whether to press anything: how big the grid is,
    # what energies it covers, and how long a pass will take.
    print(  # noqa: T201 - a CLI banner
        f"replaying {recording.label}: {height}x{width} beam positions, "
        f"{recording.channel_count} channels, {low_ev:.0f}-{high_ev:.0f} eV, "
        f"{recording.pixel_time_s:g} s/pixel{faster} "
        f"({seconds:.0f} s a pass)",
    )
    title = f"miainwoodpecker ({REPLAY_BACKEND}: {recording.label})"
    viewer = napari.Viewer(title=title)
    widget = LiveInstrumentWidget(
        viewer,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    if args.session is not None:
        widget.set_session(Session(args.session))
    viewer.window.add_dock_widget(widget, area="right", name="Instrument")
    napari.run()
    return 0


NO_RECORDING_EXIT_STATUS = 2
"""
Exit status for "the requested recording could not be opened".

Distinct from a crash so a launcher can tell "nothing to open" from "the
adapter is broken", matching ``camera_server``'s ``NO_CAMERA`` and
``nion_server``'s ``NO_HARDWARE``.
"""
