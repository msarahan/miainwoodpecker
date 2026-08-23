"""
Unit tests for the replay device.

**No vendor files are read here**, and that is the design rather than a
compromise: :class:`~miainwoodpecker.devices.replay.ReplayRecording` is a
plain value object, so everything that makes this a *device* — the
refusals, the pass, the timing, the provenance — is reachable by
constructing one. Reading DigitalMicrograph is a separate concern with
its own test below, skipped where no such file exists.

The one thing these tests are careful never to assert is that the data
came out looking like anything in particular. A replay device that
altered its recording would be useless, so what is checked is identity:
what goes in is what comes out, at the position it went in at.
"""

import pathlib
import time

import numpy as np
import pytest

from miainwoodpecker.devices import replay
from miainwoodpecker.devices.interface import (
    PROJECTED_READOUT,
    SCAN_SYNC_DETECTOR,
    Camera,
    CameraParameters,
    ScanParameters,
    Scanner,
    SynchronisedScanner,
    native_scan_parameters,
)
from miainwoodpecker.devices.replay import (
    EELS_TARGET,
    REPLAY_BACKEND,
    ReplayDataError,
    ReplayInstrument,
    ReplayRecording,
    ReplayScanner,
    ReplaySpectrometer,
    build_replay_devices,
    find_recordings,
)

_HEIGHT = 4
_WIDTH = 3
_CHANNELS = 8
_PIXEL_NM = 0.5
_DWELL_S = 0.002
# Fast enough that the suite does not wait on it, slow enough that the
# timing test below has something to measure.
_FAST = 100.0


def _recording(**overrides: object) -> ReplayRecording:
    """
    Build a recording whose every value is known, for asserting against.

    The spectra are ``position * 1000 + channel`` so that any transpose,
    any off-by-one in the acquisition loop, and any position written
    twice are all visible in the data rather than only in a shape.

    Parameters
    ----------
    **overrides : object
        Fields to replace.

    Returns
    -------
    ReplayRecording
        The recording.
    """
    positions = np.arange(_HEIGHT * _WIDTH).reshape(_HEIGHT, _WIDTH, 1)
    channels = np.arange(_CHANNELS).reshape(1, 1, _CHANNELS)
    fields: dict[str, object] = {
        "label": "007_EELS-SI",
        "spectra": (positions * 1000 + channels).astype(np.float32),
        "energy_offset_ev": 140.0,
        "energy_scale_ev": 0.4,
        "pixel_size_nm": _PIXEL_NM,
        "pixel_time_s": _DWELL_S,
        "exposure_ms": 200.0,
        "binning": 2,
        "high_tension_v": 100_000.0,
        "energy_offset_v": 160.0,
        "detector_name": "Enfina",
        "image": np.arange(_HEIGHT * _WIDTH, dtype=np.float32).reshape(
            _HEIGHT, _WIDTH,
        ),
        "survey": np.zeros((16, 16), dtype=np.float32),
        "survey_pixel_size_nm": 0.05,
        "source": pathlib.Path("session/007_EELS-SI.dm3"),
    }
    fields.update(overrides)
    return ReplayRecording(**fields)  # type: ignore[arg-type]


def _devices(**overrides: object) -> tuple[ReplayScanner, ReplaySpectrometer]:
    """
    Build a scanner and its spectrometer over one recording.

    Parameters
    ----------
    **overrides : object
        Fields to replace on the recording.

    Returns
    -------
    tuple[ReplayScanner, ReplaySpectrometer]
        The scan unit and the detector wired to it.
    """
    recording = _recording(**overrides)
    spectrometer = ReplaySpectrometer(recording, speed=_FAST)
    return ReplayScanner(recording, spectrometer, speed=_FAST), spectrometer


class TestProtocolConformance:
    """The replay devices are the real interfaces, not lookalikes."""

    def test_the_scanner_is_a_scanner(self):
        """A caller type-checking against Scanner accepts this one."""
        scanner, _ = _devices()
        assert isinstance(scanner, Scanner)

    def test_the_scanner_can_synchronise(self):
        """
        The check and the capability are the same question.

        This is the second implementation of the protocol in the project
        and the first whose data was recorded on an instrument.
        """
        scanner, _ = _devices()
        assert isinstance(scanner, SynchronisedScanner)

    def test_the_spectrometer_is_a_camera(self):
        """A spectrometer is a Camera, whatever its axes mean."""
        _, spectrometer = _devices()
        assert isinstance(spectrometer, Camera)


class TestGeometry:
    """A recording's grid is a fact about the file, not a request."""

    def test_the_grid_is_the_recordings_own(self):
        """Height, width and dwell all come from what was acquired."""
        parameters = _recording().scan_parameters()
        assert parameters.shape == (_HEIGHT, _WIDTH)
        assert parameters.pixel_time_us == pytest.approx(_DWELL_S * 1e6)

    def test_the_pixel_size_round_trips(self):
        """
        The field of view is derived so the pixel size survives it.

        ``ScanParameters`` spans its longer axis, so a field of view
        computed against the wrong one comes back as a pixel size that is
        wrong by the aspect ratio — silently, and on every stored axis.
        """
        parameters = _recording().scan_parameters()
        assert parameters.pixel_size_nm == pytest.approx(_PIXEL_NM)

    def test_a_line_scan_has_no_grid_to_scan(self):
        """
        A line scan is loadable and is not a pass.

        Real sessions hold them — this one does — so the refusal names
        what the recording is rather than treating it as corrupt.
        """
        line = _recording(spectra=np.zeros((5, _CHANNELS), dtype=np.float32))
        with pytest.raises(ReplayDataError, match="not a spectrum image"):
            line.scan_parameters()

    def test_the_device_refuses_to_be_built_on_one(self):
        """
        Failing at construction beats failing when Acquire is pressed.

        An operator two minutes into a session should not discover then
        that the recording they opened was never scannable.
        """
        recording = _recording(spectra=np.zeros((5, _CHANNELS), dtype=np.float32))
        with pytest.raises(ReplayDataError, match="not a spectrum image"):
            ReplayScanner(recording, ReplaySpectrometer(recording))

    def test_the_native_geometry_is_advertised(self):
        """A caller can ask what this device holds before acquiring it."""
        scanner, _ = _devices()
        assert native_scan_parameters(scanner).shape == (_HEIGHT, _WIDTH)

    def test_an_ordinary_scanner_advertises_nothing(self):
        """
        Most scan units take whatever grid they are given.

        Asked through a function rather than another runtime-checkable
        protocol, so a device with no such constraint simply answers None.
        """
        assert native_scan_parameters(object()) is None


class TestTheRefusals:
    """A replay hands back what was acquired, or says why it cannot."""

    def test_another_grid_is_refused_rather_than_resampled(self):
        """
        The failure this whole acquisition path exists to prevent.

        A cube of the requested shape whose every pixel was interpolated
        looks exactly like a real one, and every number computed per
        pixel from it would be computed against a position the probe
        never visited.
        """
        scanner, _ = _devices()
        elsewhere = ScanParameters(
            height=32, width=32, pixel_time_us=1.0, fov_nm=10.0,
        )
        with pytest.raises(ReplayDataError, match="cannot be replayed over"):
            scanner.scan_synchronised(elsewhere, targets=[EELS_TARGET])

    def test_the_refusal_names_the_grid_it_does_have(self):
        """An operator can act on "4x3"; they cannot act on "wrong shape"."""
        scanner, _ = _devices()
        elsewhere = ScanParameters(
            height=32, width=32, pixel_time_us=1.0, fov_nm=10.0,
        )
        with pytest.raises(ReplayDataError, match=f"{_HEIGHT}x{_WIDTH}"):
            scanner.scan_synchronised(elsewhere, targets=[EELS_TARGET])

    def test_a_target_this_scanner_has_not_got_is_refused(self):
        """Same vocabulary as every other scanner for an unknown target."""
        scanner, _ = _devices()
        with pytest.raises(ValueError, match="cannot synchronise"):
            scanner.scan_synchronised(
                _recording().scan_parameters(), targets=["ronchigram_camera"],
            )

    def test_reconfiguring_the_spectrometer_is_refused(self):
        """
        The spectra were dispersed at the settings the operator used.

        Accepting a new exposure would claim the spectra handed back
        afterwards were acquired at it, which no acquisition can make
        true.
        """
        _, spectrometer = _devices()
        with pytest.raises(ReplayDataError, match="cannot be reconfigured"):
            spectrometer.configure(CameraParameters(exposure_ms=10.0, binning=2))

    def test_the_settings_it_already_has_are_accepted(self):
        """Configuring a device to what it is already set to is a no-op."""
        _, spectrometer = _devices()
        assert spectrometer.configure(spectrometer.parameters()) == (
            spectrometer.parameters()
        )

    def test_moving_the_spectrometer_is_refused(self):
        """
        A control that accepted a value and ignored it bit this project once.

        ``probe_position`` accepted, echoed back and was silently dropped;
        an operator driving this dial gets a sentence instead.
        """
        instrument = ReplayInstrument(_recording())
        with pytest.raises(ReplayDataError, match="cannot be moved"):
            instrument.set_energy_offset_ev(200.0)

    def test_an_unusable_speed_is_refused(self, tmp_path):
        """Zero or negative speed describes no replay."""
        with pytest.raises(ValueError, match="speed must be positive"):
            build_replay_devices(tmp_path, speed=0.0)


class TestThePass:
    """One traversal, replayed, carrying what the instrument read out of it."""

    def test_the_spectra_are_the_recording_position_for_position(self):
        """
        The whole claim of a replay device, asserted directly.

        Values encode their own position, so a transpose, an off-by-one
        or a position written twice all fail here rather than passing a
        shape check.
        """
        scanner, _ = _devices()
        recording = _recording()
        result = scanner.scan_synchronised(
            recording.scan_parameters(), targets=[EELS_TARGET],
        )
        assert np.array_equal(
            result.spectra[EELS_TARGET].data, recording.spectra,
        )

    def test_the_energy_axis_is_the_recordings_own(self):
        """A spectrum acquired under an axis nobody checked shifts every edge."""
        scanner, _ = _devices()
        spectrum = scanner.scan_synchronised(
            _recording().scan_parameters(), targets=[EELS_TARGET],
        ).spectra[EELS_TARGET]
        assert spectrum.energy_offset_ev == pytest.approx(140.0)
        assert spectrum.energy_scale_ev == pytest.approx(0.4)

    def test_the_image_channel_comes_from_the_same_traversal(self):
        """
        The reason this session is worth replaying at all.

        The instrument read the HAADF channel out during the spectrum
        image's own acquisition, so the pass asserts a correlation that
        is a historical fact rather than a property of this adapter.
        """
        scanner, _ = _devices()
        recording = _recording()
        result = scanner.scan_synchronised(
            recording.scan_parameters(), channels=[0], targets=[EELS_TARGET],
        )
        assert np.array_equal(result.images[0].data, recording.image)
        assert result.images[0].metadata["scan_pass_id"] == result.pass_id

    def test_it_reports_which_device_was_master(self):
        """
        GMS drove the spectrometer and the scan followed.

        An unsynchronised acquisition has the same shape as a
        synchronised one, so only this field distinguishes them
        afterwards.
        """
        scanner, _ = _devices()
        result = scanner.scan_synchronised(
            _recording().scan_parameters(), targets=[EELS_TARGET],
        )
        assert result.scan_sync == SCAN_SYNC_DETECTOR

    def test_a_preallocated_destination_is_filled_in_place(self):
        """
        The pass views the caller's memory rather than a copy of it.

        What lets a spectrum image be written straight into an HDF5
        dataset as it is acquired.
        """
        scanner, _ = _devices()
        destination = np.zeros((_HEIGHT, _WIDTH, _CHANNELS), dtype=np.float32)
        result = scanner.scan_synchronised(
            _recording().scan_parameters(),
            targets=[EELS_TARGET],
            into={EELS_TARGET: destination},
        )
        assert result.spectra[EELS_TARGET].data is destination
        assert destination.any()

    def test_a_destination_of_the_wrong_shape_is_refused(self):
        """A caller allocating gigabytes is told which number is wrong."""
        scanner, _ = _devices()
        with pytest.raises(ValueError, match="beam positions"):
            scanner.scan_synchronised(
                _recording().scan_parameters(),
                targets=[EELS_TARGET],
                into={EELS_TARGET: np.zeros((_HEIGHT, _WIDTH, 3))},
            )

    def test_a_scanner_with_no_image_reports_no_channels(self):
        """
        Claiming a channel that hands back nothing is worse than none.

        A set with no during-SI image is real: this session has one.
        """
        scanner, _ = _devices(image=None)
        assert list(scanner.channel_names) == []


class TestHonesty:
    """Replayed data must never be mistakable for data acquired now."""

    def test_every_spectrum_names_the_backend_and_the_file(self):
        """
        By the time anyone reads a log, the session has already happened.

        So the marking is on the object, which is what reaches storage.
        """
        scanner, _ = _devices()
        metadata = scanner.scan_synchronised(
            _recording().scan_parameters(), targets=[EELS_TARGET],
        ).spectra[EELS_TARGET].metadata
        assert metadata["backend"] == REPLAY_BACKEND
        assert metadata["recorded_label"] == "007_EELS-SI"
        assert "007_EELS-SI.dm3" in metadata["recorded_source"]

    def test_a_compressed_replay_says_so(self):
        """
        The dwell in the metadata is then not what anything waited.

        Recorded because a reader comparing the stored pixel time against
        the elapsed wall clock would otherwise find them inexplicably
        different.
        """
        scanner, _ = _devices()
        metadata = scanner.scan_synchronised(
            _recording().scan_parameters(), targets=[EELS_TARGET],
        ).spectra[EELS_TARGET].metadata
        assert metadata["replay_speed"] == _FAST

    def test_an_uncompressed_replay_claims_nothing(self):
        """
        Present only when time was compressed, as ``projected_by`` is.

        A key asserting "1x" about data that was never hurried is a claim
        nothing needed to make.
        """
        recording = _recording()
        spectrometer = ReplaySpectrometer(recording)
        scanner = ReplayScanner(recording, spectrometer)
        metadata = scanner.scan_synchronised(
            recording.scan_parameters(), targets=[EELS_TARGET],
        ).spectra[EELS_TARGET].metadata
        assert "replay_speed" not in metadata

    def test_the_instrument_reports_the_replay_backend(self):
        """The Instrument panel's top line is where an operator would see it."""
        assert ReplayInstrument(_recording()).describe()["backend"] == (
            REPLAY_BACKEND
        )

    def test_the_spectrometer_says_it_projected_on_the_sensor(self):
        """
        The sensor summed before readout, so this is one readout's noise.

        A hundred rows' readout noise is a different measurement, and
        nothing downstream could recover which it had been.
        """
        _, spectrometer = _devices()
        spectrometer.start()
        frame = spectrometer.acquire_frame()
        assert frame.metadata["readout"] == PROJECTED_READOUT
        assert frame.metadata["projected_by"] == "sensor"


class TestTiming:
    """The waiting is the point, not an affectation."""

    def test_a_pass_waits_the_recorded_dwell(self):
        """
        A device answering instantly cannot show what an acquisition costs.

        Only a lower bound is asserted: a machine can always be slower
        than expected, and a test that also demanded an upper bound would
        fail on a loaded CI runner while proving nothing extra.
        """
        speed = 20.0
        recording = _recording()
        scanner = ReplayScanner(
            recording, ReplaySpectrometer(recording, speed=speed), speed=speed,
        )
        expected = _HEIGHT * _WIDTH * _DWELL_S / speed

        started = time.monotonic()
        scanner.scan_synchronised(
            recording.scan_parameters(), targets=[EELS_TARGET],
        )
        assert time.monotonic() - started >= expected * 0.5

    def test_speed_scales_the_wait(self, monkeypatch):
        """
        Faster is really faster, rather than the argument being ignored.

        The waits are counted as the device asks for them rather than
        timed against a clock, because a clock cannot see this
        difference. Each :func:`time.sleep` costs something near half a
        millisecond whatever interval it is handed, and a pass of twelve
        beam positions makes twelve such calls: at 5x it asks for 4.8ms
        and takes about 7ms, at 200x it asks for 0.12ms and still takes
        about 6ms. The forty-fold difference the device really makes
        reaches the clock as a few hundred microseconds - inside the
        noise of a loaded machine, which duly reorders the two and fails
        a comparison that was only ever passing by luck.

        That the device waits at all is a claim about a real clock, and
        :meth:`test_a_pass_waits_the_recorded_dwell` above makes it
        against one. What is left for here is that ``speed`` divides the
        wait, and the requests answer that exactly.
        """
        recording = _recording()
        slow, fast = 5.0, 200.0
        requested = {}
        for speed in (slow, fast):
            waits: list[float] = []
            # replay looks ``sleep`` up on the time module at call time,
            # so patching it there catches every wait the pass makes,
            # wherever in the device it is asked for.
            monkeypatch.setattr(replay.time, "sleep", waits.append)
            scanner = ReplayScanner(
                recording,
                ReplaySpectrometer(recording, speed=speed),
                speed=speed,
            )
            scanner.scan_synchronised(
                recording.scan_parameters(), targets=[EELS_TARGET],
            )
            requested[speed] = sum(waits)
        # Guards the ratio below from holding vacuously between a pair of
        # passes that never waited at all.
        assert requested[fast] > 0
        assert requested[slow] == pytest.approx(
            requested[fast] * (fast / slow),
        )


class TestFindingRecordings:
    """Pairing files into acquisitions, without opening any of them."""

    @staticmethod
    def _session(tmp_path: pathlib.Path, *names: str) -> pathlib.Path:
        """
        Create an empty file per name and return the directory.

        Parameters
        ----------
        tmp_path : pathlib.Path
            The directory to fill.
        *names : str
            File names to create.

        Returns
        -------
        pathlib.Path
            The directory.
        """
        for name in names:
            (tmp_path / name).touch()
        return tmp_path

    def test_a_set_is_paired_by_its_index(self, tmp_path):
        """The operator's own numbering is what groups an acquisition."""
        session = self._session(
            tmp_path,
            "004_EELS-SI.dm3",
            "004_HAADF_(DS)_during-SI.dm3",
            "004_HAADF_(DS)_30nm_SI-survey.dm3",
        )
        found = find_recordings(session)["004"]
        assert found.spectrum_image.name == "004_EELS-SI.dm3"
        assert found.image.name == "004_HAADF_(DS)_during-SI.dm3"
        assert found.survey.name == "004_HAADF_(DS)_30nm_SI-survey.dm3"

    def test_the_operators_notes_do_not_break_the_match(self, tmp_path):
        """
        Real names carry what the operator was thinking at the time.

        A pattern demanding an exact tail would find nothing in this
        session, where the files are called things like
        ``013_EELS-SI_cone_tube_rotated.dm3``.
        """
        session = self._session(
            tmp_path,
            "013_EELS-SI_cone_tube_rotated.dm3",
            "013_HAADF_(DS)_during-SI_cone_tube_rotated.dm3",
        )
        assert find_recordings(session)["013"].image is not None

    def test_unrelated_images_are_left_alone(self, tmp_path):
        """
        A file that fits nothing is ignored rather than guessed at.

        Attaching an unrelated scan to a pass would claim the two shared
        probe positions, which is the one thing a pass exists to assert.
        """
        session = self._session(
            tmp_path,
            "004_EELS-SI.dm3",
            "004_BF_(DS)_30nm.dm3",
            "004_HAADF_(DS)_30nm_after-SI.dm3",
            "004_zlp.dm3",
        )
        found = find_recordings(session)["004"]
        assert found.image is None
        assert found.survey is None

    def test_an_acquisition_with_no_spectra_is_not_a_recording(self, tmp_path):
        """Most of a session's files belong to no spectrum image."""
        session = self._session(tmp_path, "001_HAADF_(SS)-836_500nm.dm3")
        assert find_recordings(session) == {}

    def test_a_duplicate_never_displaces_the_original(self, tmp_path):
        """
        A duplicate never displaces the original, whatever it is called.

        Sessions accumulate copies, and which one opens must not depend
        on how the filesystem happens to order them.

        The real session holds a "Copy of 005_EELS-SI.dm3", which never
        matches the index at all. The hazard this pins is the other
        shape: a suffixed copy, which sorts *before* the original on
        every machine, because a space is below a full stop.
        """
        session = self._session(
            tmp_path, "005_EELS-SI.dm3", "005_EELS-SI copy.dm3",
        )
        assert find_recordings(session)["005"].spectrum_image.name == (
            "005_EELS-SI.dm3"
        )

    def test_a_directory_that_is_not_one_is_refused(self, tmp_path):
        """Told apart from a directory that is simply empty of recordings."""
        with pytest.raises(ReplayDataError, match="not a directory"):
            find_recordings(tmp_path / "nowhere")

    def test_opening_an_empty_session_says_what_is_missing(self, tmp_path):
        """The message names the shape of filename it looked for."""
        with pytest.raises(ReplayDataError, match="no spectrum-image recording"):
            build_replay_devices(tmp_path)


class TestReadingVendorFiles:
    """
    The one part that needs a real file, skipped when there is none.

    Kept apart from everything above so that the device's behaviour is
    covered on every machine, and only the *reading* depends on having
    data. Point ``MIAINWOODPECKER_REPLAY_SESSION`` at a session directory
    to run it.
    """

    @staticmethod
    def _session() -> pathlib.Path:
        """
        Return the session directory to test against, or skip.

        Returns
        -------
        pathlib.Path
            A directory of DigitalMicrograph files.
        """
        import os  # noqa: PLC0415 - only this test consults the environment

        configured = os.environ.get("MIAINWOODPECKER_REPLAY_SESSION")
        if not configured:
            pytest.skip("set MIAINWOODPECKER_REPLAY_SESSION to a session directory")
        path = pathlib.Path(configured)
        if not path.is_dir():
            pytest.skip(f"{path} is not a directory")
        return path

    def test_a_recorded_session_opens_as_devices(self):
        """
        End to end against a real acquisition: read, pair, and drive.

        The assertions are deliberately about *structure* rather than
        values — which recording it is depends on whose data this is.
        """
        pytest.importorskip("rsciio", reason="requires the 'replay' extra")
        devices = build_replay_devices(self._session(), speed=1000.0)
        recording = devices.recording

        assert recording.channel_count > 1
        assert recording.energy_scale_ev > 0
        # Energy last, whatever order the vendor stored it in.
        assert recording.spectra.shape[-1] == recording.channel_count
        assert devices.scanner.native_parameters().shape == (
            recording.navigation_shape
        )

    def test_a_recorded_pass_carries_its_own_geometry(self):
        """The pass acquires the grid the probe actually visited."""
        pytest.importorskip("rsciio", reason="requires the 'replay' extra")
        devices = build_replay_devices(self._session(), speed=1000.0)
        parameters = devices.scanner.native_parameters()

        result = devices.scanner.scan_synchronised(
            parameters,
            channels=list(range(len(devices.scanner.channel_names))),
            targets=[EELS_TARGET],
        )
        spectra = result.spectra[EELS_TARGET]
        assert spectra.navigation_shape == parameters.shape
        assert np.array_equal(spectra.data, devices.recording.spectra)
        assert result.scan_sync == SCAN_SYNC_DETECTOR
