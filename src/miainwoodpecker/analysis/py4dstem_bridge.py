"""
Adapter: read a NexusWriter file as a py4DSTEM ``DiffractionSlice``.

Phase 4 (migration plan, §5) picked HyperSpy first, over py4DSTEM/LiberTEM,
because the device layer had no synchronized scan-position/camera-frame
acquisition mode - so there was no 4D-STEM (scan_y, scan_x, det_y, det_x)
data for py4DSTEM's headline ``DataCube`` type to operate on, only plain
frame stacks. This module follows up on py4DSTEM specifically, after
*checking, not assuming*, whether that constraint has moved.

**It hasn't, and the reason is more specific than "the interface doesn't
expose it yet."** Even reaching past the vendor-neutral ``Scanner``/
``Camera`` protocols into the simulated device stack's own internals
(``nion.usim_device``), a genuine software step-scan - move the beam to a
point, grab a Ronchigram frame, repeat - does not produce real
scan-position-varying diffraction data today. The simulator does have a
settable per-point beam position
(``nion.device_kit.InstrumentDevice.Instrument.probe_position``, a public
``STEMController`` attribute) and the Ronchigram simulator's own code
(``nion.usim_device.RonchigramCameraSimulator.get_frame_data``) does read
it to offset the simulated aberrations - but only through
``CameraSimulator._get_frame_settings``, which first asks
``self.instrument.scan_controller`` for a registered ``ScanHardwareSource``
and silently ignores ``probe_position`` entirely if that resolves to
``None``. That registration only happens inside the full
``HardwareSource``/``Application`` layer - the same layer Phase 0 already
found too heavy to stand up outside Swift's own process (migration plan,
§5, Phase 0's note on ``AcquisitionTestContext``). Measured directly
against ``nion.usim_device.DeviceConfiguration.AcquisitionContextConfiguration``
(the same lightweight construction ``nion_server.py`` uses, with no
``HardwareSource`` registered): setting ``instrument.probe_position`` to
different points and re-acquiring a Ronchigram frame each time changes
nothing beyond shot noise - the mean absolute difference between frames
taken at *different* probe positions (12.31 counts) is statistically
identical to the noise floor from re-acquiring at the *same* fixed
position twice (12.31 counts), and the disk's brightest pixel jumps
around randomly call to call even with the probe held still. So even the
raw simulator, reached by going around this project's own device wrapper
entirely, cannot produce a real scan-position-varying diffraction signal
without standing up the heavier application layer this project
deliberately avoids. That forecloses py4DSTEM's ``DataCube`` for this PoC,
same as it did for HyperSpy's pick. (A separate check confirmed there is
no way around this by using external data instead: py4DSTEM ships a
Google Drive-backed sample-dataset downloader with real, non-synthetic
4D-STEM datacubes, but Google Drive is unreachable from this environment
- the outbound proxy returns ``403`` on the CONNECT tunnel to
``drive.google.com`` for both py4DSTEM's own downloader and a bare
``gdown`` call. py4DSTEM 0.14.18's downloader is also, independently,
broken against the ``gdown`` release it resolves today - it passes a
``fuzzy=`` keyword ``gdown.download`` no longer accepts - so this path is
blocked twice over, not just by network policy.)

What *is* real: single Ronchigram frames acquired one at a time through
the existing ``Camera`` protocol are genuine diffraction patterns (shot
noise and all), and py4DSTEM ships real operations that work on exactly
one diffraction pattern at a time - central-disk calibration
(``py4DSTEM.process.calibration.get_probe_size``), radial profiles
(``py4DSTEM.process.utils.radial_integral``) - because py4DSTEM applies
these same per-pattern functions internally even when it does have a full
datacube. This module reads a NexusWriter file's frame(s) with ``h5py``
(same pattern as :mod:`miainwoodpecker.analysis.hyperspy_bridge`) and
hands them to ``py4DSTEM.data.DiffractionSlice``, py4DSTEM's own
diffraction-space container for one pattern (or a small labelled stack of
them) - carrying over the axis calibration NexusWriter already wrote, the
same way the HyperSpy adapter hands its axis values to ``AxesManager``
instead of re-deriving them.

Requires the ``py4dstem`` optional dependency group
(``pip install miainwoodpecker[py4dstem]``) - kept separate from the
``analysis`` extra HyperSpy uses; see pyproject.toml for the measured
dependency-count comparison.
"""

from __future__ import annotations

import typing

import h5py
from py4DSTEM.data import Calibration, DiffractionSlice

if typing.TYPE_CHECKING:
    import os

    import numpy as np

# py4DSTEM.data.Calibration.Q_pixel_units only accepts these three literal
# values (see the assert in py4DSTEM/data/calibration.py's set_Q_pixel_units)
# - "pixels" is the only one NexusWriter's own vocabulary ("pixel", singular)
# maps onto. A calibrated diffraction-plane unit (A^-1, mrad) would need
# per-camera calibration data the device interface doesn't expose - the same
# real caveat hyperspy_bridge.py records for the Ronchigram camera.
_Q_PIXEL_UNITS = {"pixel": "pixels"}


def _axis_scale(values: np.ndarray) -> float:
    """
    Return the per-step spacing of a NeXus axis dataset's sample values.

    Parameters
    ----------
    values : np.ndarray
        The axis dataset's values, in acquisition/sample order.

    Returns
    -------
    float
        The spacing between consecutive samples, in that dataset's units.
        Falls back to a unit scale when fewer than two samples are
        available to take a spacing from.
    """
    return float(values[1] - values[0]) if len(values) > 1 else 1.0


def load_as_diffraction_slice(path: os.PathLike[str] | str) -> DiffractionSlice:
    """
    Read a NexusWriter file as a py4DSTEM ``DiffractionSlice``.

    The frame-stack dataset (``/entry/data/data``, shape
    ``(n_frames, height, width)``) becomes a single ``(height, width)``
    ``DiffractionSlice`` when the file holds exactly one frame, or a
    labelled stack (string ``slicelabels``, one per frame index)
    otherwise - calibrated on py4DSTEM's diffraction-plane (``Q``) axis
    from exactly the axis values :mod:`miainwoodpecker.storage.nexus`
    already wrote. No axis math is reimplemented; py4DSTEM's own
    ``Calibration`` object does the bookkeeping, same as
    :func:`~miainwoodpecker.analysis.hyperspy_bridge.load_as_hyperspy_signal`
    delegates to HyperSpy's ``AxesManager``.

    Parameters
    ----------
    path : os.PathLike[str] | str
        An HDF5 file written by :class:`~miainwoodpecker.storage.nexus.NexusWriter`.

    Returns
    -------
    py4DSTEM.data.DiffractionSlice
        The frame(s), calibrated on the diffraction-plane (``Q``) axis.

    Raises
    ------
    ValueError
        If the file was written by an acquisition that produced no
        frames (so it has no ``/entry/data`` group to read), or if its
        axes carry a unit ``Calibration.Q_pixel_units`` does not accept
        (real-space nanometre calibration, from a scan recording rather
        than a camera one - this adapter is for diffraction-plane data,
        matching what py4DSTEM's single-pattern operations expect).
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

    x_scale = _axis_scale(x_values)
    y_scale = _axis_scale(y_values)
    if x_units != y_units or x_units not in _Q_PIXEL_UNITS or x_scale != y_scale:
        msg = (
            f"{path}'s axes are calibrated as x={x_scale!r} {x_units!r}, "
            f"y={y_scale!r} {y_units!r}; py4DSTEM.data.Calibration.Q_pixel_size/"
            "Q_pixel_units model a single isotropic diffraction-plane scale "
            "in 'pixels', 'A^-1', or 'mrad' - this adapter is for camera "
            "(diffraction-plane) recordings, not scan (real-space) ones."
        )
        raise ValueError(msg)

    calibration = Calibration()
    calibration.Q_pixel_size = x_scale
    calibration.Q_pixel_units = _Q_PIXEL_UNITS[x_units]

    n_frames = data.shape[0]
    if n_frames == 1:
        return DiffractionSlice(
            data[0],
            name="miainwoodpecker frame",
            calibration=calibration,
        )
    return DiffractionSlice(
        data,
        name="miainwoodpecker frame stack",
        slicelabels=[str(index) for index in range(n_frames)],
        calibration=calibration,
    )
