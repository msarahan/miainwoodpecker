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

import pathlib

import pytest

pytest.importorskip("nion.usim_device", reason="requires the 'device' extra")

from miainwoodpecker.devices import Camera, ScanParameters, Scanner
from miainwoodpecker.devices.nion_server import _SHARED_MEMORY_THRESHOLD_BYTES
from miainwoodpecker.devices.remote import remote_simulated_instrument

_DEV_SHM = pathlib.Path("/dev/shm")  # noqa: S108 - inspected read-only, never written to
_LARGE_SIZE = 1536  # 1536x1536 float64 = ~18.9MB, comfortably above threshold


def _shm_names() -> set[str]:
    """Return this-process-visible /dev/shm entry names, or an empty set."""
    if not _DEV_SHM.is_dir():
        return set()
    return {entry.name for entry in _DEV_SHM.iterdir()}


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
    parameters = ScanParameters(
        height=_LARGE_SIZE,
        width=_LARGE_SIZE,
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


def test_repeated_large_frames_reuse_the_same_segment(microscope):
    """
    Two same-shape large frames in a row reuse one segment, not two.

    This is the whole point of the ring-buffer redesign: paying the
    segment create/destroy cost once instead of per frame. White-box by
    necessity - the reused name is an implementation detail the public
    Camera/Scanner API deliberately does not expose.
    """
    scanner = microscope.scanner
    parameters = ScanParameters(
        height=_LARGE_SIZE,
        width=_LARGE_SIZE,
        pixel_time_us=1.0,
        fov_nm=microscope.stage_size_nm * 0.1,
    )
    scanner.scan_frame(parameters, channel=0)
    first_name = scanner._reader._segment.name  # noqa: SLF001
    scanner.scan_frame(parameters, channel=0)
    second_name = scanner._reader._segment.name  # noqa: SLF001
    assert first_name == second_name


def test_resize_creates_a_new_segment_and_frees_the_old_one(microscope):
    """A shape change forces a new segment, and the old one is actually gone."""
    scanner = microscope.scanner
    small_large = ScanParameters(
        height=_LARGE_SIZE,
        width=_LARGE_SIZE,
        pixel_time_us=1.0,
        fov_nm=microscope.stage_size_nm * 0.1,
    )
    bigger = ScanParameters(
        height=_LARGE_SIZE + 256,
        width=_LARGE_SIZE + 256,
        pixel_time_us=1.0,
        fov_nm=microscope.stage_size_nm * 0.1,
    )
    scanner.scan_frame(small_large, channel=0)
    first_name = scanner._reader._segment.name  # noqa: SLF001
    scanner.scan_frame(bigger, channel=0)
    second_name = scanner._reader._segment.name  # noqa: SLF001
    assert first_name != second_name
    if _DEV_SHM.is_dir():
        assert first_name not in _shm_names()
    # Leave the target back at a size later tests in this module expect.
    scanner.scan_frame(small_large, channel=0)


def test_no_shared_memory_segments_leak_after_teardown():
    """
    Every segment created during a session is gone once it ends.

    Uses a fresh instrument rather than the module fixture, since the
    check is only meaningful across a full spawn-to-teardown lifecycle.
    """
    before = _shm_names()
    with remote_simulated_instrument() as instrument:
        parameters = ScanParameters(
            height=_LARGE_SIZE,
            width=_LARGE_SIZE,
            pixel_time_us=1.0,
            fov_nm=instrument.stage_size_nm * 0.1,
        )
        instrument.scanner.scan_frame(parameters, channel=0)
        instrument.ronchigram_camera.start()
        try:
            instrument.ronchigram_camera.acquire_frame()
        finally:
            instrument.ronchigram_camera.stop()
        instrument.eels_camera.start()
        try:
            instrument.eels_camera.acquire_frame()
        finally:
            instrument.eels_camera.stop()
    after = _shm_names()
    assert after == before
