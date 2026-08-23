"""
Starting a whole microscope from its configuration file.

The wiring between :mod:`miainwoodpecker.instrument_config` and the
broker: which processes get started, what each served device ends up
called, and what happens when the hardware a file enumerates does not
turn up. Exercised against stub containers and a stubbed spawn, because
what is under test is the composition - a real device server would only
add subprocesses to a test about bookkeeping.
"""

from __future__ import annotations

import contextlib
import logging
import typing

import pytest

from miainwoodpecker.broker.app import (
    _parse_args,
    configured_targets,
    open_configured_instrument,
)
from miainwoodpecker.instrument_config import (
    SCHEMA_VERSION,
    InstrumentConfig,
    InstrumentConfigError,
    parse_instrument_config,
)

if typing.TYPE_CHECKING:
    from collections.abc import Iterator

_NION = "miainwoodpecker.devices.nion_server"
_DECTRIS = "miainwoodpecker.devices.dectris_server"


class _Stub:
    """A stand-in for one device, identifiable by name."""

    def __init__(self, name: str) -> None:
        self.name = name


class _StubDevices:
    """
    A device container of the shape ``remote_instrument`` yields.

    Only the parts the broker reads: the instrument, an optional
    scanner, and the two accessors that answer "what is actually here".
    """

    def __init__(self, *, scanner=None, cameras=(), spectra=()) -> None:
        self.instrument = _Stub("instrument")
        self.scanner = _Stub(scanner) if scanner else None
        self._cameras = {name: _Stub(name) for name in cameras}
        self._spectra = {name: _Stub(name) for name in spectra}

    def cameras(self) -> dict:
        """
        Return every camera served, by served name.

        Returns
        -------
        dict
            Served name to camera.
        """
        return dict(self._cameras)

    def spectrum_detectors(self) -> dict:
        """
        Return every spectrum detector served, by served name.

        Returns
        -------
        dict
            Served name to detector.
        """
        return dict(self._spectra)


def _two_adapter_config(**server_overrides: object) -> InstrumentConfig:
    """
    Return the SuperSTEM 3 shape: a Nion column plus a renamed detector.

    Parameters
    ----------
    **server_overrides : object
        Keys to add to the detector's server table.

    Returns
    -------
    InstrumentConfig
        Parsed, so every rule the schema enforces has already run.
    """
    detector = {
        "name": "ela",
        "module": _DECTRIS,
        "device": [{"target": "eels_camera", "served_as": "camera"}],
    }
    detector.update(server_overrides)
    return parse_instrument_config(
        {
            "schema": SCHEMA_VERSION,
            "name": "Two adapters",
            "server": [
                {
                    "name": "column",
                    "module": _NION,
                    "controls_column": True,
                    "device": [
                        {"target": "scanner"},
                        {"target": "ronchigram_camera"},
                    ],
                },
                detector,
            ],
        },
    )


def _sessions(**extra: object) -> dict:
    """
    Return open sessions matching :func:`_two_adapter_config`.

    Parameters
    ----------
    **extra : object
        Sessions to add or replace.

    Returns
    -------
    dict
        Server name to device container.
    """
    sessions = {
        "column": _StubDevices(scanner="scanner", cameras=["ronchigram_camera"]),
        "ela": _StubDevices(cameras=["camera"]),
    }
    sessions.update(extra)
    return sessions


def _record_spawns(monkeypatch: pytest.MonkeyPatch) -> list:
    """
    Replace the device-server spawn with a stub that records its arguments.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The fixture.

    Returns
    -------
    list
        Filled with ``(server_module, (backend, plugin_names))`` in the
        order the servers were started.
    """
    started = []

    @contextlib.contextmanager
    def _spawn(*, backend, plugin_names, server_module) -> Iterator[_StubDevices]:
        started.append((server_module, (backend, tuple(plugin_names))))
        yield _StubDevices(
            scanner="scanner",
            cameras=["ronchigram_camera"] if server_module == _NION else ["camera"],
        )

    monkeypatch.setattr("miainwoodpecker.broker.app.remote_instrument", _spawn)
    return started


def test_two_adapters_are_served_as_one_instrument():
    """
    The case the configuration exists for.

    A Nion column and a DECTRIS detector are separate processes with no
    knowledge of one another; the file is what makes them one target map
    with one ``instrument`` in it.
    """
    sessions = _sessions()
    targets = configured_targets(_two_adapter_config(), sessions)
    assert set(targets) == {
        "instrument",
        "scanner",
        "ronchigram_camera",
        "eels_camera",
    }
    assert targets["instrument"] is sessions["column"].instrument


def test_a_detector_is_reachable_under_the_name_this_column_gives_it():
    """
    ``served_as`` is applied here, and the handle behind it must survive.

    Renaming the key while handing back the wrong device would be a
    quiet mis-wiring: everything would work, against the wrong detector.
    """
    sessions = _sessions()
    targets = configured_targets(_two_adapter_config(), sessions)
    assert targets["eels_camera"] is sessions["ela"].cameras()["camera"]


def test_the_instrument_target_comes_from_the_server_that_owns_the_column():
    """
    Every adapter has one, and taking the wrong one misplaces the stage.

    The DECTRIS server's ``instrument`` answers ``describe`` and
    ``shutdown`` and knows nothing about a column, so a broker serving
    it would accept a stage move and fail at the device.
    """
    sessions = _sessions()
    targets = configured_targets(_two_adapter_config(), sessions)
    assert targets["instrument"] is not sessions["ela"].instrument


def test_hardware_the_file_promises_and_the_server_lacks_stops_startup():
    """
    The check nothing else in the stack can make.

    A Nion server whose EELS camera failed to register reports a
    perfectly consistent instrument with one fewer camera, and every
    layer above serves it happily. Only a file that says the camera
    exists turns that into an error - and it has to be an error rather
    than a warning, because the alternative is a session that looks
    normal until somebody reaches for the spectrometer.
    """
    sessions = _sessions(ela=_StubDevices())
    with pytest.raises(InstrumentConfigError, match="eels_camera") as error:
        configured_targets(_two_adapter_config(), sessions)
    assert "no devices" in str(error.value), "the message must say what did turn up"


def test_a_device_the_file_does_not_list_is_dropped_and_named(caplog):
    """
    An enumeration is authoritative or it is decoration.

    Serving the extra anyway would mean "enumerates the hardware" really
    meant "enumerates some of it, and also whatever turns up". Dropping
    it silently would be worse still, so it is logged at warning level
    with the name to paste into the file.
    """
    sessions = _sessions(
        column=_StubDevices(
            scanner="scanner",
            cameras=["ronchigram_camera", "eels_camera"],
        ),
    )
    with caplog.at_level(logging.WARNING, logger="miainwoodpecker.broker.app"):
        targets = configured_targets(_two_adapter_config(), sessions)
    # eels_camera IS in the map, but from the ELA. The column's own is
    # the one dropped, and that collision is exactly why dropping the
    # unlisted device is the safe answer rather than the rude one.
    assert targets["eels_camera"] is sessions["ela"].cameras()["camera"]
    assert "eels_camera" in caplog.text
    assert "column" in caplog.text


def test_a_switched_off_server_is_neither_started_nor_expected(monkeypatch):
    """
    ``enabled = false`` keeps a record of hardware without opening it.

    The EDX detector on SuperSTEM 2 has no adapter yet; the file should
    still say the microscope has one, and the broker should not try.
    """
    started = _record_spawns(monkeypatch)
    config = _two_adapter_config(enabled=False)
    with open_configured_instrument(config) as sessions:
        assert set(sessions) == {"column"}
        assert set(configured_targets(config, sessions)) == {
            "instrument",
            "scanner",
            "ronchigram_camera",
        }
    assert [module for module, _ in started] == [_NION]


def test_each_server_is_started_with_its_own_module_backend_and_plugins(monkeypatch):
    """
    One file, several adapters, each launched on its own terms.

    The DECTRIS server's ``--plugin`` is a control-unit address and the
    Nion server's is a plug-in module; passing one server's arguments to
    the other is the mis-wiring this asserts against.
    """
    started = _record_spawns(monkeypatch)
    config = _two_adapter_config(backend="hardware", plugins=["192.168.1.10"])
    with open_configured_instrument(config):
        pass
    assert started == [
        (_NION, ("simulated", ())),
        (_DECTRIS, ("hardware", ("192.168.1.10",))),
    ]


def test_a_server_that_will_not_start_names_itself_and_its_file(monkeypatch):
    """
    A bare startup error does not say which of four adapters failed.

    This is a command run at an instrument, and "could not start server
    'ela'" is the difference between checking a detector and checking
    everything.
    """

    @contextlib.contextmanager
    def _refuse(*, backend, plugin_names, server_module) -> Iterator[_StubDevices]:
        del backend, plugin_names
        if server_module == _DECTRIS:
            message = "no detector answered"
            raise RuntimeError(message)
        yield _StubDevices(scanner="scanner", cameras=["ronchigram_camera"])

    monkeypatch.setattr("miainwoodpecker.broker.app.remote_instrument", _refuse)
    with (
        pytest.raises(InstrumentConfigError, match="server 'ela'") as error,
        open_configured_instrument(_two_adapter_config()),
    ):
        pass
    assert "no detector answered" in str(error.value)


def test_a_failure_part_way_through_still_closes_what_started(monkeypatch):
    """
    The failure mode worth engineering against: a half-started column.

    Every ``remote_instrument`` parks its instrument on the way out, so
    the requirement here is only that the ones already open are actually
    exited - which is what stacking them buys, and what a function
    returning handles would not.
    """
    closed = []

    @contextlib.contextmanager
    def _fail_on_the_second(
        *,
        backend,
        plugin_names,
        server_module,
    ) -> Iterator[_StubDevices]:
        del backend, plugin_names
        if server_module == _DECTRIS:
            message = "no detector answered"
            raise RuntimeError(message)
        try:
            yield _StubDevices(scanner="scanner", cameras=["ronchigram_camera"])
        finally:
            closed.append(server_module)

    monkeypatch.setattr(
        "miainwoodpecker.broker.app.remote_instrument",
        _fail_on_the_second,
    )
    with (
        pytest.raises(InstrumentConfigError),
        open_configured_instrument(_two_adapter_config()),
    ):
        pass
    assert closed == [_NION]


def test_a_configuration_and_a_single_server_command_line_are_alternatives():
    """
    Either precedence would be a wrong answer somebody debugs at an instrument.

    Honour the file and ``--backend hardware`` silently does nothing;
    honour the flag and one word overrides the backend of every server
    in the file at once. There is no reading of the two together that is
    not a mistake, so argparse refuses it.
    """
    with pytest.raises(SystemExit):
        _parse_args(["--config", "instrument.toml", "--backend", "hardware"])
    with pytest.raises(SystemExit):
        _parse_args(["--config", "instrument.toml", "--plugin", "usim"])


def test_the_single_server_command_line_still_defaults_as_it_did(monkeypatch):
    """
    ``--backend`` moved to a None default so "was it given?" is answerable.

    That is exactly the kind of change that silently alters what a bare
    command does, so what a bare command does is pinned here - with the
    environment cleared, since both defaults read it and a developer who
    exports one should not fail this.
    """
    monkeypatch.delenv("MIAINWOODPECKER_BACKEND", raising=False)
    monkeypatch.delenv("MIAINWOODPECKER_INSTRUMENT", raising=False)
    arguments = _parse_args([])
    assert arguments.config is None
    assert arguments.backend == "simulated"
    assert arguments.server_module == _NION
