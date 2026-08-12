"""
Adapter: read a NexusWriter file as a HyperSpy signal.

HyperSpy is the Phase 4 pick (migration plan, §5) over py4DSTEM/LiberTEM:
it is the lighter, more general of the three, works directly on the plain
2D image/scan stacks this app actually produces today (frames from
``Scanner``/``Camera``, no 4D-STEM diffraction-scan structure yet), and
already ships the axis-calibration model (``AxesManager``) this adapter
hands data to rather than reimplementing. py4DSTEM/LiberTEM stay the
right call *if/when* a real 4D-STEM (scan-position x diffraction-pattern)
acquisition mode exists; nothing here forecloses adding a second adapter
for one of them alongside this one later.

Our NeXus layout (see :mod:`miainwoodpecker.storage.nexus`) is our own
convention over plain HDF5, not one of the vendor/community formats
RosettaSciIO (HyperSpy's I/O backend) already reads — so there is no
existing HyperSpy reader for it, and this small function is the genuine
gap to fill. It does the minimum: read the frame stack and its axis
calibration (via :func:`miainwoodpecker.storage.nexus.read_frames`,
so the ``units``-to-axis-kind inference lives in one place for all three
adapters) and hand the arrays to ``hyperspy.signals.Signal2D``, which does
the actual axis bookkeeping. Nothing here reimplements anything HyperSpy
itself provides.

Of the three adapters this is the one whose object model actually fits:
``AxesManager`` carries ``scale``/``offset``/``units`` **per axis**, which
is exactly the shape of :class:`~miainwoodpecker.storage.calibration.FrameCalibration`,
so every axis kind round-trips without translation — real space in
nanometres, reciprocal space in ``1/nm``, a scattering angle in ``mrad``,
an EELS frame's energy axis in ``eV`` next to an uncalibrated cross axis,
or the honest ``"pixel"`` fallback. Contrast
:mod:`miainwoodpecker.analysis.py4dstem_bridge`, which has one isotropic
diffraction scale and a three-value unit vocabulary to squeeze through, and
:mod:`miainwoodpecker.analysis.libertem_bridge`, which has nowhere to put
calibration at all.

Because the model says *which* axis is energy-dispersive,
:func:`load_as_hyperspy_spectrum` can do what the domain actually asks for:
a spectrum acquired as a 2D image, flattened to one dimension. That is a
``Signal1D``, not a ``Signal2D`` — HyperSpy's own type for a spectrum — and
it is a separate function rather than a mode of the first, because
flattening throws away a real axis and should be asked for explicitly.

Two entry points per signal type, and the pairing is the point
--------------------------------------------------------------
Each ``load_as_*`` function reads a file; each ``*_from_frames`` function
takes frames somebody has already read
(:class:`~miainwoodpecker.storage.nexus.FrameStack`, which is exactly what
:func:`~miainwoodpecker.storage.nexus.read_frames` returns) and reads
nothing. The first is a one-line composition of the second, so there is one
implementation and no chance of the two drifting apart.

Separate names rather than one function taking a path *or* an array,
deliberately. The viewer's analyze-from-disk path exists to avoid reading a
2048x2048 recording twice — once for display, once for the adapter — and
the whole benefit turns on which of the two happens. A union-typed
parameter would hide that behind an ``isinstance`` check at the bottom of
the call stack; a name says it at the call site, where the person deciding
is. It also keeps the path-taking form's signature exactly as
``docs/scripting-and-automation.md`` documents it, so no script has to
change.

Frames rather than a bare array, for the reason the calibration model
exists at all: an array handed over without its axes produces a signal that
silently claims bare pixels, and a wrong axis is worse than a duplicated
read. ``FrameStack`` carries the calibration with the data so passing one
without the other is not a thing a caller can do by accident.

Where EELS and EDX meet, and why it is here
--------------------------------------------
Two spectroscopies arrive by two device paths and have to end in one
place. An EEL spectrometer disperses onto a **camera**, so the device
produces a 2D
:class:`~miainwoodpecker.devices.interface.Frame` whose one direction is
energy, and it is stored as a frame stack. An EDX silicon drift detector
is **natively** 1D, so it produces a
:class:`~miainwoodpecker.devices.interface.Spectrum` and is stored in
``NXspectrum``'s layout
(:mod:`miainwoodpecker.storage.spectra`). Both are, in the end, a
spectrum with a real energy axis, and both are analysed as one.

:func:`load_as_hyperspy_spectrum` is therefore **one function that reads
both**, dispatching on which signal dataset the file actually holds
rather than on what the caller believes. That is deliberate and it is the
alternative to the obvious design, which would have been a second
``load_as_eds_spectrum`` beside the existing one: two functions
returning the same type from two layouts, diverging quietly the first
time either grew an option. The EELS behaviour is unchanged — same
flattening, same refusal when no single axis is energy — and a spectrum
recording simply takes a shorter route to the same ``Signal1D``.

:func:`load_as_eds_signal` then sits *on top of* that shared path rather
than beside it: it loads the spectrum exactly as above and adds the EDS
signal type and eXSpy's detector metadata. Anything that later wants
``EELS`` typing for the camera path adds the same kind of thin layer and
inherits the same loader.

Requires the ``analysis`` optional dependency group
(``pip install miainwoodpecker[analysis]``).
:func:`load_as_eds_signal` additionally needs ``exspy``, which is where
HyperSpy 2.x keeps its EELS and EDS signal classes — measured: a bare
HyperSpy 2.4 install knows *no* signal types at all
(``hs.print_known_signal_types()`` returns an empty table), and
``EDSTEMSpectrum`` lives in ``exspy.signals``.
"""

from __future__ import annotations

import typing

import hyperspy.api as hs

from miainwoodpecker.devices.interface import HIGH_TENSION_V_KEY
from miainwoodpecker.storage import layout
from miainwoodpecker.storage.calibration import (
    AXIS_NAMES,
    AxisCalibration,
    AxisKind,
)
from miainwoodpecker.storage.nexus import read_frames
from miainwoodpecker.storage.spectra import read_spectra

if typing.TYPE_CHECKING:
    import os

    import numpy as np

    from miainwoodpecker.storage.nexus import FrameStack

_UNNAMED_SOURCE = "this recording"
"""
What an error message calls frames whose caller did not name them.

The arrays carry no provenance of their own, so a file-reading caller
passes ``source=`` to keep the filename in the message it always had.
"""

_EDS_TEM = "EDS_TEM"
_EDS_SEM = "EDS_SEM"
# The energy unit this project writes, and one eXSpy accepts natively -
# measured in exspy.signals.eds._get_line_energy, which handles "eV" and
# "keV" and raises for anything else.
_ENERGY_UNITS = "eV"


def _frame_time_calibration(values: np.ndarray) -> tuple[float, float]:
    """
    Return ``(scale, offset)`` for the frame-time navigation axis.

    Parameters
    ----------
    values : np.ndarray
        The ``frame_time`` dataset's values, in acquisition order.

    Returns
    -------
    tuple[float, float]
        The per-frame spacing and the first value, in seconds. Falls back
        to a unit scale when fewer than two frames are available to take a
        spacing from.
    """
    offset = float(values[0]) if len(values) else 0.0
    scale = float(values[1] - values[0]) if len(values) > 1 else 1.0
    return scale, offset


def _apply(axis: object, name: str, calibration: AxisCalibration) -> None:
    """
    Copy one axis calibration onto a HyperSpy axis object.

    Parameters
    ----------
    axis : object
        A HyperSpy ``UniformDataAxis`` from an ``AxesManager``.
    name : str
        The axis name to set, matching the on-disk dataset name.
    calibration : AxisCalibration
        The calibration to transfer verbatim — no axis math is redone.
    """
    axis.name = name
    axis.units = calibration.units
    axis.scale = calibration.scale
    axis.offset = calibration.offset


def load_as_hyperspy_signal(path: os.PathLike[str] | str) -> hs.signals.Signal2D:
    """
    Read a NexusWriter file as a HyperSpy ``Signal2D``, frames as navigation.

    The frame-stack dataset (``/entry/data/data``, shape
    ``(n_frames, height, width)``) becomes a ``Signal2D`` with one
    navigation axis (frame index, calibrated in seconds from
    ``frame_time``) and two signal axes (``y``, ``x``), each carrying
    whatever kind of calibration the recording was written with —
    nanometres for a scan's field of view, ``1/nm`` or ``mrad`` for a
    diffraction pattern, ``eV`` for a spectrum image's dispersive
    direction, or the honest ``"pixel"`` fallback when nothing was
    supplied. The two signal axes are handled independently, because on an
    EELS frame they genuinely differ.

    Propagates a ``ValueError`` from
    :func:`~miainwoodpecker.storage.nexus.read_frames` if the file was
    written by an acquisition that produced no frames, so it has no
    ``/entry/data`` group to read.

    Reads the file. A caller that already holds the frames — the viewer,
    which read them to display them — should call
    :func:`hyperspy_signal_from_frames` instead and read nothing.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`~miainwoodpecker.storage.nexus.NexusWriter`.

    Returns
    -------
    hyperspy.signals.Signal2D
        The frame stack, with calibrated navigation and signal axes.
    """
    return hyperspy_signal_from_frames(read_frames(path))


def hyperspy_signal_from_frames(frames: FrameStack) -> hs.signals.Signal2D:
    """
    Build a HyperSpy ``Signal2D`` from frames already in memory.

    The half of :func:`load_as_hyperspy_signal` that does not touch the
    disk: it takes the stack, times, and calibration that
    :func:`~miainwoodpecker.storage.nexus.read_frames` returns and does only
    the axis bookkeeping. Nothing here re-reads or re-derives anything, so
    the result is identical to the path-taking form's — that form is one
    call to this one.

    Parameters
    ----------
    frames : FrameStack
        The frames to wrap, with the calibration they were recorded with.
        Passing the calibration is not optional, because a ``Signal2D``
        built without it claims bare pixel axes and says nothing about it.

    Returns
    -------
    hyperspy.signals.Signal2D
        The frame stack, with calibrated navigation and signal axes.
    """
    data, frame_time, calibration = frames

    signal = hs.signals.Signal2D(data)
    nav_axis = signal.axes_manager.navigation_axes[0]
    nav_axis.name = "frame_time"
    nav_axis.units = "s"
    nav_axis.scale, nav_axis.offset = _frame_time_calibration(frame_time)

    # HyperSpy orders signal axes fastest-first, i.e. (x, y) against our
    # (y, x) dataset order.
    x_axis, y_axis = signal.axes_manager.signal_axes
    _apply(x_axis, "x", calibration.x)
    _apply(y_axis, "y", calibration.y)
    return signal


def load_as_hyperspy_spectrum(path: os.PathLike[str] | str) -> hs.signals.Signal1D:
    """
    Read any spectrum recording this project writes as a HyperSpy ``Signal1D``.

    Two on-disk layouts reach one signal type, which is the whole point:

    - A **frame** recording whose axis calibration names one direction as
      energy — an EELS camera. Spectra in this domain are "often acquired
      as 2D images and then flattened to one dimension", and this does
      that flattening, summing along the non-dispersive direction (which
      is what integrating a spectrum image across the spectrometer slit
      means) and keeping the frame axis as navigation.
    - A **spectrum** recording written by
      :class:`~miainwoodpecker.storage.spectra.SpectrumWriter` — an EDX
      detector, or anything else natively one-dimensional. Nothing is
      flattened, because nothing was ever two-dimensional; the energy
      axis is read straight off the file.

    Which one a file is, is read from the file rather than asked of the
    caller: a spectrum recording's signal dataset is ``intensity`` and a
    frame recording's is ``data``, so the question has an answer on disk.

    For the frame path it refuses rather than guesses when the recording
    does not say which direction is energy: picking the longer axis would
    be a plausible heuristic that is silently wrong on a rotated camera,
    and a wrongly assigned energy axis makes every eV value downstream
    wrong.

    Reads the file; :func:`hyperspy_spectrum_from_frames` is the same
    flattening applied to frames a caller already holds.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by
        :class:`~miainwoodpecker.storage.nexus.NexusWriter` with an
        energy-calibrated axis (see
        :meth:`miainwoodpecker.storage.calibration.FrameCalibration.spectrum`),
        or by :class:`~miainwoodpecker.storage.spectra.SpectrumWriter`.

    Returns
    -------
    hyperspy.signals.Signal1D
        One signal axis in the recording's energy units, and the
        navigation axes the recording has: frame time in seconds for a
        flattened camera recording, spectrum index for a series of spot
        spectra, or ``y``/``x`` in nanometres for a spectrum image.

    Notes
    -----
    Raises ``ValueError`` if the file recorded no frames (from
    :func:`~miainwoodpecker.storage.nexus.read_frames`), or if it has no
    single energy-calibrated axis to flatten along — including the
    physically meaningless case of both axes claiming to be energy (from
    :func:`hyperspy_spectrum_from_frames`, which is where that check
    lives).
    """
    if _holds_spectra(path):
        # A spectrum recording is already 1D and already has its energy
        # axis, so it takes the short route: no frames to read, nothing to
        # flatten. The dispatch is on what the file actually holds rather
        # than on what the caller believes, which is why this is one
        # function and not two -- an EELS camera stack and an EDX
        # recording are the same Signal1D by the time anyone analyses
        # them, and two loaders would have diverged the first time either
        # grew an option.
        return _spectrum_recording_as_signal(path)
    return hyperspy_spectrum_from_frames(read_frames(path), source=str(path))


def hyperspy_spectrum_from_frames(
    frames: FrameStack,
    *,
    source: str = _UNNAMED_SOURCE,
) -> hs.signals.Signal1D:
    """
    Flatten frames already in memory into a HyperSpy ``Signal1D``.

    The disk-free half of :func:`load_as_hyperspy_spectrum`; see that
    function for what the flattening means and why the dispersive direction
    is read rather than guessed.

    Parameters
    ----------
    frames : FrameStack
        The frames to flatten, with the calibration that says which
        direction is energy — without it there is nothing to flatten along
        and this raises, which is the intended outcome rather than a
        limitation.
    source : str
        What to call these frames in an error message. The arrays carry no
        provenance, so the file-reading form passes its path here to keep
        the filename in the sentence an operator sees.

    Returns
    -------
    hyperspy.signals.Signal1D
        Shape ``(n_frames, n_channels)``: one navigation axis in seconds
        and one signal axis in the recording's energy units.

    Raises
    ------
    ValueError
        If the frames have no single energy-calibrated axis to flatten
        along — including the physically meaningless case of both axes
        claiming to be energy.
    """
    data, frame_time, calibration = frames
    energy_name = calibration.energy_axis_name()
    if energy_name is None:
        msg = (
            f"{source} has no single energy-calibrated axis "
            f"(y={calibration.y.units!r}, x={calibration.x.units!r}), so there "
            f"is no dispersive direction to flatten along; record it with an "
            f"energy calibration (see FrameCalibration.spectrum) rather than "
            f"assuming one"
        )
        raise ValueError(msg)

    # data is (frames, y, x); sum away whichever spatial axis is not energy.
    summed_axis = 1 + AXIS_NAMES.index("y" if energy_name == "x" else "x")
    signal = hs.signals.Signal1D(data.sum(axis=summed_axis))

    nav_axis = signal.axes_manager.navigation_axes[0]
    nav_axis.name = "frame_time"
    nav_axis.units = "s"
    nav_axis.scale, nav_axis.offset = _frame_time_calibration(frame_time)
    _apply(
        signal.axes_manager.signal_axes[0],
        energy_name,
        calibration.axis(energy_name),
    )
    return signal


def _holds_spectra(path: os.PathLike[str] | str) -> bool:
    """
    Return whether a file is a spectrum recording rather than a frame stack.

    Parameters
    ----------
    path : os.PathLike[str] | str
        Any HDF5 file this project wrote.

    Returns
    -------
    bool
        True if the file carries ``NXspectrum``'s ``intensity`` signal.
    """
    import h5py  # noqa: PLC0415 - a base dependency, imported where it is used

    with h5py.File(path, "r") as handle:
        return layout.SPECTRUM_INTENSITY in handle


def _spectrum_recording_as_signal(
    path: os.PathLike[str] | str,
) -> hs.signals.Signal1D:
    """
    Read a ``SpectrumWriter`` recording as a ``Signal1D``.

    The short route to the same destination as the flattening path: the
    counts are already spectra, so the only work is transferring the
    energy axis and whatever navigation axes the recording has.

    Parameters
    ----------
    path : os.PathLike[str] | str
        A file written by
        :class:`~miainwoodpecker.storage.spectra.SpectrumWriter`.

    Returns
    -------
    hyperspy.signals.Signal1D
        The spectra, with a calibrated energy signal axis.
    """
    recording = read_spectra(path)
    signal = hs.signals.Signal1D(recording.data)
    energy = AxisCalibration(
        AxisKind.ENERGY,
        recording.energy_scale_ev,
        recording.energy_offset_ev,
        _ENERGY_UNITS,
    )
    _apply(signal.axes_manager.signal_axes[0], "Energy", energy)

    navigation = signal.axes_manager.navigation_axes
    if recording.is_map:
        # HyperSpy orders navigation axes fastest-first, i.e. (x, y)
        # against our (y, x) dataset order - the same transposition
        # load_as_hyperspy_signal already handles for signal axes, and the
        # same trap: getting it wrong swaps the two spatial scales and is
        # invisible on a square map.
        x_axis, y_axis = navigation
        _apply(x_axis, "x", recording.navigation.x)
        _apply(y_axis, "y", recording.navigation.y)
    elif navigation:
        index_axis = navigation[0]
        index_axis.name = "spectrum_index"
        # No units, and not "s": a series of spot spectra records which
        # spectrum each one is, not when it was taken. Calling it a time
        # would be inventing a clock the recording does not carry.
        index_axis.units = ""
        index_axis.scale = 1.0
        index_axis.offset = 0.0
    return signal


def load_as_eds_signal(
    path: os.PathLike[str] | str,
    *,
    signal_type: str = _EDS_TEM,
) -> hs.signals.Signal1D:
    """
    Read a spectrum recording as an eXSpy EDS signal, metadata and all.

    A thin layer over :func:`load_as_hyperspy_spectrum` rather than a
    second loader: the arrays and the energy axis come from there, and
    what this adds is the two things that make an EDS *signal* rather
    than an anonymous ``Signal1D`` — the signal type, and eXSpy's
    ``Acquisition_instrument.TEM.Detector.EDS`` metadata, without which
    ``get_lines_intensity`` and every quantification fall back on eXSpy's
    *defaults* for the detector geometry rather than this instrument's.

    The metadata mapping is not invented here. It is the EMSA/MAS
    standard's spectral header, which is what RosettaSciIO's ``msa``
    reader already maps onto exactly these eXSpy items and what both
    Bruker and Oxford export; see
    :class:`~miainwoodpecker.devices.interface.Spectrum` for the device
    side of the same table. Two unit conversions happen here and nowhere
    else, because eXSpy's units differ from this project's:

    - ``beam_energy`` is **keV** in eXSpy and volts in the recording's
      ``high_tension_v``, so it is divided by 1000.
    - the energy axis stays **eV**, which eXSpy accepts (its
      ``_get_line_energy`` handles ``"eV"`` and ``"keV"`` and raises for
      anything else — measured in ``exspy.signals.eds``), so nothing is
      rescaled and the file's own axis is what gets used.

    Parameters
    ----------
    path : os.PathLike[str] | str
        A file written by
        :class:`~miainwoodpecker.storage.spectra.SpectrumWriter`.
    signal_type : str
        The eXSpy signal type to set. ``"EDS_TEM"`` by default because
        that is what an EDX detector on a STEM column produces; pass
        ``"EDS_SEM"`` for an SEM, which differs in eXSpy's quantification
        model rather than in the data.

    Returns
    -------
    hyperspy.signals.Signal1D
        An ``exspy.signals.EDSTEMSpectrum`` (or the SEM equivalent) with
        a calibrated energy axis and the detector metadata the recording
        carried.

    Raises
    ------
    ValueError
        If the file is a frame recording rather than a spectrum
        recording — an EELS camera stack reaches ``Signal1D`` through
        :func:`load_as_hyperspy_spectrum` and is not EDS data.
    ImportError
        If ``exspy`` is not installed, since HyperSpy 2.x keeps its EDS
        signal classes there and without it ``set_signal_type`` silently
        produces a plain ``Signal1D``.
    """
    if not _holds_spectra(path):
        msg = (
            f"{path} is a frame recording, not a spectrum recording, so it is "
            f"not EDS data; load it with load_as_hyperspy_spectrum (an EELS "
            f"camera stack) or load_as_hyperspy_signal (an image stack)"
        )
        raise ValueError(msg)
    try:
        import exspy  # noqa: F401, PLC0415 - imported for its signal-type registration
    except ImportError as error:
        msg = (
            "reading a recording as an EDS signal needs 'exspy', which is "
            "where HyperSpy 2.x keeps its EELS and EDS signal classes - a "
            "bare HyperSpy install knows no signal types at all, so "
            "set_signal_type('EDS_TEM') would quietly leave you with a plain "
            "Signal1D. Install this project's 'analysis' extra, or exspy "
            "directly. load_as_hyperspy_spectrum needs none of this and "
            "returns the same data with the same energy axis."
        )
        raise ImportError(msg) from error

    signal = load_as_hyperspy_spectrum(path)
    signal.set_signal_type(signal_type)
    recording = read_spectra(path)
    _apply_eds_metadata(signal, recording.metadata[0], signal_type)
    return signal


def _apply_eds_metadata(
    signal: hs.signals.Signal1D,
    metadata: dict[str, object],
    signal_type: str,
) -> None:
    """
    Copy a recording's detector facts into eXSpy's metadata tree.

    Only what the recording actually reported is set. An absent key is
    left absent rather than written as zero, so eXSpy's own default
    stands and is visibly a default — the same rule the device layer
    follows when attaching metadata in the first place.

    Parameters
    ----------
    signal : hs.signals.Signal1D
        The signal to annotate, already typed.
    metadata : dict[str, object]
        The first recorded spectrum's metadata.
    signal_type : str
        ``"EDS_TEM"`` or ``"EDS_SEM"``, which selects the branch of
        eXSpy's tree the items belong under.
    """
    column = "SEM" if signal_type == _EDS_SEM else "TEM"
    detector = f"Acquisition_instrument.{column}.Detector.EDS"
    items: list[tuple[str, object]] = []
    voltage = metadata.get(HIGH_TENSION_V_KEY)
    if isinstance(voltage, (int, float)) and not isinstance(voltage, bool):
        # eXSpy's beam_energy is in keV; ours is in volts, as everywhere
        # else in this project. One division, in one place.
        beam_energy_kev = float(voltage) / 1000.0
        items.append(
            (f"Acquisition_instrument.{column}.beam_energy", beam_energy_kev),
        )
    for key, item in (
        ("live_time_s", f"{detector}.live_time"),
        ("real_time_s", f"{detector}.real_time"),
        ("azimuth_deg", f"{detector}.azimuth_angle"),
        ("elevation_deg", f"{detector}.elevation_angle"),
        ("solid_angle_sr", f"{detector}.solid_angle"),
        ("energy_resolution_ev", f"{detector}.energy_resolution_MnKa"),
        ("beam_current_a", f"Acquisition_instrument.{column}.beam_current"),
    ):
        value = metadata.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            items.append((item, float(value)))
    detector_type = metadata.get("detector_type")
    if detector_type is not None:
        items.append((f"{detector}.EDS_det", str(detector_type)))
    for item, value in items:
        signal.metadata.set_item(item, value)
