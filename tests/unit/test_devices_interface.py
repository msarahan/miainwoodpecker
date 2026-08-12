"""Tests for the vendor-neutral device interfaces."""

import datetime
import typing

import numpy as np
import pytest

from miainwoodpecker.devices import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    IMAGE_READOUT,
    PROJECTED_READOUT,
    READOUT_MODES,
    STAGE_POSITION_CONTROL,
    Camera,
    CameraParameters,
    Frame,
    InstrumentController,
    ScanParameters,
    Scanner,
)


class _FakeCamera:
    """Minimal in-memory implementation used to exercise the Camera protocol."""

    def __init__(self) -> None:
        self.running = False
        self.closed = False
        self._parameters = CameraParameters(exposure_ms=10.0)

    @property
    def camera_id(self) -> str:
        """Return the fake camera's id."""
        return "fake_camera"

    @property
    def binning_values(self) -> tuple[int, ...]:
        """Return the binning factors this fake supports."""
        return (1, 2)

    def parameters(self) -> CameraParameters:
        """Return the settings the next frame would use."""
        return self._parameters

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Record new settings and report them back.

        Parameters
        ----------
        parameters : CameraParameters
            The requested exposure and binning.

        Returns
        -------
        CameraParameters
            The same settings; this fake rounds nothing.
        """
        self._parameters = parameters
        return self._parameters

    def start(self) -> None:
        """Mark acquisition as running."""
        self.running = True

    def stop(self) -> None:
        """Mark acquisition as paused."""
        self.running = False

    def acquire_frame(self) -> Frame:
        """Return a constant 4x4 frame."""
        return Frame(
            data=np.zeros((4, 4), dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={"frame_number": 1},
        )

    def close(self) -> None:
        """Mark the device as closed."""
        self.closed = True


class _FakeScanner:
    """Minimal in-memory implementation used to exercise the Scanner protocol."""

    @property
    def scanner_id(self) -> str:
        """Return the fake scanner's id."""
        return "fake_scanner"

    @property
    def channel_names(self) -> typing.Sequence[str]:
        """Return the fake channel names."""
        return ["HAADF", "MAADF"]

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Return a zero-filled frame of the requested shape."""
        return Frame(
            data=np.zeros(parameters.shape, dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={"channel_index": channel},
        )

    def close(self) -> None:
        """Release nothing; the fake owns no resources."""


class _FakeInstrument:
    """Minimal in-memory implementation of the InstrumentController protocol."""

    def __init__(self, *, controls: typing.Sequence[str] | None = None) -> None:
        self._controls = (
            list(controls)
            if controls is not None
            else [
                STAGE_POSITION_CONTROL,
                DEFOCUS_CONTROL,
                BEAM_BLANKER_CONTROL,
                ENERGY_OFFSET_CONTROL,
            ]
        )
        self.position_nm = (0.0, 0.0)
        self.defocus = 500.0
        self.energy_offset = -20.0
        self.blanked = False
        self.park_count = 0

    def stage_size_nm(self) -> float:
        """Return a fake stage extent."""
        return 1000.0

    def available_controls(self) -> typing.Sequence[str]:
        """Return the controls this fake claims to implement."""
        return self._controls

    def stage_position_nm(self) -> tuple[float, float]:
        """Return the last position written."""
        return self.position_nm

    def set_stage_position_nm(self, y_nm: float, x_nm: float) -> None:
        """Record an absolute stage move."""
        self.position_nm = (y_nm, x_nm)

    def defocus_nm(self) -> float:
        """Return the last defocus written."""
        return self.defocus

    def set_defocus_nm(self, defocus_nm: float) -> None:
        """Record a defocus change."""
        self.defocus = defocus_nm

    def energy_offset_ev(self) -> float:
        """Return the last energy offset written."""
        return self.energy_offset

    def set_energy_offset_ev(self, offset_ev: float) -> None:
        """
        Record an energy-offset change.

        Parameters
        ----------
        offset_ev : float
            Requested offset, in electronvolts.
        """
        self.energy_offset = offset_ev

    def is_beam_blanked(self) -> bool:
        """Return whether the fake beam is blanked."""
        return self.blanked

    def set_beam_blanked(self, *, blanked: bool) -> None:
        """Record a blanker change."""
        self.blanked = blanked

    def park(self) -> None:
        """Blank the beam, as a real park does."""
        self.park_count += 1
        self.blanked = True


def test_fake_camera_satisfies_camera_protocol():
    """A structural implementation is recognized by the runtime-checkable protocol."""
    assert isinstance(_FakeCamera(), Camera)


def test_fake_scanner_satisfies_scanner_protocol():
    """A structural implementation is recognized by the runtime-checkable protocol."""
    assert isinstance(_FakeScanner(), Scanner)


def test_scan_parameters_shape_is_height_then_width():
    """The shape property follows the numpy (rows, columns) convention."""
    parameters = ScanParameters(height=32, width=48, pixel_time_us=1.0, fov_nm=100.0)
    expected_shape = (32, 48)
    assert parameters.shape == expected_shape


def test_frame_metadata_defaults_to_empty_mapping():
    """A Frame constructed without metadata has an empty mapping, not None."""
    frame = Frame(
        data=np.zeros((2, 2), dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
    )
    assert frame.metadata == {}


def test_scan_frame_uses_requested_shape():
    """The fake scanner honors ScanParameters.shape, pinning the convention."""
    parameters = ScanParameters(height=8, width=16, pixel_time_us=1.0, fov_nm=50.0)
    frame = _FakeScanner().scan_frame(parameters)
    assert frame.data.shape == parameters.shape


def test_fake_instrument_satisfies_instrument_controller_protocol():
    """A structural implementation is recognized by the runtime-checkable protocol."""
    assert isinstance(_FakeInstrument(), InstrumentController)


def test_stage_position_is_y_then_x_like_scan_parameters_shape():
    """Positions use the same (slow, fast) axis order as a frame's shape."""
    instrument = _FakeInstrument()
    instrument.set_stage_position_nm(3.0, 7.0)
    expected = (3.0, 7.0)
    assert instrument.stage_position_nm() == expected


def test_park_blanks_the_beam():
    """Parking leaves the beam blanked, which is the whole point of the hook."""
    instrument = _FakeInstrument()
    assert not instrument.is_beam_blanked()
    instrument.park()
    assert instrument.is_beam_blanked()


def test_available_controls_can_report_a_partial_instrument():
    """An instrument without a blanker says so rather than failing on use."""
    instrument = _FakeInstrument(controls=[DEFOCUS_CONTROL])
    assert BEAM_BLANKER_CONTROL not in instrument.available_controls()
    assert isinstance(instrument, InstrumentController)


def test_camera_parameters_default_to_the_image_readout():
    """
    The default readout is today's behaviour, so nothing existing changes shape.

    ``readout`` arrived after every caller and every adapter, so its
    default has to be the mode they were all written against; a default
    of ``projected`` would silently turn every existing camera call into
    a 1D acquisition.
    """
    assert CameraParameters(exposure_ms=10.0).readout == IMAGE_READOUT


def test_camera_parameters_reject_a_readout_no_camera_could_act_on():
    """
    A misspelt mode is refused at construction, not silently imaged.

    The vocabulary is closed on purpose: an adapter that fell through to
    imaging on an unrecognised mode would answer an operator asking for a
    spectrum with a picture, and the frame's own metadata would agree
    with the picture.
    """
    with pytest.raises(ValueError, match="readout must be one of"):
        CameraParameters(exposure_ms=10.0, readout="sum_project")


def test_the_readout_vocabulary_is_exactly_the_two_modes():
    """
    Both modes are in ``READOUT_MODES``, which is what adapters validate against.

    Pinned because the tuple is the shared contract: an adapter that
    cannot project compares against these names, and a third mode added
    without an adapter behind it would be a field every adapter must
    refuse.
    """
    expected = (IMAGE_READOUT, PROJECTED_READOUT)
    assert READOUT_MODES == expected
    for mode in READOUT_MODES:
        assert CameraParameters(exposure_ms=1.0, readout=mode).readout == mode
