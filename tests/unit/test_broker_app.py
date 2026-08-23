"""
The launcher: one command turning a device session into a served broker.

Exercised against a stub device container rather than a real device
server, because what is being tested is the wiring - which targets get
served, where the connection details go, and what happens on the way out
- and none of that is about Nion's stack being installed.
"""

import json
import logging
import pathlib
import stat
import sys

import pytest

from miainwoodpecker.broker.app import instrument_targets, serve_instrument
from miainwoodpecker.broker.invitation import DEFAULT_FILENAME, BrokerInvitation
from miainwoodpecker.broker.remote import connect_broker


class _Stub:
    """A stand-in for one device, identifiable by name."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.parked = 0

    def park(self) -> None:
        """Record that the instrument was parked."""
        self.parked += 1

    def available_controls(self) -> tuple[str, ...]:
        """
        Return the controls this stub implements.

        Returns
        -------
        tuple[str, ...]
            None, which is a real answer for a detector-only rig.
        """
        return ()


class _StubDevices:
    """
    A device container of the shape ``remote_instrument`` yields.

    Only the parts :func:`instrument_targets` reads: the instrument, an
    optional scanner, and the two accessors that answer "what is
    actually here" rather than "which named slots exist".
    """

    def __init__(self, *, scanner: bool = True, extras: bool = False) -> None:
        self.instrument = _Stub("instrument")
        self.scanner = _Stub("scanner") if scanner else None
        self._cameras = {"eels_camera": _Stub("eels_camera")}
        if extras:
            self._cameras["camera:2"] = _Stub("camera:2")

    def cameras(self) -> dict:
        """
        Return every camera served, by target name.

        Returns
        -------
        dict
            Target name to camera.
        """
        return dict(self._cameras)

    def spectrum_detectors(self) -> dict:
        """
        Return every spectrum detector served, by target name.

        Returns
        -------
        dict
            Target name to detector.
        """
        return {"spectrum_detector": _Stub("spectrum_detector")}


def test_targets_come_from_what_the_instrument_actually_serves():
    """
    Extra cameras are served, not truncated to the named slots.

    ``cameras()`` is the accessor that answers "what is here"; reading
    the three named attributes instead would drop a second commodity
    camera on the floor and give a client no way to lease it.
    """
    targets = instrument_targets(_StubDevices(extras=True))
    assert set(targets) == {
        "instrument",
        "scanner",
        "eels_camera",
        "camera:2",
        "spectrum_detector",
    }


def test_a_detector_only_instrument_is_served_without_a_scanner():
    """
    No scan unit is a real configuration, not a missing one.

    A direct detector driven on its own has no scanner target, and the
    broker must not invent one - a client asking for it should get the
    same KeyError it would from the device server.
    """
    targets = instrument_targets(_StubDevices(scanner=False))
    assert "scanner" not in targets
    assert "eels_camera" in targets


def test_serving_publishes_an_invitation_a_client_can_connect_with():
    """
    The published file is enough on its own to reach the broker.

    The port is chosen by the OS, so nobody can be told it in advance -
    which is the whole reason the invitation exists rather than a
    documented port number.
    """
    devices = _StubDevices()
    with serve_instrument(devices) as invitation:
        assert invitation.port > 0
        client = connect_broker(invitation.address(), invitation.authkey)
        try:
            assert set(client.targets()) == set(instrument_targets(devices))
        finally:
            client.close()


def test_the_invitation_lands_in_a_directory_as_broker_json(tmp_path):
    """A directory gets the conventional filename inside it, not a rename."""
    with serve_instrument(_StubDevices(), publish_to=tmp_path) as invitation:
        published = tmp_path / DEFAULT_FILENAME
        assert published.exists()
        assert BrokerInvitation.read_from(tmp_path) == invitation
        assert BrokerInvitation.read_from(published) == invitation


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file modes; on Windows the file inherits the directory ACL",
)
def test_the_published_invitation_is_not_world_readable(tmp_path):
    """
    The authkey is in that file, and an instrument PC is a shared login.

    0600 is not theatre here: anyone who can read this file can drive
    the microscope.
    """
    with serve_instrument(_StubDevices(), publish_to=tmp_path):
        mode = (tmp_path / DEFAULT_FILENAME).stat().st_mode
    assert not mode & (stat.S_IRGRP | stat.S_IROTH)


def test_the_authkey_is_generated_rather_than_shared_between_runs():
    """
    Two brokers do not accept each other's clients.

    A hard-coded key on an instrument PC is a key everybody on site
    knows, so the default generates one - which also means a stale
    invitation from yesterday's run cannot connect to today's.
    """
    with serve_instrument(_StubDevices()) as first, serve_instrument(
        _StubDevices(),
    ) as second:
        assert first.authkey != second.authkey


def test_shutdown_parks_the_instrument_however_it_exits():
    """
    An exception on the way out still leaves the beam somewhere chosen.

    Parking blanks the beam where one exists, so this is the difference
    between a crashed session and a damaged specimen.
    """
    devices = _StubDevices()
    message = "deliberate"
    with pytest.raises(RuntimeError, match=message), serve_instrument(devices):
        raise RuntimeError(message)
    assert devices.instrument.parked >= 1


def test_shutdown_survives_an_instrument_that_cannot_be_parked(caplog):
    """
    A park that fails is logged loudly and does not replace the exit.

    Measured case, not hypothetical: a console interrupt on Windows
    reaches every process in the group, so the device server can already
    be gone when the broker tries to park. Raising there buries the
    reason for the shutdown under a traceback about a server nothing can
    reach any more - but an instrument that could not be parked is worth
    knowing about, so it is logged at error level.
    """
    devices = _StubDevices()

    def refuse() -> None:
        message = "device server process exited"
        raise ConnectionResetError(message)

    devices.instrument.park = refuse
    with caplog.at_level(logging.ERROR), serve_instrument(devices):
        pass
    assert "could not park the instrument" in caplog.text


def test_a_stale_invitation_file_is_refused_rather_than_guessed_at(tmp_path):
    """
    An unreadable version fails loudly instead of authenticating badly.

    Guessing produces a client that authenticates against nothing and an
    operator watching a timeout with no idea why.
    """
    path = pathlib.Path(tmp_path) / DEFAULT_FILENAME
    path.write_text(
        json.dumps({"version": 99, "host": "localhost", "port": 1, "authkey": "00"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="version"):
        BrokerInvitation.read_from(path)
