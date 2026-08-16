"""
A device server for DECTRIS hybrid-pixel detectors over the SIMPLON API.

The third in-tree adapter, after :mod:`~miainwoodpecker.devices.nion_server`
(a column vendor) and :mod:`~miainwoodpecker.devices.camera_server` (a
commodity sensor). This one is a *scientific detector with no column
around it* — the shape `docs/vendor-support.md` predicted and
``tests/unit/test_out_of_tree_server.py`` made work: a `Camera` and
nothing else, served on the neutral ``camera`` target.

Run it the way any adapter is run::

    remote_instrument(
        server_module="miainwoodpecker.devices.dectris_server",
        backend="hardware",
        plugin_names=["10.42.0.10"],   # the DCU's address
    )

MIT throughout, with no vendor library: SIMPLON is an HTTP/JSON interface
served by the detector control unit itself, so the control path here is
:mod:`urllib.request` and :mod:`json` from the standard library. Nothing
is installed on the DCU and nothing proprietary is installed here.

Which detectors, and what was checked
-------------------------------------
Written against the ELA at SuperSTEM, but the SIMPLON surface used is the
common one across DECTRIS's EIGER2-chip-based electron detectors (ARINA,
ELA, QUADRO, SINGLA). ``docs/adapters/dectris.md`` records the sources and
the parts that are still unverified against real hardware.

The one claim worth repeating here, because an adapter that cannot connect
is worse than no adapter: the ELA **is** directly reachable over SIMPLON.
Its integration into Gatan Microscopy Suite — where it is sold as the
Stela camera — is a second front end onto the same detector, not the only
way in. Plotkin-Swing et al. (*Ultramicroscopy* 217, 113067, 2020), the
Nion/DECTRIS paper characterising the ELA prototype on a Nion IRIS
spectrometer, records data being taken "through the DECTRIS Simplon API"
and streamed into Nion Swift with no Gatan software anywhere.

Where this sits relative to LiberTEM-live
-----------------------------------------
Deliberately on the *other* side of the line `docs/vendor-support.md`
draws in "Do not re-plumb high-rate streaming". An ELA runs to 2250 full
frames per second and past 10 kHz on a narrow readout, which is not a rate
a synchronous ``acquire_frame()`` should pretend to serve.

SIMPLON itself makes the split easy, because it publishes two ways to get
images out and they are not the same thing:

- the **ZeroMQ stream** subsystem, which pushes every frame of a series as
  an LZ4/bitshuffle-compressed blob and is the recording path. That is
  what LiberTEM-live's Rust receiver already consumes, and this adapter
  does not touch it.
- the **monitor** subsystem, an HTTP endpoint that hands back the most
  recent image as a TIFF and is explicitly a *preview* channel: it drops
  frames when a client does not keep up, by design.

The monitor subsystem is a pull-per-frame interface, which is exactly what
:class:`~miainwoodpecker.devices.interface.Camera` is, so this adapter is
built on it and is honest about being the survey-rate path: configure the
detector, set exposure, look at what the detector sees, take single shots
and focal series. A 4D-STEM acquisition hands the detector to LiberTEM-live
instead. They must not both own it at once — SIMPLON accepts configuration
from anyone but only one armed series at a time — and the open-time state
check below is this adapter's half of that interlock.

Two backends, for the same reason the other servers have two
------------------------------------------------------------
``simulated`` needs **nothing installed and no detector**: it starts a
small HTTP server on localhost that implements the documented SIMPLON
resources — the detector, monitor and stream configuration trees, the
``arm``/``trigger``/``disarm`` command endpoints, the armed-state machine
that makes configuration read-only, and monitor images as real TIFF bytes
— and then points the *same* client at it. That is the point of doing it
this way rather than with a stub camera object: every request the hardware
backend makes is a request the tests make, parsed by something that can
refuse it. ``hardware`` points that identical client at a real DCU.

Install the ``dectris`` extra for the hardware backend. The only thing it
buys is ``tifffile``, for the monitor image: a DCU is free to send any
baseline-TIFF variant it likes, and guessing wrong about compression or
byte order silently reinterprets counts. The simulated backend's own TIFFs
are plain uncompressed strips, which this module decodes itself, so CI
needs nothing.

What honesty costs here, and what it buys
-----------------------------------------
The counterpart to ``camera_server``'s three warnings, and it is worth
saying that they come out the *other* way, because this is a detector
rather than a picture-taking device:

- **The data is photometrically linear, and that is a real claim.** Every
  frame carries ``photometrically_linear: True``. A hybrid-pixel detector
  discriminates each arriving electron against a threshold and increments
  an integer; there is no charge integration, no gain curve, no gamma, no
  demosaic. See :func:`_frame_metadata` for the one caveat that keeps this
  from being unconditional.
- **``counts_per_electron`` is 1.0, and it is measured rather than
  nominal.** That is what counting means.
- **Binning is ``[1]``, for a different reason than a webcam's.** A
  consumer sensor crops instead of binning; a counting pixel array
  *cannot* bin, because there is no charge to sum — each pixel owns its
  own discriminator and counter. Summing neighbours on the host would
  produce a smaller array with the same total counts and a calibration
  scale that no longer matches the sensor, which is a fiction this adapter
  declines to tell. ROI (``roi_mode``) is the readout reduction a DECTRIS
  detector really has, and ``CameraParameters`` has nowhere to put it yet.
- **No ``calibration`` is published**, and its absence is the honest
  answer rather than an omission. The detector reports its physical pixel
  pitch (75 µm on an ELA) and nothing else; turning that into a
  sample-plane, reciprocal or energy axis needs the camera length or the
  spectrometer dispersion, which live in the microscope and not in the
  DCU. The pitch is recorded as ``x_pixel_size_m``/``y_pixel_size_m`` so a
  caller who *does* know the optics can finish the job.
"""

from __future__ import annotations

import argparse
import datetime
import http.server
import json
import logging
import os
import socket
import socketserver
import struct
import sys
import threading
import time
import typing
import urllib.error
import urllib.request
from collections import deque

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

_LOGGER = logging.getLogger("miainwoodpecker.devices.dectris_server")

# Exit status for "the detector control unit could not be reached or is
# not usable", distinct from a crash so a launcher can tell "wrong IP" or
# "someone else has the detector" from "the adapter is broken". Mirrors
# camera_server's NO_CAMERA_EXIT_STATUS and nion_server's NO_HARDWARE.
NO_DETECTOR_EXIT_STATUS = 2
# Shared with the client, which retries these with fresh ports.
PORT_UNAVAILABLE_EXIT_STATUS = 4

CAMERA_TARGET = "camera"
"""
The neutral target this server serves its detector on.

Not ``eels_camera``, even though an ELA on an IRIS spectrometer usually
*is* one: the target name would then be asserting a microscope
configuration this adapter cannot see. The detector's own labels go in the
frame metadata, where they are what the DCU actually said.
"""

SIMPLON_API_VERSION = "1.8.0"
"""
The SIMPLON API version this adapter speaks, as it appears in every URL.

Part of the path rather than a header — ``/detector/api/1.8.0/config/...``
— so a DCU on a different version answers 404 rather than misbehaving,
which is why :func:`_open_failure_hint` treats a 404 on the identity read
as a version mismatch rather than a missing detector.
"""

_DEFAULT_ADDRESS = "localhost"
_DEFAULT_HTTP_PORT = 80
_DEFAULT_EXPOSURE_MS = 10.0
# The ELA's full frame: eight 256x256 readout chips of 75 um pixels, in a
# 4x2 arrangement, plus the inter-chip gaps the DCU reports as part of the
# image. Used only by the simulated backend; a real detector is asked.
_SIMULATED_SHAPE = (514, 1030)
_ELA_PIXEL_PITCH_M = 75e-6
# How many software triggers one arm is good for. A pull-per-frame client
# has no idea how many frames it will want, and re-arming costs a round
# trip, so the series is made long enough that re-arming is rare and
# bounded enough that a forgotten `stop()` does not leave the detector
# armed indefinitely.
_TRIGGERS_PER_ARM = 65536
_DEFAULT_TIMEOUT_S = 10.0
# How long acquire_frame() waits for the monitor buffer to produce the
# image its own trigger just asked for, over and above the exposure.
_MONITOR_GRACE_S = 5.0
_MONITOR_POLL_S = 0.002

_OK = 200
_BAD_REQUEST = 400
_FORBIDDEN = 403
_NOT_FOUND = 404
_REQUEST_TIMEOUT = 408

# SIMPLON detector states, in the order a client walks through them.
_STATE_NA = "na"
_STATE_IDLE = "idle"
_STATE_READY = "ready"
_STATE_ACQUIRE = "acquire"
_STATE_ERROR = "error"


class DetectorOpenError(RuntimeError):
    """Raised when the detector control unit cannot be reached or used."""


class SimplonError(RuntimeError):
    """
    Raised when a SIMPLON request could not be completed.

    Carries ``status``, the HTTP code the control unit answered with (or
    ``None`` when the request never reached one), because one of those
    codes is not an error at all: the monitor endpoint answers 408 for "no
    image buffered yet", which is the ordinary state of a preview channel
    and must be told apart from a detector that has gone away.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _now() -> datetime.datetime:
    """Return the current time, timezone-aware, as every Frame requires."""
    return datetime.datetime.now(tz=datetime.UTC)


# --------------------------------------------------------------------------
# Baseline TIFF, because a monitor image is a TIFF
# --------------------------------------------------------------------------
#
# SIMPLON's monitor subsystem returns images as TIFF rather than as raw
# arrays, which is a real part of the protocol and therefore a real part of
# the mock. Both directions are here: the fake DCU writes one, the client
# reads one. The reader is deliberately narrow - single-sample unsigned
# greyscale, uncompressed strips - and says so rather than guessing, which
# is why the hardware backend requires tifffile.

_TIFF_LITTLE_ENDIAN = b"II"
_TIFF_BIG_ENDIAN = b"MM"
_TIFF_MAGIC = 42
_TIFF_TAG_IMAGE_WIDTH = 256
_TIFF_TAG_IMAGE_LENGTH = 257
_TIFF_TAG_BITS_PER_SAMPLE = 258
_TIFF_TAG_COMPRESSION = 259
_TIFF_TAG_PHOTOMETRIC = 262
_TIFF_TAG_STRIP_OFFSETS = 273
_TIFF_TAG_SAMPLES_PER_PIXEL = 277
_TIFF_TAG_ROWS_PER_STRIP = 278
_TIFF_TAG_STRIP_BYTE_COUNTS = 279
_TIFF_TAG_SAMPLE_FORMAT = 339
_TIFF_TYPE_SHORT = 3
_TIFF_TYPE_LONG = 4
_TIFF_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}
_TIFF_COMPRESSION_NONE = 1
_TIFF_SAMPLE_FORMAT_UINT = 1
_TIFF_HEADER_SIZE = 8
_TIFF_ENTRY_SIZE = 12
_TIFF_INLINE_VALUE_SIZE = 4

_TIFF_DTYPES = {8: np.uint8, 16: np.uint16, 32: np.uint32}


def encode_tiff(array: np.ndarray) -> bytes:
    """
    Encode a 2D unsigned integer array as an uncompressed baseline TIFF.

    The mock DCU's half of the monitor endpoint. Little-endian, one strip,
    one sample per pixel — the least surprising thing a TIFF reader can be
    handed, so that a decode failure in a test means the *client* is wrong.

    Parameters
    ----------
    array : np.ndarray
        A 2D array of ``uint8``, ``uint16`` or ``uint32``.

    Returns
    -------
    bytes
        A complete TIFF file.

    Raises
    ------
    ValueError
        If the array is not 2D, or its dtype has no TIFF encoding here.
    """
    expected_ndim = 2
    if array.ndim != expected_ndim:
        msg = f"a monitor image is 2D; got shape {array.shape!r}"
        raise ValueError(msg)
    bits = {np.uint8: 8, np.uint16: 16, np.uint32: 32}.get(array.dtype.type)
    if bits is None:
        msg = f"no baseline TIFF encoding for dtype {array.dtype!r}"
        raise ValueError(msg)
    height, width = array.shape
    little_endian = np.ascontiguousarray(array).astype(array.dtype.newbyteorder("<"))
    payload = little_endian.tobytes()
    entries = [
        (_TIFF_TAG_IMAGE_WIDTH, _TIFF_TYPE_LONG, width),
        (_TIFF_TAG_IMAGE_LENGTH, _TIFF_TYPE_LONG, height),
        (_TIFF_TAG_BITS_PER_SAMPLE, _TIFF_TYPE_SHORT, bits),
        (_TIFF_TAG_COMPRESSION, _TIFF_TYPE_SHORT, _TIFF_COMPRESSION_NONE),
        (_TIFF_TAG_PHOTOMETRIC, _TIFF_TYPE_SHORT, 1),
        (_TIFF_TAG_STRIP_OFFSETS, _TIFF_TYPE_LONG, 0),  # patched below
        (_TIFF_TAG_SAMPLES_PER_PIXEL, _TIFF_TYPE_SHORT, 1),
        (_TIFF_TAG_ROWS_PER_STRIP, _TIFF_TYPE_LONG, height),
        (_TIFF_TAG_STRIP_BYTE_COUNTS, _TIFF_TYPE_LONG, len(payload)),
        (_TIFF_TAG_SAMPLE_FORMAT, _TIFF_TYPE_SHORT, _TIFF_SAMPLE_FORMAT_UINT),
    ]
    directory_size = 2 + _TIFF_ENTRY_SIZE * len(entries) + 4
    data_offset = _TIFF_HEADER_SIZE + directory_size
    entries = [
        (tag, kind, data_offset if tag == _TIFF_TAG_STRIP_OFFSETS else value)
        for tag, kind, value in entries
    ]
    directory = struct.pack("<H", len(entries))
    for tag, kind, value in entries:
        packed = (
            struct.pack("<HH", value, 0)
            if kind == _TIFF_TYPE_SHORT
            else struct.pack("<I", value)
        )
        directory += struct.pack("<HHI", tag, kind, 1) + packed
    directory += struct.pack("<I", 0)  # no next directory
    header = _TIFF_LITTLE_ENDIAN + struct.pack("<HI", _TIFF_MAGIC, _TIFF_HEADER_SIZE)
    return header + directory + payload


def decode_tiff(payload: bytes) -> np.ndarray:
    """
    Decode a monitor image, preferring ``tifffile`` when it is installed.

    The fallback is not a convenience: it is what lets the simulated
    backend exercise the whole HTTP-and-TIFF path in an environment with
    only numpy. When ``tifffile`` *is* present it wins, because a real DCU
    may send a variant the narrow reader here would have to refuse.

    Parameters
    ----------
    payload : bytes
        The bytes of a TIFF file, as the monitor endpoint returns them.

    Returns
    -------
    np.ndarray
        The 2D image.
    """
    try:
        import tifffile  # noqa: PLC0415 - optional, see the module docstring
    except ImportError:
        return _decode_baseline_tiff(payload)
    import io  # noqa: PLC0415 - only needed on this branch

    return np.asarray(tifffile.imread(io.BytesIO(payload)))


def _decode_baseline_tiff(payload: bytes) -> np.ndarray:
    """
    Decode an uncompressed single-sample greyscale TIFF, and nothing else.

    Parameters
    ----------
    payload : bytes
        The bytes of a TIFF file.

    Returns
    -------
    np.ndarray
        The 2D image.

    Raises
    ------
    ValueError
        If the payload is not a TIFF, or is a variant this reader will not
        guess at. The message names ``tifffile`` because installing it is
        the fix, and a wrong guess would silently reinterpret counts.
    """
    if len(payload) < _TIFF_HEADER_SIZE:
        msg = f"a monitor image of {len(payload)} bytes is not a TIFF"
        raise ValueError(msg)
    order = payload[:2]
    if order == _TIFF_LITTLE_ENDIAN:
        prefix = "<"
    elif order == _TIFF_BIG_ENDIAN:
        prefix = ">"
    else:
        msg = f"not a TIFF: byte-order mark {order!r}"
        raise ValueError(msg)
    magic, first_ifd = struct.unpack(prefix + "HI", payload[2:8])
    if magic != _TIFF_MAGIC:
        msg = (
            f"not a baseline TIFF (magic {magic}); install this project's "
            f"'dectris' extra so tifffile can read what the DCU sent"
        )
        raise ValueError(msg)
    tags = _read_tiff_directory(payload, first_ifd, prefix)

    compression = tags.get(_TIFF_TAG_COMPRESSION, (_TIFF_COMPRESSION_NONE,))[0]
    if compression != _TIFF_COMPRESSION_NONE:
        msg = (
            f"the monitor image uses TIFF compression {compression}, which "
            f"this reader will not guess at; install this project's "
            f"'dectris' extra so tifffile can decode it"
        )
        raise ValueError(msg)
    samples = tags.get(_TIFF_TAG_SAMPLES_PER_PIXEL, (1,))[0]
    sample_format = tags.get(_TIFF_TAG_SAMPLE_FORMAT, (_TIFF_SAMPLE_FORMAT_UINT,))[0]
    if samples != 1 or sample_format != _TIFF_SAMPLE_FORMAT_UINT:
        msg = (
            f"expected one unsigned-integer sample per pixel, got "
            f"{samples} sample(s) of format {sample_format}; install this "
            f"project's 'dectris' extra so tifffile can decode it"
        )
        raise ValueError(msg)
    bits = tags[_TIFF_TAG_BITS_PER_SAMPLE][0]
    dtype = _TIFF_DTYPES.get(bits)
    if dtype is None:
        msg = f"unsupported TIFF bit depth {bits}; install the 'dectris' extra"
        raise ValueError(msg)

    width = tags[_TIFF_TAG_IMAGE_WIDTH][0]
    height = tags[_TIFF_TAG_IMAGE_LENGTH][0]
    offsets = tags[_TIFF_TAG_STRIP_OFFSETS]
    counts = tags[_TIFF_TAG_STRIP_BYTE_COUNTS]
    raw = b"".join(
        payload[offset : offset + count]
        for offset, count in zip(offsets, counts, strict=True)
    )
    flat = np.frombuffer(raw, dtype=np.dtype(dtype).newbyteorder(prefix))
    if flat.size != width * height:
        msg = f"TIFF strips hold {flat.size} pixels, not {width * height}"
        raise ValueError(msg)
    return flat.reshape(height, width).astype(dtype)


def _read_tiff_directory(
    payload: bytes,
    offset: int,
    prefix: str,
) -> dict[int, tuple[int, ...]]:
    """
    Read one TIFF image file directory into ``{tag: values}``.

    Parameters
    ----------
    payload : bytes
        The whole file, since values larger than four bytes are stored
        elsewhere in it.
    offset : int
        Byte offset of the directory.
    prefix : str
        ``"<"`` or ``">"``, the struct byte-order prefix.

    Returns
    -------
    dict[int, tuple[int, ...]]
        Every tag in the directory, as a tuple of integers.
    """
    (count,) = struct.unpack_from(prefix + "H", payload, offset)
    tags: dict[int, tuple[int, ...]] = {}
    for index in range(count):
        base = offset + 2 + index * _TIFF_ENTRY_SIZE
        tag, kind, length = struct.unpack_from(prefix + "HHI", payload, base)
        size = _TIFF_TYPE_SIZES.get(kind)
        if size is None:
            continue  # a type this reader has no use for; skip rather than fail
        total = size * length
        if total <= _TIFF_INLINE_VALUE_SIZE:
            source = base + 8
        else:
            (source,) = struct.unpack_from(prefix + "I", payload, base + 8)
        code = {1: "B", 2: "B", 3: "H", 4: "I", 5: "I"}[kind]
        values = struct.unpack_from(f"{prefix}{length}{code}", payload, source)
        tags[tag] = tuple(int(value) for value in values)
    return tags


# --------------------------------------------------------------------------
# The SIMPLON client
# --------------------------------------------------------------------------


class SimplonClient:
    """
    The documented SIMPLON REST surface, and nothing more than it.

    One class for both backends: the simulated one points it at a mock DCU
    on localhost and the hardware one at a real one, so there is no second
    implementation that could drift. Every URL is
    ``http://<address>/<module>/api/<version>/<task>/<parameter>``, which
    is the structure DECTRIS's own reference client builds.
    """

    def __init__(self, address: str, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._address = _normalise_address(address)
        self._timeout_s = timeout_s
        # Never through a proxy. urllib's default opener consults
        # $HTTP_PROXY and, on macOS, the system proxy configuration - and
        # a detector control unit is exactly the host you must not send
        # through one. A DCU lives on a private instrument network, so a
        # proxy either cannot route to it (the request hangs until the
        # timeout, or fails with a confusing error attributed to the
        # detector) or, worse, resolves the address to something else
        # entirely. An explicit empty ProxyHandler is the documented way
        # to say "direct".
        #
        # This is not hypothetical: the simulated backend talks to its own
        # mock DCU on 127.0.0.1, and whether that works with the default
        # opener depends on whether the machine happens to list 127.0.0.1
        # in $no_proxy. CI is not a machine you control.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
        )

    @property
    def address(self) -> str:
        """Return the ``host:port`` this client talks to."""
        return self._address

    def get_config(self, key: str, *, module: str = "detector") -> object:
        """
        Read one configuration parameter's value.

        Parameters
        ----------
        key : str
            The parameter name, e.g. ``"count_time"``.
        module : str
            The SIMPLON subsystem: ``detector``, ``monitor``, ``stream``,
            ``filewriter`` or ``system``.

        Returns
        -------
        object
            The ``value`` field of the parameter resource.
        """
        document = self._request("GET", module, "config", key)
        return json.loads(document)["value"]

    def put_config(
        self,
        key: str,
        value: object,
        *,
        module: str = "detector",
    ) -> None:
        """
        Write one configuration parameter.

        Parameters
        ----------
        key : str
            The parameter name.
        value : object
            The new value, JSON-encoded as ``{"value": value}`` the way
            SIMPLON expects.
        module : str
            The SIMPLON subsystem.
        """
        self._request(
            "PUT",
            module,
            "config",
            key,
            body=json.dumps({"value": value}).encode("utf-8"),
        )

    def get_status(self, key: str, *, module: str = "detector") -> object:
        """
        Read one status parameter's value.

        Parameters
        ----------
        key : str
            The status name, e.g. ``"state"``.
        module : str
            The SIMPLON subsystem.

        Returns
        -------
        object
            The ``value`` field of the status resource.
        """
        document = self._request("GET", module, "status", key)
        return json.loads(document)["value"]

    def command(self, name: str, *, module: str = "detector") -> object:
        """
        Send one command, e.g. ``arm``, ``trigger``, ``disarm``.

        Parameters
        ----------
        name : str
            The command name.
        module : str
            The SIMPLON subsystem.

        Returns
        -------
        object
            The decoded reply — ``arm`` answers with a sequence id, most
            others with nothing — or ``None`` when the DCU sent no body.
        """
        reply = self._request("PUT", module, "command", name, body=b"")
        if not reply:
            return None
        try:
            return json.loads(reply)
        except json.JSONDecodeError:
            return None

    def next_monitor_image(self) -> bytes | None:
        """
        Take the next image out of the monitor buffer, if one is there.

        Returns
        -------
        bytes | None
            The TIFF bytes, or ``None`` when the buffer is empty — which
            SIMPLON reports as HTTP 408 rather than as an error, because
            "nothing yet" is the normal state of a preview channel.

        Raises
        ------
        SimplonError
            For any failure that is *not* the empty buffer.
        """
        try:
            return self._request("GET", "monitor", "images", "next")
        except SimplonError as error:
            if error.status == _REQUEST_TIMEOUT:
                return None
            raise

    def _url(self, module: str, task: str, parameter: str | None) -> str:
        """
        Build a SIMPLON resource URL.

        Parameters
        ----------
        module : str
            The subsystem name.
        task : str
            ``config``, ``status``, ``command`` or ``images``.
        parameter : str | None
            The resource within the task, or None for the whole task.

        Returns
        -------
        str
            The absolute URL.
        """
        url = f"http://{self._address}/{module}/api/{SIMPLON_API_VERSION}/{task}/"
        if parameter is not None:
            url += parameter
        return url

    def _request(
        self,
        method: str,
        module: str,
        task: str,
        parameter: str | None = None,
        *,
        body: bytes | None = None,
    ) -> bytes:
        """
        Make one HTTP request and return its body.

        Parameters
        ----------
        method : str
            ``GET`` or ``PUT``.
        module : str
            The subsystem name.
        task : str
            ``config``, ``status``, ``command`` or ``images``.
        parameter : str | None
            The resource within the task.
        body : bytes | None
            The request body, for ``PUT``.

        Returns
        -------
        bytes
            The response body.

        Raises
        ------
        SimplonError
            If the DCU answered with an error status, or could not be
            reached at all. Carries ``status`` (the HTTP code, or None)
            so callers can tell 408 "no image yet" from a real failure.
        """
        url = self._url(module, task, parameter)
        request = urllib.request.Request(  # noqa: S310 - http:// only, built above
            url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with self._opener.open(
                request,
                timeout=self._timeout_s,
            ) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = _http_error_detail(error)
            message = f"{method} {url} failed: HTTP {error.code} {detail}".strip()
            raise SimplonError(message, status=error.code) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            message = f"{method} {url} could not be completed: {reason}"
            raise SimplonError(message, status=None) from error


def _http_error_detail(error: urllib.error.HTTPError) -> str:
    """
    Return the DCU's own explanation for an HTTP error, if it gave one.

    Parameters
    ----------
    error : urllib.error.HTTPError
        The raised error, whose body SIMPLON fills with a message.

    Returns
    -------
    str
        The body text, truncated, or the empty string.
    """
    try:
        body = error.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return body[:200]


def _normalise_address(address: str) -> str:
    """
    Return ``host:port``, defaulting to the port a DCU serves SIMPLON on.

    Parameters
    ----------
    address : str
        A host name, an IP address, or either with an explicit ``:port``.

    Returns
    -------
    str
        The address with a port.
    """
    if ":" in address:
        return address
    return f"{address}:{_DEFAULT_HTTP_PORT}"


# --------------------------------------------------------------------------
# The camera
# --------------------------------------------------------------------------


class DectrisCamera:
    """
    A DECTRIS detector driven through SIMPLON, satisfying ``Camera``.

    The mapping onto the protocol is the whole design decision, so it is
    written down rather than left to be read out of the methods:

    ``start``
        Configures internal software triggering (``trigger_mode`` ``ints``,
        one image per trigger, a long series) and **arms** the detector.
        Nothing is exposed yet, which matches the protocol's "returns as
        soon as the device has been told to start".
    ``acquire_frame``
        Sends one ``trigger`` and pulls the resulting image out of the
        monitor buffer. One frame, one exposure, on demand — which is what
        a pull-per-frame interface means and what makes this the
        survey-rate path rather than the streaming one.
    ``stop``
        Disarms. The detector is then idle, its configuration writable
        again, and available to whatever else wants it.

    The cost of this mapping is dead time between frames — a round trip
    per frame, so tens of frames per second rather than the thousands the
    detector can do. That is not a defect of the implementation; it is the
    line ``docs/vendor-support.md`` draws, made concrete.
    """

    def __init__(
        self,
        address: str,
        *,
        control_unit: SimulatedControlUnit | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._client = SimplonClient(address, timeout_s=timeout_s)
        self._control_unit = control_unit
        self._lock = threading.Lock()
        self._frame_index = 0
        self._armed = False
        self._closed = False
        self._triggers_left = 0
        self._series_id: int | None = None
        self._identity = _open_detector(self._client)
        self._parameters = CameraParameters(
            exposure_ms=self._read_exposure_ms(),
            binning=1,
        )

    @property
    def camera_id(self) -> str:
        """Return an identifier naming the detector, not merely its address."""
        return self._identity["camera_id"]

    @property
    def binning_values(self) -> Sequence[int]:
        """Return ``[1]``: a counting pixel array has no charge to sum."""
        return [1]

    def parameters(self) -> CameraParameters:
        """
        Return the settings the next frame will use, re-read from the DCU.

        Returns
        -------
        CameraParameters
            The detector's own ``count_time``, in milliseconds.
        """
        self._parameters = CameraParameters(
            exposure_ms=self._read_exposure_ms(),
            binning=1,
        )
        return self._parameters

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Apply an exposure and report what the detector rounded it to.

        Two protocol details are doing real work here. SIMPLON makes the
        detector configuration **read-only while armed**, so a ``configure``
        during live acquisition has to disarm, write, and re-arm — which is
        allowed by the ``Camera`` contract ("which frame first shows the
        change is the device's business"). And ``frame_time`` must never be
        less than ``count_time``, so the two are written in whichever order
        keeps that true at every instant rather than in a fixed one.

        Parameters
        ----------
        parameters : CameraParameters
            The requested exposure and binning.

        Returns
        -------
        CameraParameters
            What the detector reports afterwards, which is what the next
            frame's metadata will also say.

        Raises
        ------
        ValueError
            If a binning other than 1, or a readout other than
            ``image``, is requested.
        """
        if parameters.binning != 1:
            msg = (
                f"binning {parameters.binning} is not supported by "
                f"{self.camera_id}; a hybrid-pixel counting detector has no "
                f"charge to sum, and summing on the host would report a "
                f"pixel size the sensor does not have. Use the detector's "
                f"ROI instead, once CameraParameters has somewhere to put it"
            )
            raise ValueError(msg)
        if parameters.readout != IMAGE_READOUT:
            msg = (
                f"readout {parameters.readout!r} is not supported by "
                f"{self.camera_id}: SIMPLON offers no projection readout, and "
                f"this adapter does not know which direction an ELA's "
                f"spectrometer disperses along, so a host-side sum would "
                f"guess the axis it collapses. The monitor path delivers "
                f"images; project in analysis instead"
            )
            raise ValueError(msg)
        with self._lock:
            was_armed = self._armed
            if was_armed:
                self._disarm_locked()
            seconds = parameters.exposure_ms / 1000.0
            current = float(self._client.get_config("count_time"))
            if seconds >= current:
                # Growing: widen the frame period first, or the DCU
                # rejects a count_time that momentarily exceeds it.
                self._client.put_config("frame_time", seconds)
                self._client.put_config("count_time", seconds)
            else:
                self._client.put_config("count_time", seconds)
                self._client.put_config("frame_time", seconds)
            if was_armed:
                self._arm_locked()
        return self.parameters()

    def start(self) -> None:
        """
        Configure software triggering and arm the detector.

        Raises ``RuntimeError`` through :meth:`_require_open` if the
        camera has already been closed.
        """
        with self._lock:
            self._require_open()
            if self._armed:
                return
            self._client.put_config("trigger_mode", "ints")
            self._client.put_config("nimages", 1)
            self._client.put_config("ntrigger", _TRIGGERS_PER_ARM)
            self._client.put_config("mode", "enabled", module="monitor")
            self._arm_locked()

    def stop(self) -> None:
        """Disarm the detector, leaving it idle and configurable again."""
        with self._lock:
            if self._armed:
                self._disarm_locked()

    def acquire_frame(self) -> Frame:
        """
        Trigger one exposure and return the image the monitor buffer holds.

        Raises ``DetectorOpenError`` through
        :meth:`_wait_for_monitor_image` if no image arrives within the
        exposure plus a grace period, which on real hardware means the
        series ended or another client is draining the monitor buffer.

        Returns
        -------
        Frame
            A 2D integer frame of electron counts, with the metadata
            described in :func:`_frame_metadata`.

        Raises
        ------
        RuntimeError
            If ``start`` has not been called, or the camera is closed.
        """
        with self._lock:
            self._require_open()
            if not self._armed:
                msg = (
                    f"{self.camera_id} is not armed; call start() before "
                    f"acquire_frame(), as the Camera contract requires"
                )
                raise RuntimeError(msg)
            self._trigger_locked()
            payload = self._wait_for_monitor_image()
            data = decode_tiff(payload)
            metadata = _frame_metadata(self, self._frame_index)
            self._frame_index += 1
        return Frame(data=data, timestamp=_now(), metadata=metadata)

    def close(self) -> None:
        """Disarm the detector and release the mock DCU, if there is one."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._armed:
                try:
                    self._disarm_locked()
                except SimplonError:
                    # Closing must not raise: the server's shutdown
                    # handshake collects errors rather than dying on them,
                    # and a DCU that has already gone away is not a fault
                    # worth failing a teardown over.
                    _LOGGER.warning("could not disarm %s on close", self.camera_id)
            if self._control_unit is not None:
                self._control_unit.shutdown()

    @property
    def identity(self) -> dict[str, object]:
        """Return what the DCU said about itself when it was opened."""
        return dict(self._identity)

    @property
    def series_id(self) -> int | None:
        """Return the DCU's id for the armed series, or None when idle."""
        return self._series_id if self._armed else None

    @property
    def control_unit(self) -> SimulatedControlUnit | None:
        """
        Return the mock control unit, on the simulated backend only.

        Public because it is the only way a test can check the thing worth
        checking: not that ``start()`` returned, but that the *detector*
        ended up armed, that ``configure`` disarmed before writing, and
        that the requests went through a resource tree able to refuse
        them. On the hardware backend this is ``None`` and the real DCU is
        the only witness.
        """
        return self._control_unit

    @property
    def exposure_ms(self) -> float:
        """
        Return the exposure this adapter last read from the detector.

        The cached value rather than a fresh read, deliberately: this is
        what every frame's metadata is stamped with, and re-reading it
        would add an HTTP round trip inside the acquisition loop to
        describe an exposure that cannot have changed without going
        through :meth:`configure`.
        """
        return self._parameters.exposure_ms

    def _require_open(self) -> None:
        """
        Reject calls made after ``close``.

        Raises
        ------
        RuntimeError
            If the camera is closed.
        """
        if self._closed:
            msg = f"{self.camera_id} has been closed"
            raise RuntimeError(msg)

    def _trigger_locked(self) -> None:
        """
        Send one software trigger, re-arming if the series has run out.

        A SIMPLON series accepts a fixed number of triggers, and a
        pull-per-frame caller has no way to know that — so the boundary
        has to be invisible. Both halves of that are here, and the second
        is not redundant: the local count anticipates the end of the
        series, and the retry catches the cases it cannot anticipate,
        which is every way the DCU's idea of the series and this one can
        diverge (a reconfiguration, a firmware that counts images rather
        than triggers, a restarted control unit).
        """
        if self._triggers_left <= 0:
            self._disarm_locked()
            self._arm_locked()
        try:
            self._client.command("trigger")
        except SimplonError:
            _LOGGER.info("re-arming %s after a refused trigger", self.camera_id)
            self._disarm_locked()
            self._arm_locked()
            self._client.command("trigger")
        self._triggers_left -= 1

    def _arm_locked(self) -> None:
        """Arm the detector; the caller must hold the lock."""
        reply = self._client.command("arm")
        self._series_id = _sequence_id(reply)
        self._triggers_left = _TRIGGERS_PER_ARM
        self._armed = True

    def _disarm_locked(self) -> None:
        """Disarm the detector; the caller must hold the lock."""
        self._client.command("disarm")
        self._armed = False
        self._triggers_left = 0

    def _read_exposure_ms(self) -> float:
        """
        Read ``count_time`` from the detector, in milliseconds.

        Returns
        -------
        float
            The exposure the next frame will use.
        """
        return float(self._client.get_config("count_time")) * 1000.0

    def _wait_for_monitor_image(self) -> bytes:
        """
        Poll the monitor buffer until the triggered image arrives.

        Returns
        -------
        bytes
            The TIFF bytes of one image.

        Raises
        ------
        DetectorOpenError
            If nothing arrived within the exposure plus a grace period.
        """
        deadline = (
            time.monotonic() + self._parameters.exposure_ms / 1000.0 + _MONITOR_GRACE_S
        )
        while True:
            payload = self._client.next_monitor_image()
            if payload:
                return payload
            if time.monotonic() >= deadline:
                msg = (
                    f"{self.camera_id} produced no image within "
                    f"{_MONITOR_GRACE_S:.0f}s of being triggered. The monitor "
                    f"buffer is a preview channel and is shared: another "
                    f"client (Gatan GMS/Stela, Nion Swift, or a LiberTEM-live "
                    f"session) may be draining it, or the detector may have "
                    f"left the armed state"
                )
                raise DetectorOpenError(msg)
            time.sleep(_MONITOR_POLL_S)


def _sequence_id(reply: object) -> int | None:
    """
    Pull the series id out of an ``arm`` reply.

    Parameters
    ----------
    reply : object
        Whatever the command endpoint returned. SIMPLON spells the key
        ``"sequence id"``, with a space, which is worth isolating in one
        place rather than repeating.

    Returns
    -------
    int | None
        The sequence id, or None if the DCU did not report one.
    """
    if isinstance(reply, dict):
        for key in ("sequence id", "sequence_id"):
            if key in reply:
                return int(reply[key])
    return None


def _open_detector(client: SimplonClient) -> dict[str, object]:
    """
    Read the detector's identity and confirm nothing else is driving it.

    This is the adapter's half of the ownership interlock. SIMPLON lets
    anyone read and write configuration, but only one client can hold an
    armed series, so finding the detector already armed means something
    else — Gatan's GMS integration, Nion Swift, a LiberTEM-live session —
    owns it. Discovering that at open time, by name, beats discovering it
    as a failed ``arm`` several calls later.

    Parameters
    ----------
    client : SimplonClient
        A client pointed at the control unit.

    Returns
    -------
    dict[str, object]
        The identity and fixed properties worth putting on every frame.

    Raises
    ------
    DetectorOpenError
        If the DCU cannot be reached, is not a SIMPLON DCU, is in an
        unusable state, or is already armed by someone else.
    """
    try:
        description = str(client.get_config("description"))
        serial = str(client.get_config("detector_number"))
        state = str(client.get_status("state"))
    except SimplonError as error:
        msg = (
            f"could not open a DECTRIS detector at {client.address}. "
            f"{_open_failure_hint(error)} Pass the control unit's address "
            f"with --plugin, and use --backend {SIMULATED_BACKEND} to run "
            f"without a detector."
        )
        raise DetectorOpenError(msg) from error

    if state == _STATE_NA:
        msg = (
            f"the detector at {client.address} reports state 'na': it is "
            f"powered but not initialised. Send the SIMPLON 'initialize' "
            f"command (it takes minutes and resets the detector, so this "
            f"adapter will not do it behind your back), then retry"
        )
        raise DetectorOpenError(msg)
    if state == _STATE_ERROR:
        msg = (
            f"the detector at {client.address} reports state 'error'. Read "
            f"/detector/api/{SIMPLON_API_VERSION}/status/error on the DCU "
            f"for what it is complaining about; a high-voltage or cooling "
            f"fault has to be cleared there rather than from here"
        )
        raise DetectorOpenError(msg)
    if state != _STATE_IDLE:
        msg = (
            f"the detector at {client.address} is in state {state!r} rather "
            f"than 'idle', which means another client already has it armed "
            f"— Gatan GMS (where an ELA is the Stela camera), Nion Swift, or "
            f"a LiberTEM-live session. SIMPLON admits configuration from "
            f"anyone but only one armed series at a time, so stop the "
            f"acquisition there before starting one here"
        )
        raise DetectorOpenError(msg)

    return {
        "camera_id": f"dectris:{description}:{serial}",
        "description": description,
        "detector_number": serial,
        "software_version": _optional_config(client, "software_version"),
        "sensor_material": _optional_config(client, "sensor_material"),
        "sensor_thickness_m": _optional_config(client, "sensor_thickness"),
        "x_pixel_size_m": _optional_config(client, "x_pixel_size"),
        "y_pixel_size_m": _optional_config(client, "y_pixel_size"),
        "bit_depth_image": _optional_config(client, "bit_depth_image"),
        "incident_energy_ev": _optional_config(client, "incident_energy"),
        "threshold_energy_ev": _optional_config(client, "threshold_energy"),
        "countrate_correction_applied": _optional_config(
            client,
            "countrate_correction_applied",
        ),
        "flatfield_correction_applied": _optional_config(
            client,
            "flatfield_correction_applied",
        ),
        "pixel_mask_applied": _optional_config(client, "pixel_mask_applied"),
        "roi_mode": _optional_config(client, "roi_mode"),
    }


def _optional_config(client: SimplonClient, key: str) -> object:
    """
    Read a parameter that not every DECTRIS model publishes.

    Parameters
    ----------
    client : SimplonClient
        A client pointed at the control unit.
    key : str
        The parameter name.

    Returns
    -------
    object
        The value, or ``None`` if this detector has no such parameter —
        which the frame metadata then omits rather than defaulting, per
        the ``Frame`` vocabulary's rule that an absent key means "not
        reported".
    """
    try:
        return client.get_config(key)
    except SimplonError:
        return None


def _open_failure_hint(error: SimplonError) -> str:  # noqa: PLR0911 - one branch per cause, which is the point
    """
    Return the most likely cause of a failed connection to a DCU.

    Worth the branching for the same reason ``camera_server`` branches on
    platform: the failures look identical from the outside and have
    entirely different fixes. A refused connection, a timeout and a 404
    are "nothing is listening there", "something is at that address but it
    is not answering" and "something answered but it is not this API" —
    and on a detector that lives behind a private 10 GbE link, the second
    is the one that wastes an afternoon.

    Parameters
    ----------
    error : SimplonError
        The failure raised by the first request.

    Returns
    -------
    str
        A sentence naming what to check.
    """
    status = getattr(error, "status", None)
    if status == _NOT_FOUND:
        return (
            f"Something answered, but it has no "
            f"/detector/api/{SIMPLON_API_VERSION}/ resource: either it is "
            f"not a SIMPLON control unit, or its firmware serves a "
            f"different API version than {SIMPLON_API_VERSION}."
        )
    if status == _FORBIDDEN:
        return (
            "The control unit refused the request. SIMPLON makes the "
            "detector configuration read-only while a series is armed, so "
            "this usually means another client is mid-acquisition."
        )
    if status is not None:
        return f"The control unit answered with HTTP {status}."
    cause = error.__cause__
    reason = getattr(cause, "reason", cause)
    if isinstance(reason, socket.gaierror):
        return (
            "That host name did not resolve. A DCU is usually reached by "
            "IP on a private network rather than by name."
        )
    if isinstance(reason, ConnectionRefusedError):
        return (
            "Nothing is listening there. SIMPLON is served by the *control "
            "unit* on port 80, not by the detector head, so check you have "
            "the DCU's address and not the detector's."
        )
    if isinstance(reason, TimeoutError):
        return (
            "The connection timed out rather than being refused, which is "
            "what a wrong address on a reachable subnet looks like. A DCU "
            "commonly has two interfaces - a control network and a 10 GbE "
            "data link - and only one of them serves SIMPLON."
        )
    return (
        "The control unit could not be reached; check the address, the "
        "cabling, and that the DCU has finished booting."
    )


def _frame_metadata(camera: DectrisCamera, frame_index: int) -> dict[str, object]:
    """
    Return the metadata every frame from this server carries.

    Two entries are claims rather than pass-throughs and are worth the
    explanation:

    ``photometrically_linear: True``
        The opposite of ``camera_server``'s, and meant. A hybrid-pixel
        detector compares each arriving electron's charge pulse against a
        threshold and increments an integer counter; the stored value is a
        number of events, not a digitised charge, so there is no gain
        curve, no gamma, no black level and no demosaic between the
        physics and the pixel. The one real departure from linearity is
        count-rate paralysis at very high flux, which is why
        ``countrate_correction_applied`` is recorded next to it: a reader
        that cares about the top of the dynamic range needs to know
        whether the DCU corrected for it.
    ``counts_per_electron: 1.0``
        What counting means, and the reason the flag above can be True.

    ``high_tension_v`` is deliberately **absent**. The DCU does publish
    ``incident_energy``, but that is the beam energy the detector was
    *configured for* — it sets the discriminator threshold — not a reading
    from the column. Recording someone's configuration as an instrument
    measurement is exactly the fiction the vocabulary's "omit rather than
    default" rule exists to prevent, so it is recorded under its own name.

    Parameters
    ----------
    camera : DectrisCamera
        The camera producing the frame.
    frame_index : int
        Frames produced since the device was opened, from 0.

    Returns
    -------
    dict[str, object]
        The vocabulary documented on
        :class:`~miainwoodpecker.devices.interface.Frame`, plus the
        detector constants a caller needs to finish the calibration.
    """
    identity = camera.identity
    metadata: dict[str, object] = {
        "device_id": camera.camera_id,
        "frame_index": frame_index,
        "exposure_ms": camera.exposure_ms,
        "binning": 1,
        "camera_name": identity["description"],
        "camera_type": "hybrid_pixel_counting",
        "counts_per_electron": 1.0,
        "photometrically_linear": True,
        "detector_number": identity["detector_number"],
        "series_id": camera.series_id,
    }
    for key in (
        "software_version",
        "sensor_material",
        "sensor_thickness_m",
        "x_pixel_size_m",
        "y_pixel_size_m",
        "bit_depth_image",
        "incident_energy_ev",
        "threshold_energy_ev",
        "countrate_correction_applied",
        "flatfield_correction_applied",
        "pixel_mask_applied",
        "roi_mode",
    ):
        value = identity.get(key)
        if value is not None:
            metadata[key] = value
    return metadata


# --------------------------------------------------------------------------
# The simulated control unit
# --------------------------------------------------------------------------


class _DetectorModel:
    """
    The state a SIMPLON control unit keeps, and the rules it enforces.

    Separated from the HTTP handler so the state machine can be read in
    one place: the point of the simulated backend is that it *refuses*
    things a real DCU refuses — configuring an armed detector, triggering
    an unarmed one — rather than accepting everything and passing.
    """

    def __init__(self, shape: tuple[int, int] = _SIMULATED_SHAPE) -> None:
        height, width = shape
        self.lock = threading.Lock()
        self.state = _STATE_IDLE
        self.sequence_id = 0
        self.triggers_left = 0
        self.images: deque[bytes] = deque(maxlen=512)
        self.image_number = 0
        self.config: dict[str, object] = {
            "description": "Dectris ELA 500K",
            "detector_number": "E-00-000",
            "software_version": "simulated-1.8.0",
            "sensor_material": "Si",
            "sensor_thickness": 450e-6,
            "x_pixel_size": _ELA_PIXEL_PITCH_M,
            "y_pixel_size": _ELA_PIXEL_PITCH_M,
            "x_pixels_in_detector": width,
            "y_pixels_in_detector": height,
            "bit_depth_image": 16,
            "bit_depth_readout": 16,
            "count_time": _DEFAULT_EXPOSURE_MS / 1000.0,
            "frame_time": _DEFAULT_EXPOSURE_MS / 1000.0,
            "nimages": 1,
            "ntrigger": 1,
            "trigger_mode": "ints",
            "incident_energy": 100_000.0,
            "threshold_energy": 50_000.0,
            "countrate_correction_applied": True,
            "flatfield_correction_applied": True,
            "pixel_mask_applied": True,
            "roi_mode": "disabled",
            "compression": "bslz4",
        }
        self.monitor_config: dict[str, object] = {
            "mode": "disabled",
            "buffer_size": 512,
            "discard_new": False,
        }
        self.stream_config: dict[str, object] = {
            "mode": "disabled",
            "header_detail": "basic",
        }
        self._generator = np.random.default_rng(seed=0)

    @property
    def shape(self) -> tuple[int, int]:
        """Return the current readout shape as ``(rows, columns)``."""
        return (
            int(self.config["y_pixels_in_detector"]),
            int(self.config["x_pixels_in_detector"]),
        )

    def read_only_keys(self) -> frozenset[str]:
        """
        Return the configuration keys a real DCU reports but will not take.

        Returns
        -------
        frozenset[str]
            Keys whose PUT must answer 403, so the client cannot quietly
            depend on writing something a detector would refuse.
        """
        return frozenset(
            {
                "description",
                "detector_number",
                "software_version",
                "sensor_material",
                "sensor_thickness",
                "x_pixel_size",
                "y_pixel_size",
                "x_pixels_in_detector",
                "y_pixels_in_detector",
                "bit_depth_readout",
            },
        )

    def arm(self) -> int:
        """
        Arm the detector for a series.

        Returns
        -------
        int
            The new sequence id.

        Raises
        ------
        _SimplonRefusalError
            If the detector is not idle, which is what a second client
            trying to take it over would hit.
        """
        if self.state != _STATE_IDLE:
            msg = f"cannot arm from state {self.state!r}"
            raise _SimplonRefusalError(_BAD_REQUEST, msg)
        self.sequence_id += 1
        self.triggers_left = int(self.config["ntrigger"])
        self.state = _STATE_READY
        return self.sequence_id

    def disarm(self) -> None:
        """Return the detector to idle, whatever state it was in."""
        self.state = _STATE_IDLE
        self.triggers_left = 0

    def trigger(self) -> None:
        """
        Expose ``nimages`` frames and push them into the monitor buffer.

        Raises
        ------
        _SimplonRefusalError
            If the detector is not armed, or the series is exhausted.
        """
        if self.state != _STATE_READY:
            msg = f"cannot trigger from state {self.state!r}; arm first"
            raise _SimplonRefusalError(_BAD_REQUEST, msg)
        if self.triggers_left <= 0:
            msg = "the armed series has no triggers left"
            raise _SimplonRefusalError(_BAD_REQUEST, msg)
        self.triggers_left -= 1
        for _ in range(int(self.config["nimages"])):
            if self.monitor_config["mode"] == "enabled":
                self.images.append(encode_tiff(self._synthesise()))
            self.image_number += 1

    def next_image(self) -> bytes | None:
        """
        Take the oldest buffered image, as ``monitor/images/next`` does.

        Returns
        -------
        bytes | None
            TIFF bytes, or None when the buffer is empty.
        """
        if not self.images:
            return None
        return self.images.popleft()

    def _synthesise(self) -> np.ndarray:
        """
        Return one frame of counts that looks like what an ELA sees.

        Returns
        -------
        np.ndarray
            A ``uint16`` frame: an intense zero-loss peak, a falling tail,
            and Poisson noise. Poisson rather than Gaussian because the
            whole point of a counting detector is that its noise *is*
            counting statistics, and a simulator with the wrong noise model
            would make every downstream statistical assertion meaningless.
        """
        height, width = self.shape
        columns = np.arange(width, dtype=np.float64)
        # The zero-loss peak drifts a little frame to frame, so successive
        # frames differ structurally and not only by noise - the same
        # reason camera_server's simulator moves.
        centre = width * 0.1 + (self.image_number % 8) * 0.25
        zero_loss = 4000.0 * np.exp(-0.5 * ((columns - centre) / 2.5) ** 2)
        tail = 40.0 * np.exp(-(np.clip(columns - centre, 0.0, None)) / (width / 6.0))
        edge = 12.0 * (columns > width * 0.55)
        profile = zero_loss + tail + edge + 0.5
        rows = np.linspace(0.85, 1.0, height)[:, None]
        expected = profile[None, :] * rows * (self.config["count_time"] / 0.01)
        counts = self._generator.poisson(expected)
        return np.clip(counts, 0, np.iinfo(np.uint16).max).astype(np.uint16)


class _SimplonRefusalError(Exception):
    """A refusal the mock DCU answers with, carrying an HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _SimplonRequestHandler(http.server.BaseHTTPRequestHandler):
    """
    The mock DCU's HTTP surface: real routing, real statuses, real refusals.

    ``model`` is attached to the server rather than passed in, which is how
    :mod:`http.server` lets a handler reach shared state.
    """

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # http.server's own dispatch name
        """Answer a read of a config, status, or monitor image resource."""
        self._dispatch("GET")

    def do_PUT(self) -> None:  # http.server's own dispatch name
        """Answer a configuration write or a command."""
        self._dispatch("PUT")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """
        Route the mock's request log to this module's logger.

        Parameters
        ----------
        format : str
            The printf-style format http.server passes.
        *args : object
            Its arguments.
        """
        _LOGGER.debug("simulated DCU: " + format, *args)  # noqa: G003

    def _dispatch(self, method: str) -> None:
        """
        Route one request, translating refusals into HTTP statuses.

        Parameters
        ----------
        method : str
            ``GET`` or ``PUT``.
        """
        model: _DetectorModel = self.server.model  # type: ignore[attr-defined]
        parts = [part for part in self.path.split("?")[0].split("/") if part]
        expected_parts = 4
        if len(parts) < expected_parts or parts[1] != "api":
            self._respond_json(_NOT_FOUND, {"message": f"no resource {self.path!r}"})
            return
        module, _, version, task = parts[:expected_parts]
        parameter = parts[expected_parts] if len(parts) > expected_parts else None
        if version != SIMPLON_API_VERSION:
            self._respond_json(
                _NOT_FOUND,
                {"message": f"this control unit serves {SIMPLON_API_VERSION}"},
            )
            return
        try:
            with model.lock:
                self._route(method, model, module, task, parameter)
        except _SimplonRefusalError as refusal:
            self._respond_json(refusal.status, {"message": str(refusal)})

    def _route(
        self,
        method: str,
        model: _DetectorModel,
        module: str,
        task: str,
        parameter: str | None,
    ) -> None:
        """
        Answer one already-parsed request.

        Parameters
        ----------
        method : str
            ``GET`` or ``PUT``.
        model : _DetectorModel
            The shared detector state; the caller holds its lock.
        module : str
            ``detector``, ``monitor`` or ``stream``.
        task : str
            ``config``, ``status``, ``command`` or ``images``.
        parameter : str | None
            The resource within the task.

        Raises
        ------
        _SimplonRefusalError
            For anything the resource tree does not contain, or the state
            machine will not allow.
        """
        store = {
            ("detector", "config"): model.config,
            ("monitor", "config"): model.monitor_config,
            ("stream", "config"): model.stream_config,
        }.get((module, task))
        if store is not None and parameter is not None:
            self._config(method, model, store, parameter)
            return
        if (module, task) == ("detector", "status") and method == "GET":
            self._detector_status(model, parameter)
            return
        if (module, task) == ("monitor", "images") and method == "GET":
            self._monitor_image(model, parameter)
            return
        if task == "command" and method == "PUT":
            self._command(model, module, parameter)
            return
        msg = f"no resource /{module}/api/{SIMPLON_API_VERSION}/{task}/{parameter}"
        raise _SimplonRefusalError(_NOT_FOUND, msg)

    def _config(
        self,
        method: str,
        model: _DetectorModel,
        store: dict[str, object],
        parameter: str,
    ) -> None:
        """
        Read or write one configuration parameter.

        Parameters
        ----------
        method : str
            ``GET`` or ``PUT``.
        model : _DetectorModel
            The shared state, for the armed check.
        store : dict[str, object]
            The subsystem's parameter dictionary.
        parameter : str
            The parameter name.

        Raises
        ------
        _SimplonRefusalError
            If the parameter does not exist, is read-only, or the detector
            is armed and therefore not configurable.
        """
        if parameter not in store:
            msg = f"no such parameter {parameter!r}"
            raise _SimplonRefusalError(_NOT_FOUND, msg)
        if method == "GET":
            value = store[parameter]
            self._respond_json(
                _OK,
                {
                    "value": value,
                    "value_type": type(value).__name__,
                    "access_mode": "r" if parameter in model.read_only_keys() else "rw",
                },
            )
            return
        if parameter in model.read_only_keys():
            msg = f"{parameter!r} is read-only"
            raise _SimplonRefusalError(_FORBIDDEN, msg)
        if store is model.config and model.state != _STATE_IDLE:
            # The rule that makes DectrisCamera.configure() disarm first.
            msg = (
                f"the detector configuration is read-only while armed "
                f"(state {model.state!r})"
            )
            raise _SimplonRefusalError(_FORBIDDEN, msg)
        store[parameter] = self._body().get("value")
        self._respond_json(_OK, [parameter])

    def _detector_status(self, model: _DetectorModel, parameter: str | None) -> None:
        """
        Answer a detector status read.

        Parameters
        ----------
        model : _DetectorModel
            The shared state.
        parameter : str | None
            The status name.

        Raises
        ------
        _SimplonRefusalError
            If there is no such status.
        """
        statuses: dict[str, object] = {
            "state": model.state,
            "error": [],
            "time": _now().isoformat(),
        }
        if parameter not in statuses:
            msg = f"no such status {parameter!r}"
            raise _SimplonRefusalError(_NOT_FOUND, msg)
        self._respond_json(_OK, {"value": statuses[parameter], "value_type": "string"})

    def _monitor_image(self, model: _DetectorModel, parameter: str | None) -> None:
        """
        Answer ``monitor/images/next``, including the empty-buffer case.

        Parameters
        ----------
        model : _DetectorModel
            The shared state.
        parameter : str | None
            ``next`` or ``monitor``.

        Raises
        ------
        _SimplonRefusalError
            With 404 for an unknown image name, or 408 for "nothing
            buffered yet" — which is SIMPLON's own way of saying it and
            therefore something the client has to handle.
        """
        if parameter not in ("next", "monitor"):
            msg = f"no such monitor image {parameter!r}"
            raise _SimplonRefusalError(_NOT_FOUND, msg)
        payload = model.next_image()
        if payload is None:
            msg = "no image is currently buffered"
            raise _SimplonRefusalError(_REQUEST_TIMEOUT, msg)
        self.send_response(_OK)
        self.send_header("Content-Type", "application/tiff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _command(
        self,
        model: _DetectorModel,
        module: str,
        parameter: str | None,
    ) -> None:
        """
        Run one command against the state machine.

        Parameters
        ----------
        model : _DetectorModel
            The shared state.
        module : str
            The subsystem the command belongs to.
        parameter : str | None
            The command name.

        Raises
        ------
        _SimplonRefusalError
            If the command does not exist, or the state machine refuses it.
        """
        if module == "monitor" and parameter == "clear":
            model.images.clear()
            self._respond_json(_OK, {})
            return
        if module != "detector":
            msg = f"no {module!r} command {parameter!r}"
            raise _SimplonRefusalError(_NOT_FOUND, msg)
        if parameter == "arm":
            self._respond_json(_OK, {"sequence id": model.arm()})
            return
        if parameter == "trigger":
            model.trigger()
            self._respond_json(_OK, {})
            return
        if parameter in ("disarm", "abort", "cancel"):
            model.disarm()
            self._respond_json(_OK, {})
            return
        msg = f"no detector command {parameter!r}"
        raise _SimplonRefusalError(_NOT_FOUND, msg)

    def _body(self) -> dict[str, object]:
        """
        Read and decode the request body.

        Returns
        -------
        dict[str, object]
            The decoded JSON object, or an empty one.
        """
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def _respond_json(self, status: int, document: object) -> None:
        """
        Send a JSON response.

        Parameters
        ----------
        status : int
            The HTTP status code.
        document : object
            The body, JSON-encoded.
        """
        payload = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _LoopbackHTTPServer(http.server.ThreadingHTTPServer):
    """
    A threading HTTP server that does not do a reverse DNS lookup to bind.

    ``http.server.HTTPServer.server_bind`` calls
    ``socket.getfqdn(host)`` purely to fill in ``server_name``, which is
    used for CGI and logging and by nothing here. On Linux that resolves
    ``127.0.0.1`` from ``/etc/hosts`` instantly. On a machine with no
    resolver for the loopback range it blocks — and it blocks *inside*
    ``__init__``, before the mock control unit is listening and long
    before the device server binds its own RPC ports.

    That is not a hypothetical either: macOS CI runners failed every test
    that spawns a DECTRIS device server with ``ConnectionRefusedError``,
    because the server was still stuck in this lookup when the client's
    15-second connect deadline passed. The whole macOS job took 167
    seconds against Linux's 40. Skipping the lookup and using the bound
    address is what the standard library does anyway when the name cannot
    be resolved, minus the wait.
    """

    def server_bind(self) -> None:
        """Bind without resolving a name for the address."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class SimulatedControlUnit:
    """
    A mock DECTRIS control unit, served over real HTTP on localhost.

    Deliberately not a fake camera object. The simulated backend exists so
    the whole path runs with nothing installed, and a stub that returned
    frames from a Python method would leave the part most likely to be
    wrong — URL construction, JSON shapes, HTTP statuses, the armed-state
    rules, TIFF decoding — completely untested. This serves the documented
    resources instead, and the same :class:`SimplonClient` drives it.
    """

    def __init__(self, shape: tuple[int, int] = _SIMULATED_SHAPE) -> None:
        self._model = _DetectorModel(shape)
        self._server = _LoopbackHTTPServer(
            ("127.0.0.1", 0),
            _SimplonRequestHandler,
        )
        self._server.model = self._model  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="simulated-dcu",
            daemon=True,
        )
        self._thread.start()

    @property
    def address(self) -> str:
        """Return the ``host:port`` this control unit is listening on."""
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    @property
    def model(self) -> _DetectorModel:
        """Return the detector state, so a test can inspect what happened."""
        return self._model

    def shutdown(self) -> None:
        """Stop serving and release the socket; idempotent."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------


class ServerInstrument:
    """
    The ``instrument`` target for a detector with no microscope around it.

    The same shape ``camera_server`` uses, and for the same reason: the
    client connects here first to ask what exists. A DECTRIS detector on
    someone else's column has no stage, no defocus and no blanker of its
    own, so the honest control list is empty and ``park`` has nothing to
    park. The column's own adapter, if there is one, is a separate server.
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
            One micrometre, the same documented stand-in the other servers
            use when no controller publishes a stage size.
        """
        return 1000.0

    def available_controls(self) -> Sequence[str]:
        """Return no controls; a detector owns none of them."""
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
            and ``endpoints`` — the last naming the port and kind of
            every target, which is how the client reaches them.
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
        Answer "is this server alive?" without touching the detector.

        Returns
        -------
        dict[str, object]
            The same shape the other servers report, so one client reads
            all of them.
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
        Disarm and release the detector, then let the server exit.

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


def open_camera(backend: str, address: str) -> DectrisCamera:
    """
    Build the camera the requested backend describes.

    Parameters
    ----------
    backend : str
        ``"simulated"`` or ``"hardware"``.
    address : str
        The control unit's address, for the hardware backend; ignored when
        simulated, which starts its own.

    Returns
    -------
    DectrisCamera
        The camera to serve.

    Raises
    ------
    ValueError
        If ``backend`` is not one this server accepts.
    DetectorOpenError
        If the hardware backend's optional dependency is missing, or the
        control unit cannot be opened.
    Exception
        Whatever opening the simulated control unit raised — re-raised
        unchanged, but only after the mock DCU has been shut down, so a
        failed open does not leave a listening socket behind.
    """
    if backend not in BACKENDS:
        msg = f"unknown backend {backend!r}; expected one of {', '.join(BACKENDS)}"
        raise ValueError(msg)
    if backend == SIMULATED_BACKEND:
        unit = SimulatedControlUnit()
        try:
            return DectrisCamera(unit.address, control_unit=unit)
        except Exception:
            unit.shutdown()
            raise
    try:
        import tifffile  # noqa: F401, PLC0415 - presence check, see below
    except ImportError as error:
        msg = (
            "the hardware backend needs tifffile to decode the monitor "
            "images a control unit returns, and it is not installed. "
            "Install this project's 'dectris' extra, or use "
            f"--backend {SIMULATED_BACKEND} to run without a detector."
        )
        raise DetectorOpenError(msg) from error
    return DectrisCamera(address)


def serve(
    ports: typing.Mapping[str, int],
    authkey: bytes,
    *,
    backend: str = SIMULATED_BACKEND,
    address: str = _DEFAULT_ADDRESS,
) -> None:
    """
    Open the detector and serve it until a client shuts the server down.

    Parameters
    ----------
    ports : typing.Mapping[str, int]
        Ports the caller pins, by target name. In practice that is
        ``instrument`` and nothing else: every other target binds on an
        OS-assigned port and is reported through ``describe``.
    authkey : bytes
        Shared secret for every Listener.
    backend : str
        ``"simulated"`` or ``"hardware"``.
    address : str
        The control unit the hardware backend opens.

    Raises
    ------
    SystemExit
        With :data:`PORT_UNAVAILABLE_EXIT_STATUS` when a listener cannot
        bind, so the client retries with fresh ports.
    """
    camera = open_camera(backend, address)
    stop_event = threading.Event()
    targets: dict[str, object] = {CAMERA_TARGET: camera}
    instrument = ServerInstrument(targets, stop_event, backend)
    targets[INSTRUMENT_TARGET] = instrument
    writers = {CAMERA_TARGET: SharedFrameWriter(), INSTRUMENT_TARGET: None}

    names = list(targets)
    try:
        listeners, bound = bind_targets(names, ports, authkey)
    except OSError as error:
        _LOGGER.error(  # noqa: TRY400 - a traceback adds nothing here
            "could not bind a listener (%s); the client will retry",
            error,
        )
        camera.close()
        raise SystemExit(PORT_UNAVAILABLE_EXIT_STATUS) from error
    instrument.publish_endpoints(
        endpoint_map(bound, {CAMERA_TARGET: camera.camera_id}),
    )

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
        With ``backend``, ``plugin`` and ``instrument_port``.
    """
    parser = argparse.ArgumentParser(
        description="Serve a DECTRIS detector over the miainwoodpecker protocol.",
    )
    parser.add_argument("--backend", choices=BACKENDS, default=SIMULATED_BACKEND)
    parser.add_argument(
        "--plugin",
        action="append",
        default=None,
        help=(
            "the detector control unit's address for the hardware backend: "
            "a host name or IP, optionally with :port (default port 80)"
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
    address = arguments.plugin[0] if arguments.plugin else _DEFAULT_ADDRESS
    try:
        serve(ports, authkey, backend=arguments.backend, address=address)
    except DetectorOpenError as error:
        _LOGGER.error("%s", error)  # noqa: TRY400 - the message is the diagnostic
        return NO_DETECTOR_EXIT_STATUS
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
