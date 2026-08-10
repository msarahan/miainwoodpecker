"""
Vendor-neutral device interfaces for the acquisition layer.

Everything above the device layer (acquisition orchestration, the live
viewer, storage) depends only on the protocols in this module, never on a
vendor SDK, so another vendor's hardware can be added later as a new
adapter without touching those layers (see docs/migration-plan.md, §2).

These are deliberately the *smallest* interfaces that support the Phase 2
live-viewer MVP: a camera produces frames continuously once started, and a
scanner produces one frame per request (live scanning is a repeated
``scan_frame`` loop). Exposure/settings modeling and hardware-synchronized
multi-signal acquisition are deferred until the phases that need them.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass, field

if typing.TYPE_CHECKING:
    import datetime

    import numpy.typing as npt


@dataclass(frozen=True)
class Frame:
    """
    A single acquired frame and its acquisition metadata.

    This is the neutral currency between the device layer and everything
    downstream: the live viewer displays ``data`` directly, and the
    storage layer (migration plan, Phase 3) persists ``data`` plus
    ``metadata``.

    Attributes
    ----------
    data : npt.NDArray[typing.Any]
        The acquired array. 2D for images; may be 1D for binned spectra.
    timestamp : datetime.datetime
        Acquisition time. Always timezone-aware (UTC).
    metadata : typing.Mapping[str, typing.Any]
        Vendor-reported acquisition properties (frame number, channel,
        field of view, ...). Keys are not standardized yet; mapping them
        onto NXem is Phase 3 work.
    """

    data: npt.NDArray[typing.Any]
    timestamp: datetime.datetime
    metadata: typing.Mapping[str, typing.Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScanParameters:
    """
    Parameters for a single scanned frame, in vendor-neutral units.

    These are the units operators think in (pixels, microseconds,
    nanometers); each vendor adapter translates them into its SDK's own
    frame-parameter objects.

    Attributes
    ----------
    height : int
        Number of scan lines (slow axis), in pixels.
    width : int
        Number of pixels per line (fast axis), in pixels.
    pixel_time_us : float
        Dwell time per pixel, in microseconds.
    fov_nm : float
        Field of view of the scanned region, in nanometers.
    """

    height: int
    width: int
    pixel_time_us: float
    fov_nm: float

    @property
    def shape(self) -> tuple[int, int]:
        """Return the numpy-style (rows, columns) shape of a scanned frame."""
        return (self.height, self.width)


@typing.runtime_checkable
class Camera(typing.Protocol):
    """
    A 2D detector that produces frames continuously once started.

    Contract: call ``start`` before ``acquire_frame``; ``stop`` pauses
    acquisition and ``start`` may be called again afterwards; call
    ``close`` exactly once when the device will not be used again.
    Implementations may own background threads that keep the process
    alive until ``close`` is called.
    """

    @property
    def camera_id(self) -> str:
        """Return the stable identifier for this camera."""
        ...

    def start(self) -> None:
        """Begin continuous acquisition; blocks until the first frame is available."""
        ...

    def stop(self) -> None:
        """Pause continuous acquisition."""
        ...

    def acquire_frame(self) -> Frame:
        """Return the next available frame; requires ``start`` to have been called."""
        ...

    def close(self) -> None:
        """Release the device and any background threads it owns."""
        ...


@typing.runtime_checkable
class Scanner(typing.Protocol):
    """
    A scan generator that produces one scanned frame per request.

    Continuous live imaging is a repeated ``scan_frame`` loop (migration
    plan, Phase 2). Hardware-synchronized multi-signal acquisition
    (e.g. camera-per-probe-position spectrum imaging) is deliberately not
    part of this interface yet; it arrives with Phase 3.
    """

    @property
    def scanner_id(self) -> str:
        """Return the stable identifier for this scanner."""
        ...

    @property
    def channel_names(self) -> typing.Sequence[str]:
        """Return the detector channel names, indexed by channel number."""
        ...

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Scan and return a single frame from the given detector channel."""
        ...

    def close(self) -> None:
        """Release the device."""
        ...
