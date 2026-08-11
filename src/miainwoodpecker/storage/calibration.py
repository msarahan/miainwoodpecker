"""
Per-axis, per-acquisition calibration for the axes of a recorded frame.

Why this exists
---------------
Until now :mod:`miainwoodpecker.storage.nexus` could build a physical axis
for exactly one case — a scan frame reporting ``fov_nm`` — and honestly
labelled everything else ``"pixel"`` (docs/migration-plan.md, §7:
"per-detector calibration ... still absent"). That gap propagated into all
three Phase 4 analysis adapters: a Ronchigram or EELS frame reached
HyperSpy, py4DSTEM, and LiberTEM with no physical axis at all.

The model this module chooses follows from what the axes actually are, and
each of the four decisions below is a decision *against* a simpler model
that would have been wrong.

**1. The axis kind belongs to the acquisition, not to the detector.** The
same physical camera produces reciprocal-space diffraction patterns in one
microscope mode and something else in another, so ``AxisKind`` is a value
supplied per acquisition rather than a property looked up per camera.
Hard-coding "the Ronchigram camera is reciprocal space" would be a
statement about a mode, wearing a detector's name.

**2. Calibration is per axis, not per frame.** An EELS frame is a 2D image
in which *one* direction is energy-dispersive and the other is not energy
at all — so a single unit per frame cannot describe it. Measured, not
assumed, against the simulator this project actually runs:
``nion.usim_device.EELSCameraSimulator.get_dimensional_calibrations``
returns ``[Calibration(), Calibration(offset=energy_offset_eV,
scale=energy_per_channel_eV, units="eV")]`` — index 0 (the slow axis,
``y``, 256 rows on this project's real 256x1024 frames) carries *no*
calibration, and index 1 (the fast axis, ``x``, 1024 columns) is the
dispersive one at 0.5 eV/channel. Hence :meth:`FrameCalibration.spectrum`
defaults ``dispersive_axis="x"`` and leaves the other axis at the honest
pixel fallback — and takes the axis as a parameter, because a rotated
camera or a different spectrometer can put it the other way round.

**3. Angles are their own kind, because the alternative is fabrication.**
The one camera calibration that exists anywhere in this stack is angular:
``nion.usim_device.RonchigramCameraSimulator.get_dimensional_calibrations``
reports ``units="rad"`` with ``offset=-scale * n / 2`` (an axis centred on
the optic axis). Converting a scattering *angle* into a scattering
*vector* needs the electron wavelength, hence the accelerating voltage,
which the device interface does not expose — so folding angles into
``RECIPROCAL_SPACE`` would mean inventing a wavelength. ``AxisKind.ANGLE``
lets the axis be recorded as what the instrument actually reports.

**4. An uncalibrated axis stays uncalibrated.** ``AxisKind.UNCALIBRATED``
(scale 1, offset 0, units ``"pixel"``) is a first-class state and the
default, mirroring the NXem decision in §5 Phase 3: the writer declines to
claim what it cannot demonstrate. A confidently wrong reciprocal
calibration on a diffraction pattern is worse than an admitted pixel axis,
because every downstream number computed from it is wrong by that factor
and nothing says so.

What NeXus specifies, checked rather than guessed
-------------------------------------------------
NeXus has **no** attribute for "what kind of axis is this". ``NXdata``'s
``AXISNAME`` field (base_classes/NXdata.nxdl.xml) carries exactly
``units``, ``long_name``, and a few index/range hints; the *unit string* is
the entire carrier of the physics. Inventing an ``axis_kind`` attribute
would be inventing NeXus, so this module writes none — instead the unit
vocabulary below is chosen so the kind is recoverable from the unit
(:func:`axis_kind_for_units`).

``NXimage``'s own class documentation states the same per-axis rule this
module implements, for exactly this data: "``image_3d`` and ``image_4d``
``NXdata`` instances should be used for these cases with ``axis_k`` and
``axis_m`` respectively of NeXus Unit Category ``NX_LENGTH`` and
``axis_i`` and ``axis_j`` respectively of NeXus Unit Category
``NX_WAVENUMBER`` or ``NX_UNITLESS``." ``NXspectrum`` pairs an
``axis_energy`` field of category ``NX_ENERGY`` with ``NX_LENGTH`` spatial
axes. So real space -> ``NX_LENGTH``, reciprocal space ->
``NX_WAVENUMBER``, energy -> ``NX_ENERGY``, angle -> ``NX_ANGLE``: all
four are existing NeXus unit categories, and none of this is our
invention.

**The spelling matters, and one obvious spelling is wrong.** ``pynxtools``
validates a unit string against its category with
``pynxtools.units.NXUnitSet.matches``, and measured against it:
``"1/nm"``, ``"nm^-1"``, ``"1/angstrom"`` all match ``NX_WAVENUMBER``
while ``"nm-1"`` does **not**, and ``"A^-1"`` — the spelling py4DSTEM
requires — does not either. So this module's canonical reciprocal unit is
``"1/nm"``, and translating to py4DSTEM's vocabulary is the adapter's job
(see :mod:`miainwoodpecker.analysis.py4dstem_bridge`), not the file's.
``scripts/validate_nexus_schema.py`` checks every unit in the vocabulary
below against its declared category, so this claim cannot rot quietly.

Deliberately not a units framework
----------------------------------
This is a data model — five kinds, a short frozen vocabulary per kind, and
one exact conversion factor per unit. :meth:`AxisCalibration.converted_to`
converts *within* a kind only (nm <-> angstrom, 1/nm <-> 1/angstrom, eV
<-> meV, rad <-> mrad), which is all any adapter here needs and all that
can be done without physical constants. Adding ``pint`` or ``astropy.units``
to convert between kinds would be the bespoke over-build §1-§3 warns
against, and would not help: the conversions this project actually lacks
(angle -> reciprocal) need instrument state, not a unit registry.

Placed in ``storage`` because ``storage`` is what writes it, but it imports
only ``numpy`` — no ``h5py``, no device SDK — so the device layer can
eventually construct these values without depending on the file format.
"""

from __future__ import annotations

import dataclasses
import enum
import math
import typing

import numpy as np

if typing.TYPE_CHECKING:
    from collections.abc import Mapping

    import numpy.typing as npt

PIXEL_UNITS = "pixel"
"""Units written for an axis whose real calibration is unknown."""

METADATA_KEY = "calibration"
"""
Frame-metadata key :func:`resolve_frame_calibration` reads a model from.

``fov_nm`` reaches the axis code through ``Frame.metadata`` already; this
is the same route for the kinds ``fov_nm`` cannot express. See
:func:`resolve_frame_calibration` for the accepted shape.
"""

FIELD_SIZE_KEY = "fov_size_nm"
"""
Frame-metadata key carrying a scan's extent as ``(y_nm, x_nm)``.

Preferred over the scalar ``fov_nm`` because it states the extent of each
axis rather than leaving storage to re-derive it from a convention. See
:meth:`FrameCalibration.from_field_size`, and
:attr:`~miainwoodpecker.devices.interface.ScanParameters.fov_size_nm`,
which is what a scanner puts here.
"""

AXIS_NAMES = ("y", "x")
"""
The frame's two axis names, slow first, matching the on-disk datasets.

Fixed by the ``(height, width)`` convention Phase 1 pinned empirically and
by ``NXdata``'s ``axes = ["frame_time", "y", "x"]`` hint the writer emits.
"""


class AxisKind(enum.StrEnum):
    """
    What physical quantity a frame axis measures in this acquisition.

    A :class:`str` enum so a mode can be named in plain frame metadata
    (``{"kind": "energy"}``) and survive JSON, pickling over the device IPC
    boundary, and a human reading the file's vendor-metadata blob.
    """

    UNCALIBRATED = "uncalibrated"
    REAL_SPACE = "real_space"
    RECIPROCAL_SPACE = "reciprocal_space"
    ENERGY = "energy"
    ANGLE = "angle"


NEXUS_UNIT_CATEGORIES: dict[AxisKind, str] = {
    AxisKind.UNCALIBRATED: "NX_UNITLESS",
    AxisKind.REAL_SPACE: "NX_LENGTH",
    AxisKind.RECIPROCAL_SPACE: "NX_WAVENUMBER",
    AxisKind.ENERGY: "NX_ENERGY",
    AxisKind.ANGLE: "NX_ANGLE",
}
"""
The NeXus unit category each kind's units belong to.

Not written to the file — NeXus has no attribute for it — but checked in
``scripts/validate_nexus_schema.py`` against ``pynxtools``' own matcher, so
the vocabulary below cannot drift away from the categories it claims.
``UNCALIBRATED`` is the one entry whose units are deliberately outside its
category: ``"pixel"`` is not a NeXus unit at all (measured: it matches no
category), which is precisely the point of saying it.
"""

# Multiplier from each unit to its kind's canonical unit (the first entry).
# Exact by construction - every factor here is a power of ten between two
# metric spellings of the same quantity - which is what lets
# `converted_to` be a lookup rather than a units library.
_UNIT_FACTORS: dict[AxisKind, dict[str, float]] = {
    AxisKind.UNCALIBRATED: {PIXEL_UNITS: 1.0},
    AxisKind.REAL_SPACE: {
        "nm": 1.0,
        "pm": 1.0e-3,
        "angstrom": 0.1,
        "um": 1.0e3,
        "mm": 1.0e6,
        "m": 1.0e9,
    },
    AxisKind.RECIPROCAL_SPACE: {
        "1/nm": 1.0,
        # 1 A^-1 spans ten times as much reciprocal space as 1 nm^-1: one
        # per angstrom is one per 0.1 nm. This factor is the whole reason
        # the py4DSTEM adapter must divide by ten - see that module.
        "1/angstrom": 10.0,
        "1/um": 1.0e-3,
    },
    AxisKind.ENERGY: {"eV": 1.0, "meV": 1.0e-3, "keV": 1.0e3},
    # mrad rather than rad is canonical for the same reason ScanParameters
    # is in nanometres rather than metres (migration plan, §5 Phase 3):
    # units here are the operator's, and a STEM convergence or collection
    # angle is tens of mrad. It is also py4DSTEM's own literal, so an
    # angular axis reaches that adapter needing no conversion at all.
    AxisKind.ANGLE: {"mrad": 1.0, "rad": 1.0e3},
}

_LONG_NAMES: dict[AxisKind, str] = {
    AxisKind.UNCALIBRATED: "pixel index",
    AxisKind.REAL_SPACE: "position",
    AxisKind.RECIPROCAL_SPACE: "scattering vector",
    AxisKind.ENERGY: "energy",
    AxisKind.ANGLE: "scattering angle",
}

_AXIS_SPEC_KEYS = frozenset({"kind", "scale", "offset", "units", "centered"})


def canonical_units(kind: AxisKind) -> str:
    """
    Return the unit this project writes for a kind unless told otherwise.

    Parameters
    ----------
    kind : AxisKind
        The kind of quantity the axis measures.

    Returns
    -------
    str
        The canonical unit string, e.g. ``"nm"`` for real space and
        ``"1/nm"`` for reciprocal space.
    """
    return next(iter(_UNIT_FACTORS[kind]))


def accepted_units(kind: AxisKind) -> tuple[str, ...]:
    """
    Return every unit string a kind accepts, canonical first.

    Parameters
    ----------
    kind : AxisKind
        The kind of quantity the axis measures.

    Returns
    -------
    tuple[str, ...]
        The accepted unit strings. Deliberately short: each is a metric
        spelling of the same quantity, so conversion between them is exact.
    """
    return tuple(_UNIT_FACTORS[kind])


def axis_kind_for_units(units: str) -> AxisKind:
    """
    Return the kind a unit string belongs to.

    This is the inverse the file relies on: NeXus records no axis kind, so
    reading a calibration back out of a written file (see
    :func:`miainwoodpecker.storage.nexus.read_calibration`) recovers it
    from ``units`` alone. The vocabulary is kept small and non-overlapping
    precisely so this inverse exists.

    Parameters
    ----------
    units : str
        A unit string, as written to an axis dataset's ``units`` attribute.

    Returns
    -------
    AxisKind
        The kind that unit belongs to.

    Raises
    ------
    ValueError
        If no kind claims that unit — rather than guessing a kind, which
        would attach physical meaning to an axis on no evidence.
    """
    for kind, factors in _UNIT_FACTORS.items():
        if units in factors:
            return kind
    known = sorted(unit for factors in _UNIT_FACTORS.values() for unit in factors)
    msg = (
        f"units {units!r} match none of this project's calibration "
        f"vocabulary ({', '.join(known)}), so the axis kind cannot be "
        f"recovered from them"
    )
    raise ValueError(msg)


@dataclasses.dataclass(frozen=True)
class AxisCalibration:
    """
    Calibration of one frame axis: a kind, a linear mapping, and a unit.

    Linear because that is what a NeXus ``AXISNAME`` field and every
    consumer here (HyperSpy's ``AxesManager``, py4DSTEM's ``Calibration``)
    models: axis value *i* is ``offset + scale * i``. Frozen so a
    calibration cannot be mutated out from under a file being written.

    Attributes
    ----------
    kind : AxisKind
        What the axis measures. Defaults to
        :attr:`AxisKind.UNCALIBRATED`, so the no-argument instance is the
        honest pixel fallback.
    scale : float
        Units per pixel — energy dispersion per channel for an
        :attr:`AxisKind.ENERGY` axis, nanometres per pixel for a real-space
        one. Must be finite and non-zero.
    offset : float
        The axis value at pixel 0, in the same units. Non-zero for a
        diffraction axis centred on the optic axis; see :meth:`centered`.
    units : str | None
        The unit the values are expressed in. ``None`` means the kind's
        canonical unit and is replaced by it during construction, so a
        constructed instance's ``units`` is always a concrete string.

    Raises
    ------
    ValueError
        If ``units`` is not one this project writes for ``kind``, if
        ``scale`` is zero or not finite, or if an
        :attr:`AxisKind.UNCALIBRATED` axis is given anything but the
        identity mapping — a "pixel" axis with a scale is a contradiction,
        and the point of that state is that it claims nothing.
    """

    kind: AxisKind = AxisKind.UNCALIBRATED
    scale: float = 1.0
    offset: float = 0.0
    units: str | None = None

    def __post_init__(self) -> None:
        """Normalize the unit and reject self-contradictory calibrations."""
        kind = AxisKind(self.kind)
        object.__setattr__(self, "kind", kind)
        units = canonical_units(kind) if self.units is None else str(self.units)
        if units not in _UNIT_FACTORS[kind]:
            msg = (
                f"units {units!r} cannot express a {kind.value!r} axis; this "
                f"project writes {', '.join(accepted_units(kind))} for it"
            )
            raise ValueError(msg)
        object.__setattr__(self, "units", units)
        object.__setattr__(self, "scale", float(self.scale))
        object.__setattr__(self, "offset", float(self.offset))
        if not math.isfinite(self.scale) or self.scale == 0.0:
            msg = f"axis scale must be finite and non-zero, got {self.scale!r}"
            raise ValueError(msg)
        if not math.isfinite(self.offset):
            msg = f"axis offset must be finite, got {self.offset!r}"
            raise ValueError(msg)
        if kind is AxisKind.UNCALIBRATED and (
            self.scale != 1.0 or self.offset != 0.0
        ):
            msg = (
                f"an uncalibrated axis is a bare pixel index, so it cannot "
                f"carry scale={self.scale!r} offset={self.offset!r}; give it a "
                f"kind if it has a physical calibration"
            )
            raise ValueError(msg)

    @property
    def is_calibrated(self) -> bool:
        """Return whether the axis claims a physical calibration at all."""
        return self.kind is not AxisKind.UNCALIBRATED

    @property
    def long_name(self) -> str:
        """Return the ``NXdata`` ``AXISNAME@long_name`` label for this kind."""
        return _LONG_NAMES[self.kind]

    @property
    def nexus_unit_category(self) -> str:
        """Return the NeXus unit category this axis's units belong to."""
        return NEXUS_UNIT_CATEGORIES[self.kind]

    def values(self, length: int) -> npt.NDArray[np.float64]:
        """
        Return the axis coordinate values for a dimension of this length.

        Parameters
        ----------
        length : int
            Number of pixels along the axis.

        Returns
        -------
        npt.NDArray[np.float64]
            ``offset + scale * arange(length)`` as float64, which for the
            uncalibrated case is exactly the bare pixel indices the writer
            emitted before this module existed.
        """
        return self.offset + self.scale * np.arange(length, dtype="float64")

    def centered(self, length: int) -> AxisCalibration:
        """
        Return the same calibration with its origin at the axis centre.

        A diffraction axis's physically meaningful zero is the optic axis,
        not the detector corner — this is how the simulated Ronchigram
        camera expresses its own axes (``offset=-scale * n / 2``, see the
        module docstring), and getting it wrong offsets every scattering
        vector by half a detector.

        Parameters
        ----------
        length : int
            Number of pixels along the axis.

        Returns
        -------
        AxisCalibration
            A copy whose ``offset`` puts zero at pixel ``length / 2``.

        Raises
        ------
        ValueError
            If the axis is uncalibrated; a pixel index has no origin to
            move, and moving it would make the axis claim something.
        """
        if not self.is_calibrated:
            msg = "an uncalibrated pixel axis cannot be centered"
            raise ValueError(msg)
        return dataclasses.replace(self, offset=-self.scale * length / 2.0)

    def converted_to(self, units: str) -> AxisCalibration:
        """
        Return the same axis expressed in another unit of the same kind.

        Exact, and deliberately limited to within-kind conversions: every
        factor involved is a power of ten between two metric spellings of
        one quantity. Crossing kinds (a scattering angle to a scattering
        vector) needs the electron wavelength, which nothing in this
        project knows, so it is refused rather than approximated.

        Parameters
        ----------
        units : str
            The target unit, which must belong to this axis's own kind.

        Returns
        -------
        AxisCalibration
            The same physical axis with ``scale``/``offset`` rescaled.

        Raises
        ------
        ValueError
            If ``units`` belongs to a different kind, or to no kind at all.
        """
        factors = _UNIT_FACTORS[self.kind]
        if units not in factors:
            msg = (
                f"cannot express a {self.kind.value!r} axis in {units!r}: this "
                f"is an exact within-kind conversion only, and {self.kind.value!r} "
                f"accepts {', '.join(accepted_units(self.kind))}"
            )
            raise ValueError(msg)
        assert self.units is not None  # noqa: S101 - normalized in __post_init__
        factor = factors[self.units] / factors[units]
        return dataclasses.replace(
            self,
            scale=self.scale * factor,
            offset=self.offset * factor,
            units=units,
        )


PIXELS = AxisCalibration()
"""
The honest fallback: a bare pixel index, claiming no physical calibration.

A module constant rather than a call because it is immutable and is the
default for every axis nobody has told us about.
"""


@dataclasses.dataclass(frozen=True)
class FrameCalibration:
    """
    Calibration of a frame's two axes, which need not be the same kind.

    Named ``y``/``x`` rather than held in a sequence so the slow/fast
    ordering the ``(height, width)`` convention pins cannot be transposed
    by accident. The default is a fully uncalibrated frame.

    Attributes
    ----------
    y : AxisCalibration
        The slow (row, height) axis.
    x : AxisCalibration
        The fast (column, width) axis.
    """

    y: AxisCalibration = PIXELS
    x: AxisCalibration = PIXELS

    @property
    def is_calibrated(self) -> bool:
        """Return whether either axis claims a physical calibration."""
        return self.y.is_calibrated or self.x.is_calibrated

    def axis(self, name: str) -> AxisCalibration:
        """
        Return one axis by its on-disk name.

        Parameters
        ----------
        name : str
            ``"y"`` or ``"x"``.

        Returns
        -------
        AxisCalibration
            That axis's calibration.

        Raises
        ------
        ValueError
            If ``name`` is not one of the frame's two axes.
        """
        if name not in AXIS_NAMES:
            msg = f"a frame has axes {AXIS_NAMES}, not {name!r}"
            raise ValueError(msg)
        return typing.cast("AxisCalibration", getattr(self, name))

    def energy_axis_name(self) -> str | None:
        """
        Return the name of the single energy-dispersive axis, if there is one.

        This is what makes the "spectra are acquired as 2D images and then
        flattened to one dimension" case expressible: a consumer can ask
        which direction is energy instead of assuming one. ``None`` covers
        both "no energy axis" and the physically meaningless "both axes are
        energy", because neither identifies a direction to flatten along.

        Returns
        -------
        str | None
            ``"y"``, ``"x"``, or ``None``.
        """
        energy = [
            name
            for name in AXIS_NAMES
            if self.axis(name).kind is AxisKind.ENERGY
        ]
        return energy[0] if len(energy) == 1 else None

    @classmethod
    def uncalibrated(cls) -> FrameCalibration:
        """
        Return a frame calibration that claims nothing, in bare pixels.

        Returns
        -------
        FrameCalibration
            Both axes uncalibrated.
        """
        return cls()

    @classmethod
    def real_space(
        cls,
        scale: float,
        *,
        units: str = "nm",
        offset: float = 0.0,
    ) -> FrameCalibration:
        """
        Return an isotropic real-space calibration, as an image mode gives.

        Parameters
        ----------
        scale : float
            Length per pixel, the same on both axes.
        units : str
            A length unit; ``"nm"`` is the operator unit this project uses
            everywhere else (``ScanParameters``, ``stage_size_nm``).
        offset : float
            The value at pixel 0 on both axes.

        Returns
        -------
        FrameCalibration
            Both axes real space.
        """
        axis = AxisCalibration(AxisKind.REAL_SPACE, scale, offset, units)
        return cls(y=axis, x=axis)

    @classmethod
    def from_field_of_view(
        cls,
        fov_nm: float,
        shape: tuple[int, int],
    ) -> FrameCalibration:
        """
        Return the real-space calibration a scan's field of view implies.

        ``fov_nm`` spans the **longer** axis and pixels are square, so the
        scale is ``fov_nm / max(height, width)`` on *both* axes — the
        convention :class:`~miainwoodpecker.devices.interface.ScanParameters`
        documents and Nion's scan layer implements
        (``get_scan_calibrations`` computes exactly this).

        This used to divide ``fov_nm`` by each dimension independently,
        which is right only for square scans and silently wrong for every
        other: a 4x10 scan at 20 nm got a 5 nm/px slow axis against a
        2 nm/px fast one, describing a frame the instrument never
        acquired. Square scans are unaffected, which is why nothing
        failed. Prefer :meth:`from_field_size` when the device reports its
        extent per axis; this remains for the scalar ``fov_nm`` path.

        Parameters
        ----------
        fov_nm : float
            Field of view in nanometres, spanning the frame's longer axis.
        shape : tuple[int, int]
            The frame's ``(height, width)``.

        Returns
        -------
        FrameCalibration
            Both axes real space in nanometres, offset zero, same scale.

        Raises
        ------
        ValueError
            If the field of view or either dimension is not positive.
        """
        height, width = shape
        if fov_nm <= 0 or height <= 0 or width <= 0:
            msg = (
                f"a field of view calibration needs a positive fov and shape, "
                f"got fov_nm={fov_nm!r} shape={shape!r}"
            )
            raise ValueError(msg)
        pixel_nm = fov_nm / max(height, width)
        return cls(
            y=AxisCalibration(AxisKind.REAL_SPACE, pixel_nm, units="nm"),
            x=AxisCalibration(AxisKind.REAL_SPACE, pixel_nm, units="nm"),
        )

    @classmethod
    def from_field_size(
        cls,
        fov_size_nm: tuple[float, float],
        shape: tuple[int, int],
    ) -> FrameCalibration:
        """
        Return the real-space calibration an explicit per-axis extent implies.

        The preferred scan path: the device states what it actually
        scanned, per axis, and storage divides rather than re-deriving a
        convention from a scalar. That removes the class of bug
        :meth:`from_field_of_view`'s docstring describes — storage cannot
        disagree with the device about which axis a single number spanned
        if the device sends both.

        Parameters
        ----------
        fov_size_nm : tuple[float, float]
            The scanned extent as ``(y_nm, x_nm)``, in nanometres, in the
            same axis order as ``shape``.
        shape : tuple[int, int]
            The frame's ``(height, width)``.

        Returns
        -------
        FrameCalibration
            Both axes real space in nanometres, offset zero.

        Raises
        ------
        ValueError
            If either extent or either dimension is not positive.
        """
        y_nm, x_nm = fov_size_nm
        height, width = shape
        if y_nm <= 0 or x_nm <= 0 or height <= 0 or width <= 0:
            msg = (
                f"a field size calibration needs a positive extent and shape, "
                f"got fov_size_nm={fov_size_nm!r} shape={shape!r}"
            )
            raise ValueError(msg)
        return cls(
            y=AxisCalibration(AxisKind.REAL_SPACE, y_nm / height, units="nm"),
            x=AxisCalibration(AxisKind.REAL_SPACE, x_nm / width, units="nm"),
        )

    @classmethod
    def diffraction(
        cls,
        scale: float,
        *,
        units: str = "1/nm",
        shape: tuple[int, int] | None = None,
    ) -> FrameCalibration:
        """
        Return an isotropic reciprocal-space or angular calibration.

        Parameters
        ----------
        scale : float
            Reciprocal length (or angle) per pixel, the same on both axes,
            as a detector's square pixels give.
        units : str
            ``"1/nm"``/``"1/angstrom"`` for a scattering vector, or
            ``"rad"``/``"mrad"`` for the scattering angle a camera
            typically reports directly. The kind follows from the unit.
        shape : tuple[int, int] | None
            The frame's ``(height, width)``. When given, both axes are
            centred on it, so zero sits at the optic axis rather than at
            the detector corner. ``None`` leaves the origin at pixel 0.

        Returns
        -------
        FrameCalibration
            Both axes reciprocal space or angle, per ``units``.

        Raises
        ------
        ValueError
            If ``units`` is neither a reciprocal-space nor an angular unit
            — a diffraction pattern's axes are one or the other, and a
            length or an energy here would be a mode mix-up worth
            refusing.
        """
        kind = axis_kind_for_units(units)
        if kind not in (AxisKind.RECIPROCAL_SPACE, AxisKind.ANGLE):
            msg = (
                f"a diffraction axis is a scattering vector or a scattering "
                f"angle, so {units!r} ({kind.value}) cannot express one"
            )
            raise ValueError(msg)
        axis = AxisCalibration(kind, scale, units=units)
        if shape is None:
            return cls(y=axis, x=axis)
        height, width = shape
        return cls(y=axis.centered(height), x=axis.centered(width))

    @classmethod
    def spectrum(
        cls,
        dispersion: float,
        *,
        units: str = "eV",
        offset: float = 0.0,
        dispersive_axis: str = "x",
        cross_axis: AxisCalibration = PIXELS,
    ) -> FrameCalibration:
        """
        Return a spectrum-image calibration: one energy axis, one other.

        The heterogeneous case the whole per-axis model exists for. The
        default ``dispersive_axis="x"`` is not a guess: the EELS camera
        this project actually acquires from calibrates its *fast* axis in
        eV and leaves the slow one uncalibrated (measured from
        ``nion.usim_device.EELSCameraSimulator``; see the module
        docstring), and on this project's real 256x1024 EELS frames that is
        the 1024-channel direction. It is still a parameter, because a
        rotated camera or a different spectrometer inverts it.

        Parameters
        ----------
        dispersion : float
            Energy per channel along the dispersive axis.
        units : str
            An energy unit — ``"eV"`` or ``"meV"`` for the resolutions this
            data has, ``"keV"`` accepted.
        offset : float
            Energy at channel 0 of the dispersive axis; the zero-loss
            peak's position is what sets this on a real spectrometer.
        dispersive_axis : str
            ``"x"`` (the fast axis, the default) or ``"y"``.
        cross_axis : AxisCalibration
            Calibration of the non-dispersive axis. Defaults to the honest
            pixel fallback, because on a spectrum image that direction's
            calibration is a separate fact nobody has supplied — pass a
            real-space axis for a spectrum image that has one.

        Returns
        -------
        FrameCalibration
            One energy axis and one ``cross_axis``.

        Raises
        ------
        ValueError
            If ``dispersive_axis`` is not one of the frame's two axes, or
            if ``units`` is not an energy unit.
        """
        if dispersive_axis not in AXIS_NAMES:
            msg = (
                f"the dispersive axis is one of a frame's two axes "
                f"{AXIS_NAMES}, not {dispersive_axis!r}"
            )
            raise ValueError(msg)
        if axis_kind_for_units(units) is not AxisKind.ENERGY:
            msg = f"a spectrum's dispersive axis needs an energy unit, not {units!r}"
            raise ValueError(msg)
        energy = AxisCalibration(AxisKind.ENERGY, dispersion, offset, units)
        if dispersive_axis == "x":
            return cls(y=cross_axis, x=energy)
        return cls(y=energy, x=cross_axis)


def _axis_from_spec(
    spec: object,
    length: int,
    name: str,
) -> AxisCalibration:
    """
    Build one axis's calibration from its metadata mapping.

    Parameters
    ----------
    spec : object
        The per-axis mapping, or an :class:`AxisCalibration` already.
    length : int
        Number of pixels along the axis, needed only for ``centered``.
    name : str
        The axis name, for error messages.

    Returns
    -------
    AxisCalibration
        The axis calibration the spec describes.

    Raises
    ------
    TypeError
        If the spec is neither a mapping nor an :class:`AxisCalibration`.
    ValueError
        If it names a key this vocabulary does not define, or omits
        ``kind``. Unknown keys are rejected rather than ignored: a typo
        that silently produced a pixel axis is exactly the failure this
        model exists to prevent.
    """
    if isinstance(spec, AxisCalibration):
        return spec
    if not isinstance(spec, typing.Mapping):
        msg = (
            f"metadata[{METADATA_KEY!r}][{name!r}] must be a mapping or an "
            f"AxisCalibration, got {type(spec).__name__}"
        )
        raise TypeError(msg)
    unknown = sorted(set(spec) - _AXIS_SPEC_KEYS)
    if unknown:
        msg = (
            f"metadata[{METADATA_KEY!r}][{name!r}] has unknown key(s) "
            f"{', '.join(repr(key) for key in unknown)}; the vocabulary is "
            f"{', '.join(sorted(_AXIS_SPEC_KEYS))}"
        )
        raise ValueError(msg)
    if "kind" not in spec:
        msg = (
            f"metadata[{METADATA_KEY!r}][{name!r}] must name a 'kind' "
            f"({', '.join(kind.value for kind in AxisKind)})"
        )
        raise ValueError(msg)
    try:
        kind = AxisKind(spec["kind"])
    except ValueError as error:
        msg = (
            f"metadata[{METADATA_KEY!r}][{name!r}]['kind'] is "
            f"{spec['kind']!r}; expected one of "
            f"{', '.join(kind.value for kind in AxisKind)}"
        )
        raise ValueError(msg) from error
    units = spec.get("units")
    axis = AxisCalibration(
        kind=kind,
        scale=float(typing.cast("float", spec.get("scale", 1.0))),
        offset=float(typing.cast("float", spec.get("offset", 0.0))),
        units=None if units is None else str(units),
    )
    if spec.get("centered", False):
        axis = axis.centered(length)
    return axis


def _from_metadata_mapping(
    spec: object,
    shape: tuple[int, int],
) -> FrameCalibration:
    """
    Build a frame calibration from the ``calibration`` metadata value.

    Parameters
    ----------
    spec : object
        The value found at ``metadata["calibration"]``.
    shape : tuple[int, int]
        The frame's ``(height, width)``.

    Returns
    -------
    FrameCalibration
        The calibration the metadata describes; axes it omits stay
        uncalibrated.

    Raises
    ------
    TypeError
        If the value is neither a mapping nor a :class:`FrameCalibration`.
    ValueError
        If it names something other than the frame's two axes.
    """
    if isinstance(spec, FrameCalibration):
        return spec
    if not isinstance(spec, typing.Mapping):
        msg = (
            f"metadata[{METADATA_KEY!r}] must be a FrameCalibration or a "
            f"mapping of axis name to calibration, got {type(spec).__name__}"
        )
        raise TypeError(msg)
    unknown = sorted(set(spec) - set(AXIS_NAMES))
    if unknown:
        msg = (
            f"metadata[{METADATA_KEY!r}] names {', '.join(repr(k) for k in unknown)}; "
            f"a frame has axes {AXIS_NAMES}"
        )
        raise ValueError(msg)
    axes = {
        name: _axis_from_spec(spec[name], length, name)
        for name, length in zip(AXIS_NAMES, shape, strict=True)
        if name in spec
    }
    return FrameCalibration(**axes)


def resolve_frame_calibration(
    shape: tuple[int, int],
    *,
    calibration: FrameCalibration | None = None,
    metadata: Mapping[str, object] | None = None,
) -> FrameCalibration:
    """
    Decide a frame's axis calibration from what the caller actually supplied.

    Three sources, in descending precedence, so an existing caller keeps
    working and a new one has somewhere to put a real calibration without
    the device or viewer layers having to grow a parameter first:

    1. an explicit :class:`FrameCalibration`, which the writer takes as a
       keyword option;
    2. ``metadata["calibration"]`` — the route ``fov_nm`` already travels,
       so a device or acquisition layer can attach a mode's calibration to
       the frames it produces;
    3. ``metadata["fov_nm"]``, the pre-existing scan path, unchanged.

    Anything else yields the uncalibrated pixel model. That fallback is
    silent because "nobody told us" is not an error; a *malformed*
    ``metadata["calibration"]`` is, and propagates a ``ValueError`` (or a
    ``TypeError`` for an outright wrong type) from the parsing helpers,
    because a typo that quietly degraded to pixels would hide the very
    calibration it was meant to supply.

    Parameters
    ----------
    shape : tuple[int, int]
        The frame's ``(height, width)``.
    calibration : FrameCalibration | None
        An explicit model, winning over anything in ``metadata``.
    metadata : Mapping[str, object] | None
        A frame's vendor/acquisition metadata.

    Returns
    -------
    FrameCalibration
        The calibration to write, never ``None`` — the uncalibrated model
        is a real answer.
    """
    if calibration is not None:
        return calibration
    if metadata is None:
        return FrameCalibration.uncalibrated()
    if METADATA_KEY in metadata:
        return _from_metadata_mapping(metadata[METADATA_KEY], shape)
    height, width = shape
    fov_size_nm = metadata.get(FIELD_SIZE_KEY)
    if (
        isinstance(fov_size_nm, (tuple, list))
        and len(fov_size_nm) == len(AXIS_NAMES)
        and all(
            isinstance(extent, (int, float)) and extent > 0
            for extent in fov_size_nm
        )
        and height
        and width
    ):
        y_nm, x_nm = fov_size_nm
        return FrameCalibration.from_field_size((float(y_nm), float(x_nm)), shape)
    fov_nm = metadata.get("fov_nm")
    if isinstance(fov_nm, (int, float)) and fov_nm > 0 and height and width:
        return FrameCalibration.from_field_of_view(float(fov_nm), shape)
    return FrameCalibration.uncalibrated()
