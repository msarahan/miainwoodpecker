"""
A device server that runs inside someone else's application, for Gatan.

Every other adapter in this project is launched by
:func:`~miainwoodpecker.devices.remote.remote_instrument` as
``python -m something``. This one cannot be, and the reason is documented
by the vendor rather than inferred: Gatan's own Python FAQ states that the
DigitalMicrograph Python API depends on the DigitalMicrograph application
and that its functions cannot be executed from Python *outside* that
application. So a Gatan adapter is not a subprocess — it is a bridge that
an operator starts inside GMS, which then joins a session with this
client over the ordinary protocol. That is what
:func:`~miainwoodpecker.devices.remote.attached_instrument` exists for,
and this module is its reference implementation.

**Read this before assuming you need it.** Two scoping facts, both of
which cost less to check than this module costs to deploy:

- *A Gatan spectrometer on a Nion column is very likely not a Gatan
  problem at all.* Nion's instrumentation kit models an optional
  ``eels_camera`` on the STEM controller, and this project's
  :mod:`miainwoodpecker.devices.nion_server` already serves it and already
  exposes the spectrometer's energy offset as an
  :class:`~miainwoodpecker.devices.interface.InstrumentController` control
  (Nion's ``ZLPoffset``). Where the spectrometer is registered in Nion's
  ``Registry`` as the EELS camera, it is supported **today**, over the
  existing spawn path, with none of this module involved. SuperSTEM 2 — a
  Nion UltraSTEM 100 with a Gatan UHV Enfina — is exactly that shape, and
  the check is one command on the instrument PC: run the Nion server with
  ``--backend hardware`` and read what ``describe()`` reports. See
  docs/adapters/gatan.md.
- *This module is for the other case*: a Gatan camera or spectrometer whose
  only control surface is GMS — on a non-Nion column, or where the vendor
  integration genuinely goes through DigitalMicrograph.

**Two backends, for the reason every server here has two.** ``simulated``
runs anywhere, needs no Gatan software, and is what CI exercises; it is a
mock of *this bridge's behaviour*, not a stub — same connection
choreography, same threading, same targets, same metadata, same shutdown
semantics, with only the device objects swapped. ``hardware`` imports
``DigitalMicrograph``, which exists only inside GMS.

What the hardware backend does and does not claim
-------------------------------------------------
Written from documentation, never run against GMS, and the boundary is
marked here rather than left for someone to discover:

- **Acquisition reads DM's front image.** Not a private acquisition
  loop of our own, deliberately. It is the pattern InsteaDMatic uses to
  collect continuous-rotation electron diffraction inside DM —
  synchronising with the live view, which makes it independent of which
  Gatan camera is fitted — and it means the operator keeps using DM's own
  camera controls, with this bridge as a reader rather than a competitor
  for the detector. The precondition is therefore real and stated:
  **a live view must be running in DM.**
- **Instrument control goes through DM-Script, and the command names are
  configuration.** ``DM.ExecuteScriptString`` reaches the whole DM-Script
  surface — which is how ``execdmscript`` works — so the bridge is never
  limited to whatever the Python API happens to wrap. But the exact
  spelling of the imaging-filter energy-offset commands differs by
  spectrometer generation, and a UHV Enfina is two generations older than
  the GIF Quantum/Continuum documentation describes. Rather than hard-code
  a guess, the snippets are parameters with placeholder defaults that
  **must be confirmed on hardware** (docs/hardware-validation-checklist.md).
  A snippet that fails surfaces as an ordinary
  :class:`~miainwoodpecker.devices.rpc.RemoteCallError` naming the script
  that failed, not as a wrong number.

Running it inside GMS
---------------------
The embedded interpreter is not a normal one, and three of its quirks are
load-bearing here:

- **``sys.argv`` may not exist.** LiberTEM documents setting it before
  anything that needs it. So :func:`run_bridge` takes keyword arguments
  and :func:`main` touches ``sys.argv`` only when it has one.
- **Long work belongs off the calling thread.** :func:`start_bridge`
  returns a handle rather than blocking, which is what a DM script window
  wants.
- **The interpreter is old.** Gatan's FAQ names Python 3.7.2 for the
  GMS 3.4-era ``GMS_VENV_PYTHON`` environment, against this project's
  ``requires-python >= 3.11``. Two consequences. First, the pickle
  protocol: the client caps outgoing calls at
  :data:`~miainwoodpecker.devices.rpc.COMPATIBLE_PICKLE_PROTOCOL` for
  exactly this reason. Second, installation: ``pip install
  miainwoodpecker`` into a 3.7 environment will be refused, so on such a
  GMS the three modules this bridge needs
  (:mod:`~miainwoodpecker.devices.interface`,
  :mod:`~miainwoodpecker.devices.rpc`,
  :mod:`~miainwoodpecker.devices.serving`) have to be copied in by hand.
  This module is written to import on 3.8 for that reason — no ``match``,
  no ``datetime.UTC``, no ``zip(strict=)``.

Licence
-------
MIT, like everything here outside ``nion_server``. Nothing of Gatan's is
redistributed: ``DigitalMicrograph`` is imported from the copy GMS
installed, on the microscope's own computer. The one obvious off-the-shelf
alternative — LaurentRDC's ``gms-socket-plugin``, which exposes
``TCPSocketBind``/``TCPSocketConnect`` to DM-Script — is **GPL-3.0**, and
is therefore deliberately not used: pulling it in would put the project
back in exactly the position the subprocess boundary exists to avoid.
Python's own ``socket`` module inside the embedded interpreter needs no
such plug-in.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import logging
import os
import sys
import threading
import time
import typing

import numpy as np

from miainwoodpecker.devices.interface import (
    ENERGY_OFFSET_CONTROL,
    IMAGE_READOUT,
    CameraParameters,
    Frame,
)
from miainwoodpecker.devices.rpc import (
    BACKENDS,
    SIMULATED_BACKEND,
    TARGET_NAMES,
    disable_nagle,
)
from miainwoodpecker.devices.serving import accept_loop, serve_connection

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

    from miainwoodpecker.devices.remote import AttachInvitation

_LOGGER = logging.getLogger("miainwoodpecker.devices.gatan_bridge")

__all__ = [
    "BridgeInstrument",
    "DigitalMicrographCamera",
    "DigitalMicrographSpectrometer",
    "GatanUnavailableError",
    "SimulatedEELSCamera",
    "SimulatedSpectrometer",
    "main",
    "run_bridge",
    "start_bridge",
]

EELS_TARGET = "eels_camera"
"""
The target a Gatan spectrometer's camera is served on.

Not the neutral ``camera`` slot: an EELS camera is one of the two names
the vocabulary already has, every existing recording and the viewer use
it, and calling a spectrometer's readout "camera" would lose the one fact
an analysis tool most needs about it.
"""

# The bridge dials (or binds) these in this order, and the order is part
# of the contract with attached_instrument(): two connections to the
# instrument target first - control, then health - and the device targets
# afterwards. It mirrors the order the spawn path opens them in.
_INSTRUMENT_TARGET = "instrument"

_DEFAULT_EXPOSURE_MS = 100.0
_DEFAULT_DISPERSION_EV = 0.5
_SIMULATED_SHAPE = (64, 1024)
_DIAL_RETRY_S = 0.25

# Placeholder DM-Script snippets. Named constants rather than literals in
# the class because they are the part a facility must replace, and because
# a checklist item should be able to point at something.
#
# The contract of each: the getter writes the value into the persistent
# tag group under GET_TAG (which is how execdmscript passes values back
# out of DM-Script), and the setter reads a DM-Script variable named
# offset_ev that the bridge prepends. Both are executed by
# DM.ExecuteScriptString.
ENERGY_OFFSET_GET_SCRIPT = (
    "number value = IFGetEnergyLoss()\n"
    "GetPersistentTagGroup().TagGroupSetTagAsFloat("
    '"miainwoodpecker:energy_offset_ev", value)\n'
)
ENERGY_OFFSET_SET_SCRIPT = "IFSetEnergyLoss(offset_ev)\n"
ENERGY_OFFSET_TAG = "miainwoodpecker:energy_offset_ev"


class GatanUnavailableError(RuntimeError):
    """Raised when the hardware backend is asked for outside DigitalMicrograph."""


class BridgeAuthenticationError(RuntimeError):
    """
    Raised when the client rejects this bridge's authkey.

    Its own type because the operator response is specific and nothing
    else produces it: the two ends are holding different copies of the
    invitation, which is the expected consequence of publishing a
    rendezvous to a file somebody has to copy.
    """


def _now() -> datetime.datetime:
    """
    Return the current time, timezone-aware, as every Frame requires.

    Returns
    -------
    datetime.datetime
        ``datetime.timezone.utc`` rather than ``datetime.UTC``: the latter
        is 3.11 and this module has to import on the embedded interpreter.
    """
    return datetime.datetime.now(tz=datetime.timezone.utc)  # noqa: UP017 - see above


def _digital_micrograph() -> typing.Any:  # noqa: ANN401 - the vendor module
    """
    Import GMS's ``DigitalMicrograph`` module, or say why that failed.

    Returns
    -------
    typing.Any
        The vendor module.

    Raises
    ------
    GatanUnavailableError
        If it is not importable, which outside GMS it never is.
    """
    try:
        import DigitalMicrograph  # noqa: PLC0415 - vendor module, inside DM only
    except ImportError as error:
        msg = (
            "the 'hardware' backend of this bridge needs the DigitalMicrograph "
            "module, which exists only inside a running Gatan Microscopy "
            "Suite. Gatan's own documentation is explicit that DM functions "
            "cannot be executed from Python outside the application, so this "
            "is not something an install can fix: run the bridge from GMS's "
            "script window, or use the 'simulated' backend."
        )
        raise GatanUnavailableError(msg) from error
    return DigitalMicrograph


def _energy_calibration(
    dispersion_ev: float,
    offset_ev: float,
    channels: int,
) -> dict[str, object]:
    """
    Describe the dispersive axis in the project's calibration vocabulary.

    Parameters
    ----------
    dispersion_ev : float
        Electronvolts per channel.
    offset_ev : float
        The spectrometer's energy offset, which is what moves the whole
        axis. Recorded per frame rather than once per session because a
        drift-tube sweep changes it between frames — the same reason the
        focal series records defocus per frame.
    channels : int
        Number of dispersive channels, so the axis can be centred on the
        zero-loss position the offset implies.

    Returns
    -------
    dict[str, object]
        The ``metadata['calibration']`` mapping
        :func:`miainwoodpecker.storage.calibration.resolve_frame_calibration`
        reads. The non-dispersive axis is left uncalibrated rather than
        given a guessed length scale: on a spectrometer camera it is the
        non-dispersive direction of the spectrum, and inventing a
        nanometre scale for it would be a claim about optics this bridge
        cannot make.
    """
    return {
        "y": {"kind": "uncalibrated"},
        "x": {
            "kind": "energy",
            "scale": dispersion_ev,
            "offset": offset_ev - dispersion_ev * (channels / 2.0),
            "units": "eV",
        },
    }


class SimulatedSpectrometer:
    """
    A spectrometer whose only control is the energy offset, in software.

    Faithful in the way that matters for testing the path: the offset is
    not merely stored, it *moves the zero-loss peak* in
    :class:`SimulatedEELSCamera`'s frames. A simulator whose control had
    no observable effect would let a broken wiring between the instrument
    target and the camera target pass every test.
    """

    def __init__(self, dispersion_ev: float = _DEFAULT_DISPERSION_EV) -> None:
        self.dispersion_ev = float(dispersion_ev)
        self._offset_ev = 0.0

    def energy_offset_ev(self) -> float:
        """
        Return the spectrometer energy offset, in electronvolts.

        Returns
        -------
        float
            The offset last set.
        """
        return self._offset_ev

    def set_energy_offset_ev(self, offset_ev: float) -> None:
        """
        Set the spectrometer energy offset, in electronvolts.

        Parameters
        ----------
        offset_ev : float
            Target offset.
        """
        self._offset_ev = float(offset_ev)


class DigitalMicrographSpectrometer:
    """
    The energy offset, driven through DM-Script from inside GMS.

    Python rather than DM-Script would be preferable and is not available:
    the imaging-filter controls live in DM-Script's command set, and
    ``DM.ExecuteScriptString`` is the documented way to reach it from the
    embedded Python (it is what ``execdmscript`` is built on). Values come
    back through the persistent tag group for the same reason — DM-Script
    has no return channel to Python except the tag tree.

    **The command names are unverified.** See the module docstring: they
    are constructor parameters with placeholder defaults precisely so a
    facility can correct them without editing this file, and so the
    correction is a checklist item rather than a code change.
    """

    def __init__(
        self,
        dispersion_ev: float = _DEFAULT_DISPERSION_EV,
        get_script: str = ENERGY_OFFSET_GET_SCRIPT,
        set_script: str = ENERGY_OFFSET_SET_SCRIPT,
        tag: str = ENERGY_OFFSET_TAG,
    ) -> None:
        self.dispersion_ev = float(dispersion_ev)
        self._get_script = get_script
        self._set_script = set_script
        self._tag = tag

    def energy_offset_ev(self) -> float:
        """
        Read the spectrometer energy offset from DM, in electronvolts.

        Returns
        -------
        float
            The value the getter script wrote into the persistent tags.

        Raises
        ------
        RuntimeError
            If the script ran but left no value, which means the command
            name is wrong for this spectrometer rather than that the
            offset is zero — a distinction worth raising over, because the
            wrong answer here is silently mis-labelled spectra.
        """
        dm = _digital_micrograph()
        dm.ExecuteScriptString(self._get_script)
        found, value = dm.GetPersistentTagGroup().GetTagAsFloat(self._tag)
        if not found:
            msg = (
                f"the energy-offset getter script ran but wrote no "
                f"{self._tag!r} tag. The DM-Script command it uses is almost "
                f"certainly not the one this spectrometer answers to; pass "
                f"the correct snippet as get_script."
            )
            raise RuntimeError(msg)
        return float(value)

    def set_energy_offset_ev(self, offset_ev: float) -> None:
        """
        Set the spectrometer energy offset through DM-Script.

        Parameters
        ----------
        offset_ev : float
            Target offset, in electronvolts. Passed by prepending a
            DM-Script declaration rather than by string-formatting it into
            the snippet, so a facility's replacement snippet reads a named
            variable instead of a hole in its own source.
        """
        dm = _digital_micrograph()
        dm.ExecuteScriptString(
            f"number offset_ev = {float(offset_ev)}\n" + self._set_script,
        )


class SimulatedEELSCamera:
    """
    A spectrometer camera that synthesises a zero-loss peak, offset and all.

    Deliberately not a still image and not pure noise: the peak sits where
    the spectrometer's energy offset puts it, and successive frames differ
    everywhere, so the frame-identity contracts ported from Nion's suite
    (a held frame must not change; consecutive frames must not be
    identical) are meaningful rather than vacuously true against it.
    """

    def __init__(
        self,
        spectrometer: SimulatedSpectrometer,
        shape: tuple[int, int] = _SIMULATED_SHAPE,
    ) -> None:
        self._spectrometer = spectrometer
        self._shape = shape
        self._parameters = CameraParameters(exposure_ms=_DEFAULT_EXPOSURE_MS)
        self._frame_index = 0
        self._running = False
        self._closed = False
        self._generator = np.random.default_rng()

    @property
    def camera_id(self) -> str:
        """
        Return this camera's stable identifier.

        Returns
        -------
        str
            A name that says it is not a real spectrometer.
        """
        return "simulated_gatan_eels"

    @property
    def binning_values(self) -> Sequence[int]:
        """
        Return the binning factors supported.

        Returns
        -------
        Sequence[int]
            The powers of two a spectrometer camera typically offers.
            Binning on a spectrometer camera bins the *dispersive* axis
            too, which multiplies the dispersion — see
            :meth:`acquire_frame`, which is where that has to show up.
        """
        return [1, 2, 4, 8]

    def parameters(self) -> CameraParameters:
        """
        Return the settings the next frame will use.

        Returns
        -------
        CameraParameters
            The current exposure and binning.
        """
        return self._parameters

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Apply settings and report what was taken.

        Parameters
        ----------
        parameters : CameraParameters
            The requested exposure and binning.

        Returns
        -------
        CameraParameters
            What the device took.

        Raises
        ------
        ValueError
            If the binning is not one this camera offers, or a readout
            other than ``image`` is requested.
        """
        if parameters.binning not in self.binning_values:
            msg = (
                f"binning {parameters.binning} is not one of "
                f"{list(self.binning_values)} on {self.camera_id}"
            )
            raise ValueError(msg)
        if parameters.readout != IMAGE_READOUT:
            msg = (
                f"readout {parameters.readout!r} is not supported by "
                f"{self.camera_id}: this simulator stands in for the DM "
                f"front-image path, which delivers whatever DigitalMicrograph "
                f"is showing — a projection would be DM's to perform, in its "
                f"own SI/spectrum modes"
            )
            raise ValueError(msg)
        self._parameters = parameters
        return self._parameters

    def start(self) -> None:
        """Begin acquisition."""
        self._running = True

    def stop(self) -> None:
        """Pause acquisition."""
        self._running = False

    def acquire_frame(self) -> Frame:
        """
        Return a synthesised spectrum image whose peak follows the offset.

        Returns
        -------
        Frame
            A 2D frame, dispersive axis last, calibrated in eV.
        """
        binning = self._parameters.binning
        height, width = self._shape[0] // binning, self._shape[1] // binning
        dispersion_ev = self._spectrometer.dispersion_ev * binning
        offset_ev = self._spectrometer.energy_offset_ev()
        # The zero-loss peak sits at the energy the offset selects, so
        # driving set_energy_offset_ev() through the instrument target
        # visibly moves the peak in frames from the camera target. A
        # simulator where it did not would hide a mis-wired bridge.
        channels = np.arange(width, dtype=np.float64)
        centre = width / 2.0 - offset_ev / dispersion_ev
        peak = 1000.0 * np.exp(-0.5 * ((channels - centre) / 3.0) ** 2)
        data = np.tile(peak, (height, 1))
        data = data + self._generator.normal(0.0, 1.0, (height, width))
        metadata = {
            "device_id": self.camera_id,
            "frame_index": self._frame_index,
            "exposure_ms": self._parameters.exposure_ms,
            "binning": binning,
            "camera_name": "simulated Gatan spectrometer camera",
            "camera_type": "eels",
            "energy_offset_ev": offset_ev,
            # A spectrometer camera is a counting/integrating scientific
            # detector, unlike the commodity-camera server's frames: this
            # says so and means it.
            "photometrically_linear": True,
            "calibration": _energy_calibration(dispersion_ev, offset_ev, width),
        }
        self._frame_index += 1
        return Frame(
            data=data.astype(np.float32),
            timestamp=_now(),
            metadata=metadata,
        )

    def close(self) -> None:
        """Release nothing; idempotent, like every adapter's close."""
        self._closed = True


class DigitalMicrographCamera:
    """
    A camera read from DigitalMicrograph's front image, inside GMS.

    Reading DM's own live view rather than driving an acquisition of our
    own is the deliberate choice, and it has two justifications beyond
    being simpler. It is the published pattern — InsteaDMatic collects
    continuous-rotation diffraction this way, synchronising with the live
    view so that it works with whichever Gatan camera is fitted. And it
    avoids the ownership fight: DM keeps the detector and the operator
    keeps DM's controls, while this bridge reads what is already being
    produced. The cost is stated rather than hidden: **exposure and
    binning are DM's to set**, so :meth:`configure` records a request and
    reports back what DM says, which on this path is what DM was already
    doing.
    """

    def __init__(self, spectrometer: DigitalMicrographSpectrometer) -> None:
        self._spectrometer = spectrometer
        self._parameters = CameraParameters(exposure_ms=_DEFAULT_EXPOSURE_MS)
        self._frame_index = 0
        self._closed = False
        self._lock = threading.Lock()

    @property
    def camera_id(self) -> str:
        """
        Return this camera's stable identifier.

        Returns
        -------
        str
            Names the route, not a model: the bridge does not know which
            Gatan camera DM has in front.
        """
        return "gatan_dm_front_image"

    @property
    def binning_values(self) -> Sequence[int]:
        """
        Return the binning factors this path can report.

        Returns
        -------
        Sequence[int]
            ``[1]``. Not because Gatan cameras do not bin — they do — but
            because on this path DM owns the acquisition, so the honest
            answer is that the bridge cannot change it. An adapter that
            drove the camera manager directly would report the real list.
        """
        return [1]

    def parameters(self) -> CameraParameters:
        """
        Return the settings the next frame will use.

        Returns
        -------
        CameraParameters
            What was last requested; see the class docstring for why this
            is a record rather than a reading.
        """
        return self._parameters

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Record a settings request and return what this path can honour.

        Parameters
        ----------
        parameters : CameraParameters
            The requested exposure and binning.

        Returns
        -------
        CameraParameters
            The exposure as asked and binning 1, which is what the front
            image path delivers.

        Raises
        ------
        ValueError
            If a binning other than 1 is requested, because pretending to
            accept it would put a wrong number in every frame's metadata
            and a wrong scale on every calibration — or a readout other
            than ``image``, for the same reason DM owns the binning.
        """
        if parameters.binning != 1:
            msg = (
                f"binning {parameters.binning} cannot be set from this bridge: "
                f"the front-image path reads what DigitalMicrograph is already "
                f"acquiring, so binning is set in DM's own camera controls"
            )
            raise ValueError(msg)
        if parameters.readout != IMAGE_READOUT:
            msg = (
                f"readout {parameters.readout!r} cannot be set from this "
                f"bridge: the front-image path reads what DigitalMicrograph "
                f"is already acquiring, so a projected (summed) readout is "
                f"DM's to configure in its own spectrum modes"
            )
            raise ValueError(msg)
        self._parameters = parameters
        return self._parameters

    def start(self) -> None:
        """
        Check that DM has a live image to read.

        Raises
        ------
        RuntimeError
            If there is no front image, which is the precondition this
            path has and the one an operator will forget.
        """
        dm = _digital_micrograph()
        if dm.GetFrontImage() is None:
            msg = (
                "DigitalMicrograph has no front image, so there is nothing for "
                "this bridge to read. Start a live view on the camera in DM "
                "first; this bridge reads DM's acquisition rather than taking "
                "the detector from it."
            )
            raise RuntimeError(msg)

    def stop(self) -> None:
        """Do nothing: DM's live view is DM's to stop."""

    def acquire_frame(self) -> Frame:
        """
        Copy DM's current front image out as a frame.

        Returns
        -------
        Frame
            A 2D frame with the spectrometer's offset recorded on it.

        Raises
        ------
        RuntimeError
            If DM has no front image at this moment.
        """
        dm = _digital_micrograph()
        with self._lock:
            image = dm.GetFrontImage()
            if image is None:
                msg = (
                    "DigitalMicrograph's front image disappeared mid-session; "
                    "the live view was probably closed"
                )
                raise RuntimeError(msg)
            # A copy, not a view: DM owns that buffer and will write the
            # next live frame into it, and this one is about to travel to
            # another process.
            data = np.array(image.GetNumArray(), copy=True)
        offset_ev = self._spectrometer.energy_offset_ev()
        dispersion_ev = self._spectrometer.dispersion_ev
        metadata = {
            "device_id": self.camera_id,
            "frame_index": self._frame_index,
            "exposure_ms": self._parameters.exposure_ms,
            "binning": 1,
            "camera_name": "DigitalMicrograph front image",
            "camera_type": "eels",
            "energy_offset_ev": offset_ev,
            "photometrically_linear": True,
            "calibration": _energy_calibration(
                dispersion_ev,
                offset_ev,
                int(data.shape[-1]),
            ),
        }
        self._frame_index += 1
        return Frame(data=data, timestamp=_now(), metadata=metadata)

    def close(self) -> None:
        """Release nothing; DM owns the camera. Idempotent."""
        self._closed = True


class BridgeInstrument:
    """
    The ``instrument`` target for a spectrometer with no column behind it.

    Every server needs this target — the client connects to it first and
    asks what else exists — and what is interesting here is how little it
    can honestly offer. A Gatan spectrometer is not the microscope: it has
    no stage, no defocus, and no beam blanker, because those belong to
    whichever column vendor's software is running next door. So the
    control list has exactly one entry, the stage-position and defocus
    methods are simply absent (a call gets ``unknown method``, which is
    the protocol's answer for "this server does not do that"), and
    :meth:`park` does nothing.

    ``park`` doing nothing deserves the explicit note, because on the Nion
    path it blanks the beam and that is a safety property. Here there is
    no beam to blank — this bridge cannot make the column safe, and a
    ``park`` that pretended to would be worse than one that admits it.
    Whatever parks the column has to be the column's own adapter.
    """

    def __init__(
        self,
        targets: dict[str, object],
        spectrometer: object,
        stop_event: threading.Event,
        backend: str,
    ) -> None:
        self._targets = targets
        self._spectrometer = spectrometer
        self._stop = stop_event
        self._backend = backend
        self._started = time.monotonic()
        self._shutting_down = False

    def stage_size_nm(self) -> float:
        """
        Return a nominal extent, since there is no stage to ask.

        Returns
        -------
        float
            One micrometre, the same documented stand-in the other servers
            use when no stage size is published.
        """
        return 1000.0

    def available_controls(self) -> Sequence[str]:
        """
        Return the one control a spectrometer actually has.

        Returns
        -------
        Sequence[str]
            ``[energy_offset]``.
        """
        return [ENERGY_OFFSET_CONTROL]

    def energy_offset_ev(self) -> float:
        """
        Return the spectrometer energy offset, in electronvolts.

        Returns
        -------
        float
            The offset.
        """
        return float(self._spectrometer.energy_offset_ev())

    def set_energy_offset_ev(self, offset_ev: float) -> None:
        """
        Set the spectrometer energy offset, in electronvolts.

        Parameters
        ----------
        offset_ev : float
            Target offset.
        """
        self._spectrometer.set_energy_offset_ev(offset_ev)

    def park(self) -> None:
        """Do nothing: there is no beam here to blank. See the class docstring."""

    def describe(self) -> dict[str, object]:
        """
        Report what this bridge serves, as plain data.

        Returns
        -------
        dict[str, object]
            ``backend``, ``targets``, ``controls`` and ``stage_size_nm``.
        """
        return {
            "backend": self._backend,
            "targets": [name for name in self._targets if name != _INSTRUMENT_TARGET],
            "controls": list(self.available_controls()),
            "stage_size_nm": self.stage_size_nm(),
        }

    def health(self) -> dict[str, object]:
        """
        Answer "is this bridge alive?" without touching the spectrometer.

        Returns
        -------
        dict[str, object]
            The same shape the other servers report, so one client reads
            all of them. ``pid`` is the *host application's* pid here —
            DigitalMicrograph's — which is worth knowing and is also
            exactly why the client will not describe a lost connection as
            "the process exited": that process is very likely still
            running.
        """
        return {
            "ok": True,
            "pid": os.getpid(),
            "backend": self._backend,
            "uptime_s": time.monotonic() - self._started,
            "targets": list(self._targets),
            "open_devices": [
                name
                for name, target in self._targets.items()
                if name != _INSTRUMENT_TARGET and not getattr(target, "_closed", False)
            ],
            "shutting_down": self._shutting_down,
        }

    def shutdown(self) -> dict[str, object]:
        """
        End this session, without ending the application the bridge lives in.

        The one place the bridge's semantics differ from a spawned
        server's, and the difference is not negotiable: a spawned server
        exits its process here, and doing the equivalent would mean
        terminating DigitalMicrograph — with somebody's acquisition in it.
        So this releases what the *session* took and leaves the host alone,
        which also makes the bridge re-attachable for the next session.

        Returns
        -------
        dict[str, object]
            A report in the shape the client's teardown expects.
        """
        self._shutting_down = True
        errors: list[str] = []
        closed: list[str] = []
        for name, target in self._targets.items():
            if name == _INSTRUMENT_TARGET:
                continue
            try:
                target.close()
            except Exception as error:  # noqa: BLE001 - reported, not raised
                errors.append(f"{name}: {type(error).__name__}: {error}")
            else:
                closed.append(name)
        self._stop.set()
        return {"beam_blanked": False, "errors": errors, "closed": closed}


def _build_targets(
    backend: str,
    dispersion_ev: float,
) -> tuple[dict[str, object], object]:
    """
    Build the devices this bridge serves for a backend.

    Parameters
    ----------
    backend : str
        ``"simulated"`` or ``"hardware"``.
    dispersion_ev : float
        Electronvolts per unbinned channel, which nothing on this path can
        read from the hardware and which someone therefore has to state.

    Returns
    -------
    tuple[dict[str, object], object]
        The target map (without the instrument target) and the
        spectrometer the instrument target will drive.

    Raises
    ------
    ValueError
        If ``backend`` is not one this bridge accepts.
    """
    if backend not in BACKENDS:
        msg = f"unknown backend {backend!r}; expected one of {', '.join(BACKENDS)}"
        raise ValueError(msg)
    if backend == SIMULATED_BACKEND:
        spectrometer: object = SimulatedSpectrometer(dispersion_ev)
        camera: object = SimulatedEELSCamera(
            typing.cast("SimulatedSpectrometer", spectrometer),
        )
    else:
        # Eagerly, before anything is served. The lazy import inside each
        # method is what lets this module import anywhere; it is not what
        # should decide whether a *session* can start. Without this check
        # the bridge would attach happily and then fail every single call,
        # which is a much worse failure than refusing to start - and it is
        # the one an operator would meet by running the hardware backend
        # from a shell instead of from DM's script window.
        _digital_micrograph()
        spectrometer = DigitalMicrographSpectrometer(dispersion_ev)
        camera = DigitalMicrographCamera(
            typing.cast("DigitalMicrographSpectrometer", spectrometer),
        )
    return {EELS_TARGET: camera}, spectrometer


def _dial(
    host: str,
    port: int,
    authkey: bytes,
    deadline: float,
) -> typing.Any:  # noqa: ANN401 - multiprocessing Connection
    """
    Connect out to a client that is listening, retrying until a deadline.

    Retrying is the whole point rather than a nicety: a bridge inside GMS
    is normally started *before* the application it serves, so "connection
    refused" is the expected state for as long as the operator takes to
    launch the client, and giving up on the first one would make the
    inbound direction unusable.

    Parameters
    ----------
    host : str
        The client's host.
    port : int
        The client's port for this target.
    authkey : bytes
        The shared secret from the invitation.
    deadline : float
        ``time.monotonic()`` value after which to give up.

    Returns
    -------
    typing.Any
        The connected end, with Nagle disabled.

    Raises
    ------
    OSError
        If nothing was listening by the deadline.
    BridgeAuthenticationError
        If something was listening and rejected this bridge's key. Not
        retried, unlike a refusal: a wrong shared secret is wrong on every
        attempt, and retrying it until the deadline would report a
        timeout for a problem that was already known in milliseconds.
    """
    from multiprocessing.connection import (  # noqa: PLC0415 - lazy, as elsewhere
        AuthenticationError,
        Client,
    )

    while True:
        try:
            connection = Client((host, port), authkey=authkey)
        except AuthenticationError as error:
            msg = (
                f"the client listening on {host}:{port} rejected this "
                f"bridge's authkey. Both ends must use the same invitation "
                f"file; a stale copy on the microscope PC is the usual cause."
            )
            raise BridgeAuthenticationError(msg) from error
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(_DIAL_RETRY_S)
        else:
            disable_nagle(connection)
            return connection


def run_bridge(  # noqa: PLR0913 - the bridge's whole configuration, all named
    invitation: AttachInvitation,
    *,
    backend: str = SIMULATED_BACKEND,
    dispersion_ev: float = _DEFAULT_DISPERSION_EV,
    timeout_s: float = 120.0,
    sessions: int = 1,
    ready: threading.Event | None = None,
) -> None:
    """
    Serve one or more client sessions, in whichever socket direction was agreed.

    Blocks. Inside GMS use :func:`start_bridge` instead, which runs this on
    a thread — the embedded interpreter does not want long work on the
    thread a script window called from.

    Parameters
    ----------
    invitation : AttachInvitation
        The rendezvous both ends share: host, ports, authkey, and which
        end binds. Read it from the file the client published
        (:meth:`~miainwoodpecker.devices.remote.AttachInvitation.read_from`)
        rather than retyping it.
    backend : str
        ``"simulated"`` (needs no Gatan software) or ``"hardware"``
        (needs to be running inside GMS).
    dispersion_ev : float
        Electronvolts per unbinned channel, for the frame calibration.
    timeout_s : float
        How long to keep trying to reach the client, per connection.
    sessions : int
        How many client sessions to serve before returning. ``0`` means
        until stopped, which is what an operator inside GMS wants: the
        host application outlives many runs of the client, and a bridge
        that had to be restarted for each of them would be a worse
        arrangement than the one it replaced.
    ready : threading.Event | None
        Set once this bridge is listening or has begun dialling. Only
        useful to a test or a launcher that must not race the client.

    Raises
    ------
    ValueError
        If the invitation's transport is not one this bridge implements.
    """
    from miainwoodpecker.devices.remote import (  # noqa: PLC0415 - avoids a cycle
        ACCEPT_TRANSPORT,
        CONNECT_TRANSPORT,
    )

    if invitation.transport not in (ACCEPT_TRANSPORT, CONNECT_TRANSPORT):
        msg = f"unknown transport {invitation.transport!r}"
        raise ValueError(msg)
    served = 0
    while sessions == 0 or served < sessions:
        devices, spectrometer = _build_targets(backend, dispersion_ev)
        stop_event = threading.Event()
        targets = dict(devices)
        targets[_INSTRUMENT_TARGET] = BridgeInstrument(
            targets,
            spectrometer,
            stop_event,
            backend,
        )
        if invitation.transport == ACCEPT_TRANSPORT:
            _serve_by_dialling(invitation, targets, stop_event, timeout_s, ready)
        else:
            _serve_by_listening(invitation, targets, stop_event, ready)
        served += 1


def _serve_by_dialling(
    invitation: AttachInvitation,
    targets: dict[str, object],
    stop_event: threading.Event,
    timeout_s: float,
    ready: threading.Event | None,
) -> None:
    """
    Dial the client's listeners and serve each connection until shutdown.

    The order is the contract: the instrument target twice — control
    first, health second — then one connection per device target. Each
    connection gets its own handler thread as soon as it is up, which is
    what lets the client call ``describe()`` on the control connection
    while this function is still dialling the rest.

    No ``SharedFrameWriter`` is passed to any of them. That is deliberate
    and is the one capability the inbound path gives up: an attached
    client may be on a different machine, where a shared-memory segment
    means nothing, so frames travel as ordinary pickles.

    Parameters
    ----------
    invitation : AttachInvitation
        The rendezvous.
    targets : dict[str, object]
        The devices to serve, keyed by target name.
    stop_event : threading.Event
        Set by the instrument target's ``shutdown``.
    timeout_s : float
        Per-connection dial deadline.
    ready : threading.Event | None
        Set once dialling has begun.
    """
    connections = []
    if ready is not None:
        ready.set()
    try:
        order = [_INSTRUMENT_TARGET, _INSTRUMENT_TARGET] + [
            name
            for name in TARGET_NAMES
            if name in targets and name != _INSTRUMENT_TARGET
        ]
        for name in order:
            deadline = time.monotonic() + timeout_s
            connection = _dial(
                invitation.host,
                invitation.ports[name],
                invitation.authkey,
                deadline,
            )
            connections.append(connection)
            _LOGGER.info("bridge: connected out for target %s", name)
            threading.Thread(
                target=serve_connection,
                args=(connection, name, targets[name], None, stop_event),
                name=f"bridge-serve-{name}",
                daemon=True,
            ).start()
        stop_event.wait()
    finally:
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.close()


def _serve_by_listening(
    invitation: AttachInvitation,
    targets: dict[str, object],
    stop_event: threading.Event,
    ready: threading.Event | None,
) -> None:
    """
    Bind a listener per served target and let the client dial in.

    The direction to prefer where a firewall allows it, for an
    operational reason rather than an architectural one: GMS is started
    once and outlives many runs of the client, so a listening bridge is
    simply there whenever a session wants it.

    Parameters
    ----------
    invitation : AttachInvitation
        The rendezvous.
    targets : dict[str, object]
        The devices to serve, keyed by target name.
    stop_event : threading.Event
        Set by the instrument target's ``shutdown``.
    ready : threading.Event | None
        Set once the listeners are bound, so a launcher need not race them.
    """
    from multiprocessing.connection import Listener  # noqa: PLC0415 - lazy, as above

    bound = [
        (
            name,
            Listener(
                (invitation.host, invitation.ports[name]),
                authkey=invitation.authkey,
            ),
        )
        for name in TARGET_NAMES
        if name in targets
    ]
    try:
        for name, listener in bound:
            threading.Thread(
                target=accept_loop,
                args=(listener, name, targets[name], None, stop_event),
                name=f"bridge-accept-{name}",
                daemon=True,
            ).start()
        if ready is not None:
            ready.set()
        stop_event.wait()
    finally:
        for _name, listener in bound:
            with contextlib.suppress(OSError):
                listener.close()


class BridgeHandle:
    """
    A running bridge, for a caller that must not block.

    What a DM script window wants: :func:`start_bridge` returns one of
    these immediately, and the operator keeps using DigitalMicrograph.
    """

    def __init__(
        self,
        thread: threading.Thread,
        ready: threading.Event,
        failures: list[BaseException],
    ) -> None:
        self.thread = thread
        self.ready = ready
        self._failures = failures

    @property
    def failure(self) -> BaseException | None:
        """
        Return what stopped the bridge, if anything did.

        Returns
        -------
        BaseException | None
            The exception the bridge thread ended on — an authentication
            rejection is the one to expect — or ``None`` if it finished
            its sessions normally or is still running.
        """
        return self._failures[0] if self._failures else None

    def wait_ready(self, timeout_s: float = 10.0) -> bool:
        """
        Wait until the bridge is listening or has begun dialling.

        Parameters
        ----------
        timeout_s : float
            Bounded wait.

        Returns
        -------
        bool
            Whether it became ready in time.
        """
        return self.ready.wait(timeout=timeout_s)

    def join(self, timeout_s: float = 10.0) -> None:
        """
        Wait for the bridge thread to finish.

        Parameters
        ----------
        timeout_s : float
            Bounded wait.
        """
        self.thread.join(timeout=timeout_s)


def start_bridge(
    invitation: AttachInvitation,
    **options: object,
) -> BridgeHandle:
    """
    Run :func:`run_bridge` on a background thread and return a handle.

    Parameters
    ----------
    invitation : AttachInvitation
        The rendezvous.
    **options : object
        Passed through to :func:`run_bridge`.

    Returns
    -------
    BridgeHandle
        The running bridge.
    """
    ready = threading.Event()
    failures: list[BaseException] = []

    def _run() -> None:
        # Caught rather than allowed to escape, and recorded rather than
        # only logged. A thread that dies with a traceback inside GMS
        # writes it wherever DigitalMicrograph sends stderr, which on
        # Windows is usually nowhere an operator will look; the caller
        # holding the handle is the one who can say something useful.
        try:
            run_bridge(invitation, ready=ready, **options)  # type: ignore[arg-type]
        except Exception as error:  # noqa: BLE001 - reported through the handle
            failures.append(error)
            _LOGGER.error(  # noqa: TRY400 - the message is the diagnostic
                "bridge stopped: %s: %s",
                type(error).__name__,
                error,
            )
        finally:
            # Whatever happened, nothing is coming: a waiter must not
            # block for its full timeout on a bridge that already failed.
            ready.set()

    thread = threading.Thread(
        target=_run,
        name="miainwoodpecker-gatan-bridge",
        daemon=True,
    )
    thread.start()
    return BridgeHandle(thread, ready, failures)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """
    Parse the bridge's command line.

    Parameters
    ----------
    argv : Sequence[str]
        Arguments after the program name.

    Returns
    -------
    argparse.Namespace
        With ``config``, ``backend``, ``dispersion_ev``, ``sessions`` and
        ``timeout_s``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Serve a Gatan spectrometer to a miainwoodpecker client that "
            "cannot launch it."
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        help="the attach invitation published by the client (JSON)",
    )
    parser.add_argument("--backend", choices=BACKENDS, default=SIMULATED_BACKEND)
    parser.add_argument("--dispersion-ev", type=float, default=_DEFAULT_DISPERSION_EV)
    parser.add_argument("--sessions", type=int, default=1)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the bridge from a command line.

    Present for the simulated backend, for CI, and for an operator who
    would rather run a script than paste into DM's Python window. Inside
    GMS the import route is the one to use — see the module docstring —
    partly because the embedded interpreter may not define ``sys.argv``
    at all, which is why this reads it defensively.

    Parameters
    ----------
    argv : Sequence[str] | None
        Arguments after the program name, or ``None`` to read
        ``sys.argv`` if there is one.

    Returns
    -------
    int
        Process exit status.
    """
    from miainwoodpecker.devices.remote import (  # noqa: PLC0415 - avoids a cycle
        AttachInvitation,
    )

    logging.basicConfig(
        level=os.environ.get("MIAINWOODPECKER_DEVICE_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    arguments = _parse_args(
        list(getattr(sys, "argv", ["gatan_bridge"]))[1:] if argv is None else argv,
    )
    run_bridge(
        AttachInvitation.read_from(arguments.config),
        backend=arguments.backend,
        dispersion_ev=arguments.dispersion_ev,
        sessions=arguments.sessions,
        timeout_s=arguments.timeout_s,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
