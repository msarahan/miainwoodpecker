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
calibration (via :func:`miainwoodpecker.storage.nexus.read_calibration`,
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

    from miainwoodpecker.storage.calibration import AxisCalibration, FrameCalibration


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


def _read(
    path: os.PathLike[str] | str,
) -> tuple[np.ndarray, np.ndarray, FrameCalibration]:
    """
    Read a written file's frame stack, frame times, and axis calibration.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`~miainwoodpecker.storage.nexus.NexusWriter`.

    Returns
    -------
    tuple[np.ndarray, np.ndarray, FrameCalibration]
        The ``(n_frames, height, width)`` stack, the per-frame elapsed
        times, and the ``y``/``x`` calibration.

    Notes
    -----
    Propagates :class:`~miainwoodpecker.storage.layout.NoFramesError`
    from :func:`~miainwoodpecker.storage.nexus.read_frames` when the file
    recorded no frames.
    """
    return read_frames(path)


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

    Propagates a ``ValueError`` from :func:`_read` if the file was written
    by an acquisition that produced no frames, so it has no
    ``/entry/data`` group to read.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`~miainwoodpecker.storage.nexus.NexusWriter`.

    Returns
    -------
    hyperspy.signals.Signal2D
        The frame stack, with calibrated navigation and signal axes.
    """
    data, frame_time, calibration = _read(path)

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

    Raises
    ------
    ValueError
        If the file recorded no frames, or if it has no single
        energy-calibrated axis to flatten along — including the
        physically meaningless case of both axes claiming to be energy.
    """
    data, frame_time, calibration = _read(path)
    energy_name = calibration.energy_axis_name()
    if energy_name is None:
        msg = (
            f"{path} has no single energy-calibrated axis (y={calibration.y.units!r}, "
            f"x={calibration.x.units!r}), so there is no dispersive direction to "
            f"flatten along; record it with an energy calibration (see "
            f"FrameCalibration.spectrum) rather than assuming one"
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
