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


HIGH_TENSION_V_KEY = "high_tension_v"
"""
Frame-metadata key for the accelerating voltage, in volts.

The one key in the vocabulary below with a constant, because it is the
one NeXus specifies a home for
(``NXem``'s ``instrument/ebeam_column/electron_source/voltage``) and so
the one the storage layer reads by name rather than persisting whole.
Naming the other fifteen here would add constants with one use each.
"""


@dataclass(frozen=True)
class Frame:
    """
    A single acquired frame and its acquisition metadata.

    This is the neutral currency between the device layer and everything
    downstream: the live viewer displays ``data`` directly, and the
    storage layer (migration plan, Phase 3) persists ``data`` plus
    ``metadata``.

    **The metadata vocabulary.**
    Every adapter attaches what it can of the following, and omits what
    the instrument does not report rather than substituting a default —
    an absent key means "not reported", which a stored zero would not.
    Units are the operator's throughout, as everywhere else here.

    The set is Nion's, arrived at by reading the two tests whose entire
    purpose is to enumerate it
    (``CameraControl_test.test_acquire_attaches_required_metadata`` and
    ``ScanControl_test.test_context_scan_attaches_required_metadata``);
    the *names* are this project's, because adopting ``stem.scan.fov_nm``
    wholesale would put a vendor's schema in the vendor-neutral layer.

    Every device:

    ``device_id``
        The scanner's or camera's stable id — which detector produced this.
    ``frame_index``
        Frames produced by this device since it was opened, from 0.
        Monotonic and gapless, so a *missing* index in a recording is
        visible evidence of a dropped frame rather than silently absent
        data.
    ``high_tension_v``, ``defocus_nm``, ``beam_current_a``
        Instrument state at acquisition time, read per frame rather than
        cached — a focal series changes the defocus between frames, and
        it is the workflow that most needs the value to be the frame's
        own.
    ``calibration``
        Per-axis physical axes, when the device reports them; see
        :mod:`miainwoodpecker.storage.calibration`.

    Scanners additionally:

    ``channel_index``, ``channel_name``
        Which detector channel this frame came from.
    ``fov_nm``, ``fov_size_nm``, ``pixel_time_us``
        The requested scan geometry; see :class:`ScanParameters`.
    ``line_time_us``, ``frame_time_s``
        Derived timings, recorded because a reader reconstructing them
        needs to know the flyback convention and cannot.
    ``rotation_rad``, ``center_nm``
        Scan orientation and centre, without which the field of view does
        not say *which* region was scanned.

    Cameras additionally:

    ``camera_name``, ``camera_type``
        The vendor's own label for the detector (``"ronchigram"``,
        ``"eels"``), which is what an analysis tool needs to know what
        kind of data this is.
    ``counts_per_electron``
        Detector gain, where the device publishes it.
    ``frame_number``, ``integration_count``
        Passed through from the vendor's own frame properties.

    **No ``scan_id``.** Nion carries one to group the channels of a single
    simultaneous multi-channel scan. This interface has no such call — a
    second channel is a second ``scan_frame``, and therefore a second pass
    of the beam — so an id claiming to group them would be a fiction.

    Attributes
    ----------
    data : npt.NDArray[typing.Any]
        The acquired array. 2D for images; may be 1D for binned spectra.
    timestamp : datetime.datetime
        Acquisition time. Always timezone-aware (UTC).
    metadata : typing.Mapping[str, typing.Any]
        Acquisition properties, in the vocabulary above plus whatever
        else the vendor reported.
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
        Field of view of the scanned region, in nanometres, spanning the
        **longer** axis. Pixels are square, so on a non-square scan the
        shorter axis covers proportionally less than ``fov_nm``; use
        :attr:`fov_size_nm` rather than assuming both axes span it.

        This is not a free choice: it is the convention Nion's own scan
        layer implements, and a vendor-neutral type that disagreed with
        the only adapter behind it would be neutral in name only.
        ``nion.instrumentation.scan_base.get_scan_calibrations`` computes
        ``pixel_size_nm = fov_nm / max(scan_shape)`` and applies it to
        both axes, and ``ScanFrameParameters.fov_size_nm`` derives the
        second axis from the pixel aspect ratio.
    """

    height: int
    width: int
    pixel_time_us: float
    fov_nm: float

    @property
    def shape(self) -> tuple[int, int]:
        """Return the numpy-style (rows, columns) shape of a scanned frame."""
        return (self.height, self.width)

    @property
    def pixel_size_nm(self) -> float:
        """Return the size of one square scan pixel, in nanometres."""
        return self.fov_nm / max(self.height, self.width)

    @property
    def fov_size_nm(self) -> tuple[float, float]:
        """
        Return the scanned extent as ``(y_nm, x_nm)``, in nanometres.

        The per-axis form of :attr:`fov_nm`, in the same ``(y, x)`` order
        as :attr:`shape`. Storage and analysis want this rather than the
        scalar: deriving per-axis extents by dividing ``fov_nm`` by each
        dimension independently silently produces non-square pixels and
        writes a wrong scale on the shorter axis of every non-square scan.
        """
        pixel_nm = self.pixel_size_nm
        return (self.height * pixel_nm, self.width * pixel_nm)


@dataclass(frozen=True)
class CameraParameters:
    """
    Settings for a camera acquisition, in vendor-neutral units.

    The camera's counterpart to :class:`ScanParameters`, and a value
    object for the same reason: a camera has two settings that must change
    together to stay coherent, since binning changes both the frame shape
    and the calibration scale. Setting them one at a time leaves a window
    in which the two disagree.

    Attributes
    ----------
    exposure_ms : float
        Integration time per frame, in milliseconds. Must be positive.
    binning : int
        How many sensor pixels are combined per stored pixel, in each
        direction. A binned pixel spans proportionally more of the axis,
        so this multiplies the calibration scale — which is why the two
        are one type. Must be one of the camera's ``binning_values``.
    """

    exposure_ms: float
    binning: int = 1

    def __post_init__(self) -> None:
        """Reject values no camera could act on."""
        if not self.exposure_ms > 0:
            msg = f"exposure_ms must be positive, got {self.exposure_ms!r}"
            raise ValueError(msg)
        if self.binning < 1:
            msg = f"binning must be at least 1, got {self.binning!r}"
            raise ValueError(msg)


@typing.runtime_checkable
class Camera(typing.Protocol):
    """
    A 2D detector that produces frames continuously once started.

    Contract: call ``start`` before ``acquire_frame``; ``stop`` pauses
    acquisition and ``start`` may be called again afterwards; call
    ``close`` exactly once when the device will not be used again.
    Implementations may own background threads that keep the process
    alive until ``close`` is called.

    ``configure`` may be called at any point, including while started;
    which frame first shows the change is the device's business, and the
    contract is only that a later frame does.
    """

    @property
    def camera_id(self) -> str:
        """Return the stable identifier for this camera."""
        ...

    @property
    def binning_values(self) -> typing.Sequence[int]:
        """Return the binning factors this camera supports, ascending."""
        ...

    def parameters(self) -> CameraParameters:
        """Return the settings the next frame will be acquired with."""
        ...

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Apply new settings and return the ones the device actually took.

        Returned rather than assumed, because a device may round an
        exposure to its own precision. Callers that care what a frame was
        taken under should read the return value or the frame's metadata,
        not the request.
        """
        ...

    def start(self) -> None:
        """
        Begin continuous acquisition.

        Returns as soon as the device has been told to start; it does
        **not** wait for a frame. ``acquire_frame`` is what blocks.
        This said 'blocks until the first frame is available' for
        three phases while no implementation did so - neither the Nion
        adapter (a bare ``start_live()``) nor the remote client (one
        RPC) - so a caller trusting it would have raced.
        """
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
