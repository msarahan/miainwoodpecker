"""
The Gatan bridge's own devices, without a socket in the way.

``test_attached_server.py`` drives this bridge over the wire, which is
where the transport claims are proved. These are the claims that are
about the *adapter* rather than the transport, and they are the ones a
socket test would obscure: what the simulated spectrometer camera does
when its binning changes, what the hardware backend does when it is not
inside Gatan Microscopy Suite, and what the instrument target refuses to
pretend about.

Nothing here needs any Gatan software. That is the point of the simulated
backend existing at all — the same reason ``nionswift-usim`` exists for
the Nion side and the synthesised frames exist for the commodity-camera
server.
"""

from __future__ import annotations

import numpy as np
import pytest

from miainwoodpecker.devices import Camera, InstrumentController
from miainwoodpecker.devices.gatan_bridge import (
    BridgeInstrument,
    DigitalMicrographCamera,
    DigitalMicrographSpectrometer,
    GatanUnavailableError,
    SimulatedEELSCamera,
    SimulatedSpectrometer,
    _build_targets,
)
from miainwoodpecker.devices.interface import (
    IMAGE_READOUT,
    PROJECTED_READOUT,
    CameraParameters,
)

_DISPERSION_EV = 0.5
_BINNING = 4
_OFFSET_EV = 20.0


def test_the_simulated_camera_satisfies_the_neutral_camera_protocol():
    """
    The bridge's camera is a ``Camera`` in-process, not only once wrapped.

    Worth checking here and not only over IPC, because the whole design
    rests on an adapter author writing protocol-satisfying objects and
    never thinking about transport. If these objects only satisfied the
    protocols once wrapped by the remote client, that claim would be
    false and the in-process path this project keeps for testing would
    quietly not work for this adapter.
    """
    camera = SimulatedEELSCamera(SimulatedSpectrometer(_DISPERSION_EV))
    assert isinstance(camera, Camera)


def test_a_one_control_instrument_cannot_satisfy_instrumentcontroller():
    """
    Pinning a real gap in the interfaces, rather than working around it.

    ``InstrumentController`` is all-or-nothing to ``isinstance``: a
    ``runtime_checkable`` protocol checks that every method is present,
    so an instrument target with one genuine control and no stage, no
    defocus and no blanker does **not** satisfy it — even though
    ``available_controls()`` exists precisely so that partial instruments
    are supported, and even though this one works perfectly over the
    protocol. The commodity-camera server's ``ServerInstrument`` has the
    same property, so this is the second adapter to hit it rather than a
    quirk of the first.

    That is a finding worth a test rather than a fix here. The workable
    resolutions — splitting the protocol into per-control pieces, or
    dropping the unimplemented methods from it entirely — are interface
    changes that should land with a caller that needs them, and this test
    exists so the next person meets the fact deliberately instead of
    reading an ``isinstance`` failure as a broken adapter.
    """
    import threading  # noqa: PLC0415 - one line, only this test needs it

    spectrometer = SimulatedSpectrometer(_DISPERSION_EV)
    instrument = BridgeInstrument({}, spectrometer, threading.Event(), "simulated")
    assert not isinstance(instrument, InstrumentController)
    # But every method it *does* claim works, which is what the protocol
    # boundary actually depends on: available_controls() is the contract,
    # and it is honest.
    assert list(instrument.available_controls()) == ["energy_offset"]


def test_binning_multiplies_the_energy_dispersion():
    """
    Binning a spectrometer camera changes the eV per channel, not just the shape.

    The contract ``CameraParameters`` exists to keep coherent — binning
    and exposure are one value object because binning multiplies the
    calibration scale — and on a *dispersive* axis getting it wrong is
    not a cosmetic error: every energy in the recording would be off by
    the binning factor, which is the kind of mistake that survives into
    published numbers.
    """
    spectrometer = SimulatedSpectrometer(_DISPERSION_EV)
    camera = SimulatedEELSCamera(spectrometer)
    camera.start()
    unbinned = camera.acquire_frame()
    camera.configure(CameraParameters(exposure_ms=50.0, binning=_BINNING))
    binned = camera.acquire_frame()
    camera.stop()

    assert unbinned.metadata["calibration"]["x"]["scale"] == _DISPERSION_EV
    assert binned.metadata["calibration"]["x"]["scale"] == _DISPERSION_EV * _BINNING
    assert binned.data.shape[1] * _BINNING == unbinned.data.shape[1]


def test_consecutive_frames_differ_and_a_held_frame_does_not_change():
    """
    The frame-identity contracts hold against this simulator too.

    Ported from Nion's suite and applied here for the same reason the
    commodity-camera server's simulator produces moving frames: a
    simulator that returned one constant array would make both contracts
    vacuously true and would let a real buffer-reuse bug through.
    """
    camera = SimulatedEELSCamera(SimulatedSpectrometer(_DISPERSION_EV))
    camera.start()
    first = camera.acquire_frame()
    held = first.data.copy()
    second = camera.acquire_frame()
    camera.stop()
    assert not np.array_equal(first.data, second.data)
    assert np.array_equal(first.data, held)
    assert second.metadata["frame_index"] == first.metadata["frame_index"] + 1


def test_the_hardware_backend_says_why_it_cannot_run_here():
    """
    Outside GMS, the hardware backend fails with the vendor's own reason.

    This is the error every user of this module meets first, because the
    natural thing to try is running it like every other device server —
    from a shell. The message has to say that no install fixes it, since
    the obvious next move otherwise is to go looking for a package called
    ``DigitalMicrograph`` on PyPI.
    """
    with pytest.raises(GatanUnavailableError, match="inside a running Gatan"):
        _build_targets("hardware", _DISPERSION_EV)


def test_the_front_image_path_refuses_binning_it_cannot_set():
    """
    Reading DM's live view means DM owns the binning, and this says so.

    Accepting the request and reporting success would be the damaging
    choice: ``configure`` returning what it was given is the contract
    callers rely on to know what a frame was taken under, so a bridge
    that lied here would put a wrong binning and a wrong dispersion in
    every frame's metadata while the camera carried on unbinned.
    """
    camera = DigitalMicrographCamera(DigitalMicrographSpectrometer(_DISPERSION_EV))
    assert list(camera.binning_values) == [1]
    with pytest.raises(ValueError, match="DigitalMicrograph"):
        camera.configure(CameraParameters(exposure_ms=50.0, binning=2))


def test_the_instrument_target_offers_only_the_control_it_has():
    """
    A spectrometer is not a microscope, and the bridge does not pretend it is.

    A Gatan spectrometer has no stage, no defocus and no beam blanker;
    those belong to whichever column vendor's software is running next
    door. Reporting one control is the honest answer the protocol already
    supports, and ``park`` doing nothing matters more than it looks:
    on the Nion path ``park`` blanks the beam and is a safety property,
    so a bridge whose ``park`` silently did nothing *while claiming a
    blanker* would be the worst of both.
    """
    import threading  # noqa: PLC0415 - one line, only this test needs it

    spectrometer = SimulatedSpectrometer(_DISPERSION_EV)
    instrument = BridgeInstrument({}, spectrometer, threading.Event(), "simulated")
    assert list(instrument.available_controls()) == ["energy_offset"]
    instrument.set_energy_offset_ev(_OFFSET_EV)
    assert instrument.energy_offset_ev() == _OFFSET_EV
    instrument.park()  # does nothing, and must not raise
    assert not hasattr(instrument, "set_beam_blanked")
    assert not hasattr(instrument, "set_stage_position_nm")


def test_shutdown_releases_the_devices_without_claiming_to_stop_the_host():
    """
    Ending a session must not mean ending DigitalMicrograph.

    The one place this bridge's semantics differ from a spawned server's,
    and the difference is not negotiable: a spawned server exits its
    process on ``shutdown``, and doing the equivalent here would terminate
    the application the bridge lives in — with somebody else's acquisition
    in it. So the report says what the session released, the stop event is
    set, and the host is left alone.
    """
    import threading  # noqa: PLC0415 - one line, only this test needs it

    spectrometer = SimulatedSpectrometer(_DISPERSION_EV)
    camera = SimulatedEELSCamera(spectrometer)
    stop = threading.Event()
    targets: dict[str, object] = {"eels_camera": camera}
    instrument = BridgeInstrument(targets, spectrometer, stop, "simulated")
    targets["instrument"] = instrument

    assert instrument.health()["open_devices"] == ["eels_camera"]
    report = instrument.shutdown()
    assert report["closed"] == ["eels_camera"]
    assert report["errors"] == []
    # No beam was blanked, and the report says so rather than omitting it:
    # a reader of a teardown record should not have to infer that this
    # adapter has no blanker.
    assert report["beam_blanked"] is False
    assert stop.is_set()


def test_the_gatan_bridge_refuses_a_projected_readout_rather_than_imaging():
    """
    A camera that cannot project says so; it does not quietly send a picture.

    This bridge stands in for DM's front-image path, which delivers
    whatever DigitalMicrograph is already showing — so a projection is
    DM's to perform in its own spectrum modes, and accepting the request
    here would answer an operator asking for a spectrum with an image
    whose metadata claimed to be one.
    """
    camera = SimulatedEELSCamera(SimulatedSpectrometer(_DISPERSION_EV))
    with pytest.raises(ValueError, match="is not supported by"):
        camera.configure(
            CameraParameters(exposure_ms=10.0, readout=PROJECTED_READOUT),
        )
    assert camera.parameters().readout == IMAGE_READOUT
