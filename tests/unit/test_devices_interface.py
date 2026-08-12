"""Tests for the vendor-neutral device interfaces."""

import datetime
import typing
import uuid

import numpy as np
import pytest

from miainwoodpecker.devices import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    STAGE_POSITION_CONTROL,
    Camera,
    CameraParameters,
    Frame,
    Instrument,
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

    def scan_frames(
        self,
        parameters: ScanParameters,
        channels: typing.Sequence[int],
    ) -> list[Frame]:
        """
        Return one frame per channel, sharing the identity of one fake pass.

        The reference shape of the contract: the pass identity is minted
        *inside* this call, every sibling carries it plus the channel
        list, and the single-channel ``scan_frame`` above attaches
        neither — which is what makes the identity mean something.

        Parameters
        ----------
        parameters : ScanParameters
            Scan geometry, shared by every returned frame.
        channels : typing.Sequence[int]
            Channel indices to read out during the fake pass.

        Returns
        -------
        list[Frame]
            One frame per requested channel, in request order.
        """
        pass_id = str(uuid.uuid4())
        timestamp = datetime.datetime.now(tz=datetime.UTC)
        return [
            Frame(
                data=np.zeros(parameters.shape, dtype=np.float32),
                timestamp=timestamp,
                metadata={
                    "channel_index": channel,
                    "scan_pass_id": pass_id,
                    "simultaneous_channels": list(channels),
                },
            )
            for channel in channels
        ]

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


def test_fake_instrument_satisfies_the_instrument_protocol():
    """A structural implementation is recognized by the runtime-checkable core."""
    assert isinstance(_FakeInstrument(), Instrument)


def test_the_full_controller_is_not_a_runtime_check():
    """
    ``isinstance`` against ``InstrumentController`` is a ``TypeError``.

    Deliberate, and worth pinning so it cannot quietly regress: the full
    control surface used to be ``runtime_checkable``, and its
    all-or-nothing ``isinstance`` failed two working adapters (a
    spectrometer with one control, a webcam with none) — it tested for
    Nion-shapedness rather than conformance. The runtime question is now
    :class:`Instrument` plus ``available_controls()``; the controller
    exists for static typing, and asking it at runtime should fail
    loudly rather than reintroduce the wrong question.
    """
    with pytest.raises(TypeError, match="runtime_checkable"):
        isinstance(_FakeInstrument(), InstrumentController)


def test_scan_frames_share_a_pass_identity_that_scan_frame_never_carries():
    """
    The pass identity exists exactly where a pass exists.

    This is the contract the interface docstrings promise: frames of one
    ``scan_frames`` call all name the same ``scan_pass_id`` and the same
    sibling channels, two *calls* never share one (each is its own pass
    of the beam), and the single-channel ``scan_frame`` attaches neither
    key — an identifier nothing establishes would be a claim, which is
    what the earlier no-``scan_id`` rule was rightly protecting against.
    """
    scanner = _FakeScanner()
    parameters = ScanParameters(height=8, width=16, pixel_time_us=1.0, fov_nm=50.0)
    first_pass = scanner.scan_frames(parameters, [0, 1])
    second_pass = scanner.scan_frames(parameters, [0, 1])
    expected = 2
    assert len(first_pass) == expected
    pass_ids = {frame.metadata["scan_pass_id"] for frame in first_pass}
    assert len(pass_ids) == 1
    assert all(
        frame.metadata["simultaneous_channels"] == [0, 1] for frame in first_pass
    )
    # A second call is a second pass, so its identity must be fresh.
    assert (
        second_pass[0].metadata["scan_pass_id"]
        != first_pass[0].metadata["scan_pass_id"]
    )
    single = scanner.scan_frame(parameters, channel=0)
    assert "scan_pass_id" not in single.metadata
    assert "simultaneous_channels" not in single.metadata


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
    assert isinstance(instrument, Instrument)
