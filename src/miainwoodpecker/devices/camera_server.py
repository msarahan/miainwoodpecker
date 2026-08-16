"""
A device server for commodity cameras: USB microscopes, webcams, files.

The counterpart to :mod:`miainwoodpecker.devices.nion_server`, for the
other end of the market. A UVC microscope or a webcam is a real
instrument for alignment, teaching, documentation and plenty of light
microscopy, and it is also the cheapest way to exercise this whole stack
against *hardware* rather than a simulator — one USB device and the
client, the transport, the session layer, the storage and the viewer are
all running for real.

Run it the way any adapter is run::

    remote_instrument(server_module="miainwoodpecker.devices.camera_server")

MIT throughout, with no vendor SDK: the hardware backend uses OpenCV's
``VideoCapture``, which speaks V4L2 on Linux, AVFoundation on macOS and
DirectShow/MSMF on Windows. That covers essentially every USB microscope,
because they are all UVC devices pretending to be webcams.

Two backends, for the same reason ``nion_server`` has two
---------------------------------------------------------
``simulated`` synthesises frames and needs **nothing installed** — not
OpenCV, not a camera. It exists so the whole path is exercisable in CI
and on a laptop with nothing plugged in, exactly as ``nionswift-usim``
does for the Nion side. ``hardware`` opens a real capture device.

Which device, and how it is named
---------------------------------
``--plugin`` is the protocol's server-specific configuration channel (see
:data:`~miainwoodpecker.devices.remote.DEFAULT_SERVER_MODULE`), so this
server reads it as *what to open*: an index (``"0"``), a device path
(``"/dev/video0"``), or a media file. A file is not a curiosity — it is
how a capture is replayed, which makes a recorded session into a
regression fixture.

**Repeat it for each camera.** A laptop with a built-in webcam and a USB
microscope plugged in has two cameras, and wanting the microscope
without unplugging the webcam is the ordinary case rather than an exotic
one::

    --plugin 0 --plugin 1

Each opens on its own target — ``camera``, then ``camera:2``,
``camera:3`` — and all of them are live at once. The first keeps the
plain ``camera`` name that every existing recording and every existing
test already uses. The number is a **slot**, not an identity: which
physical device is behind it is in ``device_id`` on every frame and in
the endpoint label, because the order the devices were named on the
command line is not something a stored file should depend on.

Targets past the first are names the *client* cannot know in advance, so
it cannot allocate their ports. They bind on an OS-assigned port instead
and the server reports where, through ``describe()``'s ``endpoints``
map. See docs/vendor-support.md, "The target names are a fixed tuple".

Cameras are served on the neutral ``camera`` family rather than
``ronchigram_camera``, because calling a USB microscope a Ronchigram
camera would put a fiction in every file.

What honesty costs here, and why it is worth paying
----------------------------------------------------
A consumer camera is not a scientific detector, and three of the
differences are the difference between an image and a measurement. This
server states all three in the frame metadata rather than hiding them:

- **The data is not photometrically linear.** A UVC camera delivers
  MJPEG or YUY2 that has already been through demosaicing, gamma, white
  balance and sharpening inside the camera, and none of that is
  recoverable. Every frame therefore carries
  ``photometrically_linear: False``, so an analysis step can refuse to
  treat it as counts. A scientific detector's adapter would set it
  ``True`` and mean it.
- **Colour is discarded, not averaged into significance.**
  :class:`~miainwoodpecker.devices.interface.Frame` data is 2D by
  design; the camera has already demosaiced, so there is no raw Bayer
  plane left to deliver. The conversion actually applied is named in
  ``colour_conversion`` rather than left for a reader to guess.
- **Exposure and gain may not be settable at all.** UVC exposes them
  through a driver-dependent auto/manual dance that many cheap devices
  implement badly or not at all. ``configure`` reports what the device
  *took*, which is the existing contract and is doing real work here:
  asking for 10 ms and being given whatever auto-exposure decided is the
  normal case, not an error.

Binning is ``[1]``. Consumer sensors crop; they do not bin.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
import threading
import typing

import numpy as np

from miainwoodpecker.devices.interface import (
    IMAGE_READOUT,
    CameraParameters,
    Frame,
)
from miainwoodpecker.devices.rpc import (
    BACKENDS,
    INSTRUMENT_TARGET,
    SIMULATED_BACKEND,
)
from miainwoodpecker.devices.serving import accept_loop, bind_targets, endpoint_map
from miainwoodpecker.devices.shared_frame import SharedFrameWriter

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

_LOGGER = logging.getLogger("miainwoodpecker.devices.camera_server")

# Exit status for "the requested capture device could not be opened",
# distinct from a crash so a launcher can tell "no camera plugged in"
# from "the adapter is broken". Mirrors nion_server's NO_HARDWARE.
NO_CAMERA_EXIT_STATUS = 2
# Shared with the client, which retries these with fresh ports.
PORT_UNAVAILABLE_EXIT_STATUS = 4

CAMERA_TARGET = "camera"
"""The neutral target this server serves its camera on."""

_DEFAULT_DEVICE = "0"
_SIMULATED_SHAPE = (480, 640)
_DEFAULT_EXPOSURE_MS = 33.0


class CameraOpenError(RuntimeError):
    """Raised when the requested capture device cannot be opened."""


def _now() -> datetime.datetime:
    """Return the current time, timezone-aware, as every Frame requires."""
    return datetime.datetime.now(tz=datetime.UTC)


class SimulatedCamera:
    """
    A camera that synthesises frames, so the path runs with nothing plugged in.

    Deliberately not a still image: successive frames differ, and they
    differ *everywhere*, so the frame-identity contracts ported from
    Nion's suite (a held frame must not change; consecutive frames must
    not be identical) are meaningful against it rather than vacuously
    true.
    """

    def __init__(self, shape: tuple[int, int] = _SIMULATED_SHAPE) -> None:
        self._shape = shape
        self._parameters = CameraParameters(exposure_ms=_DEFAULT_EXPOSURE_MS)
        self._frame_index = 0
        self._running = False
        self._closed = False
        self._generator = np.random.default_rng()

    @property
    def camera_id(self) -> str:
        """Return this camera's stable identifier."""
        return "simulated_camera"

    @property
    def binning_values(self) -> Sequence[int]:
        """Return the binning factors supported: one, like any consumer sensor."""
        return [1]

    def parameters(self) -> CameraParameters:
        """Return the settings the next frame will use."""
        return self._parameters

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Accept new settings verbatim; a simulator rounds nothing.

        Parameters
        ----------
        parameters : CameraParameters
            The requested exposure and binning.

        Returns
        -------
        CameraParameters
            The same settings.

        Raises
        ------
        ValueError
            If a binning other than 1 is requested, which is what a
            consumer sensor would also say, or a readout other than
            ``image``.
        """
        if parameters.binning != 1:
            msg = (
                f"binning {parameters.binning} is not supported by "
                f"{self.camera_id}; consumer sensors crop rather than bin"
            )
            raise ValueError(msg)
        if parameters.readout != IMAGE_READOUT:
            msg = (
                f"readout {parameters.readout!r} is not supported by "
                f"{self.camera_id}: a consumer sensor delivers images and has "
                f"no dispersive axis to project along"
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
        Return a synthesised frame with a moving feature and noise.

        Returns
        -------
        Frame
            An 8-bit greyscale frame, as a UVC device would deliver.
        """
        height, width = self._shape
        column = np.linspace(0.0, 1.0, width, dtype=np.float32)
        row = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
        # A gradient that drifts frame to frame, so successive frames
        # differ structurally and not only by noise.
        phase = (self._frame_index % 64) / 64.0
        image = 128.0 + 96.0 * np.sin(2 * np.pi * (column + row + phase))
        image = image + self._generator.normal(0.0, 2.0, self._shape)
        data = np.clip(image, 0, 255).astype(np.uint8)
        metadata = _common_metadata(self, self._frame_index)
        metadata["camera_name"] = "simulated commodity camera"
        self._frame_index += 1
        return Frame(data=data, timestamp=_now(), metadata=metadata)

    def close(self) -> None:
        """Release nothing; idempotent, like every adapter's close."""
        self._closed = True


class OpenCVCamera:
    """
    A camera backed by OpenCV's ``VideoCapture``.

    Covers USB microscopes and webcams (all UVC devices), and media files
    — which is how a capture is replayed as a fixture. OpenCV is imported
    lazily so this module still imports, and the simulated backend still
    runs, without the ``camera`` extra installed.
    """

    def __init__(self, device: str) -> None:
        self._device = device
        self._capture = _open_capture(device)
        self._parameters = CameraParameters(
            exposure_ms=self._read_exposure_ms() or _DEFAULT_EXPOSURE_MS,
        )
        self._frame_index = 0
        self._closed = False
        self._lock = threading.Lock()

    @property
    def camera_id(self) -> str:
        """Return an identifier naming what was actually opened."""
        return f"opencv:{self._device}"

    @property
    def binning_values(self) -> Sequence[int]:
        """Return the binning factors supported: one, on any consumer sensor."""
        return [1]

    def parameters(self) -> CameraParameters:
        """Return the settings the next frame will use, re-read from the device."""
        exposure_ms = self._read_exposure_ms()
        if exposure_ms is not None:
            self._parameters = CameraParameters(exposure_ms=exposure_ms)
        return self._parameters

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Ask the device for these settings and report what it took.

        The report is doing real work on this class of hardware. UVC
        exposure is a driver-dependent auto/manual dance that many cheap
        devices implement partially or not at all, so "asked for 10 ms,
        got whatever auto-exposure decided" is the normal outcome rather
        than an error — and a caller that assumed its request took would
        record the wrong exposure on every frame.

        Parameters
        ----------
        parameters : CameraParameters
            The requested exposure and binning.

        Returns
        -------
        CameraParameters
            What the device reports afterwards.

        Raises
        ------
        ValueError
            If a binning other than 1, or a readout other than
            ``image``, is requested.
        """
        if parameters.binning != 1:
            msg = (
                f"binning {parameters.binning} is not supported by "
                f"{self.camera_id}; consumer sensors crop rather than bin"
            )
            raise ValueError(msg)
        if parameters.readout != IMAGE_READOUT:
            msg = (
                f"readout {parameters.readout!r} is not supported by "
                f"{self.camera_id}: a capture device delivers images and has "
                f"no dispersive axis to project along"
            )
            raise ValueError(msg)
        import cv2  # noqa: PLC0415 - lazy, see the class docstring

        with self._lock:
            # Many UVC drivers ignore CAP_PROP_EXPOSURE until auto
            # exposure is switched off, and the magic value for "manual"
            # is itself driver-specific. Both writes are attempted and
            # neither is required to succeed; the read-back is the truth.
            self._capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
            self._capture.set(cv2.CAP_PROP_EXPOSURE, parameters.exposure_ms / 1000.0)
        return self.parameters()

    def start(self) -> None:
        """Begin acquisition; a capture device is already streaming."""

    def stop(self) -> None:
        """Pause acquisition; nothing to do for a capture device."""

    def acquire_frame(self) -> Frame:
        """
        Read one frame, convert it to 2D, and say what was done to it.

        Returns
        -------
        Frame
            A 2D frame, greyscale-converted if the device delivered
            colour, with the conversion named in its metadata.

        Raises
        ------
        CameraOpenError
            If the device returned no frame — usually an unplugged USB
            camera, or a media file that has run out.
        """
        import cv2  # noqa: PLC0415 - lazy, see the class docstring

        with self._lock:
            ok, raw = self._capture.read()
        if not ok or raw is None:
            msg = (
                f"{self.camera_id} returned no frame; the device may have "
                f"been unplugged, or a media file has reached its end"
            )
            raise CameraOpenError(msg)
        metadata = _common_metadata(self, self._frame_index)
        metadata["camera_name"] = self._device
        if raw.ndim == 3:  # noqa: PLR2004 - height, width, channels
            # OpenCV hands back BGR that the camera already demosaiced.
            # Frame data is 2D; there is no raw Bayer plane left to
            # deliver, so the conversion is named rather than implied.
            data = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
            metadata["colour_conversion"] = "bgr_to_grey"
        else:
            data = raw
            metadata["colour_conversion"] = "none"
        self._frame_index += 1
        return Frame(data=data, timestamp=_now(), metadata=metadata)

    def close(self) -> None:
        """Release the capture device; idempotent."""
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self._capture.release()

    def _read_exposure_ms(self) -> float | None:
        """Read the device's exposure in milliseconds, or None if it will not say."""
        import cv2  # noqa: PLC0415 - lazy, see the class docstring

        value = self._capture.get(cv2.CAP_PROP_EXPOSURE)
        if not value or value <= 0:
            # 0 and negative are both "not reported" in practice: V4L2
            # returns 0 for a device with no manual exposure, and some
            # backends return -1 rather than failing.
            return None
        return float(value) * 1000.0


def _common_metadata(camera: object, frame_index: int) -> dict[str, object]:
    """
    Return the metadata every frame from this server carries.

    Parameters
    ----------
    camera : object
        The camera producing the frame, for its id.
    frame_index : int
        Frames produced since the device was opened, from 0.

    Returns
    -------
    dict[str, object]
        The vocabulary documented on
        :class:`~miainwoodpecker.devices.interface.Frame`, plus the two
        honesty flags this class of hardware needs.
    """
    return {
        "device_id": camera.camera_id,
        "frame_index": frame_index,
        "exposure_ms": camera.parameters().exposure_ms,
        "binning": 1,
        "camera_type": "commodity",
        # The flag that stops someone treating gamma-corrected,
        # white-balanced, in-camera-sharpened pixels as counts. A
        # scientific detector's adapter would set this True and mean it.
        "photometrically_linear": False,
    }


def _open_capture(device: str) -> typing.Any:  # noqa: ANN401 - cv2.VideoCapture
    """
    Open a capture device by index, path, or file name.

    Parameters
    ----------
    device : str
        A camera index (``"0"``), a device path (``"/dev/video0"``), or a
        media file to replay.

    Returns
    -------
    typing.Any
        An opened ``cv2.VideoCapture``.

    Raises
    ------
    CameraOpenError
        If OpenCV is not installed, or the device will not open.
    """
    try:
        import cv2  # noqa: PLC0415 - lazy, so the base package needs no OpenCV
    except ImportError as error:
        msg = (
            "the hardware backend needs OpenCV, which is not installed. "
            "Install this project's 'camera' extra, or use "
            f"--backend {SIMULATED_BACKEND} to run without a device."
        )
        raise CameraOpenError(msg) from error
    target: int | str = int(device) if device.isdigit() else device
    capture = cv2.VideoCapture(target)
    if not capture.isOpened():
        msg = (
            f"could not open capture device {device!r}. {_open_failure_hint()} "
            f"Pass the device with --plugin, and use "
            f"--backend {SIMULATED_BACKEND} to run without one."
        )
        raise CameraOpenError(msg)
    return capture


def _open_failure_hint() -> str:
    """
    Return the platform's most likely reason a capture device would not open.

    Worth the branch because the three platforms fail for entirely
    different reasons, and the macOS one is genuinely obscure: camera
    access is granted per *responsible process*, which for a server
    launched from a shell is the terminal application rather than
    Python, so the permission dialog may never appear and the failure
    looks like absent hardware.

    Returns
    -------
    str
        A sentence naming what to check on this platform.
    """
    if sys.platform == "darwin":
        return (
            "On macOS, camera access is granted to the application "
            "*responsible* for the process - your terminal (Terminal, "
            "iTerm, VS Code), not Python - so check System Settings > "
            "Privacy & Security > Camera and confirm it is listed and "
            "enabled. A denied camera can also open successfully and "
            "then deliver black frames, so check the pixel values too. "
            "The built-in camera is index 0; a Continuity Camera iPhone "
            "usually appears alongside it."
        )
    if sys.platform == "win32":
        return (
            "On Windows, check Settings > Privacy & security > Camera, "
            "and that no other application currently holds the device - "
            "DirectShow admits one consumer at a time."
        )
    return (
        "On Linux, check that /dev/video* exists and this user can read "
        "it (usually membership of the 'video' group); a camera index is "
        "the number in that path."
    )


class ServerInstrument:
    """
    The ``instrument`` target for a camera that has no microscope around it.

    Every server needs this target — the client connects to it first and
    asks what else exists — but a webcam has no stage, no defocus and no
    blanker. Reporting an empty control list is the supported answer, and
    ``park`` doing nothing is the honest one: there is no beam to blank.
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
        self._endpoints: dict[str, dict[str, object]] = {}

    def publish_endpoints(self, endpoints: dict[str, dict[str, object]]) -> None:
        """
        Record where each target is listening, for ``describe`` to report.

        Set after the listeners bind rather than passed to ``__init__``,
        because a target bound on an OS-assigned port does not know its
        own port until it has one.

        Parameters
        ----------
        endpoints : dict[str, dict[str, object]]
            Target name to its ``port``, ``kind`` and ``label``.
        """
        self._endpoints = endpoints

    def stage_size_nm(self) -> float:
        """
        Return a nominal extent, since there is no stage to ask.

        Returns
        -------
        float
            One micrometre, the same documented stand-in ``nion_server``
            uses when a controller publishes no stage size.
        """
        return 1000.0

    def available_controls(self) -> Sequence[str]:
        """Return no controls; a commodity camera has none of them."""
        return []

    def park(self) -> None:
        """Do nothing: there is no beam to blank and no column to park."""

    def describe(self) -> dict[str, object]:
        """
        Report what this server serves, as plain data.

        Returns
        -------
        dict[str, object]
            ``backend``, ``targets``, ``controls``, ``stage_size_nm``
            and ``endpoints`` — the last naming the port, kind and
            label of every target, so a client can dial targets whose
            names it does not know in advance.
        """
        return {
            "backend": self._backend,
            "targets": [name for name in self._targets if name != "instrument"],
            "controls": [],
            "stage_size_nm": self.stage_size_nm(),
            "endpoints": self._endpoints,
        }

    def health(self) -> dict[str, object]:
        """
        Answer "is this server alive?" without touching the camera.

        Returns
        -------
        dict[str, object]
            The same shape ``nion_server`` reports, so one client reads
            both.
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
                if name != "instrument" and not getattr(target, "_closed", False)
            ],
            "shutting_down": self._shutting_down,
        }

    def shutdown(self) -> dict[str, object]:
        """
        Release the camera and let the server exit.

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


def open_camera(backend: str, device: str) -> SimulatedCamera | OpenCVCamera:
    """
    Build the camera the requested backend describes.

    Parameters
    ----------
    backend : str
        ``"simulated"`` or ``"hardware"``.
    device : str
        What the hardware backend should open; ignored when simulated.

    Returns
    -------
    SimulatedCamera | OpenCVCamera
        The camera to serve.

    Raises
    ------
    ValueError
        If ``backend`` is not one this server accepts.
    """
    if backend not in BACKENDS:
        msg = f"unknown backend {backend!r}; expected one of {', '.join(BACKENDS)}"
        raise ValueError(msg)
    if backend == SIMULATED_BACKEND:
        return SimulatedCamera()
    return OpenCVCamera(device)


def camera_target_name(index: int) -> str:
    """
    Return the target name for the *index*-th camera this server opens.

    The first keeps the plain ``camera`` name every existing recording
    and every existing test already uses; the rest are numbered from two,
    which is how an operator refers to them out loud. The number is a
    **slot**, not an identity — which physical device is behind it is in
    ``device_id`` on every frame, and in the endpoint's label, because
    the order the devices were named on the command line is not
    something a file should depend on.

    Parameters
    ----------
    index : int
        Zero-based position in the served list.

    Returns
    -------
    str
        ``"camera"`` for the first, ``"camera:2"``, ``"camera:3"``, … after.
    """
    return CAMERA_TARGET if index == 0 else f"{CAMERA_TARGET}:{index + 1}"


def serve(
    ports: typing.Mapping[str, int],
    authkey: bytes,
    *,
    backend: str = SIMULATED_BACKEND,
    devices: Sequence[str] = (_DEFAULT_DEVICE,),
) -> None:
    """
    Open every requested camera and serve until a client shuts the server down.

    More than one camera is an ordinary case rather than an exotic one:
    a laptop with a built-in webcam and a USB microscope plugged in has
    two, and an operator wants the microscope without unplugging
    anything. Each opens on its own target, so both are live at once.

    Parameters
    ----------
    ports : typing.Mapping[str, int]
        Ports the client allocated, by target name. Targets it could not
        name in advance — every camera after the first — bind on an
        OS-assigned port and are reported through ``describe``.
    authkey : bytes
        Shared secret for every Listener.
    backend : str
        ``"simulated"`` or ``"hardware"``.
    devices : Sequence[str]
        What the hardware backend opens, one camera per entry.

    Raises
    ------
    SystemExit
        With :data:`PORT_UNAVAILABLE_EXIT_STATUS` when a listener cannot
        bind, so the client retries with fresh ports.
    """
    stop_event = threading.Event()
    targets: dict[str, object] = {}
    writers: dict[str, SharedFrameWriter | None] = {}
    labels: dict[str, str] = {}
    for index, device in enumerate(devices):
        name = camera_target_name(index)
        camera = open_camera(backend, device)
        targets[name] = camera
        writers[name] = SharedFrameWriter()
        labels[name] = camera.camera_id
    instrument = ServerInstrument(targets, stop_event, backend)
    targets["instrument"] = instrument
    writers["instrument"] = None

    names = list(targets)
    try:
        listeners, bound = bind_targets(names, ports, authkey)
    except OSError as error:
        _LOGGER.error(  # noqa: TRY400 - a traceback adds nothing here
            "could not bind a listener (%s); the client will retry", error,
        )
        raise SystemExit(PORT_UNAVAILABLE_EXIT_STATUS) from error
    instrument.publish_endpoints(endpoint_map(bound, labels))

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
    Parse the server's command line, matching ``nion_server``'s shape.

    Parameters
    ----------
    argv : Sequence[str]
        Arguments after the program name.

    Returns
    -------
    argparse.Namespace
        With ``backend``, ``plugin`` and ``instrument_port``.
    """
    parser = argparse.ArgumentParser(
        description="Serve a commodity camera over the miainwoodpecker protocol.",
    )
    parser.add_argument("--backend", choices=BACKENDS, default=SIMULATED_BACKEND)
    parser.add_argument(
        "--plugin",
        action="append",
        default=None,
        help=(
            "what the hardware backend opens: a camera index (0), a device "
            "path (/dev/video0), or a media file to replay"
        ),
    )
    parser.add_argument("--enable-test-hooks", action="store_true")
    parser.add_argument(
        "--instrument-port",
        type=int,
        required=True,
        help=(
            "the one port the client allocates; every other target binds "
            "an OS-assigned port and is reported through describe()"
        ),
    )
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
    logging.basicConfig(
        level=os.environ.get("MIAINWOODPECKER_DEVICE_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    ports = {INSTRUMENT_TARGET: arguments.instrument_port}
    authkey = bytes.fromhex(os.environ["MIAINWOODPECKER_AUTHKEY"])
    devices = list(arguments.plugin) if arguments.plugin else [_DEFAULT_DEVICE]
    try:
        serve(ports, authkey, backend=arguments.backend, devices=devices)
    except CameraOpenError as error:
        _LOGGER.error("%s", error)  # noqa: TRY400 - the message is the diagnostic
        return NO_CAMERA_EXIT_STATUS
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
