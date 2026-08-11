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

import os
import pathlib
import signal
import subprocess
import threading
import time
from multiprocessing.connection import Client

import numpy as np
import pytest

pytest.importorskip("nion.usim_device", reason="requires the 'device' extra")

from miainwoodpecker.acquisition import energy_offset_series, focal_series
from miainwoodpecker.devices import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    STAGE_POSITION_CONTROL,
    Camera,
    CameraParameters,
    InstrumentController,
    ScanParameters,
    Scanner,
    remote,
)
from miainwoodpecker.devices.nion_server import (
    PARK_TIMEOUT_EXIT_STATUS,
    _SHARED_MEMORY_THRESHOLD_BYTES,
)
from miainwoodpecker.devices.remote import (
    HARDWARE_BACKEND,
    SERVER_EXITED,
    SERVER_RESPONSIVE,
    SERVER_UNRESPONSIVE,
    SIMULATED_BACKEND,
    _CONNECT_TIMEOUT_S,
    DeviceServerStartupError,
    RemoteInstrumentDevices,
    remote_instrument,
    remote_simulated_instrument,
)
from miainwoodpecker.devices.rpc import (
    Call,
    RemoteCallError,
    RemoteCallTimeoutError,
    RemoteConnectionLostError,
)
from miainwoodpecker.storage.calibration import AxisKind, resolve_frame_calibration

_DEV_SHM = pathlib.Path("/dev/shm")  # noqa: S108 - inspected read-only, never written to
_PROC = pathlib.Path("/proc")
_LARGE_SIZE = 1536  # 1536x1536 float64 = ~18.9MB, comfortably above threshold
# A scan slow enough that a kill can land while it is genuinely in flight:
# 4096x4096 measured ~0.9s against usim on this container, versus ~0.15s for
# _LARGE_SIZE. Sized for "reliably still running after a short sleep", not
# for realism.
_SLOW_SCAN_SIZE = 4096
# A dead or wedged server must be reported in well under this. Deliberately
# loose - the point is "does not hang", and the measured figures are two to
# three orders of magnitude below it, so the assertion cannot go flaky on a
# loaded CI machine while still failing an actual hang.
_FAIL_FAST_BUDGET_S = 20.0
# A health check must be cheap enough to poll from a UI timer. Measured
# ~0.4ms against the simulator; asserted loosely for the same reason.
_HEALTH_LATENCY_BUDGET_MS = 500.0
# Enough polls to be a meaningful sample while a scan runs, capped so a fast
# machine cannot spin here indefinitely.
_MAX_HEALTH_POLLS = 20
# A supported camera binning big enough that the frame shape change is
# unmistakable, and enough to force the shared segment to be replaced.
_TEST_BINNING = 4


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


@pytest.fixture
def spawned_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> list[subprocess.Popen[bytes]]:
    """
    Capture every device server subprocess a test spawns.

    Several tests below need the ``Popen`` handle itself — to signal it, or
    to assert how it died — and ``remote_instrument`` deliberately does not
    hand it out (the client API is about devices, not about process
    management). Wrapping the private spawn helper is the least invasive
    way to get it.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Used to restore the real spawn helper afterwards.

    Returns
    -------
    list[subprocess.Popen[bytes]]
        Filled in spawn order as the test runs.
    """
    spawned: list[subprocess.Popen[bytes]] = []
    spawn_server = remote._spawn_server  # noqa: SLF001

    def capturing_spawn(
        *args: object,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        process = spawn_server(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(remote, "_spawn_server", capturing_spawn)
    return spawned


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


def test_camera_settings_configure_over_ipc_and_resize_the_segment(microscope):
    """
    Exposure and binning cross the boundary, and a shape change is survived.

    Binning is the one camera setting that changes the *shape* of every
    subsequent frame, which on this transport means the shared-memory
    segment the frames arrive through has to be replaced. Configured
    while the camera is stopped, so the first frame afterwards is already
    at the new settings.
    """
    camera = microscope.ronchigram_camera
    assert _TEST_BINNING in camera.binning_values
    original = camera.parameters()
    camera.start()
    try:
        unbinned = camera.acquire_frame()
    finally:
        camera.stop()
    binned_settings = camera.configure(
        CameraParameters(exposure_ms=20.0, binning=_TEST_BINNING),
    )
    assert binned_settings.binning == _TEST_BINNING
    assert camera.parameters() == binned_settings
    camera.start()
    try:
        binned = camera.acquire_frame()
    finally:
        camera.stop()
        camera.configure(original)
    assert binned.metadata["binning"] == _TEST_BINNING
    assert binned.metadata["exposure_ms"] == pytest.approx(20.0)
    calibration = resolve_frame_calibration(binned.data.shape, metadata=binned.metadata)
    assert calibration.x.kind is AxisKind.ANGLE
    # A quarter the size in each direction, so the frames no longer fill
    # the segment the unbinned ones sized - the reader has to follow the
    # writer's rather than read the old shape out of the old one.
    assert binned.data.shape[0] * _TEST_BINNING == unbinned.data.shape[0]


def test_camera_calibration_crosses_the_boundary_on_the_shared_memory_path(
    microscope,
):
    """
    A camera's instrument-resolved calibration survives the IPC boundary.

    Worth asserting over IPC rather than only in-process because the two
    transports carry metadata differently: a small frame is pickled whole,
    while a Ronchigram frame goes as a ``SharedFrameRef`` with its metadata
    alongside. The calibration is a nested mapping, so it is exactly the
    shape that a transport carrying only the array would drop silently -
    leaving every recording pixel-calibrated with nothing to say why.
    """
    camera = microscope.ronchigram_camera
    camera.start()
    try:
        frame = camera.acquire_frame()
    finally:
        camera.stop()
    assert frame.data.nbytes >= _SHARED_MEMORY_THRESHOLD_BYTES
    calibration = resolve_frame_calibration(frame.data.shape, metadata=frame.metadata)
    assert calibration.x.kind is AxisKind.ANGLE
    assert calibration.x.units == "rad"
    assert calibration.y.kind is AxisKind.ANGLE


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


# ---------------------------------------------- frame identity
#
# Two contracts Nion asserts about its own scan device
# (ScanControl_test.test_frame_do_not_change_after_acquisition and
# test_consecutive_frames_have_unique_data). In-process for Nion they are
# nearly tautological. Here they are not: every frame above the threshold
# arrives through *one reused* shared-memory segment, so a caller holding
# frame N while frame N+1 is written is exactly the situation the segment
# reuse creates and the copy-out exists to survive.
#
# Both use frames comfortably above the threshold and identical in shape,
# which is what guarantees the segment is reused rather than replaced.

_IDENTITY_FRAME_SIZE = 256  # 256x256 float64 = 512KB, 8x the threshold
_IDENTITY_FRAME_COUNT = 4


def _identity_scan_parameters(microscope) -> ScanParameters:
    """Return scan parameters that put a frame on the shared-memory path."""
    return ScanParameters(
        height=_IDENTITY_FRAME_SIZE,
        width=_IDENTITY_FRAME_SIZE,
        pixel_time_us=1.0,
        fov_nm=microscope.stage_size_nm * 0.1,
    )


def test_a_held_frame_does_not_change_when_later_frames_arrive(microscope):
    """
    Frames already returned stay as they were, however many follow them.

    The failure this rules out is specific and silent: if ``_frame`` handed
    back a view onto the shared segment instead of copying out of it, every
    frame a caller was holding would mutate under them on the next
    acquisition — a live viewer's history, a recording's buffered frames,
    a focal series' earlier steps. Checksums are taken as each frame
    arrives and re-checked at the end, which is Nion's own construction.
    """
    scanner = microscope.scanner
    parameters = _identity_scan_parameters(microscope)
    frames = []
    checksums = []
    for _ in range(_IDENTITY_FRAME_COUNT):
        frame = scanner.scan_frame(parameters, channel=0)
        assert frame.data.nbytes >= _SHARED_MEMORY_THRESHOLD_BYTES
        frames.append(frame)
        checksums.append(float(np.sum(frame.data)))
    for checksum, frame in zip(checksums, frames, strict=True):
        assert float(np.sum(frame.data)) == checksum


def test_consecutive_frames_are_not_the_same_frame_twice(microscope):
    """
    Successive scans of one region differ, so no frame is a stale re-read.

    The complement of the test above: that one catches a frame being
    overwritten, this one catches a frame never being written — a reader
    that returns whatever the segment last held would pass every checksum
    test while showing the operator a frozen image. Shot noise makes two
    scans of the same region differ in every row; Nion's version notes
    this is invalid on a saturated detector, which is why the assertion
    is on rows rather than on an exact-equality of the whole frame.
    """
    scanner = microscope.scanner
    parameters = _identity_scan_parameters(microscope)
    first = scanner.scan_frame(parameters, channel=0)
    for _ in range(_IDENTITY_FRAME_COUNT - 1):
        later = scanner.scan_frame(parameters, channel=0)
        identical_rows = int(np.sum(np.all(first.data == later.data, axis=1)))
        assert identical_rows == 0


def test_a_big_scan_does_not_prevent_the_next_one(microscope):
    """
    A scan large enough to force a new segment leaves the device usable.

    Ported from Nion's ``test_big_scan_does_not_prevent_further_playing``.
    The suite already scans 4096² to prove the big scan itself succeeds;
    what it never checked is the half that actually bites, because the
    large frame replaces the shared segment — a client that failed to
    follow the new segment would break on the *next*, ordinary scan
    rather than on the big one.
    """
    scanner = microscope.scanner
    fov_nm = microscope.stage_size_nm * 0.1
    big = ScanParameters(
        height=_LARGE_SIZE,
        width=_LARGE_SIZE,
        pixel_time_us=1.0,
        fov_nm=fov_nm,
    )
    scanner.scan_frame(big, channel=0)
    ordinary = ScanParameters(height=32, width=48, pixel_time_us=1.0, fov_nm=fov_nm)
    frame = scanner.scan_frame(ordinary, channel=0)
    assert frame.data.shape == ordinary.shape
    assert float(np.std(frame.data)) > 0.0


# ------------------------------------------ device errors and recovery
#
# Nion asserts that a device error stops acquisition, surfaces, and leaves
# the device restartable (test_exception_during_view_halts_scan and
# test_able_to_restart_scan_after_exception_scan). In-process, for Nion,
# the first two are exception propagation. Across this boundary they are a
# genuinely different question - the exception raises in another process,
# and what reaches the caller is a Result carrying a string - and the
# third is the one that matters: is the *server* still usable afterwards,
# or does the client have to respawn it and lose every device setting and
# instrument control with it?

_BAD_CHANNEL = 99


def test_a_device_error_surfaces_with_its_type_and_leaves_the_device_usable(
    microscope,
):
    """
    A vendor exception crosses as a typed error, and the device works after.

    Scanning a channel that does not exist raises ``IndexError`` inside
    Nion's own scan device — a plain operator mistake rather than a
    contrived failure. What matters is both halves: the caller learns
    *what* failed rather than getting one indistinguishable
    ``RemoteCallError``, and the very next scan on the same device and
    the same connection succeeds. Without the second half, every bad
    argument would cost a session.
    """
    scanner = microscope.scanner
    parameters = ScanParameters(
        height=16,
        width=16,
        pixel_time_us=1.0,
        fov_nm=microscope.stage_size_nm * 0.1,
    )
    with pytest.raises(RemoteCallError) as failure:
        scanner.scan_frame(parameters, channel=_BAD_CHANNEL)
    assert failure.value.error_type == "IndexError"

    recovered = scanner.scan_frame(parameters, channel=0)
    assert recovered.data.shape == parameters.shape


def test_a_device_error_leaves_the_server_and_its_other_targets_alive(microscope):
    """
    One device's failure is not the server's, nor the other devices'.

    Each target is served on its own connection and its own handler
    thread, so this is the property that makes a bad call cheap: after
    the scanner raises, the server still answers a health check and the
    camera still acquires. A failure that took the process down would
    also drop the instrument controls and leave the column unparked.
    """
    scanner = microscope.scanner
    parameters = ScanParameters(
        height=16,
        width=16,
        pixel_time_us=1.0,
        fov_nm=microscope.stage_size_nm * 0.1,
    )
    with pytest.raises(RemoteCallError):
        scanner.scan_frame(parameters, channel=_BAD_CHANNEL)

    assert microscope.instrument.check_health().state == SERVER_RESPONSIVE
    camera = microscope.eels_camera
    camera.start()
    try:
        assert camera.acquire_frame().data.ndim == 2  # noqa: PLR2004 - a 2D detector
    finally:
        camera.stop()


def test_outgrowing_the_segment_creates_a_new_one_and_frees_the_old(microscope):
    """
    A frame too big for the segment forces a new one, and the old one goes.

    Sized relative to the segment's *current* capacity rather than to
    fixed constants, because the writer only replaces a segment when a
    frame does not fit — shrinking deliberately reuses it, which is what
    stops an alternating workload from paying create/unlink per frame.
    Fixed sizes made this test depend on what had run before it: any
    earlier test scanning larger (the 4096² health-check scan, say)
    left a segment both of these frames fit inside, so no resize
    happened and the assertion failed. Serial execution in file order
    hid that; running the module under ``pytest-xdist`` distributes the
    tests and it surfaces immediately.
    """
    scanner = microscope.scanner
    fov_nm = microscope.stage_size_nm * 0.1

    def scan(side: int) -> None:
        scanner.scan_frame(
            ScanParameters(
                height=side, width=side, pixel_time_us=1.0, fov_nm=fov_nm
            ),
            channel=0,
        )

    scan(_LARGE_SIZE)
    first_name = scanner._reader._segment.name  # noqa: SLF001
    capacity = scanner._reader._segment.size  # noqa: SLF001

    # Comfortably past the current capacity, whatever earlier tests left it at.
    side = int((capacity / 8) ** 0.5) + 512
    scan(side)

    second_name = scanner._reader._segment.name  # noqa: SLF001
    assert second_name != first_name
    assert scanner._reader._segment.size > capacity  # noqa: SLF001
    if _DEV_SHM.is_dir():
        assert first_name not in _shm_names()


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
        ENERGY_OFFSET_CONTROL,
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


def test_energy_offset_series_moves_the_spectrum_it_labels(microscope):
    """
    An energy sweep changes the data, not just the metadata.

    The claim ``focal_series`` explicitly cannot make against the
    simulator, and the reason this sweep is the better documentation
    example: stepping the spectrometer offset visibly moves the zero-loss
    peak across the detector, so the frames a reader gets really do
    differ. Both halves are asserted — the recorded energy axis follows
    the offset, *and* the spectrum underneath it moved.

    This is also the regression guard for the reason the generator stops
    the camera between steps: acquired continuously, the frame returned
    after a control change was generated before it, so the peak would
    stay put while the axis moved.
    """
    instrument = microscope.instrument
    assert ENERGY_OFFSET_CONTROL in instrument.available_controls()
    original = instrument.energy_offset_ev()
    values = [-20.0, 20.0]
    frames = list(
        energy_offset_series(microscope.eels_camera, values, instrument=instrument),
    )

    assert [frame.metadata["requested_energy_offset_ev"] for frame in frames] == values
    assert [
        frame.metadata["energy_offset_ev"] for frame in frames
    ] == pytest.approx(values)
    # The camera resolves its energy axis from the same instrument
    # control, so the recorded calibration tracks the sweep for free.
    axis_offsets = [
        resolve_frame_calibration(
            frame.data.shape,
            metadata=frame.metadata,
        ).x.offset
        for frame in frames
    ]
    assert axis_offsets == pytest.approx(values)
    # And the data moved: the zero-loss peak is in a different channel.
    peaks = [int(np.argmax(frame.data.sum(axis=0))) for frame in frames]
    assert peaks[0] != peaks[1]
    assert instrument.energy_offset_ev() == pytest.approx(original)


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


def test_an_explicit_shutdown_leaves_teardown_nothing_to_kill(spawned_servers):
    """
    Calling the handshake yourself makes teardown a no-op, not a second SIGTERM.

    Acknowledging a shutdown ends the server's life — it closes that
    connection and exits — so the handshake is deliberately *not*
    re-callable over the same connection. What teardown must do instead is
    notice the process has already gone. Asserted by exit status: a clean
    ``0`` rather than the ``-SIGTERM`` a redundant kill would leave.
    """
    with remote_instrument() as instrument:
        instrument.instrument.shutdown()
    assert spawned_servers[0].returncode == 0


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
    monkeypatch.setenv("MIAINWOODPECKER_ENABLE_TEST_HOOKS", "1")
    monkeypatch.setenv("MIAINWOODPECKER_WEDGE_SHUTDOWN", "1")
    monkeypatch.setattr(remote, "_SHUTDOWN_TIMEOUT_S", 1.0)
    with remote_instrument() as instrument, pytest.raises(
        RemoteCallTimeoutError,
        match="did not reply",
    ):
        instrument.instrument.shutdown()


def test_sigterm_fallback_fires_when_the_server_is_wedged(
    monkeypatch,
    spawned_servers,
):
    """
    A wedged server dies to the fallback, and its segments are still freed.

    Asserts the fallback actually fired rather than inferring it, and the
    three segments the session used must be gone anyway — the per-device
    ``close()`` fallback is what unlinks them when the handshake cannot.

    **How the server dies changed when it grew signal handlers, and the
    change is the point.** It used to die *of* SIGTERM (``-15``), the
    default disposition. Now SIGTERM is caught and the server tries to
    park first — but in this test the park is exactly what is wedged (the
    hook blocks ``park_and_release`` itself), so the bounded attempt
    expires and the server exits with ``PARK_TIMEOUT_EXIT_STATUS``. That
    is the correct outcome and the reason the attempt is bounded at all:
    an unbounded park here would have made a wedged server survive
    SIGTERM entirely, forcing the client to escalate to ``SIGKILL`` —
    which is what this test caught when the handler was first written.

    Either way the process is gone promptly and the segments are
    reclaimed; ``-SIGKILL`` is the one outcome that would mean the
    handler had made things worse.
    """
    monkeypatch.setenv("MIAINWOODPECKER_ENABLE_TEST_HOOKS", "1")
    monkeypatch.setenv("MIAINWOODPECKER_WEDGE_SHUTDOWN", "1")
    monkeypatch.setattr(remote, "_SHUTDOWN_TIMEOUT_S", 1.0)
    with remote_instrument() as instrument:
        used_names = _exercise_shared_memory(instrument)
    expected_segment_count = 3  # scanner + both cameras
    assert len(used_names) == expected_segment_count
    assert len(spawned_servers) == 1
    assert spawned_servers[0].returncode == PARK_TIMEOUT_EXIT_STATUS
    assert used_names.isdisjoint(_shm_names())


# ------------------------------------------------------------ health check


def test_health_check_reports_a_responsive_server(microscope):
    """
    A live server answers with its own process state, quickly.

    The latency assertion is part of the contract rather than decoration:
    the check exists to be polled routinely (a UI status timer), which it
    can only be if it stays cheap. It is cheap because the server's handler
    touches no device and takes no lock — see ``NionInstrument.health``.
    """
    health = microscope.instrument.check_health()
    assert health.state == SERVER_RESPONSIVE
    assert health.is_responsive
    assert health.exit_status is None
    assert health.latency_ms < _HEALTH_LATENCY_BUDGET_MS
    report = health.report
    assert report["ok"] is True
    assert report["backend"] == SIMULATED_BACKEND
    assert report["pid"] > 0
    assert report["shutting_down"] is False
    assert set(report["targets"]) == {
        "ronchigram_camera",
        "eels_camera",
        "scanner",
    }
    # Nothing has been closed yet, so every served device is still open.
    assert set(report["open_devices"]) == set(report["targets"])
    assert "responsive" in health.detail


def test_health_check_does_not_disturb_an_acquisition(microscope):
    """
    Polling health while a scan is in flight neither blocks nor corrupts it.

    This is the property that makes the check usable at all, and it is not
    self-evident: a health call sharing the instrument control connection
    would serialize behind whatever is on it, and a health call that
    touched a device would contend with the acquisition itself. So the scan
    runs on a worker thread while the main thread polls, and both halves
    are asserted — every poll answers promptly, *and* the frame comes back
    intact.
    """
    parameters = ScanParameters(
        height=_SLOW_SCAN_SIZE,
        width=_SLOW_SCAN_SIZE,
        pixel_time_us=1.0,
        fov_nm=microscope.stage_size_nm * 0.1,
    )
    outcome: dict[str, object] = {}

    def scan() -> None:
        frame = microscope.scanner.scan_frame(parameters, channel=0)
        outcome["shape"] = frame.data.shape

    worker = threading.Thread(target=scan)
    worker.start()
    try:
        polled = []
        while worker.is_alive() and len(polled) < _MAX_HEALTH_POLLS:
            polled.append(microscope.instrument.check_health())
            time.sleep(0.01)
    finally:
        worker.join(timeout=_FAIL_FAST_BUDGET_S)
    assert not worker.is_alive()
    assert outcome["shape"] == parameters.shape
    assert polled, "the scan finished before a single health check ran"
    assert all(health.state == SERVER_RESPONSIVE for health in polled)
    assert all(health.latency_ms < _HEALTH_LATENCY_BUDGET_MS for health in polled)
    # Restore the segment size the rest of this module's tests expect.
    microscope.scanner.scan_frame(
        ScanParameters(
            height=_LARGE_SIZE,
            width=_LARGE_SIZE,
            pixel_time_us=1.0,
            fov_nm=microscope.stage_size_nm * 0.1,
        ),
        channel=0,
    )


def test_health_check_detects_a_killed_server(spawned_servers):
    """
    SIGKILL at idle is reported as "exited", naming the signal.

    ``SIGKILL`` rather than ``SIGTERM`` on purpose: it is the one signal a
    process cannot handle, so nothing on the server side gets to tidy up or
    reply, which is exactly the condition the client must diagnose on its
    own.
    """
    with remote_instrument() as instrument:
        server = spawned_servers[0]
        assert instrument.instrument.check_health().is_responsive
        server.send_signal(signal.SIGKILL)
        server.wait(timeout=_FAIL_FAST_BUDGET_S)
        started = time.monotonic()
        health = instrument.instrument.check_health()
        assert time.monotonic() - started < _FAIL_FAST_BUDGET_S
    assert health.state == SERVER_EXITED
    assert health.exit_status == -signal.SIGKILL
    assert "SIGKILL" in health.detail
    assert "cannot be recovered" in health.detail


def test_health_check_detects_a_wedged_server(monkeypatch, spawned_servers):
    """
    A server that is running but not answering is a third, distinct verdict.

    Driven by a documented test-only server hook
    (``MIAINWOODPECKER_WEDGE_HEALTH``) so the real bounded-wait path runs
    against a genuinely unresponsive handler rather than a mocked client.

    Two things are asserted beyond the verdict itself, both consequences of
    giving the probe its own connection. The instrument's *control* calls
    must still work — a wedged status probe must not cost the operator the
    ability to blank the beam. And the retired probe must keep answering
    from what it already knows instead of sending a second request, since
    the first reply could still arrive and would be misread as the second's.
    """
    monkeypatch.setenv("MIAINWOODPECKER_ENABLE_TEST_HOOKS", "1")
    monkeypatch.setenv("MIAINWOODPECKER_WEDGE_HEALTH", "1")
    with remote_instrument() as instrument:
        started = time.monotonic()
        health = instrument.instrument.check_health(timeout_s=1.0)
        elapsed = time.monotonic() - started
        assert health.state == SERVER_UNRESPONSIVE
        assert health.exit_status is None
        assert "did not answer" in health.detail
        assert "wedged rather than busy" in health.detail
        assert elapsed < _FAIL_FAST_BUDGET_S
        # The control connection is untouched by the wedged probe.
        assert instrument.instrument.defocus_nm() > 0
        again = time.monotonic()
        second = instrument.instrument.check_health(timeout_s=1.0)
        assert time.monotonic() - again < 1.0
        assert second.state == SERVER_UNRESPONSIVE
        assert "retired this probe" in second.detail
    assert spawned_servers[0].returncode is not None


# --------------------------------------------------------- kill and recover


def test_a_device_call_after_a_kill_fails_fast_and_names_the_cause(spawned_servers):
    """
    Both a device call and a control call fail immediately, saying why.

    The behaviour being pinned is that a dead server does *not* need a
    timeout to be detected: its socket closes, so the call fails at once.
    What the client adds is the diagnosis — before this, the symptom was a
    bare ``EOFError`` with no message, naming neither the target nor the
    fact that the server had died.
    """
    with remote_instrument() as instrument:
        server = spawned_servers[0]
        parameters = ScanParameters(
            height=32,
            width=48,
            pixel_time_us=1.0,
            fov_nm=instrument.stage_size_nm * 0.1,
        )
        server.send_signal(signal.SIGKILL)
        server.wait(timeout=_FAIL_FAST_BUDGET_S)

        started = time.monotonic()
        with pytest.raises(RemoteConnectionLostError) as failure:
            instrument.scanner.scan_frame(parameters, channel=0)
        assert time.monotonic() - started < _FAIL_FAST_BUDGET_S
        message = str(failure.value)
        assert "scanner.scan_frame()" in message
        assert "SIGKILL" in message
        assert "cannot be recovered" in message

        started = time.monotonic()
        with pytest.raises(RemoteConnectionLostError, match="SIGKILL"):
            instrument.instrument.defocus_nm()
        assert time.monotonic() - started < _FAIL_FAST_BUDGET_S


def test_killing_the_server_mid_acquisition_fails_the_call_in_flight(
    monkeypatch, spawned_servers,
):
    """
    A kill during a scan raises in the blocked call rather than hanging it.

    The mid-acquisition case is the one that matters operationally and the
    one an idle-only test would miss: the client is parked in
    ``connection.recv()`` with no timeout — correctly, because a real
    exposure takes as long as it takes — so the only thing that can wake it
    is the socket closing. That it does, and that the resulting error names
    the cause, is what is asserted here.

    "In flight" is arranged deterministically rather than raced for. This
    originally sized a scan to be slow (4096x4096) and slept a fixed 0.3s
    before killing, which is a bet on the machine: on a CI runner the scan
    finished inside the window and the test failed on its own premise,
    reporting nothing about the behaviour under test. The server's
    ``MIAINWOODPECKER_DELAY_SCAN_S`` test hook now holds the scan open for a
    known duration — the same approach the wedge hooks already take — and the
    worker signals when it has actually issued the call, so no sleep is
    guessing at anything.
    """
    delay_s = 10.0
    monkeypatch.setenv("MIAINWOODPECKER_ENABLE_TEST_HOOKS", "1")
    monkeypatch.setenv("MIAINWOODPECKER_DELAY_SCAN_S", str(delay_s))
    with remote_instrument() as instrument:
        server = spawned_servers[0]
        parameters = ScanParameters(
            height=_LARGE_SIZE,
            width=_LARGE_SIZE,
            pixel_time_us=1.0,
            fov_nm=instrument.stage_size_nm * 0.1,
        )
        outcome: dict[str, object] = {}
        issued = threading.Event()

        def scan() -> None:
            started = time.monotonic()
            issued.set()
            try:
                instrument.scanner.scan_frame(parameters, channel=0)
            except BaseException as error:  # noqa: BLE001 - recorded for the assertions
                outcome["error"] = error
            outcome["elapsed_s"] = time.monotonic() - started

        worker = threading.Thread(target=scan)
        worker.start()
        assert issued.wait(timeout=_FAIL_FAST_BUDGET_S), "the scan never started"
        # The call is issued and the server is sleeping in the hook, so the
        # client is parked in recv(); killing now is guaranteed mid-flight.
        assert worker.is_alive()
        server.send_signal(signal.SIGKILL)
        worker.join(timeout=_FAIL_FAST_BUDGET_S)
        assert not worker.is_alive()

    assert isinstance(outcome["error"], RemoteConnectionLostError)
    # Well inside the hook's delay, so the error came from the kill waking a
    # blocked recv() rather than from the scan completing on its own.
    assert outcome["elapsed_s"] < delay_s
    assert "SIGKILL" in str(outcome["error"])


def test_a_broken_connection_reports_more_than_a_bare_eof_error(spawned_servers):
    """
    A connection lost with the server still alive is also diagnosed, not raw.

    The third failure mode, and the one that is *not* process death: the
    socket goes away while the server is fine. Provoked by closing the
    client's own end, which is the only way to produce it deterministically.
    The point is the shape of the error — named call, stated cause — since
    the underlying ``OSError``/``EOFError`` carries no message at all.
    """
    with remote_instrument() as instrument:
        parameters = ScanParameters(
            height=32,
            width=48,
            pixel_time_us=1.0,
            fov_nm=instrument.stage_size_nm * 0.1,
        )
        instrument.scanner._connection.close()  # noqa: SLF001
        with pytest.raises(RemoteConnectionLostError) as failure:
            instrument.scanner.scan_frame(parameters, channel=0)
        message = str(failure.value)
        assert "scanner.scan_frame()" in message
        assert "still running" in message
        assert instrument.instrument.check_health().is_responsive
    # The handshake still ran, so the server exited cleanly despite the
    # broken device connection.
    assert spawned_servers[0].returncode == 0


def test_a_sigkilled_server_leaves_no_shared_memory_segments(spawned_servers):
    """
    The three segments a killed session used are gone once it tears down.

    Same pattern as ``test_no_shared_memory_segments_leak_after_teardown``:
    name the segments the session actually used and assert those are gone,
    rather than diffing whole-directory ``/dev/shm`` snapshots, which is
    flaky because ``/dev/shm`` is machine-global.

    Two independent mechanisms can satisfy this, which is why the next test
    exists to separate them: the server's own ``resource_tracker`` (a child
    process that outlives a ``SIGKILL``-ed parent and cleans up after it)
    and, failing that, the client's orphan unlink.
    """
    with remote_instrument() as instrument:
        used_names = _exercise_shared_memory(instrument)
        server = spawned_servers[0]
        server.send_signal(signal.SIGKILL)
        server.wait(timeout=_FAIL_FAST_BUDGET_S)
    expected_segment_count = 3  # scanner + both cameras
    assert len(used_names) == expected_segment_count
    assert spawned_servers[0].returncode == -signal.SIGKILL
    assert used_names.isdisjoint(_shm_names())


def _resource_tracker_pids(pid: int) -> list[int]:
    """
    Return the ``multiprocessing.resource_tracker`` children of a process.

    Parameters
    ----------
    pid : int
        Parent process id to search under.

    Returns
    -------
    list[int]
        Pids of that process's resource-tracker children.
    """
    found = []
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
            cmdline = (entry / "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue  # the process exited while we were reading it
        parent = [
            line.split()[1] for line in status.splitlines() if line.startswith("PPid:")
        ]
        if parent and int(parent[0]) == pid and "resource_tracker" in cmdline:
            found.append(int(entry.name))
    return found


@pytest.mark.skipif(
    not _PROC.is_dir() or not _DEV_SHM.is_dir(),
    reason="needs Linux /proc and /dev/shm to find the tracker and see segments",
)
def test_the_client_reclaims_segments_when_the_tracker_dies_too(spawned_servers):
    """
    Kill the server *and* its resource tracker: the client still cleans up.

    This test earns its complexity by isolating a mechanism the simpler leak
    test cannot see. Measuring rather than assuming what a ``SIGKILL``
    leaves behind turned up something the design had not accounted for: the
    server's ``multiprocessing.resource_tracker`` is a *separate child
    process*, it registers every segment the writer creates, and it unlinks
    them when the server's end of its pipe closes — so an ordinary
    ``SIGKILL`` of the server alone leaks nothing, despite the server itself
    running no cleanup at all.

    That is a CPython implementation detail, not a guarantee, and it fails
    exactly when the tracker dies alongside the server: a process-group
    kill, a cgroup OOM kill, a container stop. So this test kills both, and
    asserts in two stages — first that the segments really do survive (the
    leak is real, and this test would be vacuous otherwise), then that the
    client's own unlink reclaims them, which is the only thing left that
    can.
    """
    with remote_instrument() as instrument:
        used_names = _exercise_shared_memory(instrument)
        server = spawned_servers[0]
        trackers = _resource_tracker_pids(server.pid)
        assert trackers, "expected the server to own a resource_tracker child"
        for tracker_pid in trackers:
            os.kill(tracker_pid, signal.SIGKILL)
        server.send_signal(signal.SIGKILL)
        server.wait(timeout=_FAIL_FAST_BUDGET_S)
        # Stage one: with no tracker, the leak is real and observable.
        leaked = used_names & _shm_names()
        assert leaked == used_names, (
            "expected every segment to survive a server killed alongside its "
            f"resource tracker; only {sorted(leaked)} of {sorted(used_names)} did"
        )
    # Stage two: teardown noticed the abnormal exit and unlinked them.
    assert used_names.isdisjoint(_shm_names())


@pytest.fixture
def spawn_details(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """
    Capture the ports and authkey each spawned server was given.

    The client API deliberately exposes devices rather than transport
    details, so a test that needs to open its *own* connection to a target
    - to prove the server refuses a second one - has to learn them from
    the spawn call.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Used to restore the real spawn helper afterwards.

    Returns
    -------
    list[dict[str, object]]
        One entry per spawn, in order, with ``ports`` and ``authkey``.
    """
    captured: list[dict[str, object]] = []
    spawn_server = remote._spawn_server  # noqa: SLF001

    def capturing_spawn(
        ports: dict[str, int],
        authkey: bytes,
        *args: object,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        captured.append({"ports": dict(ports), "authkey": authkey})
        return spawn_server(ports, authkey, *args, **kwargs)

    monkeypatch.setattr(remote, "_spawn_server", capturing_spawn)
    return captured


def test_a_second_connection_to_a_frame_target_is_refused(spawn_details):
    """
    A frame-producing target admits one client at a time, and says so.

    ``SharedFrameWriter`` reuses one segment per source, which is safe
    only while a single request/response is in flight: a second client's
    publish would overwrite the segment while the first is still copying
    out of it, splicing two frames together with nothing raised anywhere.
    That invariant used to rest on client convention alone - one
    ``_RemoteDevice`` per target - which nothing server-side could check
    and a second viewer pointed at these ports would quietly break.

    The rejection is *served* rather than dropped, so the intruder learns
    why on its first call instead of getting a bare EOF.
    """
    with remote_instrument() as instrument:
        details = spawn_details[0]
        port = details["ports"]["scanner"]
        intruder = Client(("localhost", port), authkey=details["authkey"])
        try:
            intruder.send(Call("scanner", "scanner_id"))
            result = intruder.recv()
            assert result.error is not None
            assert "already driven by another connection" in result.error
        finally:
            intruder.close()

        # The legitimate client is untouched by the refusal.
        assert isinstance(instrument.scanner.scanner_id, str)


def test_a_refused_connection_releases_the_target_when_it_closes(spawn_details):
    """A rejected client must not permanently hold the exclusivity slot."""
    with remote_instrument() as instrument:
        details = spawn_details[0]
        port = details["ports"]["scanner"]
        for _ in range(3):
            intruder = Client(("localhost", port), authkey=details["authkey"])
            intruder.send(Call("scanner", "scanner_id"))
            assert intruder.recv().error is not None
            intruder.close()
        assert isinstance(instrument.scanner.scanner_id, str)


def test_the_instrument_target_still_accepts_two_connections(spawn_details):
    """
    Exclusivity applies to frame targets only, not to ``instrument``.

    The client itself opens two connections to that port on every
    session - one for controls, one for health - precisely so a status
    poll cannot queue behind a slow stage move. It publishes no frames,
    so it has no segment to corrupt.
    """
    with remote_instrument():
        details = spawn_details[0]
        port = details["ports"]["instrument"]
        extra = Client(("localhost", port), authkey=details["authkey"])
        try:
            extra.send(Call("instrument", "health"))
            result = extra.recv()
            assert result.error is None
            assert result.value["ok"] is True
        finally:
            extra.close()


def test_sigterm_parks_the_beam_before_the_server_exits(spawned_servers):
    """
    The client's fallback is terminate(), and it must not leave the beam on.

    A signal-less server answered SIGTERM by dying with the column live -
    and SIGTERM is precisely the wedged-server path that fallback exists
    for, so it is the case where parking matters most. The assertion is
    on observable outcomes rather than on a log line: the process leaves
    through ``serve()``'s own return (status 0, not death by signal), and
    parking released the devices, which is what retires their segments.
    """
    with remote_instrument() as instrument:
        controls = instrument.instrument.available_controls()
        assert BEAM_BLANKER_CONTROL in controls, "usim models a blanker"
        instrument.instrument.set_beam_blanked(blanked=False)
        assert not instrument.instrument.is_beam_blanked()
        used_names = _exercise_shared_memory(instrument)

        server = spawned_servers[0]
        server.send_signal(signal.SIGTERM)
        server.wait(timeout=_FAIL_FAST_BUDGET_S)
        assert server.returncode == 0
    assert used_names.isdisjoint(_shm_names())


def test_the_wedge_hook_does_nothing_without_the_flag(monkeypatch, spawned_servers):
    """
    The environment variable alone must not wedge a server.

    This is the safety property the flag exists for. The environment is
    inherited wholesale by the subprocess, so an operator with
    MIAINWOODPECKER_WEDGE_SHUTDOWN left set in a shell profile used to
    get a real instrument whose shutdown blocked forever with nothing
    saying why. With the hook set but the flag absent, shutdown is
    ordinary: the handshake succeeds and the server exits 0, rather than
    timing out and being terminated.
    """
    monkeypatch.setenv("MIAINWOODPECKER_WEDGE_SHUTDOWN", "1")
    monkeypatch.delenv("MIAINWOODPECKER_ENABLE_TEST_HOOKS", raising=False)
    monkeypatch.setattr(remote, "_SHUTDOWN_TIMEOUT_S", 10.0)

    with remote_instrument() as instrument:
        used_names = _exercise_shared_memory(instrument)

    assert len(spawned_servers) == 1
    assert spawned_servers[0].returncode == 0, (
        "an unarmed wedge hook must leave the graceful shutdown intact"
    )
    assert used_names.isdisjoint(_shm_names())


def test_an_orphaned_server_parks_itself_and_exits(monkeypatch, spawned_servers):
    """
    A server whose client vanished must not hold the instrument forever.

    Without this, ``serve()`` blocks on its stop event indefinitely: the
    vendor devices stay open, the camera threads keep running, the ports
    stay bound, and the beam stays unblanked, with nothing to reclaim any
    of it. On real hardware that is a column nobody can use and nobody is
    watching.

    Simulated by closing every connection without the shutdown
    handshake, which is what a crashed client looks like from the
    server's side. "All connections gone" is a sound signal here
    specifically because reconnect is deliberately unsupported (migration
    plan, §6), so a live client holds its connections for its whole life
    - an *idle* client is still a connected one, and cannot trip this.
    """
    monkeypatch.setenv("MIAINWOODPECKER_ENABLE_TEST_HOOKS", "1")
    monkeypatch.setenv("MIAINWOODPECKER_ORPHAN_GRACE_S", "1.0")

    with remote_instrument() as instrument:
        used_names = _exercise_shared_memory(instrument)
        server = spawned_servers[0]
        devices = [
            instrument.ronchigram_camera,
            instrument.eels_camera,
            instrument.scanner,
        ]
        for device in devices:
            if device is not None:
                device._connection.close()  # noqa: SLF001 - simulating a crash
        instrument.instrument._connection.close()  # noqa: SLF001
        instrument.instrument._health_connection.close()  # noqa: SLF001

        # The watchdog should notice and take the server down on its own.
        server.wait(timeout=_FAIL_FAST_BUDGET_S)
        assert server.returncode == 0, (
            "an orphaned server should park and exit through its normal path"
        )

    assert used_names.isdisjoint(_shm_names())
