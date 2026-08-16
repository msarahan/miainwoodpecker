"""
Vendor-neutral device interfaces for the acquisition layer.

Everything above the device layer (acquisition orchestration, the live
viewer, storage) depends only on the protocols in this module, never on a
vendor SDK, so another vendor's hardware can be added later as a new
adapter without touching those layers (see docs/migration-plan.md, §2).

These are deliberately the *smallest* interfaces that support the phase
that needs them: a camera produces frames continuously once started, and a
scanner produces the frames of one scanned pass per request — one channel
through ``scan_frame`` (live scanning is a repeated loop of it), several
channels simultaneously through ``scan_frames``, because one pass of the
probe reads every enabled detector out at once. Synchronising a *camera*
to the scan (the cross-device pass 4D-STEM needs) is still deferred.

:class:`Instrument` and :class:`InstrumentController` are the Phase 3
addition, and hold to the same rule. The controller exposes four controls
— stage position, defocus, beam blanker, spectrometer energy offset —
because those are what a parameter sweep
(``acquisition.sequence.focal_series``,
``acquisition.sequence.energy_offset_series``) and a safe teardown
(:meth:`Instrument.park`) actually need, not because the
underlying instruments expose only four. A real Nion STEM controller has
hundreds of named controls (``C10``, ``C12``, ``CAperture``, ``EHT``, …);
proxying all of them would be a vendor API in vendor-neutral clothing.
Adding a control here should be driven by a caller that needs it — which
is how the energy offset arrived, with ``energy_offset_series`` and not
before it.

:class:`SpectrumDetector` is the fourth kind, and the first one that is
not a picture. An energy-dispersive X-ray detector produces a *histogram
of photon energies* — one spectrum in spot mode, one spectrum per probe
position when mapping — which is neither a scanned frame nor a camera
frame. See docs/adapters/spectrum-detectors.md for the design argument,
including why :class:`Spectrum` is its own type rather than a
:class:`Frame` with a 1D array.
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
        data. It counts **frames, not passes**: a two-channel
        :meth:`Scanner.scan_frames` advances it by two, because two
        frames were produced. What groups those two is ``scan_pass_id``
        below, and keeping the two jobs apart is what lets the
        dropped-frame check keep working unchanged.
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

    Frames from a simultaneous multi-channel scan
    (:meth:`Scanner.scan_frames`) additionally:

    ``scan_pass_id``
        The identity of the **one pass of the probe** these frames came
        from, as an opaque unique string minted by the device adapter
        inside the multi-channel acquisition path — never supplied by a
        caller, and never attached by :meth:`Scanner.scan_frame`, whose
        frames genuinely are one pass each and have no siblings to name.
        Two frames with equal ``scan_pass_id`` were read out during the
        same traversal of the specimen, so per-pixel arithmetic between
        them (DPC, iDPC, centre of mass) compares the same probe
        positions.

        Opaque, and specifically **not derived from the vendor's frame
        counter**: those restart at zero with the device, so two
        recordings made on two days would claim to share passes. A
        random unique string cannot make that mistake. This is also what
        Nion itself does one layer above the device
        (``scan_base.ScanAcquisitionTask`` mints a ``uuid4`` per frame
        and writes it to every simultaneously-read channel as
        ``stem.scan.scan_id``), so the concept is the vendor's; only the
        neutral spelling — and the rule that a call must establish it —
        is this project's.
    ``simultaneous_channels``
        The channel indices that shared that pass, in the order they were
        requested — each frame names its siblings, including itself, so a
        single frame pulled out of a recording still says what else
        exists and where to look for it.

        Channel indices rather than device ids, which is what the
        spectrum side's :class:`Spectrum` ``simultaneous_with`` names,
        because these siblings are channels of *one* scan unit reading
        out together. A pass shared across separate devices — the
        scan unit and an EDX detector, or a camera per probe position —
        is a different and still-missing claim; see
        :meth:`SpectrumDetector.acquire_map`.

    Cameras additionally:

    ``camera_name``, ``camera_type``
        The vendor's own label for the detector (``"ronchigram"``,
        ``"eels"``), which is what an analysis tool needs to know what
        kind of data this is.
    ``exposure_ms``, ``binning``
        What the frame was taken with. Every camera adapter here attaches
        both, and the EELS adapter
        (:func:`~miainwoodpecker.analysis.hyperspy_bridge.load_as_eels_signal`)
        maps the exposure onto eXSpy's own detector item — so it is part
        of the vocabulary rather than a Nion detail, and listed here
        where the rest of it is. The caveat ``nion_server`` records
        applies: this is the *configured* exposure, which the frame
        already in flight during a ``configure`` was not taken at.
    ``readout``, ``projected_by``
        Which readout mode the frame was actually taken with — recovered
        from the data's rank, not echoed from the request, for the same
        in-flight reason as ``binning``. ``projected_by`` is present only
        on a projected frame and says **who summed**: ``"sensor"`` when
        the detector itself projected (accumulating before readout, so
        one readout's noise), ``"server"`` when the device server summed
        an image the sensor delivered (N rows' readout noise). The noise
        statistics differ, so the distinction is recorded rather than
        implied.
    ``counts_per_electron``
        Detector gain, where the device publishes it.
    ``frame_number``, ``integration_count``
        Passed through from the vendor's own frame properties.

    **Why the pass identity is earned, not declared.** An earlier
    revision of this docstring declined a ``scan_id`` outright, on the
    grounds that "a second channel is a second pass of the beam". That
    premise was false on real hardware — a scanned instrument reads every
    enabled detector out during *one* pass, always — but the refusal had
    the right instinct: an identifier nothing establishes is a claim,
    which is exactly how ``probe_position`` bit this project
    (docs/migration-plan.md, §7). So the identity exists now that a call
    exists to establish it: ``scan_pass_id`` is produced by
    :meth:`Scanner.scan_frames` and only by it, and the single-channel
    :meth:`Scanner.scan_frame` still attaches nothing — its frames never
    shared a pass with anything, and a recording must not say otherwise.

    Attributes
    ----------
    data : npt.NDArray[typing.Any]
        The acquired array. 2D for images.

        **An EELS frame is a ``Frame``, and stays one.** A spectrometer
        disperses onto a 2D detector, so what the device produces is an
        image in which one direction is energy and the other is not —
        exactly what this type plus a per-axis calibration describes
        (:meth:`~miainwoodpecker.storage.calibration.FrameCalibration.spectrum`,
        and the adapter reads *which* direction from the device rather
        than assuming). :class:`Spectrum` is for detectors that are
        **natively** 1D, of which an EDX silicon drift detector is the
        case in hand; it is not a second home for EELS.

        **The 1D case is real and now stored.** A camera configured with
        :data:`PROJECTED_READOUT` produces a 1D frame whose one axis is
        the dispersive one, calibration kept verbatim.
        :class:`~miainwoodpecker.storage.nexus.NexusWriter` still names
        exactly two frame axes and raises on any other rank
        (docs/architecture-review.md, §1.6: "a layout decision, not a
        shape guess") — a projected frame is routed by the recording path
        into :class:`~miainwoodpecker.storage.spectra.SpectrumWriter`
        instead, landing in the same ``NXspectrum`` layout as EDX
        (:func:`miainwoodpecker.acquisition.sequence.record` is the
        seam). On the Nion side the mode maps onto
        ``CameraFrameParameters.processing = "sum_project"`` — which the
        device honours only for sequence/SI acquisition, measured, so on
        the plain-acquire path this project drives the server sums and
        the frame's ``projected_by`` metadata says so; see
        docs/adapters/spectrum-detectors.md §6.

        **Deliberately not 3D**, which a colour sensor would need. A
        camera with a colour filter array should deliver its raw Bayer
        plane here — which is 2D — and name the CFA pattern in
        ``metadata``. Demosaicing invents two thirds of every pixel, so a
        recording made from it measures the interpolation; a viewer is
        free to demosaic for display. See docs/vendor-support.md.
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


IMAGE_READOUT = "image"
"""
Camera readout mode: the sensor's 2D image, unchanged. The default.
"""

PROJECTED_READOUT = "projected"
"""
Camera readout mode: the whole non-dispersive direction summed to 1D.

The operation an EELS operator asks for when trading dynamic range
against SNR — vertical binning taken to its limit — and deliberately a
*readout mode* rather than a per-axis binning factor, because that is
what the only adapter behind this interface can honour: Nion's
``CameraFrameParameters`` has no per-axis binning anywhere, and what it
offers is ``processing = "sum_project"``, a full projection to 1D. See
docs/adapters/spectrum-detectors.md §6 for the measurement behind the
decision, and for when a per-axis tuple would earn its place (a second
camera adapter that can honour one).

A projected readout produces a **1D** :class:`Frame` whose surviving
axis keeps the dispersive calibration verbatim; the summed axis is gone,
not merged. Cameras that cannot project refuse in ``configure`` with a
sentence, rather than silently imaging.
"""

READOUT_MODES = (IMAGE_READOUT, PROJECTED_READOUT)
"""Every readout mode a :class:`CameraParameters` may request."""


@dataclass(frozen=True)
class CameraParameters:
    """
    Settings for a camera acquisition, in vendor-neutral units.

    The camera's counterpart to :class:`ScanParameters`, and a value
    object for the same reason: a camera has settings that must change
    together to stay coherent, since binning changes both the frame shape
    and the calibration scale, and the readout mode changes the frame's
    very rank. Setting them one at a time leaves a window in which they
    disagree.

    Attributes
    ----------
    exposure_ms : float
        Integration time per frame, in milliseconds. Must be positive.
    binning : int
        How many sensor pixels are combined per stored pixel, in each
        direction. A binned pixel spans proportionally more of the axis,
        so this multiplies the calibration scale — which is why the two
        are one type. Must be one of the camera's ``binning_values``.
    readout : str
        One of :data:`READOUT_MODES`. :data:`IMAGE_READOUT` (the
        default) delivers the sensor's 2D image exactly as before;
        :data:`PROJECTED_READOUT` sums the whole non-dispersive
        direction and delivers a 1D spectrum. A camera that cannot
        project refuses in ``configure`` rather than silently imaging.
    """

    exposure_ms: float
    binning: int = 1
    readout: str = IMAGE_READOUT

    def __post_init__(self) -> None:
        """Reject values no camera could act on."""
        if not self.exposure_ms > 0:
            msg = f"exposure_ms must be positive, got {self.exposure_ms!r}"
            raise ValueError(msg)
        if self.binning < 1:
            msg = f"binning must be at least 1, got {self.binning!r}"
            raise ValueError(msg)
        if self.readout not in READOUT_MODES:
            msg = (
                f"readout must be one of {READOUT_MODES}, got {self.readout!r}"
            )
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
    A scan generator that produces the frames of one scanned pass per request.

    Two acquisition calls, one physics distinction between them.
    ``scan_frame`` is one pass of the probe read out on one detector
    channel; ``scan_frames`` is one pass read out on *several* channels at
    once, which is what a scanned instrument natively does — every
    enabled detector sees the same traversal of the specimen. Serial
    single-channel acquisition is the special case, not the general one,
    and asking for k channels through ``scan_frame`` costs k passes of
    dose with drift between them.

    Continuous live imaging is a repeated ``scan_frame`` loop (migration
    plan, Phase 2). Synchronising a *camera* to the scan
    (camera-per-probe-position spectrum imaging, 4D-STEM) is still
    deliberately not part of this interface: that is a cross-device pass,
    and ``scan_frames`` covers only the scan unit's own channels.

    ``scan_frames`` is part of the protocol rather than an optional
    extra, so an adapter written before it exists no longer satisfies
    this ``isinstance`` check and a remote one answers *unknown method*.
    That is the intended reading: every scanned instrument reads its
    enabled detectors out together, so an adapter that cannot say what
    happens when two are asked for has not finished describing its
    device. What it must not do is loop ``scan_frame`` — see that
    method.
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

    def scan_frames(
        self,
        parameters: ScanParameters,
        channels: typing.Sequence[int],
    ) -> typing.Sequence[Frame]:
        """
        Scan **once** and return one frame per requested channel.

        The contract is the physics: every returned frame comes from the
        same single pass of the probe, so the device is asked for one
        scan, not ``len(channels)`` of them — one pass of dose, no drift
        between channels, and per-pixel arithmetic between the returned
        frames (DPC, iDPC, centre of mass) compares the same probe
        positions. Each frame carries ``scan_pass_id`` and
        ``simultaneous_channels`` in its metadata (see :class:`Frame`)
        precisely because this call is what establishes them.

        An implementation whose hardware cannot read the requested
        channels out simultaneously must raise rather than quietly loop
        ``scan_frame`` — k sequential passes labelled as one shared pass
        would put a false identity on stored data, which is worse than an
        honest error. A single-channel request is always satisfiable:
        one pass with one readout is still one pass.

        Frames are returned in the order the channels were requested.

        **One error vocabulary across adapters**, so a caller (or the
        conformance suite docs/vendor-support.md proposes) can branch on
        the kind of mistake rather than on an adapter's phrasing:
        ``ValueError`` for a request no scanner could honour (no
        channels, or the same channel twice — a detector cannot read
        itself out twice in one pass), and ``IndexError`` for a channel
        index this scanner does not have, which is the exception the
        single-channel path already raises for that mistake. Across the
        device-server boundary those arrive as the ``error_type`` on
        :class:`~miainwoodpecker.devices.rpc.RemoteCallError`.

        Parameters
        ----------
        parameters : ScanParameters
            Scan geometry and timing, shared by every returned frame.
        channels : typing.Sequence[int]
            Detector channel indices to read out during the pass. Must
            be non-empty and without duplicates; a channel index the
            scanner does not have is an error.

        Returns
        -------
        typing.Sequence[Frame]
            One frame per requested channel, in request order, all from
            the same pass.
        """
        ...

    def close(self) -> None:
        """Release the device."""
        ...


SPOT_MODE = "spot"
"""
Acquisition mode: one spectrum from wherever the beam is standing.

The mode every spectrum detector has, because it needs nothing from the
scan unit — an accumulating counter and a clock. Named rather than
implied because :attr:`SpectrumDetector.acquisition_modes` reports which
of these a device can actually do, and a detector that cannot be
scan-synchronised should say so instead of failing on the first map.
"""

MAP_MODE = "map"
"""
Acquisition mode: one spectrum per probe position over a scanned region.

A *spectrum image*. Reported separately from :data:`SPOT_MODE` because it
requires something spot mode does not: a hardware synchronisation between
the scan and the detector's per-pixel binning. A benchtop XRF head or an
unsynchronised analyser has :data:`SPOT_MODE` and not this one, and the
distinction is not cosmetic — see
:meth:`SpectrumDetector.acquire_map` for what this interface can and
cannot promise about that synchronisation.
"""

ACQUISITION_MODES = (SPOT_MODE, MAP_MODE)
"""Every acquisition mode a :class:`SpectrumDetector` may report."""


@dataclass(frozen=True)
class SpectrumParameters:
    """
    Settings for a spectrum acquisition, in vendor-neutral units.

    The spectrum detector's counterpart to :class:`CameraParameters`, and
    a value object for the same reason: the channel count and the energy
    calibration must change together to stay coherent, since a detector
    reconfigured from 2048 to 4096 channels at a fixed dispersion covers
    a different energy range. Setting them one at a time leaves a window
    in which a caller's idea of the energy axis and the device's disagree.

    Energies are in electronvolts throughout, which is this project's
    canonical energy unit
    (:mod:`miainwoodpecker.storage.calibration`,
    :meth:`InstrumentController.energy_offset_ev`) — **not** the
    kiloelectronvolts every EDX vendor uses on screen and in its files.
    RosettaSciIO's Bruker reader builds its energy axis in ``keV``
    (``rsciio.bruker._api``, ``"units": "keV"``), and eXSpy accepts either
    but converts internally, so an adapter converts once, here, rather
    than leaving a factor of a thousand loose in the metadata.

    Attributes
    ----------
    live_time_s : float
        Preset acquisition time in *live* seconds — clock time minus the
        time the pulse processor spent busy. EDX presets are specified in
        live time rather than real time precisely because that is what
        makes two spectra comparable at different count rates; the
        acquired spectrum reports both (see :class:`Spectrum`). Must be
        positive.

        In :data:`MAP_MODE` this is the *per-pixel* preset, and a device
        is free to ignore it in favour of the scan's own dwell time —
        which is why ``configure`` returns what the device took.
    channel_count : int
        Number of energy channels in the spectrum. Must be at least one.
    energy_scale_ev : float
        Energy per channel, in electronvolts — the detector's dispersion,
        typically 5-20 eV/channel on a silicon drift detector. Must be
        positive.
    energy_offset_ev : float
        Energy at channel 0, in electronvolts. Not always zero: vendors
        commonly digitise a little below zero so the noise peak is
        visible and the zero-strobe can be fitted.
    """

    live_time_s: float
    channel_count: int = 4096
    energy_scale_ev: float = 10.0
    energy_offset_ev: float = 0.0

    def __post_init__(self) -> None:
        """Reject values no spectrum detector could act on."""
        if not self.live_time_s > 0:
            msg = f"live_time_s must be positive, got {self.live_time_s!r}"
            raise ValueError(msg)
        if self.channel_count < 1:
            msg = f"channel_count must be at least 1, got {self.channel_count!r}"
            raise ValueError(msg)
        if not self.energy_scale_ev > 0:
            msg = f"energy_scale_ev must be positive, got {self.energy_scale_ev!r}"
            raise ValueError(msg)


@dataclass(frozen=True)
class Spectrum:
    """
    Acquired photon counts against energy, and the acquisition's metadata.

    The spectrum-side counterpart to :class:`Frame`, and deliberately a
    *separate* type rather than a ``Frame`` carrying a 1D array. Three
    things forced that, and the full argument is in
    docs/adapters/spectrum-detectors.md:

    - **A spectrum cannot exist without its energy axis.** A ``Frame``
      may carry a calibration in ``metadata["calibration"]``, and
      routinely does not — an uncalibrated frame is still an image. An
      array of counts with no dispersion is not a spectrum; it is a
      histogram of nothing. So the energy axis is a constructor argument
      here, not an optional key.
    - **A spectrum image is rank 3**, and ``Frame`` forbids that
      explicitly (see :attr:`Frame.data`).
    - **The calibration model does not fit.**
      :class:`~miainwoodpecker.storage.calibration.FrameCalibration` is
      exactly two axes named ``y`` and ``x``. A spot spectrum has one
      axis and a spectrum image has three, so either case would have to
      lie about one of the two.

    **The energy axis is always the last one**, whatever the rank. That
    is not this project's convention: it is NeXus's
    (``NXspectrum``'s ``n_energy`` symbol reads "Number of energy bins
    (**always the fastest dimension**)"), RosettaSciIO's, whose Bruker
    HyperMap reader emits ``(height, width, Energy)``, and HyperSpy's,
    where the signal axis is last and the navigation axes precede it. One
    invariant, agreed by the format, the reader, and the analysis library
    this data has to reach.

    **The metadata vocabulary.** As on :class:`Frame`, every adapter
    attaches what it can and omits what the instrument does not report
    rather than substituting a default.

    This set is **not** this project's invention, and that is the point:
    it is the EMSA/MAS Standard File Format's spectral header, which both
    vendors on the target instrument already export and which HyperSpy
    already consumes. Read off RosettaSciIO's ``msa`` reader, whose
    keyword table maps ``XPERCHAN``/``OFFSET`` (dispersion and channel
    zero), ``LIVETIME``/``REALTIME``, ``AZIMANGLE``/``ELEVANGLE``/
    ``SOLIDANGLE``, ``FWHMMNKA`` and ``EDSDET`` onto exactly the eXSpy
    ``Acquisition_instrument.TEM.Detector.EDS.*`` items. Every name below
    is one of those, spelled in this project's own conventions, and every
    one also has a home in a NeXus base class — see
    docs/adapters/spectrum-detectors.md for the three-column table.

    Units are the operator's throughout, as everywhere else here, and two
    are worth naming because the vendors disagree with each other:
    energies are in **eV** (Oxford's H5OINA writes ``Channel Width`` and
    ``Start Channel`` in electronvolts; Bruker's ``bcf``/``spx`` reader
    builds its axis in keV), and detector angles are in **degrees**
    (EMSA's ``#AZIMANGLE-dg:`` and eXSpy both use degrees; H5OINA stores
    ``Detector Elevation``/``Detector Azimuth`` in radians). An adapter
    converts once, here, so a factor of a thousand and a factor of 57.3
    are not left loose in the metadata.

    Every spectrum detector:

    ``device_id``
        The detector's stable id.
    ``spectrum_index``
        Spectra produced by this device since it was opened, from 0.
        Monotonic and gapless, for the same reason
        ``Frame.frame_index`` is.
    ``live_time_s``, ``real_time_s``
        The live (counting) and real (clock) durations this spectrum
        integrated for. Both, not one: their ratio is the dead-time
        fraction, and a quantification that assumes real time at a high
        count rate is wrong by it. eXSpy wants both
        (``Acquisition_instrument.TEM.Detector.EDS.live_time`` and
        ``.real_time``) and RosettaSciIO's Bruker reader fills both from
        ``LifeTime``/``RealTime``.
    ``azimuth_deg``, ``elevation_deg``
        Where the detector sits relative to the specimen and the beam.
        Required by any absorption correction, so a quantification
        against a spectrum lacking them is using eXSpy's *defaults*
        rather than this instrument's geometry.
    ``solid_angle_sr``
        The solid angle the detector subtends at the specimen, in
        steradians — what turns counts into a collection efficiency.
    ``energy_resolution_ev``
        Detector resolution as the FWHM of the Mn Ka line, in eV. The one
        number every EDX vendor quotes, and what eXSpy's peak model uses.
    ``detector_type``, ``technique``
        What kind of detector this is (``"sdd"``, ``"sili"``) and what
        kind of spectroscopy it is doing (``"eds"``, ``"wds"``,
        ``"xrf"``). The technique is metadata rather than a target name
        for the same reason ``Frame.camera_type`` is.
    ``high_tension_v``, ``beam_current_a``
        Instrument state at acquisition time, in the same units and under
        the same key as :class:`Frame` uses — the accelerating voltage
        matters more here than anywhere else, because it sets which X-ray
        lines can be excited at all.

    Spectrum images additionally:

    ``fov_size_nm``, ``pixel_time_us``, ``rotation_rad``, ``center_nm``
        The scan geometry the map was collected over, in exactly the
        vocabulary a scanned :class:`Frame` uses, so storage calibrates
        the navigation axes through the same path
        (:meth:`~miainwoodpecker.storage.calibration.FrameCalibration.from_field_size`)
        rather than a second one.
    ``scan_sync``
        **Which device was the master.** Free text naming how the scan
        and the detector were synchronised — ``"detector"`` when the
        analyser drove the column's external scan input, ``"scanner"``
        when the column drove and the analyser followed a pixel-advance
        line, ``"none"`` when nothing did and the correspondence between
        pixel and spectrum is an assumption. This interface cannot
        *enforce* synchronisation (see
        :meth:`SpectrumDetector.acquire_map`), so the least it can do is
        make a recording say which it had — and the difference matters,
        because everything computed per pixel from an unsynchronised map
        is computed against the wrong position.
    ``simultaneous_with``
        **What else came from the same pass**, when anything did: a list
        of the device ids whose signals share this acquisition's probe
        positions. Empty or absent means nothing is claimed, which is the
        honest answer today, because nothing in this project can yet
        establish it. It is in the vocabulary now rather than later so
        that an adapter which *does* know — a vendor map job that
        collects its own image channels alongside the spectra — has
        somewhere truthful to say so, and so that a recording made before
        the acquisition layer grows a pass concept is not silently
        indistinguishable from one made after.

    Attributes
    ----------
    data : npt.NDArray[typing.Any]
        Counts per channel, with energy on the **last** axis. Rank 1 is a
        spot spectrum, rank 2 a line, rank 3 a spectrum image over a 2D
        scan — matching ``NXspectrum``'s ``spectrum_0d``/``_1d``/``_2d``.
        Ranks above 3 are refused rather than stored ambiguously.
    timestamp : datetime.datetime
        Acquisition time. Always timezone-aware (UTC), as on
        :class:`Frame`.
    energy_offset_ev : float
        Energy at channel 0, in electronvolts, as actually acquired —
        which need not equal what was requested, exactly as a camera may
        round an exposure.
    energy_scale_ev : float
        Energy per channel, in electronvolts, as actually acquired. Must
        be positive: a zero or negative dispersion describes no axis, and
        a spectrum whose axis cannot be built is not a spectrum.
    metadata : typing.Mapping[str, typing.Any]
        Acquisition properties, in the vocabulary above plus whatever
        else the vendor reported.
    """

    data: npt.NDArray[typing.Any]
    timestamp: datetime.datetime
    energy_offset_ev: float
    energy_scale_ev: float
    metadata: typing.Mapping[str, typing.Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject arrays and dispersions that describe no spectrum."""
        if not 1 <= self.data.ndim <= _MAX_SPECTRUM_RANK:
            msg = (
                f"a Spectrum is counts against energy with up to "
                f"{_MAX_SPECTRUM_RANK - 1} navigation axes (NXspectrum's "
                f"spectrum_0d, _1d, _2d); got a {self.data.ndim}D array of "
                f"shape {self.data.shape}"
            )
            raise ValueError(msg)
        if not self.energy_scale_ev > 0:
            msg = (
                f"energy_scale_ev must be positive to describe an energy "
                f"axis, got {self.energy_scale_ev!r}"
            )
            raise ValueError(msg)

    @property
    def channel_count(self) -> int:
        """Return the number of energy channels, i.e. the last axis's length."""
        return int(self.data.shape[-1])

    @property
    def navigation_shape(self) -> tuple[int, ...]:
        """
        Return the scanned shape, ``()`` for a spot spectrum.

        Every axis but the last, in the same slow-to-fast order the
        ``(height, width)`` scan convention pins — so a spectrum image
        acquired over a :class:`ScanParameters` has this equal to that
        parameter set's :attr:`~ScanParameters.shape`.
        """
        return tuple(int(size) for size in self.data.shape[:-1])


@typing.runtime_checkable
class SpectrumDetector(typing.Protocol):
    """
    A detector that produces energy spectra rather than images.

    An EDX silicon drift detector is the case this was built for, but
    nothing here is EDX-specific: a WDS spectrometer, a micro-XRF head,
    or a cathodoluminescence spectrometer produce the same shape of data
    and want the same calls.

    Contract: call ``start`` before acquiring; ``stop`` pauses
    acquisition and ``start`` may be called again afterwards; call
    ``close`` exactly once when the device will not be used again. The
    same lifecycle :class:`Camera` has, deliberately — an adapter author
    who has written one has written this.

    **Which modes exist is asked, not assumed.** A detector reports
    :attr:`acquisition_modes`, and a caller consults it before driving
    one, exactly as it consults
    :meth:`Instrument.available_controls` before driving a
    control. This project has been bitten before by treating "the setter
    returned" as "the control works" (docs/migration-plan.md, §7), and a
    map on an unsynchronised detector is the same trap with a much more
    expensive failure: it returns a plausible cube in which pixel and
    spectrum have no relationship.
    """

    @property
    def detector_id(self) -> str:
        """Return the stable identifier for this detector."""
        ...

    @property
    def acquisition_modes(self) -> typing.Sequence[str]:
        """Return the modes this detector supports, from :data:`ACQUISITION_MODES`."""
        ...

    def parameters(self) -> SpectrumParameters:
        """Return the settings the next spectrum will be acquired with."""
        ...

    def configure(self, parameters: SpectrumParameters) -> SpectrumParameters:
        """
        Apply new settings and return the ones the device actually took.

        Returned rather than assumed, for the reason
        :meth:`Camera.configure` gives and one more that is specific to
        this class of device: analysers commonly quantise the channel
        count to a power of two and the dispersion to a small set of
        hardware gain steps, so "asked for 7 eV/channel, got 10" is the
        normal outcome. A caller that assumed its request took would
        record an energy axis the detector never used, which shifts every
        identified line.
        """
        ...

    def start(self) -> None:
        """
        Begin acquisition.

        Returns as soon as the device has been told to start; it does
        **not** wait for a spectrum, matching :meth:`Camera.start`.
        """
        ...

    def stop(self) -> None:
        """Pause acquisition."""
        ...

    def acquire_spectrum(self) -> Spectrum:
        """
        Return one spot spectrum; requires ``start`` to have been called.

        Blocks for the configured live time. Whether successive calls
        return independent spectra or a single accumulating one is the
        device's business, and the contract is only that each reports the
        ``live_time_s`` and ``real_time_s`` it actually covers — so a
        caller can tell the two apart from the data rather than from
        documentation.
        """
        ...

    def acquire_map(self, parameters: ScanParameters) -> Spectrum:
        """
        Return a spectrum image over a scanned region.

        Only meaningful when :attr:`acquisition_modes` includes
        :data:`MAP_MODE`; a detector without it should raise rather than
        return something map-shaped.

        Takes :class:`ScanParameters` — the same type the scanner takes —
        because the geometry is the scan's, and because storage then
        calibrates the navigation axes through exactly the path a scanned
        frame uses. The returned :class:`Spectrum` has
        :attr:`Spectrum.navigation_shape` equal to
        :attr:`ScanParameters.shape`.

        **What this call cannot promise, stated plainly.** On real
        hardware a spectrum image is synchronised in *hardware*: either
        the analyser drives the column's external scan input, or the
        column drives and the analyser advances on a pixel-clock line.
        Nothing in this interface establishes either. This method assumes
        the adapter has arranged one of them out of band — through the
        vendor's own map job, which is how Bruker's ESPRIT and Oxford's
        AZtec do it — and requires it to record which, in
        ``metadata["scan_sync"]``.

        **This models the vendor-owned map job, and only that.** It is a
        real and common case: ESPRIT and AZtec both accept "map this
        region" as one instruction to the analyser, and hand back a cube.
        What it is *not* is the general answer, because a scanned
        instrument does not acquire one signal at a time. One pass of the
        probe reads out every detector at once — HAADF and MAADF
        together on the Nion columns this project already drives, and on
        an instrument with an EDX detector fitted, the X-ray spectra
        alongside them. Serial acquisition is the special case, not
        simultaneity.

        Within one scan unit that is now expressed:
        :meth:`Scanner.scan_frames` reads several detector channels out
        of one pass and stamps the frames with the shared
        ``scan_pass_id`` that call establishes. What is still missing is
        the *cross-device* pass: the transport gives each target its own
        connection with no shared trigger, so a scan overlapping this
        map's acquisition overlaps in *wall-clock time* rather than
        sharing a probe position.

        So this method is deliberately the *device-level* primitive and
        not the acquisition-level answer. The acquisition-level answer is
        a pass that yields a set of correlated outputs across devices,
        and it is the same missing concept that blocks simultaneous EELS
        and 4D-STEM — one change, not three. See
        docs/adapters/spectrum-detectors.md for the options and the
        recommendation.
        """
        ...

    def close(self) -> None:
        """Release the device and any background threads it owns."""
        ...


# NXspectrum defines spectrum_0d, _1d and _2d - a spot spectrum, a line, and a
# map - so up to two navigation axes plus energy. spectrum_3d exists there for
# a 3D ROI, which is a serial-sectioning result rather than something a
# detector hands back in one acquisition, so it is refused here rather than
# accepted and then not storable.
_MAX_SPECTRUM_RANK = 3


# Neutral names for the controls an InstrumentController may report through
# available_controls(). Strings rather than an enum so the value survives the
# device-server IPC boundary as plain data (see devices/rpc.py).
STAGE_POSITION_CONTROL = "stage_position"
DEFOCUS_CONTROL = "defocus"
BEAM_BLANKER_CONTROL = "beam_blanker"
ENERGY_OFFSET_CONTROL = "energy_offset"


@typing.runtime_checkable
class Instrument(typing.Protocol):
    """
    What every ``instrument`` target implements, and the one runtime check.

    Three methods, and exactly three on purpose: an identity a caller can
    size a field of view against (:meth:`stage_size_nm`), the capability
    question (:meth:`available_controls`), and the safe-state hook every
    teardown calls (:meth:`park`). That is the whole of what the
    ``isinstance`` call sites were actually asking — "is this an
    instrument target I can hold a session against" — and it is what a
    detector-only server can honestly provide: a Gatan spectrometer has
    an energy offset and no stage, a commodity camera has no controls at
    all, and both are complete instruments *of their kind*.

    Which controls exist is asked through :meth:`available_controls`, not
    through ``isinstance``. The per-control methods live on
    :class:`InstrumentController`, the full control surface for static
    typing, which is deliberately **not** ``runtime_checkable``: a
    ``runtime_checkable`` Protocol's ``isinstance`` is all-or-nothing, so
    checking against the full control surface demanded every method
    regardless of what the instrument said it supported. Two adapters
    (``camera_server.ServerInstrument``, ``gatan_bridge.BridgeInstrument``)
    failed that check while working perfectly, which meant it was testing
    for Nion-shapedness rather than for conformance. This split is the
    fix: the runtime check tests the core every instrument has, and the
    capability list carries the rest.
    """

    def stage_size_nm(self) -> float:
        """Return the usable stage extent, for choosing a sensible field of view."""
        ...

    def available_controls(self) -> typing.Sequence[str]:
        """Return the ``*_CONTROL`` names this instrument actually implements."""
        ...

    def park(self) -> None:
        """
        Put the instrument in a safe unattended state.

        Blanks the beam if a blanker exists, and does nothing — honestly —
        on an instrument with no beam to blank. Deliberately *not* a full
        teardown: stopping detectors and releasing devices belongs to the
        code that owns them (the device server's shutdown handshake), not
        to a single-instrument control surface.
        """
        ...


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

    **Range limits belong behind these setters, not in front of them**,
    and that is the project owner's decision rather than an omission.
    A caller — the viewer's instrument panel included — passes the value
    the operator asked for and lets the instrument refuse it. Clamping in
    a client would mean two sources of truth for a limit that only the
    hardware knows, and a client whose idea of the limit had drifted
    would silently send a *different* value than the one on screen, which
    is worse than an honest refusal.

    One thing a safety limit must never do here is **turn the beam off**.
    Blanking is an operator action with its own control
    (:meth:`set_beam_blanked`); a clamp that blanked as a side effect
    would abort an acquisition to prevent a setting, and an operator who
    lost an hour of beam time to a guard rail would be right to be
    furious.

    This is the *static* face of an instrument: annotate with it when the
    code genuinely calls the control methods (a defocus sweep, an energy
    series), and the type checker holds the implementation to them. It is
    deliberately not ``runtime_checkable`` — ``isinstance`` against it
    raises ``TypeError`` — because the runtime question is never "does
    this object have every method" but "is this an instrument"
    (:class:`Instrument`) followed by "does it serve this control"
    (:meth:`~Instrument.available_controls`). An adapter that implements
    a subset of the controls is a partial *controller* but a whole
    *instrument*, and the type system now says exactly that.

    A superset of :class:`Instrument` by re-declaration rather than by
    inheritance, and the distinction is load-bearing: ``runtime_checkable``
    is *inherited* by protocol subclasses (the flag is a class attribute,
    and there is no opt-out), so a subclass of :class:`Instrument` would
    quietly keep the all-or-nothing ``isinstance`` this split exists to
    remove. Protocols are structural, so every implementation — and every
    value typed as this protocol — still satisfies :class:`Instrument`
    without naming it.
    """

    def stage_size_nm(self) -> float:
        """Return the usable stage extent, for choosing a sensible field of view."""
        ...

    def available_controls(self) -> typing.Sequence[str]:
        """Return the ``*_CONTROL`` names this instrument actually implements."""
        ...

    def park(self) -> None:
        """
        Put the instrument in a safe unattended state.

        See :meth:`Instrument.park`, whose contract this repeats.
        """
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

    def energy_offset_ev(self) -> float:
        """Return the spectrometer's energy offset, in electronvolts."""
        ...

    def set_energy_offset_ev(self, offset_ev: float) -> None:
        """Set the spectrometer's energy offset, in electronvolts."""
        ...

    def is_beam_blanked(self) -> bool:
        """Return whether the beam is currently blanked."""
        ...

    def set_beam_blanked(self, *, blanked: bool) -> None:
        """Blank or unblank the beam."""
        ...
