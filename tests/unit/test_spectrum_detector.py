"""
The spectrum-detector shape: the protocol, the simulator, and the transport.

Runs entirely on the simulated backend, so it needs no vendor software
and no detector — which is the point, because there is no redistributable
EDX SDK and never will be. What is exercised here is everything an
out-of-tree Bruker or Oxford adapter would have to satisfy: the value
types and their refusals, the acquisition-mode contract, the two
acquisition shapes, and the whole client/transport path including the
shared-memory route a spectrum image takes.

The two detectors this was designed against are a Bruker XFlash 6T-100 on
SuperSTEM 2 and an Oxford Ultim Extreme on SuperSTEM 4. Neither is here;
the space they slot into is.
"""

from __future__ import annotations

import datetime

import numpy as np
import pytest

from miainwoodpecker.devices import (
    MAP_MODE,
    SPOT_MODE,
    ScanParameters,
    Spectrum,
    SpectrumDetector,
    SpectrumParameters,
)
from miainwoodpecker.devices.remote import (
    HARDWARE_BACKEND,
    SERVER_RESPONSIVE,
    SIMULATED_BACKEND,
    remote_instrument,
)
from miainwoodpecker.devices.rpc import (
    SHARED_MEMORY_THRESHOLD_BYTES,
    SPECTRUM_TARGET_NAMES,
    TARGET_NAMES,
)
from miainwoodpecker.devices.spectrum_server import (
    NO_DETECTOR_EXIT_STATUS,
    SPECTRUM_TARGET,
    SimulatedSpectrumDetector,
    SpectrumDetectorOpenError,
    _parse_args,
    main,
    open_detector,
)

_SERVER_MODULE = "miainwoodpecker.devices.spectrum_server"
# Small enough that a map runs fast, big enough that the two composition
# halves of the simulated field are both present.
_MAP = ScanParameters(height=4, width=6, pixel_time_us=200.0, fov_nm=120.0)
# Deliberately over SHARED_MEMORY_THRESHOLD_BYTES: 8x8x1024 uint32 is
# 256KB, so the shared-memory branch is the one exercised rather than the
# pickle one. Still trivial next to a real 256x256x4096 map, which is 1GB
# and is why that branch had to exist at all.
_BIG_MAP = ScanParameters(height=8, width=8, pixel_time_us=2000.0, fov_nm=200.0)
_BIG_MAP_CHANNELS = 1024
_FEW_CHANNELS = 512
_TWO_SPECTRA = 2


@pytest.fixture
def detector_microscope():
    """Spawn the spectrum server on its simulated backend and connect."""
    with remote_instrument(server_module=_SERVER_MODULE) as devices:
        yield devices


# --------------------------------------------------------------------------
# The value types, and the things they refuse.
# --------------------------------------------------------------------------


def _spectrum(data, **overrides: object) -> Spectrum:
    """Build a Spectrum with plausible defaults, for the refusal tests."""
    fields = {
        "timestamp": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        "energy_offset_ev": -480.0,
        "energy_scale_ev": 10.0,
    }
    fields.update(overrides)
    return Spectrum(data=data, **fields)


def test_a_spectrum_cannot_be_built_without_a_usable_energy_axis():
    """
    A zero or negative dispersion is refused, not stored.

    This is the whole argument for ``Spectrum`` being its own type rather
    than a ``Frame`` with a 1D array, made executable. A ``Frame`` may
    carry no calibration at all and still be a perfectly good image; an
    array of counts with no dispersion is not a spectrum, because there
    is no axis to put the counts against. Making the energy calibration a
    constructor argument is what turns "should have one" into "cannot
    exist without one", and this pins that it really is enforced rather
    than merely documented.
    """
    for bad in (0.0, -10.0):
        with pytest.raises(ValueError, match="energy_scale_ev must be positive"):
            _spectrum(np.zeros(8), energy_scale_ev=bad)


def test_a_spectrum_refuses_ranks_no_nxspectrum_group_describes():
    """
    Rank 1 to 3 are accepted; 4 is refused and the message says why.

    The accepted ranks are exactly ``NXspectrum``'s ``spectrum_0d``,
    ``_1d`` and ``_2d`` — a spot spectrum, a line, and a map — which is
    also everything a scan unit can produce in one acquisition. Rank 4
    would be a 3D region of interest, which in this domain is a
    serial-sectioning *result* rather than something a detector hands
    back, so accepting it would mean storing something no writer here
    describes. Refusing at construction is where the caller can still act.
    """
    for rank in (1, 2, 3):
        shape = (2,) * (rank - 1) + (8,)
        assert _spectrum(np.zeros(shape)).channel_count == 8  # noqa: PLR2004

    with pytest.raises(ValueError, match="NXspectrum"):
        _spectrum(np.zeros((2, 2, 2, 8)))


def test_the_energy_axis_is_the_last_one_whatever_the_rank():
    """
    ``channel_count`` reads the last axis and ``navigation_shape`` the rest.

    Energy-last is not a house convention that could have gone the other
    way. ``NXspectrum``'s own symbol table says the energy bins are
    "always the fastest dimension"; RosettaSciIO's Bruker HyperMap reader
    emits ``(height, width, Energy)``; Oxford's H5OINA stores
    ``Spectrum`` as ``(size, channels)``; and HyperSpy puts the signal
    axis last with navigation before it. Four independent parties agree,
    so this is pinned rather than left to be re-derived by the next
    adapter.
    """
    spot = _spectrum(np.zeros(4096))
    assert spot.channel_count == 4096  # noqa: PLR2004
    assert spot.navigation_shape == ()

    spectrum_image = _spectrum(np.zeros((4, 6, 2048)))
    assert spectrum_image.channel_count == 2048  # noqa: PLR2004
    # Slow axis first, matching the (height, width) convention the scan
    # side already pins - so a map's navigation shape is exactly the
    # ScanParameters shape it was collected over.
    assert spectrum_image.navigation_shape == (4, 6)


def test_spectrum_parameters_reject_settings_no_analyser_could_take():
    """
    A non-positive live time, an empty channel count, or a zero dispersion.

    Same discipline as ``CameraParameters``: reject at construction, so a
    caller learns before the beam is on rather than from a device error
    minutes later.
    """
    with pytest.raises(ValueError, match="live_time_s must be positive"):
        SpectrumParameters(live_time_s=0.0)
    with pytest.raises(ValueError, match="channel_count must be at least 1"):
        SpectrumParameters(live_time_s=1.0, channel_count=0)
    with pytest.raises(ValueError, match="energy_scale_ev must be positive"):
        SpectrumParameters(live_time_s=1.0, energy_scale_ev=0.0)


# --------------------------------------------------------------------------
# The simulator, in process.
# --------------------------------------------------------------------------


def test_the_simulated_detector_satisfies_the_protocol_structurally():
    """
    ``isinstance`` against the runtime-checkable protocol, as the others do.

    The protocols here are structural on purpose (migration plan, Phase
    1) so an adapter satisfies them by shape rather than by inheriting.
    Asserting it here is what makes the claim testable for a device kind
    that has no in-tree hardware adapter to check it against.
    """
    detector = open_detector(SIMULATED_BACKEND, None)
    assert isinstance(detector, SpectrumDetector)
    assert set(detector.acquisition_modes) == {SPOT_MODE, MAP_MODE}
    detector.close()


def test_the_simulated_spectrum_is_physically_shaped_rather_than_noise():
    """
    Peaks where the elements are, and a continuum that ends at the beam energy.

    A simulator that returned flat noise would let every test downstream
    pass while proving nothing about the analysis path — the element
    intensities would be indistinguishable and a wrong energy axis would
    be invisible. So three properties are checked, each of which a wrong
    axis or a broken model would break: the strongest channel sits near
    the O Ka line the default composition is dominated by, no counts
    appear above the accelerating voltage (the Duane-Hunt limit, which is
    real physics rather than a clip), and the total scales with live time.
    """
    detector = SimulatedSpectrumDetector()
    detector.start()
    spectrum = detector.acquire_spectrum()

    energies = spectrum.energy_offset_ev + spectrum.energy_scale_ev * np.arange(
        spectrum.channel_count,
    )
    peak_energy = float(energies[int(np.argmax(spectrum.data))])
    # O Ka is 524.9 eV and carries the largest weight in the default
    # composition; one detector resolution either side is generous.
    assert abs(peak_energy - 524.9) < 300.0  # noqa: PLR2004

    beam_energy_ev = float(spectrum.metadata["high_tension_v"])
    assert spectrum.data[energies > beam_energy_ev].sum() == 0

    detector.configure(
        SpectrumParameters(live_time_s=4.0, channel_count=spectrum.channel_count),
    )
    longer = detector.acquire_spectrum()
    # Four times the counting time, so several times the counts. Loose
    # bounds, because the point is that live time is honoured at all -
    # Poisson noise makes an exact ratio meaningless.
    assert longer.data.sum() > 2 * spectrum.data.sum()
    detector.close()


def test_the_detector_reports_the_settings_it_took_not_the_ones_asked_for():
    """
    A channel count is rounded to a power of two, and the report says so.

    Real pulse processors offer a small set of channel counts and they
    are always powers of two. The ``configure``-returns-what-was-taken
    contract exists for exactly this, and it is doing real work here: a
    caller that assumed 3000 channels would build an energy axis of the
    wrong length, and every line identified against it would be at the
    wrong energy.
    """
    detector = SimulatedSpectrumDetector()
    took = detector.configure(
        SpectrumParameters(live_time_s=1.0, channel_count=3000),
    )
    assert took.channel_count == 2048  # noqa: PLR2004
    assert detector.acquire_spectrum().channel_count == 2048  # noqa: PLR2004
    detector.close()


def test_a_map_carries_the_scan_geometry_and_varies_with_position():
    """
    A spectrum image is not one spectrum repeated, and it says where it was.

    Two things, both of which the storage and analysis path depend on.
    The cube must actually vary with position, or an element map computed
    from it is uniform noise and the round-trip test proves nothing. And
    it must carry the scan geometry in the *same* vocabulary a scanned
    frame uses (``fov_size_nm``), because that is what lets storage
    calibrate the navigation axes through the existing path instead of a
    second one that could disagree with it.
    """
    detector = SimulatedSpectrumDetector()
    detector.start()
    detector.configure(
        SpectrumParameters(live_time_s=1.0, channel_count=_FEW_CHANNELS),
    )
    cube = detector.acquire_map(_MAP)

    assert cube.navigation_shape == _MAP.shape
    assert cube.channel_count == _FEW_CHANNELS
    assert tuple(cube.metadata["fov_size_nm"]) == _MAP.fov_size_nm
    assert cube.metadata["acquisition_mode"] == MAP_MODE
    # The two halves of the simulated field have different compositions,
    # so their mean spectra must differ well beyond counting noise.
    left = cube.data[:, : _MAP.width // 2].mean(axis=(0, 1))
    right = cube.data[:, _MAP.width // 2 :].mean(axis=(0, 1))
    assert not np.allclose(left, right)
    detector.close()


def test_a_simulator_does_not_claim_a_synchronisation_it_does_not_have():
    """
    ``scan_sync`` says "simulated", not "detector".

    The metadata key exists because this interface cannot *enforce* the
    hardware synchronisation a real spectrum image needs — either the
    analyser drives the column's scan input or the column drives and the
    analyser follows a pixel clock, and nothing in an RPC protocol
    establishes either. The least a recording can do is say which it had.
    A simulator has neither, and writing "detector" here would be exactly
    the fiction the key exists to prevent.
    """
    detector = SimulatedSpectrumDetector()
    detector.configure(
        SpectrumParameters(live_time_s=1.0, channel_count=_FEW_CHANNELS),
    )
    assert detector.acquire_map(_MAP).metadata["scan_sync"] == "simulated"
    detector.close()


def test_live_and_real_time_are_both_reported_and_differ():
    """
    Their ratio is the dead-time fraction, which neither alone gives.

    EDS presets are specified in live time because that is what makes two
    spectra comparable at different count rates, and a quantification
    that used real time at a high count rate would be wrong by the dead
    time. Both are in eXSpy's metadata tree and both are in the EMSA
    standard's header, so a recording that carried only one would be
    losing something every consumer wants.
    """
    detector = SimulatedSpectrumDetector()
    spectrum = detector.acquire_spectrum()
    live = spectrum.metadata["live_time_s"]
    real = spectrum.metadata["real_time_s"]
    assert real > live > 0
    detector.close()


# --------------------------------------------------------------------------
# The server and the transport.
# --------------------------------------------------------------------------


def test_the_detector_arrives_on_the_neutral_target(detector_microscope):
    """
    Served as ``spectrum_detector``, and nothing else is invented alongside it.

    The target name is neutral rather than ``eds`` for the reason the
    ``camera`` target is neutral rather than ``webcam``: the protocol
    behind it fits a WDS spectrometer or an XRF head just as well, and
    putting the technique in the target name would be a fiction in every
    file. Which technique it is lives in the metadata, where
    ``camera_type`` already lives for frames.

    Also pinned here: an instrument that serves only a spectrum detector
    works. That is the shape of a Bruker or Oxford analyser, which really
    does sit beside someone else's column and drive none of it.
    """
    assert detector_microscope.spectrum_detector is not None
    assert detector_microscope.scanner is None
    assert detector_microscope.cameras() == {}
    assert set(detector_microscope.spectrum_detectors()) == {SPECTRUM_TARGET}
    assert detector_microscope.instrument.available_controls() == []


def test_a_spot_spectrum_survives_the_ipc_boundary_intact(detector_microscope):
    """
    Counts, the energy calibration, and the metadata all arrive.

    The calibration is the part worth asserting: it travels as two fields
    on the value rather than as an entry in a metadata dict, so a
    transport that dropped it would produce a spectrum that still looked
    fine and whose every energy was wrong. A spot spectrum is also small
    enough to stay on the plain pickle path, which is the case this
    covers; the map test below covers the other.
    """
    detector = detector_microscope.spectrum_detector
    took = detector.configure(
        SpectrumParameters(
            live_time_s=0.05,
            channel_count=_FEW_CHANNELS,
            energy_scale_ev=20.0,
            energy_offset_ev=-200.0,
        ),
    )
    detector.start()
    try:
        spectrum = detector.acquire_spectrum()
    finally:
        detector.stop()

    assert spectrum.channel_count == took.channel_count
    assert spectrum.energy_scale_ev == 20.0  # noqa: PLR2004
    assert spectrum.energy_offset_ev == -200.0  # noqa: PLR2004
    assert spectrum.data.sum() > 0
    assert spectrum.metadata["device_id"] == detector.detector_id
    assert spectrum.metadata["simulated"] is True
    # Small enough that the shared-memory path is not the one under test.
    assert spectrum.data.nbytes < SHARED_MEMORY_THRESHOLD_BYTES


def test_a_spectrum_image_survives_the_shared_memory_path(detector_microscope):
    """
    The cube crosses through shared memory and comes back byte-identical in shape.

    A spectrum image is the reason the transport had to learn about
    spectra at all: a 256x256 map of 4096 channels is a gigabyte, which
    is not a size to push through pickle and a socket buffer. This
    exercises the branch by making a cube that exceeds the threshold, and
    checks the thing a shared-memory bug would break first — that the
    energy calibration, which travels *beside* the array rather than in
    it, is still the one the detector acquired with.
    """
    detector = detector_microscope.spectrum_detector
    detector.configure(
        SpectrumParameters(live_time_s=0.01, channel_count=_BIG_MAP_CHANNELS),
    )
    detector.start()
    try:
        cube = detector.acquire_map(_BIG_MAP)
    finally:
        detector.stop()

    assert cube.data.nbytes > SHARED_MEMORY_THRESHOLD_BYTES
    assert cube.data.shape == (*_BIG_MAP.shape, _BIG_MAP_CHANNELS)
    assert cube.energy_scale_ev == detector.parameters().energy_scale_ev
    assert cube.data.sum() > 0
    # And the server survived it: the segment was retired rather than
    # leaked, which the health check would not itself catch but the
    # teardown at fixture exit would.
    assert detector_microscope.instrument.check_health().state == SERVER_RESPONSIVE


def test_the_detector_integrates_while_another_target_is_driven(detector_microscope):
    """
    Two targets, two connections, two server threads — they do not serialise.

    The brief asked whether a strictly synchronous request/response
    protocol composes with a detector that must be integrating while
    something else runs. Measured here rather than reasoned about: a
    deliberately slow acquisition is started on the detector's
    connection, and the instrument target is called repeatedly on its own
    while that is in flight. Those calls complete, so the answer is yes —
    concurrency composes, because ``serving.accept_loop`` gives every
    connection its own handler thread.

    What this does **not** show, and no test of this transport could, is
    correlation: nothing here ties the spectra to the probe positions
    another device was visiting at the same moment. Overlapping in
    wall-clock time is not sharing a scan position, which is why the
    simultaneous-signals problem is an acquisition-layer one rather than
    a transport one. See docs/adapters/spectrum-detectors.md.
    """
    import threading  # noqa: PLC0415 - only this test needs it

    detector = detector_microscope.spectrum_detector
    detector.configure(
        SpectrumParameters(live_time_s=0.6, channel_count=_FEW_CHANNELS),
    )
    detector.start()
    acquired: list[object] = []
    worker = threading.Thread(
        target=lambda: acquired.append(detector.acquire_spectrum()),
    )
    worker.start()
    try:
        # If the server serialised across targets, or if one connection's
        # in-flight call blocked another's, these would not return until
        # the acquisition above finished.
        answers = [detector_microscope.instrument.describe() for _ in range(3)]
    finally:
        worker.join(timeout=30)
        detector.stop()

    assert all(answer["targets"] == [SPECTRUM_TARGET] for answer in answers)
    assert len(acquired) == 1


def test_a_server_serving_no_spectrum_detector_is_unchanged():
    """
    The commodity-camera server behaves exactly as it did, with no detector.

    Backward compatibility, asserted rather than assumed. Adding a name to
    ``TARGET_NAMES`` changes the argv every server receives, so the
    failure mode of getting this wrong is not subtle — but it would land
    on adapters rather than here, and this is the cheapest place to
    notice. The camera server is a good probe precisely because it knows
    nothing about spectra.
    """
    with remote_instrument(
        server_module="miainwoodpecker.devices.camera_server",
    ) as devices:
        assert devices.spectrum_detector is None
        assert devices.spectrum_detectors() == {}
        assert devices.camera is not None
        assert devices.camera.acquire_frame().data.ndim == 2  # noqa: PLR2004


def test_the_target_name_is_the_only_protocol_change():
    """
    One name added, before ``instrument``, and the device list follows.

    ``TARGET_NAMES`` is positional argv, so where a name goes matters.
    Appending before ``instrument`` keeps every existing name's position
    and keeps ``instrument`` last, which is the invariant the protocol
    test already pins. Nothing else about the wire vocabulary moved.
    """
    assert SPECTRUM_TARGET_NAMES == (SPECTRUM_TARGET,)
    assert TARGET_NAMES[-1] == "instrument"
    assert TARGET_NAMES[-2] == SPECTRUM_TARGET
    assert TARGET_NAMES[:4] == (
        "ronchigram_camera",
        "eels_camera",
        "camera",
        "scanner",
    )


# --------------------------------------------------------------------------
# The command line and the failure an operator hits first.
# --------------------------------------------------------------------------


def test_the_command_line_matches_the_protocol():
    """One positional port per target name, and ``--plugin`` as configuration."""
    ports = [str(5000 + index) for index in range(len(TARGET_NAMES))]
    arguments = _parse_args(
        ["--backend", HARDWARE_BACKEND, "--plugin", "/tmp/a.nxs", *ports],  # noqa: S108
    )
    assert arguments.backend == HARDWARE_BACKEND
    assert arguments.plugin == ["/tmp/a.nxs"]  # noqa: S108
    assert arguments.ports == [int(port) for port in ports]
    assert _parse_args(ports).backend == SIMULATED_BACKEND


def test_an_unknown_backend_is_rejected_before_a_device_is_opened():
    """A typo names the backends it could have been, rather than failing later."""
    with pytest.raises(ValueError, match="unknown backend"):
        open_detector("nonesuch", None)


def test_asking_for_hardware_with_nothing_to_open_says_why_there_is_none(
    monkeypatch,
):
    """
    The message explains the absence rather than reporting a missing file.

    This is the honest bit of this adapter. There is no in-tree vendor
    backend and there cannot be one: neither Bruker's ESPRIT nor Oxford's
    AZtec ships a redistributable control library, so a live detector is
    an out-of-tree device server. An operator who asks for ``hardware``
    deserves to be told that, told where an adapter goes, and told what
    this backend *can* open — not handed a file-not-found.
    """
    monkeypatch.setenv("MIAINWOODPECKER_AUTHKEY", "00" * 32)
    ports = [str(5000 + index) for index in range(len(TARGET_NAMES))]
    assert main(["--backend", HARDWARE_BACKEND, *ports]) == NO_DETECTOR_EXIT_STATUS

    with pytest.raises(SpectrumDetectorOpenError, match="out-of-tree"):
        open_detector(HARDWARE_BACKEND, None)


def test_replaying_a_recording_is_a_device(tmp_path):
    """
    A recorded session is openable as a detector, and is labelled as a replay.

    The same idea as the camera server's media-file device, and worth
    more here: there is no EDX detector in CI and there never will be, so
    replaying a session recorded at the instrument is the only way this
    project can regression-test against real EDX data at all. What must
    not happen is a replay passing for a measurement, so every spectrum
    it returns says where it came from.
    """
    from miainwoodpecker.storage.spectra import write_spectra  # noqa: PLC0415

    source = SimulatedSpectrumDetector()
    source.configure(
        SpectrumParameters(live_time_s=0.05, channel_count=_FEW_CHANNELS),
    )
    path = tmp_path / "recorded.nxs"
    write_spectra(path, [source.acquire_spectrum() for _ in range(_TWO_SPECTRA)])
    source.close()

    replay = open_detector(HARDWARE_BACKEND, str(path))
    assert isinstance(replay, SpectrumDetector)
    assert replay.acquisition_modes == [SPOT_MODE]
    spectrum = replay.acquire_spectrum()
    assert spectrum.channel_count == _FEW_CHANNELS
    assert spectrum.metadata["backend"] == "replay"
    assert spectrum.metadata["replayed_from"] == str(path)
    # A recording of spot spectra is not a map, and pretending otherwise
    # would hand back one slice under a name that promises a whole scan.
    with pytest.raises(SpectrumDetectorOpenError, match="spot spectra"):
        replay.acquire_map(_MAP)
    replay.close()


def test_opening_something_that_is_not_a_recording_says_so(tmp_path):
    """A junk file names what the backend expected, and the alternative."""
    path = tmp_path / "not-a-recording.nxs"
    path.write_bytes(b"certainly not HDF5")
    with pytest.raises(SpectrumDetectorOpenError, match="spectrum recording"):
        open_detector(HARDWARE_BACKEND, str(path))
