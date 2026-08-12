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

:func:`load_as_eds_signal` and :func:`load_as_eels_signal` then sit *on
top of* that shared path rather than beside it: each loads the spectrum
exactly as above and adds only the two things the shared loader cannot —
the eXSpy signal type, and the metadata that spectroscopy's own
quantification reads. They are twins by construction, and each refuses
the *other* one's recordings rather than typing them: both
spectroscopies end as the same ``Signal1D``, so nothing downstream would
catch an EELS recording wearing ``EDS_TEM`` (eXSpy would fit X-ray lines
to electron energy losses) or an EDX one wearing ``EELS`` (it would fit
ionisation edges to X-ray lines). For a frame recording the layout
answers by itself; for a spectrum recording it no longer can, because a
camera's **projected readout** stores EELS in the same ``NXspectrum``
layout as EDX — there the check is of what the recording *says it is*,
the ``technique`` metadata that
:func:`~miainwoodpecker.storage.spectra.spectrum_from_projected_frame`
stamps and ``SpectrumWriter`` writes into ``NXdetector``'s description.

Requires the ``analysis`` optional dependency group
(``pip install miainwoodpecker[analysis]``).
:func:`load_as_eds_signal` and :func:`load_as_eels_signal` additionally
need ``exspy``, which is where HyperSpy 2.x keeps its EELS and EDS signal
classes — measured: a bare HyperSpy 2.4 install knows *no* signal types
at all (``hs.print_known_signal_types()`` returns an empty table), so
``set_signal_type("EELS")`` silently leaves a plain ``Signal1D``, and
both ``EDSTEMSpectrum`` and ``EELSSpectrum`` live in ``exspy.signals``.

The one asymmetry between the two is the energy unit, and it is the
reason :func:`load_as_eels_signal` normalizes the axis rather than
trusting it. eXSpy's **EDS** side validates: ``_get_line_energy``
(``exspy.signals.eds``) accepts ``"eV"`` and ``"keV"`` and raises for
anything else. Its **EELS** side does not check the axis units anywhere —
measured by reading ``exspy/signals/eels.py`` in full — while assuming eV
throughout: the ionisation-edge table it matches against
``axes_manager.signal_axes[0].axis`` is in eV
(``onset_energy (eV)``), ``align_zero_loss_peak``'s subpixel window
defaults to ``(-3.0, 3.0)``, and ``kramers_kronig_analysis`` works in eV.
This project's energy vocabulary also admits ``meV`` and ``keV``
(:mod:`miainwoodpecker.storage.calibration`), so a recording in either
would be silently misread by every one of those. Hence the exact,
within-kind conversion in :func:`load_as_eels_signal`.
"""

from __future__ import annotations

import json
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
from miainwoodpecker.storage.spectra import (
    EELS_TECHNIQUE,
    TECHNIQUE_KEY,
    read_spectra,
)

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
_EELS = "EELS"
# The energy unit this project writes, and one eXSpy accepts natively -
# measured in exspy.signals.eds._get_line_energy, which handles "eV" and
# "keV" and raises for anything else. It is also the unit eXSpy's EELS
# side assumes without ever checking, which is why load_as_eels_signal
# converts to it rather than asserting it.
_ENERGY_UNITS = "eV"
# Volts to eXSpy's keV, amps to its nA, milliseconds to its seconds.
# Every unit gap between this project's operator units and eXSpy's, in
# one place, so a factor cannot go loose in the metadata - which would
# make every number computed from it wrong by exactly that factor with
# nothing saying so. See docs/adapters/spectrum-detectors.md, section 4.
_VOLTS_PER_KILOVOLT = 1000.0
_NANOAMPS_PER_AMP = 1.0e9
_MILLISECONDS_PER_SECOND = 1000.0


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


def _recorded_technique(metadata: dict[str, object]) -> str:
    """
    Return what kind of spectroscopy a spectrum recording says it holds.

    Parameters
    ----------
    metadata : dict[str, object]
        The recording's first spectrum's metadata.

    Returns
    -------
    str
        The ``technique`` value, lower-cased (``"eels"``, ``"eds"``, …),
        or ``""`` when the recording does not say.

    Notes
    -----
    The two loaders read this asymmetrically, deliberately. ``"eels"``
    is a *claim*, and :func:`load_as_eels_signal` requires it, because
    the only thing making a projected camera readout distinguishable
    from EDX on disk is the recording saying so. Silence is not that
    claim, so it stays EDX's — this layout was EDX's before projection
    landed in it, and every recording written before that predates the
    key entirely.
    """
    return str(metadata.get(TECHNIQUE_KEY, "") or "").lower()


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
        :func:`load_as_eels_signal` and is not EDS data — or a spectrum
        recording whose ``technique`` metadata says it is EELS: a
        camera's projected readout lands in the same ``NXspectrum``
        layout as EDX, so the refusal keys on what the recording says it
        is rather than on its shape, which can no longer tell.

    Notes
    -----
    Propagates ``ImportError`` from :func:`_require_exspy` when ``exspy``
    is not installed, since HyperSpy 2.x keeps its EDS signal classes
    there and without it ``set_signal_type`` silently produces a plain
    ``Signal1D``.
    """
    if not _holds_spectra(path):
        msg = (
            f"{path} is a frame recording, not a spectrum recording, so it is "
            f"not EDS data; load it with load_as_eels_signal (an EELS camera "
            f"stack), load_as_hyperspy_spectrum (the same, untyped), or "
            f"load_as_hyperspy_signal (an image stack)"
        )
        raise ValueError(msg)
    recording = read_spectra(path)
    if _recorded_technique(recording.metadata[0]) == EELS_TECHNIQUE:
        # A projected EELS camera readout lands in the same NXspectrum
        # layout as EDX, so the shape can no longer tell them apart - but
        # the recording says what it is (`technique`, written into
        # NXdetector's description), and eXSpy fitting X-ray lines to
        # electron energy losses is exactly the mix-up this refusal
        # exists to prevent.
        msg = (
            f"{path} is an EELS spectrum recording (its technique metadata "
            f"says 'eels' - a camera's projected readout), not EDS data; "
            f"load it with load_as_eels_signal, or with "
            f"load_as_hyperspy_spectrum for an untyped Signal1D"
        )
        raise ValueError(msg)
    _require_exspy("EDS", signal_type)

    signal = load_as_hyperspy_spectrum(path)
    signal.set_signal_type(signal_type)
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
    for key, item, factor in (
        # eXSpy's beam_energy is in keV; ours is in volts, as everywhere
        # else in this project.
        (
            HIGH_TENSION_V_KEY,
            f"Acquisition_instrument.{column}.beam_energy",
            1.0 / _VOLTS_PER_KILOVOLT,
        ),
        # And its beam_current is in **nanoamps**, measured in
        # exspy.signals.eds_tem: the dose calculation behind
        # quantification reads it as nA and multiplies by 1e-9 to reach
        # coulombs ("1e-9 is included here because the beam_current is in
        # nA"). This project records amps, so a 200 pA probe written
        # straight through arrived as 2e-10 nA and made every dose a
        # billion times too small - silently, since neither end range
        # checks it.
        (
            "beam_current_a",
            f"Acquisition_instrument.{column}.beam_current",
            _NANOAMPS_PER_AMP,
        ),
        ("live_time_s", f"{detector}.live_time", 1.0),
        ("real_time_s", f"{detector}.real_time", 1.0),
        ("azimuth_deg", f"{detector}.azimuth_angle", 1.0),
        ("elevation_deg", f"{detector}.elevation_angle", 1.0),
        ("solid_angle_sr", f"{detector}.solid_angle", 1.0),
        ("energy_resolution_ev", f"{detector}.energy_resolution_MnKa", 1.0),
    ):
        value = _number(metadata.get(key))
        if value is not None:
            items.append((item, value * factor))
    detector_type = metadata.get("detector_type")
    if detector_type is not None:
        items.append((f"{detector}.EDS_det", str(detector_type)))
    for item, value in items:
        signal.metadata.set_item(item, value)


def _number(value: object) -> float | None:
    """
    Return a recorded value as a float, or ``None`` if it is not a number.

    The rule the whole metadata mapping runs on: only what the recording
    actually reported is set, and an absent or non-numeric entry is left
    absent rather than written as zero. ``bool`` is excluded deliberately
    — it is an ``int`` in Python and a flag in every recording that
    carries one, so ``True`` reaching a beam energy would be a fabricated
    1 keV.

    Parameters
    ----------
    value : object
        A value read out of a recording's metadata mapping.

    Returns
    -------
    float | None
        The value as a float, or ``None`` if it is not a real number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _require_exspy(technique: str, signal_type: str) -> None:
    """
    Import eXSpy, or raise a message naming what to install.

    Imported for its side effect: eXSpy registers its signal classes with
    HyperSpy on import, and nothing here uses the module object. Doing it
    lazily, inside the function, is what lets this module be imported
    without the extra — the same rule every other analysis import in this
    project follows.

    Parameters
    ----------
    technique : str
        ``"EDS"`` or ``"EELS"``, for the sentence an operator reads.
    signal_type : str
        The signal type that would have been silently ignored.

    Raises
    ------
    ImportError
        If ``exspy`` is not installed. Raised rather than returning
        something that looks right, because a bare HyperSpy install knows
        *no* signal types at all — measured:
        ``hs.print_known_signal_types()`` returns an empty table — so
        ``set_signal_type`` neither errors nor warns, it simply does
        nothing.
    """
    try:
        import exspy  # noqa: F401, PLC0415 - imported for its signal-type registration
    except ImportError as error:
        msg = (
            f"reading a recording as an {technique} signal needs 'exspy', "
            f"which is where HyperSpy 2.x keeps its EELS and EDS signal "
            f"classes - a bare HyperSpy install knows no signal types at all, "
            f"so set_signal_type({signal_type!r}) would quietly leave you with "
            f"a plain Signal1D. Install this project's 'analysis' extra, or "
            f"exspy directly. load_as_hyperspy_spectrum needs none of this and "
            f"returns the same data with the same energy axis."
        )
        raise ImportError(msg) from error


def load_as_eels_signal(path: os.PathLike[str] | str) -> hs.signals.Signal1D:
    """
    Read an EELS camera recording as an eXSpy ``EELSSpectrum``.

    The exact counterpart of :func:`load_as_eds_signal`, and a thin layer
    over :func:`load_as_hyperspy_spectrum` for the same reason: the
    arrays, the flattening across the spectrometer slit, and the energy
    axis all come from there, and what this adds is the signal type and
    the instrument metadata eXSpy's EELS model reads.

    **The energy axis is normalized to eV here, and that is not
    cosmetic.** eXSpy's EDS side validates its axis unit
    (``_get_line_energy`` accepts ``"eV"``/``"keV"`` and raises
    otherwise); its EELS side never checks. It assumes eV everywhere
    instead — the ionisation-edge onsets it matches against the axis are
    tabulated in eV, ``align_zero_loss_peak``'s subpixel window defaults
    to ±3 *eV*, and ``kramers_kronig_analysis`` works in eV. This
    project's energy vocabulary also admits ``meV`` and ``keV``, so a
    spectrometer recorded in either would be misread by all of them
    without a word. The conversion is
    :meth:`~miainwoodpecker.storage.calibration.AxisCalibration.converted_to`,
    which is exact within a kind, so an eV recording (every one this
    project's device layer produces today) is untouched.

    **What eXSpy metadata this can and cannot fill in.** Set, from the
    frame metadata the recording carries:

    - ``Acquisition_instrument.TEM.beam_energy`` — from
      ``high_tension_v``, **divided by 1000** because eXSpy holds it in
      keV and this project holds accelerating voltage in volts.
    - ``Acquisition_instrument.TEM.beam_current`` — from
      ``beam_current_a``, **times 1e9** because eXSpy holds it in nA.
    - ``Acquisition_instrument.TEM.Detector.EELS.exposure`` — from
      ``exposure_ms``, in seconds, which is the unit RosettaSciIO's
      DigitalMicrograph reader maps this item from.

    **Left unset, deliberately**, because nothing this project records
    carries them and a plausible-looking wrong angle poisons every
    quantification computed from it:

    - ``Acquisition_instrument.TEM.convergence_angle`` (convergence
      semi-angle, mrad). The only place a convergence angle exists
      anywhere in this stack is usim's ``"ConvergenceAngle"`` control,
      which appears in *no* Nion package outside the simulator — reading
      it would be encoding a simulator detail as an instrument
      convention, which is the mistake this project's control-name list
      is careful not to make.
    - ``Acquisition_instrument.TEM.Detector.EELS.collection_angle``
      (collection semi-angle, mrad). It is set by the spectrometer
      entrance aperture and the camera length, neither of which any
      device here reports.
    - ``…Detector.EELS.spectrometer``, ``…aperture_size``,
      ``…frame_number``. A recording knows the *camera*'s name, not the
      spectrometer's model or its aperture; ``frame_number`` counts
      readouts summed into one spectrum, which this project never
      requests.

    Leaving them out is not merely honest, it is *safe*: eXSpy checks for
    exactly those three items (``_are_microscope_parameters_missing``)
    and **refuses** the operations that need them — ``estimate_thickness``
    with a ``density`` raises rather than applying an angular correction
    from someone else's geometry. Supply them per session with eXSpy's
    own ``signal.set_microscope_parameters(...)``, which is the
    documented way in and the only one that puts the operator's knowledge
    where it belongs.

    Reads the file; there is no frames-taking twin because the metadata
    this adds is read from the file, not from a
    :class:`~miainwoodpecker.storage.nexus.FrameStack`.

    Parameters
    ----------
    path : os.PathLike[str] | str
        A frame recording written by
        :class:`~miainwoodpecker.storage.nexus.NexusWriter` whose axis
        calibration names one direction as energy — an EEL spectrometer
        dispersing onto a camera (see
        :meth:`miainwoodpecker.storage.calibration.FrameCalibration.spectrum`)
        — or a spectrum recording whose ``technique`` metadata says it is
        EELS, which is what a camera's **projected readout** produces:
        the same spectrometer, already summed to 1D at the device, stored
        by :class:`~miainwoodpecker.storage.spectra.SpectrumWriter` in
        the ``NXspectrum`` layout with the technique stamped by
        :func:`~miainwoodpecker.storage.spectra.spectrum_from_projected_frame`.

    Returns
    -------
    hyperspy.signals.Signal1D
        An ``exspy.signals.EELSSpectrum`` with an eV energy axis, the
        navigation axis the recording has (frame time in seconds for a
        flattened camera stack, spectrum index for a projected series),
        and whatever of the metadata above the recording carried.

    Raises
    ------
    ValueError
        If the file is a spectrum recording that does **not** say it is
        EELS — an EDX detector's counts are not electron energy losses,
        and once loaded the two are indistinguishable, so this refuses
        instead of typing it. The check is of what the recording says it
        is (``technique``), because a projected EELS readout and an EDX
        spectrum wear the same layout and the shape can no longer
        answer. Also propagated from :func:`load_as_hyperspy_spectrum`
        when a frame recording has no single energy-calibrated axis to
        flatten along.

    Notes
    -----
    Propagates ``ImportError`` from :func:`_require_exspy` when ``exspy``
    is not installed, since HyperSpy 2.x keeps its EELS signal classes
    there and without it ``set_signal_type`` silently produces a plain
    ``Signal1D``.
    """
    if _holds_spectra(path):
        metadata = read_spectra(path).metadata[0]
        if _recorded_technique(metadata) != EELS_TECHNIQUE:
            msg = (
                f"{path} is a spectrum recording that does not say it is EELS "
                f"(technique={metadata.get(TECHNIQUE_KEY)!r}): this project's "
                f"spectrum layout is written by SpectrumWriter for natively 1D "
                f"detectors, of which an EDX detector is the case in hand, and "
                f"for a camera's projected EELS readout, which stamps "
                f"technique='eels'. Load it with load_as_eds_signal, or with "
                f"load_as_hyperspy_spectrum for an untyped Signal1D"
            )
            raise ValueError(msg)
    else:
        metadata = _first_frame_metadata(path)
    _require_exspy("EELS", _EELS)

    signal = load_as_hyperspy_spectrum(path)
    # A no-op for a spectrum recording, whose layout is always eV; a
    # frame recording's axis may be meV or keV and eXSpy's EELS side
    # assumes eV without checking.
    _to_electronvolts(signal.axes_manager.signal_axes[0])
    signal.set_signal_type(_EELS)
    _apply_eels_metadata(signal, metadata)
    return signal


def _to_electronvolts(axis: typing.Any) -> None:  # noqa: ANN401 - HyperSpy UniformDataAxis
    """
    Rescale one HyperSpy energy axis into electronvolts, in place.

    Parameters
    ----------
    axis : typing.Any
        A HyperSpy ``UniformDataAxis`` carrying an energy calibration in
        one of this project's energy units.

    Notes
    -----
    Propagates ``ValueError`` from
    :class:`~miainwoodpecker.storage.calibration.AxisCalibration` if the
    axis is not in an energy unit at all. Unreachable through
    :func:`load_as_eels_signal`, which reaches here only for an axis the
    calibration model already called
    :attr:`~miainwoodpecker.storage.calibration.AxisKind.ENERGY`.
    """
    if axis.units == _ENERGY_UNITS:
        return
    converted = AxisCalibration(
        AxisKind.ENERGY,
        axis.scale,
        axis.offset,
        str(axis.units),
    ).converted_to(_ENERGY_UNITS)
    _apply(axis, str(axis.name), converted)


def _first_frame_metadata(path: os.PathLike[str] | str) -> dict[str, object]:
    """
    Read a frame recording's first-frame metadata blob.

    The frame-side twin of ``read_spectra(path).metadata[0]``, which is
    where :func:`load_as_eds_signal` gets the same facts. The blob is the
    first frame's metadata, written at ``close()``; a recording that was
    never finalized simply has none, and an absent blob yields an empty
    mapping rather than an error, because "nobody recorded the beam
    energy" is not a reason to refuse to load the spectrum.

    Parameters
    ----------
    path : os.PathLike[str] | str
        A frame recording written by
        :class:`~miainwoodpecker.storage.nexus.NexusWriter`.

    Returns
    -------
    dict[str, object]
        The first frame's metadata, or ``{}`` if the file carries none.
    """
    import h5py  # noqa: PLC0415 - a base dependency, imported where it is used

    with h5py.File(path, "r") as handle:
        dataset = handle.get(layout.VENDOR_METADATA)
        raw = None if dataset is None else dataset[()]
    if raw is None:
        return {}
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    stored = json.loads(text)
    return stored if isinstance(stored, dict) else {}


def _apply_eels_metadata(
    signal: hs.signals.Signal1D,
    metadata: dict[str, object],
) -> None:
    """
    Copy a recording's instrument facts into eXSpy's EELS metadata tree.

    Only what the recording actually reported is set, by exactly the rule
    :func:`_apply_eds_metadata` follows: an absent key is left absent
    rather than written as zero, so eXSpy's own behaviour for a missing
    item stands and is visibly a default. See
    :func:`load_as_eels_signal` for the three items this can fill and the
    three it deliberately cannot.

    Parameters
    ----------
    signal : hs.signals.Signal1D
        The signal to annotate, already typed.
    metadata : dict[str, object]
        The recording's first-frame metadata.
    """
    tem = "Acquisition_instrument.TEM"
    for key, item, factor in (
        (HIGH_TENSION_V_KEY, f"{tem}.beam_energy", 1.0 / _VOLTS_PER_KILOVOLT),
        ("beam_current_a", f"{tem}.beam_current", _NANOAMPS_PER_AMP),
        (
            "exposure_ms",
            f"{tem}.Detector.EELS.exposure",
            1.0 / _MILLISECONDS_PER_SECOND,
        ),
    ):
        value = _number(metadata.get(key))
        if value is not None:
            signal.metadata.set_item(item, value * factor)
