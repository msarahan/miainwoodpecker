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

The unit impedance mismatch, and the factor of ten inside it
------------------------------------------------------------
``py4DSTEM.data.Calibration.Q_pixel_units`` accepts **only** the literal
strings ``"pixels"``, ``"A^-1"``, and ``"mrad"`` - a hard ``assert`` in
py4DSTEM's own ``set_Q_pixel_units``, not a convention. None of those is
what this project writes: NeXus wants a unit string its ``NX_WAVENUMBER``
category accepts, and measured against ``pynxtools``' own matcher,
``"A^-1"`` is **not** one of them (nor is ``"nm-1"``; ``"1/nm"`` and
``"1/angstrom"`` are). So a reciprocal axis recorded in ``1/nm`` cannot be
handed to py4DSTEM in the unit it was written in at all.

It therefore has to be converted, and the conversion is where this could go
badly wrong: **1 A^-1 is 10 nm^-1**, so a scale in ``1/nm`` becomes a scale
in ``1/angstrom`` by dividing by ten. Passing an unconverted nm^-1
magnitude while labelling it ``"A^-1"`` would leave every downstream
py4DSTEM number - probe radius, scattering vector, lattice spacing - wrong
by exactly 10x, with nothing in the file or the object saying so. The
conversion is done by
:meth:`miainwoodpecker.storage.calibration.AxisCalibration.converted_to`,
which is an exact within-kind factor table and is unit-tested
independently of py4DSTEM being installed, and the numbers are asserted in
``tests/integration/test_py4dstem_bridge.py`` specifically. This is the
same class of error the hardware checklist flags as its
highest-consequence check (a metres/nanometres mix-up, migration plan §7).

An axis this container genuinely cannot carry is refused rather than
approximated: a real-space (nanometre) recording, which is what a *scan*
produces, and an energy axis, which is what a spectrum image produces.
Both would have to be mislabelled as a diffraction-plane quantity to fit,
and the point of the calibration model is that a wrong axis is worse than
an admitted pixel index. A scattering *angle* in ``mrad`` needs no
conversion - it is one of py4DSTEM's own three - which is a second reason
:class:`~miainwoodpecker.storage.calibration.AxisKind` keeps angles
distinct from reciprocal space instead of converting them through a
wavelength nothing here knows.

One thing py4DSTEM models differently and this adapter deliberately does
not translate: the diffraction-plane **origin**. Our axes put it in each
axis's ``offset`` (a Ronchigram axis centred on the optic axis, as the
simulated camera itself reports); py4DSTEM keeps it in ``qx0``/``qy0``,
per scan position within a full ``DataCube``, not as an axis offset. There
is no single-pattern home for it, and ``get_probe_size`` measures the
centre from the data anyway, so the offset stays in the file rather than
being forced into a field that means something else.

Requires the ``py4dstem`` optional dependency group
(``pip install miainwoodpecker[py4dstem]``) - kept separate from the
``analysis`` extra HyperSpy uses; see pyproject.toml for the measured
dependency-count comparison.
"""

from __future__ import annotations

import typing

import h5py
from py4DSTEM.data import Calibration, DiffractionSlice

from miainwoodpecker.storage.calibration import PIXEL_UNITS, AxisKind
from miainwoodpecker.storage.nexus import read_calibration

if typing.TYPE_CHECKING:
    import os

# Which of our axis kinds py4DSTEM's diffraction-plane calibration can
# express, as (the unit we must convert the axis to, py4DSTEM's own label
# for it). The labels are the only three `Calibration.set_Q_pixel_units`
# asserts on; the units are the NeXus-valid spellings of the same
# quantities. `1/angstrom` -> `A^-1` is the factor-of-ten conversion the
# module docstring is about; `mrad` needs none; `pixel` -> `pixels` is the
# pre-existing singular/plural mismatch.
_Q_PIXEL_UNITS: dict[AxisKind, tuple[str, str]] = {
    AxisKind.UNCALIBRATED: (PIXEL_UNITS, "pixels"),
    AxisKind.RECIPROCAL_SPACE: ("1/angstrom", "A^-1"),
    AxisKind.ANGLE: ("mrad", "mrad"),
}


def load_as_diffraction_slice(path: os.PathLike[str] | str) -> DiffractionSlice:
    """
    Read a NexusWriter file as a py4DSTEM ``DiffractionSlice``.

    The frame-stack dataset (``/entry/data/data``, shape
    ``(n_frames, height, width)``) becomes a single ``(height, width)``
    ``DiffractionSlice`` when the file holds exactly one frame, or a
    labelled stack (string ``slicelabels``, one per frame index)
    otherwise - calibrated on py4DSTEM's diffraction-plane (``Q``) axis
    from the axis calibration :mod:`miainwoodpecker.storage.nexus` already
    wrote, converted into one of the three unit strings py4DSTEM accepts.
    That conversion is exact and is the only axis arithmetic here (see the
    module docstring for why it is a factor of ten and why getting it wrong
    would be silent); py4DSTEM's own ``Calibration`` object does the rest of
    the bookkeeping, same as
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
        frames (so it has no ``/entry/data`` group to read); if its axes
        are not both the same kind of quantity at the same scale, since
        ``Q_pixel_size`` is a single isotropic number; or if that kind is
        not one a diffraction plane can hold - real-space nanometres, from
        a *scan* recording, and an energy axis, from a spectrum image, are
        both refused rather than mislabelled as a diffraction-plane
        quantity.
    """
    with h5py.File(path, "r") as handle:
        entry = handle["entry"]
        if "data" not in entry:
            msg = f"{path} has no /entry/data group; it recorded no frames"
            raise ValueError(msg)
        data = entry["data/data"][()]
    frame_calibration = read_calibration(path)
    y_axis, x_axis = frame_calibration.y, frame_calibration.x

    if y_axis.kind is not x_axis.kind or y_axis.kind not in _Q_PIXEL_UNITS:
        msg = (
            f"{path}'s axes are calibrated as y={y_axis.scale!r} "
            f"{y_axis.units!r}, x={x_axis.scale!r} {x_axis.units!r}; "
            f"py4DSTEM.data.Calibration.Q_pixel_size/Q_pixel_units model a "
            f"single isotropic diffraction-plane scale in 'pixels', 'A^-1', "
            f"or 'mrad' - this adapter is for camera (diffraction-plane) "
            f"recordings, not scan (real-space) or spectrum (energy) ones."
        )
        raise ValueError(msg)

    nexus_units, py4dstem_units = _Q_PIXEL_UNITS[y_axis.kind]
    y_converted = y_axis.converted_to(nexus_units)
    x_converted = x_axis.converted_to(nexus_units)
    if y_converted.scale != x_converted.scale:
        msg = (
            f"{path}'s diffraction axes are anisotropic "
            f"({y_converted.scale!r} vs {x_converted.scale!r} "
            f"{nexus_units}); py4DSTEM.data.Calibration models one "
            f"Q_pixel_size for both, so this cannot be expressed without "
            f"discarding one of them."
        )
        raise ValueError(msg)

    calibration = Calibration()
    calibration.Q_pixel_size = x_converted.scale
    calibration.Q_pixel_units = py4dstem_units

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
