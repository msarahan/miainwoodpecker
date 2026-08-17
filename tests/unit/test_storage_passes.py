"""
Unit tests for storing a scan pass.

The property that matters most here is not what the file contains but
*how the data got into it*: the cube is written where it will live,
rather than acquired into memory and copied. A test comparing only
values would pass either way, so identity is asserted directly.
"""

import pathlib

import h5py
import numpy as np
import pytest

from miainwoodpecker.devices import (
    SCAN_SYNC_SCANNER,
    CameraParameters,
    ScanParameters,
)
from miainwoodpecker.storage.calibration import AxisKind, FrameCalibration
from miainwoodpecker.storage.passes import PassWriter, read_pass
from miainwoodpecker.viewer.preview import _CAMERA_PIXELS, build_preview_devices

_A_GRID = ScanParameters(height=6, width=4, pixel_time_us=1.0, fov_nm=12.0)
# Binned 4x so the cube stays small: 6*4 positions of 32x32 floats is
# under a megabyte, where unbinned would be sixteen times that in every
# test in this file.
_A_BINNING = 4
_A_DETECTOR = (_CAMERA_PIXELS // _A_BINNING, _CAMERA_PIXELS // _A_BINNING)
_FOUR_AXES = 4


def _text(value: object) -> str:
    """
    Return an HDF5 string as ``str``, whatever h5py handed back.

    h5py returns ``str`` for attributes on some versions and ``bytes`` on
    others, and which one is not a property of this project's files.

    Parameters
    ----------
    value : object
        The stored value.

    Returns
    -------
    str
        The decoded text.
    """
    return value.decode() if isinstance(value, bytes) else str(value)
_TWO_CHANNELS = 2


def _acquire(path: pathlib.Path, **kwargs: object) -> object:
    """
    Acquire a pass straight into a file, the way the writer intends.

    Parameters
    ----------
    path : pathlib.Path
        Where to write.
    **kwargs : object
        Passed to :func:`build_preview_devices`.

    Returns
    -------
    ScanPass
        The completed pass.
    """
    devices = build_preview_devices(scan=True, camera=True, **kwargs)
    devices.cameras["camera"].configure(
        CameraParameters(exposure_ms=10.0, binning=_A_BINNING),
    )
    with PassWriter(path, _A_GRID, cubes={"camera": _A_DETECTOR}) as writer:
        result = devices.scanner.scan_synchronised(
            _A_GRID, channels=[0, 1], targets=["camera"],
            into=writer.destinations(),
        )
        writer.finish(result)
    return result


class TestStreaming:
    """The cube is written where it lives, not copied there."""

    def test_the_device_fills_the_files_own_dataset(self, tmp_path):
        """
        The destination handed to the device *is* the file's dataset.

        This is the whole design: a 64x64 grid on a 512x512 detector is
        four gigabytes, so acquiring into memory and copying is not a
        slower correct option, it is an impossible one.
        """
        path = tmp_path / "pass.nxs"
        with PassWriter(path, _A_GRID, cubes={"camera": _A_DETECTOR}) as writer:
            destinations = writer.destinations()
            assert isinstance(destinations["camera"], h5py.Dataset)
            assert destinations["camera"].shape == (*_A_GRID.shape, *_A_DETECTOR)

    def test_the_cube_is_chunked_one_beam_position_at_a_time(self, tmp_path):
        """
        Chunking matches how the device writes, one detector image at a time.

        A chunk spanning several positions would turn every per-position
        write into a read-modify-write of its neighbours.
        """
        path = tmp_path / "pass.nxs"
        with PassWriter(path, _A_GRID, cubes={"camera": _A_DETECTOR}) as writer:
            assert writer.destinations()["camera"].chunks == (1, 1, *_A_DETECTOR)

    def test_an_acquired_pass_lands_on_disk(self, tmp_path):
        """End to end: acquire through the writer, read the file back."""
        path = tmp_path / "pass.nxs"
        result = _acquire(path)

        recording = read_pass(path)
        assert recording.pass_id == result.pass_id
        assert recording.scan_sync == SCAN_SYNC_SCANNER
        assert recording.signals["data_camera"] == (*_A_GRID.shape, *_A_DETECTOR)

    def test_the_written_cube_is_not_all_zeros(self, tmp_path):
        """
        The acquisition really wrote through, rather than allocating and stopping.

        Without this the streaming tests above would pass against a
        writer that created the dataset and never filled it.
        """
        path = tmp_path / "pass.nxs"
        _acquire(path)
        with h5py.File(path, "r") as handle:
            assert np.asarray(handle["entry/data_camera/data"][0, 0]).any()


class TestLayout:
    """One entry, one NXdata per signal, and a default that points at one."""

    def test_every_signal_gets_its_own_nxdata(self, tmp_path):
        """
        Two channels and a cube are three signals, so three groups.

        The frame writer never had to answer "where does a second signal
        go"; a pass does, and NeXus's own answer is several NXdata under
        one entry.
        """
        path = tmp_path / "pass.nxs"
        _acquire(path)

        signals = read_pass(path).signals
        assert set(signals) == {"data_camera", "data_HAADF", "data_MAADF"}

    def test_the_default_points_at_the_diffraction_cube(self, tmp_path):
        """
        A reader opening the file plots the reason the pass was taken.

        Linked rather than copied — writing the cube's bytes twice is
        exactly what this module exists to avoid.
        """
        path = tmp_path / "pass.nxs"
        _acquire(path)

        with h5py.File(path, "r") as handle:
            assert handle["entry"].attrs["default"] == "data"
            assert handle["entry/data/data"].shape == (*_A_GRID.shape, *_A_DETECTOR)

    def test_the_cube_declares_four_named_axes(self, tmp_path):
        """
        Navigation first, then detector, so nothing has to guess.

        A 4D dataset whose axes are unlabelled is one a reader can
        transpose without noticing.
        """
        path = tmp_path / "pass.nxs"
        _acquire(path)

        with h5py.File(path, "r") as handle:
            group = handle["entry/data_camera"]
            axes = [_text(name) for name in group.attrs["axes"]]
            assert axes == ["scan_y", "scan_x", "det_y", "det_x"]
            assert len(axes) == _FOUR_AXES
            assert group.attrs["scan_y_indices"] == 0
            assert group.attrs["det_x_indices"] == _FOUR_AXES - 1

    def test_the_navigation_axes_are_calibrated_from_the_scan(self, tmp_path):
        """
        Beam-position axes are nanometres, through the scan's own path.

        Same calibration a scanned frame gets, rather than a second
        derivation that could disagree with it.
        """
        path = tmp_path / "pass.nxs"
        _acquire(path)

        expected = FrameCalibration.from_field_size(
            _A_GRID.fov_size_nm, _A_GRID.shape,
        )
        with h5py.File(path, "r") as handle:
            scan_y = handle["entry/data_camera/scan_y"]
            assert _text(scan_y.attrs["units"]) == "nm"
            assert scan_y[1] == pytest.approx(expected.y.scale)

    def test_uncalibrated_detector_axes_claim_nothing(self, tmp_path):
        """
        With no camera length known, the detector axes stay pixels.

        A fabricated reciprocal scale is one a strain measurement would
        use without hesitating.
        """
        path = tmp_path / "pass.nxs"
        _acquire(path)

        with h5py.File(path, "r") as handle:
            units = handle["entry/data_camera/det_x"].attrs["units"]
            assert _text(units) == FrameCalibration().x.units

    def test_a_detector_calibration_is_written_when_given(self, tmp_path):
        """A known camera length reaches the file as reciprocal-space axes."""
        path = tmp_path / "pass.nxs"
        devices = build_preview_devices(scan=True, camera=True)
        devices.cameras["camera"].configure(
            CameraParameters(exposure_ms=10.0, binning=_A_BINNING),
        )
        diffraction = FrameCalibration.diffraction(0.05)

        with PassWriter(
            path, _A_GRID,
            cubes={"camera": _A_DETECTOR},
            detector_calibration=diffraction,
        ) as writer:
            writer.finish(
                devices.scanner.scan_synchronised(
                    _A_GRID, targets=["camera"], into=writer.destinations(),
                ),
            )

        with h5py.File(path, "r") as handle:
            axis = handle["entry/data_camera/det_x"]
            assert _text(axis.attrs["units"]) == diffraction.x.units
            assert diffraction.x.kind is AxisKind.RECIPROCAL_SPACE


class TestRefusals:
    """The file and the acquisition must agree about what was collected."""

    def test_diffraction_with_no_allocated_cube_is_refused(self, tmp_path):
        """
        A cube the writer never allocated means the two disagree.

        Refused rather than written as a fresh dataset, because the
        silent version would defeat the streaming the writer exists for.
        """
        path = tmp_path / "pass.nxs"
        devices = build_preview_devices(scan=True, camera=True)
        with PassWriter(path, _A_GRID) as writer:
            result = devices.scanner.scan_synchronised(
                _A_GRID, targets=["camera"],
            )
            with pytest.raises(ValueError, match="allocated no cube"):
                writer.finish(result)

    def test_the_pass_identity_reaches_the_file(self, tmp_path):
        """
        Identity and synchronisation are fields, not JSON to be parsed.

        They are the evidence for the file's claim that its signals
        share probe positions, so a reader should not have to decode a
        blob to find them.
        """
        path = tmp_path / "pass.nxs"
        result = _acquire(path)

        with h5py.File(path, "r") as handle:
            assert _text(handle["entry/pass_id"][()]) == result.pass_id
            assert _text(handle["entry/scan_sync"][()]) == SCAN_SYNC_SCANNER
