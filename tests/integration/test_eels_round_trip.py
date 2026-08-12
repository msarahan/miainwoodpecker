"""
The whole EELS chain, end to end: spectrometer sweep to an eXSpy signal.

Every other test of the EELS path builds its spectrum from an array this
repository wrote, which can only ever check that the adapter is
self-consistent. This one does not: the counts come from the simulated
EEL spectrometer over the device IPC boundary, and the assertion is
against a fact about that instrument rather than about our arrays.

**The fact.** The zero-loss peak is, by definition, the electrons that
lost no energy — it sits at 0 eV whatever the spectrometer is doing.
Stepping the spectrometer's energy offset moves it across the detector
(that is the point of ``energy_offset_series``, and Nion's
``MultipleShiftEELSAcquire`` behind it) while the camera's own
calibration follows the same control, so the peak's *channel* changes by
hundreds and its *energy* does not change at all. The simulator
implements exactly this — it plots the zero-loss Gaussian at calibrated
energy zero on an axis whose offset is the ``ZLPoffset`` control — so
the two halves are independent of anything here.

**Why that is a real test and not a smoke test.** eXSpy's
``estimate_zero_loss_peak_centre`` reports the peak in the units it finds
on ``axes_manager``, so:

- an adapter that lost the axis offset would report the peak at
  ``-offset`` (+100, +60, +20 eV for the sweep below) instead of zero;
- an adapter that got the dispersion wrong by a factor *k* would report
  it at ``offset * (1 - k)`` — a wrong dispersion is invisible at
  offset 0 and shows up here precisely because the sweep is not centred;
- an adapter that dropped the calibration entirely would report a
  channel index, 40 to 200.

Only carrying both numbers through the flattening, the file, and the
signal type puts the peak at zero in all three. The peak's channel is
asserted too, so the test cannot pass by the peak simply not moving.

Needs both the ``device`` extra (the simulator, in a subprocess) and the
``analysis`` extra (HyperSpy and eXSpy); skipped without either.
"""

import numpy as np
import pytest

pytest.importorskip("nion.usim_device", reason="requires the 'device' extra")
pytest.importorskip("exspy", reason="requires the 'analysis' extra")

import exspy

from miainwoodpecker.acquisition import energy_offset_series, record
from miainwoodpecker.analysis.hyperspy_bridge import (
    load_as_eds_signal,
    load_as_eels_signal,
)
from miainwoodpecker.devices import (
    ENERGY_OFFSET_CONTROL,
    IMAGE_READOUT,
    PROJECTED_READOUT,
    CameraParameters,
)
from miainwoodpecker.devices.remote import remote_simulated_instrument
from miainwoodpecker.storage.nexus import write_frames
from miainwoodpecker.storage.spectra import EELS_TECHNIQUE, TECHNIQUE_KEY, read_spectra

# Three offsets, none of them zero and none of them equal, all negative
# enough to keep the zero-loss peak on the detector: at 0.5 eV/channel
# the peak lands at channel -offset/0.5, i.e. 200, 120 and 40 of 1024.
_OFFSETS_EV = [-100.0, -60.0, -20.0]
# usim's EELS camera dispersion, which the sentence above assumes.
_DISPERSION_EV = 0.5
# usim's accelerating voltage, in this project's volts and eXSpy's keV.
_HIGH_TENSION_V = 100_000.0
_BEAM_ENERGY_KEV = _HIGH_TENSION_V / 1000.0
_EXPOSURE_MS = 20.0


@pytest.fixture(scope="module")
def microscope():
    """Spawn one simulated device server for this module."""
    with remote_simulated_instrument() as instrument:
        yield instrument


def test_a_swept_eels_series_reaches_exspy_with_the_zero_loss_peak_still_at_zero(
    microscope,
    tmp_path_factory,
):
    """
    The peak moves across the detector by hundreds of channels and stays at 0 eV.

    See this module's docstring for why each of the three ways an adapter
    can mishandle the axis would break this, and for where the two facts
    being compared come from.

    One recording per step, deliberately: a NeXus frame stack carries
    **one** axis calibration for the whole stack, and these frames
    genuinely have different ones — that is what the sweep changes. Three
    files is what an energy-offset series honestly is until something
    models a per-frame axis.
    """
    instrument = microscope.instrument
    assert ENERGY_OFFSET_CONTROL in instrument.available_controls()
    original_offset_ev = instrument.energy_offset_ev()
    directory = tmp_path_factory.mktemp("eels_series")

    frames = list(
        energy_offset_series(
            microscope.eels_camera,
            _OFFSETS_EV,
            instrument=instrument,
        ),
    )
    assert instrument.energy_offset_ev() == pytest.approx(original_offset_ev)

    peak_channels = []
    for offset_ev, frame in zip(_OFFSETS_EV, frames, strict=True):
        path = directory / f"eels_{offset_ev:+.0f}eV.nxs"
        write_frames(path, [frame])

        signal = load_as_eels_signal(path)
        assert isinstance(signal, exspy.signals.EELSSpectrum)
        energy = signal.axes_manager.signal_axes[0]
        assert energy.units == "eV"
        assert energy.offset == pytest.approx(offset_ev)

        spectrum = np.asarray(signal.data).reshape(-1, energy.size)[0]
        peak_channels.append(int(np.argmax(spectrum)))
        # eXSpy's own routine, reading eXSpy's own axes_manager: the
        # zero-loss peak is where the physics says it is, in eV.
        centre = float(
            np.asarray(signal.estimate_zero_loss_peak_centre().data).ravel()[0],
        )
        assert centre == pytest.approx(0.0, abs=energy.scale)

    # The sweep really did move the peak: had it stayed put, the
    # assertion above would be about an axis that never had to work.
    expected = [round(-offset / 0.5) for offset in _OFFSETS_EV]
    assert peak_channels == pytest.approx(expected, abs=1)
    assert len(set(peak_channels)) == len(_OFFSETS_EV)


def test_the_instrument_state_the_sweep_recorded_reaches_exspys_metadata(
    microscope,
    tmp_path_factory,
):
    """
    Beam energy in keV, from the accelerating voltage the frame carried.

    The one conversion between this project's units and eXSpy's that a
    real acquisition can demonstrate rather than assert about a literal:
    the volts come from the instrument's own ``EHT`` control, through the
    frame metadata and the recording, and have to arrive as 100.0 rather
    than 100000.

    The two semi-angles eXSpy's EELS model also wants are checked to be
    *absent*, here where a real instrument is on the other end of the
    connection: usim publishes no collection angle at all, and the one
    convergence-angle control it does have exists in no Nion package
    outside the simulator, so recording it would be dressing a simulator
    detail as instrument state.
    """
    instrument = microscope.instrument
    directory = tmp_path_factory.mktemp("eels_metadata")
    path = directory / "eels.nxs"
    frames = list(
        energy_offset_series(
            microscope.eels_camera,
            [instrument.energy_offset_ev()],
            instrument=instrument,
        ),
    )
    write_frames(path, frames)

    signal = load_as_eels_signal(path)
    tem = signal.metadata.Acquisition_instrument.TEM
    assert tem.beam_energy == pytest.approx(_BEAM_ENERGY_KEV)
    assert tem.Detector.EELS.exposure > 0.0
    assert not signal.metadata.has_item("Acquisition_instrument.TEM.convergence_angle")
    assert not signal.metadata.has_item(
        "Acquisition_instrument.TEM.Detector.EELS.collection_angle",
    )


def test_a_projected_readout_reaches_exspy_through_the_edx_spectrum_layout(
    microscope,
    tmp_path_factory,
):
    """
    The same sweep, the same peak at 0 eV, through the *other* on-disk layout.

    This is the projected readout's round trip end to end, and it is the
    same fact under a different route: the camera sums its non-dispersive
    direction (server-side here, since Nion's ``sum_project`` is a
    sequence/SI mode — see ``test_nion_server``), ``record`` dispatches
    the 1D frames into ``SpectrumWriter``'s ``NXspectrum`` layout, and
    ``load_as_eels_signal`` reads them back with **nothing flattened**,
    because there is nothing left to flatten.

    The assertion is the same one the frame-stack test makes and for the
    same reasons: the zero-loss peak is at 0 eV by definition, stepping
    the spectrometer offset moves its *channel* by hundreds, and an
    adapter that dropped the offset, got the dispersion wrong, or lost
    the calibration entirely would report it at ``-offset``, at
    ``offset * (1 - k)``, or as a channel index. What is new is
    everything between the camera and eXSpy: a different writer, a
    different reader, and a 1D device frame that never becomes a stack.
    """
    instrument = microscope.instrument
    camera = microscope.eels_camera
    directory = tmp_path_factory.mktemp("eels_projected")
    original_offset_ev = instrument.energy_offset_ev()

    took = camera.configure(
        CameraParameters(exposure_ms=_EXPOSURE_MS, readout=PROJECTED_READOUT),
    )
    assert took.readout == PROJECTED_READOUT
    try:
        frames = list(
            energy_offset_series(camera, _OFFSETS_EV, instrument=instrument),
        )
    finally:
        # Leave the shared module-scoped camera as the other tests expect.
        camera.configure(CameraParameters(exposure_ms=_EXPOSURE_MS))
    assert camera.parameters().readout == IMAGE_READOUT

    peak_channels = []
    for offset_ev, frame in zip(_OFFSETS_EV, frames, strict=True):
        assert frame.data.ndim == 1
        assert frame.metadata["readout"] == PROJECTED_READOUT
        # Measured against usim, and the recording must not claim better.
        assert frame.metadata["projected_by"] == "server"

        path = directory / f"projected_{offset_ev:+.0f}eV.nxs"
        assert record([frame], path) == 1

        # It really is the EDX layout, not a frame stack that happens to
        # load: the spectrum reader opens it and the technique is stamped.
        recording = read_spectra(path)
        assert recording.data.shape == (1, frame.data.size)
        assert recording.metadata[0][TECHNIQUE_KEY] == EELS_TECHNIQUE
        # And the EDS loader refuses it, where the shape alone would not.
        with pytest.raises(ValueError, match="is an EELS spectrum recording"):
            load_as_eds_signal(path)

        signal = load_as_eels_signal(path)
        assert isinstance(signal, exspy.signals.EELSSpectrum)
        energy = signal.axes_manager.signal_axes[0]
        assert energy.units == "eV"
        assert energy.offset == pytest.approx(offset_ev)
        assert energy.size == frame.data.size

        spectrum = np.asarray(signal.data).reshape(-1, energy.size)[0]
        peak_channels.append(int(np.argmax(spectrum)))
        centre = float(
            np.asarray(signal.estimate_zero_loss_peak_centre().data).ravel()[0],
        )
        assert centre == pytest.approx(0.0, abs=energy.scale)

    assert instrument.energy_offset_ev() == pytest.approx(original_offset_ev)
    expected = [round(-offset / _DISPERSION_EV) for offset in _OFFSETS_EV]
    assert peak_channels == pytest.approx(expected, abs=1)
    assert len(set(peak_channels)) == len(_OFFSETS_EV)
