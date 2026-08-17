"""
Unit tests for the in-process preview instrument.

No napari, no display, and no device subprocess: the point of
:mod:`miainwoodpecker.viewer.preview` is that its devices are ordinary
objects, so the parts of it worth testing are testable the cheap way.
The window it opens is covered by the integration suite instead.
"""

import h5py
import numpy as np
import pytest

from miainwoodpecker.devices.interface import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    SCAN_SYNC_SCANNER,
    STAGE_POSITION_CONTROL,
    Camera,
    CameraParameters,
    ScanParameters,
    Scanner,
    SynchronisedScanner,
)
from miainwoodpecker.devices.rpc import SCANNER_TARGET
from miainwoodpecker.viewer.preview import (
    PREVIEW_BACKEND,
    _CAMERA_PIXELS,
    PreviewCamera,
    PreviewInstrument,
    PreviewScanner,
    build_preview_devices,
    parse_preview_args,
)

_A_SCAN = ScanParameters(height=16, width=24, pixel_time_us=2.0, fov_nm=100.0)
_A_BIG_DEFOCUS_NM = 4000.0
_A_STAGE_STEP_NM = 5000.0
_AN_ENERGY_OFFSET_EV = 12.5
_TWO_CAMERAS = 2
_TWO_CHANNELS = 2
# Small on purpose: a synchronised pass renders a full detector image per
# beam position, so an 8x8 grid is already 8*8*128*128 floats.
_A_SYNC_GRID = ScanParameters(height=8, width=8, pixel_time_us=1.0, fov_nm=3.0)
_NOISE_TOLERANCE = 0.05
_STRONG_CORRELATION = 0.8


def _scan_once(scanner: PreviewScanner) -> np.ndarray:
    """Return one frame's data from the scanner, for comparing images."""
    return scanner.scan_frame(_A_SCAN).data


class TestProtocolConformance:
    """The preview devices are the real interfaces, not lookalikes."""

    def test_camera_satisfies_the_camera_protocol(self):
        """A caller type-checking against Camera accepts the preview one."""
        assert isinstance(PreviewCamera(), Camera)

    def test_scanner_satisfies_the_scanner_protocol(self):
        """A caller type-checking against Scanner accepts the preview one."""
        assert isinstance(PreviewScanner(), Scanner)

    def test_instrument_publishes_every_control_by_default(self):
        """The default instrument offers the whole control surface."""
        assert set(PreviewInstrument().available_controls()) == {
            DEFOCUS_CONTROL,
            ENERGY_OFFSET_CONTROL,
            STAGE_POSITION_CONTROL,
            BEAM_BLANKER_CONTROL,
        }


class TestScanner:
    """Scanned frames match the request, and vary."""

    def test_frames_match_the_requested_shape(self):
        """The scan honours height and width rather than a fixed size."""
        assert _scan_once(PreviewScanner()).shape == _A_SCAN.shape

    def test_successive_frames_differ(self):
        """
        A live display needs motion, so the specimen drifts between frames.

        A constant frame would make every display-path bug — a stalled
        timer, a layer that is never updated — look exactly like correct
        behaviour.
        """
        scanner = PreviewScanner()
        assert not np.array_equal(_scan_once(scanner), _scan_once(scanner))

    def test_channels_are_distinguishable(self):
        """Two detector channels give two different images."""
        scanner = PreviewScanner()
        haadf = scanner.scan_frame(_A_SCAN, channel=0).data
        maadf = scanner.scan_frame(_A_SCAN, channel=1).data
        assert not np.array_equal(haadf, maadf)

    def test_an_unknown_channel_is_an_index_error(self):
        """The interface's vocabulary: a channel this scanner lacks is IndexError."""
        with pytest.raises(IndexError):
            PreviewScanner().scan_frame(_A_SCAN, channel=99)

    def test_frames_carry_the_documented_metadata(self):
        """Frame metadata is populated, so downstream storage has something real."""
        metadata = PreviewScanner().scan_frame(_A_SCAN).metadata
        assert metadata["device_id"] == PreviewScanner().scanner_id
        assert metadata["channel_index"] == 0
        assert metadata["channel_name"] == "HAADF"
        assert metadata["fov_nm"] == _A_SCAN.fov_nm
        assert metadata["pixel_time_us"] == _A_SCAN.pixel_time_us

    def test_frame_index_is_monotonic_and_gapless(self):
        """A dropped frame must be visible as a missing index."""
        scanner = PreviewScanner()
        indices = [
            scanner.scan_frame(_A_SCAN).metadata["frame_index"] for _ in range(3)
        ]
        assert indices == [0, 1, 2]


class TestSimultaneousScan:
    """``scan_frames`` is one pass, and says so."""

    def test_one_pass_yields_one_frame_per_channel(self):
        """Two channels come back in request order."""
        frames = PreviewScanner().scan_frames(_A_SCAN, [1, 0])
        assert [frame.metadata["channel_index"] for frame in frames] == [1, 0]

    def test_every_frame_shares_one_pass_id(self):
        """The physics: both frames came from the same probe pass."""
        frames = PreviewScanner().scan_frames(_A_SCAN, [0, 1])
        assert len({frame.metadata["scan_pass_id"] for frame in frames}) == 1

    def test_frame_index_counts_frames_not_passes(self):
        """A two-channel pass advances the frame index by two."""
        scanner = PreviewScanner()
        frames = scanner.scan_frames(_A_SCAN, [0, 1])
        assert [frame.metadata["frame_index"] for frame in frames] == [0, 1]

    def test_no_channels_is_a_value_error(self):
        """A pass reading nothing out is a request no scanner could honour."""
        with pytest.raises(ValueError, match="at least one channel"):
            PreviewScanner().scan_frames(_A_SCAN, [])

    def test_a_repeated_channel_is_a_value_error(self):
        """A detector cannot read itself out twice in one pass."""
        with pytest.raises(ValueError, match="twice"):
            PreviewScanner().scan_frames(_A_SCAN, [0, 0])

    def test_an_unknown_channel_is_an_index_error(self):
        """Same vocabulary as the single-channel path."""
        with pytest.raises(IndexError):
            PreviewScanner().scan_frames(_A_SCAN, [0, 99])


class TestCamera:
    """The camera honours its start/stop contract and its settings."""

    def test_acquiring_before_start_is_refused(self):
        """``start`` before ``acquire_frame`` is the interface's contract."""
        with pytest.raises(RuntimeError, match="start"):
            PreviewCamera().acquire_frame()

    def test_successive_frames_differ(self):
        """The live camera view has to move for the same reason the scan does."""
        camera = PreviewCamera()
        camera.start()
        assert not np.array_equal(
            camera.acquire_frame().data, camera.acquire_frame().data,
        )

    def test_binning_shrinks_the_frame(self):
        """Binning is real here, not a label: the frame actually gets smaller."""
        camera = PreviewCamera()
        camera.start()
        unbinned = camera.acquire_frame().data.shape
        camera.configure(CameraParameters(exposure_ms=10.0, binning=2))
        binned = camera.acquire_frame().data.shape
        assert binned == (unbinned[0] // 2, unbinned[1] // 2)

    def test_configure_returns_what_was_taken(self):
        """The caller reads back the settings rather than assuming them."""
        camera = PreviewCamera()
        taken = camera.configure(CameraParameters(exposure_ms=25.0, binning=2))
        assert taken == camera.parameters()

    def test_an_unsupported_binning_is_refused(self):
        """A binning the camera does not offer is an error, not a silent round."""
        camera = PreviewCamera()
        with pytest.raises(ValueError, match="binning"):
            camera.configure(CameraParameters(exposure_ms=10.0, binning=7))

    def test_stop_then_start_resumes(self):
        """``stop`` pauses; it does not retire the camera."""
        camera = PreviewCamera()
        camera.start()
        camera.stop()
        with pytest.raises(RuntimeError, match="start"):
            camera.acquire_frame()
        camera.start()
        assert camera.acquire_frame().data is not None


class TestControlsChangeTheImage:
    """
    The point of the preview: a control that does nothing visible is untestable.

    Every assertion here is one an operator could make by eye. Without
    them the preview would be a set of dials wired to nothing, and any UI
    work on the Instrument panel would be guessing whether the click
    landed.
    """

    def test_blanking_the_beam_collapses_the_scan_signal(self):
        """Blanked means no beam, so the scan goes to its noise floor."""
        instrument = PreviewInstrument()
        scanner = PreviewScanner(instrument=instrument)
        lit = _scan_once(scanner)
        instrument.set_beam_blanked(blanked=True)
        blanked = _scan_once(scanner)
        assert blanked.mean() < lit.mean() / 2

    def test_blanking_the_beam_collapses_the_camera_signal(self):
        """The camera sees the same blanker the scanner does."""
        instrument = PreviewInstrument()
        camera = PreviewCamera(instrument=instrument)
        camera.start()
        lit = camera.acquire_frame().data
        instrument.set_beam_blanked(blanked=True)
        blanked = camera.acquire_frame().data
        assert blanked.mean() < lit.mean() / 2

    def test_defocus_softens_the_scan(self):
        """
        Defocusing blurs, so the image's contrast falls.

        Measured as the standard deviation of the frame: a blurred image
        of the same specimen has less of it.
        """
        instrument = PreviewInstrument()
        scanner = PreviewScanner(instrument=instrument)
        focused = _scan_once(scanner)
        instrument.set_defocus_nm(_A_BIG_DEFOCUS_NM)
        defocused = _scan_once(scanner)
        assert defocused.std() < focused.std()

    def test_two_fresh_scanners_produce_the_same_first_frame(self):
        """
        The preview is seeded, so a session can be reproduced.

        Also what makes the stage test below mean anything: with the noise
        held identical between two fresh scanners, a difference in their
        frames can only have come from the control that was changed.
        """
        assert np.array_equal(
            _scan_once(PreviewScanner()), _scan_once(PreviewScanner()),
        )

    def test_moving_the_stage_changes_the_field_of_view(self):
        """
        A stage move shows a different part of the specimen.

        Compared across two *fresh* scanners rather than two frames from
        one, so the noise and the drift are identical on both sides and
        the stage is the only difference. Taking two frames in a row from
        one scanner would differ whether or not the stage did anything,
        which would make this pass on a preview whose stage was a dud.
        """
        moved = PreviewInstrument()
        moved.set_stage_position_nm(_A_STAGE_STEP_NM, _A_STAGE_STEP_NM)
        assert not np.allclose(
            _scan_once(PreviewScanner(instrument=PreviewInstrument())),
            _scan_once(PreviewScanner(instrument=moved)),
        )

    def test_a_control_reads_back_what_was_set(self):
        """The panel's Refresh button has something true to read."""
        instrument = PreviewInstrument()
        instrument.set_defocus_nm(_A_BIG_DEFOCUS_NM)
        instrument.set_energy_offset_ev(_AN_ENERGY_OFFSET_EV)
        instrument.set_stage_position_nm(1.0, 2.0)
        instrument.set_beam_blanked(blanked=True)

        assert instrument.defocus_nm() == _A_BIG_DEFOCUS_NM
        assert instrument.energy_offset_ev() == _AN_ENERGY_OFFSET_EV
        assert instrument.stage_position_nm() == (1.0, 2.0)
        assert instrument.is_beam_blanked() is True

    def test_park_blanks_the_beam(self):
        """``park`` leaves the instrument in a safe unattended state."""
        instrument = PreviewInstrument()
        instrument.park()
        assert instrument.is_beam_blanked() is True


class TestSelectableCapabilities:
    """The preview can be cut down, so absent-device UI paths are reachable."""

    def test_a_withheld_control_is_not_published(self):
        """
        Publishing a subset is how the 'absent, not disabled' rule gets exercised.

        The panel builds a row only for a published control, so a preview
        that always published all four could never show what a microscope
        with no blanker looks like.
        """
        instrument = PreviewInstrument(controls=[DEFOCUS_CONTROL])
        assert list(instrument.available_controls()) == [DEFOCUS_CONTROL]

    def test_camera_only_devices_have_no_scanner(self):
        """A detector-only instrument is a real configuration, and reachable here."""
        devices = build_preview_devices(scan=False, camera=True)
        assert devices.scanner is None
        assert devices.cameras

    def test_scan_only_devices_have_no_camera(self):
        """So is a scan-only one."""
        devices = build_preview_devices(scan=True, camera=False)
        assert devices.scanner is not None
        assert not devices.cameras

    def test_an_instrument_with_neither_is_refused(self):
        """Nothing to display is worth refusing rather than opening empty."""
        with pytest.raises(ValueError, match="scan unit or a camera"):
            build_preview_devices(scan=False, camera=False)

    def test_describe_reports_the_backend_and_what_is_served(self):
        """The panel's top line says what you are connected to; it must be true."""
        devices = build_preview_devices(scan=True, camera=True)
        description = devices.instrument.describe()
        assert description["backend"] == PREVIEW_BACKEND
        assert SCANNER_TARGET in description["targets"]
        assert "camera" in description["targets"]

    def test_two_cameras_are_served_under_distinct_names(self):
        """The two-camera layout is a UI case worth being able to open."""
        devices = build_preview_devices(
            scan=False, camera=True, camera_count=_TWO_CAMERAS,
        )
        assert len(devices.cameras) == _TWO_CAMERAS
        assert len(set(devices.cameras)) == _TWO_CAMERAS

    def test_two_served_cameras_do_not_produce_identical_frames(self):
        """
        Each served camera gets its own noise, so they are tellable apart.

        Without this a bug that pointed both camera sections at one
        camera would look exactly like two cameras working.
        """
        devices = build_preview_devices(
            scan=False, camera=True, camera_count=_TWO_CAMERAS,
        )
        frames = []
        for camera in devices.cameras.values():
            camera.start()
            frames.append(camera.acquire_frame().data)
        assert not np.array_equal(frames[0], frames[1])


class TestSynchronisedPass:
    """
    The preview really performs a synchronised pass, and that is the point.

    The nionswift-usim backend cannot — measured, not assumed, in
    ``analysis/py4dstem_bridge.py`` — so the preview is the only device
    in this project against which spectrum-imaging and 4D-STEM work can
    be built and tested at all.
    """

    def test_the_scanner_declares_the_capability(self):
        """
        ``isinstance`` is the honest question here, unlike on Scanner.

        The protocol has exactly the methods the capability needs, so an
        adapter either implements synchronised acquisition or does not —
        no all-or-nothing problem to work around.
        """
        devices = build_preview_devices(scan=True, camera=True)
        assert isinstance(devices.scanner, SynchronisedScanner)

    def test_a_scanner_with_no_camera_synchronises_nothing(self):
        """A column with nothing wired to it says so rather than pretending."""
        devices = build_preview_devices(scan=True, camera=False)
        assert list(devices.scanner.synchronised_targets()) == []

    def test_one_pass_yields_images_and_diffraction_together(self):
        """
        The whole concept: several signals, one traversal, one identity.

        The shared ``scan_pass_id`` is what makes the correlation a fact
        rather than a claim, and it is only stamped because one call to
        one device really did traverse once.
        """
        devices = build_preview_devices(scan=True, camera=True)
        result = devices.scanner.scan_synchronised(
            _A_SYNC_GRID, channels=[0, 1], targets=["camera"],
        )

        assert len(result.images) == _TWO_CHANNELS
        stack = result.diffraction["camera"]
        assert result.images[0].metadata["scan_pass_id"] == result.pass_id
        assert stack.metadata["scan_pass_id"] == result.pass_id

    def test_the_datacube_is_beam_positions_by_detector(self):
        """The cube's navigation axes are the grid that was asked for."""
        devices = build_preview_devices(scan=True, camera=True)
        stack = devices.scanner.scan_synchronised(
            _A_SYNC_GRID, targets=["camera"],
        ).diffraction["camera"]

        assert stack.navigation_shape == _A_SYNC_GRID.shape
        assert len(stack.detector_shape) == _TWO_CHANNELS

    def test_binning_shrinks_the_detector_axes_only(self):
        """
        Binning is the camera's, so it changes the signal axes and not the grid.

        The beam-position count is the scan's business; confusing the two
        is how a 4D dataset ends up with the wrong axes.
        """
        devices = build_preview_devices(scan=True, camera=True)
        devices.cameras["camera"].configure(
            CameraParameters(exposure_ms=10.0, binning=2),
        )
        stack = devices.scanner.scan_synchronised(
            _A_SYNC_GRID, targets=["camera"],
        ).diffraction["camera"]

        assert stack.navigation_shape == _A_SYNC_GRID.shape
        assert stack.detector_shape == (_CAMERA_PIXELS // 2, _CAMERA_PIXELS // 2)

    def test_the_diffraction_actually_varies_with_probe_position(self):
        """
        Every pattern identical would make any 4D analysis "succeed".

        This is the property that separates a real implementation from a
        stack of copies, and the reason usim cannot stand in: there,
        patterns at different probe positions differ only by shot noise.
        """
        devices = build_preview_devices(scan=True, camera=True)
        cube = devices.scanner.scan_synchronised(
            _A_SYNC_GRID, targets=["camera"],
        ).diffraction["camera"].data

        first = cube[0, 0]
        last = cube[-1, -1]
        assert not np.allclose(first, last, atol=_NOISE_TOLERANCE)

    def test_centre_of_mass_reconstructs_the_specimen_gradient(self):
        """
        The deflection model has a right answer, so a DPC map can be checked.

        The disc is pushed off axis by the local slope of the specimen
        field, which is what a real phase gradient does. So the centre of
        mass across the cube should track that slope — and a
        centre-of-mass implementation run against this data can be
        *wrong*, which is what makes the fixture worth having.
        """
        devices = build_preview_devices(scan=True, camera=True)
        result = devices.scanner.scan_synchronised(
            _A_SYNC_GRID, channels=[0], targets=["camera"],
        )
        cube = result.diffraction["camera"].data

        _, columns = np.indices(cube.shape[2:])
        totals = cube.sum(axis=(2, 3))
        centre_x = (cube * columns).sum(axis=(2, 3)) / totals

        # The same slope the deflection was built from, recovered from
        # the images the pass also produced rather than from the model.
        _, expected = np.gradient(np.asarray(result.images[0].data))
        correlation = np.corrcoef(centre_x.ravel(), expected.ravel())[0, 1]
        assert correlation > _STRONG_CORRELATION

    def test_a_pass_reports_which_device_was_master(self):
        """
        A pass says how it synchronised, because the shape cannot.

        A detector-mastered acquisition and an unsynchronised one produce
        identically shaped datasets. The preview's loop is the scan
        driving and the camera following, so it says so.
        """
        devices = build_preview_devices(scan=True, camera=True)
        result = devices.scanner.scan_synchronised(
            _A_SYNC_GRID, targets=["camera"],
        )
        assert result.scan_sync == SCAN_SYNC_SCANNER

    def test_a_pre_allocated_cube_is_filled_rather_than_copied(self):
        """
        The caller's array *is* the pass's array, not a copy of it.

        This is the whole point of ``into``: a 64x64 grid on a 512x512
        detector is four gigabytes, and the vendor APIs fill a
        caller-owned destination precisely so the data never moves
        twice. Asserted by identity, because a version that filled the
        buffer and then returned a copy would pass any value-based
        check while doing exactly the work this avoids.
        """
        devices = build_preview_devices(scan=True, camera=True)
        detector = _CAMERA_PIXELS
        destination = np.zeros(
            (*_A_SYNC_GRID.shape, detector, detector), dtype=np.float32,
        )

        stack = devices.scanner.scan_synchronised(
            _A_SYNC_GRID, targets=["camera"], into={"camera": destination},
        ).diffraction["camera"]

        assert stack.data is destination
        assert destination.any(), "the destination was filled, not left zeroed"

    def test_the_destination_can_be_an_on_disk_dataset(self, tmp_path):
        """
        Streaming and pre-allocation are the same mechanism, not rival ones.

        A 64x64 grid on a 512x512 detector is four gigabytes: it cannot
        be held in RAM *and* it wants a caller-owned buffer handed to the
        device. Both at once is only possible if the buffer does not have
        to be memory — so ``into`` is defined by what it supports
        (shape, and ``__setitem__`` per beam position) rather than as
        ``numpy.ndarray``, and an HDF5 dataset satisfies it. The cube is
        then written to disk as it is acquired and never exists whole in
        memory.

        Chunked one beam position per chunk, which is what makes the
        per-position writes land as single chunk writes rather than
        read-modify-write of a larger block.
        """
        devices = build_preview_devices(scan=True, camera=True)
        path = tmp_path / "cube.h5"
        detector = _CAMERA_PIXELS

        with h5py.File(path, "w") as handle:
            dataset = handle.create_dataset(
                "cube",
                shape=(*_A_SYNC_GRID.shape, detector, detector),
                dtype="float32",
                chunks=(1, 1, detector, detector),
            )
            stack = devices.scanner.scan_synchronised(
                _A_SYNC_GRID, targets=["camera"], into={"camera": dataset},
            ).diffraction["camera"]

            assert stack.data is dataset
            assert stack.navigation_shape == _A_SYNC_GRID.shape
            assert np.asarray(dataset[0, 0]).any()

        assert path.stat().st_size > 0

    def test_a_destination_of_the_wrong_shape_is_refused(self):
        """
        A mismatch means one of the two is wrong about what is being acquired.

        Refused rather than reshaped around: the caller allocated from
        its own idea of the grid, and quietly ignoring that would hand
        back a dataset whose axes are not what was asked for.
        """
        devices = build_preview_devices(scan=True, camera=True)
        wrong = np.zeros((2, 2, _CAMERA_PIXELS, _CAMERA_PIXELS), dtype=np.float32)

        with pytest.raises(ValueError, match="navigation shape"):
            devices.scanner.scan_synchronised(
                _A_SYNC_GRID, targets=["camera"], into={"camera": wrong},
            )

    def test_a_pass_reading_nothing_out_is_refused(self):
        """Naming no channel and no target is a traversal for nothing."""
        devices = build_preview_devices(scan=True, camera=True)
        with pytest.raises(ValueError, match="must read something out"):
            devices.scanner.scan_synchronised(_A_SYNC_GRID)

    def test_an_unsynchronisable_target_is_refused(self):
        """
        Refused rather than quietly acquired serially.

        A caller cannot tell a synchronised cube from an unsynchronised
        one by looking at it, so the refusal has to happen here.
        """
        devices = build_preview_devices(scan=True, camera=True)
        with pytest.raises(ValueError, match="cannot synchronise"):
            devices.scanner.scan_synchronised(_A_SYNC_GRID, targets=["eels_camera"])

    def test_a_repeated_channel_is_refused(self):
        """Same vocabulary as ``scan_frames``: one pass cannot read one twice."""
        devices = build_preview_devices(scan=True, camera=True)
        with pytest.raises(ValueError, match="twice"):
            devices.scanner.scan_synchronised(_A_SYNC_GRID, channels=[0, 0])

    def test_an_unknown_channel_is_an_index_error(self):
        """Same vocabulary as ``scan_frames`` for a channel that is not fitted."""
        devices = build_preview_devices(scan=True, camera=True)
        with pytest.raises(IndexError):
            devices.scanner.scan_synchronised(_A_SYNC_GRID, channels=[99])


class TestArgumentParsing:
    """The command line resolves to the configuration it names."""

    def test_defaults_serve_a_scan_and_a_camera(self):
        """The default preview is the full window."""
        args = parse_preview_args([])
        assert args.scan is True
        assert args.camera is True

    def test_camera_only_is_selectable(self):
        """``--no-scan`` opens the detector-only window."""
        assert parse_preview_args(["--no-scan"]).scan is False

    def test_controls_can_be_narrowed(self):
        """``--controls`` picks which rows the Instrument panel builds."""
        args = parse_preview_args(["--controls", "defocus,beam_blanker"])
        assert args.controls == [DEFOCUS_CONTROL, BEAM_BLANKER_CONTROL]

    def test_an_unknown_control_is_rejected(self):
        """A typo names no control, and is worth failing on rather than ignoring."""
        with pytest.raises(SystemExit):
            parse_preview_args(["--controls", "focus"])
