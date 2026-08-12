"""
The EELS round trip: a dispersed camera frame to an eXSpy ``EELSSpectrum``.

The counterpart of ``test_hyperspy_eds.py``, and deliberately the same
shape: build the recording the device layer would write, read it back
through the adapter, and run a real eXSpy operation whose *answer*
depends on the energy axis having survived. What differs is where the
data comes from — an EEL spectrometer disperses onto a camera, so the
recording is a frame stack with a per-axis calibration, and the adapter
has to flatten it before eXSpy sees a spectrum at all.

The spectra here are synthesized rather than acquired, for one reason:
these are unit tests and the simulator lives behind the ``device`` extra
and a subprocess. That costs the strongest possible assertion about the
axis, so the strongest one is made where the simulator *is* available —
``tests/integration/test_remote_nion.py`` sweeps the real spectrometer
offset and checks that eXSpy still finds the zero-loss peak at 0 eV in
every frame. What is checked here is everything that does not need a
microscope: the signal type, the flattening, the unit normalization, the
metadata mapping, and the two refusals.

Skipped without the ``analysis`` extra, as every analysis test here is.
"""

from __future__ import annotations

import datetime

import numpy as np
import pytest

pytest.importorskip("hyperspy", reason="needs the 'analysis' extra")

from miainwoodpecker.analysis.hyperspy_bridge import (
    load_as_eels_signal,
    load_as_hyperspy_spectrum,
)
from miainwoodpecker.devices.interface import (
    Frame,
    Spectrum,
    SpectrumParameters,
)
from miainwoodpecker.devices.spectrum_server import SimulatedSpectrumDetector
from miainwoodpecker.storage.calibration import FrameCalibration
from miainwoodpecker.storage.nexus import write_frames
from miainwoodpecker.storage.spectra import write_spectra

# A 64x512 frame is the shape of the thing: one direction across the
# spectrometer slit, one dispersive. usim's real EELS camera is 256x1024
# at 0.5 eV/channel; this is the same arrangement, smaller.
_SLIT_ROWS = 64
_CHANNELS = 512
_DISPERSION_EV = 0.5
_OFFSET_EV = -30.0
# Where the zero-loss peak sits on the detector, in channels, given that
# calibration: channel 60 is (-30 + 0.5 * 60) = 0.0 eV.
_ZLP_CHANNEL = 60
_PLASMON_EV = 24.0
_HIGH_TENSION_V = 100_000.0
_BEAM_CURRENT_A = 2.0e-10
_EXPOSURE_MS = 125.0
_FRAMES = 3


def _dispersed_frame(index: int) -> Frame:
    """
    Build one EELS camera frame: a zero-loss peak and a plasmon, across a slit.

    The counts are laid out against the *physical* energy the recording's
    calibration describes, so the zero-loss peak really is at 0 eV and
    really is at channel 60 — which is what lets a test tell an adapter
    that carried the axis from one that lost half of it.

    Parameters
    ----------
    index : int
        Which frame of the series this is, used for its timestamp.

    Returns
    -------
    Frame
        A ``(_SLIT_ROWS, _CHANNELS)`` frame with the instrument state a
        Nion camera attaches to every frame it produces.
    """
    energy = _OFFSET_EV + _DISPERSION_EV * np.arange(_CHANNELS)
    zero_loss = 1000.0 * np.exp(-0.5 * (energy / 1.0) ** 2)
    plasmon = 120.0 * np.exp(-0.5 * ((energy - _PLASMON_EV) / 4.0) ** 2)
    spectrum = zero_loss + plasmon + 1.0
    return Frame(
        data=np.tile(spectrum, (_SLIT_ROWS, 1)),
        timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        + datetime.timedelta(seconds=index),
        metadata={
            "device_id": "eels_camera",
            "frame_index": index,
            "camera_type": "eels",
            "exposure_ms": _EXPOSURE_MS,
            "high_tension_v": _HIGH_TENSION_V,
            "beam_current_a": _BEAM_CURRENT_A,
        },
    )


@pytest.fixture
def eels_recording(tmp_path):
    """Write a short EELS camera series with an eV-calibrated fast axis."""
    path = tmp_path / "eels.nxs"
    write_frames(
        path,
        [_dispersed_frame(index) for index in range(_FRAMES)],
        calibration=FrameCalibration.spectrum(_DISPERSION_EV, offset=_OFFSET_EV),
    )
    return path


def test_the_camera_recording_loads_as_a_real_eels_signal(eels_recording):
    """
    An ``EELSSpectrum``, not a ``Signal1D`` that merely came from a spectrometer.

    The distinction is the whole reason this function exists. On a bare
    HyperSpy 2.x install ``set_signal_type("EELS")`` does nothing at all
    and says nothing about it — HyperSpy 2.0 moved EELS out to eXSpy, and
    a HyperSpy that has never seen eXSpy knows no signal types — so a
    caller would get an object that looks right, has none of
    ``align_zero_loss_peak``, ``estimate_thickness`` or ``EELSModel`` on
    it, and blames itself. Asserting the concrete class is what makes
    "this project has an EELS path" a checkable claim rather than a
    hopeful one.
    """
    exspy = pytest.importorskip("exspy", reason="needs exspy for EELS signal types")
    signal = load_as_eels_signal(eels_recording)
    assert isinstance(signal, exspy.signals.EELSSpectrum)
    # The camera's two directions become one navigation axis and one
    # signal axis: summed across the slit, dispersive direction kept.
    assert signal.axes_manager.signal_shape == (_CHANNELS,)
    assert signal.axes_manager.navigation_shape == (_FRAMES,)
    assert signal.axes_manager.navigation_axes[0].units == "s"


def test_the_energy_axis_survives_the_flattening_into_exspy(eels_recording):
    """
    Dispersion and offset reach the ``EELSSpectrum``, and eXSpy reads them.

    The load-bearing property, checked twice over: once against the
    recording's own numbers, and once through eXSpy itself, by asking it
    where the zero-loss peak is. The spectrum was built with the peak at
    0 eV, which is channel 60 of an axis starting at -30 eV — so an
    adapter that dropped the offset would report the ZLP at +30 eV, and
    one that dropped the dispersion would report it at channel 60. Only
    the true axis puts it at zero.

    ``estimate_zero_loss_peak_centre`` is eXSpy's own routine and knows
    nothing about this project; it reads ``axes_manager`` and reports in
    whatever units it finds there.
    """
    pytest.importorskip("exspy", reason="needs exspy for EELS signal types")
    signal = load_as_eels_signal(eels_recording)
    energy = signal.axes_manager.signal_axes[0]
    assert energy.units == "eV"
    assert energy.scale == pytest.approx(_DISPERSION_EV)
    assert energy.offset == pytest.approx(_OFFSET_EV)

    centres = np.asarray(signal.estimate_zero_loss_peak_centre().data)
    assert centres == pytest.approx(np.zeros(_FRAMES), abs=_DISPERSION_EV)
    # And the peak really is off-centre on the detector, so the assertion
    # above is not satisfied by an axis that happens to start at zero.
    assert int(np.argmax(signal.inav[0].data)) == _ZLP_CHANNEL


def test_a_spectrometer_recorded_in_millielectronvolts_arrives_in_electronvolts(
    tmp_path,
):
    """
    The unit normalization, and the reason it cannot be left to eXSpy.

    eXSpy's EDS side *validates* its axis unit — ``_get_line_energy``
    accepts eV and keV and raises otherwise — so a wrong unit there is
    loud. Its EELS side has no such check anywhere, while assuming eV
    throughout: the ionisation-edge table it matches against the axis is
    in eV, ``align_zero_loss_peak``'s subpixel window defaults to ±3 eV,
    and ``kramers_kronig_analysis`` works in eV. This project's energy
    vocabulary admits meV, which a monochromated vibrational spectrum is
    the natural use for, so the conversion has to happen on this side or
    not at all.

    The conversion is exact (a factor of a thousand between two metric
    spellings of one quantity), so the physics is untouched: the
    zero-loss peak stays in the channel it was recorded in and lands on
    the same 0 eV it would have had in an eV recording.
    """
    pytest.importorskip("exspy", reason="needs exspy for EELS signal types")
    path = tmp_path / "vibrational.nxs"
    write_frames(
        path,
        [_dispersed_frame(0)],
        # The same physical axis as the fixture's, spelled in meV:
        # 500 meV per channel from -30000 meV.
        calibration=FrameCalibration.spectrum(
            500.0,
            offset=-30_000.0,
            units="meV",
        ),
    )

    untyped = load_as_hyperspy_spectrum(path)
    assert untyped.axes_manager.signal_axes[0].units == "meV"

    signal = load_as_eels_signal(path)
    energy = signal.axes_manager.signal_axes[0]
    assert energy.units == "eV"
    assert energy.scale == pytest.approx(0.5)
    assert energy.offset == pytest.approx(-30.0)
    centre = float(np.asarray(signal.estimate_zero_loss_peak_centre().data).ravel()[0])
    assert centre == pytest.approx(0.0, abs=0.5)


def test_the_instrument_metadata_lands_where_exspy_looks_for_it(eels_recording):
    """
    Three items, two unit conversions, and eXSpy's own item paths.

    ``beam_energy`` is the conversion that matters most and the one most
    easily got wrong: eXSpy holds it in **keV** while this project holds
    accelerating voltage in **volts** everywhere, so 100 kV must arrive
    as 100.0. ``beam_current`` is the same class of error one step
    further from view — eXSpy's dose calculation reads it as **nA** and
    multiplies by 1e-9 to reach coulombs, so a 200 pA probe passed
    through as amps would be off by a factor of a billion with nothing
    saying so. The exposure is seconds, which is the unit RosettaSciIO's
    DigitalMicrograph reader maps this item from.
    """
    pytest.importorskip("exspy", reason="needs exspy for EELS signal types")
    signal = load_as_eels_signal(eels_recording)
    tem = signal.metadata.Acquisition_instrument.TEM

    assert tem.beam_energy == pytest.approx(100.0)
    assert tem.beam_current == pytest.approx(0.2)
    assert tem.Detector.EELS.exposure == pytest.approx(0.125)


def test_the_angles_nobody_recorded_are_absent_and_exspy_refuses_rather_than_guesses(
    eels_recording,
):
    """
    An unmeasured convergence angle stays unmeasured, and that is the safe state.

    Nothing in this project records a convergence or collection
    semi-angle: the collection angle is set by the spectrometer entrance
    aperture and camera length, which no device here reports, and the
    only convergence angle anywhere in this stack is a control that
    exists in the Nion *simulator* and in no Nion package outside it.
    Writing a plausible number for either would be inventing instrument
    geometry, and it would be invisible — a thickness corrected with
    someone else's collection angle is wrong by a factor that looks like
    a result.

    What makes leaving them out actively better than filling them in is
    the second half of this test: eXSpy checks for exactly these items
    and **refuses** the operations that need them. The relative
    thickness, which needs neither, still computes — so the refusal is
    specific to the missing geometry rather than a signal that does not
    work.
    """
    pytest.importorskip("exspy", reason="needs exspy for EELS signal types")
    signal = load_as_eels_signal(eels_recording)

    assert not signal.metadata.has_item("Acquisition_instrument.TEM.convergence_angle")
    assert not signal.metadata.has_item(
        "Acquisition_instrument.TEM.Detector.EELS.collection_angle",
    )
    # t/lambda by the log-ratio method: elastic intensity below the
    # threshold against the whole spectrum. The threshold is in the
    # axis's own units, which is another way the energy axis is
    # load-bearing - 10.0 here means 10 eV, between the zero-loss peak
    # and the plasmon.
    relative = signal.estimate_thickness(threshold=10.0)
    assert float(np.mean(relative.data)) > 0.0
    with pytest.raises(RuntimeError, match="microscope parameters are missing"):
        signal.estimate_thickness(threshold=10.0, density=2.2)


def test_an_edx_recording_is_refused_as_eels_rather_than_mislabelled(tmp_path):
    """
    The mirror of the EDS loader's refusal, and it matters for the same reason.

    Both spectroscopies end as the same ``Signal1D``, so once loaded
    nothing downstream can tell them apart: an EDX recording typed
    ``EELS`` would have eXSpy fitting ionisation edges to X-ray lines,
    quietly and plausibly. The two loaders therefore each refuse the
    other's on-disk layout, which is a question the file can answer —
    a spectrum recording's signal dataset is ``intensity`` and a frame
    recording's is ``data``.
    """
    pytest.importorskip("exspy", reason="needs exspy for EELS signal types")
    detector = SimulatedSpectrumDetector()
    detector.configure(SpectrumParameters(live_time_s=0.01, channel_count=512))
    detector.start()
    try:
        spectra: list[Spectrum] = [detector.acquire_spectrum()]
    finally:
        detector.close()
    path = tmp_path / "eds.nxs"
    write_spectra(path, spectra)

    with pytest.raises(ValueError, match="not EELS data"):
        load_as_eels_signal(path)


def test_a_camera_recording_with_no_energy_axis_is_refused(tmp_path):
    """
    A Ronchigram is not an EEL spectrum, and the refusal comes from the axes.

    The EELS loader adds no check of its own here: it inherits
    ``load_as_hyperspy_spectrum``'s, which refuses when the recording
    does not say which direction is energy rather than picking the longer
    axis. Pinned from this side too because it is the failure an operator
    is most likely to hit — pointing the EELS path at the other camera on
    the same column — and the message should be about the missing
    calibration rather than about eXSpy.
    """
    pytest.importorskip("exspy", reason="needs exspy for EELS signal types")
    path = tmp_path / "ronchigram.nxs"
    write_frames(
        path,
        [
            Frame(
                data=np.ones((16, 16)),
                timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            ),
        ],
        calibration=FrameCalibration.diffraction(0.2, units="mrad"),
    )
    with pytest.raises(ValueError, match="no single energy-calibrated axis"):
        load_as_eels_signal(path)
