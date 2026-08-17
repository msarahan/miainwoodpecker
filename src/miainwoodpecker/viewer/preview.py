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
and moving the stage moves the field of view. A preview whose dials did
nothing would let a broken Instrument panel — a signal never connected, a
setter called on the wrong object — look exactly like a working one, and
that is the panel this module exists to iterate on.
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
    PROJECTED_READOUT,
    STAGE_POSITION_CONTROL,
    CameraParameters,
    Frame,
    ScanParameters,
)
from miainwoodpecker.devices.rpc import CAMERA_TARGET_NAMES, SCANNER_TARGET

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
_RNG_SEED = 20260816
_CAMERA_SEED = _RNG_SEED + 1


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
    controls : typing.Iterable[str] | None
        Which of :data:`PREVIEW_CONTROLS` to publish from
        ``available_controls``, or None for all of them. Publishing a
        subset is how the panel's "an unpublished control gets no row"
        rule is exercised without owning a microscope that lacks a
        blanker.
    targets : typing.Iterable[str]
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
    """

    def __init__(self, instrument: PreviewInstrument | None = None) -> None:
        super().__init__(instrument, _RNG_SEED)
        self._pass_index = 0

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
        channels : typing.Sequence[int]
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
        lattice : numpy.ndarray
            The pass's sampled specimen modulation.
        parameters : ScanParameters
            The pass's scan geometry.
        channel : int
            Which channel to read out.
        pass_id : str
            Identifier shared by every frame from this pass.
        simultaneous : typing.Sequence[int]
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

    def close(self) -> None:
        """Release nothing; this scanner owns no resources."""


class PreviewCamera(_SyntheticSource):
    """
    A camera producing a synthetic Ronchigram.

    Implements :class:`~miainwoodpecker.devices.interface.Camera`,
    including its start/stop contract: acquiring before ``start`` is an
    error rather than a frame, because a viewer that never calls
    ``start`` would otherwise appear to work here and stall against every
    real detector.

    Parameters
    ----------
    instrument : PreviewInstrument | None
        The instrument supplying defocus and blanker state, or None to
        own a private one.
    camera_id : str
        Stable identifier, so two preview cameras are distinguishable.
    seed : int | None
        Seed for this camera's noise, or None for the default. Served
        cameras are given different ones by
        :func:`build_preview_devices`, so no two produce bit-identical
        frames: a wiring bug that pointed two camera sections at one
        camera would otherwise look exactly like two working ones.
    """

    def __init__(
        self,
        instrument: PreviewInstrument | None = None,
        camera_id: str = "preview_camera",
        seed: int | None = None,
    ) -> None:
        super().__init__(instrument, _CAMERA_SEED if seed is None else seed)
        self._camera_id = camera_id
        self._started = False
        self._parameters = CameraParameters(exposure_ms=_DEFAULT_EXPOSURE_MS)

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
            The settings in force, which this camera takes unchanged.

        Raises
        ------
        ValueError
            If the requested binning is not one this camera offers.
            Refused rather than rounded, so the viewer's own handling of a
            rejected setting is reachable.
        """
        if parameters.binning not in _BINNING_VALUES:
            msg = (
                f"binning {parameters.binning} is not supported; "
                f"{self._camera_id} offers {list(_BINNING_VALUES)}"
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
            A Ronchigram-like image, or a 1D spectrum under
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
        pixels = _CAMERA_PIXELS // self._parameters.binning
        data = self._ronchigram(pixels)
        if self._parameters.readout == PROJECTED_READOUT:
            data = data.sum(axis=0)
        return Frame(
            data=np.asarray(data, dtype=np.float32),
            timestamp=_now(),
            metadata={
                "device_id": self._camera_id,
                "frame_index": self._next_frame_index(),
                "exposure_ms": self._parameters.exposure_ms,
                "binning": self._parameters.binning,
                "readout": self._parameters.readout,
                "defocus_nm": self._instrument.defocus_nm(),
            },
        )

    def _ronchigram(self, pixels: int) -> np.ndarray:
        """
        Return a synthetic Ronchigram of the given size.

        A bright central disc crossed by rings whose visibility falls
        with defocus — the shape an operator aligns against, which is
        what makes the Defocus dial's effect legible on this detector.

        Parameters
        ----------
        pixels : int
            Frame side length.

        Returns
        -------
        numpy.ndarray
            The image, float32.
        """
        shape = (pixels, pixels)
        if self._instrument.is_beam_blanked():
            return self._noise(shape)
        axis = np.linspace(-1.0, 1.0, pixels, dtype=np.float32)
        radius = np.hypot(*np.meshgrid(axis, axis, indexing="ij"))
        disc = (radius < _RONCHIGRAM_RADIUS).astype(np.float32)
        rings = np.cos(radius * _RONCHIGRAM_RINGS * np.pi)
        envelope = self._instrument.contrast_envelope()
        return (
            _SPECIMEN_BACKGROUND * disc
            + _SPECIMEN_CONTRAST * envelope * disc * rings
            + self._noise(shape)
        )

    def close(self) -> None:
        """Release nothing; this camera owns no resources."""


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
    cameras : typing.Mapping[str, PreviewCamera]
        Every camera served, by target name. Empty for a scan-only
        instrument.
    instrument : PreviewInstrument
        The instrument controls, shared by every device above.
    stage_size_nm : float
        The stage extent, for choosing a field of view.
    """

    scanner: PreviewScanner | None
    cameras: Mapping[str, PreviewCamera]
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
    controls : typing.Iterable[str] | None
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
    return PreviewDevices(
        scanner=PreviewScanner(instrument=instrument) if scan else None,
        cameras={
            name: PreviewCamera(
                instrument=instrument,
                camera_id=name,
                seed=_CAMERA_SEED + offset,
            )
            for offset, name in enumerate(names)
        },
        instrument=instrument,
        stage_size_nm=instrument.stage_size_nm(),
    )


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
        help=f"how many cameras to serve, 1 to {_MAX_PREVIEW_CAMERAS}",
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
    from miainwoodpecker.viewer.live import LiveInstrumentWidget  # noqa: PLC0415

    args = parse_preview_args(argv)
    devices = build_preview_devices(
        scan=args.scan,
        camera=args.camera,
        camera_count=args.cameras,
        controls=args.controls,
    )
    viewer = napari.Viewer(title=f"miainwoodpecker ({PREVIEW_BACKEND})")
    widget = LiveInstrumentWidget(
        viewer,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    if args.session is not None:
        widget.set_session(Session(args.session))
    viewer.window.add_dock_widget(widget, area="right", name="Instrument")
    # No explicit widget.shutdown() after this, matching
    # miainwoodpecker.viewer.app: closeEvent already calls it as part of
    # Qt's app-quit teardown, and calling it again here reaches a widget
    # whose C++ side has already been destroyed.
    napari.run()
    return 0
