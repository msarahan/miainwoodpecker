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
calibration with ``h5py`` (already a base dependency; no new I/O code)
and hand the arrays to ``hyperspy.signals.Signal2D``, which does the
actual axis bookkeeping. Nothing here reimplements anything HyperSpy
itself provides.

Requires the ``analysis`` optional dependency group
(``pip install miainwoodpecker[analysis]``).
"""

from __future__ import annotations

import typing

import h5py
import hyperspy.api as hs

if typing.TYPE_CHECKING:
    import os

    import numpy as np


def _axis_calibration(values: np.ndarray) -> tuple[float, float]:
    """
    Return ``(scale, offset)`` for a NeXus axis dataset's sample values.

    Parameters
    ----------
    values : np.ndarray
        The axis dataset's values, in acquisition/sample order.

    Returns
    -------
    tuple[float, float]
        The per-step spacing and the first value, in that dataset's
        units. Falls back to a unit scale when fewer than two samples
        are available to take a spacing from.
    """
    offset = float(values[0]) if len(values) else 0.0
    scale = float(values[1] - values[0]) if len(values) > 1 else 1.0
    return scale, offset


def load_as_hyperspy_signal(path: os.PathLike[str] | str) -> hs.signals.Signal2D:
    """
    Read a NexusWriter file as a HyperSpy ``Signal2D``, frames as navigation.

    The frame-stack dataset (``/entry/data/data``, shape
    ``(n_frames, height, width)``) becomes a ``Signal2D`` with one
    navigation axis (frame index, calibrated in seconds from
    ``frame_time``) and two signal axes (``y``, ``x``), calibrated in
    nanometres when the recording carried a field of view, or left as
    bare pixel indices otherwise — exactly the units/values already
    written to the file (see :mod:`miainwoodpecker.storage.nexus`), just
    handed to HyperSpy's own calibration model instead of re-derived.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`~miainwoodpecker.storage.nexus.NexusWriter`.

    Returns
    -------
    hyperspy.signals.Signal2D
        The frame stack, with calibrated navigation and signal axes.

    Raises
    ------
    ValueError
        If the file was written by an acquisition that produced no
        frames (so it has no ``/entry/data`` group to read).
    """
    with h5py.File(path, "r") as handle:
        entry = handle["entry"]
        if "data" not in entry:
            msg = f"{path} has no /entry/data group; it recorded no frames"
            raise ValueError(msg)
        data_group = entry["data"]
        data = data_group["data"][()]
        x_values = data_group["x"][()]
        y_values = data_group["y"][()]
        x_units = data_group["x"].attrs["units"]
        y_units = data_group["y"].attrs["units"]
        frame_time = data_group["frame_time"][()]

    signal = hs.signals.Signal2D(data)
    nav_axis = signal.axes_manager.navigation_axes[0]
    x_axis, y_axis = signal.axes_manager.signal_axes

    nav_axis.name = "frame_time"
    nav_axis.units = "s"
    nav_axis.scale, nav_axis.offset = _axis_calibration(frame_time)

    x_axis.name = "x"
    x_axis.units = x_units
    x_axis.scale, x_axis.offset = _axis_calibration(x_values)

    y_axis.name = "y"
    y_axis.units = y_units
    y_axis.scale, y_axis.offset = _axis_calibration(y_values)

    return signal
