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

Requires the ``analysis`` optional dependency group
(``pip install miainwoodpecker[analysis]``).
"""

from __future__ import annotations

import typing

import hyperspy.api as hs

from miainwoodpecker.storage.calibration import AXIS_NAMES
from miainwoodpecker.storage.nexus import read_frames

if typing.TYPE_CHECKING:
    import os

    import numpy as np

    from miainwoodpecker.storage.calibration import AxisCalibration
    from miainwoodpecker.storage.nexus import FrameStack

_UNNAMED_SOURCE = "this recording"
"""
What an error message calls frames whose caller did not name them.

The arrays carry no provenance of their own, so a file-reading caller
passes ``source=`` to keep the filename in the message it always had.
"""


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
    Read a spectrum-image recording as a flattened HyperSpy ``Signal1D``.

    Spectra in this domain are "often acquired as 2D images and then
    flattened to one dimension" — one detector direction is
    energy-dispersive and the other is not. This does that flattening,
    summing along the non-dispersive direction (which is what integrating
    a spectrum image across the spectrometer slit means) and keeping the
    frame axis as navigation, so the result is HyperSpy's own spectrum
    type with a real energy axis.

    It refuses rather than guesses when the recording does not say which
    direction is energy: picking the longer axis would be a plausible
    heuristic that is silently wrong on a rotated camera, and a wrongly
    assigned energy axis makes every eV value downstream wrong.

    Reads the file; :func:`hyperspy_spectrum_from_frames` is the same
    flattening applied to frames a caller already holds.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`~miainwoodpecker.storage.nexus.NexusWriter`
        with an energy-calibrated axis (see
        :meth:`miainwoodpecker.storage.calibration.FrameCalibration.spectrum`).

    Returns
    -------
    hyperspy.signals.Signal1D
        Shape ``(n_frames, n_channels)``: one navigation axis in seconds
        and one signal axis in the recording's energy units.

    Notes
    -----
    Raises ``ValueError`` if the file recorded no frames (from
    :func:`~miainwoodpecker.storage.nexus.read_frames`), or if it has no
    single energy-calibrated axis to flatten along — including the
    physically meaningless case of both axes claiming to be energy (from
    :func:`hyperspy_spectrum_from_frames`, which is where that check
    lives).
    """
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
