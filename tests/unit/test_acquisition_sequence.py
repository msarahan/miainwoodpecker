"""Tests for the acquisition sequence generators."""

import datetime
import itertools
import typing

import numpy as np
import pytest

from miainwoodpecker.acquisition import camera_series, focal_series, record, scan_series
from miainwoodpecker.devices import Frame, ScanParameters
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
