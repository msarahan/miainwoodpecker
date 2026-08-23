"""
Integration: a microscope started from its configuration file, for real.

``tests/unit/test_broker_config.py`` covers the composition against
stubs. This covers the thing the stubs stand in for - two device-server
subprocesses started from one file, checked against what that file says
the instrument has, and served to a broker client as one instrument. The
shipped ``instruments/simulator.toml`` is the fixture, so the file
operators are told to copy is the file under test.

Skipped automatically unless the ``device`` optional dependency group is
installed (``pixi run -e device pytest tests/integration``).
"""

import dataclasses
import logging
import pathlib

import pytest

pytest.importorskip("nion.usim_device", reason="requires the 'device' extra")

from miainwoodpecker.broker.app import (
    configured_targets,
    open_configured_instrument,
    serve_targets,
)
from miainwoodpecker.broker.remote import connect_broker
from miainwoodpecker.instrument_config import (
    DeviceConfig,
    InstrumentConfig,
    InstrumentConfigError,
    load_instrument_config,
)

ABSENT_CAMERA = DeviceConfig(
    target="camera:99",
    served_as="camera:99",
    description="a camera this instrument does not have",
)

SIMULATOR = (
    pathlib.Path(__file__).resolve().parents[2] / "instruments" / "simulator.toml"
)


def _everything_on() -> InstrumentConfig:
    """
    Return the shipped simulator configuration with every server enabled.

    The file ships with its second adapter switched off, because "the
    simulator" an operator means is the Nion one. Turning it on is what
    makes this a two-process test, and doing it here rather than in a
    second file keeps the shipped example the one under test.

    Returns
    -------
    InstrumentConfig
        Both servers enabled.
    """
    config = load_instrument_config(SIMULATOR)
    return dataclasses.replace(
        config,
        servers=tuple(
            dataclasses.replace(server, enabled=True) for server in config.servers
        ),
    )


def test_two_device_servers_are_served_to_a_client_as_one_instrument():
    """
    The whole point, exercised with real subprocesses.

    A Nion server and a spectrum server know nothing about each other -
    separate processes, separate protocols on the inside, separate
    ``instrument`` targets. A client connecting to the broker should see
    one microscope with a scan unit, two cameras and a detector on it,
    and should not be able to tell which process any of them came from.
    """
    config = _everything_on()
    with open_configured_instrument(config) as sessions:
        assert set(sessions) == {"column", "edx"}
        targets = configured_targets(config, sessions)
        with serve_targets(targets) as invitation:
            client = connect_broker(invitation.address(), invitation.authkey)
            try:
                assert set(client.targets()) == {
                    "instrument",
                    "scanner",
                    "ronchigram_camera",
                    "eels_camera",
                    "spectrum_detector",
                }
                described = client.describe()
            finally:
                client.close()
    assert described["spectrum_detector"].kind == "spectrum"
    assert described["scanner"].channel_names, "the scan unit's channels came through"
    # The column's instrument, not the spectrum server's: only one of the
    # two answers to stage and defocus, and controls_column says which.
    assert "stage_position" in described["instrument"].controls


def test_hardware_the_file_promises_and_the_server_lacks_stops_startup():
    """
    The check that needs a file, run against a real server.

    Nothing the device layer reports can distinguish "this instrument
    has no third camera" from "this instrument's third camera did not
    come up". The file can, and this is that difference happening to an
    actual nionswift-usim session rather than to a stub.
    """
    config = load_instrument_config(SIMULATOR)
    column = config.servers[0]
    absent = dataclasses.replace(
        column,
        devices=(*column.devices, ABSENT_CAMERA),
    )
    config = dataclasses.replace(config, servers=(absent,))
    with open_configured_instrument(config) as sessions:
        with pytest.raises(InstrumentConfigError, match="camera:99") as error:
            configured_targets(config, sessions)
        assert "ronchigram_camera" in str(error.value), (
            "the message must list what the server did serve"
        )


def test_a_device_the_file_does_not_list_is_dropped_and_named(caplog):
    """
    An enumeration is authoritative, against a server that serves more.

    usim registers both cameras. A file listing only the scan unit gets
    only the scan unit - and a warning naming the two cameras it left
    behind, because an unreachable detector should never be a silent
    outcome.
    """
    config = load_instrument_config(SIMULATOR)
    column = config.servers[0]
    scan_only = dataclasses.replace(column, devices=column.devices[:1])
    config = dataclasses.replace(config, servers=(scan_only,))
    with (
        open_configured_instrument(config) as sessions,
        caplog.at_level(logging.WARNING, logger="miainwoodpecker.broker.app"),
    ):
        targets = configured_targets(config, sessions)
    assert set(targets) == {"instrument", "scanner"}
    assert "ronchigram_camera" in caplog.text
    assert "eels_camera" in caplog.text
