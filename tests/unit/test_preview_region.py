"""
A spectrum image over a *region* of the survey scan, on the preview.

The workflow this pins: take a survey scan, draw a rectangle on it, and
acquire a spectrum image over that rectangle. For that to mean anything
the region's pass has to land on the piece of specimen the survey showed
there — which is a property of how the preview samples its specimen,
and is measured here rather than assumed. The other half is watching
the pass build: the scan channels are written through one beam position
at a time, alongside the spectrum image, so the survey image of the pass
can be drawn while the probe is still moving.
"""

from __future__ import annotations

import time

import numpy as np

from miainwoodpecker.devices.interface import (
    PROJECTED_READOUT,
    CameraParameters,
    ScanParameters,
)
from miainwoodpecker.viewer.preview import _EELS_TARGET, build_preview_devices
from miainwoodpecker.viewer.progress import PassPreview

_SURVEY = ScanParameters(height=64, width=64, pixel_time_us=1.0, fov_nm=8.0)
# A quarter of the survey, off-centre: 16x16 pixels of it, so the region
# is a real sub-region and not the survey again.
_REGION_PIXELS = 16
_REGION_OFFSET_PIXELS = (8, -12)
# The preview drifts its specimen by 0.02 nm per pass on purpose (so a
# stalled display is visible), which at a 0.3 nm lattice costs about a
# tenth of the correlation between one pass and the next. The threshold
# sits under that and well above what a one-pixel misplacement leaves.
_STRONG_CORRELATION = 0.85
_WEAK_CORRELATION = 0.5
_TWO_CAMERAS = 2
_A_SLOW_EXPOSURE_MS = 5.0
_FOUR_POSITIONS = ScanParameters(height=2, width=2, pixel_time_us=1.0, fov_nm=1.0)


def _region_of(survey: ScanParameters) -> ScanParameters:
    """
    Return the pass that covers one quarter of the survey, off-centre.

    Parameters
    ----------
    survey : ScanParameters
        The survey scan.

    Returns
    -------
    ScanParameters
        A pass with the same pixel size over a 16x16 region of it.
    """
    pixel_nm = survey.pixel_size_nm
    return ScanParameters(
        height=_REGION_PIXELS,
        width=_REGION_PIXELS,
        pixel_time_us=survey.pixel_time_us,
        fov_nm=_REGION_PIXELS * pixel_nm,
        center_nm=(
            _REGION_OFFSET_PIXELS[0] * pixel_nm,
            _REGION_OFFSET_PIXELS[1] * pixel_nm,
        ),
    )


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    """
    Return the Pearson correlation of two images.

    Parameters
    ----------
    a : np.ndarray
        One image.
    b : np.ndarray
        The other, the same shape.

    Returns
    -------
    float
        Their correlation coefficient.
    """
    return float(np.corrcoef(a.ravel(), b.ravel())[0, 1])


class TestARegionOfTheSurveyScan:
    """A pass over a region samples the specimen the survey showed there."""

    def test_the_default_centre_is_the_axis(self):
        """A scan that says nothing about its centre scans about the axis."""
        assert _SURVEY.center_nm == (0.0, 0.0)

    def test_a_region_pass_matches_the_survey_crop(self):
        """
        The region's image is the survey's image at the region.

        This is the whole point of a centre on the scan geometry: an
        operator draws a rectangle on the survey scan, and the spectrum
        image acquired over it has to be *of that rectangle*. Measured
        as a correlation against the crop, with a threshold that a pass
        landing one lattice period off would fail.
        """
        devices = build_preview_devices(scan=True, camera=True)
        survey = devices.scanner.scan_frame(_SURVEY, channel=0).data
        region = _region_of(_SURVEY)

        crop_row = _SURVEY.height // 2 + _REGION_OFFSET_PIXELS[0] - _REGION_PIXELS // 2
        crop_col = _SURVEY.width // 2 + _REGION_OFFSET_PIXELS[1] - _REGION_PIXELS // 2
        crop = survey[
            crop_row : crop_row + _REGION_PIXELS,
            crop_col : crop_col + _REGION_PIXELS,
        ]
        result = devices.scanner.scan_synchronised(
            region,
            channels=[0],
            targets=["camera"],
        )

        assert _correlation(result.images[0].data, crop) > _STRONG_CORRELATION

    def test_a_crop_one_pixel_off_is_a_different_image(self):
        """
        The check above is not vacuous: one pixel off, and it fails.

        One survey pixel is 0.42 of the lattice period here, so a crop
        displaced by one pixel on each axis is nearly anti-correlated
        with the pass - which is what makes the threshold above a
        statement about *where* the probe went and not only about the
        specimen looking like itself everywhere.
        """
        devices = build_preview_devices(scan=True, camera=True)
        survey = devices.scanner.scan_frame(_SURVEY, channel=0).data
        region = _region_of(_SURVEY)
        crop_row = _SURVEY.height // 2 + _REGION_OFFSET_PIXELS[0] - _REGION_PIXELS // 2
        crop_col = _SURVEY.width // 2 + _REGION_OFFSET_PIXELS[1] - _REGION_PIXELS // 2
        off_by_one = survey[
            crop_row + 1 : crop_row + 1 + _REGION_PIXELS,
            crop_col + 1 : crop_col + 1 + _REGION_PIXELS,
        ]

        result = devices.scanner.scan_synchronised(
            region,
            channels=[0],
            targets=["camera"],
        )

        assert _correlation(result.images[0].data, off_by_one) < _WEAK_CORRELATION

    def test_the_frames_record_where_they_were_scanned(self):
        """A region pass says so in every frame's metadata."""
        devices = build_preview_devices(scan=True, camera=True)
        region = _region_of(_SURVEY)

        result = devices.scanner.scan_synchronised(
            region,
            channels=[0],
            targets=["camera"],
        )

        assert result.images[0].metadata["center_nm"] == list(region.center_nm)
        assert result.parameters == region


class TestWatchingTheScanChannelsBuild:
    """The image channels of a pass are written through as it goes."""

    def test_a_channel_named_destination_is_filled_position_by_position(self):
        """
        A destination keyed by a channel's name is written per position.

        Through a tee that counts, so the assertion is about *how* it
        was written and not only that it ended up full: one write per
        beam position, in the pass's own order.
        """
        devices = build_preview_devices(scan=True, camera=True)
        grid = ScanParameters(height=3, width=5, pixel_time_us=1.0, fov_nm=2.0)
        window = PassPreview(np.zeros(grid.shape, dtype=np.float32))

        result = devices.scanner.scan_synchronised(
            grid,
            channels=[0, 1],
            targets=["camera"],
            into={"HAADF": window},
        )

        assert window.positions == grid.height * grid.width
        np.testing.assert_array_equal(window[...], result.images[0].data)

    def test_the_window_is_a_window_and_not_the_record(self):
        """The channel's frame in the pass is complete whatever was offered."""
        devices = build_preview_devices(scan=True, camera=True)
        grid = ScanParameters(height=3, width=5, pixel_time_us=1.0, fov_nm=2.0)

        with_window = devices.scanner.scan_synchronised(
            grid,
            channels=[0],
            targets=["camera"],
            into={"HAADF": np.zeros(grid.shape, dtype=np.float32)},
        )

        assert with_window.images[0].data.shape == grid.shape
        assert with_window.images[0].data.any()

    def test_a_scalar_write_summarises_to_itself(self):
        """
        The progress tee reduces one intensity value to that value.

        A scan channel's destination is fed one number per position,
        not a spectrum or a pattern; the map it builds is then the image
        itself, which is what makes the survey image watchable through
        the same tee the detector uses.
        """
        preview = PassPreview(np.zeros((2, 2), dtype=np.float32))

        preview[0, 1] = np.float32(7.5)

        assert preview.map[0, 1] == np.float32(7.5)
        assert preview.positions == 1
        assert preview.latest_spectrum is None


class TestPacing:
    """A paced pass takes the time a real one would; an unpaced one does not."""

    def test_an_unpaced_pass_runs_flat_out(self):
        """The default, so a test suite full of passes finishes."""
        devices = build_preview_devices(
            scan=True,
            camera=True,
            camera_count=_TWO_CAMERAS,
        )
        devices.cameras[_EELS_TARGET].configure(
            CameraParameters(exposure_ms=_A_SLOW_EXPOSURE_MS, binning=1),
        )
        positions = _FOUR_POSITIONS.height * _FOUR_POSITIONS.width
        started = time.monotonic()
        devices.scanner.scan_synchronised(_FOUR_POSITIONS, targets=[_EELS_TARGET])

        assert time.monotonic() - started < positions * (_A_SLOW_EXPOSURE_MS / 1000.0)

    def test_a_paced_pass_waits_out_the_exposure_at_every_position(self):
        """
        Each position takes at least the camera's exposure.

        That is what a column driving a detector's trigger does - it
        waits for the detector - and it is what makes the window's live
        view of a pass a view of something rather than a flash.
        """
        devices = build_preview_devices(
            scan=True,
            camera=True,
            camera_count=_TWO_CAMERAS,
            paced=True,
        )
        devices.cameras[_EELS_TARGET].configure(
            CameraParameters(
                exposure_ms=_A_SLOW_EXPOSURE_MS,
                binning=1,
                readout=PROJECTED_READOUT,
            ),
        )
        positions = _FOUR_POSITIONS.height * _FOUR_POSITIONS.width
        started = time.monotonic()
        devices.scanner.scan_synchronised(_FOUR_POSITIONS, targets=[_EELS_TARGET])

        assert time.monotonic() - started >= positions * (_A_SLOW_EXPOSURE_MS / 1000.0)
