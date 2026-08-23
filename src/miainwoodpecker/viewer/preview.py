"""
An in-process synthetic instrument, for iterating on the viewer's UI.

Run with ``miainwoodpecker-preview`` (or ``uv run --extra viewer
miainwoodpecker-preview``). Needs the ``viewer`` extra and nothing else:
no vendor SDK, no ``device`` extra, no device server subprocess.

Why this exists alongside ``--server-module camera_server``
-----------------------------------------------------------
:mod:`miainwoodpecker.viewer.app` can already open a window against
synthesised frames, and that path is the honest end-to-end exercise:
frames cross a real socket from a real subprocess, so it proves the IPC,
the handshake, and the shutdown. This module deliberately gives up all
of that. The devices here are ordinary Python objects living in the
viewer's own process, which buys three things the subprocess path cannot:

* **Startup is an import.** There is no server to spawn, no port to
  bind, and no handshake to wait out, so the edit-run loop on a panel's
  layout is as long as napari takes to draw.
* **The window is reachable in whatever shape you need.** Scan-only,
  camera-only, two cameras, an instrument publishing one control out of
  four — the Instrument and Devices panels build themselves from what the
  instrument reports, and those branches are otherwise reachable only by
  owning the corresponding hardware.
* **A failure is the viewer's.** With no transport under it, a widget
  that misbehaves here misbehaves in code you just edited.

It is a UI development tool, and the numbers it produces are not
measurements of anything. That is why the backend it reports is
:data:`PREVIEW_BACKEND` rather than ``simulated``: the panel's top line
names it, so a screenshot taken from this window can never be mistaken
for one taken from the microscope simulator, let alone an instrument.
Recordings made here are real NeXus files full of invented data — write
them to a scratch session directory, not to one holding real work.

The controls are wired to the image, on purpose
-----------------------------------------------
Blanking the beam collapses the signal, defocusing damps the contrast,
moving the stage moves the field of view, and driving the spectrometer's
energy offset moves the zero-loss peak across the EELS camera's channels.
A preview whose dials did nothing would let a broken Instrument panel — a
signal never connected, a setter called on the wrong object — look
exactly like a working one, and that is the panel this module exists to
iterate on.

Two detectors, and two kinds of pass
------------------------------------
:class:`PreviewCamera` is a Ronchigram camera and
:class:`PreviewEELSCamera` is an EEL spectrometer; which one a target
gets is decided by its *name*, so an instrument built with two cameras
serves a real spectrometer on the ``eels_camera`` target rather than a
Ronchigram wearing that label.

**What makes it a spectrometer is the axis, not the rank.** A
spectrometer is a detector one of whose axes is calibrated in energy
rather than in space; how many axes it has besides that one is the
device's business. The ordinary EELS readout is 2D — the spectrum
dispersed across a camera, and this one delivers that by default —
and summing the non-dispersive direction to 1D is a mode an operator
*chooses*, not what the word means. Keeping the whole 2D readout is a
real experiment rather than a misconfiguration: momentum-resolved EELS
and angle-resolved work need exactly it.

So what a synchronised pass does with a target follows its **readout
mode** and never its type. Projecting, it contributes a rank-3 spectrum
image; imaging, it contributes a 4D stack of whole detector readouts —
the same container a Ronchigram camera fills, because at that point the
two are the same shape of data and differ only in what their axes mean.
Which is why the axes travel with it: see :meth:`_diffraction_stack`.
One method, :meth:`PreviewScanner.scan_synchronised`, therefore covers
4D-STEM, 2D-readout spectrum imaging and projected spectrum imaging
without branching on what kind of detector it was handed.

**The synthetic data encodes something checkable**, which is the whole
reason either model is more than decoration. A diffraction pattern is
deflected by the specimen's local phase gradient, so a centre-of-mass
map reconstructs that gradient; a spectrum's silicon and carbon edges
vary with what the probe is standing on, so a silicon map integrated out
of a spectrum image rises and falls with the HAADF channel *of the same
pass*. An analysis run against a cube of identical patterns, or a
spectrum image of one repeated spectrum, would "succeed" while proving
nothing.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import math
import typing

import numpy as np

from miainwoodpecker.devices.interface import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    IMAGE_READOUT,
    PROJECTED_READOUT,
    SCAN_SYNC_SCANNER,
    STAGE_POSITION_CONTROL,
    CameraParameters,
    DiffractionStack,
    Frame,
    ScanParameters,
    ScanPass,
    Spectrum,
    validate_binning,
)
from miainwoodpecker.devices.rpc import CAMERA_TARGET_NAMES, SCANNER_TARGET
from miainwoodpecker.storage.calibration import METADATA_KEY, FrameCalibration
from miainwoodpecker.storage.spectra import EELS_TECHNIQUE, TECHNIQUE_KEY

if typing.TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

#: The backend name this instrument reports, and never ``simulated``.
#: The Instrument panel shows it verbatim, which is the whole point: a
#: window full of invented data should say so on its face.
PREVIEW_BACKEND = "preview"

#: Every control the preview knows how to publish, in panel order.
PREVIEW_CONTROLS = (
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    STAGE_POSITION_CONTROL,
    BEAM_BLANKER_CONTROL,
)

_CHANNEL_NAMES = ("HAADF", "MAADF")
# Atomic-column spacing, near silicon's projected <110> separation. The
# figure is chosen to be *representative* rather than arbitrary: the
# panel's default field of view is 15 nm, so this puts fifty-odd columns
# across the frame. An earlier 12 nm gave barely one period, and a
# display of two grey blobs is no use for judging a colormap, an
# autocontrast pass, or anything else the viewer does to an image.
_SPECIMEN_SPACING_NM = 0.3
_SPECIMEN_BACKGROUND = 0.5
_SPECIMEN_CONTRAST = 0.4
# Defocus at which lattice contrast has fallen by 1/e. At atomic
# resolution real contrast dies within tens of nanometres, so this is
# both about right and a usable dial: the panel's first few tens of
# nanometres visibly soften the image instead of doing nothing until
# some cliff.
_DEFOCUS_ENVELOPE_NM = 100.0
_NOISE_LEVEL = 0.02
# A fraction of a lattice period per pass: enough that the live display
# is visibly alive, little enough that it reads as drift rather than as
# the image jumping.
_DRIFT_NM_PER_PASS = 0.02
_STAGE_SIZE_NM = 2.0e6
_CAMERA_PIXELS = 128
_BINNING_VALUES = (1, 2, 4)
_DEFAULT_EXPOSURE_MS = 20.0
_RONCHIGRAM_RADIUS = 0.55
_RONCHIGRAM_RINGS = 9.0
_MAX_PREVIEW_CAMERAS = len(CAMERA_TARGET_NAMES)
# How far the bright-field disc is pushed off axis per unit of local
# specimen slope, in detector half-widths. Sized so an ordinary lattice
# gradient moves the disc a visible fraction of the detector without
# ever walking it off the edge, where a centre-of-mass would saturate
# and stop reconstructing anything.
_DEFLECTION_PER_UNIT_SLOPE = 0.35
# The Ronchigram camera's angular scale, in milliradians per unbinned
# pixel. Attached to its frames so the projected-readout refusal below
# can say what the axis *is* rather than only that it is not energy —
# and so the preview's camera frames carry axes at all, which every
# other adapter's do.
_RONCHIGRAM_MRAD_PER_PIXEL = 0.4
_RNG_SEED = 20260816
_CAMERA_SEED = _RNG_SEED + 1

# --- The EELS spectrometer -------------------------------------------
#
# The energy axis is nionswift-usim's, not this module's invention:
# 0.5 eV per channel with channel 0 at -20 eV, which is what that
# simulator's EELS camera publishes through its calibration controls and
# what docs/pre-hardware-work.md §1 measured. The preview's spectrometer
# therefore lands on the same axis as the backend this project validated
# its calibration path against, so a spectrum from one is directly
# comparable with a spectrum from the other.
#
# 1340 channels of it spans -20 to 650 eV, which is what makes the model
# below possible: the zero-loss peak, a plasmon, the silicon L2,3 edge at
# 99.8 eV and the carbon K edge at 284.2 eV all fit in one acquisition.
# 1340x100 is the EEL spectrometer on SuperSTEM 1, so the preview opens
# the window against a detector shape an operator there will recognise
# rather than a round number.
_EELS_CHANNELS = 1340
_EELS_DISPERSION_EV = 0.5
_EELS_BASE_OFFSET_EV = -20.0
# Rows in the unprojected readout. A spectrometer disperses onto a 2D
# sensor, and what that sensor sees is a horizontal streak a few rows
# tall rather than an evenly lit rectangle - so the rows exist, carry no
# information the projection loses, and are few because the interesting
# direction is the other one.
_EELS_ROWS = 100
_EELS_STREAK_ROWS = 6.0
# Binning offered per axis, and deliberately not the same set. Binning
# rows trades dynamic range for signal-to-noise and is what a
# spectrometer is routinely run with, so the range down goes all the way;
# binning channels spends the energy resolution the instrument exists to
# deliver, so only a token amount is offered across.
#
# The numbers are a real detector's rather than invented: the EEL
# spectrometer on SuperSTEM 1 reads out 1340x100 and bins up to 100x
# vertically. **That top factor is the whole sensor height**, so binning
# the rows all the way is the same operation as PROJECTED_READOUT - the
# two meet at the same place on real hardware, which is why the readout
# mode exists as well as the binning and not instead of it.
_EELS_ROW_BINNING = (1, 2, 4, 5, 10, 20, 25, 50, 100)
_EELS_CHANNEL_BINNING = (1, 2)
# Energy resolution as the zero-loss peak's FWHM. A Schottky-source STEM
# figure; at this dispersion it is two channels wide, which is what real
# EELS at 0.5 eV/channel looks like rather than a defect of the model.
_EELS_RESOLUTION_EV = 1.0
_FWHM_PER_SIGMA = 2.354_820_045
_EELS_ZLP_COUNTS_PER_MS = 4.0e4
_EELS_DARK_COUNTS_PER_MS = 2.0
# Silicon's bulk plasmon and amorphous carbon's, and how much plasmon
# there is relative to the zero-loss peak - a specimen about a third of
# an inelastic mean free path thick, which is the range a real EELS
# operator aims for.
_SILICON_PLASMON_EV = 16.7
_CARBON_PLASMON_EV = 25.0
_PLASMON_FWHM_EV = 4.0
_PLASMON_RATIO = 0.35
# The two core-loss edges, at their real onsets. The heights are counts
# in the first channel above the onset, and they are chosen *relative to
# the background there* rather than in isolation: the power law below
# falls as E^-3, so it is about 113 counts per millisecond at the silicon
# onset and about 5 at the carbon one. Both edges therefore rise clear of
# it, which is what a spectrum from a thin specimen at a sensible dose
# looks like. Sizing them without that comparison gave a silicon edge
# buried in its own background — visible in the data only as a slightly
# slower decay, which is a fair model of a *bad* acquisition and no use
# as the thing a demonstration is pointed at.
_SILICON_L_EDGE_EV = 99.8
_CARBON_K_EDGE_EV = 284.2
_SILICON_EDGE_COUNTS_PER_MS = 250.0
_CARBON_EDGE_COUNTS_PER_MS = 60.0
# Each edge is a sharp onset decaying as a power law, which is the shape
# a background-subtracted edge has. The onset width keeps the first
# channel finite rather than dividing by zero at the edge itself.
_EDGE_DECAY = 2.5
_EDGE_ONSET_WIDTH_EV = 5.0
# The pre-edge background: AE^-r, the power law every EELS quantification
# fits and subtracts. Referenced at 50 eV so the constant is a count rate
# somewhere legible rather than an extrapolation to zero energy.
_BACKGROUND_COUNTS_PER_MS = 900.0
_BACKGROUND_DECAY = 3.0
_BACKGROUND_REFERENCE_EV = 50.0
# What a live EELS view sees with the probe parked: half silicon, half
# carbon film. A spectrum image varies this per beam position, which is
# the whole point of one.
_EELS_LIVE_FRACTION = 0.5
_EELS_SEED_OFFSET = 7
# Which served target is the spectrometer. One of
# `rpc.CAMERA_TARGET_NAMES`, spelled out here because that tuple is a
# list of every camera target rather than a lookup, and this module needs
# the one. A test pins that it is still a member.
_EELS_TARGET = "eels_camera"


def _now() -> datetime.datetime:
    """
    Return the current UTC time, for a frame's timestamp.

    Returns
    -------
    datetime.datetime
        The current time, timezone-aware.
    """
    return datetime.datetime.now(tz=datetime.UTC)


class PreviewInstrument:
    """
    Instrument controls held in memory, and reflected in the images.

    Implements
    :class:`~miainwoodpecker.devices.interface.InstrumentController`. The
    scanner and camera read their state from the instrument they were
    given, so setting a control here changes what the next frame looks
    like — see this module's docstring for why that is load-bearing
    rather than decorative.

    Parameters
    ----------
    controls : Iterable[str] | None
        Which of :data:`PREVIEW_CONTROLS` to publish from
        ``available_controls``, or None for all of them. Publishing a
        subset is how the panel's "an unpublished control gets no row"
        rule is exercised without owning a microscope that lacks a
        blanker.
    targets : Iterable[str]
        Device target names to report from :meth:`describe`. Normally
        set by :func:`build_preview_devices`, which knows what it built.

    Raises
    ------
    ValueError
        If ``controls`` names something that is not a known control. A
        typo would otherwise silently produce a panel with a missing row,
        which looks exactly like the feature under test working.
    """

    def __init__(
        self,
        controls: Iterable[str] | None = None,
        targets: Iterable[str] = (),
    ) -> None:
        published = (
            list(PREVIEW_CONTROLS) if controls is None else [str(c) for c in controls]
        )
        unknown = [name for name in published if name not in PREVIEW_CONTROLS]
        if unknown:
            msg = (
                f"unknown control(s) {unknown}; "
                f"the preview publishes {list(PREVIEW_CONTROLS)}"
            )
            raise ValueError(msg)
        self._controls = published
        self._targets = [str(name) for name in targets]
        self._defocus_nm = 0.0
        self._energy_offset_ev = 0.0
        self._stage_nm = (0.0, 0.0)
        self._blanked = False

    def describe(self) -> dict[str, object]:
        """
        Report the backend and the devices served.

        Returns
        -------
        dict[str, object]
            The same shape a device server's ``describe`` returns, so the
            Instrument panel needs no preview-specific branch.
        """
        return {
            "backend": PREVIEW_BACKEND,
            "targets": list(self._targets),
            "controls": list(self._controls),
        }

    def stage_size_nm(self) -> float:
        """
        Return the usable stage extent, in nanometres.

        Returns
        -------
        float
            A two-millimetre stage, which is the right order of magnitude
            for choosing a field of view.
        """
        return _STAGE_SIZE_NM

    def available_controls(self) -> Sequence[str]:
        """
        Return the control names this instrument publishes.

        Returns
        -------
        typing.Sequence[str]
            The published subset, in panel order.
        """
        return list(self._controls)

    def park(self) -> None:
        """Blank the beam, leaving the instrument safe to walk away from."""
        self._blanked = True

    def defocus_nm(self) -> float:
        """
        Return the current defocus, in nanometres.

        Returns
        -------
        float
            The defocus last set.
        """
        return self._defocus_nm

    def set_defocus_nm(self, defocus_nm: float) -> None:
        """
        Set the defocus, in nanometres.

        Parameters
        ----------
        defocus_nm : float
            The requested defocus. Not range-checked: an instrument's
            limits belong to the instrument, and this one has none.
        """
        self._defocus_nm = float(defocus_nm)

    def energy_offset_ev(self) -> float:
        """
        Return the spectrometer's energy offset, in electronvolts.

        Returns
        -------
        float
            The offset last set.
        """
        return self._energy_offset_ev

    def set_energy_offset_ev(self, offset_ev: float) -> None:
        """
        Set the spectrometer's energy offset, in electronvolts.

        Parameters
        ----------
        offset_ev : float
            The requested offset.
        """
        self._energy_offset_ev = float(offset_ev)

    def stage_position_nm(self) -> tuple[float, float]:
        """
        Return the stage position as ``(y, x)``, in nanometres.

        Returns
        -------
        tuple[float, float]
            The position last set.
        """
        return self._stage_nm

    def set_stage_position_nm(self, y_nm: float, x_nm: float) -> None:
        """
        Move the stage to an absolute ``(y, x)`` position, in nanometres.

        Parameters
        ----------
        y_nm : float
            Slow-axis position.
        x_nm : float
            Fast-axis position.
        """
        self._stage_nm = (float(y_nm), float(x_nm))

    def is_beam_blanked(self) -> bool:
        """
        Return whether the beam is currently blanked.

        Returns
        -------
        bool
            True if blanked.
        """
        return self._blanked

    def set_beam_blanked(self, *, blanked: bool) -> None:
        """
        Blank or unblank the beam.

        Parameters
        ----------
        blanked : bool
            True to blank.
        """
        self._blanked = bool(blanked)

    def contrast_envelope(self) -> float:
        """
        Return how much specimen contrast survives the current defocus.

        A Gaussian damping envelope rather than a real transfer function:
        the property the viewer needs is that turning the Defocus dial
        visibly softens the image and that zero is sharpest, which this
        has and a two-line phase-plate model would not obviously improve.

        Returns
        -------
        float
            A factor in ``(0, 1]``, 1 at exact focus.
        """
        return math.exp(-((self._defocus_nm / _DEFOCUS_ENVELOPE_NM) ** 2))


class _SyntheticSource:
    """
    Shared machinery for the preview's two detectors.

    Holds the instrument the detector reads its state from, a seeded
    random generator (so a preview session is reproducible, and a
    screenshot can be retaken), and the frame counter whose gaplessness
    the storage layer relies on.

    Parameters
    ----------
    instrument : PreviewInstrument | None
        The instrument whose controls shape the images, or None to own a
        private one so the detector can be built standalone.
    seed : int
        Seed for this source's random generator.
    """

    def __init__(self, instrument: PreviewInstrument | None, seed: int) -> None:
        self._instrument = instrument if instrument is not None else PreviewInstrument()
        self._rng = np.random.default_rng(seed)
        self._frame_index = 0

    @property
    def instrument(self) -> PreviewInstrument:
        """
        Return the instrument whose state shapes this source's frames.

        Returns
        -------
        PreviewInstrument
            The attached instrument.
        """
        return self._instrument

    def _next_frame_index(self) -> int:
        """
        Return the next frame index, advancing the counter.

        Returns
        -------
        int
            A monotonic, gapless index counting frames produced.
        """
        index = self._frame_index
        self._frame_index += 1
        return index

    def _noise(self, shape: tuple[int, ...]) -> np.ndarray:
        """
        Return a noise field of the given shape.

        Parameters
        ----------
        shape : tuple[int, ...]
            The shape to fill.

        Returns
        -------
        numpy.ndarray
            Uniform noise in ``[0, _NOISE_LEVEL)``, float32.
        """
        return self._rng.random(shape, dtype=np.float32) * _NOISE_LEVEL


class PreviewScanner(_SyntheticSource):
    """
    A scan unit imaging a synthetic square lattice.

    Implements :class:`~miainwoodpecker.devices.interface.Scanner`. The
    specimen is fixed in stage coordinates, so moving the stage moves the
    field of view across it and the image changes the way an operator
    expects; a slow drift between passes keeps the live display moving,
    which is what makes a stalled refresh timer visible rather than
    indistinguishable from a correct still image.

    Parameters
    ----------
    instrument : PreviewInstrument | None
        The instrument supplying defocus, stage position, and blanker
        state, or None to own a private one.
    cameras : Mapping[str, _PreviewCameraBase] | None
        Cameras wired to this column, readable during a synchronised
        pass. None for a scan unit with nothing attached to it.
    """

    def __init__(
        self,
        instrument: PreviewInstrument | None = None,
        cameras: Mapping[str, _PreviewCameraBase] | None = None,
    ) -> None:
        super().__init__(instrument, _RNG_SEED)
        self._pass_index = 0
        # The cameras this scanner can read out *during* a pass. Held by
        # the scanner because that is where the capability lives: a
        # synchronised acquisition is the scan unit driving the detector
        # trigger, so a camera nothing has wired to the column is not
        # synchronisable however reachable it is otherwise.
        self._cameras: dict[str, _PreviewCameraBase] = dict(cameras or {})

    @property
    def scanner_id(self) -> str:
        """
        Return the stable identifier for this scanner.

        Returns
        -------
        str
            The scanner's id.
        """
        return "preview_scanner"

    @property
    def channel_names(self) -> Sequence[str]:
        """
        Return the detector channel names.

        Returns
        -------
        typing.Sequence[str]
            Two channels, as a Nion-style scan unit has.
        """
        return list(_CHANNEL_NAMES)

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """
        Scan once and return the frame from one detector channel.

        Parameters
        ----------
        parameters : ScanParameters
            Scan geometry and timing.
        channel : int
            Which detector channel to read out.

        Returns
        -------
        Frame
            The scanned frame and its metadata.
        """
        return self.scan_frames(parameters, [channel])[0]

    def scan_frames(
        self,
        parameters: ScanParameters,
        channels: Sequence[int],
    ) -> Sequence[Frame]:
        """
        Scan once and return one frame per requested channel.

        One pass really is one pass here: the drift offset and the
        sampled field are computed once and every returned frame is read
        out of them, so per-pixel arithmetic between the frames compares
        the same probe positions, as the interface promises.

        Parameters
        ----------
        parameters : ScanParameters
            Scan geometry and timing, shared by every returned frame.
        channels : Sequence[int]
            Detector channel indices to read out during the pass.

        Returns
        -------
        typing.Sequence[Frame]
            One frame per requested channel, in request order.

        Raises
        ------
        ValueError
            If no channels are requested, or one is requested twice.
        IndexError
            If a channel index this scanner does not have is requested.
        """
        requested = list(channels)
        if not requested:
            msg = "a scan pass must read at least one channel"
            raise ValueError(msg)
        if len(set(requested)) != len(requested):
            msg = (
                f"channels {requested} names a detector twice; "
                "one detector cannot be read out twice in one pass"
            )
            raise ValueError(msg)
        for index in requested:
            if not 0 <= index < len(_CHANNEL_NAMES):
                msg = (
                    f"channel {index} does not exist on {self.scanner_id}; "
                    f"it has {len(_CHANNEL_NAMES)}"
                )
                raise IndexError(msg)

        pass_id = f"{self.scanner_id}-pass-{self._pass_index}"
        drift_nm = self._pass_index * _DRIFT_NM_PER_PASS
        self._pass_index += 1
        lattice = self._sample_specimen(parameters, drift_nm)
        timestamp = _now()
        return [
            self._read_out(
                lattice,
                parameters=parameters,
                channel=index,
                pass_id=pass_id,
                simultaneous=requested,
                timestamp=timestamp,
            )
            for index in requested
        ]

    def _sample_specimen(
        self,
        parameters: ScanParameters,
        drift_nm: float,
    ) -> np.ndarray:
        """
        Return the lattice modulation over the scanned field.

        Parameters
        ----------
        parameters : ScanParameters
            Scan geometry; its field of view and shape set the sampling.
        drift_nm : float
            How far the specimen has crept since the first pass.

        Returns
        -------
        numpy.ndarray
            Values in ``[-1, 1]``, before contrast and blanking.
        """
        height, width = parameters.shape
        span_y_nm, span_x_nm = parameters.fov_size_nm
        stage_y_nm, stage_x_nm = self._instrument.stage_position_nm()
        y_nm = np.linspace(0.0, span_y_nm, height, dtype=np.float32)
        y_nm += stage_y_nm + drift_nm
        x_nm = np.linspace(0.0, span_x_nm, width, dtype=np.float32)
        x_nm += stage_x_nm
        wavenumber = 2.0 * np.pi / _SPECIMEN_SPACING_NM
        return np.outer(np.cos(y_nm * wavenumber), np.cos(x_nm * wavenumber))

    def _read_out(  # noqa: PLR0913 - one call site; splitting it would only hide the arity
        self,
        lattice: np.ndarray,
        *,
        parameters: ScanParameters,
        channel: int,
        pass_id: str,
        simultaneous: Sequence[int],
        timestamp: datetime.datetime,
    ) -> Frame:
        """
        Read one detector channel out of an already-scanned pass.

        Parameters
        ----------
        lattice : np.ndarray
            The pass's sampled specimen modulation.
        parameters : ScanParameters
            The pass's scan geometry.
        channel : int
            Which channel to read out.
        pass_id : str
            Identifier shared by every frame from this pass.
        simultaneous : Sequence[int]
            Every channel read out during this pass.
        timestamp : datetime.datetime
            The pass's acquisition time.

        Returns
        -------
        Frame
            The channel's frame.
        """
        instrument = self._instrument
        if instrument.is_beam_blanked():
            data = self._noise(lattice.shape)
        else:
            # The two channels differ in how steeply they map the same
            # scattering: a high-angle detector's contrast is the harder
            # of the two, so squaring it stands in for that.
            normalised = (lattice + 1.0) / 2.0
            signal = normalised**2 if channel == 0 else normalised
            data = (
                _SPECIMEN_BACKGROUND
                + _SPECIMEN_CONTRAST * instrument.contrast_envelope() * signal
                + self._noise(lattice.shape)
            )
        return Frame(
            data=np.asarray(data, dtype=np.float32),
            timestamp=timestamp,
            metadata={
                "device_id": self.scanner_id,
                "frame_index": self._next_frame_index(),
                "channel_index": channel,
                "channel_name": _CHANNEL_NAMES[channel],
                "scan_pass_id": pass_id,
                "simultaneous_channels": list(simultaneous),
                "fov_nm": parameters.fov_nm,
                "fov_size_nm": list(parameters.fov_size_nm),
                "pixel_time_us": parameters.pixel_time_us,
                "defocus_nm": instrument.defocus_nm(),
            },
        )

    def synchronised_targets(self) -> Sequence[str]:
        """
        Return the camera targets this scanner can read out during a pass.

        Returns
        -------
        typing.Sequence[str]
            Target names, or empty if no camera is wired to the column.
        """
        return list(self._cameras)

    def scan_synchronised(
        self,
        parameters: ScanParameters,
        *,
        channels: Sequence[int] = (),
        targets: Sequence[str] = (),
        into: Mapping[str, np.ndarray] | None = None,
    ) -> ScanPass:
        """
        Traverse the probe once, reading every named signal out per position.

        The preview's implementation is a real one, which is the whole
        reason it exists: the diffraction pattern at each beam position
        is deflected by the **local gradient** of the specimen field, so
        the centre of mass across the datacube reconstructs that
        gradient. That makes the output something a 4D-STEM analysis can
        be tested against — a centre-of-mass or DPC map computed from it
        has a right answer — rather than a cube of identical patterns
        that any analysis would "succeed" on.

        The nionswift-usim backend implements none of this, on purpose;
        see :class:`~miainwoodpecker.devices.interface.SynchronisedScanner`
        for the measurement showing it cannot.

        Parameters
        ----------
        parameters : ScanParameters
            The beam-position grid.
        channels : Sequence[int]
            Intensity channels to read out during the pass.
        targets : Sequence[str]
            Camera targets to read out at each position.
        into : Mapping[str, np.ndarray] | None
            Pre-allocated destination cubes by target name, filled in
            place. None allocates. A preview that allocated the wrong
            way would be a poor model of the acquisition it exists to
            prototype, so this path is the same one the real adapters
            will take.

        Returns
        -------
        ScanPass
            The pass and everything read out of it.

        Raises
        ------
        ValueError
            If nothing was asked for, a channel is repeated, or a target
            is not one this scanner can synchronise.
        IndexError
            If a channel this scanner does not have is requested.
        """
        requested = list(channels)
        wanted = list(targets)
        if not requested and not wanted:
            msg = (
                "a synchronised pass must read something out - name at "
                "least one channel or one target"
            )
            raise ValueError(msg)
        if len(set(requested)) != len(requested):
            msg = f"channels {requested} names a detector twice"
            raise ValueError(msg)
        for index in requested:
            if not 0 <= index < len(_CHANNEL_NAMES):
                msg = (
                    f"channel {index} does not exist on {self.scanner_id}; "
                    f"it has {len(_CHANNEL_NAMES)}"
                )
                raise IndexError(msg)
        unknown = [name for name in wanted if name not in self._cameras]
        if unknown:
            msg = (
                f"cannot synchronise {unknown}; {self.scanner_id} can "
                f"synchronise {list(self._cameras)}"
            )
            raise ValueError(msg)

        pass_id = f"{self.scanner_id}-sync-{self._pass_index}"
        drift_nm = self._pass_index * _DRIFT_NM_PER_PASS
        self._pass_index += 1
        lattice = self._sample_specimen(parameters, drift_nm)
        timestamp = _now()
        images = [
            self._read_out(
                lattice,
                parameters=parameters,
                channel=index,
                pass_id=pass_id,
                simultaneous=requested,
                timestamp=timestamp,
            )
            for index in requested
        ]
        # What a target contributes is decided by the readout mode it is
        # *in*, not by what kind of detector it is: a spectrometer left
        # imaging really does produce a full frame per beam position, and
        # storing that as a 4D stack is the truthful thing to do with it.
        # The camera's settings are the acquisition's settings, which is
        # also why `DiffractionStack` carries one `CameraParameters` for
        # the whole stack rather than one per position.
        destinations = dict(into or {})
        # Every device that read this traversal out, named once so a
        # spectrum image can say what shares its probe positions. The
        # scanner appears as one id covering all its channels, which is
        # what it is: one device, several detectors.
        participants = [
            *([self.scanner_id] if requested else []),
            *(self._cameras[name].camera_id for name in wanted),
        ]
        diffraction = {}
        spectra = {}
        for name in wanted:
            if self._cameras[name].parameters().readout == PROJECTED_READOUT:
                spectra[name] = self._spectrum_image(
                    name,
                    lattice,
                    parameters=parameters,
                    pass_id=pass_id,
                    timestamp=timestamp,
                    destination=destinations.get(name),
                    participants=participants,
                )
            else:
                diffraction[name] = self._diffraction_stack(
                    name,
                    lattice,
                    pass_id=pass_id,
                    timestamp=timestamp,
                    destination=destinations.get(name),
                )
        return ScanPass(
            pass_id=pass_id,
            parameters=parameters,
            images=images,
            # Every synthetic signal here comes from one loop over one
            # sampled region, so the correlation is a fact about how it
            # was produced. Reported as scanner-mastered because that is
            # what this loop is: the scan drives and the camera follows.
            scan_sync=SCAN_SYNC_SCANNER,
            diffraction=diffraction,
            spectra=spectra,
        )

    def _spectrum_image(  # noqa: PLR0913 - one call site; splitting it would only hide the arity
        self,
        name: str,
        lattice: np.ndarray,
        *,
        parameters: ScanParameters,
        pass_id: str,
        timestamp: datetime.datetime,
        destination: np.ndarray | None,
        participants: Sequence[str],
    ) -> Spectrum:
        """
        Build one spectrometer's spectrum image for an already-traversed pass.

        The spectrum-side twin of :meth:`_diffraction_stack`, and it
        differs in one thing that matters: a
        :class:`~miainwoodpecker.devices.interface.Spectrum` **cannot
        exist without its energy axis**, so a target that is projecting
        but has no dispersive direction is refused here rather than
        stored as counts against nothing. Nothing this module assembles
        can reach that state — a camera with no dispersive axis refuses a
        projected readout in ``configure`` — but the scanner accepts the
        cameras it is given, and a detector that lies about its own axes
        should meet an error rather than produce a spectrum image whose
        energies are pixel indices.

        The composition under the probe is read from the same sampled
        specimen field the image channels are read from, which is what
        makes a silicon map computed from this pass track the HAADF
        channel of the same pass rather than merely resemble it.

        Parameters
        ----------
        name : str
            The camera's target name.
        lattice : np.ndarray
            The pass's sampled specimen modulation, one value per beam
            position.
        parameters : ScanParameters
            The pass's scan geometry, recorded so storage calibrates the
            navigation axes through the path a scanned frame uses.
        pass_id : str
            Identifier shared by every output of this pass.
        timestamp : datetime.datetime
            The pass's acquisition time.
        destination : np.ndarray | None
            A pre-allocated cube to fill, or None to allocate one.
        participants : Sequence[str]
            Every device id read out during this pass, recorded as the
            spectrum's ``simultaneous_with``.

        Returns
        -------
        Spectrum
            The rank-3 spectrum image, energy on the last axis.

        Raises
        ------
        ValueError
            If the target has no energy axis, or if ``destination`` does
            not match what the pass will produce.
        """
        camera = self._cameras[name]
        calibration = camera.frame_calibration()
        energy_name = calibration.energy_axis_name()
        if energy_name is None:
            msg = (
                f"{name} is set to a projected readout but reports no "
                f"energy-calibrated axis, so its counts are not spectra; a "
                f"camera with no dispersive direction cannot produce a "
                f"spectrum image"
            )
            raise ValueError(msg)
        energy = calibration.axis(energy_name).converted_to("eV")
        channels = camera.channel_count
        expected = (*lattice.shape, channels)
        cube = destination
        if cube is None:
            cube = np.empty(expected, dtype=np.float32)
        elif tuple(cube.shape) != expected:
            msg = (
                f"destination for {name} has shape {tuple(cube.shape)}, but "
                f"the pass produces {expected} ({lattice.shape} beam "
                f"positions of {channels} energy channels)"
            )
            raise ValueError(msg)
        # Written through position by position for the reason the
        # diffraction cube is: the destination may be an h5py dataset
        # chunked one beam position per chunk, and then each assignment
        # is a single chunk write that overlaps the next position's
        # exposure instead of following the whole acquisition.
        for row in range(lattice.shape[0]):
            for column in range(lattice.shape[1]):
                cube[row, column] = camera.readout_at(
                    self._probe_state(lattice, row, column),
                )
        settings = camera.parameters()
        metadata: dict[str, object] = {
            "device_id": camera.camera_id,
            "camera_type": camera.camera_type,
            "scan_pass_id": pass_id,
            # Which device was master, in the same key a vendor-owned map
            # job would report it under. The pass carries this as a field
            # too; it is repeated here so a spectrum image pulled out of
            # the pass on its own still says how its positions were
            # guaranteed.
            "scan_sync": SCAN_SYNC_SCANNER,
            # Every signal of this pass shares these probe positions, and
            # this call is what establishes that - so naming them is a
            # fact about how the data was produced, not the bare
            # correlation hint docs/adapters/spectrum-detectors.md §2.3
            # rejected as "an id that nothing establishes".
            "simultaneous_with": list(participants),
            "fov_size_nm": list(parameters.fov_size_nm),
            "pixel_time_us": parameters.pixel_time_us,
            "exposure_ms": settings.exposure_ms,
            "binning": settings.binning,
            "defocus_nm": self._instrument.defocus_nm(),
        }
        if camera.camera_type == EELS_TECHNIQUE:
            # The same rule `spectrum_from_projected_frame` follows, for
            # the same reason: once a spectrum image lands in the
            # NXspectrum layout, this string is the only thing on disk
            # distinguishing electron energy losses from X-ray lines.
            metadata[TECHNIQUE_KEY] = EELS_TECHNIQUE
        return Spectrum(
            data=cube,
            timestamp=timestamp,
            energy_offset_ev=energy.offset,
            energy_scale_ev=energy.scale,
            metadata=metadata,
        )

    def _diffraction_stack(
        self,
        name: str,
        lattice: np.ndarray,
        *,
        pass_id: str,
        timestamp: datetime.datetime,
        destination: np.ndarray | None,
    ) -> DiffractionStack:
        """
        Build one camera's datacube for an already-traversed pass.

        Parameters
        ----------
        name : str
            The camera's target name.
        lattice : np.ndarray
            The pass's sampled specimen modulation, one value per beam
            position.
        pass_id : str
            Identifier shared by every output of this pass.
        timestamp : datetime.datetime
            The pass's acquisition time.
        destination : np.ndarray | None
            A pre-allocated cube to fill, or None to allocate one.

        Returns
        -------
        DiffractionStack
            The 4D data, navigation axes first.

        Raises
        ------
        ValueError
            If ``destination`` does not cover the pass's beam positions.
        """
        camera = self._cameras[name]
        detector = camera.readout_shape
        expected = (*lattice.shape, *detector)
        cube = destination
        if cube is None:
            cube = np.empty(expected, dtype=np.float32)
        elif tuple(cube.shape) != expected:
            # The whole shape, not just the navigation axes. Checking half
            # of it let a wrong detector size through to the write loop,
            # where it surfaced as an h5py broadcast TypeError naming
            # neither the target nor the acquisition - and a caller
            # pre-allocating gigabytes deserves to be told which of the
            # two numbers it got wrong.
            msg = (
                f"destination for {name} has shape {tuple(cube.shape)}, but "
                f"the pass produces {expected} ({lattice.shape} beam "
                f"positions of {detector})"
            )
            raise ValueError(msg)
        # Written into the cube position by position rather than built as
        # a list of arrays and stacked. The stacking version allocated
        # every pattern twice and moved the whole dataset a second time,
        # which is the cost this interface's `into` exists to avoid - and
        # a preview that allocated the wrong way would be a poor model of
        # the acquisition it exists to prototype.
        for row in range(lattice.shape[0]):
            for column in range(lattice.shape[1]):
                cube[row, column] = camera.readout_at(
                    self._probe_state(lattice, row, column),
                )
        return DiffractionStack(
            data=cube,
            camera_id=camera.camera_id,
            parameters=camera.parameters(),
            metadata={
                "scan_pass_id": pass_id,
                "device_id": camera.camera_id,
                "camera_type": camera.camera_type,
                "timestamp": timestamp.isoformat(),
                "defocus_nm": self._instrument.defocus_nm(),
                # The per-position axes, carried rather than left to the
                # writer's default. This container's name says
                # "diffraction" and its contents are whatever the
                # detector delivered per beam position - for a
                # spectrometer left imaging, that is a spectrum
                # dispersed across a camera, whose fast axis is energy
                # and not an angle. Storing those axes as an
                # uncalibrated `det_x` would lose the one fact that
                # makes the detector a spectrometer.
                METADATA_KEY: _calibration_metadata(camera.frame_calibration()),
            },
        )

    @staticmethod
    def _probe_state(lattice: np.ndarray, row: int, column: int) -> _ProbeState:
        """
        Return what the specimen looks like under the probe at one position.

        **The one place the specimen model reaches the detectors**, and
        the reason a pass from this instrument is internally consistent:
        both quantities are read from the *same* sampled field that
        :meth:`_read_out` turns into detector intensity, so a
        centre-of-mass map, an elemental map and the HAADF channel of one
        pass are all descriptions of one specimen rather than three
        independent inventions.

        The deflection is the local *slope* of that field, which is what
        a real phase gradient does to the transmitted disc, and the
        composition is its local *value*. Computing the gradient here
        rather than once for the whole grid costs a few neighbour lookups
        per position and keeps the two quantities defined in one place;
        the alternative had the gradient live in the camera loop, where
        the spectrometer could not see it.

        Parameters
        ----------
        lattice : np.ndarray
            The pass's sampled specimen modulation.
        row : int
            Beam position's slow-axis index.
        column : int
            Beam position's fast-axis index.

        Returns
        -------
        _ProbeState
            The local specimen state.
        """
        height, width = lattice.shape
        up = lattice[max(row - 1, 0), column]
        down = lattice[min(row + 1, height - 1), column]
        left = lattice[row, max(column - 1, 0)]
        right = lattice[row, min(column + 1, width - 1)]
        # Centred differences, halved, which is what np.gradient computes
        # in the interior and at the edges degrades to the one-sided
        # difference it uses there.
        scale = _DEFLECTION_PER_UNIT_SLOPE / 2.0
        return _ProbeState(
            deflection=(
                float(down - up) * scale,
                float(right - left) * scale,
            ),
            silicon_fraction=_silicon_fraction(float(lattice[row, column])),
        )

    def close(self) -> None:
        """Release nothing; this scanner owns no resources."""


def _calibration_metadata(calibration: FrameCalibration) -> dict[str, object]:
    """
    Render a frame calibration as the plain data a frame's metadata holds.

    ``metadata["calibration"]`` accepts a :class:`FrameCalibration`
    object as well as this mapping, so the object would work in-process
    — and the preview is in-process. It is rendered anyway, for two
    reasons that outlive that convenience: it is what every other
    adapter puts there (``nion_server`` resolves Nion's own controls into
    exactly this shape, because an object cannot cross the device-server
    boundary), and a stored pass writes its frame metadata as JSON, where
    an object degrades to whatever ``str()`` makes of it.

    Parameters
    ----------
    calibration : FrameCalibration
        The calibration to render.

    Returns
    -------
    dict[str, object]
        One ``{kind, scale, offset, units}`` mapping per axis, keyed
        ``"y"`` and ``"x"``.
    """
    return {
        name: {
            "kind": axis.kind.value,
            "scale": axis.scale,
            "offset": axis.offset,
            "units": axis.units,
        }
        for name, axis in (("y", calibration.y), ("x", calibration.x))
    }


@dataclasses.dataclass(frozen=True)
class _ProbeState:
    """
    What the specimen looks like under the probe at one beam position.

    The scan unit samples the specimen once per pass and every detector
    reads *that* sample out — which is what makes the outputs of a pass
    correlated rather than merely simultaneous. This is the sample, in
    the terms each detector needs it: an angular deflection for a camera
    watching the transmitted disc, a composition for one dispersing
    energy losses.

    Both are carried for every position rather than each detector being
    handed its own kind, because that is the honest shape of the thing:
    one probe position has one local specimen state, and which parts of
    it a given detector is sensitive to is the detector's business.

    Attributes
    ----------
    deflection : tuple[float, float]
        How far the local phase gradient pushes the transmitted disc off
        axis, in detector half-widths, as ``(y, x)``.
    silicon_fraction : float
        How much of what the probe stands on is silicon rather than
        carbon film, in ``[0, 1]``.
    """

    deflection: tuple[float, float]
    silicon_fraction: float


#: What a *live* view sees, with the probe parked rather than scanning:
#: an undeflected disc over half silicon, half carbon film.
_LIVE_PROBE_STATE = _ProbeState(
    deflection=(0.0, 0.0),
    silicon_fraction=_EELS_LIVE_FRACTION,
)


class _PreviewCameraBase(_SyntheticSource):
    """
    The lifecycle and settings every preview camera shares.

    Factored out when the EELS spectrometer arrived, because the two
    detectors differ in exactly one thing — what they make of a probe
    position — and agree on everything else: the start/stop contract, the
    binning refusal, the settings value object, and the metadata
    vocabulary a frame carries. A subclass supplies :meth:`readout_at`,
    :attr:`readout_shape` and :meth:`frame_calibration`; this class
    supplies the rest, including the live view, which is just
    :meth:`readout_at` with the probe parked.

    Implements :class:`~miainwoodpecker.devices.interface.Camera`,
    including its start/stop contract: acquiring before ``start`` is an
    error rather than a frame, because a viewer that never calls
    ``start`` would otherwise appear to work here and stall against every
    real detector.

    Parameters
    ----------
    instrument : PreviewInstrument | None
        The instrument supplying defocus, blanker and energy-offset
        state, or None to own a private one.
    camera_id : str
        Stable identifier, so two preview cameras are distinguishable.
    seed : int | None
        Seed for this camera's noise, or None for the default. Served
        cameras are given different ones by
        :func:`build_preview_devices`, so no two produce bit-identical
        frames: a wiring bug that pointed two camera sections at one
        camera would otherwise look exactly like two working ones.
    """

    #: The vendor's own label for this kind of detector, as
    #: ``Frame.metadata["camera_type"]``. What
    #: :func:`~miainwoodpecker.storage.spectra.spectrum_from_projected_frame`
    #: reads to tell an EELS recording from an EDX one once both wear the
    #: same 1D shape.
    _CAMERA_TYPE = "ronchigram"

    #: Whether this detector can deliver
    #: :data:`~miainwoodpecker.devices.interface.PROJECTED_READOUT`.
    #: A camera with no dispersive direction cannot, and the interface
    #: says such a camera refuses in ``configure`` rather than silently
    #: imaging — so this drives a refusal, not a fallback.
    _CAN_PROJECT = False

    #: Who summed, on a projected frame. ``"sensor"`` when the detector
    #: accumulated before readout, ``"server"`` when something above it
    #: summed a delivered image; see
    #: :class:`~miainwoodpecker.devices.interface.Frame`.
    _PROJECTED_BY = "sensor"

    def __init__(
        self,
        instrument: PreviewInstrument | None = None,
        camera_id: str = "preview_camera",
        seed: int | None = None,
    ) -> None:
        super().__init__(instrument, _CAMERA_SEED if seed is None else seed)
        self._camera_id = camera_id
        self._started = False
        # Every camera starts imaging, including the spectrometer.
        # A projected readout is what an operator switches a spectrometer
        # into for a spectrum image, and starting there would mean the
        # live view of the EELS camera was a line of numbers rather than
        # the dispersed image an operator actually aligns on.
        self._parameters = CameraParameters(
            exposure_ms=_DEFAULT_EXPOSURE_MS,
            readout=IMAGE_READOUT,
        )

    @property
    def camera_id(self) -> str:
        """
        Return the stable identifier for this camera.

        Returns
        -------
        str
            The camera's id.
        """
        return self._camera_id

    @property
    def camera_type(self) -> str:
        """
        Return the vendor-style label for what kind of detector this is.

        Returns
        -------
        str
            The value frames carry as ``metadata["camera_type"]``.
        """
        return self._CAMERA_TYPE

    @property
    def binning_values(self) -> Sequence[int]:
        """
        Return the binning factors this camera supports, ascending.

        Returns
        -------
        typing.Sequence[int]
            The supported factors.
        """
        return list(_BINNING_VALUES)

    def parameters(self) -> CameraParameters:
        """
        Return the settings the next frame will be acquired with.

        Returns
        -------
        CameraParameters
            The current settings.
        """
        return self._parameters

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Apply new settings and return the ones actually taken.

        Parameters
        ----------
        parameters : CameraParameters
            The requested settings.

        Returns
        -------
        CameraParameters
            The settings in force, which these cameras take unchanged.

        Raises
        ------
        ValueError
            If the requested binning is not one this camera offers, or if
            a projected readout is asked of a camera with no dispersive
            direction. Both refused rather than quietly approximated, so
            the viewer's own handling of a rejected setting is reachable.
        """
        validate_binning(self, parameters)
        if parameters.readout == PROJECTED_READOUT and not self._CAN_PROJECT:
            msg = (
                f"{self._camera_id} cannot project: it has no dispersive "
                f"direction, so summing one of its axes would produce a "
                f"line of numbers that is not a spectrum"
            )
            raise ValueError(msg)
        self._parameters = parameters
        return self._parameters

    def start(self) -> None:
        """Begin continuous acquisition."""
        self._started = True

    def stop(self) -> None:
        """Pause continuous acquisition; ``start`` may be called again."""
        self._started = False

    def acquire_frame(self) -> Frame:
        """
        Return the next frame.

        Returns
        -------
        Frame
            Whatever this detector produces at its current settings: 2D
            under :data:`~miainwoodpecker.devices.interface.IMAGE_READOUT`
            and 1D under
            :data:`~miainwoodpecker.devices.interface.PROJECTED_READOUT`.

        Raises
        ------
        RuntimeError
            If called before ``start``, which is the interface's contract.
        """
        if not self._started:
            msg = (
                f"{self._camera_id} has not been started; "
                "call start() before acquire_frame()"
            )
            raise RuntimeError(msg)
        data = self._readout_data()
        metadata: dict[str, object] = {
            "device_id": self._camera_id,
            "frame_index": self._next_frame_index(),
            "camera_name": self._camera_id,
            "camera_type": self._CAMERA_TYPE,
            "exposure_ms": self._parameters.exposure_ms,
            "binning": self._parameters.binning,
            "readout": self._parameters.readout,
            "defocus_nm": self._instrument.defocus_nm(),
            METADATA_KEY: _calibration_metadata(self.frame_calibration()),
        }
        if self._parameters.readout == PROJECTED_READOUT:
            # Present only on a projected frame, as the interface's
            # vocabulary specifies: on an imaged one there is nobody to
            # name, and a key saying "sensor" about a frame nothing summed
            # would be a claim about noise statistics that is not true.
            metadata["projected_by"] = self._PROJECTED_BY
        return Frame(
            data=np.asarray(data, dtype=np.float32),
            timestamp=_now(),
            metadata=metadata,
        )

    def _readout_data(self) -> np.ndarray:
        """
        Return the array a live view sees, with the probe parked.

        Returns
        -------
        numpy.ndarray
            The frame data, before it is wrapped and stamped.
        """
        return self.readout_at(_LIVE_PROBE_STATE)

    @property
    def readout_shape(self) -> tuple[int, ...]:
        """
        Return the shape of one readout at the current settings.

        Asked of the detector rather than derived from it, because a
        synchronised pass allocates its whole destination from this
        answer *before* acquiring anything — and an allocation of the
        wrong size is refused rather than reshaped around.

        Returns
        -------
        tuple[int, ...]
            The per-position array shape: 2D imaging, 1D projecting.

        Raises
        ------
        NotImplementedError
            If a subclass has not said what it produces.
        """
        raise NotImplementedError

    def readout_at(self, state: _ProbeState) -> np.ndarray:
        """
        Return one readout for a probe standing on a given specimen state.

        The per-beam-position call a synchronised pass drives, and
        deliberately **not** :meth:`acquire_frame`: that one advances the
        frame counter and honours the start/stop contract, neither of
        which applies to a readout the scan unit is driving.

        One method for both detectors, which is what lets
        :meth:`PreviewScanner.scan_synchronised` traverse the probe once
        and read out whatever is wired to the column without knowing what
        kind of thing it is.

        Parameters
        ----------
        state : _ProbeState
            The local specimen state at this beam position.

        Returns
        -------
        numpy.ndarray
            The readout, of :attr:`readout_shape`.

        Raises
        ------
        NotImplementedError
            If a subclass has not said what it produces.
        """
        raise NotImplementedError

    def frame_calibration(self) -> FrameCalibration:
        """
        Return the physical axes of the frames this camera produces.

        Not part of :class:`~miainwoodpecker.devices.interface.Camera`,
        and deliberately: a camera publishes its calibration through
        ``metadata["calibration"]``, which is plain data and crosses the
        device-server boundary. This is the preview's *in-process*
        shortcut to the same fact, and it exists because
        :meth:`PreviewScanner.scan_synchronised` has to know whether a
        target's surviving axis is an energy one **before** it acquires
        anything — a spectrum image is allocated from the answer.

        Returns
        -------
        FrameCalibration
            The per-axis calibration, in the frame's ``(y, x)`` order.

        Raises
        ------
        NotImplementedError
            If a subclass has not said what its axes are.
        """
        raise NotImplementedError

    # --- The energy-dispersive contract -------------------------------
    #
    # The two things a scan unit needs from a detector it is going to
    # read out per beam position *as spectra*: how long a spectrum is, so
    # the destination can be allocated before the acquisition starts, and
    # how to produce one. Declared here rather than only on the
    # spectrometer so the contract is visible in one place, and so a
    # camera with no energy axis fails with a sentence naming that fact
    # rather than with a bare AttributeError.

    @property
    def channel_count(self) -> int:
        """
        Return the length of this detector's energy-dispersive axis.

        A property of the detector and its binning, not of the readout
        mode: the dispersive axis is the same length whether or not the
        other one has been summed away.

        Returns
        -------
        int
            How many energy channels a spectrum from this detector has.

        Raises
        ------
        NotImplementedError
            If this camera has no dispersive direction.
        """
        raise NotImplementedError(self._no_projection_message())

    def spectrum_at(self, silicon_fraction: float) -> np.ndarray:
        """
        Return one spectrum for a probe standing on a given composition.

        Parameters
        ----------
        silicon_fraction : float
            How much of what the probe is standing on is silicon rather
            than carbon film, in ``[0, 1]``.

        Returns
        -------
        numpy.ndarray
            Counts per channel.

        Raises
        ------
        NotImplementedError
            If this camera has no dispersive direction.
        """
        del silicon_fraction
        raise NotImplementedError(self._no_projection_message())

    def _no_projection_message(self) -> str:
        """
        Return the sentence a non-dispersive camera refuses projection with.

        Returns
        -------
        str
            The refusal, naming the camera.
        """
        return (
            f"{self._camera_id} has no energy-dispersive axis, so it "
            f"produces no spectra; only a detector with one can be read out "
            f"as spectra"
        )

    def close(self) -> None:
        """Release nothing; these cameras own no resources."""


class PreviewCamera(_PreviewCameraBase):
    """
    A camera producing a synthetic Ronchigram.

    The detector an operator aligns against: a bright central disc
    crossed by rings whose visibility falls with defocus, on an angular
    axis. During a synchronised pass it is the 4D-STEM detector, its disc
    deflected per beam position by the specimen's local phase gradient
    (:meth:`PreviewScanner.scan_synchronised`).

    **It refuses to project**, which is not an omission. A projected
    readout sums the whole non-dispersive direction, and a Ronchigram
    camera has no dispersive direction for the survivor to be — the
    result would be a line of numbers with an angular axis, which
    :func:`~miainwoodpecker.storage.spectra.spectrum_from_projected_frame`
    would then refuse to store as a spectrum, one layer too late to say
    anything useful. The interface asks such a camera to refuse in
    ``configure``, and this one does; the EELS camera below is where a
    projected readout means something.
    """

    @property
    def readout_shape(self) -> tuple[int, ...]:
        """
        Return the square frame this camera produces at the current binning.

        Returns
        -------
        tuple[int, ...]
            ``(pixels, pixels)``.
        """
        down, across = self._parameters.binning_yx
        return (_CAMERA_PIXELS // down, _CAMERA_PIXELS // across)

    def readout_at(self, state: _ProbeState) -> np.ndarray:
        """
        Return one Ronchigram, its disc pushed off axis by the specimen.

        The deflection is the part of the probe state this detector is
        sensitive to; the composition beside it means nothing here, which
        is exactly why the state carries both and lets the detector
        choose.

        Parameters
        ----------
        state : _ProbeState
            The local specimen state at this beam position.

        Returns
        -------
        numpy.ndarray
            The pattern, float32, at the camera's current binning.
        """
        return np.asarray(
            self._ronchigram(self.readout_shape[0], state.deflection),
            dtype=np.float32,
        )

    def frame_calibration(self) -> FrameCalibration:
        """
        Return the angular axes a Ronchigram is measured on.

        Centred on the optic axis, which is the convention
        docs/pre-hardware-work.md §1 found the vendor's own calibration
        controls already using — the offset that arrives from the
        instrument is the centred one.

        Returns
        -------
        FrameCalibration
            Milliradians per pixel on both axes, centred.
        """
        # binning_yx, though this camera only ever bins symmetrically:
        # reading the pair costs nothing and means the arithmetic does not
        # have to be revisited if it ever learns to do otherwise.
        down, _ = self._parameters.binning_yx
        return FrameCalibration.diffraction(
            _RONCHIGRAM_MRAD_PER_PIXEL * down,
            units="mrad",
            shape=self.readout_shape,
        )

    def _ronchigram(
        self,
        pixels: int,
        deflection: tuple[float, float] = (0.0, 0.0),
    ) -> np.ndarray:
        """
        Return a synthetic Ronchigram of the given size.

        A bright central disc crossed by rings whose visibility falls
        with defocus — the shape an operator aligns against, which is
        what makes the Defocus dial's effect legible on this detector.

        Parameters
        ----------
        pixels : int
            Frame side length.
        deflection : tuple[float, float]
            How far the disc is pushed off centre, in detector half-widths,
            as ``(y, x)``. Zero for a live view, and set per beam position
            during a synchronised pass — see
            :meth:`PreviewScanner.scan_synchronised`.

        Returns
        -------
        numpy.ndarray
            The image, float32.
        """
        shape = (pixels, pixels)
        if self._instrument.is_beam_blanked():
            return self._noise(shape)
        axis = np.linspace(-1.0, 1.0, pixels, dtype=np.float32)
        grid_y, grid_x = np.meshgrid(axis, axis, indexing="ij")
        radius = np.hypot(grid_y - deflection[0], grid_x - deflection[1])
        disc = (radius < _RONCHIGRAM_RADIUS).astype(np.float32)
        rings = np.cos(radius * _RONCHIGRAM_RINGS * np.pi)
        envelope = self._instrument.contrast_envelope()
        return (
            _SPECIMEN_BACKGROUND * disc
            + _SPECIMEN_CONTRAST * envelope * disc * rings
            + self._noise(shape)
        )


class PreviewEELSCamera(_PreviewCameraBase):
    """
    An EEL spectrometer camera, and the preview's second kind of signal.

    A spectrometer disperses electrons that have lost energy in the
    specimen across a sensor, so what this produces under
    :data:`~miainwoodpecker.devices.interface.IMAGE_READOUT` is a 2D
    frame in which the fast axis is energy and the slow one is not —
    exactly the case
    :meth:`~miainwoodpecker.storage.calibration.FrameCalibration.spectrum`
    describes — and under
    :data:`~miainwoodpecker.devices.interface.PROJECTED_READOUT` the same
    spectrum with that direction summed away.

    **Neither rank is the "real" one.** What makes this a spectrometer is
    that one axis is calibrated in energy rather than in space; the rest
    of its shape is a fact about the detector behind it, and this one
    happens to have rows. Projecting is a *choice* an operator makes to
    trade the non-dispersive direction for signal-to-noise, and it is
    what an ordinary EELS spectrum image is acquired with — but keeping
    the whole 2D readout per beam position is a real experiment too, not
    a mistake, and this camera supports being read out either way.
    Defaulting to the 2D image is what a spectrometer's live view shows,
    and it is what an operator aligns the spectrum on the detector with.

    **What is modelled, and why each part is there.** The point of a
    synthetic spectrum is that something computed from it has a right
    answer; a plausible-looking curve that encodes nothing would let a
    broken spectrum-image path look exactly like a working one.

    * a **zero-loss peak** at 0 eV, whose position on the *channel* axis
      moves when the spectrometer's energy offset does — that control is
      wired end to end already
      (:func:`~miainwoodpecker.acquisition.sequence.energy_offset_series`),
      and this is the detector on which its effect is visible;
    * a **plasmon**, at silicon's 16.7 eV or amorphous carbon's 25 eV in
      proportion to what the probe is standing on, so the low-loss region
      carries composition too;
    * a **power-law background**, the ``AE^-r`` every EELS
      quantification fits and subtracts — without it, an edge integral
      taken naively would be right, and on real data it is not;
    * the **silicon L2,3 edge** at 99.8 eV and the **carbon K edge** at
      284.2 eV, with heights that are complementary across the specimen.
      That is the checkable answer: a silicon map made from a spectrum
      image tracks the HAADF channel of the same pass, and a carbon map
      is its negative.
    * **Poisson noise**, because counting statistics are what a real
      acquisition trades exposure against.

    What is deliberately **not** modelled: multiple scattering (so
    thickness is a single-scattering fiction and a log-ratio thickness
    measurement from this would be meaningless), the fine structure at
    each edge onset, and any relationship between beam current and count
    rate. The rule
    :mod:`~miainwoodpecker.devices.spectrum_server` sets applies here
    too — a simulator that faked those would invite trusting numbers from
    it.

    The specimen is silicon on a carbon film, which is not an arbitrary
    choice either: the preview's lattice spacing is already silicon's
    projected ``<110>`` separation, so the two halves of the preview
    describe one specimen rather than two.
    """

    _CAMERA_TYPE = "eels"
    _CAN_PROJECT = True
    # The spectrometer accumulates the non-dispersive direction before
    # readout, which is what Nion's `processing = "sum_project"` asks the
    # device to do. One readout's noise, therefore, and the projected
    # frame is generated as such rather than by summing a frame that was
    # itself already noisy.
    _PROJECTED_BY = "sensor"

    def __init__(
        self,
        instrument: PreviewInstrument | None = None,
        camera_id: str = "preview_eels_camera",
        seed: int | None = None,
    ) -> None:
        super().__init__(
            instrument,
            camera_id,
            _CAMERA_SEED + _EELS_SEED_OFFSET if seed is None else seed,
        )

    @property
    def binning_values(self) -> Sequence[int]:
        """
        Return the factors this camera will take on *both* axes at once.

        The intersection of the two per-axis sets, because that is what
        the question means for a detector whose axes differ: a caller
        asking for one number is asking to bin both directions by it, and
        the channel axis is the one that limits how far that can go.

        Returns
        -------
        Sequence[int]
            The symmetric factors, ascending.
        """
        across = set(_EELS_CHANNEL_BINNING)
        return tuple(value for value in _EELS_ROW_BINNING if value in across)

    @property
    def binning_values_yx(self) -> tuple[Sequence[int], Sequence[int]]:
        """
        Return what this spectrometer will bin, per axis — they differ.

        Publishing this is what tells
        :func:`~miainwoodpecker.devices.interface.validate_binning` that
        this detector can tell its axes apart, and it is the reason the
        binning controls appear per axis for this camera and as one
        control for the Ronchigram camera beside it.

        The two axes are not doing the same job. Binning rows together
        trades dynamic range for signal-to-noise and is the routine move
        on a spectrometer, so a wide range is offered down. Binning
        channels together spends spectral resolution, which is the thing
        the instrument exists to provide, so only a token amount is
        offered across — enough that a caller can ask for it deliberately
        and see what it costs, not enough to reach for by accident.

        Returns
        -------
        tuple[Sequence[int], Sequence[int]]
            Row and channel factors, ascending.
        """
        return (_EELS_ROW_BINNING, _EELS_CHANNEL_BINNING)

    @property
    def channel_count(self) -> int:
        """
        Return how many energy channels a frame has at the current binning.

        Returns
        -------
        int
            The dispersive axis's length.
        """
        _, across = self._parameters.binning_yx
        return _EELS_CHANNELS // across

    def frame_calibration(self) -> FrameCalibration:
        """
        Return the energy axis, and the uncalibrated direction beside it.

        Binning multiplies the dispersion, because a binned channel spans
        proportionally more of the axis — the same arithmetic Nion's
        ``build_calibration`` does with its ``relative_scale``, and the
        reason :class:`~miainwoodpecker.devices.interface.CameraParameters`
        holds binning and exposure together as one value.

        **Only the dispersive axis's binning does that.** Binning the
        rows together trades dynamic range for signal-to-noise and
        changes how many rows there are, but it cannot change how many
        electronvolts a channel spans, so the energy scale here reads the
        ``x`` factor alone.

        The offset moves with the spectrometer's energy offset, so the
        zero-loss peak sits at a different *channel* when the control is
        driven while staying at 0 eV — which is the whole point of a
        calibrated axis, and what makes
        :func:`~miainwoodpecker.acquisition.sequence.energy_offset_series`
        demonstrable here.

        Returns
        -------
        FrameCalibration
            An energy ``x`` axis and an uncalibrated ``y`` axis.
        """
        _, across = self._parameters.binning_yx
        return FrameCalibration.spectrum(
            _EELS_DISPERSION_EV * across,
            offset=self._offset_ev(),
            dispersive_axis="x",
        )

    def _offset_ev(self) -> float:
        """
        Return the energy at channel 0, in electronvolts.

        Returns
        -------
        float
            The spectrometer's base offset, shifted by its energy-offset
            control.
        """
        return _EELS_BASE_OFFSET_EV + self._instrument.energy_offset_ev()

    def _energy_axis(self) -> np.ndarray:
        """
        Return the energy of every channel, in electronvolts.

        Returns
        -------
        numpy.ndarray
            One energy per channel, at the current binning.
        """
        axis = self.frame_calibration().x
        return np.asarray(axis.values(self.channel_count), dtype=np.float64)

    def spectrum_at(self, silicon_fraction: float) -> np.ndarray:
        """
        Return one projected spectrum for a given composition.

        The spectrum on its own, whatever readout mode the camera is in —
        which is what makes it worth having beside :meth:`readout_at`:
        the model can be examined without first putting the device into a
        mode, and a caller that wants counts against energy is not asking
        about the detector's rows.

        Parameters
        ----------
        silicon_fraction : float
            How much of what the probe is standing on is silicon rather
            than carbon film, in ``[0, 1]``.

        Returns
        -------
        numpy.ndarray
            Counts per channel, float32, at the camera's current settings.
        """
        return np.asarray(
            self._counts(float(silicon_fraction)),
            dtype=np.float32,
        )

    @property
    def readout_shape(self) -> tuple[int, ...]:
        """
        Return the shape of one readout at the current settings.

        Returns
        -------
        tuple[int, ...]
            ``(channels,)`` projecting, ``(rows, channels)`` imaging.
        """
        if self._parameters.readout == PROJECTED_READOUT:
            return (self.channel_count,)
        return (self._rows, self.channel_count)

    def readout_at(self, state: _ProbeState) -> np.ndarray:
        """
        Return one readout for a probe standing on a given composition.

        The composition is the part of the probe state this detector is
        sensitive to; the deflection beside it belongs to the camera
        watching the transmitted disc.

        Parameters
        ----------
        state : _ProbeState
            The local specimen state at this beam position.

        Returns
        -------
        numpy.ndarray
            1D counts under a projected readout; the 2D dispersed image
            otherwise.
        """
        if self._parameters.readout == PROJECTED_READOUT:
            return self._counts(state.silicon_fraction)
        return self._dispersed_image(state.silicon_fraction)

    @property
    def _rows(self) -> int:
        """
        Return how many rows the unprojected readout has.

        Returns
        -------
        int
            The non-dispersive direction's length, at least one.
        """
        down, _ = self._parameters.binning_yx
        return max(1, _EELS_ROWS // down)

    def _dispersed_image(self, silicon_fraction: float) -> np.ndarray:
        """
        Return the 2D frame the sensor sees before anything projects it.

        The spectrum spread over the non-dispersive direction as a
        streak a few rows tall, which is what a spectrometer image
        actually looks like. The spread is a *split* of the same expected
        counts rather than a copy of them, so summing the rows recovers
        the projected spectrum's statistics exactly — the two readout
        modes then differ in what they discard, not in how much signal
        there was.

        Parameters
        ----------
        silicon_fraction : float
            The composition under the probe.

        Returns
        -------
        numpy.ndarray
            Counts, ``(rows, channels)``.
        """
        rows = self._rows
        expected = self._expected_counts(silicon_fraction)
        if rows == 1:
            return self._rng.poisson(expected).astype(np.float32)
        centre = (rows - 1) / 2.0
        sigma = max(_EELS_STREAK_ROWS / self._parameters.binning, 1.0)
        offsets = np.arange(rows, dtype=np.float64) - centre
        profile = np.exp(-0.5 * (offsets / sigma) ** 2)
        profile /= profile.sum()
        return self._rng.poisson(np.outer(profile, expected)).astype(np.float32)

    def _counts(self, silicon_fraction: float) -> np.ndarray:
        """
        Return one projected spectrum, with counting noise.

        Parameters
        ----------
        silicon_fraction : float
            The composition under the probe.

        Returns
        -------
        numpy.ndarray
            Counts per channel.
        """
        expected = self._expected_counts(silicon_fraction)
        return self._rng.poisson(expected).astype(np.float32)

    def _expected_counts(self, silicon_fraction: float) -> np.ndarray:
        """
        Return the noiseless spectrum: the model, before counting statistics.

        Separate from :meth:`_counts` because it is the thing a test can
        reason about — the noise is what makes two acquisitions of the
        same specimen differ, and asserting on a model plus noise means
        asserting on the noise.

        Parameters
        ----------
        silicon_fraction : float
            How much silicon rather than carbon film the probe stands on.

        Returns
        -------
        numpy.ndarray
            Expected counts per channel, non-negative.
        """
        energy_ev = self._energy_axis()
        exposure_ms = self._parameters.exposure_ms
        # A binned channel spans proportionally more of the energy axis,
        # so it collects proportionally more of everything in it.
        gain = exposure_ms * self._parameters.binning
        if self._instrument.is_beam_blanked():
            # No beam, no losses: the dark level and nothing else. The
            # same collapse the scan and the Ronchigram show, so the
            # blanker reads as one instrument state rather than three
            # unrelated behaviours.
            return np.full_like(energy_ev, _EELS_DARK_COUNTS_PER_MS * gain)

        silicon = min(max(silicon_fraction, 0.0), 1.0)
        zlp_scale = _EELS_ZLP_COUNTS_PER_MS * gain
        counts = zlp_scale * _gaussian(
            energy_ev, 0.0, _EELS_RESOLUTION_EV,
        )
        counts += (
            zlp_scale
            * _PLASMON_RATIO
            * (
                silicon * _gaussian(energy_ev, _SILICON_PLASMON_EV, _PLASMON_FWHM_EV)
                + (1.0 - silicon)
                * _gaussian(energy_ev, _CARBON_PLASMON_EV, _PLASMON_FWHM_EV)
            )
        )
        above = energy_ev > _BACKGROUND_REFERENCE_EV
        counts[above] += (
            _BACKGROUND_COUNTS_PER_MS
            * gain
            * (energy_ev[above] / _BACKGROUND_REFERENCE_EV) ** -_BACKGROUND_DECAY
        )
        counts += _edge(
            energy_ev,
            _SILICON_L_EDGE_EV,
            _SILICON_EDGE_COUNTS_PER_MS * gain * silicon,
        )
        counts += _edge(
            energy_ev,
            _CARBON_K_EDGE_EV,
            _CARBON_EDGE_COUNTS_PER_MS * gain * (1.0 - silicon),
        )
        return counts + _EELS_DARK_COUNTS_PER_MS * gain


def _silicon_fraction(lattice_value: float) -> float:
    """
    Return how much silicon the probe stands on at one specimen value.

    The single place the preview's *image* model and its *spectrum*
    model are tied together, and the tie is what gives a spectrum image
    from this instrument a checkable answer: the same normalised
    specimen value that :meth:`PreviewScanner._read_out` turns into
    detector intensity is what becomes composition here. So a silicon map
    integrated out of a spectrum image rises and falls with the HAADF
    channel of the same pass — monotonically, not identically, since the
    high-angle channel squares its signal and this one does not.

    Parameters
    ----------
    lattice_value : float
        The sampled specimen modulation at one beam position, in
        ``[-1, 1]``.

    Returns
    -------
    float
        The silicon fraction, in ``[0, 1]``; the rest is carbon film.
    """
    return (lattice_value + 1.0) / 2.0


def _gaussian(energy_ev: np.ndarray, centre_ev: float, fwhm_ev: float) -> np.ndarray:
    """
    Return a unit-height Gaussian sampled on an energy axis.

    Parameters
    ----------
    energy_ev : np.ndarray
        Where to sample, in electronvolts.
    centre_ev : float
        The peak's position.
    fwhm_ev : float
        Its full width at half maximum, which is how every spectroscopy
        instrument quotes a width — converted once, here, rather than
        leaving a factor of 2.35 loose in the caller.

    Returns
    -------
    numpy.ndarray
        The sampled peak, 1 at the centre.
    """
    sigma = fwhm_ev / _FWHM_PER_SIGMA
    return np.exp(-0.5 * ((energy_ev - centre_ev) / sigma) ** 2)


def _edge(energy_ev: np.ndarray, onset_ev: float, height: float) -> np.ndarray:
    """
    Return one core-loss edge: a sharp onset decaying as a power law.

    The shape a background-subtracted ionisation edge has, and the reason
    an EELS elemental map is made by integrating a window *after* the
    onset rather than by fitting a peak: there is no peak, only a step
    that decays.

    Parameters
    ----------
    energy_ev : np.ndarray
        Where to sample, in electronvolts.
    onset_ev : float
        The ionisation threshold.
    height : float
        Counts in the first channel above the onset.

    Returns
    -------
    numpy.ndarray
        The edge, zero everywhere below the onset.
    """
    shape = np.zeros_like(energy_ev)
    above = energy_ev >= onset_ev
    width = _EDGE_ONSET_WIDTH_EV
    shape[above] = ((energy_ev[above] - onset_ev + width) / width) ** -_EDGE_DECAY
    return height * shape


@dataclasses.dataclass(frozen=True)
class PreviewDevices:
    """
    The devices of one preview instrument.

    Deliberately the same shape as
    :class:`~miainwoodpecker.devices.remote.RemoteInstrumentDevices`
    where it overlaps, so code that opens a window against one opens a
    window against the other unchanged.

    Attributes
    ----------
    scanner : PreviewScanner | None
        The scan unit, or None for a detector-only instrument.
    cameras : Mapping[str, _PreviewCameraBase]
        Every camera served, by target name — a Ronchigram camera or an
        EEL spectrometer, decided by the name (see
        :func:`_build_camera`). Empty for a scan-only instrument.
    instrument : PreviewInstrument
        The instrument controls, shared by every device above.
    stage_size_nm : float
        The stage extent, for choosing a field of view.
    """

    scanner: PreviewScanner | None
    cameras: Mapping[str, _PreviewCameraBase]
    instrument: PreviewInstrument
    stage_size_nm: float


def build_preview_devices(
    *,
    scan: bool = True,
    camera: bool = True,
    camera_count: int = 1,
    controls: Iterable[str] | None = None,
) -> PreviewDevices:
    """
    Build a preview instrument in whatever shape the window needs.

    Parameters
    ----------
    scan : bool
        Whether to serve a scan unit.
    camera : bool
        Whether to serve any cameras.
    camera_count : int
        How many cameras to serve when ``camera`` is true. More than one
        opens the multi-camera layout, which otherwise needs two
        detectors on a bench.
    controls : Iterable[str] | None
        Which controls the instrument publishes, or None for all.

    Returns
    -------
    PreviewDevices
        The assembled devices, sharing one instrument.

    Raises
    ------
    ValueError
        If neither a scan unit nor a camera is asked for — the widget
        refuses to build a window with nothing to display, so this
        refuses first and says why — or if ``camera_count`` is outside
        the range of names there are to serve them under.
    """
    if not scan and not camera:
        msg = (
            "a preview instrument needs a scan unit or a camera; "
            "one with neither has nothing to display"
        )
        raise ValueError(msg)
    served_cameras = camera_count if camera else 0
    if camera and not 1 <= served_cameras <= _MAX_PREVIEW_CAMERAS:
        msg = (
            f"camera_count must be between 1 and {_MAX_PREVIEW_CAMERAS}, "
            f"got {camera_count}"
        )
        raise ValueError(msg)

    names = _camera_target_names(served_cameras)
    targets = ([SCANNER_TARGET] if scan else []) + list(names)
    instrument = PreviewInstrument(controls=controls, targets=targets)
    cameras = {
        name: _build_camera(name, instrument, _CAMERA_SEED + offset)
        for offset, name in enumerate(names)
    }
    return PreviewDevices(
        # The cameras go to the scanner as well as into the mapping: a
        # synchronised pass is the scan unit reading them out per beam
        # position, so it has to hold them. Built after them for that
        # reason, rather than beside them.
        scanner=PreviewScanner(instrument=instrument, cameras=cameras)
        if scan
        else None,
        cameras=cameras,
        instrument=instrument,
        stage_size_nm=instrument.stage_size_nm(),
    )


def _build_camera(
    name: str,
    instrument: PreviewInstrument,
    seed: int,
) -> _PreviewCameraBase:
    """
    Build the kind of camera a target name promises.

    **The name decides the detector**, which is a correction as much as a
    feature: the served names come from
    :data:`~miainwoodpecker.devices.rpc.CAMERA_TARGET_NAMES`, which is
    Nion's own device list showing through, and until the spectrometer
    existed a preview asked for two cameras served a *Ronchigram* on the
    ``eels_camera`` target. That is the shape of quiet lie this module
    exists to avoid — a window that looks right against a device that is
    not what it says it is.

    Parameters
    ----------
    name : str
        The target name this camera is served under.
    instrument : PreviewInstrument
        The instrument whose controls shape its frames.
    seed : int
        Seed for its noise.

    Returns
    -------
    _PreviewCameraBase
        A spectrometer for the EELS target, a Ronchigram camera
        otherwise.
    """
    if name == _EELS_TARGET:
        return PreviewEELSCamera(instrument=instrument, camera_id=name, seed=seed)
    return PreviewCamera(instrument=instrument, camera_id=name, seed=seed)


def _camera_target_names(count: int) -> tuple[str, ...]:
    """
    Return the target names to serve ``count`` cameras under.

    A single camera is served as the neutral ``camera`` target, which is
    what a commodity detector server uses; more than one takes the named
    targets instead, since that is the case those names exist for.

    Parameters
    ----------
    count : int
        How many cameras are served.

    Returns
    -------
    tuple[str, ...]
        One target name per camera.
    """
    if count == 0:
        return ()
    if count == 1:
        return ("camera",)
    return tuple(CAMERA_TARGET_NAMES[:count])


def parse_preview_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse the preview's command-line arguments.

    Parameters
    ----------
    argv : list[str] | None
        Argument list, or None to read ``sys.argv``.

    Returns
    -------
    argparse.Namespace
        The parsed arguments, with ``controls`` already validated.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Open the live viewer against an in-process synthetic "
            "instrument. For UI development: the data is invented."
        ),
    )
    parser.add_argument(
        "--scan",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="serve a scan unit (--no-scan opens the detector-only window)",
    )
    parser.add_argument(
        "--camera",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="serve cameras (--no-camera opens the scan-only window)",
    )
    parser.add_argument(
        "--cameras",
        type=int,
        default=1,
        metavar="N",
        help=(
            f"how many cameras to serve, 1 to {_MAX_PREVIEW_CAMERAS}. Two or "
            f"more includes an EEL spectrometer on the {_EELS_TARGET!r} "
            f"target, which is what a spectrum image is acquired from"
        ),
    )
    parser.add_argument(
        "--controls",
        default=None,
        metavar="NAMES",
        help=(
            "comma-separated controls the instrument publishes "
            f"(default: all of {','.join(PREVIEW_CONTROLS)})"
        ),
    )
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "session directory for recordings. Recordings made here hold "
            "invented data; point this at a scratch directory."
        ),
    )
    args = parser.parse_args(argv)
    if args.controls is not None:
        names = [name.strip() for name in args.controls.split(",") if name.strip()]
        unknown = [name for name in names if name not in PREVIEW_CONTROLS]
        if unknown:
            parser.error(
                f"unknown control(s) {', '.join(unknown)}; "
                f"choose from {', '.join(PREVIEW_CONTROLS)}",
            )
        args.controls = names
    return args


def main(argv: list[str] | None = None) -> int:
    """
    Open the live viewer against the preview instrument.

    Parameters
    ----------
    argv : list[str] | None
        Argument list, or None to read ``sys.argv``.

    Returns
    -------
    int
        Process exit status.
    """
    import napari  # noqa: PLC0415 - the CLI needs the viewer extra; the devices above do not

    from miainwoodpecker.storage.session import Session  # noqa: PLC0415
    from miainwoodpecker.viewer import documents  # noqa: PLC0415
    from miainwoodpecker.viewer.live import LiveInstrumentWidget  # noqa: PLC0415

    args = parse_preview_args(argv)
    devices = build_preview_devices(
        scan=args.scan,
        camera=args.camera,
        camera_count=args.cameras,
        controls=args.controls,
    )
    window = documents.open_window(f"miainwoodpecker ({PREVIEW_BACKEND})")
    widget = LiveInstrumentWidget(
        window.board,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    if args.session is not None:
        widget.set_session(Session(args.session))
    window.set_panel(widget)
    window.show()
    # No explicit widget.shutdown() after this, matching
    # miainwoodpecker.viewer.app: DocumentWindow.closeEvent calls it as
    # the window closes, and calling it again here reaches a widget whose
    # C++ side has already been destroyed. force=True for the same reason
    # as there - no viewer exists until a document opens.
    napari.run(force=True)
    return 0
