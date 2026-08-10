"""
Integration tests: the Nion adapter against the nionswift-usim simulator.

Skipped automatically unless the ``device`` optional dependency group is
installed (``uv run --extra device --extra tests pytest tests/integration``).
"""

import pytest

pytest.importorskip("nion.usim_device", reason="requires the 'device' extra")

from miainwoodpecker.devices import Camera, ScanParameters, Scanner
from miainwoodpecker.devices.nion_adapter import simulated_instrument


def test_simulated_devices_satisfy_the_protocols():
    """The adapted usim devices are recognized by the runtime-checkable protocols."""
    with simulated_instrument() as microscope:
        assert isinstance(microscope.ronchigram_camera, Camera)
        assert isinstance(microscope.eels_camera, Camera)
        assert isinstance(microscope.scanner, Scanner)


def test_camera_round_trip_produces_a_2d_frame():
    """Start/acquire/stop against the simulated Ronchigram camera."""
    with simulated_instrument() as microscope:
        camera = microscope.ronchigram_camera
        assert camera.camera_id == "usim_ronchigram_camera"
        camera.start()
        try:
            frame = camera.acquire_frame()
        finally:
            camera.stop()
        expected_ndim = 2
        assert frame.data.ndim == expected_ndim
        assert frame.timestamp.tzinfo is not None
        assert frame.metadata["frame_number"] >= 1


def test_scanner_honors_non_square_shape_and_reports_channel():
    """A non-square scan pins the (height, width) convention through the adapter."""
    with simulated_instrument() as microscope:
        scanner = microscope.scanner
        assert "HAADF" in scanner.channel_names
        parameters = ScanParameters(
            height=32,
            width=48,
            pixel_time_us=1.0,
            fov_nm=microscope.stage_size_nm * 0.1,
        )
        frame = scanner.scan_frame(parameters, channel=0)
        assert frame.data.shape == parameters.shape
        assert frame.metadata["channel_name"] == "HAADF"
        assert frame.metadata["fov_nm"] == parameters.fov_nm
