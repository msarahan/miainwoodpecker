"""
Vendor-neutral device interfaces for the acquisition layer.

Everything above the device layer (acquisition orchestration, the live
viewer, storage) depends only on the protocols in this module, never on a
vendor SDK, so another vendor's hardware can be added later as a new
adapter without touching those layers (see docs/migration-plan.md, §2).

These are deliberately the *smallest* interfaces that support the phase
that needs them: a camera produces frames continuously once started, and a
scanner produces one frame per request (live scanning is a repeated
``scan_frame`` loop). Exposure/settings modeling and hardware-synchronized
multi-signal acquisition are still deferred.

:class:`InstrumentController` is the Phase 3 addition, and holds to the
same rule. It exposes exactly three controls — stage position, defocus,
beam blanker — because those are what a parameter sweep
(``acquisition.sequence.focal_series``) and a safe teardown
(:meth:`InstrumentController.park`) actually need, not because the
underlying instruments expose only three. A real Nion STEM controller has
hundreds of named controls (``C10``, ``C12``, ``CAperture``, ``EHT``, …);
proxying all of them would be a vendor API in vendor-neutral clothing.
Adding a control here should be driven by a caller that needs it.
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


# Neutral names for the controls an InstrumentController may report through
# available_controls(). Strings rather than an enum so the value survives the
# device-server IPC boundary as plain data (see devices/rpc.py).
STAGE_POSITION_CONTROL = "stage_position"
DEFOCUS_CONTROL = "defocus"
BEAM_BLANKER_CONTROL = "beam_blanker"


@typing.runtime_checkable
class InstrumentController(typing.Protocol):
    """
    Instrument-level controls that are not owned by any single detector.

    Units are the operator's, matching :class:`ScanParameters`: nanometres
    for lengths, never the vendor's metres. Positions are ``(y, x)``
    tuples, the same axis order as ``ScanParameters.shape``.

    Not every instrument has every control (a microscope may have no beam
    blanker; a simulator may model a control it then ignores), so callers
    must consult :meth:`available_controls` before driving one rather than
    assuming a successful setter means a working control — a distinction
    this project has already been bitten by (docs/migration-plan.md, §7).
    """

    def stage_size_nm(self) -> float:
        """Return the usable stage extent, for choosing a sensible field of view."""
        ...

    def available_controls(self) -> typing.Sequence[str]:
        """Return the ``*_CONTROL`` names this instrument actually implements."""
        ...

    def stage_position_nm(self) -> tuple[float, float]:
        """Return the current stage position as ``(y, x)``, in nanometres."""
        ...

    def set_stage_position_nm(self, y_nm: float, x_nm: float) -> None:
        """Move the stage to an absolute ``(y, x)`` position, in nanometres."""
        ...

    def defocus_nm(self) -> float:
        """Return the current defocus, in nanometres."""
        ...

    def set_defocus_nm(self, defocus_nm: float) -> None:
        """Set the defocus, in nanometres."""
        ...

    def is_beam_blanked(self) -> bool:
        """Return whether the beam is currently blanked."""
        ...

    def set_beam_blanked(self, *, blanked: bool) -> None:
        """Blank or unblank the beam."""
        ...

    def park(self) -> None:
        """
        Put the instrument in a safe unattended state.

        Blanks the beam if a blanker exists. Deliberately *not* a full
        teardown: stopping detectors and releasing devices belongs to the
        code that owns them (the device server's shutdown handshake), not
        to a single-instrument control surface.
        """
        ...
