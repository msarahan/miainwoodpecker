"""
The client can drive a device server it did not launch.

The companion to ``test_out_of_tree_server.py``, which proves the client
can launch an adapter it does not ship. This proves the harder case: an
adapter that **cannot be launched at all**, because its SDK exists only
inside another application that somebody else started. Gatan's is the
motivating one — Gatan's own documentation says DigitalMicrograph
functions cannot be executed from Python outside DigitalMicrograph — but
nothing here is Gatan-specific, and nothing here needs any Gatan software:
the bridge under test is ``gatan_bridge``'s ``simulated`` backend, which
is the same code path with the device objects swapped.

Two directions are exercised rather than one, and that is the finding
these tests encode. The *launch* really is inverted: this client cannot
start the server, cannot poll its exit status, and must not kill it. The
*socket* is not — which end dials is a firewall question, so both are
supported and both are tested here, against the same bridge, the same
protocol and the same device objects.

The failure modes get as much attention as the happy path, because they
are where an unlaunched server differs most: with no subprocess there is
no exit status to read, no stderr to quote, and no process to wait on, so
every diagnosis has to be built from what actually crossed the socket.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
import time

import numpy as np
import pytest

from miainwoodpecker.devices import Camera, Instrument
from miainwoodpecker.devices.gatan_bridge import start_bridge
from miainwoodpecker.devices.remote import (
    ACCEPT_TRANSPORT,
    CONNECT_TRANSPORT,
    SERVER_DISCONNECTED,
    SERVER_RESPONSIVE,
    AttachInvitation,
    DeviceServerAttachTimeoutError,
    _free_port,
    attached_instrument,
)
from miainwoodpecker.devices.rpc import (
    COMPATIBLE_PICKLE_PROTOCOL,
    TARGET_NAMES,
    RemoteConnectionLostError,
)

# Long enough that a loaded CI machine does not flake, short enough that a
# genuine "nobody is coming" answer arrives while a human is still watching.
_ATTACH_TIMEOUT_S = 30.0
# The timeout tests deliberately wait for nothing at all, so this is as
# small as it can be while still being longer than the accept machinery
# takes to bind and start its threads.
_NOBODY_IS_COMING_S = 1.0
# Ceiling for "that failure was bounded", generously above the timeout it
# is bounding so the assertion is about the bound existing, not its value.
_BOUNDED_FAILURE_CEILING_S = 15.0
# An energy offset the bridge does not start at, large enough that the
# simulated zero-loss peak moves by many channels.
_OFFSET_EV = 50.0
_DISPERSION_EV = 0.5


def _invitation(transport: str = ACCEPT_TRANSPORT) -> AttachInvitation:
    """Return a fresh rendezvous on free ports, as a client would publish."""
    return AttachInvitation(
        host="localhost",
        ports={name: _free_port() for name in TARGET_NAMES},
        authkey=secrets.token_bytes(32),
        transport=transport,
    )


@pytest.fixture
def accepting_bridge():
    """
    Start the simulated bridge in a thread and hand back its invitation.

    A thread rather than a subprocess on purpose, and it is not a
    shortcut: the real bridge runs *inside another application's process*,
    which is what a thread in the test process reproduces and what a
    subprocess does not. The subprocess variants below exist for the two
    tests that need to kill something.
    """
    invitation = _invitation(ACCEPT_TRANSPORT)
    handle = start_bridge(
        invitation,
        backend="simulated",
        dispersion_ev=_DISPERSION_EV,
    )
    handle.wait_ready()
    yield invitation
    handle.join(timeout_s=5.0)


def test_an_unlaunchable_device_server_can_be_driven_end_to_end(accepting_bridge):
    """
    A bridge that dials in serves the whole client contract unchanged.

    The point of the test is everything it does *not* have to say. There
    is no new device API here: the same ``Camera`` protocol, the same
    ``Instrument`` core, the same ``describe()``-first handshake, the
    same ``RemoteInstrumentDevices``. A caller written against
    ``remote_instrument`` works against this one by changing which context
    manager it opens, which is the whole claim being made — that the
    inbound path is a transport direction rather than a second stack.
    """
    with attached_instrument(accepting_bridge, timeout_s=_ATTACH_TIMEOUT_S) as scope:
        assert isinstance(scope.eels_camera, Camera)
        assert isinstance(scope.instrument, Instrument)
        description = scope.instrument.describe()
        assert list(description["targets"]) == ["eels_camera"]
        # A spectrometer is not a microscope: one control, and honestly
        # no others rather than stubs that would pretend.
        assert list(description["controls"]) == ["energy_offset"]
        assert scope.scanner is None
        assert scope.ronchigram_camera is None

        camera = scope.eels_camera
        assert camera is not None
        camera.start()
        try:
            frame = camera.acquire_frame()
        finally:
            camera.stop()
        assert frame.data.ndim == 2  # noqa: PLR2004 - a spectrum image
        assert frame.metadata["camera_type"] == "eels"
        # The flag that separates a detector from a webcam, and the
        # reason the commodity-camera server sets it False.
        assert frame.metadata["photometrically_linear"] is True


def test_the_instrument_control_reaches_the_camera_through_the_bridge(
    accepting_bridge,
):
    """
    Driving the spectrometer moves the peak in the camera's frames.

    Worth asserting rather than trusting, because it is the one thing a
    two-target bridge can get wrong in a way that no protocol check would
    catch: the ``instrument`` target and the ``eels_camera`` target are
    separate connections, separate handler threads, and separate remote
    objects, and a bridge that wired them to *different* spectrometer
    instances would answer every call correctly and quietly record the
    wrong energy axis on every frame.
    """
    with attached_instrument(accepting_bridge, timeout_s=_ATTACH_TIMEOUT_S) as scope:
        camera = scope.eels_camera
        assert camera is not None
        camera.start()
        try:
            before = camera.acquire_frame()
            scope.instrument.set_energy_offset_ev(_OFFSET_EV)
            assert scope.instrument.energy_offset_ev() == _OFFSET_EV
            after = camera.acquire_frame()
        finally:
            camera.stop()
    moved = int(np.argmax(before.data.mean(axis=0))) - int(
        np.argmax(after.data.mean(axis=0)),
    )
    assert moved == pytest.approx(_OFFSET_EV / _DISPERSION_EV, abs=2)
    # And the frame says what it was taken under, per frame rather than
    # per session, because a drift-tube sweep changes it between frames.
    assert after.metadata["energy_offset_ev"] == _OFFSET_EV
    assert after.metadata["calibration"]["x"]["units"] == "eV"


def test_the_client_can_dial_a_bridge_that_listens():
    """
    The other socket direction, same everything else.

    This is the correction to the assumption the work started from. What
    an in-application adapter genuinely inverts is *process ownership* —
    we cannot launch it, cannot poll it, must not kill it. Which end opens
    the socket is a separate and free choice, and this direction is the
    one to prefer where a network allows it: the host application is
    started once and outlives many runs of this client, so a listening
    bridge is simply there whenever a session wants it. It is also the
    long-standing arrangement in this field, SerialEM's DigitalMicrograph
    plug-in being the obvious precedent.
    """
    invitation = _invitation(CONNECT_TRANSPORT)
    handle = start_bridge(invitation, backend="simulated")
    assert handle.wait_ready()
    try:
        with attached_instrument(invitation, timeout_s=_ATTACH_TIMEOUT_S) as scope:
            assert list(scope.instrument.describe()["targets"]) == ["eels_camera"]
            assert scope.instrument.check_health().state == SERVER_RESPONSIVE
    finally:
        handle.join(timeout_s=5.0)


def test_a_bridge_that_never_dials_in_fails_with_a_diagnosis(tmp_path):
    """
    Waiting for a server nobody started ends, and says what to check.

    The realistic first failure of the inbound path, and the one where
    this client has least to work with: nothing was launched, so there is
    no exit status, no stderr, and no process to inspect. All the message
    can honestly do is name the address it waited on and list what to go
    and look at, which is what it must therefore actually do.
    """
    invitation = _invitation(ACCEPT_TRANSPORT)
    started = time.monotonic()
    with (
        pytest.raises(DeviceServerAttachTimeoutError) as failure,
        attached_instrument(
            invitation,
            publish_to=tmp_path / "attach.json",
            timeout_s=_NOBODY_IS_COMING_S,
        ),
    ):
        pass  # pragma: no cover - the context manager must not open
    assert time.monotonic() - started < _BOUNDED_FAILURE_CEILING_S
    message = str(failure.value)
    assert "no device server dialled in" in message
    assert str(invitation.ports["instrument"]) in message
    # And it does not pretend to know about a process it never started.
    assert "exited with status" not in message


def test_a_bridge_with_the_wrong_authkey_is_named_as_such(tmp_path):
    """
    An authentication failure must not present as "nobody came".

    The single most likely misconfiguration of a published rendezvous is
    a stale copy of the invitation, and the least useful possible
    rendering of it is a bare timeout: the bridge *is* running, it *can*
    reach this client, and the operator is told to check none of the
    things that are wrong. So failed handshakes are counted while the
    wait continues, and named in the timeout instead.

    Continuing to wait rather than failing on the first bad handshake is
    also deliberate: on an instrument network anything may connect to a
    listening port, and one stray probe must not end a session that the
    right bridge is about to join.
    """
    invitation = _invitation(ACCEPT_TRANSPORT)
    stale = AttachInvitation(
        host=invitation.host,
        ports=dict(invitation.ports),
        authkey=secrets.token_bytes(32),  # a different key: the stale copy
        transport=ACCEPT_TRANSPORT,
    )
    handle = start_bridge(stale, backend="simulated", timeout_s=_ATTACH_TIMEOUT_S)
    handle.wait_ready()
    with (
        pytest.raises(DeviceServerAttachTimeoutError) as failure,
        attached_instrument(
            invitation,
            publish_to=tmp_path / "attach.json",
            timeout_s=_NOBODY_IS_COMING_S,
        ),
    ):
        pass  # pragma: no cover - the context manager must not open
    message = str(failure.value)
    assert "failed authentication" in message
    assert "different authkey" in message


def test_a_bridge_that_dies_mid_session_is_reported_honestly(tmp_path):
    """
    A dead bridge is "disconnected", never "the process exited".

    The distinction this whole lifecycle abstraction exists for. On the
    spawn path, "the device server process was killed by SIGKILL" is a
    fact this client read from a ``Popen`` it owns. Here there is no such
    handle, and the same wording would be actively misleading: the bridge
    lives inside a host application that is very probably still running,
    quite possibly on a different computer, so a broken socket is evidence
    of a broken socket and nothing more.

    A subprocess rather than a thread for this one, because the point is
    to kill something and a thread cannot be killed. The bridge is the
    same module either way.
    """
    invitation = _invitation(ACCEPT_TRANSPORT)
    config = invitation.write_to(tmp_path / "attach.json")
    bridge = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "miainwoodpecker.devices.gatan_bridge",
            "--config",
            str(config),
            "--backend",
            "simulated",
        ],
    )
    try:
        with attached_instrument(invitation, timeout_s=_ATTACH_TIMEOUT_S) as scope:
            camera = scope.eels_camera
            assert camera is not None
            camera.start()
            assert scope.instrument.check_health().state == SERVER_RESPONSIVE

            bridge.kill()
            bridge.wait(timeout=10)

            with pytest.raises(RemoteConnectionLostError) as failure:
                camera.acquire_frame()
            message = str(failure.value)
            assert "eels_camera.acquire_frame()" in message
            assert "attached device server" in message
            assert "did not launch it" in message
            assert "exited with status" not in message
            assert "killed by" not in message

            health = scope.instrument.check_health()
            assert health.state == SERVER_DISCONNECTED
            assert health.exit_status is None
            assert "bridge's own log" in health.detail
    finally:
        if bridge.poll() is None:  # pragma: no cover - only if the kill missed
            bridge.kill()
            bridge.wait(timeout=10)


def test_the_invitation_round_trips_through_a_published_file(tmp_path):
    """
    The rendezvous is published, because it cannot be passed on a command line.

    The spawn path hands ports and an authkey to a child in its argv,
    where nobody ever sees them. Nothing can do that here: the far end is
    started by a person, inside another application, possibly on another
    machine. So the same three facts have to become a file that an
    operator copies — which makes the file format part of the contract,
    and worth pinning rather than leaving to whatever ``json.dumps``
    happened to produce.
    """
    invitation = _invitation(ACCEPT_TRANSPORT)
    path = invitation.write_to(tmp_path / "attach.json")
    restored = AttachInvitation.read_from(path)
    assert restored == invitation
    assert restored.authkey == invitation.authkey
    # The instructions name a direction and the ports, because the person
    # reading them has to start the other end by hand.
    instructions = invitation.operator_instructions(path)
    assert "LISTENING" in instructions
    assert str(invitation.ports["instrument"]) in instructions
    # And the secret is not in them: it lives in the 0600 file only.
    assert invitation.authkey.hex() not in instructions


def test_a_configuration_from_another_version_is_refused(tmp_path):
    """
    A rendezvous file this code does not understand fails loudly.

    Guessing would be worse than failing here. A misread port or a
    truncated key produces a bridge that authenticates against nothing and
    an operator watching a silent timeout with no idea why — the exact
    failure the version field exists to convert into a sentence.
    """
    path = tmp_path / "attach.json"
    path.write_text('{"version": 99, "host": "localhost"}', encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        AttachInvitation.read_from(path)


def test_a_connect_attach_refuses_to_invent_the_far_ends_ports():
    """
    Only the end that binds may choose the ports, and this end knows it.

    Silently picking free ports for a ``connect`` attach would produce a
    client dialling addresses nothing is listening on, and a timeout whose
    message would be about the wrong thing entirely.
    """
    with (
        pytest.raises(ValueError, match="cannot derive them"),
        attached_instrument(transport=CONNECT_TRANSPORT),
    ):
        pass  # pragma: no cover - the context manager must not open


def test_outgoing_calls_are_capped_at_a_protocol_an_old_interpreter_can_read():
    """
    The attach path defaults to pickle protocol 4, and that is load-bearing.

    ``Connection.send`` serialises with the *sender's* default protocol,
    which on 3.8 and later is 5, and Python 3.7 — which Gatan's FAQ names
    for the GMS embedded environment — cannot read protocol 5 at all.
    Every call this client sent would fail to unpickle while every reply
    decoded perfectly, which presents as a broken server rather than a
    version mismatch. Capping costs nothing (protocol 5's addition is
    out-of-band buffers, which plain ``pickle.dumps`` never emits) and is
    exactly the kind of default that must not be changed by accident.
    """
    assert COMPATIBLE_PICKLE_PROTOCOL == 4  # noqa: PLR2004 - the number is the point
    # 4 is what Python 3.7 reports as HIGHEST_PROTOCOL, which is why it is
    # the cap rather than 3 or 5.
    assert COMPATIBLE_PICKLE_PROTOCOL < 5  # noqa: PLR2004 - see above
