"""Tests for the acquisition sequence generators."""

import datetime
import itertools
import json
import typing

import h5py
import numpy as np
import pytest

from miainwoodpecker.acquisition import (
    camera_series,
    energy_offset_series,
    focal_series,
    multichannel_scan_series,
    record,
    scan_series,
)
from miainwoodpecker.devices import (
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    CameraParameters,
    Frame,
    ScanParameters,
)
from miainwoodpecker.storage import read_series, read_spectra

_PARAMETERS = ScanParameters(height=4, width=4, pixel_time_us=1.0, fov_nm=10.0)
_CHANNELS = 16
_DISPERSION_EV = 0.5
_ZERO_LOSS_OFFSET_EV = -100.0


class _RecordingScanner:
    """Scanner that records the parameters of every scan it is asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[ScanParameters, int]] = []
        self.pass_calls: list[tuple[ScanParameters, tuple[int, ...]]] = []

    @property
    def scanner_id(self) -> str:
        """Return the fake scanner's id."""
        return "recording_scanner"

    @property
    def channel_names(self) -> typing.Sequence[str]:
        """Return the fake channel names."""
        return ["HAADF", "MAADF"]

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Record the request and return a frame tagged with its sequence number."""
        self.calls.append((parameters, channel))
        return Frame(
            data=np.full(parameters.shape, len(self.calls), dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={"fov_nm": parameters.fov_nm, "channel_index": channel},
        )

    def scan_frames(
        self,
        parameters: ScanParameters,
        channels: typing.Sequence[int],
    ) -> list[Frame]:
        """
        Record one pass and return its frames, sharing a per-pass identity.

        The identity is the pass's ordinal in this fake — stable, unique
        per call, and visibly not per frame, which is what the series
        tests need to tell one pass's frames from the next's.

        Parameters
        ----------
        parameters : ScanParameters
            Scan geometry, shared by every returned frame.
        channels : typing.Sequence[int]
            Channel indices to read out during the fake pass.

        Returns
        -------
        list[Frame]
            One frame per requested channel, in request order.
        """
        self.pass_calls.append((parameters, tuple(channels)))
        pass_id = f"pass-{len(self.pass_calls)}"
        timestamp = datetime.datetime.now(tz=datetime.UTC)
        return [
            Frame(
                data=np.full(parameters.shape, len(self.pass_calls), dtype=np.float32),
                timestamp=timestamp,
                metadata={
                    "channel_index": channel,
                    "scan_pass_id": pass_id,
                    "simultaneous_channels": list(channels),
                },
            )
            for channel in channels
        ]

    def close(self) -> None:
        """Release nothing; the fake owns no resources."""


class _CountingCamera:
    """Camera that tracks start/stop calls so lifecycle can be asserted."""

    def __init__(self) -> None:
        self.running = False
        self.start_count = 0
        self.stop_count = 0
        self._parameters = CameraParameters(exposure_ms=10.0)

    @property
    def camera_id(self) -> str:
        """Return the fake camera's id."""
        return "counting_camera"

    @property
    def binning_values(self) -> tuple[int, ...]:
        """Return the binning factors this fake supports."""
        return (1,)

    def parameters(self) -> CameraParameters:
        """Return the settings the next frame would use."""
        return self._parameters

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Record new settings and report them back.

        Parameters
        ----------
        parameters : CameraParameters
            The requested exposure and binning.

        Returns
        -------
        CameraParameters
            The same settings; this fake rounds nothing.
        """
        self._parameters = parameters
        return self._parameters

    def start(self) -> None:
        """Mark acquisition as running."""
        self.running = True
        self.start_count += 1

    def stop(self) -> None:
        """Mark acquisition as paused."""
        self.running = False
        self.stop_count += 1

    def acquire_frame(self) -> Frame:
        """Return a constant frame."""
        return Frame(
            data=np.ones((2, 2), dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
        )

    def close(self) -> None:
        """Release nothing; the fake owns no resources."""


class _RecordingInstrument:
    """InstrumentController that records every defocus it was asked for."""

    def __init__(self, *, controls: typing.Sequence[str] = (DEFOCUS_CONTROL,)) -> None:
        self._controls = list(controls)
        self.defocus = 500.0
        self.requested: list[float] = []
        self.energy_offset = -20.0
        self.requested_offsets: list[float] = []

    def stage_size_nm(self) -> float:
        """Return a fake stage extent."""
        return 1000.0

    def available_controls(self) -> typing.Sequence[str]:
        """Return the controls this fake claims to implement."""
        return self._controls

    def stage_position_nm(self) -> tuple[float, float]:
        """Return a fixed position; this fake only models defocus."""
        return (0.0, 0.0)

    def set_stage_position_nm(self, y_nm: float, x_nm: float) -> None:
        """Ignore stage moves; this fake only models defocus."""

    def defocus_nm(self) -> float:
        """Return the current defocus."""
        return self.defocus

    def set_defocus_nm(self, defocus_nm: float) -> None:
        """
        Record and apply a defocus change, landing 0.5nm off the setpoint.

        Deliberately *not* exactly what was requested: a real instrument
        lands near a setpoint, not on it, and ``focal_series`` is supposed
        to record what it read back rather than what it asked for.

        Parameters
        ----------
        defocus_nm : float
            Requested defocus, in nanometres.
        """
        self.requested.append(defocus_nm)
        self.defocus = defocus_nm + 0.5

    def energy_offset_ev(self) -> float:
        """Return the current energy offset."""
        return self.energy_offset

    def set_energy_offset_ev(self, offset_ev: float) -> None:
        """
        Record and apply an energy-offset change, landing 0.25eV off.

        Off the setpoint for the same reason ``set_defocus_nm`` is:
        ``energy_offset_series`` must record what it read back, not what
        it asked for.

        Parameters
        ----------
        offset_ev : float
            Requested offset, in electronvolts.
        """
        self.requested_offsets.append(offset_ev)
        self.energy_offset = offset_ev + 0.25

    def is_beam_blanked(self) -> bool:
        """Return a fixed unblanked state."""
        return False

    def set_beam_blanked(self, *, blanked: bool) -> None:
        """Ignore blanker changes; this fake only models defocus."""

    def park(self) -> None:
        """Do nothing; this fake has no blanker."""


def test_scan_series_yields_requested_count_on_the_chosen_channel():
    """Every frame uses the given parameters and channel."""
    scanner = _RecordingScanner()
    frames = list(scan_series(scanner, _PARAMETERS, 3, channel=1))
    expected = 3
    assert len(frames) == expected
    assert len(scanner.calls) == expected
    assert all(channel == 1 for _, channel in scanner.calls)
    assert all(frame.data.shape == _PARAMETERS.shape for frame in frames)


def test_scan_series_is_lazy():
    """Nothing is acquired until the generator is consumed."""
    scanner = _RecordingScanner()
    series = scan_series(scanner, _PARAMETERS, 100)
    assert scanner.calls == []
    next(series)
    assert len(scanner.calls) == 1


def test_multichannel_series_asks_the_scanner_once_per_pass():
    """
    The dose accounting the multi-channel call exists for, at this layer.

    Two channels over three passes must be three ``scan_frames`` calls
    and zero ``scan_frame`` calls — a series that quietly looped the
    single-channel call would double the dose and break the shared
    identity while yielding the same number of frames, which is exactly
    the failure mode that must not be reachable by accident.
    """
    scanner = _RecordingScanner()
    frames = list(
        multichannel_scan_series(scanner, _PARAMETERS, 3, channels=(0, 1)),
    )
    expected_frames = 6  # 3 passes x 2 channels
    expected_passes = 3
    assert len(frames) == expected_frames
    assert len(scanner.pass_calls) == expected_passes
    assert scanner.calls == []  # never the single-channel path
    assert all(channels == (0, 1) for _, channels in scanner.pass_calls)


def test_multichannel_series_frames_group_by_pass_identity():
    """
    Consecutive frames pair up under one pass id; the next pass gets a new one.

    The yield order is the storage order, so this grouping is what a
    recorded file will say about which frames shared a pass.
    """
    frames = list(
        multichannel_scan_series(_RecordingScanner(), _PARAMETERS, 2, channels=(0, 1)),
    )
    ids = [frame.metadata["scan_pass_id"] for frame in frames]
    assert ids[0] == ids[1]
    assert ids[2] == ids[3]
    assert ids[0] != ids[2]
    assert [frame.metadata["channel_index"] for frame in frames] == [0, 1, 0, 1]


def test_multichannel_series_is_lazy():
    """Nothing is acquired until the generator is consumed, like scan_series."""
    scanner = _RecordingScanner()
    series = multichannel_scan_series(scanner, _PARAMETERS, 100, channels=(0, 1))
    assert scanner.pass_calls == []
    next(series)
    assert len(scanner.pass_calls) == 1


def test_multichannel_series_rejects_a_negative_count():
    """A negative count is a programming error, matching the other series."""
    with pytest.raises(ValueError, match="non-negative"):
        list(
            multichannel_scan_series(
                _RecordingScanner(),
                _PARAMETERS,
                -1,
                channels=(0, 1),
            ),
        )


def test_recording_a_multichannel_series_stores_the_pass_identity(tmp_path):
    """
    The shared identity survives the trip to disk, per frame.

    ``record`` streams into ``NexusWriter``, which keeps every frame's
    whole metadata mapping as JSON — so nothing multi-channel-specific
    was added to the storage layer, and this test is what proves nothing
    needed to be: a reader of the file can reconstruct which frames
    shared a pass, which channels they were, and in what order, from the
    per-frame metadata alone.
    """
    path = tmp_path / "multichannel.nxs"
    written = record(
        multichannel_scan_series(
            _RecordingScanner(),
            _PARAMETERS,
            2,
            channels=(0, 1),
        ),
        path,
        title="simultaneous pair",
    )
    expected = 4
    assert written == expected
    with h5py.File(path, "r") as handle:
        stored = [
            json.loads(entry)
            for entry in handle["entry/metadata/frame_metadata_json"][()]
        ]
    assert [item["channel_index"] for item in stored] == [0, 1, 0, 1]
    assert stored[0]["scan_pass_id"] == stored[1]["scan_pass_id"]
    assert stored[2]["scan_pass_id"] == stored[3]["scan_pass_id"]
    assert stored[0]["scan_pass_id"] != stored[2]["scan_pass_id"]
    assert all(item["simultaneous_channels"] == [0, 1] for item in stored)


def test_camera_series_starts_and_always_stops():
    """The camera is started once and stopped once for a full series."""
    camera = _CountingCamera()
    frames = list(camera_series(camera, 3))
    expected = 3
    assert len(frames) == expected
    assert camera.start_count == 1
    assert camera.stop_count == 1
    assert not camera.running


def test_camera_series_stops_when_abandoned_early():
    """Abandoning the generator still releases the camera."""
    camera = _CountingCamera()
    series = camera_series(camera, 100)
    taken = list(itertools.islice(series, 2))
    expected = 2
    assert len(taken) == expected
    series.close()  # what `break` out of a for-loop does at GC time
    assert camera.stop_count == 1
    assert not camera.running


def test_focal_series_sweeps_field_of_view():
    """Each step replaces fov_nm while keeping the other scan settings."""
    scanner = _RecordingScanner()
    values = [5.0, 10.0, 20.0]
    frames = list(focal_series(scanner, _PARAMETERS, values))
    assert [call.fov_nm for call, _ in scanner.calls] == values
    assert [frame.metadata["fov_nm"] for frame in frames] == values
    # Unrelated settings are untouched.
    assert all(
        call.pixel_time_us == _PARAMETERS.pixel_time_us for call, _ in scanner.calls
    )
    assert all(call.height == _PARAMETERS.height for call, _ in scanner.calls)


def test_focal_series_sweeps_real_defocus_when_given_an_instrument():
    """With an instrument, the swept values are defocus, not field of view."""
    scanner = _RecordingScanner()
    instrument = _RecordingInstrument()
    values = [100.0, 200.0, 300.0]
    frames = list(focal_series(scanner, _PARAMETERS, values, instrument=instrument))
    assert len(frames) == len(values)
    # The sweep drove defocus...
    assert instrument.requested[: len(values)] == values
    # ...and left the field of view alone, unlike the fov-sweeping mode.
    assert all(call.fov_nm == _PARAMETERS.fov_nm for call, _ in scanner.calls)


def test_focal_series_records_read_back_defocus_not_the_request():
    """Metadata says what the instrument did, and also what it was asked for."""
    instrument = _RecordingInstrument()
    frames = list(
        focal_series(_RecordingScanner(), _PARAMETERS, [100.0], instrument=instrument),
    )
    expected_request = 100.0
    expected_read_back = 100.5  # the fake's 0.5nm settling error
    assert frames[0].metadata["requested_defocus_nm"] == expected_request
    assert frames[0].metadata["defocus_nm"] == expected_read_back


def test_focal_series_restores_defocus_even_when_abandoned_early():
    """An interrupted sweep leaves the instrument where it found it."""
    instrument = _RecordingInstrument()
    original = instrument.defocus_nm()
    series = focal_series(
        _RecordingScanner(),
        _PARAMETERS,
        [100.0, 200.0, 300.0],
        instrument=instrument,
    )
    taken = list(itertools.islice(series, 1))
    assert len(taken) == 1
    series.close()  # what `break` out of a for-loop does at GC time
    assert instrument.requested[-1] == original


def test_focal_series_without_an_instrument_still_sweeps_field_of_view():
    """The historical mode is untouched: no instrument means no defocus metadata."""
    scanner = _RecordingScanner()
    frames = list(focal_series(scanner, _PARAMETERS, [5.0, 10.0]))
    assert [call.fov_nm for call, _ in scanner.calls] == [5.0, 10.0]
    assert all("defocus_nm" not in frame.metadata for frame in frames)


def test_focal_series_rejects_an_instrument_without_defocus():
    """Sweeping a control the instrument does not have is a programming error."""
    instrument = _RecordingInstrument(controls=())
    with pytest.raises(ValueError, match="defocus"):
        list(
            focal_series(
                _RecordingScanner(),
                _PARAMETERS,
                [100.0],
                instrument=instrument,
            ),
        )


def test_negative_count_is_rejected():
    """A negative count is a programming error, not an empty series."""
    scanner = _RecordingScanner()
    with pytest.raises(ValueError, match="non-negative"):
        list(scan_series(scanner, _PARAMETERS, -1))
    with pytest.raises(ValueError, match="non-negative"):
        list(camera_series(_CountingCamera(), -1))


def test_record_streams_a_series_to_disk(tmp_path):
    """record() persists a generator straight to a readable NeXus file."""
    scanner = _RecordingScanner()
    path = tmp_path / "series.nxs"
    written = record(scan_series(scanner, _PARAMETERS, 4), path, title="focal test")
    expected = 4
    assert written == expected
    recovered = list(read_series(path))
    assert len(recovered) == expected
    # Frames arrive in acquisition order: pixel value equals sequence number.
    assert [int(data.flat[0]) for data, _ in recovered] == [1, 2, 3, 4]


def _projected_frame(index: int) -> Frame:
    """
    Build one 1D frame of the kind a projected camera readout produces.

    Parameters
    ----------
    index : int
        Which frame in the series this is, written into the counts so the
        recording's order is checkable.

    Returns
    -------
    Frame
        A 1D frame with an energy calibration on its surviving axis.
    """
    return Frame(
        data=np.full(_CHANNELS, index, dtype="float32"),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={
            "device_id": "eels_camera",
            "camera_type": "eels",
            "frame_index": index,
            "calibration": {
                "x": {
                    "kind": "energy",
                    "scale": _DISPERSION_EV,
                    "offset": _ZERO_LOSS_OFFSET_EV,
                    "units": "eV",
                },
            },
        },
    )


def test_record_routes_projected_frames_into_the_spectrum_layout(tmp_path):
    """
    A 1D series lands in ``NXspectrum``, the same file shape EDX lands in.

    The seam this change exists to establish: ``NexusWriter`` refuses any
    rank but 2 by design, so a projected readout would otherwise have
    nowhere to go — and routing it into ``SpectrumWriter`` rather than
    teaching the frame writer a second layout is what lets it reach
    ``load_as_hyperspy_spectrum`` with no flattening.
    """
    path = tmp_path / "projected.nxs"
    written = record((_projected_frame(index) for index in range(3)), path)

    expected = 3
    assert written == expected
    recording = read_spectra(path)
    assert recording.data.shape == (expected, _CHANNELS)
    assert recording.energy_scale_ev == pytest.approx(_DISPERSION_EV)
    assert recording.energy_offset_ev == pytest.approx(_ZERO_LOSS_OFFSET_EV)
    # Acquisition order survives, so the dispatch did not reorder or
    # collapse anything on its way through the generator.
    assert [int(row[0]) for row in recording.data] == [0, 1, 2]


def test_record_still_writes_image_frames_the_way_it_always_did(tmp_path):
    """
    The dispatch is invisible to a 2D series, which is the compatibility claim.

    Asserted beside the projected case rather than trusted: the branch
    reads the first frame before opening any file, so this is what pins
    that an image series still reaches ``NexusWriter`` unchanged.
    """
    path = tmp_path / "images.nxs"
    written = record(scan_series(_RecordingScanner(), _PARAMETERS, 2), path)

    expected = 2
    assert written == expected
    assert len(list(read_series(path))) == expected


def test_recording_nothing_still_writes_the_empty_frame_file(tmp_path):
    """
    An empty series cannot say what it is, so it stays what it always was.

    A cancelled recording yields no frames, and the rank it *would* have
    produced is not knowable from an empty iterator — so this keeps the
    existing behaviour (a readable, frameless frame recording) rather
    than guessing a layout from the camera's configuration.
    """
    path = tmp_path / "empty.nxs"
    assert record([], path) == 0
    assert list(read_series(path)) == []


def test_a_projected_frame_with_no_energy_axis_is_refused_by_the_recorder(tmp_path):
    """
    A 1D frame that cannot say what its axis is does not get stored as a guess.

    The refusal comes from ``spectrum_from_projected_frame`` and reaches
    the caller unchanged, because "record it as something else" is the
    only alternative and every option is a fabricated axis.
    """
    frame = Frame(
        data=np.zeros(_CHANNELS, dtype="float32"),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={"device_id": "eels_camera"},
    )
    with pytest.raises(ValueError, match="cannot exist without its energy axis"):
        record([frame], tmp_path / "unstorable.nxs")


def test_energy_offset_series_steps_the_offset_and_records_the_read_back():
    """
    Each frame carries the offset the instrument reached, beside the request.

    The same discipline ``focal_series`` applies to defocus: a recording
    should say what the instrument did, not what it was asked to do. The
    fake lands 0.25eV off its setpoint so the two cannot be confused.
    """
    camera = _CountingCamera()
    instrument = _RecordingInstrument(controls=(ENERGY_OFFSET_CONTROL,))
    frames = list(
        energy_offset_series(camera, [-10.0, 0.0, 10.0], instrument=instrument),
    )

    expected_count = 3
    assert len(frames) == expected_count
    assert instrument.requested_offsets[:expected_count] == [-10.0, 0.0, 10.0]
    assert [frame.metadata["requested_energy_offset_ev"] for frame in frames] == [
        -10.0,
        0.0,
        10.0,
    ]
    assert [frame.metadata["energy_offset_ev"] for frame in frames] == [
        -9.75,
        0.25,
        10.25,
    ]


def test_energy_offset_series_restarts_the_camera_around_every_step():
    """
    The camera is stopped and started per step, not once for the series.

    Not incidental structure: a running camera returns a frame generated
    *before* the control changed, so a series acquired without the
    restart is mislabelled by one step throughout. Asserted on the
    start/stop counts because that is the only externally visible trace
    of it.
    """
    camera = _CountingCamera()
    instrument = _RecordingInstrument(controls=(ENERGY_OFFSET_CONTROL,))
    steps = 3
    list(energy_offset_series(camera, [-10.0, 0.0, 10.0], instrument=instrument))

    assert camera.start_count == steps
    assert camera.stop_count == steps
    assert not camera.running


def test_energy_offset_series_restores_the_offset_when_abandoned_early():
    """
    Abandoning the generator still puts the spectrometer back.

    The same guarantee ``focal_series`` gives for defocus, and it matters
    more here: an operator who interrupts a sweep should not be left with
    a spectrometer offset from wherever the sweep happened to stop.
    """
    camera = _CountingCamera()
    instrument = _RecordingInstrument(controls=(ENERGY_OFFSET_CONTROL,))
    original = instrument.energy_offset_ev()

    series = energy_offset_series(camera, [-10.0, 0.0, 10.0], instrument=instrument)
    next(series)
    series.close()  # what `break` out of a for-loop does at GC time

    assert instrument.requested_offsets[-1] == original
    assert not camera.running


def test_energy_offset_series_rejects_an_instrument_without_the_control():
    """
    An instrument with no spectrometer control says so rather than failing later.

    ``available_controls`` is the documented way to ask, and this project
    has already been bitten by a control that accepts a value and ignores
    it — so the check is on what the instrument *claims*, before any
    frame is acquired.
    """
    camera = _CountingCamera()
    instrument = _RecordingInstrument(controls=(DEFOCUS_CONTROL,))

    with pytest.raises(ValueError, match=ENERGY_OFFSET_CONTROL):
        list(energy_offset_series(camera, [0.0], instrument=instrument))
    assert camera.start_count == 0
