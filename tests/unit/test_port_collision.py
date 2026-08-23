"""
Losing a port between probing it and binding it is survivable, not fatal.

``_free_port()`` picks a port by binding to port 0 and *releasing* the
socket, so the port is reserved by convention only until the child binds
it — seconds later, after a whole vendor stack has been imported. Anything
else on the machine may take it in that window, and the server answers by
exiting with ``PORT_UNAVAILABLE_EXIT_STATUS``. The client's job is to
notice and try again with different ports.

The detection used to be a stopwatch: spawn, then ``process.wait(0.4)``
and treat a timeout as "healthy". That was wrong in both directions. A
healthy server never exits, so *every* good startup paid the full 0.4 s
for an answer it already knew; and a machine loaded enough to take longer
than 0.4 s to reach its bind — which is exactly the machine most likely to
have a collision — reported the curable failure as an anonymous startup
error. It was seen in CI once, after the target list grew to five ports.

So the retry now spans the whole spawn-and-connect. ``_connect_with_retry``
polls the child on every attempt anyway, and when it finds it dead with
that status it raises an internal ``_PortsLostError`` instead of the
generic diagnostic; ``_spawn_and_connect`` catches it, releases whatever
this attempt had already opened, and respawns with fresh ports and a fresh
deadline. These tests provoke that deterministically — a server module
that loses its ports on the first *n* spawns and then serves normally,
counted in a file rather than raced for — and pin the three things worth
pinning: recovery before any connection, recovery *after* connections were
already made to the doomed server, and the diagnostic a genuinely hostile
machine still gets.

No ``device`` extra needed; nothing here imports ``nion.*``.
"""

from __future__ import annotations

import textwrap
import typing

import pytest

from miainwoodpecker.devices import Camera
from miainwoodpecker.devices import remote as remote_module
from miainwoodpecker.devices.remote import (
    PORT_UNAVAILABLE_EXIT_STATUS,
    _PORT_RETRY_ATTEMPTS,
    DeviceServerStartupError,
    _start_server,
    remote_instrument,
)

if typing.TYPE_CHECKING:
    import pathlib

_VENDOR_CAMERA_ID = "acme_collision_camera"
# How long the "late exit" server dawdles before reporting its lost
# ports. Comfortably longer than the 0.4 s window this change deleted:
# under the old code this collision was invisible and surfaced as an
# anonymous startup failure, so the delay is the point of the test rather
# than an incidental sleep. Still short enough to pay for twice.
_LATE_EXIT_S = 1.0
# More collisions than the client will ever retry, for the hostile-machine
# case. Any number above the attempt budget does; this one cannot be
# mistaken for a boundary value.
_ENDLESS_COLLISIONS = 99
# One collision, then a server that behaves: the recovery cases both cost
# exactly two spawns, and counting them is how "it retried" is told apart
# from "it happened to work".
_ONE_COLLISION = "1"
_SPAWNS_AFTER_ONE_COLLISION = 2
# The authkey `_start_server` mints for each spawn.
_AUTHKEY_BYTES = 32

_SERVER_SOURCE = '''
"""A device server that loses its ports on the first N spawns, then serves."""

from __future__ import annotations

import argparse
import datetime
import os
import socket
import sys
import threading
import time
from multiprocessing.connection import Listener

from miainwoodpecker.devices.interface import Frame
from miainwoodpecker.devices.rpc import Result, target_kind


def unbound_port():
    """A port nothing is listening on: bound to learn the number, released."""
    with socket.socket() as probe:
        probe.bind(("localhost", 0))
        return probe.getsockname()[1]

PORT_UNAVAILABLE_EXIT_STATUS = {exit_status}
VENDOR_CAMERA_ID = "{camera_id}"
LATE_EXIT_S = {late_exit_s}


def count_this_spawn(state_path):
    """Record this spawn and return how many have happened before it."""
    with open(state_path, "a", encoding="utf-8") as state:
        state.write("spawned\\n")
    with open(state_path, encoding="utf-8") as state:
        return len(state.readlines()) - 1


class FakeCamera:
    """A camera whose frames are constant, so a test can recognise them."""

    @property
    def camera_id(self):
        return VENDOR_CAMERA_ID

    @property
    def binning_values(self):
        return [1]

    def parameters(self):
        from miainwoodpecker.devices.interface import CameraParameters

        return CameraParameters(exposure_ms=5.0)

    def configure(self, parameters):
        return parameters

    def start(self):
        pass

    def stop(self):
        pass

    def acquire_frame(self):
        import numpy as np

        return Frame(
            data=np.ones((4, 4), dtype=np.float32),
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata={{"device_id": VENDOR_CAMERA_ID, "frame_index": 0}},
        )

    def close(self):
        pass


class FakeInstrument:
    """The instrument target: describe, health, shutdown, and no controls."""

    def __init__(self, targets, stop_event, answered=None, endpoints=None):
        self._targets = targets
        self._stop = stop_event
        self._answered = answered
        self._endpoints = endpoints or {{}}

    def publish_endpoints(self, endpoints):
        self._endpoints = endpoints

    def stage_size_nm(self):
        return 1000.0

    def available_controls(self):
        return []

    def park(self):
        pass

    def describe(self):
        description = {{
            "backend": "simulated",
            "targets": list(self._targets),
            "controls": [],
            "stage_size_nm": self.stage_size_nm(),
            "endpoints": self._endpoints,
        }}
        if self._answered is not None:
            # Answered *after* the value is built but before it is sent,
            # which is close enough: the client cannot ask for anything
            # else until this reply lands.
            self._answered.set()
        return description

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


def lose_the_ports_before_connecting():
    """Exit without binding anything, after a deliberately late delay."""
    time.sleep(LATE_EXIT_S)
    sys.exit(PORT_UNAVAILABLE_EXIT_STATUS)


def lose_the_ports_after_describing(ports, authkey):
    """
    Bind and serve only the instrument, then exit once it has described itself.

    The collision a fixed watch window is worst at: this server looks
    entirely healthy for two connections and one call, and only then
    reports that a port it needed was taken. The client is mid-connect,
    holding two live connections to a server about to be gone.
    """
    stop_event = threading.Event()
    answered = threading.Event()
    # A camera the client will be told about and can never reach: the
    # endpoint names a port nothing is listening on, which is what a
    # server whose camera listener lost its port looks like from outside.
    instrument = FakeInstrument(
        ["ronchigram_camera"],
        stop_event,
        answered,
        {{
            "ronchigram_camera": {{
                "port": unbound_port(),
                "kind": target_kind("ronchigram_camera"),
                "label": VENDOR_CAMERA_ID,
            }},
        }},
    )
    listener = Listener(("localhost", ports["instrument"]), authkey=authkey)
    threading.Thread(
        target=serve_target,
        args=(listener, instrument, stop_event),
        daemon=True,
    ).start()
    answered.wait(timeout=30.0)
    sys.exit(PORT_UNAVAILABLE_EXIT_STATUS)


def serve_everything(ports, authkey):
    """
    Behave, for the spawn that is meant to succeed.

    "Behave" includes losing the port for real. The collisions this file
    provokes are counted from a file, but the client-allocated port here
    can also be taken by something else on the machine — a parallel test
    run is the case — and a server that let that escape as a traceback
    would turn a curable collision into a test failure about the wrong
    thing. So the real one is reported the same way the fake ones are.
    """
    stop_event = threading.Event()
    instrument = FakeInstrument(["ronchigram_camera"], stop_event)
    targets = {{
        "ronchigram_camera": FakeCamera(),
        "instrument": instrument,
    }}
    try:
        listeners = {{
            name: Listener(("localhost", ports.get(name, 0)), authkey=authkey)
            for name in targets
        }}
    except OSError:
        sys.exit(PORT_UNAVAILABLE_EXIT_STATUS)
    instrument.publish_endpoints({{
        name: {{
            "port": listener.address[1],
            "kind": target_kind(name),
            "label": name,
        }}
        for name, listener in listeners.items()
    }})
    for name, listener in listeners.items():
        threading.Thread(
            target=serve_target,
            args=(listener, targets[name], stop_event),
            daemon=True,
        ).start()
    stop_event.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="simulated")
    parser.add_argument("--plugin", action="append", default=[])
    parser.add_argument("--enable-test-hooks", action="store_true")
    parser.add_argument("--instrument-port", type=int, required=True)
    arguments = parser.parse_args()

    # --plugin is the protocol's general "server-specific configuration"
    # channel, and this stand-in reads it as where to count spawns, how
    # many of them lose their ports, and how they lose them.
    state_path, collisions, mode = arguments.plugin
    authkey = bytes.fromhex(os.environ["MIAINWOODPECKER_AUTHKEY"])
    ports = {{"instrument": arguments.instrument_port}}

    if count_this_spawn(state_path) < int(collisions):
        if mode == "after_describing":
            lose_the_ports_after_describing(ports, authkey)
        lose_the_ports_before_connecting()
    serve_everything(ports, authkey)


if __name__ == "__main__":
    main()
'''


@pytest.fixture
def colliding_server(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, pathlib.Path]:
    """
    Write a server module that loses its ports on demand, and count its spawns.

    How many spawns collide, and how, is decided per test through
    ``plugin_names`` — the protocol's own "server-specific configuration"
    channel — so one module serves all three cases and the count is a
    file on disk rather than something the test has to race for.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Where the stand-in adapter package is written.
    monkeypatch : pytest.MonkeyPatch
        Used to put that package on the path of this process and of the
        subprocesses it spawns.

    Returns
    -------
    tuple[str, pathlib.Path]
        The module name to launch, and the file the server appends to on
        every spawn.
    """
    package = tmp_path / "acme_collision_adapter"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "server.py").write_text(
        textwrap.dedent(
            _SERVER_SOURCE.format(
                exit_status=PORT_UNAVAILABLE_EXIT_STATUS,
                camera_id=_VENDOR_CAMERA_ID,
                late_exit_s=_LATE_EXIT_S,
            ),
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    return "acme_collision_adapter.server", tmp_path / "spawns.log"


def _spawn_count(spawns: pathlib.Path) -> int:
    """
    Return how many times the colliding server has been launched.

    Parameters
    ----------
    spawns : pathlib.Path
        The file the server appends a line to on every spawn.

    Returns
    -------
    int
        The number of spawns, or zero if the server never started.
    """
    if not spawns.exists():
        return 0
    return len(spawns.read_text(encoding="utf-8").splitlines())


def test_a_late_port_collision_is_retried_rather_than_fatal(colliding_server):
    """
    A collision reported long after the spawn still costs only a respawn.

    This is the case the deleted stopwatch could not see. The first server
    waits a full second — more than twice the old 0.4 s grace — before
    reporting that a port it needed was taken, which under the old code
    meant the watch window had already timed out and declared it healthy,
    so the loss surfaced as an anonymous ``DeviceServerStartupError``
    naming exit status 4 with no suggestion that trying again would work.

    Now the connect loop is what notices, and it is still looking. The
    session that comes back is from the *second* spawn, on fresh ports,
    and is an ordinary working session — which is the whole claim: a race
    the client provably lost is invisible to the caller.
    """
    server_module, spawns = colliding_server

    with remote_instrument(
        plugin_names=[str(spawns), _ONE_COLLISION, "before_connecting"],
        server_module=server_module,
    ) as microscope:
        assert isinstance(microscope.ronchigram_camera, Camera)
        assert microscope.ronchigram_camera.camera_id == _VENDOR_CAMERA_ID

    assert _spawn_count(spawns) == _SPAWNS_AFTER_ONE_COLLISION


def test_a_collision_found_mid_connect_recovers_from_a_half_built_session(
    colliding_server,
):
    """
    The client can lose its ports after it has already connected twice.

    The nastiest ordering, and the reason the retry could not simply stay
    where it was. This server binds the instrument port, accepts the
    control connection *and* the separate health connection, answers
    ``describe()`` — advertising a camera on a port it never bound — and
    only then exits with the port-collision status. When the client goes
    looking for that camera it finds a corpse.

    So a respawn is not just "call spawn again": the two connections
    already made belong to a process that no longer exists and have to be
    released, and the connect deadline has to start over rather than be
    inherited from the attempt that burned it. Both are what
    ``_spawn_and_connect`` does per attempt, and a session that works
    afterwards is the observable consequence — a leaked connection would
    strand a handler thread, and a shared deadline would make each retry a
    shorter straw than the last.
    """
    server_module, spawns = colliding_server

    with remote_instrument(
        plugin_names=[str(spawns), _ONE_COLLISION, "after_describing"],
        server_module=server_module,
    ) as microscope:
        assert microscope.ronchigram_camera is not None
        camera = microscope.ronchigram_camera
        camera.start()
        try:
            frame = camera.acquire_frame()
        finally:
            camera.stop()
        assert frame.metadata["device_id"] == _VENDOR_CAMERA_ID

    assert _spawn_count(spawns) == _SPAWNS_AFTER_ONE_COLLISION


def test_a_machine_that_keeps_taking_the_ports_says_so(colliding_server):
    """
    A collision that never resolves is still a bounded, explained failure.

    Retrying forever would turn a hostile machine into a hang, so the
    attempt budget is unchanged and the diagnostic it ends with is the
    one worth keeping: it names the *pattern* — something is claiming
    localhost ports faster than they can be used — rather than reporting
    exit status 4 as if the server had crashed. The point of the fix was
    to stop mistaking a curable collision for a fatal startup error, not
    to stop the incurable one from being reported.
    """
    server_module, spawns = colliding_server

    with pytest.raises(
        DeviceServerStartupError,
        match="claiming localhost ports faster",
    ), remote_instrument(
        plugin_names=[str(spawns), str(_ENDLESS_COLLISIONS), "before_connecting"],
        server_module=server_module,
    ):
        pytest.fail("a server that never binds must not yield a session")

    assert _spawn_count(spawns) == _PORT_RETRY_ATTEMPTS


class _UntouchedProcess:
    """A stand-in child that fails the test if the spawn step waits on it."""

    def wait(self, timeout: float | None = None) -> int:
        """
        Refuse to be waited on.

        Parameters
        ----------
        timeout : float | None
            Ignored; being called at all is the failure.

        Returns
        -------
        int
            Never; this always raises.

        Raises
        ------
        AssertionError
            Always. Spawning must not block on the child.
        """
        msg = (
            f"_start_server waited {timeout}s on a freshly spawned child; "
            f"the port-collision grace window was removed precisely so "
            f"that healthy startups pay nothing for it"
        )
        raise AssertionError(msg)

    def poll(self) -> int | None:
        """
        Report the child as still running.

        Returns
        -------
        int | None
            Always ``None``.
        """
        return None


def test_the_spawn_step_does_not_watch_the_child_at_all(monkeypatch):
    """
    The grace window is gone by construction, not by being short.

    Asserting this with a clock would be measuring the very thing that
    made the old design bad, so it is asserted structurally instead: the
    spawn step is handed a child that raises if anything waits on it, and
    it returns anyway. The removed constant is checked in the same breath,
    because a reintroduced ``_PORT_COLLISION_GRACE_S`` is exactly how this
    would come back — the detection belongs in the connect loop, which
    polls the process for its own reasons and therefore costs a healthy
    startup nothing.
    """
    assert not hasattr(remote_module, "_PORT_COLLISION_GRACE_S")

    process = _UntouchedProcess()
    monkeypatch.setattr(
        remote_module,
        "_spawn_server",
        lambda *args, **kwargs: process,  # noqa: ARG005 - stands in for a Popen
    )

    ports, authkey, spawned = _start_server("simulated", (), "irrelevant.module")

    assert spawned is process
    assert set(ports) == {"instrument"}
    assert len(authkey) == _AUTHKEY_BYTES
