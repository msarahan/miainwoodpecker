"""
Unit tests: turning a frame's calibration into napari layer properties.

No display and no napari import — ``viewer/axes.py`` deliberately depends
only on the calibration model, so the rule it encodes (geometry only
where the axes are commensurable) can be pinned without a GL canvas.
"""

import numpy as np
import pytest

from miainwoodpecker.storage.calibration import (
    AxisCalibration,
    AxisKind,
    FrameCalibration,
)
from miainwoodpecker.viewer import axes


def test_isotropic_real_space_is_drawn_to_scale():
    """A real-space image gets its nanometres per pixel as layer scale."""
    calibration = FrameCalibration.real_space(0.25)
    built = axes.layer_axes(calibration)

    assert built["scale"] == (0.25, 0.25)
    assert built["units"] == ("nm", "nm")
    assert built["axis_labels"] == ("position", "position")
    assert built["metadata"][axes.CALIBRATION] is calibration


def test_anisotropic_sampling_is_drawn_anisotropically():
    """
    A frame sampled more finely across than down is drawn wider than tall.

    This is the case that makes ``scale`` worth setting at all: the
    picture's true shape is its physical extent, not its pixel count.
    Drawing it on square pixels would be the distortion, not the fix.
    """
    calibration = FrameCalibration.from_field_size((10.0, 40.0), (100, 100))
    built = axes.layer_axes(calibration)

    y_scale, x_scale = built["scale"]
    assert x_scale == pytest.approx(4 * y_scale)


def test_axes_in_different_units_of_one_kind_are_converted():
    """
    Nanometres and angstroms are commensurable, so one is converted.

    Refusing here would treat a unit choice as a physical incompatibility
    and drop a calibration this project can express perfectly well.
    """
    calibration = FrameCalibration(
        y=AxisCalibration(kind=AxisKind.REAL_SPACE, scale=1.0, units="nm"),
        x=AxisCalibration(kind=AxisKind.REAL_SPACE, scale=5.0, units="angstrom"),
    )
    assert axes.commensurable(calibration) is True

    built = axes.layer_axes(calibration)
    assert built["units"] == ("nm", "nm")
    # 5 angstrom per pixel is 0.5 nm per pixel.
    assert built["scale"] == pytest.approx((1.0, 0.5))
    assert axes.scale_bar_unit(calibration) == "nm"


def test_an_asymmetrically_binned_detector_is_drawn_to_its_physical_shape():
    """
    A spectrometer camera binned hard across the dispersion still measures.

    Binning 1x along the dispersive direction to keep energy resolution
    while binning 4x across it for signal is ordinary practice, and it
    makes a stored pixel four times taller than it is wide. Both axes
    still measure the same quantity, so the two scales are comparable and
    the frame must be drawn to its physical shape: this 64x256 readout is
    square on screen, and its 4:1 pixel count is a fact about the readout
    rather than about what was measured.
    """
    calibration = FrameCalibration(
        y=AxisCalibration(kind=AxisKind.ANGLE, scale=1.6, units="mrad"),
        x=AxisCalibration(kind=AxisKind.ANGLE, scale=0.4, units="mrad"),
    )
    assert axes.commensurable(calibration) is True

    built = axes.layer_axes(calibration)
    assert built["scale"] == pytest.approx((1.6, 0.4))
    # 64 rows of 1.6 mrad against 256 columns of 0.4 mrad: physically square.
    height, width = 64, 256
    assert height * built["scale"][0] == pytest.approx(width * built["scale"][1])
    assert axes.scale_bar_unit(calibration) == "mrad"


def test_an_eels_readout_keeps_pixel_geometry():
    """
    Energy against position is not drawn to scale, and says so.

    There is no rate of exchange between an electronvolt and a nanometre,
    so a geometric ratio between them would assert something no
    instrument measured. The calibration is still recorded in full — the
    units, the labels, and the model itself — because the readout and the
    ROI work need it even though the picture must not be reshaped by it.
    """
    calibration = FrameCalibration.spectrum(0.5, dispersive_axis="x")
    assert axes.commensurable(calibration) is False

    built = axes.layer_axes(calibration)
    assert built["scale"] == (1.0, 1.0)
    assert built["translate"] == (0.0, 0.0)
    assert built["units"] == ("pixel", "eV")
    assert built["axis_labels"] == ("pixel index", "energy")
    assert built["metadata"][axes.CALIBRATION] is calibration
    assert axes.scale_bar_unit(calibration) is None


def test_an_uncalibrated_frame_claims_nothing():
    """Bare pixels get identity geometry and no scale bar."""
    calibration = FrameCalibration.uncalibrated()
    built = axes.layer_axes(calibration)

    assert built["scale"] == (1.0, 1.0)
    assert built["units"] == ("pixel", "pixel")
    assert axes.scale_bar_unit(calibration) is None


def test_a_diffraction_frame_is_centred_on_the_optic_axis():
    """
    A reciprocal-space frame carries its offset as well as its scale.

    The origin of a diffraction pattern is the undiffracted beam, not the
    corner pixel, so the offset is what puts zero scattering vector where
    it physically belongs.
    """
    calibration = FrameCalibration.diffraction(0.1, shape=(64, 64))
    built = axes.layer_axes(calibration)

    assert built["scale"] == pytest.approx((0.1, 0.1))
    assert built["translate"][0] < 0
    assert built["translate"][1] < 0
    assert axes.scale_bar_unit(calibration) == "1/nm"


def test_a_frame_stack_gets_identity_axes_for_its_slider():
    """
    A recording's leading axis is navigated, not drawn, so it is left alone.

    napari gives a 3D array a frame slider; giving that axis a physical
    scale would put the frame index into the picture's geometry.
    """
    calibration = FrameCalibration.real_space(0.25)
    built = axes.layer_axes(calibration, ndim=3)

    assert built["scale"] == (1.0, 0.25, 0.25)
    assert built["translate"] == (0.0, 0.0, 0.0)
    assert built["units"] == ("pixel", "nm", "nm")
    assert built["axis_labels"] == ("frame", "position", "position")


def test_calibration_is_resolved_from_frame_metadata():
    """
    A scan frame reporting its field of view is calibrated from it.

    This is the route a live frame actually travels — the scanner puts
    the extent in metadata and nothing in the viewer restates the
    ``(height, width)`` convention.
    """
    data = np.zeros((256, 256))
    calibration = axes.frame_calibration(data, {"fov_size_nm": (15.0, 15.0)})

    assert calibration.y.kind is AxisKind.REAL_SPACE
    assert calibration.y.scale == pytest.approx(15.0 / 256)
    assert axes.scale_bar_unit(calibration) == "nm"


def test_a_frame_reporting_nothing_is_uncalibrated_not_an_error():
    """A device that reports no calibration displays in honest pixels."""
    assert axes.frame_calibration(np.zeros((8, 8)), None).is_calibrated is False
    assert axes.frame_calibration(np.zeros((8, 8)), {}).is_calibrated is False


class TestTheAxisOfASpectrum:
    """
    The rank-1 case, which the two-axis model cannot answer at all.

    A projecting detector delivers one axis of counts, and asking
    :func:`axes.frame_calibration` about it raises rather than answering
    — which is exactly what happened to the first spectrum that reached
    the display. These pin what the 1D path answers instead.
    """

    def test_the_dispersion_comes_off_the_frames_own_metadata(self):
        """
        A projected readout keeps the fast axis, so its axis is ``x``.

        Not a convention invented for the display: it is where a
        detector summing its non-dispersive direction already writes the
        dispersion, and the values here are the preview spectrometer's
        own.
        """
        data = np.zeros(1340, dtype=np.float32)
        axis = axes.spectrum_axis(
            data,
            {
                "calibration": {
                    "y": {"kind": "uncalibrated"},
                    "x": {"kind": "energy", "scale": 0.5, "offset": -20.0,
                          "units": "eV"},
                },
            },
        )

        assert axis.kind is AxisKind.ENERGY
        assert axis.units == "eV"
        assert axis.scale == pytest.approx(0.5)
        assert axis.values(1340)[0] == pytest.approx(-20.0)

    def test_an_uncalibrated_readout_is_channels_rather_than_an_error(self):
        """A detector that reports no dispersion still plots, in channels."""
        axis = axes.spectrum_axis(np.zeros(512), None)

        assert axis.is_calibrated is False
        assert np.array_equal(axis.values(4), [0.0, 1.0, 2.0, 3.0])

    def test_a_scan_calibration_does_not_become_an_energy_axis(self):
        """
        One axis of counts against nanometres is not a spectrum.

        A pass's metadata carries the *scan* geometry, and resolving a
        rank-1 readout through it would hand the plot a real-space ruler
        to label energies with. Channels are the honest answer, and they
        are still a usable axis.
        """
        axis = axes.spectrum_axis(np.zeros(64), {"fov_size_nm": (15.0, 15.0)})

        assert axis.is_calibrated is False

    def test_one_position_of_a_spectrum_image_asks_the_same_question(self):
        """
        Energy is the last axis whatever the rank, so this reads it there.

        That invariant is NeXus's, RosettaSciIO's and HyperSpy's before
        it is this project's, which is why the 1D helper can be asked
        about a cube's slice without being told which axis to look at.
        """
        cube = np.zeros((4, 4, 200), dtype=np.float32)
        axis = axes.spectrum_axis(
            cube,
            {"calibration": {"x": {"kind": "energy", "scale": 2.0,
                                   "units": "eV"}}},
        )

        assert axis.kind is AxisKind.ENERGY
        assert axis.values(200)[-1] == pytest.approx(398.0)
