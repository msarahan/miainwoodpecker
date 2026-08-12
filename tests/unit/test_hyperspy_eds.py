"""
The full EDX round trip: simulated acquisition to a HyperSpy EDS signal.

This is the test the whole spectrum-detector shape exists to make
possible, and it is deliberately end to end rather than three unit tests
about its pieces: acquire from the simulated detector, write a NeXus
file, read it back as an ``EDSTEMSpectrum``, and run a real eXSpy
operation on it. Every layer in between gets exercised for real, and any
of them putting the energy axis a channel out would show up as lines
identified at the wrong energies rather than as a passing shape check.

Also covered here is the structural point the project owner made about
where EELS and EDX converge: **one** loader reads both layouts. An EELS
spectrometer disperses onto a camera and is stored as a frame stack; an
EDX detector is natively 1D and is stored in ``NXspectrum``'s layout;
both reach ``Signal1D`` through ``load_as_hyperspy_spectrum``, and the
EELS path's behaviour is asserted here to be unchanged by the addition.

Skipped without the ``analysis`` extra, as every analysis test here is.
"""

from __future__ import annotations

import datetime

import numpy as np
import pytest

pytest.importorskip("hyperspy", reason="needs the 'analysis' extra")

from miainwoodpecker.analysis.hyperspy_bridge import (
    load_as_eds_signal,
    load_as_hyperspy_spectrum,
)
from miainwoodpecker.devices.interface import (
    Frame,
    ScanParameters,
    Spectrum,
    SpectrumParameters,
)
from miainwoodpecker.devices.spectrum_server import (
    SimulatedSpectrumDetector,
)
from miainwoodpecker.storage.calibration import FrameCalibration
from miainwoodpecker.storage.nexus import write_frames
from miainwoodpecker.storage.spectra import write_spectra

_CHANNELS = 2048
_SPECTRA = 3
_LIVE_TIME_S = 0.5
_SCALE_EV = 10.0
_OFFSET_EV = -480.0
_MAP = ScanParameters(height=3, width=4, pixel_time_us=5000.0, fov_nm=120.0)
# What the simulator's default composition is dominated by, strongest
# first. The ordering, not the values, is what a correct energy axis
# guarantees - a shifted axis assigns these intensities to the wrong
# elements and the ordering breaks.
_EXPECTED_ORDER = ["O_Ka", "Si_Ka", "Al_Ka", "Fe_Ka"]


@pytest.fixture
def spot_recording(tmp_path):
    """Acquire a few spot spectra from the simulator and record them."""
    detector = SimulatedSpectrumDetector()
    # Every setting, not only the two being changed: SpectrumParameters is
    # one value object precisely because a channel count and an energy
    # calibration that disagree describe an axis the detector never used,
    # so `configure` replaces the set rather than patching it. Writing the
    # offset out here is what that costs, and it is the right cost.
    detector.configure(
        SpectrumParameters(
            live_time_s=_LIVE_TIME_S,
            channel_count=_CHANNELS,
            energy_scale_ev=_SCALE_EV,
            energy_offset_ev=_OFFSET_EV,
        ),
    )
    detector.start()
    try:
        spectra = [detector.acquire_spectrum() for _ in range(_SPECTRA)]
    finally:
        detector.close()
    path = tmp_path / "eds.nxs"
    write_spectra(path, spectra)
    return path


def test_a_recording_reaches_hyperspy_with_the_energy_axis_it_was_acquired_with(
    spot_recording,
):
    """
    Signal axis in eV, with the detector's own dispersion and offset.

    The load-bearing assertion of the whole change. eV is also the unit
    eXSpy actually accepts — measured in ``exspy.signals.eds``, whose
    ``_get_line_energy`` handles ``"eV"`` and ``"keV"`` and raises for
    anything else — so this is not merely round-tripping a string, it is
    the difference between a signal eXSpy can compute on and one it
    refuses.
    """
    signal = load_as_hyperspy_spectrum(spot_recording)
    energy = signal.axes_manager.signal_axes[0]
    assert energy.units == "eV"
    assert energy.size == _CHANNELS
    assert energy.scale == pytest.approx(10.0)
    assert energy.offset == pytest.approx(-480.0)
    # A series of spot spectra navigates by index, not by time: the
    # recording says which spectrum each one is, and calling that a clock
    # would be inventing one.
    navigation = signal.axes_manager.navigation_axes[0]
    assert navigation.size == _SPECTRA
    assert navigation.name == "spectrum_index"
    assert navigation.units == ""


def test_the_recording_loads_as_a_real_eds_signal_and_finds_the_right_lines(
    spot_recording,
):
    """
    ``EDSTEMSpectrum``, with metadata, and line intensities in the right order.

    The end of the round trip, and checked by *ordering* rather than by
    absolute counts, which is what makes it a real test. The simulator's
    default composition weights oxygen above silicon above aluminium and
    iron; ``get_lines_intensity`` integrates under each line at the
    energy eXSpy believes it to be, using the axis this file carries. An
    energy axis that was off by even a few channels would assign those
    integrals to the wrong places and the ordering would break, so this
    passes only if every layer agreed about the axis.
    """
    exspy = pytest.importorskip("exspy", reason="needs exspy for EDS signal types")
    signal = load_as_eds_signal(spot_recording)
    assert isinstance(signal, exspy.signals.EDSTEMSpectrum)

    signal.add_elements(["O", "Si", "Al", "Fe"])
    intensities = {
        line.metadata.Sample.xray_lines[0]: float(np.mean(line.data))
        for line in signal.get_lines_intensity()
    }
    ranked = sorted(intensities, key=intensities.get, reverse=True)
    assert ranked == _EXPECTED_ORDER


def test_the_detector_metadata_lands_where_exspy_looks_for_it(spot_recording):
    """
    EXSpy's own item paths, and the one unit conversion that has to happen.

    Without these, ``get_lines_intensity`` and every quantification fall
    back on eXSpy's *defaults* for the detector geometry — which are
    plausible numbers for some other instrument, and silently so. The
    mapping is the EMSA standard's, which RosettaSciIO's ``msa`` reader
    already maps onto exactly these items, so nothing here is this
    project's invention.

    The conversion worth pinning is the beam energy: eXSpy holds it in
    **keV** and this project holds accelerating voltage in **volts**
    everywhere, so 100 kV must arrive as 100.0 and not as 100000. That is
    the class of error that makes every overvoltage check wrong while the
    file still looks fine.
    """
    pytest.importorskip("exspy", reason="needs exspy for EDS signal types")
    signal = load_as_eds_signal(spot_recording)
    eds = signal.metadata.Acquisition_instrument.TEM.Detector.EDS

    assert signal.metadata.Acquisition_instrument.TEM.beam_energy == pytest.approx(
        100.0,
    )
    assert eds.live_time == pytest.approx(_LIVE_TIME_S)
    assert eds.real_time > eds.live_time
    assert eds.azimuth_angle == pytest.approx(45.0)
    assert eds.elevation_angle == pytest.approx(18.0)
    assert eds.solid_angle == pytest.approx(0.7)
    assert eds.energy_resolution_MnKa == pytest.approx(129.0)
    assert eds.EDS_det == "sdd"


def test_the_beam_current_arrives_in_the_nanoamps_exspy_reads_it_as(tmp_path):
    """
    Amps to nanoamps, the conversion that was missing and was invisible.

    eXSpy holds ``Acquisition_instrument.TEM.beam_current`` in **nA**:
    its dose calculation (``exspy/signals/eds_tem.py``) multiplies the
    stored value by 1e-9 to reach coulombs, with a comment saying that is
    because the current is in nA. This project holds current in amps
    everywhere, as it holds voltage in volts, so a 200 pA probe written
    through unconverted arrived as 2e-10 nA — every dose-based
    quantification wrong by a factor of a billion, with nothing at either
    end range-checking it.

    Nothing caught it because nothing in the suite computed a dose, which
    is exactly why the assertion is on the stored value rather than on a
    downstream result: it is the conversion itself that has to be pinned.
    The detector simulator reports no beam current, so this builds the
    spectrum rather than acquiring it — the value under test comes from
    the instrument, not from the detector.
    """
    pytest.importorskip("exspy", reason="needs exspy for EDS signal types")
    path = tmp_path / "with_beam_current.nxs"
    write_spectra(
        path,
        [
            Spectrum(
                data=np.ones(256, dtype=np.uint32),
                timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
                energy_offset_ev=0.0,
                energy_scale_ev=10.0,
                metadata={"high_tension_v": 100_000.0, "beam_current_a": 2.0e-10},
            ),
        ],
    )

    signal = load_as_eds_signal(path)
    tem = signal.metadata.Acquisition_instrument.TEM
    assert tem.beam_energy == pytest.approx(100.0)
    assert tem.beam_current == pytest.approx(0.2)


def test_a_spectrum_image_arrives_with_two_navigation_axes_in_nanometres(tmp_path):
    """
    A map is navigation over the scan and signal over energy, calibrated both ways.

    HyperSpy orders navigation axes fastest-first, against this project's
    ``(y, x)`` dataset order, and getting that transposition wrong swaps
    the two spatial scales — invisible on a square map and wrong on every
    other. The frame adapter already handles the same trap for signal
    axes; this pins it for navigation axes too, on a deliberately
    non-square map.
    """
    pytest.importorskip("exspy", reason="needs exspy for EDS signal types")
    detector = SimulatedSpectrumDetector()
    detector.configure(SpectrumParameters(live_time_s=0.01, channel_count=512))
    detector.start()
    try:
        cube = detector.acquire_map(_MAP)
    finally:
        detector.close()
    path = tmp_path / "map.nxs"
    write_spectra(path, [cube])

    signal = load_as_eds_signal(path)
    assert signal.axes_manager.navigation_shape == (_MAP.width, _MAP.height)
    x_axis, y_axis = signal.axes_manager.navigation_axes
    assert x_axis.name == "x"
    assert y_axis.name == "y"
    assert x_axis.units == "nm"
    assert x_axis.scale == pytest.approx(_MAP.pixel_size_nm)
    assert y_axis.scale == pytest.approx(_MAP.pixel_size_nm)
    assert signal.axes_manager.signal_axes[0].units == "eV"


def test_one_loader_reads_both_the_camera_path_and_the_detector_path(
    tmp_path,
    spot_recording,
):
    """
    An EELS frame stack and an EDX recording reach ``Signal1D`` the same way.

    The structural point, made executable. EELS disperses onto a camera,
    so it stays a ``Frame`` with a per-axis calibration and is flattened
    on load; EDX is natively 1D and needs no flattening. Two device
    paths, two storage layouts, **one** analysis entry point — because
    two functions returning the same type from two layouts would diverge
    the first time either grew an option.

    The EELS behaviour asserted here is the pre-existing one, unchanged:
    sum along the non-dispersive direction, keep the frame axis as
    navigation in seconds, and carry the recording's own energy
    calibration onto the signal axis.
    """
    eels_path = tmp_path / "eels.nxs"
    write_frames(
        eels_path,
        [
            Frame(
                data=np.ones((8, 32)),
                timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
                + datetime.timedelta(seconds=index),
            )
            for index in range(2)
        ],
        calibration=FrameCalibration.spectrum(0.5, offset=-20.0),
    )

    eels = load_as_hyperspy_spectrum(eels_path)
    eds = load_as_hyperspy_spectrum(spot_recording)

    assert type(eels) is type(eds)
    # The camera path: flattened across the slit, energy on the fast axis.
    assert eels.axes_manager.signal_axes[0].size == 32  # noqa: PLR2004
    assert eels.axes_manager.signal_axes[0].units == "eV"
    assert eels.axes_manager.signal_axes[0].scale == pytest.approx(0.5)
    assert eels.axes_manager.navigation_axes[0].units == "s"
    # The detector path: nothing was flattened, because nothing was 2D.
    assert eds.axes_manager.signal_axes[0].size == _CHANNELS


def test_a_frame_recording_is_refused_as_eds_rather_than_mislabelled(tmp_path):
    """
    An EELS stack is not EDS data, and typing it as such would be a lie.

    Both end up as 1D spectra, which is exactly why this refusal matters:
    the two are indistinguishable once loaded, so nothing downstream
    would catch an EELS recording wearing ``EDS_TEM`` and eXSpy would
    happily fit X-ray lines to electron energy losses.
    """
    pytest.importorskip("exspy", reason="needs exspy for EDS signal types")
    path = tmp_path / "eels.nxs"
    write_frames(
        path,
        [
            Frame(
                data=np.ones((8, 32)),
                timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            ),
        ],
        calibration=FrameCalibration.spectrum(0.5),
    )
    with pytest.raises(ValueError, match="not EDS data"):
        load_as_eds_signal(path)
