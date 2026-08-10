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
import signal
import subprocess
import time

import pytest

pytest.importorskip("nion.usim_device", reason="requires the 'device' extra")

from miainwoodpecker.acquisition import focal_series
from miainwoodpecker.devices import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    STAGE_POSITION_CONTROL,
    Camera,
    InstrumentController,
    ScanParameters,
    Scanner,
    remote,
)
from miainwoodpecker.devices.nion_server import _SHARED_MEMORY_THRESHOLD_BYTES
from miainwoodpecker.devices.remote import (
    HARDWARE_BACKEND,
    SIMULATED_BACKEND,
    _CONNECT_TIMEOUT_S,
    DeviceServerStartupError,
    RemoteInstrumentDevices,
    remote_instrument,
    remote_simulated_instrument,
)
from miainwoodpecker.devices.rpc import RemoteCallTimeoutError

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

    Asserts against the specific segment names this session used rather
    than comparing whole-directory ``/dev/shm`` snapshots before and
    after. A snapshot diff was order-dependently flaky under
    ``pytest-randomly``: ``/dev/shm`` is machine-global and also holds
    POSIX semaphores and segments created by anything else running in the
    same interpreter (napari/Qt thread pools, numpy, other test modules),
    so unrelated churn between the two snapshots failed a test that was
    only ever meant to check *our* segments. Naming them directly is both
    immune to that and a stricter statement of the actual property.
    """
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
        # Collected while the session is still alive: once it tears down,
        # the readers have detached and no longer know the names.
        used_names = {
            reader._segment.name  # noqa: SLF001
            for reader in (
                instrument.scanner._reader,  # noqa: SLF001
                instrument.ronchigram_camera._reader,  # noqa: SLF001
                instrument.eels_camera._reader,  # noqa: SLF001
            )
            if reader._segment is not None  # noqa: SLF001
        }

    # Guards against this test passing vacuously if the shared-memory
    # path was never actually taken (e.g. a threshold change).
    expected_segment_count = 3  # scanner + both cameras
    assert len(used_names) == expected_segment_count
    assert used_names.isdisjoint(_shm_names())


def _exercise_shared_memory(
    instrument: RemoteInstrumentDevices,
) -> set[str]:
    """
    Push a large frame through every device, then name the segments used.

    Returns the segment names while the session is still alive, since the
    readers detach on teardown and stop knowing them — the same technique
    ``test_no_shared_memory_segments_leak_after_teardown`` uses, factored
    out so the graceful-shutdown and wedged-server variants below assert
    the same property against the same three segments.

    Parameters
    ----------
    instrument : RemoteInstrumentDevices
        A live session.

    Returns
    -------
    set[str]
        The ``/dev/shm`` names the session's three readers are attached to.
    """
    parameters = ScanParameters(
        height=_LARGE_SIZE,
        width=_LARGE_SIZE,
        pixel_time_us=1.0,
        fov_nm=instrument.stage_size_nm * 0.1,
    )
    instrument.scanner.scan_frame(parameters, channel=0)
    for camera in (instrument.ronchigram_camera, instrument.eels_camera):
        camera.start()
        try:
            camera.acquire_frame()
        finally:
            camera.stop()
    return {
        reader._segment.name  # noqa: SLF001
        for reader in (
            instrument.scanner._reader,  # noqa: SLF001
            instrument.ronchigram_camera._reader,  # noqa: SLF001
            instrument.eels_camera._reader,  # noqa: SLF001
        )
        if reader._segment is not None  # noqa: SLF001
    }


# --------------------------------------------------- instrument controls


def test_remote_instrument_satisfies_the_controller_protocol(microscope):
    """RemoteInstrument is recognized by the runtime-checkable protocol."""
    assert isinstance(microscope.instrument, InstrumentController)


def test_describe_reports_the_backend_targets_and_controls(microscope):
    """
    The client learns what the server has before connecting to it.

    This is what lets a real instrument omit a camera the simulator
    happens to have: the client connects only to the reported targets.
    """
    description = microscope.instrument.describe()
    assert description["backend"] == SIMULATED_BACKEND
    assert list(description["targets"]) == [
        "ronchigram_camera",
        "eels_camera",
        "scanner",
    ]
    assert set(description["controls"]) == {
        STAGE_POSITION_CONTROL,
        DEFOCUS_CONTROL,
        BEAM_BLANKER_CONTROL,
    }
    assert description["stage_size_nm"] == microscope.stage_size_nm


def test_defocus_round_trips_over_ipc(microscope):
    """Setting defocus over IPC changes what a later read returns."""
    instrument = microscope.instrument
    original = instrument.defocus_nm()
    try:
        instrument.set_defocus_nm(original + 750.0)
        assert instrument.defocus_nm() == pytest.approx(original + 750.0)
    finally:
        instrument.set_defocus_nm(original)
    assert instrument.defocus_nm() == pytest.approx(original)


def test_stage_position_round_trips_over_ipc(microscope):
    """A (y, x) stage move survives the round trip in the right axis order."""
    instrument = microscope.instrument
    original = instrument.stage_position_nm()
    try:
        instrument.set_stage_position_nm(original[0] + 12.0, original[1] + 34.0)
        moved = instrument.stage_position_nm()
        assert moved == pytest.approx((original[0] + 12.0, original[1] + 34.0))
    finally:
        instrument.set_stage_position_nm(*original)


def test_beam_blanker_round_trips_over_ipc(microscope):
    """The blanker's keyword-only setter crosses the RPC boundary as a kwarg."""
    instrument = microscope.instrument
    assert not instrument.is_beam_blanked()
    try:
        instrument.set_beam_blanked(blanked=True)
        assert instrument.is_beam_blanked()
    finally:
        instrument.set_beam_blanked(blanked=False)
    assert not instrument.is_beam_blanked()


def test_focal_series_sweeps_real_defocus_over_ipc(microscope):
    """
    A defocus sweep over the real device server produces the expected frames.

    Note what this does *not* claim: usim's scan generator does not model
    defocus at all (measured with
    ``scripts/device_control_verification.py`` - a 2500nm defocus change
    moves scan data by 1.00x the shot-noise floor, versus 6.2x for the
    Ronchigram camera), so the frames here differ only by noise. What is
    verified is that the sweep drives the real control, records the
    read-back value per frame, and restores the instrument afterwards.
    """
    instrument = microscope.instrument
    parameters = ScanParameters(
        height=32,
        width=32,
        pixel_time_us=1.0,
        fov_nm=microscope.stage_size_nm * 0.1,
    )
    original = instrument.defocus_nm()
    values = [original + 100.0, original + 200.0, original + 300.0]
    frames = list(
        focal_series(microscope.scanner, parameters, values, instrument=instrument),
    )
    assert len(frames) == len(values)
    assert [frame.metadata["requested_defocus_nm"] for frame in frames] == values
    assert [frame.metadata["defocus_nm"] for frame in frames] == pytest.approx(values)
    assert all(frame.data.shape == parameters.shape for frame in frames)
    # A defocus sweep leaves the field of view alone.
    assert all(frame.metadata["fov_nm"] == parameters.fov_nm for frame in frames)
    assert instrument.defocus_nm() == pytest.approx(original)


# ----------------------------------------------------- graceful shutdown


def test_graceful_shutdown_parks_and_releases_every_device():
    """
    A healthy server stops the cameras, blanks the beam, and releases devices.

    Uses its own session: the handshake ends that server's life, so it
    cannot share the module fixture.
    """
    with remote_instrument() as instrument:
        report = instrument.instrument.shutdown()
    assert report["backend"] == SIMULATED_BACKEND
    assert set(report["cameras_stopped"]) == {"ronchigram_camera", "eels_camera"}
    assert report["beam_blanked"] is True
    assert set(report["devices_released"]) == {
        "ronchigram_camera",
        "eels_camera",
        "scanner",
    }
    assert report["errors"] == []


def test_an_explicit_shutdown_leaves_teardown_nothing_to_kill(monkeypatch):
    """
    Calling the handshake yourself makes teardown a no-op, not a second SIGTERM.

    Acknowledging a shutdown ends the server's life — it closes that
    connection and exits — so the handshake is deliberately *not*
    re-callable over the same connection. What teardown must do instead is
    notice the process has already gone. Asserted by exit status: a clean
    ``0`` rather than the ``-SIGTERM`` a redundant kill would leave.
    """
    spawned = []
    spawn_server = remote._spawn_server  # noqa: SLF001

    def capturing_spawn(
        *args: object,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        process = spawn_server(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(remote, "_spawn_server", capturing_spawn)
    with remote_instrument() as instrument:
        instrument.instrument.shutdown()
    assert spawned[0].returncode == 0


def test_graceful_shutdown_leaves_no_shared_memory_segments():
    """
    The handshake retires the reused segments, which SIGTERM alone cannot.

    Named POSIX segments outlive the process that created them, so this is
    the property the whole shutdown ordering exists to preserve.
    """
    with remote_instrument() as instrument:
        used_names = _exercise_shared_memory(instrument)
        report = instrument.instrument.shutdown()
    expected_segment_count = 3  # scanner + both cameras
    assert len(used_names) == expected_segment_count
    assert report["errors"] == []
    assert used_names.isdisjoint(_shm_names())


def test_hardware_backend_over_ipc_fails_fast_and_says_why():
    """
    Asking for hardware with none attached fails promptly, not after a timeout.

    The server prints what it looked for and exits; the client notices the
    exit instead of retrying until the 15s connect deadline, which would
    otherwise turn the one useful diagnostic into a silent hang followed by
    ``ConnectionRefusedError``. This is the hardware backend's *testable*
    failure mode, and the one a misconfigured instrument computer hits.
    """
    started = time.monotonic()
    with pytest.raises(
        DeviceServerStartupError,
        match="exited with status",
    ), remote_instrument(HARDWARE_BACKEND):
        pass  # pragma: no cover - must not get this far
    assert time.monotonic() - started < _CONNECT_TIMEOUT_S


def test_shutdown_times_out_against_a_wedged_server(monkeypatch):
    """
    A server that never answers raises rather than blocking forever.

    The wedge is a documented test-only hook in the server's shutdown
    handler (``MIAINWOODPECKER_WEDGE_SHUTDOWN``), so this exercises the
    real socket-timeout path rather than a mocked-out client.
    """
    monkeypatch.setenv("MIAINWOODPECKER_WEDGE_SHUTDOWN", "1")
    monkeypatch.setattr(remote, "_SHUTDOWN_TIMEOUT_S", 1.0)
    with remote_instrument() as instrument, pytest.raises(
        RemoteCallTimeoutError,
        match="did not reply",
    ):
        instrument.instrument.shutdown()


def test_sigterm_fallback_fires_when_the_server_is_wedged(monkeypatch):
    """
    A wedged server is killed, and its shared-memory segments are still freed.

    Asserts the fallback actually fired rather than inferring it: the
    server process must have died of SIGTERM, and the three segments the
    session used must be gone anyway — the per-device ``close()`` fallback
    is what unlinks them when the handshake cannot.
    """
    monkeypatch.setenv("MIAINWOODPECKER_WEDGE_SHUTDOWN", "1")
    monkeypatch.setattr(remote, "_SHUTDOWN_TIMEOUT_S", 1.0)
    spawned = []
    spawn_server = remote._spawn_server  # noqa: SLF001

    def capturing_spawn(
        *args: object,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        process = spawn_server(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(remote, "_spawn_server", capturing_spawn)
    with remote_instrument() as instrument:
        used_names = _exercise_shared_memory(instrument)
    expected_segment_count = 3  # scanner + both cameras
    assert len(used_names) == expected_segment_count
    assert len(spawned) == 1
    assert spawned[0].returncode == -signal.SIGTERM
    assert used_names.isdisjoint(_shm_names())
