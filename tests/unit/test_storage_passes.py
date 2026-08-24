"""
Unit tests for storing a scan pass.

The property that matters most here is not what the file contains but
*how the data got into it*: the cube is written where it will live,
rather than acquired into memory and copied. A test comparing only
values would pass either way, so identity is asserted directly.
"""

import dataclasses
import pathlib

import h5py
import numpy as np
import pytest

from miainwoodpecker.devices import (
    PROJECTED_READOUT,
    SCAN_SYNC_SCANNER,
    CameraParameters,
    ScanParameters,
)
from miainwoodpecker.storage.calibration import (
    METADATA_KEY,
    AxisKind,
    FrameCalibration,
)
from miainwoodpecker.storage.passes import PassWriter, read_pass
from miainwoodpecker.viewer.preview import (
    _CAMERA_PIXELS,
    _EELS_CHANNELS,
    _EELS_TARGET,
    build_preview_devices,
)

_A_GRID = ScanParameters(height=6, width=4, pixel_time_us=1.0, fov_nm=12.0)
# Binned 4x so the cube stays small: 6*4 positions of 32x32 floats is
# under a megabyte, where unbinned would be sixteen times that in every
# test in this file.
_A_BINNING = 4
_A_DETECTOR = (_CAMERA_PIXELS // _A_BINNING, _CAMERA_PIXELS // _A_BINNING)
_FOUR_AXES = 4
# Two cameras, because that is what serves a real spectrometer: the
# preview decides a camera's kind from the target name it is served
# under, and a single camera is served under the neutral one.
_TWO_CAMERAS = 2
# Where NXspectrum puts the energy axis: last, always.
_ENERGY_AXIS_INDEX = 2


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
            # NX_UINT, and h5py would write a bare Python int as a
            # signed int64 - which pynxtools rejects on type. The
            # values above pass either way, so the dtype is what
            # actually pins it.
            for name in ("scan_y_indices", "det_x_indices"):
                assert group.attrs[name].dtype.kind == "u", name

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

    def test_the_detectors_own_axes_reach_the_file(self, tmp_path):
        """
        A detector that publishes its axes has them written.

        This used to assert the opposite — that the detector axes stayed
        pixels — because the preview's camera published nothing and
        pixels were all there was. The case that changed it is a
        spectrometer read out in 2D: its stack lands in the same 4D
        container a Ronchigram camera's does, so the axes are the only
        thing distinguishing them, and a writer that ignored what the
        device said would drop that distinction on the floor.
        """
        path = tmp_path / "pass.nxs"
        _acquire(path)

        with h5py.File(path, "r") as handle:
            assert _text(handle["entry/data_camera/det_x"].attrs["units"]) == "mrad"

    def test_a_detector_that_publishes_nothing_still_gets_pixels(self, tmp_path):
        """
        The honest fallback survives, for a device that says nothing.

        A fabricated reciprocal scale is one a strain measurement would
        use without hesitating, so "nobody told us" has to stay
        distinguishable from a real calibration.
        """
        path = tmp_path / "pass.nxs"
        devices = build_preview_devices(scan=True, camera=True)
        devices.cameras["camera"].configure(
            CameraParameters(exposure_ms=10.0, binning=_A_BINNING),
        )
        with PassWriter(path, _A_GRID, cubes={"camera": _A_DETECTOR}) as writer:
            result = devices.scanner.scan_synchronised(
                _A_GRID, channels=[0], targets=["camera"],
                into=writer.destinations(),
            )
            result.diffraction["camera"].metadata.pop(METADATA_KEY)
            writer.finish(result)

        with h5py.File(path, "r") as handle:
            units = handle["entry/data_camera/det_x"].attrs["units"]
            assert _text(units) == FrameCalibration().x.units

    def test_the_scan_geometry_never_calibrates_the_detector(self, tmp_path):
        """
        Detector axes are not specimen axes, however the metadata reads.

        The shared calibration resolver falls back to ``fov_size_nm``
        when nothing else is offered, and that is the *scan's* field of
        view. A stack whose metadata happened to carry it would come out
        with its detector labelled in nanometres of specimen — a wrong
        answer rather than a missing one, so only the calibration key is
        ever consulted here.
        """
        path = tmp_path / "pass.nxs"
        devices = build_preview_devices(scan=True, camera=True)
        devices.cameras["camera"].configure(
            CameraParameters(exposure_ms=10.0, binning=_A_BINNING),
        )
        with PassWriter(path, _A_GRID, cubes={"camera": _A_DETECTOR}) as writer:
            result = devices.scanner.scan_synchronised(
                _A_GRID, channels=[0], targets=["camera"],
                into=writer.destinations(),
            )
            metadata = result.diffraction["camera"].metadata
            metadata.pop(METADATA_KEY)
            metadata["fov_size_nm"] = list(_A_GRID.fov_size_nm)
            writer.finish(result)

        with h5py.File(path, "r") as handle:
            units = handle["entry/data_camera/det_x"].attrs["units"]
            assert _text(units) != "nm"

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
            with pytest.raises(ValueError, match="allocated nothing"):
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


def _acquire_spectrum_image(
    path: pathlib.Path,
    *,
    projected: bool = True,
) -> tuple[object, object]:
    """
    Acquire one EELS pass straight into a file, the way the writer intends.

    Parameters
    ----------
    path : pathlib.Path
        Where to write.
    projected : bool
        Whether to put the spectrometer into its projected readout, which
        is what decides whether the pass carries a spectrum image or a 4D
        stack.

    Returns
    -------
    tuple[object, object]
        The completed pass and the spectrometer it came from.
    """
    devices = build_preview_devices(scan=True, camera=True, camera_count=_TWO_CAMERAS)
    camera = devices.cameras[_EELS_TARGET]
    if projected:
        camera.configure(
            dataclasses.replace(camera.parameters(), readout=PROJECTED_READOUT),
        )
        allocation = {"spectra": {_EELS_TARGET: camera.channel_count}}
    else:
        allocation = {"cubes": {_EELS_TARGET: camera.readout_shape}}
    with PassWriter(path, _A_GRID, **allocation) as writer:
        result = devices.scanner.scan_synchronised(
            _A_GRID,
            channels=[0, 1],
            targets=[_EELS_TARGET],
            into=writer.destinations(),
        )
        writer.finish(result)
    return result, camera


class TestSpectrumImages:
    """
    A pass whose per-position readout is spectra rather than images.

    Stored in the same file as the image channels it shares probe
    positions with, and spelled in ``NXspectrum``'s vocabulary so a
    reader that knows how to find spectra finds these under the names it
    already looks for.
    """

    def test_the_device_fills_the_files_own_dataset(self, tmp_path):
        """
        The same streaming contract the diffraction cube has.

        A spectrum image is smaller than a 4D cube and still large: the
        writer creates the dataset, the device fills it as it acquires,
        and the pass carries the file's array rather than a copy of it.
        """
        path = tmp_path / "si.nxs"
        result, camera = _acquire_spectrum_image(path)
        del result
        with PassWriter(
            tmp_path / "empty.nxs",
            _A_GRID,
            spectra={_EELS_TARGET: camera.channel_count},
        ) as writer:
            destination = writer.destinations()[_EELS_TARGET]
            assert isinstance(destination, h5py.Dataset)
            assert destination.shape == (*_A_GRID.shape, camera.channel_count)

    def test_it_is_chunked_one_beam_position_at_a_time(self, tmp_path):
        """
        Chunked by position, and deliberately not by row.

        ``SpectrumWriter`` chunks a whole row because it receives a
        finished map and writes it in one assignment. Here the device
        writes position by position, so a row chunk would turn each of
        those into a read-modify-write of the rest of the row.
        """
        with PassWriter(
            tmp_path / "si.nxs", _A_GRID, spectra={_EELS_TARGET: _EELS_CHANNELS},
        ) as writer:
            assert writer.destinations()[_EELS_TARGET].chunks == (
                1, 1, _EELS_CHANNELS,
            )

    def test_it_is_written_in_the_nxspectrum_vocabulary(self, tmp_path):
        """
        The signal is ``intensity`` and the fastest axis is energy.

        Two signal names in one file looks like an inconsistency and is
        the opposite: a spectrum image inside a pass is not a different
        format from a spectrum image beside one.
        """
        path = tmp_path / "si.nxs"
        _acquire_spectrum_image(path)
        with h5py.File(path, "r") as handle:
            group = handle[f"entry/data_{_EELS_TARGET}"]
            assert _text(group.attrs["signal"]) == "intensity"
            assert [_text(axis) for axis in group.attrs["axes"]] == [
                "axis_j", "axis_i", "axis_energy",
            ]
            assert group.attrs["axis_energy_indices"] == _ENERGY_AXIS_INDEX

    def test_the_energy_axis_is_the_one_the_acquisition_ran_at(self, tmp_path):
        """
        Taken from the spectrum rather than from anything requested.

        A detector is free to have rounded a requested dispersion, and a
        file recording the request would shift every identified edge.
        """
        path = tmp_path / "si.nxs"
        result, _ = _acquire_spectrum_image(path)
        spectrum = result.spectra[_EELS_TARGET]
        with h5py.File(path, "r") as handle:
            energies = handle[f"entry/data_{_EELS_TARGET}/axis_energy"][()]
            units = _text(
                handle[f"entry/data_{_EELS_TARGET}/axis_energy"].attrs["units"],
            )
        assert units == "eV"
        assert energies[0] == pytest.approx(spectrum.energy_offset_ev)
        assert energies[1] - energies[0] == pytest.approx(spectrum.energy_scale_ev)

    def test_the_spatial_axes_are_the_scans_own(self, tmp_path):
        """
        A spectrum image and an image channel of one pass cover one region.

        Both calibrations come from the pass's scan geometry through the
        same call, so they cannot disagree about their own extent.
        """
        path = tmp_path / "si.nxs"
        _acquire_spectrum_image(path)
        with h5py.File(path, "r") as handle:
            slow = handle[f"entry/data_{_EELS_TARGET}/axis_j"]
            assert _text(slow.attrs["units"]) == "nm"
            assert len(slow) == _A_GRID.height

    def test_read_pass_reports_it_beside_the_image_channels(self, tmp_path):
        """
        The reader asks each group what its signal is called.

        A reader that assumed ``data`` would silently omit every spectrum
        image from the list of what a file contains.
        """
        path = tmp_path / "si.nxs"
        _acquire_spectrum_image(path)
        signals = read_pass(path).signals
        assert signals[f"data_{_EELS_TARGET}"] == (*_A_GRID.shape, _EELS_CHANNELS)
        assert "data_HAADF" in signals
        assert "data_MAADF" in signals

    def test_the_default_points_at_the_spectrum_image(self, tmp_path):
        """The per-position signal is the reason the pass was taken."""
        path = tmp_path / "si.nxs"
        _acquire_spectrum_image(path)
        with h5py.File(path, "r") as handle:
            assert handle["entry/data/intensity"].shape == (
                *_A_GRID.shape, _EELS_CHANNELS,
            )

    def test_the_written_spectra_are_not_all_zeros(self, tmp_path):
        """The acquisition wrote through rather than allocating and stopping."""
        path = tmp_path / "si.nxs"
        _acquire_spectrum_image(path)
        with h5py.File(path, "r") as handle:
            assert np.asarray(
                handle[f"entry/data_{_EELS_TARGET}/intensity"][0, 0],
            ).any()

    def test_an_imaged_spectrometer_keeps_its_energy_axis(self, tmp_path):
        """
        A spectrometer left imaging is a real experiment, stored honestly.

        Its 4D stack lands in the same container a Ronchigram camera's
        does, because at that point the two are the same shape of data.
        What separates them is the axes, so the detector's own
        calibration has to reach the file — otherwise the one fact
        making it a spectrometer is gone.
        """
        path = tmp_path / "imaged.nxs"
        _acquire_spectrum_image(path, projected=False)
        with h5py.File(path, "r") as handle:
            fast = handle[f"entry/data_{_EELS_TARGET}/det_x"]
            assert _text(fast.attrs["units"]) == "eV"

    def test_a_spectrum_image_with_nowhere_to_go_is_refused(self, tmp_path):
        """A signal the writer allocated nothing for means the two disagree."""
        devices = build_preview_devices(
            scan=True, camera=True, camera_count=_TWO_CAMERAS,
        )
        camera = devices.cameras[_EELS_TARGET]
        camera.configure(
            dataclasses.replace(camera.parameters(), readout=PROJECTED_READOUT),
        )
        with PassWriter(tmp_path / "si.nxs", _A_GRID) as writer:
            result = devices.scanner.scan_synchronised(
                _A_GRID, targets=[_EELS_TARGET],
            )
            with pytest.raises(ValueError, match="allocated nothing"):
                writer.finish(result)

    def test_a_target_allocated_as_the_other_kind_is_refused(self, tmp_path):
        """
        Told apart from "nothing allocated", because the fix is different.

        This one means the detector's readout mode and the shape the file
        was opened for disagree — an operator who left the spectrometer
        imaging, or projected it after sizing the file.
        """
        devices = build_preview_devices(
            scan=True, camera=True, camera_count=_TWO_CAMERAS,
        )
        camera = devices.cameras[_EELS_TARGET]
        camera.configure(
            dataclasses.replace(camera.parameters(), readout=PROJECTED_READOUT),
        )
        with PassWriter(
            tmp_path / "si.nxs", _A_GRID, cubes={_EELS_TARGET: _A_DETECTOR},
        ) as writer:
            result = devices.scanner.scan_synchronised(
                _A_GRID, targets=[_EELS_TARGET],
            )
            with pytest.raises(ValueError, match="other kind of signal"):
                writer.finish(result)

    def test_a_spectrum_image_of_no_channels_is_refused(self, tmp_path):
        """Counts against an axis of nothing are not a spectrum."""
        with pytest.raises(ValueError, match="at least one energy channel"):
            PassWriter(tmp_path / "si.nxs", _A_GRID, spectra={_EELS_TARGET: 0})

    def test_one_target_cannot_be_both_kinds(self, tmp_path):
        """
        A target produces one signal per pass, so only one could be filled.

        Refused when the destinations are asked for, which is before the
        acquisition runs — the other order would discover it after the
        beam had already been on the specimen.
        """
        with PassWriter(
            tmp_path / "si.nxs",
            _A_GRID,
            cubes={_EELS_TARGET: _A_DETECTOR},
            spectra={_EELS_TARGET: _EELS_CHANNELS},
        ) as writer, pytest.raises(ValueError, match="both a diffraction cube"):
            writer.destinations()
