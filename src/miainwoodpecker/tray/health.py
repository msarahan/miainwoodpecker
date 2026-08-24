"""
Whether the pieces underneath a served instrument are answering.

A broker over a configured microscope is a broker over *several*
processes - a Nion column, a DECTRIS ELA on the spectrometer, a camera
server - and from outside it they are invisible. The broker's own
startup log names them once and then scrolls away, which is fine for a
terminal somebody is watching and no use at all to a tray icon that has
to answer "is the spectrometer there?" at four in the afternoon.

This module turns what the broker already knows into that answer. Three
sources, and none of them is a new device call:

- :meth:`~miainwoodpecker.broker.interface.InstrumentBroker.describe`
  is read once at startup, when the instrument is quiet, and carries an
  ``error`` per target for the questions a device refused to answer.
  That is an adapter half-working, and it is the state most worth
  surfacing because everything above it looks merely poor.
- :meth:`~miainwoodpecker.broker.interface.InstrumentBroker.targets`
  carries what each one is *doing*, and the ``error`` that stopped a
  live loop - a camera that timed out mid-session leaves the loop dead
  and the tile blank, and nothing else says so.
- the instrument configuration, when there is one, says which **server**
  each target came from. That is what makes this a health check on the
  device servers rather than on a flat list of names: five targets under
  three headings is the shape of the machine, and a heading gone red
  names the process to restart.

**Nothing here polls the hardware, and that is deliberate.** The obvious
health check - ask each detector for a frame every few seconds - is a
second caller on a device whose live loop may be mid-pass, which is the
interleaving :mod:`miainwoodpecker.broker` exists to prevent. It would
also take a lease, or fail because it could not. So the strongest claim
this module makes is "the broker says it is there and nothing has
reported it broken", which is honest, and which the wording of every
:class:`Condition` sticks to.
"""

from __future__ import annotations

import enum
import typing
from dataclasses import dataclass

from miainwoodpecker.devices.rpc import INSTRUMENT_TARGET

if typing.TYPE_CHECKING:
    from collections.abc import Mapping

    from miainwoodpecker.broker.interface import TargetDescription, TargetState
    from miainwoodpecker.instrument_config import InstrumentConfig

UNCONFIGURED_SERVER = "device server"
"""
What the one server is called when no configuration named it.

``--backend``/``--server-module`` describes a single adapter and gives
it no name, so there is one heading and it is this. A configured
instrument uses the names in its file instead, which are the names its
log lines and its error messages already use.
"""


class Condition(enum.IntEnum):
    """
    How bad the news is, ordered so that the worst of several is a ``max``.

    The ordering is the point rather than an artefact: a server is as
    healthy as its unhealthiest device, and an instrument as its
    unhealthiest server, and both of those are one call to :func:`max`.

    ``HEALTHY`` is present, with nothing having reported it broken - not
    "verified working", for the reason this module's docstring gives at
    length. ``DEGRADED`` is answering, but not all of it: a camera that
    would not say what binning it supports is here, and the window will
    offer an empty menu and look merely poor. ``FAILED`` is something
    that was running and has stopped, which is a live loop that died
    with an exception and left a tile blank. ``UNREACHABLE`` is the
    broker itself not answering, which is worth its own state because
    the process to go and look at is a different one.
    """

    HEALTHY = 0
    DEGRADED = 1
    FAILED = 2
    UNREACHABLE = 3


@dataclass(frozen=True)
class TargetHealth:
    """
    One device's condition, as the broker is able to report it.

    Attributes
    ----------
    name : str
        The target name, as clients lease it.
    label : str
        What the device calls itself, for an operator who knows the
        detector by its own name rather than by its slot.
    kind : str
        :func:`~miainwoodpecker.devices.rpc.target_kind` of the name.
    condition : Condition
        How bad the news is.
    detail : str
        One line saying what it is doing, and what is wrong if
        something is.
    is_live : bool
        Whether a live loop is running on it.
    fps : float | None
        The loop's recent rate, or None when it is not running.
    holder : str | None
        Who holds a lease on it, or None if nobody does.
    """

    name: str
    label: str
    kind: str
    condition: Condition
    detail: str
    is_live: bool = False
    fps: float | None = None
    holder: str | None = None


@dataclass(frozen=True)
class ServerHealth:
    """
    One device server, and the devices it was supposed to bring.

    Attributes
    ----------
    name : str
        The server's name from the instrument configuration, or
        :data:`UNCONFIGURED_SERVER`.
    description : str
        What the configuration says this server is, for the operator who
        did not write the file.
    condition : Condition
        The worst of its devices'.
    devices : tuple[TargetHealth, ...]
        Its devices, in the order the configuration lists them, or the
        order the broker serves them.
    """

    name: str
    description: str
    condition: Condition
    devices: tuple[TargetHealth, ...]


@dataclass(frozen=True)
class InstrumentHealth:
    """
    Everything the broker is wrapping, and whether it is answering.

    Attributes
    ----------
    condition : Condition
        The worst of the servers'.
    summary : str
        One line for a tray tooltip: the count that is fine, or the
        thing that is not.
    servers : tuple[ServerHealth, ...]
        The device servers behind this instrument. Empty when the broker
        could not be asked.
    """

    condition: Condition
    summary: str
    servers: tuple[ServerHealth, ...] = ()


def unreachable(reason: str) -> InstrumentHealth:
    """
    Report an instrument that could not be asked about itself.

    Its own state rather than an empty report, because "no devices" and
    "no answer" want opposite responses and look identical once one has
    been flattened into the other.

    Parameters
    ----------
    reason : str
        What went wrong asking.

    Returns
    -------
    InstrumentHealth
        Unreachable, with the reason as the summary.
    """
    return InstrumentHealth(condition=Condition.UNREACHABLE, summary=reason)


def assess(
    described: Mapping[str, TargetDescription],
    states: Mapping[str, TargetState],
    *,
    config: InstrumentConfig | None = None,
) -> InstrumentHealth:
    """
    Group what the broker reports by the server each target came from.

    Parameters
    ----------
    described : Mapping[str, TargetDescription]
        What each target is, from the broker's ``describe``.
    states : Mapping[str, TargetState]
        What each is doing, from the broker's ``targets``.
    config : InstrumentConfig | None
        The instrument this broker was started from, when it was started
        from one. Without it every target is reported under a single
        heading, which is the truth for a broker over one adapter.

    Returns
    -------
    InstrumentHealth
        Every server, its devices, and the worst condition among them.
    """
    devices = [
        _target_health(name, described.get(name), states.get(name))
        for name in _ordered(described, states)
    ]
    servers = _group(devices, config)
    condition = max(
        (server.condition for server in servers),
        default=Condition.HEALTHY,
    )
    return InstrumentHealth(
        condition=condition,
        summary=_summarise(servers, devices, condition),
        servers=servers,
    )


def _ordered(
    described: Mapping[str, TargetDescription],
    states: Mapping[str, TargetState],
) -> tuple[str, ...]:
    """
    List every target either call knows about, describe's order first.

    Both should answer with the same names; a target in one and not the
    other is a disagreement worth showing rather than hiding, since it
    means one of the two reads was taken across a change.

    Parameters
    ----------
    described : Mapping[str, TargetDescription]
        What each target is.
    states : Mapping[str, TargetState]
        What each is doing.

    Returns
    -------
    tuple[str, ...]
        Target names, without duplicates.
    """
    names = list(described)
    names += [name for name in states if name not in described]
    return tuple(names)


def _target_health(
    name: str,
    description: TargetDescription | None,
    state: TargetState | None,
) -> TargetHealth:
    """
    Turn one target's two reports into a condition and a line about it.

    Parameters
    ----------
    name : str
        The target name.
    description : TargetDescription | None
        What it is, or None if ``describe`` did not mention it.
    state : TargetState | None
        What it is doing, or None if ``targets`` did not.

    Returns
    -------
    TargetHealth
        Its condition and the line to show beside it.
    """
    condition = Condition.HEALTHY
    detail = "idle"
    if state is not None and state.is_live:
        rate = state.stats.fps if state.stats is not None else 0.0
        detail = f"acquiring at {rate:.1f} fps"
    if state is not None and state.lease is not None:
        detail = f"{detail}, held by {state.lease.holder}"
    if description is not None and description.error:
        condition = Condition.DEGRADED
        detail = f"{detail} - did not answer: {description.error}"
    if state is not None and state.error:
        # After the description's error rather than before, because a
        # loop that died is the newer and the more actionable of the two
        # - and because FAILED outranks DEGRADED either way.
        condition = Condition.FAILED
        detail = f"{detail} - stopped: {state.error}"
    if state is None:
        condition = max(condition, Condition.DEGRADED)
        detail = "served, but not reporting its state"
    return TargetHealth(
        name=name,
        label=description.label if description is not None else name,
        kind=description.kind if description is not None else "",
        condition=condition,
        detail=detail,
        is_live=bool(state is not None and state.is_live),
        fps=(
            state.stats.fps if state is not None and state.stats is not None else None
        ),
        holder=(
            state.lease.holder
            if state is not None and state.lease is not None
            else None
        ),
    )


def _group(
    devices: list[TargetHealth],
    config: InstrumentConfig | None,
) -> tuple[ServerHealth, ...]:
    """
    Put each device under the server that was supposed to bring it.

    Parameters
    ----------
    devices : list[TargetHealth]
        Every device, assessed.
    config : InstrumentConfig | None
        The instrument configuration, or None.

    Returns
    -------
    tuple[ServerHealth, ...]
        One entry per server, in the order the configuration lists them;
        one entry in total when there is no configuration.
    """
    if not devices:
        return ()
    if config is None:
        return (_server_health(UNCONFIGURED_SERVER, "", devices),)
    owners = {target: server.name for target, (server, _) in config.devices().items()}
    controlling = config.controlling_server()
    grouped: dict[str, list[TargetHealth]] = {
        server.name: [] for server in config.enabled_servers()
    }
    for device in devices:
        owner = owners.get(device.name)
        if (
            owner is None
            and controlling is not None
            and device.name == INSTRUMENT_TARGET
        ):
            # The column's controls are not a listed device - they come
            # from whichever server owns the column, which the file says
            # separately. See broker.app.configured_targets.
            owner = controlling.name
        grouped.setdefault(owner or UNCONFIGURED_SERVER, []).append(device)
    described = {server.name: server.description for server in config.enabled_servers()}
    return tuple(
        _server_health(name, described.get(name, ""), members)
        for name, members in grouped.items()
        if members
    )


def _server_health(
    name: str,
    description: str,
    devices: list[TargetHealth],
) -> ServerHealth:
    """
    Roll a server's devices up into the server's own condition.

    Parameters
    ----------
    name : str
        The server's name.
    description : str
        What the configuration says it is.
    devices : list[TargetHealth]
        Its devices.

    Returns
    -------
    ServerHealth
        The server, at the worst of its devices' conditions.
    """
    return ServerHealth(
        name=name,
        description=description,
        condition=max(
            (device.condition for device in devices),
            default=Condition.HEALTHY,
        ),
        devices=tuple(devices),
    )


def _summarise(
    servers: tuple[ServerHealth, ...],
    devices: list[TargetHealth],
    condition: Condition,
) -> str:
    """
    Write the one line a tooltip has room for.

    Names the trouble when there is any, and counts when there is not:
    "3 devices on 2 servers, all answering" is what an operator wants to
    read at a glance, and "eels_camera stopped" is what they need to
    read instead.

    Parameters
    ----------
    servers : tuple[ServerHealth, ...]
        The assessed servers.
    devices : list[TargetHealth]
        Every device, assessed.
    condition : Condition
        The instrument's overall condition.

    Returns
    -------
    str
        One line.
    """
    if not devices:
        return "no devices served"
    if condition is not Condition.HEALTHY:
        troubled = [device.name for device in devices if device.condition is condition]
        wording = "stopped" if condition is Condition.FAILED else "not fully answering"
        return f"{', '.join(troubled)} {wording}"
    counted = f"{len(devices)} device{'s' if len(devices) != 1 else ''}"
    live = sum(1 for device in devices if device.is_live)
    if len(servers) > 1:
        counted = f"{counted} on {len(servers)} servers"
    return f"{counted}, all answering" + (f"; {live} acquiring" if live else "")
