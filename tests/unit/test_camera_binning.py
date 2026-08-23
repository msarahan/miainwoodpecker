"""
Unit tests: binning that can differ between a detector's two axes.

Binning used to be one number "in each direction", which a spectrometer
camera cannot describe. Binning the non-dispersive direction trades
dynamic range for signal-to-noise and is the routine move; binning the
dispersive one spends the energy resolution the instrument exists to
provide. They are two settings with opposite costs, and these tests pin
that the model, the validation and the preview spectrometer all say so.

No display and no device server — the preview cameras are ordinary
objects, so this runs in about a second.
"""

import pytest

from miainwoodpecker.devices.interface import (
    CameraParameters,
    axis_binning_values,
    validate_binning,
)
from miainwoodpecker.viewer.preview import build_preview_devices


@pytest.fixture
def cameras():
    """
    Return the preview Ronchigram and EEL spectrometer.

    Returns
    -------
    tuple
        The symmetric-only camera and the per-axis one.
    """
    devices = build_preview_devices(scan=False, camera=True, camera_count=2)
    return devices.cameras["ronchigram_camera"], devices.cameras["eels_camera"]


def test_a_scalar_still_means_both_directions():
    """
    The common case keeps its old spelling and its old meaning.

    Most detectors bin both directions alike, and every adapter written
    before per-axis binning existed passes an integer. That has to go on
    meaning what it always did, or this change would be a silent
    recalibration of every camera in the project.
    """
    factor = 4
    parameters = CameraParameters(exposure_ms=10.0, binning=factor)

    assert parameters.binning == factor
    assert parameters.binning_yx == (factor, factor)
    assert parameters.is_symmetric_binning is True


def test_a_pair_is_slow_axis_first():
    """A ``(y, x)`` pair matches the ``(height, width)`` frame convention."""
    parameters = CameraParameters(exposure_ms=10.0, binning=(8, 1))

    assert parameters.binning_yx == (8, 1)
    assert parameters.is_symmetric_binning is False


def test_a_pair_of_equal_factors_is_symmetric():
    """Spelling something symmetrically as a pair does not make it asymmetric."""
    assert CameraParameters(exposure_ms=1.0, binning=(2, 2)).is_symmetric_binning


@pytest.mark.parametrize(
    "binning",
    [0, -1, (0, 1), (1, 0), (-2, 2)],
)
def test_a_factor_below_one_is_refused(binning):
    """
    Zero or negative binning is rejected, in a pair as well as alone.

    Checking only the first factor would let ``(1, 0)`` through, and a
    frame binned zero times in one direction is not a frame.
    """
    with pytest.raises(ValueError, match="at least 1"):
        CameraParameters(exposure_ms=10.0, binning=binning)


def test_a_pair_must_have_exactly_two_factors():
    """Three factors do not describe a 2D detector."""
    with pytest.raises(ValueError, match="2 factors"):
        CameraParameters(exposure_ms=10.0, binning=(1, 1, 1))


def test_a_camera_that_says_nothing_offers_one_set_on_both_axes(cameras):
    """
    An ordinary detector needs no edit to keep describing itself.

    This is what makes per-axis binning additive rather than a migration:
    a webcam, a Dectris detector or a replayed recording answers exactly
    as before and is read as offering its one set on each axis.
    """
    ronchigram, _ = cameras

    assert axis_binning_values(ronchigram) == (
        tuple(ronchigram.binning_values),
        tuple(ronchigram.binning_values),
    )


def test_a_spectrometer_offers_more_binning_down_than_across(cameras):
    """
    The two axes are advertised separately, and differently.

    Rows can be binned hard for signal; channels barely at all, because
    that is spectral resolution being spent.
    """
    _, eels = cameras
    down, across = axis_binning_values(eels)

    # The EEL spectrometer on SuperSTEM 1: 1340x100, binned up to 100x
    # vertically. The top factor is the full sensor height.
    assert down == (1, 2, 4, 5, 10, 20, 25, 50, 100)
    assert across == (1, 2)
    # The scalar question means "both at once", so it is the intersection.
    assert tuple(eels.binning_values) == (1, 2)


def test_a_symmetric_only_camera_refuses_an_asymmetric_pair(cameras):
    """
    A detector that has not said it can tell its axes apart is not given a pair.

    This is the rule that let per-axis binning be added without auditing
    every adapter: an asymmetric pair is rejected before it reaches a
    camera that would apply only half of it.
    """
    ronchigram, _ = cameras
    parameters = CameraParameters(exposure_ms=10.0, binning=(2, 1))

    with pytest.raises(ValueError, match="same factor"):
        validate_binning(ronchigram, parameters)
    with pytest.raises(ValueError, match="same factor"):
        ronchigram.configure(parameters)


def test_a_factor_is_refused_against_the_axis_it_was_asked_for(cameras):
    """
    The refusal names the axis, because the axes offer different things.

    100x is perfectly ordinary down this detector — it is the full sensor
    height — and impossible across it, so "binning 100 is not supported"
    without an axis would be half an answer. And 3 is refused down it
    despite being far smaller than 100, because the rows have to divide
    evenly.
    """
    _, eels = cameras

    with pytest.raises(ValueError, match="not supported on x"):
        eels.configure(CameraParameters(exposure_ms=10.0, binning=(1, 100)))
    with pytest.raises(ValueError, match="not supported on y"):
        eels.configure(CameraParameters(exposure_ms=10.0, binning=(3, 1)))


def test_binning_rows_costs_no_spectral_resolution(cameras):
    """
    The point of the whole change, measured on the readout.

    Binning the non-dispersive direction removes rows and leaves both the
    channel count and the dispersion untouched — that is why it is the
    move a spectrometer is routinely run with.
    """
    _, eels = cameras
    eels.configure(CameraParameters(exposure_ms=10.0, binning=1))
    unbinned_rows, channels = eels.readout_shape
    dispersion = eels.frame_calibration().x.scale

    eels.configure(CameraParameters(exposure_ms=10.0, binning=(10, 1)))
    rows, still_channels = eels.readout_shape

    assert rows == unbinned_rows // 10
    assert still_channels == channels
    assert eels.frame_calibration().x.scale == pytest.approx(dispersion)


def test_binning_channels_does_cost_spectral_resolution(cameras):
    """
    And its opposite, so the asymmetry is pinned from both sides.

    Binning the dispersive direction halves the channels and doubles the
    electronvolts each one spans, which is the cost the sparse offering
    on that axis exists to make deliberate.
    """
    _, eels = cameras
    eels.configure(CameraParameters(exposure_ms=10.0, binning=1))
    rows, channels = eels.readout_shape
    dispersion = eels.frame_calibration().x.scale

    eels.configure(CameraParameters(exposure_ms=10.0, binning=(1, 2)))
    still_rows, binned_channels = eels.readout_shape

    assert still_rows == rows
    assert binned_channels == channels // 2
    assert eels.frame_calibration().x.scale == pytest.approx(2 * dispersion)
