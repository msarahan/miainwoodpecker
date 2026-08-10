"""
Adapter exposing Nion's device layer through the vendor-neutral interfaces.

Wraps the ``nion.device_kit`` camera and scan device objects — the same
objects ``nionswift-usim`` constructs and Swift's own acquisition drives —
as plain :class:`~miainwoodpecker.devices.interface.Camera` /
:class:`~miainwoodpecker.devices.interface.Scanner` implementations, with
no Swift ``Application``/``DocumentController`` involvement (validated by
``scripts/phase0_usim_smoke_test.py``).

Importing this module requires the ``device`` optional dependency group
(``pip install miainwoodpecker[device]``).
"""

from __future__ import annotations

import contextlib
import datetime
import typing
from dataclasses import dataclass

from nion.device_kit import ScanDevice as _ScanDeviceKit
from nion.usim_device import DeviceConfiguration as _UsimConfiguration

from miainwoodpecker.devices.interface import Frame, ScanParameters

if typing.TYPE_CHECKING:
    from collections.abc import Iterator

    from nion.device_kit.CameraDevice import Camera as _DeviceKitCamera
    from nion.device_kit.ScanDevice import Device as _DeviceKitScanDevice


def _aware_utc(timestamp: datetime.datetime | None) -> datetime.datetime:
    """Return a timezone-aware UTC timestamp; Nion reports naive UTC datetimes."""
    if timestamp is None:
        return datetime.datetime.now(tz=datetime.UTC)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=datetime.UTC)
    return timestamp


class NionCamera:
    """A ``Camera`` implementation wrapping a Nion device-kit camera device."""

    def __init__(self, camera_device: _DeviceKitCamera) -> None:
        self._device = camera_device

    @property
    def camera_id(self) -> str:
        """Return the wrapped device's camera id."""
        return self._device.camera_id

    def start(self) -> None:
        """Begin continuous acquisition; blocks until the first frame is available."""
        self._device.start_live()

    def stop(self) -> None:
        """Pause continuous acquisition."""
        self._device.stop_live()

    def acquire_frame(self) -> Frame:
        """Return the next available frame; requires ``start`` to have been called."""
        data_element = self._device.acquire_image()
        return Frame(
            data=data_element["data"],
            timestamp=_aware_utc(data_element.get("timestamp")),
            metadata=dict(data_element.get("properties") or {}),
        )

    def close(self) -> None:
        """Release the device and join its acquisition thread."""
        self._device.close()


class NionScanner:
    """A ``Scanner`` implementation wrapping a Nion device-kit scan device."""

    def __init__(self, scan_device: _DeviceKitScanDevice) -> None:
        self._device = scan_device

    @property
    def scanner_id(self) -> str:
        """Return the wrapped device's scan device id."""
        return self._device.scan_device_id

    @property
    def channel_names(self) -> typing.Sequence[str]:
        """Return the detector channel names, indexed by channel number."""
        return [
            self._device.get_channel_name(index)
            for index in range(self._device.channel_count)
        ]

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Scan and return a single frame from the given detector channel."""
        frame_parameters = _ScanDeviceKit.ScanFrameParameters(
            pixel_size=(parameters.height, parameters.width),
            pixel_time_us=parameters.pixel_time_us,
            fov_nm=parameters.fov_nm,
        )
        data = self._device.get_scan_data(frame_parameters, channel)
        return Frame(
            data=data,
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={
                "channel_index": channel,
                "channel_name": self._device.get_channel_name(channel),
                "fov_nm": parameters.fov_nm,
                "pixel_time_us": parameters.pixel_time_us,
            },
        )

    def close(self) -> None:
        """Release the device."""
        self._device.close()


@dataclass(frozen=True)
class SimulatedInstrument:
    """
    Handles to the devices of a simulated STEM microscope.

    Attributes
    ----------
    ronchigram_camera : NionCamera
        The simulated Ronchigram camera.
    eels_camera : NionCamera
        The simulated EELS camera.
    scanner : NionScanner
        The simulated scan device (HAADF/MAADF channels).
    stage_size_nm : float
        The simulated stage extent, useful for choosing a sensible
        ``ScanParameters.fov_nm``.
    """

    ronchigram_camera: NionCamera
    eels_camera: NionCamera
    scanner: NionScanner
    stage_size_nm: float


@contextlib.contextmanager
def simulated_instrument() -> Iterator[SimulatedInstrument]:
    """
    Build the nionswift-usim simulated microscope and guarantee clean teardown.

    usim starts a background acquisition thread per camera at construction
    time, and it constructs *both* cameras up front; every camera must be
    closed on the way out or the process hangs at exit waiting for the
    un-closed thread to join. This context manager owns that lifecycle.

    Yields
    ------
    SimulatedInstrument
        Adapted handles to the simulated cameras and scanner.
    """
    configuration = _UsimConfiguration.AcquisitionContextConfiguration(
        set_configuration_location=False,
    )
    ronchigram_camera = NionCamera(configuration.ronchigram_camera_device)
    eels_camera = NionCamera(configuration.eels_camera_device)
    scanner = NionScanner(configuration.scan_module.device)
    try:
        yield SimulatedInstrument(
            ronchigram_camera=ronchigram_camera,
            eels_camera=eels_camera,
            scanner=scanner,
            stage_size_nm=configuration.instrument.stage_size_nm,
        )
    finally:
        ronchigram_camera.close()
        eels_camera.close()
        scanner.close()
