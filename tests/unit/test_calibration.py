"""
Tests for the per-axis frame calibration model.

The conversion tests carry most of the weight here. A unit-conversion error
is the class of bug that silently produces wrong physics rather than a
failure - the migration plan's hardware checklist flags a metres/nanometres
mix-up as its highest-consequence item for the same reason - so the exact
factors are asserted numerically, not merely round-tripped.
"""

import numpy as np
import pytest

from miainwoodpecker.storage.calibration import (
    PIXELS,
    PIXEL_UNITS,
    AxisCalibration,
    AxisKind,
    FrameCalibration,
    accepted_units,
    axis_kind_for_units,
    canonical_units,
    resolve_frame_calibration,
)


def test_the_default_axis_is_the_honest_pixel_fallback():
    """A no-argument axis claims nothing, in bare pixel indices."""
    assert PIXELS.kind is AxisKind.UNCALIBRATED
    assert PIXELS.units == PIXEL_UNITS
    assert PIXELS.is_calibrated is False
    assert np.allclose(PIXELS.values(3), [0.0, 1.0, 2.0])


def test_units_default_to_the_kinds_canonical_unit():
    """Omitting units gets the unit this project writes for that kind."""
    assert AxisCalibration(AxisKind.REAL_SPACE, 0.5).units == "nm"
    assert AxisCalibration(AxisKind.RECIPROCAL_SPACE, 0.5).units == "1/nm"
    assert AxisCalibration(AxisKind.ENERGY, 0.5).units == "eV"
    assert AxisCalibration(AxisKind.ANGLE, 0.5).units == "mrad"


def test_a_unit_from_the_wrong_kind_is_refused():
    """Declaring reciprocal space in nanometres is a contradiction, not a value."""
    with pytest.raises(ValueError, match="cannot express"):
        AxisCalibration(AxisKind.RECIPROCAL_SPACE, 0.5, units="nm")
    with pytest.raises(ValueError, match="cannot express"):
        AxisCalibration(AxisKind.ENERGY, 0.5, units="1/nm")


def test_an_uncalibrated_axis_cannot_carry_a_scale():
    """
    The pixel state means "we do not know", so it cannot also assert a scale.

    Without this, a caller could produce an axis labelled 'pixel' whose
    values are physical - the exact confidently-wrong axis this model
    exists to prevent.
    """
    with pytest.raises(ValueError, match="bare pixel index"):
        AxisCalibration(AxisKind.UNCALIBRATED, 0.5)
    with pytest.raises(ValueError, match="bare pixel index"):
        AxisCalibration(AxisKind.UNCALIBRATED, 1.0, 3.0)


def test_a_zero_or_non_finite_scale_is_refused():
    """A degenerate scale would collapse or poison every axis value."""
    with pytest.raises(ValueError, match="finite and non-zero"):
        AxisCalibration(AxisKind.ENERGY, 0.0)
    with pytest.raises(ValueError, match="finite and non-zero"):
        AxisCalibration(AxisKind.ENERGY, float("nan"))
    with pytest.raises(ValueError, match="offset must be finite"):
        AxisCalibration(AxisKind.ENERGY, 1.0, float("inf"))


def test_axis_values_are_offset_plus_scale_times_index():
    """The axis dataset a file gets is the linear mapping, nothing cleverer."""
    axis = AxisCalibration(AxisKind.ENERGY, 0.5, -2.0)
    assert np.allclose(axis.values(4), [-2.0, -1.5, -1.0, -0.5])


def test_centering_puts_zero_at_the_axis_middle():
    """
    A diffraction axis's origin is the optic axis, not the detector corner.

    This mirrors how the simulated Ronchigram camera expresses its own
    axes (offset = -scale * n / 2).
    """
    axis = AxisCalibration(AxisKind.RECIPROCAL_SPACE, 0.1).centered(8)
    assert axis.offset == pytest.approx(-0.4)
    assert axis.values(8)[4] == pytest.approx(0.0)


def test_an_uncalibrated_axis_cannot_be_centered():
    """A pixel index has no physical origin to move."""
    with pytest.raises(ValueError, match="cannot be centered"):
        PIXELS.centered(8)


def test_reciprocal_nm_converts_to_inverse_angstrom_by_dividing_by_ten():
    """
    1 A^-1 spans ten times as much reciprocal space as 1 nm^-1.

    This is the conversion py4DSTEM forces (its Q_pixel_units accepts only
    'pixels', 'A^-1', 'mrad'), and getting it backwards would make every
    downstream scattering-vector number wrong by 100x rather than 10x, with
    nothing saying so.
    """
    axis = AxisCalibration(AxisKind.RECIPROCAL_SPACE, 2.0, -8.0, "1/nm")
    converted = axis.converted_to("1/angstrom")
    assert converted.units == "1/angstrom"
    assert converted.kind is AxisKind.RECIPROCAL_SPACE
    assert converted.scale == pytest.approx(0.2)
    assert converted.offset == pytest.approx(-0.8)
    # And back again, exactly.
    assert converted.converted_to("1/nm").scale == pytest.approx(2.0)


def test_conversion_preserves_the_physical_axis_values():
    """
    The strongest statement of correctness: the same axis, two spellings.

    A pixel's coordinate in 1/angstrom must be exactly a tenth of its
    coordinate in 1/nm, at every index - which a wrong factor, or a factor
    applied to scale but not offset, would break.
    """
    axis = AxisCalibration(AxisKind.RECIPROCAL_SPACE, 0.05, -1.6, "1/nm")
    converted = axis.converted_to("1/angstrom")
    assert np.allclose(converted.values(64), axis.values(64) / 10.0)


def test_length_and_energy_conversions_use_the_right_factors():
    """nm/angstrom is a factor of ten the other way; eV/meV is a thousand."""
    real = AxisCalibration(AxisKind.REAL_SPACE, 0.5, units="nm")
    assert real.converted_to("angstrom").scale == pytest.approx(5.0)
    assert real.converted_to("m").scale == pytest.approx(0.5e-9)

    energy = AxisCalibration(AxisKind.ENERGY, 0.5, units="eV")
    assert energy.converted_to("meV").scale == pytest.approx(500.0)

    angle = AxisCalibration(AxisKind.ANGLE, 2.0, units="mrad")
    assert angle.converted_to("rad").scale == pytest.approx(0.002)


def test_conversion_across_kinds_is_refused_not_approximated():
    """
    A scattering angle becomes a scattering vector only via the wavelength.

    Nothing in this project knows the accelerating voltage, so this stays a
    refusal rather than a guess - which is also why AxisKind keeps ANGLE
    separate from RECIPROCAL_SPACE.
    """
    angle = AxisCalibration(AxisKind.ANGLE, 2.0, units="mrad")
    with pytest.raises(ValueError, match="within-kind conversion only"):
        angle.converted_to("1/nm")


def test_units_map_back_to_a_kind_because_nexus_records_no_kind():
    """The unit string is the only carrier of the axis kind in the file."""
    assert axis_kind_for_units("nm") is AxisKind.REAL_SPACE
    assert axis_kind_for_units("1/nm") is AxisKind.RECIPROCAL_SPACE
    assert axis_kind_for_units("eV") is AxisKind.ENERGY
    assert axis_kind_for_units("mrad") is AxisKind.ANGLE
    assert axis_kind_for_units(PIXEL_UNITS) is AxisKind.UNCALIBRATED


def test_an_unknown_unit_is_refused_rather_than_assigned_a_kind():
    """
    'nm-1' in particular: a plausible spelling this project does not write.

    Measured against pynxtools' own unit matcher, 'nm-1' matches no NeXus
    unit category while '1/nm' matches NX_WAVENUMBER - so accepting it here
    would let a file claim a category it fails.
    """
    with pytest.raises(ValueError, match="match none of this project"):
        axis_kind_for_units("nm-1")


def test_the_unit_vocabulary_is_small_and_canonical_first():
    """canonical_units is the first accepted unit, which the writer uses."""
    for kind in AxisKind:
        assert accepted_units(kind)[0] == canonical_units(kind)
        assert canonical_units(kind) in accepted_units(kind)


def test_a_spectrum_frame_has_two_different_kinds_of_axis():
    """
    The heterogeneous case the per-axis model exists for.

    An EELS frame is one energy-dispersive direction and one that is not
    energy at all, so a single unit per frame could not describe it.
    """
    calibration = FrameCalibration.spectrum(0.5, offset=-20.0)
    assert calibration.x.kind is AxisKind.ENERGY
    assert calibration.x.units == "eV"
    assert calibration.x.scale == pytest.approx(0.5)
    assert calibration.x.offset == pytest.approx(-20.0)
    assert calibration.y.kind is AxisKind.UNCALIBRATED
    assert calibration.energy_axis_name() == "x"


def test_the_dispersive_axis_is_configurable_not_assumed():
    """A rotated camera or a different spectrometer inverts the default."""
    calibration = FrameCalibration.spectrum(0.5, dispersive_axis="y")
    assert calibration.y.kind is AxisKind.ENERGY
    assert calibration.x.kind is AxisKind.UNCALIBRATED
    assert calibration.energy_axis_name() == "y"


def test_a_spectrum_image_can_calibrate_its_non_dispersive_axis_too():
    """The cross axis is a separate fact, suppliable when it is known."""
    calibration = FrameCalibration.spectrum(
        0.5,
        cross_axis=AxisCalibration(AxisKind.REAL_SPACE, 1.5),
    )
    assert calibration.y.kind is AxisKind.REAL_SPACE
    assert calibration.y.units == "nm"
    assert calibration.energy_axis_name() == "x"


def test_a_spectrum_needs_an_energy_unit_and_a_real_axis_name():
    """Refusing here is cheaper than writing a mislabelled dispersive axis."""
    with pytest.raises(ValueError, match="dispersive axis is one of"):
        FrameCalibration.spectrum(0.5, dispersive_axis="z")
    with pytest.raises(ValueError, match="needs an energy unit"):
        FrameCalibration.spectrum(0.5, units="1/nm")


def test_no_unique_energy_axis_reports_none_rather_than_picking_one():
    """Zero energy axes and two energy axes both fail to name a direction."""
    assert FrameCalibration.uncalibrated().energy_axis_name() is None
    energy = AxisCalibration(AxisKind.ENERGY, 0.5)
    assert FrameCalibration(y=energy, x=energy).energy_axis_name() is None


def test_diffraction_is_isotropic_and_can_be_centered_on_the_optic_axis():
    """A detector's square pixels give one scale; its origin is the centre."""
    plain = FrameCalibration.diffraction(0.05)
    assert plain.y.kind is AxisKind.RECIPROCAL_SPACE
    assert plain.y.offset == pytest.approx(0.0)

    centered = FrameCalibration.diffraction(0.05, shape=(8, 16))
    assert centered.y.offset == pytest.approx(-0.2)
    assert centered.x.offset == pytest.approx(-0.4)


def test_diffraction_accepts_an_angle_because_that_is_what_cameras_report():
    """
    The simulated Ronchigram camera calibrates in radians, not nm^-1.

    Folding angles into RECIPROCAL_SPACE would need the electron
    wavelength, so an angular diffraction axis is a first-class kind.
    """
    calibration = FrameCalibration.diffraction(0.5, units="mrad")
    assert calibration.x.kind is AxisKind.ANGLE
    assert calibration.x.units == "mrad"


def test_a_length_or_energy_cannot_be_a_diffraction_axis():
    """A mode mix-up here would produce a confidently wrong q axis."""
    with pytest.raises(ValueError, match="scattering vector or a scattering"):
        FrameCalibration.diffraction(0.5, units="nm")
    with pytest.raises(ValueError, match="scattering vector or a scattering"):
        FrameCalibration.diffraction(0.5, units="eV")


def test_field_of_view_gives_square_pixels_on_a_non_square_frame():
    """
    fov_nm spans the longer axis and pixels are square, per ScanParameters.

    This test previously asserted the opposite - fov_nm divided by each
    dimension independently, giving 5.0 nm/px on the slow axis against
    2.0 on the fast one. That describes a frame with rectangular pixels
    that the instrument never acquired: Nion's own
    ``get_scan_calibrations`` computes ``fov_nm / max(scan_shape)`` and
    applies it to both axes. Square scans agree either way, which is why
    nothing failed for three phases.
    """
    calibration = FrameCalibration.from_field_of_view(20.0, (4, 10))
    assert calibration.y.units == "nm"
    pixel_nm = 20.0 / 10
    assert calibration.y.scale == pytest.approx(pixel_nm)
    assert calibration.x.scale == pytest.approx(pixel_nm)
    # The short axis therefore covers 8 nm of the 20 nm field, not all of it.
    assert np.allclose(calibration.y.values(4), [0.0, 2.0, 4.0, 6.0])


def test_field_size_calibrates_each_axis_from_the_reported_extent():
    """A device reporting its extent per axis is divided, not re-derived."""
    calibration = FrameCalibration.from_field_size((8.0, 20.0), (4, 10))
    assert calibration.y.scale == pytest.approx(2.0)
    assert calibration.x.scale == pytest.approx(2.0)
    assert calibration.y.units == "nm"
    assert calibration.x.units == "nm"


def test_field_size_is_preferred_over_the_scalar_field_of_view():
    """
    A scanner sends both; the per-axis one wins.

    Both agree here, which is the point: the per-axis key is preferred so
    storage never has to re-derive which axis a scalar spanned.
    """
    resolved = resolve_frame_calibration(
        (4, 10),
        metadata={"fov_nm": 20.0, "fov_size_nm": (8.0, 20.0)},
    )
    assert resolved.y.scale == pytest.approx(2.0)
    assert resolved.x.scale == pytest.approx(2.0)


def test_a_malformed_field_size_falls_back_to_the_scalar():
    """A wrong-shaped extent is not a calibration; fov_nm still resolves."""
    resolved = resolve_frame_calibration(
        (4, 10),
        metadata={"fov_nm": 20.0, "fov_size_nm": (8.0,)},
    )
    assert resolved.y.scale == pytest.approx(2.0)


def test_a_non_positive_field_size_is_not_a_calibration():
    """Zero or negative extents cannot describe a frame."""
    with pytest.raises(ValueError, match="positive extent and shape"):
        FrameCalibration.from_field_size((0.0, 10.0), (4, 10))


def test_a_non_positive_field_of_view_is_not_a_calibration():
    """Zero or negative fov cannot describe a frame's extent."""
    with pytest.raises(ValueError, match="positive fov and shape"):
        FrameCalibration.from_field_of_view(0.0, (4, 4))


def test_asking_for_an_axis_a_frame_does_not_have_is_an_error():
    """A frame has exactly two axes, named for the (height, width) order."""
    with pytest.raises(ValueError, match="has axes"):
        FrameCalibration.uncalibrated().axis("z")


def test_resolve_prefers_an_explicit_model_over_metadata():
    """An explicit calibration wins, so a caller can override a device's."""
    explicit = FrameCalibration.diffraction(0.05)
    resolved = resolve_frame_calibration(
        (4, 4),
        calibration=explicit,
        metadata={"fov_nm": 20.0},
    )
    assert resolved is explicit


def test_resolve_reads_a_calibration_out_of_frame_metadata():
    """
    The route fov_nm already travels, generalized to the other kinds.

    This is what lets a device or acquisition layer attach a mode's
    calibration to the frames it produces without the writer growing a
    required parameter.
    """
    resolved = resolve_frame_calibration(
        (8, 16),
        metadata={
            "calibration": {
                "y": {"kind": "reciprocal_space", "scale": 0.05, "centered": True},
                "x": {"kind": "reciprocal_space", "scale": 0.05, "centered": True},
            },
        },
    )
    assert resolved.y.kind is AxisKind.RECIPROCAL_SPACE
    assert resolved.y.offset == pytest.approx(-0.2)
    assert resolved.x.offset == pytest.approx(-0.4)


def test_metadata_may_carry_the_model_objects_directly():
    """A device layer that imports the model can skip the mapping form."""
    energy = AxisCalibration(AxisKind.ENERGY, 0.5)
    resolved = resolve_frame_calibration(
        (4, 4),
        metadata={"calibration": {"x": energy}},
    )
    assert resolved.x is energy
    assert resolved.y.kind is AxisKind.UNCALIBRATED

    whole = FrameCalibration.diffraction(0.05)
    assert resolve_frame_calibration((4, 4), metadata={"calibration": whole}) is whole


def test_an_axis_metadata_typo_fails_loudly_rather_than_degrading_to_pixels():
    """
    Unknown keys are rejected, because the silent failure is the bad one.

    A misspelt 'scale' that quietly produced a 'pixel' axis would hide
    exactly the calibration it was meant to supply.
    """
    with pytest.raises(ValueError, match="unknown key"):
        resolve_frame_calibration(
            (4, 4),
            metadata={"calibration": {"x": {"kind": "energy", "scale_eV": 0.5}}},
        )
    with pytest.raises(ValueError, match="must name a 'kind'"):
        resolve_frame_calibration(
            (4, 4),
            metadata={"calibration": {"x": {"scale": 1.0}}},
        )
    with pytest.raises(ValueError, match="expected one of"):
        resolve_frame_calibration(
            (4, 4),
            metadata={"calibration": {"x": {"kind": "diffraction", "scale": 1.0}}},
        )
    with pytest.raises(ValueError, match="a frame has axes"):
        resolve_frame_calibration(
            (4, 4),
            metadata={"calibration": {"z": {"kind": "energy", "scale": 1.0}}},
        )


def test_a_wrong_type_in_the_calibration_metadata_is_a_type_error():
    """Not a mapping and not a model object is a programming mistake."""
    with pytest.raises(TypeError, match="must be a FrameCalibration"):
        resolve_frame_calibration((4, 4), metadata={"calibration": "nm"})
    with pytest.raises(TypeError, match="must be a mapping"):
        resolve_frame_calibration((4, 4), metadata={"calibration": {"x": 0.5}})


def test_resolve_falls_back_silently_when_nobody_supplied_anything():
    """
    "Nobody told us" is not an error - it is the honest pixel answer.

    Deliberately different from the malformed case above: a missing
    calibration is a fact about the acquisition, a broken one is a bug.
    """
    for metadata in (None, {}, {"index": 3}, {"fov_nm": 0.0}, {"fov_nm": "wide"}):
        resolved = resolve_frame_calibration((4, 4), metadata=metadata)
        assert resolved.is_calibrated is False
        assert resolved.x.units == PIXEL_UNITS
