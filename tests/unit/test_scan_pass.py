"""
Unit tests for the cross-device pass types.

``ScanPass`` and ``DiffractionStack`` are the acquisition unit that
docs/adapters/spectrum-detectors.md §2.3 named as missing: one traversal
of the probe yielding a *set* of correlated outputs. These tests cover
the two things the types themselves promise — that a pass carries an
identity and something to identify, and that a datacube says which of
its axes are which.
"""

import numpy as np
import pytest

from miainwoodpecker.devices import (
    SCAN_SYNC_NONE,
    SCAN_SYNC_SCANNER,
    CameraParameters,
    DiffractionStack,
    Frame,
    ScanParameters,
    ScanPass,
)

_A_GRID = ScanParameters(height=4, width=6, pixel_time_us=1.0, fov_nm=10.0)
_A_DETECTOR = (8, 8)
_AN_EXPOSURE = CameraParameters(exposure_ms=10.0)
# Named rather than inline: ruff reads a "pass_id" string literal as a
# hardcoded credential (S106), and a field name that describes the
# domain should not be bent to suit that.
_AN_ID = "p1"
_NO_ID = ""
_NOT_A_SYNC = "handshake"


def _a_stack() -> DiffractionStack:
    """
    Return a datacube over ``_A_GRID`` with an ``_A_DETECTOR`` detector.

    Returns
    -------
    DiffractionStack
        A stack of zeros, shaped navigation-axes-first.
    """
    return DiffractionStack(
        data=np.zeros((*_A_GRID.shape, *_A_DETECTOR), dtype=np.float32),
        camera_id="a_camera",
        parameters=_AN_EXPOSURE,
    )


class TestDiffractionStack:
    """The datacube says which axes are navigation and which are signal."""

    def test_navigation_axes_come_first(self):
        """
        Navigation first, matching Spectrum and py4DSTEM's DataCube.

        The ordering is the whole reason this is a type rather than a
        bare array: a caller that guesses wrong silently transposes a
        dataset, and a transposed 4D-STEM cube still analyses.
        """
        stack = _a_stack()
        assert stack.navigation_shape == _A_GRID.shape
        assert stack.detector_shape == _A_DETECTOR

    def test_the_navigation_shape_matches_the_beam_positions(self):
        """A non-square grid stays the way round it was scanned."""
        assert _a_stack().navigation_shape == (4, 6)


class TestScanPass:
    """A pass carries an identity, and something for it to identify."""

    def test_a_pass_of_images_alone_is_legal(self):
        """Multi-detector scanning with no camera is still one pass."""
        frame = Frame(data=np.zeros(_A_GRID.shape), timestamp=None)
        assert ScanPass(pass_id=_AN_ID,
            parameters=_A_GRID,
            scan_sync=SCAN_SYNC_SCANNER,
            images=[frame],
        ).images

    def test_a_pass_of_diffraction_alone_is_legal(self):
        """
        4D-STEM on a column with no fitted intensity detector.

        Unusual, but real, and not something this type should refuse.
        """
        pass_ = ScanPass(
            pass_id=_AN_ID,
            parameters=_A_GRID,
            scan_sync=SCAN_SYNC_NONE,
            diffraction={"camera": _a_stack()},
        )
        assert pass_.diffraction["camera"].navigation_shape == _A_GRID.shape

    def test_a_pass_that_read_nothing_out_is_refused(self):
        """
        An empty pass is a traversal that acquired nothing.

        Refused at construction rather than stored, because everything
        downstream would then have to handle a dataset with no data.
        """
        with pytest.raises(ValueError, match="read nothing out"):
            ScanPass(pass_id=_AN_ID, parameters=_A_GRID, scan_sync=SCAN_SYNC_SCANNER)

    def test_an_unidentified_pass_is_refused(self):
        """
        The identity is the whole point of the type.

        A pass whose outputs cannot be tied together is exactly the
        "correlation hint that nothing establishes" that
        docs/adapters/spectrum-detectors.md rejected.
        """
        frame = Frame(data=np.zeros(_A_GRID.shape), timestamp=None)
        with pytest.raises(ValueError, match="identified"):
            ScanPass(
                pass_id=_NO_ID,
                parameters=_A_GRID,
                scan_sync=SCAN_SYNC_SCANNER,
                images=[frame],
            )

    def test_a_pass_that_will_not_say_how_it_synchronised_is_refused(self):
        """
        Which device was master is evidence, not decoration.

        A detector-mastered acquisition and an unsynchronised one produce
        datasets of identical shape, so this field is the only thing that
        distinguishes them afterwards - and everything computed per pixel
        from an unsynchronised map is computed against a position nothing
        guaranteed.
        """
        frame = Frame(data=np.zeros(_A_GRID.shape), timestamp=None)
        with pytest.raises(ValueError, match="scan_sync"):
            ScanPass(
                pass_id=_AN_ID,
                parameters=_A_GRID,
                scan_sync=_NOT_A_SYNC,
                images=[frame],
            )

    def test_an_unsynchronised_pass_is_legal_and_says_so(self):
        """
        Not every acquisition is synchronised, and that is not a failure.

        A slow, stable one can be good enough; what must not happen is
        it being indistinguishable from a synchronised one later.
        """
        frame = Frame(data=np.zeros(_A_GRID.shape), timestamp=None)
        pass_ = ScanPass(
            pass_id=_AN_ID,
            parameters=_A_GRID,
            scan_sync=SCAN_SYNC_NONE,
            images=[frame],
        )
        assert pass_.scan_sync == SCAN_SYNC_NONE
