"""Tests for the acquisition sequence generators."""

import datetime
import itertools
import typing

import numpy as np
import pytest

from miainwoodpecker.acquisition import camera_series, focal_series, record, scan_series
from miainwoodpecker.devices import DEFOCUS_CONTROL, Frame, ScanParameters
from miainwoodpecker.storage import read_series

_PARAMETERS = ScanParameters(height=4, width=4, pixel_time_us=1.0, fov_nm=10.0)


class _RecordingScanner:
    """Scanner that records the parameters of every scan it is asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[ScanParameters, int]] = []

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

    def close(self) -> None:
        """Release nothing; the fake owns no resources."""


class _CountingCamera:
    """Camera that tracks start/stop calls so lifecycle can be asserted."""

    def __init__(self) -> None:
        self.running = False
        self.start_count = 0
        self.stop_count = 0

    @property
    def camera_id(self) -> str:
        """Return the fake camera's id."""
        return "counting_camera"

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
