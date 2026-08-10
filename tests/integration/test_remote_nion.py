"""
Integration tests: the Nion device server over the actual IPC boundary.

Unlike ``tests/integration/test_nion_server.py`` (which imports the
GPL-3.0 server module directly, in-process), these tests drive it the
same way the shipped application does: through
:mod:`miainwoodpecker.devices.remote`, which imports nothing from
``nion.*`` and spawns the server as a subprocess (see
docs/migration-plan.md, §6, for why).

A module-scoped fixture spawns one subprocess for the whole file rather
than one per test, since subprocess startup (~1-2s to launch and connect
four Listeners) dominates test time otherwise and none of these
operations are mutually interfering.

Skipped automatically unless the ``device`` optional dependency group is
installed.
"""

import pytest

pytest.importorskip("nion.usim_device", reason="requires the 'device' extra")

from miainwoodpecker.devices import Camera, ScanParameters, Scanner
from miainwoodpecker.devices.nion_server import _SHARED_MEMORY_THRESHOLD_BYTES
from miainwoodpecker.devices.remote import remote_simulated_instrument


@pytest.fixture(scope="module")
def microscope():
    """Spawn one device server subprocess for every test in this module."""
    with remote_simulated_instrument() as instrument:
        yield instrument


def test_remote_devices_satisfy_the_protocols(microscope):
    """RemoteCamera/RemoteScanner are recognized by the runtime-checkable protocols."""
    assert isinstance(microscope.ronchigram_camera, Camera)
    assert isinstance(microscope.eels_camera, Camera)
    assert isinstance(microscope.scanner, Scanner)


def test_camera_round_trip_over_ipc(microscope):
    """
    Start/acquire/stop over IPC for the simulated Ronchigram camera.

    A default Ronchigram frame (2048x2048 float32, ~16.8MB) is above the
    shared-memory threshold, so this exercises that transport path.
    """
    camera = microscope.ronchigram_camera
    assert camera.camera_id == "usim_ronchigram_camera"
    camera.start()
    try:
        frame = camera.acquire_frame()
    finally:
        camera.stop()
    expected_ndim = 2
    assert frame.data.ndim == expected_ndim
    assert frame.data.nbytes >= _SHARED_MEMORY_THRESHOLD_BYTES
    assert frame.timestamp.tzinfo is not None
    assert frame.metadata["frame_number"] >= 1


def test_small_scan_uses_the_pickle_path_over_ipc(microscope):
    """A scan frame below the shared-memory threshold round-trips correctly."""
    scanner = microscope.scanner
    parameters = ScanParameters(
        height=32,
        width=48,
        pixel_time_us=1.0,
        fov_nm=microscope.stage_size_nm * 0.1,
    )
    frame = scanner.scan_frame(parameters, channel=0)
    assert frame.data.nbytes < _SHARED_MEMORY_THRESHOLD_BYTES
    assert frame.data.shape == parameters.shape
    assert frame.metadata["channel_name"] == "HAADF"
    assert frame.metadata["fov_nm"] == parameters.fov_nm


def test_large_scan_uses_the_shared_memory_path_over_ipc(microscope):
    """A scan frame above the shared-memory threshold round-trips correctly."""
    scanner = microscope.scanner
    size = 1536  # 1536x1536 float64 = ~18.9MB, above the 8MB threshold
    parameters = ScanParameters(
        height=size,
        width=size,
        pixel_time_us=1.0,
        fov_nm=microscope.stage_size_nm * 0.1,
    )
    frame = scanner.scan_frame(parameters, channel=1)
    assert frame.data.nbytes >= _SHARED_MEMORY_THRESHOLD_BYTES
    assert frame.data.shape == parameters.shape
    assert frame.metadata["channel_name"] == "MAADF"


def test_channel_names_over_ipc(microscope):
    """channel_names is a property, not a method, and must still round-trip."""
    assert list(microscope.scanner.channel_names) == [
        "HAADF", "MAADF", "X1", "X2",
    ]
