"""
Unit tests for the in-process preview instrument.

No napari, no display, and no device subprocess: the point of
:mod:`miainwoodpecker.viewer.preview` is that its devices are ordinary
objects, so the parts of it worth testing are testable the cheap way.
The window it opens is covered by the integration suite instead.
"""

import numpy as np
import pytest

from miainwoodpecker.devices.interface import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    STAGE_POSITION_CONTROL,
    Camera,
    CameraParameters,
    ScanParameters,
    Scanner,
)
from miainwoodpecker.devices.rpc import SCANNER_TARGET
from miainwoodpecker.viewer.preview import (
    PREVIEW_BACKEND,
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
