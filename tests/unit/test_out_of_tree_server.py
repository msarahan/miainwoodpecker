"""
The client can drive a device server it did not ship.

This is the second-vendor question turned into a test. A vendor adapter
is a site-specific thing — Zeiss's SmartSEM ActiveX needs an agreement
with Zeiss before anyone may develop against it, JEOL's PyJEM lives on
the TEM control PC rather than on PyPI — so a lab has to be able to
write, install, and run one without forking this project. That means the
client has to launch a module it does not own.

The server here is a complete, tiny, vendor-free one: it implements the
command-line and wire contract in twenty-odd lines and serves fake
devices. That is the point — it measures how much of a real adapter is
protocol plumbing rather than vendor work, and it is the executable
specification an out-of-tree package writes against.

No ``device`` extra needed; nothing here imports ``nion.*``.
"""

from __future__ import annotations

import textwrap
import time

import numpy as np
import pytest

from miainwoodpecker.devices import Camera, Scanner
from miainwoodpecker.devices.interface import CameraParameters, ScanParameters
from miainwoodpecker.devices.remote import (
    DEFAULT_SERVER_MODULE,
    SERVER_RESPONSIVE,
    DeviceServerStartupError,
    remote_instrument,
)

# What the fake vendor's "detector" reports, so the assertions below are
# about *this* server rather than about anything Nion-shaped.
_VENDOR_CAMERA_ID = "acme_direct_detector"
_VENDOR_SCANNER_ID = "acme_scan_unit"
_FRAME_VALUE = 7.0
# A missing module must fail on the server's own early exit, not on the
# 15s connect deadline. Measured at ~0.04s; this is a loose ceiling.
_STARTUP_FAILURE_CEILING_S = 5.0
# An exposure the fake server did not start with, so a round trip is visible.
_CONFIGURED_EXPOSURE_MS = 9.0
# The fake vendor's scan unit has two detectors, SE and BSE, and reads
# both out of one pass - the ordinary case on a scanned instrument.
_CHANNEL_COUNT = 2

_SERVER_SOURCE = '''
"""A minimal out-of-tree device server, standing in for a vendor adapter."""

from __future__ import annotations

import argparse
import datetime
import os
import threading
import uuid
from multiprocessing.connection import Listener

from miainwoodpecker.devices.interface import CameraParameters, Frame
from miainwoodpecker.devices.rpc import TARGET_NAMES, Result

VENDOR_CAMERA_ID = "{camera_id}"
VENDOR_SCANNER_ID = "{scanner_id}"
FRAME_VALUE = {frame_value}


class FakeCamera:
    """A camera whose frames are constant, so a test can recognise them."""

    def __init__(self) -> None:
        self._parameters = CameraParameters(exposure_ms=5.0)

    @property
    def camera_id(self):
        return VENDOR_CAMERA_ID

    @property
    def binning_values(self):
        return [1, 2]

    def parameters(self):
        return self._parameters

    def configure(self, parameters):
        self._parameters = parameters
        return self._parameters

    def start(self):
        pass

    def stop(self):
        pass

    def acquire_frame(self):
        import numpy as np

        return Frame(
            data=np.full((8, 8), FRAME_VALUE, dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={{"device_id": VENDOR_CAMERA_ID, "frame_index": 0}},
        )

    def close(self):
        pass


class FakeScanner:
    """A scanner that honours the requested shape and nothing else."""

    @property
    def scanner_id(self):
        return VENDOR_SCANNER_ID

    @property
    def channel_names(self):
        return ["SE", "BSE"]

    def scan_frame(self, parameters, channel=0):
        import numpy as np

        return Frame(
            data=np.full(parameters.shape, FRAME_VALUE, dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={{"device_id": VENDOR_SCANNER_ID, "channel_index": channel}},
        )

    def scan_frames(self, parameters, channels):
        # One pass, several detectors: the identity is minted here,
        # inside the call that establishes it, and every sibling gets the
        # same one. A vendor adapter whose hardware cannot read several
        # channels out of one pass raises here instead.
        import numpy as np

        pass_id = str(uuid.uuid4())
        timestamp = datetime.datetime.now(tz=datetime.UTC)
        return [
            Frame(
                # Distinguishable per channel, so a caller can tell which
                # frame is which rather than trusting the order.
                data=np.full(
                    parameters.shape, FRAME_VALUE + channel, dtype=np.float32,
                ),
                timestamp=timestamp,
                metadata={{
                    "device_id": VENDOR_SCANNER_ID,
                    "channel_index": channel,
                    "scan_pass_id": pass_id,
                    "simultaneous_channels": list(channels),
                }},
            )
            for channel in channels
        ]

    def close(self):
        pass


class FakeInstrument:
    """The instrument target: describe, health, shutdown, and no controls."""

    def __init__(self, targets, stop_event):
        self._targets = targets
        self._stop = stop_event

    def stage_size_nm(self):
        return 1000.0

    def available_controls(self):
        return []

    def park(self):
        pass

    def describe(self):
        return {{
            "backend": "simulated",
            "targets": list(self._targets),
            "controls": [],
            "stage_size_nm": self.stage_size_nm(),
        }}

    def health(self):
        return {{
            "ok": True,
            "pid": os.getpid(),
            "backend": "simulated",
            "uptime_s": 0.0,
            "targets": list(self._targets),
            "open_devices": [],
            "shutting_down": False,
        }}

    def shutdown(self):
        self._stop.set()
        return {{"beam_blanked": False, "errors": [], "closed": []}}


def serve_target(listener, target, stop_event):
    while not stop_event.is_set():
        try:
            connection = listener.accept()
        except OSError:
            return
        threading.Thread(
            target=handle, args=(connection, target), daemon=True
        ).start()


def handle(connection, target):
    while True:
        try:
            call = connection.recv()
        except (EOFError, OSError):
            return
        try:
            attribute = getattr(target, call.method)
            value = (
                attribute(*call.args, **call.kwargs)
                if callable(attribute)
                else attribute
            )
        except Exception as error:  # noqa: BLE001
            connection.send(
                Result(error=f"{{type(error).__name__}}: {{error}}",
                       error_type=type(error).__name__)
            )
            continue
        connection.send(Result(value=value))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="simulated")
    parser.add_argument("--plugin", action="append", default=[])
    parser.add_argument("--enable-test-hooks", action="store_true")
    parser.add_argument("ports", nargs=len(TARGET_NAMES), type=int)
    arguments = parser.parse_args()

    authkey = bytes.fromhex(os.environ["MIAINWOODPECKER_AUTHKEY"])
    stop_event = threading.Event()
    # A vendor with one detector and one scan unit by default: two of
    # the four target names go unserved, which describe() reports.
    # --plugin is the protocol's general "server-specific configuration"
    # channel. This project's Nion server reads it as nionswift_plugin
    # module names; nothing requires that, and this stand-in reads it as
    # which targets to serve. An adapter is free to use it for a device
    # address, a config file, or nothing at all.
    served = tuple(arguments.plugin or ("ronchigram_camera", "scanner"))
    available = {{
        "ronchigram_camera": FakeCamera,
        "eels_camera": FakeCamera,
        "scanner": FakeScanner,
    }}
    targets = {{name: available[name]() for name in served}}
    targets["instrument"] = FakeInstrument(served, stop_event)
    threads = []
    for name, port in zip(TARGET_NAMES, arguments.ports):
        if name not in targets:
            continue
        listener = Listener(("localhost", port), authkey=authkey)
        thread = threading.Thread(
            target=serve_target,
            args=(listener, targets[name], stop_event),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    stop_event.wait()


if __name__ == "__main__":
    main()
'''


@pytest.fixture
def vendor_server_module(tmp_path, monkeypatch):
    """Write a stand-in vendor device server and make it importable."""
    package = tmp_path / "acme_miainwoodpecker_adapter"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "server.py").write_text(
        textwrap.dedent(
            _SERVER_SOURCE.format(
                camera_id=_VENDOR_CAMERA_ID,
                scanner_id=_VENDOR_SCANNER_ID,
                frame_value=_FRAME_VALUE,
            ),
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    return "acme_miainwoodpecker_adapter.server"


def test_the_default_server_module_is_this_projects_own():
    """The default is unchanged, so nothing existing has to name it."""
    assert DEFAULT_SERVER_MODULE == "miainwoodpecker.devices.nion_server"


def test_a_third_party_server_module_can_be_driven_end_to_end(
    vendor_server_module,
):
    """
    An out-of-tree adapter serves this client with no change to it.

    Everything a real vendor package would have to satisfy is exercised
    here: the command line, the authkey handshake, ``describe()``,
    connecting only to the targets it reports, both device protocols, and
    the shutdown handshake. What a real one adds on top is vendor work,
    not protocol work — which is the point of measuring it this way.
    """
    with remote_instrument(server_module=vendor_server_module) as microscope:
        assert isinstance(microscope.ronchigram_camera, Camera)
        assert isinstance(microscope.scanner, Scanner)
        assert microscope.ronchigram_camera.camera_id == _VENDOR_CAMERA_ID
        assert microscope.scanner.scanner_id == _VENDOR_SCANNER_ID

        camera = microscope.ronchigram_camera
        took = camera.configure(CameraParameters(exposure_ms=_CONFIGURED_EXPOSURE_MS))
        assert took.exposure_ms == _CONFIGURED_EXPOSURE_MS
        camera.start()
        try:
            frame = camera.acquire_frame()
        finally:
            camera.stop()
        assert float(np.mean(frame.data)) == pytest.approx(_FRAME_VALUE)

        parameters = ScanParameters(
            height=16, width=24, pixel_time_us=1.0, fov_nm=100.0,
        )
        scanned = microscope.scanner.scan_frame(parameters, channel=1)
        assert scanned.data.shape == parameters.shape
        assert scanned.metadata["channel_index"] == 1


def test_a_vendor_scanner_can_return_a_whole_simultaneous_pass(
    vendor_server_module,
):
    """
    A third-party adapter's multi-channel pass reaches the client intact.

    ``scan_frames`` is part of the ``Scanner`` protocol, so this is now
    part of what an out-of-tree adapter has to implement — and the twenty
    lines above are the whole of what it costs, since the transport,
    the client handle, and the metadata rules are the project's. What is
    exercised here is the client half of the multi-frame path with no
    vendor software present at all: the request crosses as one call (not
    one per channel, which is the behaviour the whole feature exists to
    remove), and the frames come back in request order with the pass
    identity the adapter minted.
    """
    with remote_instrument(server_module=vendor_server_module) as microscope:
        parameters = ScanParameters(
            height=16, width=24, pixel_time_us=1.0, fov_nm=100.0,
        )
        frames = microscope.scanner.scan_frames(parameters, (0, 1))

        assert len(frames) == _CHANNEL_COUNT
        assert [frame.metadata["channel_index"] for frame in frames] == [0, 1]
        assert frames[0].metadata["scan_pass_id"] == frames[1].metadata["scan_pass_id"]
        assert all(
            frame.metadata["simultaneous_channels"] == [0, 1] for frame in frames
        )
        # Each frame is its own channel's data rather than the first one
        # twice, which is what a set arriving mis-ordered would look like.
        assert float(np.mean(frames[0].data)) == pytest.approx(_FRAME_VALUE)
        assert float(np.mean(frames[1].data)) == pytest.approx(_FRAME_VALUE + 1)


def test_a_vendor_serving_fewer_targets_is_handled(vendor_server_module):
    """
    A vendor with no EELS camera simply has none, rather than failing.

    The mechanism already existed for real instruments that lack one of
    usim's two cameras; this pins that it is what an entirely different
    vendor relies on. A Zeiss or Thermo SEM has detectors and no camera
    at all, which is the same shape one step further.
    """
    with remote_instrument(server_module=vendor_server_module) as microscope:
        description = microscope.instrument.describe()
        assert list(description["targets"]) == ["ronchigram_camera", "scanner"]
        assert microscope.eels_camera is None
        assert description["controls"] == []


def test_an_unimportable_server_module_fails_rather_than_hanging(monkeypatch):
    """
    Naming a module that is not installed is a bounded, explained failure.

    The realistic first mistake with an out-of-tree adapter: the package
    is not on the path of the interpreter running the application. The
    old hang this project has already been bitten by makes it worth
    asserting that this is *not* how that presents.
    """
    monkeypatch.delenv("PYTHONPATH", raising=False)
    module = "miainwoodpecker_no_such_vendor_adapter.server"
    started = time.monotonic()
    with (
        pytest.raises(DeviceServerStartupError, match=module),
        remote_instrument(server_module=module),
    ):
        pass  # pragma: no cover - the context manager must not open
    # Bounded, and by the server's early exit rather than by the 15s
    # connect deadline: naming a missing module must not cost a wait.
    assert time.monotonic() - started < _STARTUP_FAILURE_CEILING_S


def test_the_stand_in_server_needs_nothing_from_this_package_but_the_protocol(
    vendor_server_module,
):
    """
    The adapter contract is two imports wide.

    ``interface`` for the neutral types and ``rpc`` for the wire
    vocabulary; nothing else of this project is needed to serve it. That
    is what makes an out-of-tree adapter a small dependency rather than a
    fork, and it is worth pinning so a future change cannot quietly widen
    it without someone noticing.
    """
    source = (
        __import__(vendor_server_module, fromlist=["main"]).__file__
    )
    with open(source, encoding="utf-8") as handle:  # noqa: PTH123 - plain read
        text = handle.read()
    imported = {
        line.split()[1]
        for line in text.splitlines()
        if line.startswith("from miainwoodpecker")
    }
    assert imported == {
        "miainwoodpecker.devices.interface",
        "miainwoodpecker.devices.rpc",
    }




def test_a_detector_only_server_needs_no_scanner(vendor_server_module):
    """
    A camera with no scan unit is a supported instrument, not a KeyError.

    This is the shape of every direct detector — a Direct Electron,
    DECTRIS, or Hamamatsu camera driven through its own SDK rather than
    through a microscope vendor's — and of a camera on a bench. The
    cameras were already optional here; the scanner was not, so
    "vendor-neutral" quietly meant "must have a scan unit shaped like
    Nion's". Reachable through ``plugin_names``, which is the protocol's
    server-specific configuration channel.
    """
    with remote_instrument(
        server_module=vendor_server_module,
        plugin_names=["ronchigram_camera"],
    ) as detector:
        assert detector.scanner is None
        assert detector.eels_camera is None
        camera = detector.ronchigram_camera
        assert camera is not None
        assert camera.camera_id == _VENDOR_CAMERA_ID

        camera.start()
        try:
            frame = camera.acquire_frame()
        finally:
            camera.stop()
        assert float(np.mean(frame.data)) == pytest.approx(_FRAME_VALUE)

        # And the teardown handshake still runs against a device set the
        # Nion server would never produce.
        assert detector.instrument.check_health().state == SERVER_RESPONSIVE


def test_cameras_can_be_enumerated_without_asking_for_nion_shaped_slots(
    vendor_server_module,
):
    """
    ``cameras()`` reports what is there, by name.

    The named attributes stay because the viewer and the scripts use
    them, but a detector-only caller should not have to ask about
    ``eels_camera`` to find out it does not exist.
    """
    with remote_instrument(
        server_module=vendor_server_module,
        plugin_names=["ronchigram_camera", "eels_camera"],
    ) as detector:
        assert set(detector.cameras()) == {"ronchigram_camera", "eels_camera"}
        assert detector.scanner is None
