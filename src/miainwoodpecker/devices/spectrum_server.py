"""
A device server for spectrum detectors: simulated EDX, and replayed recordings.

The third in-tree adapter, after :mod:`miainwoodpecker.devices.nion_server`
(a STEM column) and :mod:`miainwoodpecker.devices.camera_server` (commodity
cameras). This one serves
:class:`~miainwoodpecker.devices.interface.SpectrumDetector` — a detector
whose output is a histogram of photon energies rather than a picture.

Run it the way any adapter is run::

    remote_instrument(server_module="miainwoodpecker.devices.spectrum_server")

MIT throughout, with no vendor SDK — and unlike the camera server, that
is not a happy accident but the whole reason this exists as a *simulated*
adapter. See "Why there is no vendor backend here" below.

Two backends, for the reason ``nion_server`` and ``camera_server`` have two
---------------------------------------------------------------------------
``simulated`` synthesises a physically-shaped EDX spectrum and needs
**nothing installed** beyond this package's base dependencies — no vendor
software, no detector, no analysis stack. It exists so the whole path,
from RPC through storage to a HyperSpy EDS signal, is exercisable in CI
and on a laptop.

``replay`` opens what ``--plugin`` names: a spectrum recording this
project wrote. That is the same "a media file is a first-class device"
idea the camera server uses, and it is worth more here than there,
because it is how a real EDX session recorded at the instrument becomes a
regression fixture anyone can run without the instrument. Every spectrum
it hands back carries ``backend: "replay"`` and the file it came from.

``hardware`` is accepted by the parser and then **refused with a
sentence**, rather than being an alias for ``replay``. The name carries a
promise: ``viewer/app.py`` says the two failures its backend selector
exists to prevent are driving a microscope you meant to simulate, and
believing you are on hardware when you are not. A ``hardware`` backend
that opens a file is the second one, and honest frame metadata does not
undo it — by the time anyone reads the metadata, the session has already
happened.

Why there is no vendor backend here
-----------------------------------
The two detectors this was designed against are a **Bruker XFlash 6T-100**
driven by ESPRIT and an **Oxford Instruments Ultim Extreme** driven by
AZtec. Neither vendor's control library can be redistributed in this
repository, and neither ships on PyPI, so a live adapter for either is
necessarily out-of-tree — which
:data:`~miainwoodpecker.devices.remote.DEFAULT_SERVER_MODULE` already
supports and ``tests/unit/test_out_of_tree_server.py`` already
demonstrates. What this module provides is the space such an adapter
slots into, plus a simulator that keeps the space honest by actually
filling it.

For *offline* data, both vendors are already covered without any of this:
RosettaSciIO reads Bruker's ``bcf``/``spx`` and the vendor-neutral
EMSA ``msa`` that both export, straight into a HyperSpy EDS signal.
docs/adapters/spectrum-detectors.md has the details and the honest
recommendation about which problem is worth solving first.

What the simulator gets right, and what it does not
---------------------------------------------------
Right, because the tests and the analysis path would be vacuous
otherwise: a bremsstrahlung continuum that falls to zero at the
Duane-Hunt limit (the beam energy — so changing the accelerating voltage
visibly changes the spectrum), Gaussian characteristic peaks at real
X-ray line energies whose widths follow the standard Fano/noise
resolution model anchored on the detector's Mn Ka figure, Poisson
counting noise, and a live/real time pair with a plausible dead-time
fraction. A spectrum image additionally varies composition with position,
so a map is not the same spectrum repeated and an element map computed
from it has structure to find.

Not right, and deliberately not modelled: absorption and fluorescence
within the specimen, detector escape peaks, sum peaks, the low-energy
window transmission, and any relationship between beam current and count
rate. Those are what a real quantification corrects for, and a simulator
that faked them would invite trusting numbers from it.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import math
import os
import sys
import threading
import typing

import numpy as np

from miainwoodpecker.devices.interface import (
    HIGH_TENSION_V_KEY,
    MAP_MODE,
    SPOT_MODE,
    Spectrum,
    SpectrumParameters,
)
from miainwoodpecker.devices.rpc import (
    HARDWARE_BACKEND,
    REPLAY_BACKEND,
    SIMULATED_BACKEND,
)
from miainwoodpecker.devices.serving import accept_loop
from miainwoodpecker.devices.shared_frame import SharedFrameWriter

SPECTRUM_BACKENDS = (SIMULATED_BACKEND, REPLAY_BACKEND, HARDWARE_BACKEND)
"""
What this server accepts on its command line.

``hardware`` is listed so that asking for it gets a sentence rather
than an argparse error, and then refused: there is no in-tree vendor
backend, and naming a file playback ``hardware`` would be the exact
confusion ``viewer/app.py``'s selector exists to prevent.
"""

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy.typing as npt

    from miainwoodpecker.devices.interface import ScanParameters

_LOGGER = logging.getLogger("miainwoodpecker.devices.spectrum_server")

# "The requested detector could not be opened", distinct from a crash so a
# launcher can tell "nothing to open" from "the adapter is broken". Mirrors
# camera_server's NO_CAMERA and nion_server's NO_HARDWARE.
NO_DETECTOR_EXIT_STATUS = 2
# Shared with the client, which retries these with fresh ports.
PORT_UNAVAILABLE_EXIT_STATUS = 4

SPECTRUM_TARGET = "spectrum_detector"
"""The neutral target this server serves its detector on."""

_DEFAULT_LIVE_TIME_S = 1.0
_DEFAULT_CHANNELS = 4096
_DEFAULT_ENERGY_SCALE_EV = 10.0
_DEFAULT_ENERGY_OFFSET_EV = -480.0
_DEFAULT_HIGH_TENSION_V = 100_000.0

# The XFlash 6T-100's published class of geometry and resolution, used as
# the simulator's defaults so a spectrum from it is the shape a real one
# from the target instrument would be. Not a claim to reproduce that
# detector - see the module docstring for what is and is not modelled.
_DEFAULT_ENERGY_RESOLUTION_EV = 129.0
_DEFAULT_AZIMUTH_DEG = 45.0
_DEFAULT_ELEVATION_DEG = 18.0
_DEFAULT_SOLID_ANGLE_SR = 0.7

# Fano factor and the mean electron-hole pair creation energy in silicon,
# which together with the electronic noise term give a detector's energy
# resolution as a function of energy. Standard values; this is the model
# every EDS package uses to widen a peak away from the quoted Mn Ka figure.
_FANO_FACTOR = 0.12
_PAIR_ENERGY_EV = 3.64
_MN_KA_EV = 5898.75
# eV of FWHM per eV of sigma, i.e. 2*sqrt(2*ln 2).
_FWHM_PER_SIGMA = 2.354_820_045

# A handful of real K-line energies, in eV, with rough relative
# intensities. Enough elements that a simulated map has something to
# separate and a quantification has something to fit; not a line database,
# which is exspy's job (exspy._misc.elements) and not this simulator's.
_LINES: dict[str, tuple[tuple[float, float], ...]] = {
    "C": (((277.0), 1.0),),
    "O": (((524.9), 1.0),),
    "Al": ((1486.7, 1.0), (1557.0, 0.05)),
    "Si": ((1739.98, 1.0), (1835.9, 0.05)),
    "Ti": ((4510.84, 1.0), (4931.81, 0.13)),
    "Fe": ((6403.84, 1.0), (7057.98, 0.13)),
    "Cu": ((8047.78, 1.0), (8905.29, 0.13)),
}

_DEFAULT_COMPOSITION = {"O": 0.45, "Si": 0.30, "Al": 0.15, "Fe": 0.10}
# The second phase a simulated map contains, so the two halves of a
# spectrum image differ in a way an element map can actually show.
_INCLUSION_COMPOSITION = {"O": 0.20, "Ti": 0.45, "Fe": 0.35}

_TOTAL_COUNTS_PER_LIVE_SECOND = 120_000.0
_CONTINUUM_FRACTION = 0.35
_DEAD_TIME_FRACTION = 0.20


class SpectrumDetectorOpenError(RuntimeError):
    """Raised when the requested spectrum detector cannot be opened."""


def _now() -> datetime.datetime:
    """Return the current time, timezone-aware, as every Spectrum requires."""
    return datetime.datetime.now(tz=datetime.UTC)


def _resolution_ev(energy_ev: float, mn_ka_fwhm_ev: float) -> float:
    """
    Return a detector's FWHM at one energy, from its Mn Ka figure.

    The standard silicon-detector resolution model: the measured width is
    the quadrature sum of a fixed electronic-noise term and a
    statistical term that grows with the number of electron-hole pairs
    the photon creates. Anchoring it on the quoted Mn Ka resolution is
    how every EDS package turns the one number a vendor publishes into a
    width at every other line, and it is why a simulated carbon peak is
    correctly much narrower than a copper one.

    Parameters
    ----------
    energy_ev : float
        The line energy, in electronvolts.
    mn_ka_fwhm_ev : float
        The detector's resolution at Mn Ka (5898.75 eV), in electronvolts.

    Returns
    -------
    float
        The FWHM at ``energy_ev``, in electronvolts.
    """
    statistical_at_mn = _FANO_FACTOR * _PAIR_ENERGY_EV * _MN_KA_EV
    noise_squared = max((mn_ka_fwhm_ev / _FWHM_PER_SIGMA) ** 2 - statistical_at_mn, 0.0)
    variance = noise_squared + _FANO_FACTOR * _PAIR_ENERGY_EV * max(energy_ev, 0.0)
    return _FWHM_PER_SIGMA * math.sqrt(variance)


def _synthesise(  # noqa: PLR0913 - one physical model, not a call signature
    energies_ev: npt.NDArray[np.float64],
    composition: typing.Mapping[str, float],
    *,
    beam_energy_ev: float,
    live_time_s: float,
    resolution_ev: float,
    generator: np.random.Generator,
) -> npt.NDArray[np.uint32]:
    """
    Build one Poisson-noisy EDX spectrum for a composition.

    Parameters
    ----------
    energies_ev : npt.NDArray[np.float64]
        The energy of each channel, in electronvolts.
    composition : typing.Mapping[str, float]
        Element symbol to fraction of the characteristic intensity. Not a
        weight fraction: turning one into the other needs the absorption
        and fluorescence corrections this simulator deliberately omits,
        so calling it a composition would invite quantifying against it.
    beam_energy_ev : float
        The accelerating voltage in electronvolts, which is the
        Duane-Hunt limit — no X-ray is emitted above it, and no line
        above it is excited.
    live_time_s : float
        Counting time, which scales the total counts and therefore the
        Poisson noise.
    resolution_ev : float
        Detector FWHM at Mn Ka, in electronvolts.
    generator : np.random.Generator
        Source of the counting noise.

    Returns
    -------
    npt.NDArray[np.uint32]
        Counts per channel.
    """
    total = _TOTAL_COUNTS_PER_LIVE_SECOND * live_time_s
    # Kramers' law: the bremsstrahlung intensity falls linearly to zero at
    # the beam energy, which is what makes the continuum end where it
    # physically must instead of running off the end of the axis.
    continuum = np.clip(beam_energy_ev - energies_ev, 0.0, None) / max(
        beam_energy_ev, 1.0
    )
    continuum = np.where(energies_ev > 0.0, continuum, 0.0)
    if continuum.sum() > 0:
        continuum = continuum / continuum.sum() * total * _CONTINUUM_FRACTION

    peaks = np.zeros_like(energies_ev)
    weight_total = sum(composition.values()) or 1.0
    for element, weight in composition.items():
        for line_energy, relative in _LINES.get(element, ()):
            if line_energy >= beam_energy_ev:
                continue  # not excited at this accelerating voltage
            sigma = _resolution_ev(line_energy, resolution_ev) / _FWHM_PER_SIGMA
            amplitude = (
                total * (1.0 - _CONTINUUM_FRACTION) * (weight / weight_total) * relative
            )
            peaks += (
                amplitude
                * np.exp(
                    -0.5 * ((energies_ev - line_energy) / sigma) ** 2,
                )
                / (sigma * math.sqrt(2.0 * math.pi))
            )

    expected = np.clip(continuum + peaks, 0.0, None)
    return generator.poisson(expected).astype(np.uint32)


class SimulatedSpectrumDetector:
    """
    An EDX detector that synthesises spectra, so the path runs with no hardware.

    Deliberately not a stored spectrum played back: successive spot
    spectra differ by their counting noise, and a map's spectra differ by
    position, so the contracts that matter here — that a map is not one
    spectrum repeated, that live time changes the counts, that the beam
    energy changes where the continuum ends — are testable against it
    rather than vacuously true.
    """

    def __init__(self, *, high_tension_v: float = _DEFAULT_HIGH_TENSION_V) -> None:
        self._parameters = SpectrumParameters(
            live_time_s=_DEFAULT_LIVE_TIME_S,
            channel_count=_DEFAULT_CHANNELS,
            energy_scale_ev=_DEFAULT_ENERGY_SCALE_EV,
            energy_offset_ev=_DEFAULT_ENERGY_OFFSET_EV,
        )
        self._high_tension_v = high_tension_v
        self._spectrum_index = 0
        self._running = False
        self._closed = False
        self._generator = np.random.default_rng()

    @property
    def detector_id(self) -> str:
        """Return this detector's stable identifier."""
        return "simulated_sdd"

    @property
    def acquisition_modes(self) -> Sequence[str]:
        """Return both modes: this simulator has a synchronised scan by fiat."""
        return [SPOT_MODE, MAP_MODE]

    @property
    def is_closed(self) -> bool:
        """Return whether this device has been released."""
        return self._closed

    def parameters(self) -> SpectrumParameters:
        """Return the settings the next spectrum will use."""
        return self._parameters

    def configure(self, parameters: SpectrumParameters) -> SpectrumParameters:
        """
        Accept new settings, rounding the channel count as an analyser would.

        The rounding is not decoration. Real pulse processors offer a
        small set of channel counts, always powers of two, and a caller
        that assumed its request took would build an energy axis of the
        wrong length. Reporting what was taken is the existing contract
        and this is the cheapest honest way to exercise it.

        Parameters
        ----------
        parameters : SpectrumParameters
            The requested live time, channel count, and energy calibration.

        Returns
        -------
        SpectrumParameters
            What this detector took: the request with its channel count
            rounded down to a power of two, floored at 256.
        """
        channels = max(256, 2 ** int(math.log2(max(parameters.channel_count, 1))))
        self._parameters = SpectrumParameters(
            live_time_s=parameters.live_time_s,
            channel_count=channels,
            energy_scale_ev=parameters.energy_scale_ev,
            energy_offset_ev=parameters.energy_offset_ev,
        )
        return self._parameters

    def start(self) -> None:
        """Begin acquisition."""
        self._running = True

    def stop(self) -> None:
        """Pause acquisition."""
        self._running = False

    def acquire_spectrum(self) -> Spectrum:
        """
        Return one synthesised spot spectrum.

        Returns
        -------
        Spectrum
            Counts per channel for the configured live time, with the
            full metadata vocabulary attached.
        """
        parameters = self._parameters
        data = _synthesise(
            self._energies_ev(),
            _DEFAULT_COMPOSITION,
            beam_energy_ev=self._high_tension_v,
            live_time_s=parameters.live_time_s,
            resolution_ev=_DEFAULT_ENERGY_RESOLUTION_EV,
            generator=self._generator,
        )
        metadata = self._metadata(live_time_s=parameters.live_time_s)
        metadata["acquisition_mode"] = SPOT_MODE
        return self._spectrum(data, metadata)

    def acquire_map(self, parameters: ScanParameters) -> Spectrum:
        """
        Return a synthesised spectrum image over a scanned region.

        The two halves of the scanned field have different compositions,
        so an element map computed from the result has structure rather
        than uniform noise — which is what makes the storage and analysis
        round trip a real test instead of a shape check.

        Parameters
        ----------
        parameters : ScanParameters
            The scan geometry to map over. Its ``pixel_time_us`` becomes
            the per-pixel live time, since on a real map the dwell is the
            scan's and not the detector's.

        Returns
        -------
        Spectrum
            Shape ``(height, width, channels)``, with the scan geometry
            in its metadata so storage calibrates the navigation axes
            through the same path a scanned frame uses.
        """
        settings = self._parameters
        energies = self._energies_ev()
        height, width = parameters.shape
        pixel_live_time_s = parameters.pixel_time_us * 1e-6
        data = np.empty((height, width, settings.channel_count), dtype=np.uint32)
        for row in range(height):
            for column in range(width):
                composition = (
                    _INCLUSION_COMPOSITION
                    if column >= width // 2
                    else _DEFAULT_COMPOSITION
                )
                data[row, column] = _synthesise(
                    energies,
                    composition,
                    beam_energy_ev=self._high_tension_v,
                    live_time_s=pixel_live_time_s,
                    resolution_ev=_DEFAULT_ENERGY_RESOLUTION_EV,
                    generator=self._generator,
                )
        metadata = self._metadata(
            live_time_s=pixel_live_time_s * height * width,
        )
        metadata["acquisition_mode"] = MAP_MODE
        metadata["fov_size_nm"] = parameters.fov_size_nm
        metadata["pixel_time_us"] = parameters.pixel_time_us
        # A simulator has no synchronisation hardware to describe, and
        # saying "detector" would be exactly the fiction the metadata key
        # exists to prevent: nothing here drives a scan coil.
        metadata["scan_sync"] = "simulated"
        return self._spectrum(data, metadata)

    def close(self) -> None:
        """Release nothing; idempotent, like every adapter's close."""
        self._closed = True

    def _energies_ev(self) -> npt.NDArray[np.float64]:
        """Return the energy of each channel, in electronvolts."""
        settings = self._parameters
        return settings.energy_offset_ev + settings.energy_scale_ev * np.arange(
            settings.channel_count,
            dtype=np.float64,
        )

    def _spectrum(
        self,
        data: npt.NDArray[typing.Any],
        metadata: dict[str, object],
    ) -> Spectrum:
        """
        Wrap synthesised counts as a Spectrum and advance the index.

        Parameters
        ----------
        data : npt.NDArray[typing.Any]
            Counts, energy on the last axis.
        metadata : dict[str, object]
            The metadata to attach, already mode-specific.

        Returns
        -------
        Spectrum
            The spectrum, carrying the calibration it was built with.
        """
        settings = self._parameters
        metadata["spectrum_index"] = self._spectrum_index
        self._spectrum_index += 1
        return Spectrum(
            data=data,
            timestamp=_now(),
            energy_offset_ev=settings.energy_offset_ev,
            energy_scale_ev=settings.energy_scale_ev,
            metadata=metadata,
        )

    def _metadata(self, *, live_time_s: float) -> dict[str, object]:
        """
        Return the metadata every spectrum from this detector carries.

        Parameters
        ----------
        live_time_s : float
            The live time this spectrum integrated for.

        Returns
        -------
        dict[str, object]
            The vocabulary documented on
            :class:`~miainwoodpecker.devices.interface.Spectrum`.
        """
        return {
            "device_id": self.detector_id,
            "live_time_s": live_time_s,
            # Real time exceeds live time by the dead-time fraction, which
            # is the whole reason both are recorded: their ratio is what a
            # quantification needs and neither alone gives.
            "real_time_s": live_time_s / (1.0 - _DEAD_TIME_FRACTION),
            "azimuth_deg": _DEFAULT_AZIMUTH_DEG,
            "elevation_deg": _DEFAULT_ELEVATION_DEG,
            "solid_angle_sr": _DEFAULT_SOLID_ANGLE_SR,
            "energy_resolution_ev": _DEFAULT_ENERGY_RESOLUTION_EV,
            "detector_type": "sdd",
            "technique": "eds",
            HIGH_TENSION_V_KEY: self._high_tension_v,
            "backend": SIMULATED_BACKEND,
            # The honesty flag, and the counterpart of camera_server's
            # photometrically_linear=False: nothing here was measured.
            "simulated": True,
        }


class ReplaySpectrumDetector:
    """
    A detector that replays a spectrum recording this project wrote.

    The ``hardware`` backend, and honestly labelled rather than
    pretending: a real EDX detector needs a vendor library that cannot be
    shipped here (see the module docstring), so what this backend can
    actually open is a file. That is worth having anyway, and for the
    reason ``camera_server``'s media-file device is worth having — a
    session recorded at the instrument becomes a fixture that runs
    anywhere, which is the only way this project can regression-test
    against real EDX data at all.

    Every spectrum it returns carries ``backend: "replay"`` and the path
    it came from, so no analysis can mistake replayed data for a fresh
    measurement.
    """

    def __init__(self, path: str) -> None:
        from miainwoodpecker.storage.spectra import read_spectra  # noqa: PLC0415

        try:
            recording = read_spectra(path)
        except (OSError, ValueError) as error:
            msg = (
                f"could not open {path!r} as a spectrum recording: {error}. "
                f"The hardware backend replays a file written by "
                f"miainwoodpecker.storage.spectra.SpectrumWriter; there is no "
                f"in-tree vendor backend, because neither Bruker's nor Oxford's "
                f"control library can be redistributed here. Use "
                f"--backend {SIMULATED_BACKEND} to run without a file."
            )
            raise SpectrumDetectorOpenError(msg) from error
        self._path = path
        self._recording = recording
        self._parameters = SpectrumParameters(
            live_time_s=float(recording.metadata[0].get("live_time_s", 1.0) or 1.0),
            channel_count=recording.channel_count,
            energy_scale_ev=recording.energy_scale_ev,
            energy_offset_ev=recording.energy_offset_ev,
        )
        self._index = 0
        self._closed = False
        self._lock = threading.Lock()

    @property
    def detector_id(self) -> str:
        """Return an identifier naming what was actually opened."""
        return f"replay:{self._path}"

    @property
    def acquisition_modes(self) -> Sequence[str]:
        """Return the modes the recording can serve, which is what it holds."""
        return [MAP_MODE] if self._recording.is_map else [SPOT_MODE]

    @property
    def is_closed(self) -> bool:
        """Return whether this device has been released."""
        return self._closed

    def parameters(self) -> SpectrumParameters:
        """Return the settings the recording was acquired with."""
        return self._parameters

    def configure(self, parameters: SpectrumParameters) -> SpectrumParameters:
        """
        Refuse to change anything, and say why rather than pretending.

        A replay cannot honour a different live time or energy
        calibration: the counts on disk were acquired at one setting, and
        relabelling them with another would put a wrong energy axis on
        real data. Returning the stored settings unchanged is what the
        ``configure`` contract already allows — "what the device took" —
        and here the device took nothing.

        Parameters
        ----------
        parameters : SpectrumParameters
            The requested settings, ignored.

        Returns
        -------
        SpectrumParameters
            The recording's own settings, unchanged.
        """
        del parameters
        return self._parameters

    def start(self) -> None:
        """Begin acquisition; a file is already there."""

    def stop(self) -> None:
        """Pause acquisition; nothing to do for a file."""

    def acquire_spectrum(self) -> Spectrum:
        """
        Return the next spectrum in the recording, wrapping around at the end.

        Returns
        -------
        Spectrum
            One recorded spectrum, labelled as replayed.

        Raises
        ------
        SpectrumDetectorOpenError
            If the recording holds a spectrum image rather than spot
            spectra — a map is served through :meth:`acquire_map`, and
            returning one slice of it here would silently change what the
            caller asked for.
        """
        if self._recording.is_map:
            msg = (
                f"{self._path!r} holds a spectrum image, so it replays through "
                f"acquire_map() rather than acquire_spectrum()"
            )
            raise SpectrumDetectorOpenError(msg)
        with self._lock:
            index = self._index % self._recording.count
            self._index += 1
        return self._replayed(self._recording.data[index], index)

    def acquire_map(self, parameters: ScanParameters) -> Spectrum:
        """
        Return the recorded spectrum image, ignoring the requested geometry.

        The geometry is refused rather than resampled: a recording was
        collected over one field of view at one pixel count, and handing
        it back as though it were a different scan would make every
        navigation-axis value wrong.

        Parameters
        ----------
        parameters : ScanParameters
            The requested geometry, which must match the recording's own
            shape.

        Returns
        -------
        Spectrum
            The recorded spectrum image, labelled as replayed.

        Raises
        ------
        SpectrumDetectorOpenError
            If the recording holds spot spectra, or if the requested
            shape is not the recorded one.
        """
        if not self._recording.is_map:
            msg = (
                f"{self._path!r} holds spot spectra, not a spectrum image, so "
                f"it replays through acquire_spectrum()"
            )
            raise SpectrumDetectorOpenError(msg)
        recorded_shape = self._recording.data.shape[:-1]
        if tuple(parameters.shape) != tuple(recorded_shape):
            msg = (
                f"{self._path!r} recorded a {recorded_shape} map; a replay "
                f"cannot serve a {tuple(parameters.shape)} one, because "
                f"resampling it would put wrong positions on real data"
            )
            raise SpectrumDetectorOpenError(msg)
        return self._replayed(self._recording.data, 0)

    def close(self) -> None:
        """Release the recording; idempotent."""
        self._closed = True

    def _replayed(self, data: npt.NDArray[typing.Any], index: int) -> Spectrum:
        """
        Wrap recorded counts as a Spectrum, labelled as a replay.

        Parameters
        ----------
        data : npt.NDArray[typing.Any]
            The recorded counts.
        index : int
            Which stored spectrum this is, for its metadata.

        Returns
        -------
        Spectrum
            The recorded spectrum with replay provenance attached.
        """
        metadata = dict(self._recording.metadata[min(index, self._recording.count - 1)])
        metadata["device_id"] = self.detector_id
        metadata["spectrum_index"] = index
        metadata["backend"] = "replay"
        metadata["replayed_from"] = self._path
        return Spectrum(
            data=data,
            timestamp=_now(),
            energy_offset_ev=self._recording.energy_offset_ev,
            energy_scale_ev=self._recording.energy_scale_ev,
            metadata=metadata,
        )


class ServerInstrument:
    """
    The ``instrument`` target for a detector that has no microscope around it.

    The same shape ``camera_server``'s has, and for the same reason: the
    client connects to this target first and asks what else exists, but an
    analyser bolted to someone else's column has no stage, no defocus, and
    no blanker of its own. Reporting an empty control list is the
    supported answer, and it is also the truthful one for a Bruker or
    Oxford analyser, which really does not drive the optics.
    """

    def __init__(
        self,
        targets: dict[str, object],
        stop_event: threading.Event,
        backend: str,
    ) -> None:
        self._targets = targets
        self._stop = stop_event
        self._backend = backend
        self._shutting_down = False

    def stage_size_nm(self) -> float:
        """
        Return a nominal extent, since there is no stage to ask.

        Returns
        -------
        float
            One micrometre, the same documented stand-in the other
            servers use when no controller publishes a stage size.
        """
        return 1000.0

    def available_controls(self) -> Sequence[str]:
        """Return no controls; an analyser drives none of the column."""
        return []

    def park(self) -> None:
        """Do nothing: there is no beam to blank and no column to park."""

    def describe(self) -> dict[str, object]:
        """
        Report what this server serves, as plain data.

        Returns
        -------
        dict[str, object]
            ``backend``, ``targets``, ``controls`` and ``stage_size_nm``.
        """
        return {
            "backend": self._backend,
            "targets": [name for name in self._targets if name != "instrument"],
            "controls": [],
            "stage_size_nm": self.stage_size_nm(),
        }

    def health(self) -> dict[str, object]:
        """
        Answer "is this server alive?" without touching the detector.

        Returns
        -------
        dict[str, object]
            The same shape the other servers report, so one client reads
            all three.
        """
        return {
            "ok": True,
            "pid": os.getpid(),
            "backend": self._backend,
            "uptime_s": 0.0,
            "targets": list(self._targets),
            "open_devices": [
                name
                for name, target in self._targets.items()
                if name != "instrument" and not getattr(target, "is_closed", False)
            ],
            "shutting_down": self._shutting_down,
        }

    def shutdown(self) -> dict[str, object]:
        """
        Release the detector and let the server exit.

        Returns
        -------
        dict[str, object]
            A report in the shape the client's teardown expects.
        """
        self._shutting_down = True
        errors: list[str] = []
        closed: list[str] = []
        for name, target in self._targets.items():
            if name == "instrument":
                continue
            try:
                target.close()
            except Exception as error:  # noqa: BLE001 - reported, not raised
                errors.append(f"{name}: {type(error).__name__}: {error}")
            else:
                closed.append(name)
        self._stop.set()
        return {"beam_blanked": False, "errors": errors, "closed": closed}


def open_detector(
    backend: str,
    device: str | None,
) -> SimulatedSpectrumDetector | ReplaySpectrumDetector:
    """
    Build the detector the requested backend describes.

    Parameters
    ----------
    backend : str
        ``"simulated"`` or ``"hardware"``.
    device : str | None
        What the hardware backend should open: the path of a spectrum
        recording. Ignored when simulated.

    Returns
    -------
    SimulatedSpectrumDetector | ReplaySpectrumDetector
        The detector to serve.

    Raises
    ------
    ValueError
        If ``backend`` is not one this server accepts.
    SpectrumDetectorOpenError
        If the hardware backend was asked for with nothing to open.
    """
    if backend not in SPECTRUM_BACKENDS:
        msg = (
            f"unknown backend {backend!r}; expected one of "
            f"{', '.join(SPECTRUM_BACKENDS)}"
        )
        raise ValueError(msg)
    if backend == SIMULATED_BACKEND:
        return SimulatedSpectrumDetector()
    if backend == HARDWARE_BACKEND:
        # Refused rather than quietly aliased to replay. A Bruker XFlash
        # through ESPRIT or an Oxford Ultim through AZtec needs a control
        # library that cannot be redistributed here, so there is no
        # in-tree vendor backend and there may never be one; a live
        # adapter is an out-of-tree device server. Answering "hardware"
        # with a file would mean an operator could run a whole session
        # believing a detector was attached.
        msg = (
            "this server has no hardware backend. A live EDX detector needs "
            "a vendor control library that cannot be redistributed here "
            "(Bruker ESPRIT, Oxford AZtec), so a real adapter is an "
            "out-of-tree device server - see "
            "miainwoodpecker.devices.remote.DEFAULT_SERVER_MODULE and "
            "docs/adapters/spectrum-detectors.md. To replay a spectrum "
            f"recording this project wrote, use --backend {REPLAY_BACKEND} "
            f"--plugin PATH; to run without any file, use --backend "
            f"{SIMULATED_BACKEND}."
        )
        raise SpectrumDetectorOpenError(msg)
    if not device:
        msg = (
            f"the {REPLAY_BACKEND} backend needs something to open, named "
            "with --plugin: the path of a spectrum recording this project "
            "wrote. That is how instrument data becomes a fixture that runs "
            f"anywhere. Use --backend {SIMULATED_BACKEND} to run without one."
        )
        raise SpectrumDetectorOpenError(msg)
    return ReplaySpectrumDetector(device)


def serve(
    ports: typing.Mapping[str, int],
    authkey: bytes,
    *,
    backend: str = SIMULATED_BACKEND,
    device: str | None = None,
) -> None:
    """
    Open the detector and serve it until a client shuts the server down.

    Parameters
    ----------
    ports : typing.Mapping[str, int]
        Port for each target name; only the ones served are bound.
    authkey : bytes
        Shared secret for every Listener.
    backend : str
        ``"simulated"`` or ``"hardware"``.
    device : str | None
        What the hardware backend opens.

    Raises
    ------
    SystemExit
        With :data:`PORT_UNAVAILABLE_EXIT_STATUS` when a listener cannot
        bind, so the client retries with fresh ports.
    """
    from multiprocessing.connection import Listener  # noqa: PLC0415 - see nion_server

    detector = open_detector(backend, device)
    stop_event = threading.Event()
    targets: dict[str, object] = {SPECTRUM_TARGET: detector}
    targets["instrument"] = ServerInstrument(targets, stop_event, backend)
    # A writer for the detector target even though a spot spectrum never
    # reaches the threshold: a spectrum image does, and the writer's
    # presence is also what makes accept_loop treat this target as
    # exclusive - one client at a time, which an accumulating detector
    # needs at least as much as a camera does.
    writers = {SPECTRUM_TARGET: SharedFrameWriter(), "instrument": None}

    names = list(targets)
    try:
        listeners = [
            Listener(("localhost", ports[name]), authkey=authkey) for name in names
        ]
    except OSError as error:
        _LOGGER.error(  # noqa: TRY400 - a traceback adds nothing here
            "could not bind a listener (%s); the client will retry",
            error,
        )
        raise SystemExit(PORT_UNAVAILABLE_EXIT_STATUS) from error

    _LOGGER.info("serving %s on backend %s", ", ".join(names), backend)
    for listener, name in zip(listeners, names, strict=True):
        threading.Thread(
            target=accept_loop,
            args=(listener, name, targets[name], writers[name], stop_event),
            daemon=True,
        ).start()
    stop_event.wait()
    for listener in listeners:
        listener.close()


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """
    Parse the server's command line, matching the other servers' shape.

    Parameters
    ----------
    argv : Sequence[str]
        Arguments after the program name.

    Returns
    -------
    argparse.Namespace
        With ``backend``, ``plugin`` and ``ports``.
    """
    parser = argparse.ArgumentParser(
        description=("Serve a spectrum detector over the miainwoodpecker protocol."),
    )
    parser.add_argument(
        "--backend", choices=SPECTRUM_BACKENDS, default=SIMULATED_BACKEND
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=None,
        help=(
            "what the hardware backend opens: the path of a spectrum "
            "recording written by this project, replayed as a device"
        ),
    )
    parser.add_argument("--enable-test-hooks", action="store_true")
    from miainwoodpecker.devices.rpc import TARGET_NAMES  # noqa: PLC0415 - argv shape

    parser.add_argument("ports", nargs=len(TARGET_NAMES), type=int)
    arguments = parser.parse_args(list(argv))
    if arguments.plugin is None:
        arguments.plugin = []
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the server from the command line.

    Parameters
    ----------
    argv : Sequence[str] | None
        Arguments after the program name, or None to read ``sys.argv``.

    Returns
    -------
    int
        Process exit status.
    """
    from miainwoodpecker.devices.rpc import TARGET_NAMES  # noqa: PLC0415 - argv shape

    logging.basicConfig(
        level=os.environ.get("MIAINWOODPECKER_DEVICE_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    ports = dict(zip(TARGET_NAMES, arguments.ports, strict=True))
    authkey = bytes.fromhex(os.environ["MIAINWOODPECKER_AUTHKEY"])
    device = arguments.plugin[0] if arguments.plugin else None
    try:
        serve(ports, authkey, backend=arguments.backend, device=device)
    except SpectrumDetectorOpenError as error:
        _LOGGER.error("%s", error)  # noqa: TRY400 - the message is the diagnostic
        return NO_DETECTOR_EXIT_STATUS
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
