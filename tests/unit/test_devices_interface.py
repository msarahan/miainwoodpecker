"""Tests for the vendor-neutral device interfaces."""

import datetime
import typing

import numpy as np

from miainwoodpecker.devices import Camera, Frame, ScanParameters, Scanner


class _FakeCamera:
    """Minimal in-memory implementation used to exercise the Camera protocol."""

    def __init__(self) -> None:
        self.running = False
        self.closed = False

    @property
    def camera_id(self) -> str:
        """Return the fake camera's id."""
        return "fake_camera"

    def start(self) -> None:
        """Mark acquisition as running."""
        self.running = True

    def stop(self) -> None:
        """Mark acquisition as paused."""
        self.running = False

    def acquire_frame(self) -> Frame:
        """Return a constant 4x4 frame."""
        return Frame(
            data=np.zeros((4, 4), dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={"frame_number": 1},
        )

    def close(self) -> None:
        """Mark the device as closed."""
        self.closed = True


class _FakeScanner:
    """Minimal in-memory implementation used to exercise the Scanner protocol."""

    @property
    def scanner_id(self) -> str:
        """Return the fake scanner's id."""
        return "fake_scanner"

    @property
    def channel_names(self) -> typing.Sequence[str]:
        """Return the fake channel names."""
        return ["HAADF", "MAADF"]

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Return a zero-filled frame of the requested shape."""
        return Frame(
            data=np.zeros(parameters.shape, dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={"channel_index": channel},
        )

    def close(self) -> None:
        """Release nothing; the fake owns no resources."""


def test_fake_camera_satisfies_camera_protocol():
    """A structural implementation is recognized by the runtime-checkable protocol."""
    assert isinstance(_FakeCamera(), Camera)


def test_fake_scanner_satisfies_scanner_protocol():
    """A structural implementation is recognized by the runtime-checkable protocol."""
    assert isinstance(_FakeScanner(), Scanner)


def test_scan_parameters_shape_is_height_then_width():
    """The shape property follows the numpy (rows, columns) convention."""
    parameters = ScanParameters(height=32, width=48, pixel_time_us=1.0, fov_nm=100.0)
    expected_shape = (32, 48)
    assert parameters.shape == expected_shape


def test_frame_metadata_defaults_to_empty_mapping():
    """A Frame constructed without metadata has an empty mapping, not None."""
    frame = Frame(
        data=np.zeros((2, 2), dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
    )
    assert frame.metadata == {}


def test_scan_frame_uses_requested_shape():
    """The fake scanner honors ScanParameters.shape, pinning the convention."""
    parameters = ScanParameters(height=8, width=16, pixel_time_us=1.0, fov_nm=50.0)
    frame = _FakeScanner().scan_frame(parameters)
    assert frame.data.shape == parameters.shape
