"""
What hardware a given microscope has, and which processes serve it.

An instrument is not one device server. SuperSTEM 3 is a Nion column
whose scan unit and Ronchigram camera come from Nion's stack, plus a
DECTRIS ELA on the spectrometer that speaks SIMPLON over HTTP and knows
nothing about Nion; SuperSTEM 2 is a Nion column plus a Bruker EDX
detector behind a vendor SDK that cannot be redistributed. Each of those
is a separate adapter, and each adapter is a separate process - that is
the whole shape of :mod:`miainwoodpecker.devices`, where a server is
launched with ``python -m`` and talked to over a socket.

Until now the broker could start exactly one of them, named on its
command line, which meant an instrument with two adapters could not be
served whole at all. This module is the missing description: a file per
microscope that says what the microscope *has*, which process serves
each piece, and what to call it once it is served. The broker reads one
and starts them all.

**The file is the enumeration, and the enumeration is authoritative.**
A device server reports what it found; this says what should be there.
The difference is the point. A Nion server that comes up without its
EELS camera - a spectrometer left switched off, a plug-in that failed to
load - reports a perfectly consistent instrument with one fewer camera,
and every layer above happily serves it. Checked against a file that
says the camera exists, the same startup is an error with a name in it.
Nothing else in the stack can make that check, because nothing else
knows what the hardware is.

Three consequences of that, each deliberate:

- A device the file does not list is **not served**, even if its server
  offers it. Otherwise "enumerates the hardware" would mean "enumerates
  some of the hardware, and also whatever turns up", which is not an
  enumeration. It is logged by name, so the fix is to add three lines.
- A device the file lists and its server does not serve is a **startup
  failure**, not a warning. The alternative is a session that looks
  normal until somebody reaches for the detector that was never there.
- Naming is done here rather than by the server. Two adapters both serve
  a target called ``camera``; the file says which one is the
  instrument's ``eels_camera`` and which is its ``ronchigram_camera``. A
  server has no way to know - the ELA is an EELS detector on SuperSTEM 3
  and a 4D-STEM camera on somebody else's column, with identical
  firmware.

TOML rather than JSON or YAML: comments. A file describing an instrument
is mostly the reasons - which control unit, why that plug-in, what the
detector is actually mounted on - and a format that cannot carry them
loses exactly the part a second operator needs. :mod:`tomllib` is in the
standard library from 3.11, which this project already requires.

Unknown keys are refused rather than ignored. The failure mode of
ignoring them is silence: ``plugin = "..."`` where the key is
``plugins`` starts a hardware server with no arguments and no complaint,
and on an instrument that is a session spent driving the wrong thing.

What this does not do, deliberately: it does not spawn anything. It
parses and validates, and :mod:`miainwoodpecker.broker.app` does the
launching, for the reason that module already gives about
:func:`~miainwoodpecker.broker.app.serve_instrument` - the part with the
decisions in it should be testable without processes.
"""

from __future__ import annotations

import pathlib
import tomllib
import typing
from dataclasses import dataclass, field

from miainwoodpecker.devices.rpc import (
    BACKENDS,
    INSTRUMENT_TARGET,
    SIMULATED_BACKEND,
)

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Iterator, Mapping, Sequence

SCHEMA_VERSION = 1
"""
Version this module reads, stamped in every file as ``schema``.

Refused rather than guessed at when it disagrees, for the reason
:data:`~miainwoodpecker.broker.invitation.CONFIG_VERSION` gives: a file
half-understood is worse than a file rejected, and here the thing being
half-understood is which hardware to open.
"""

DEFAULT_FILENAME = "instrument.toml"
"""The conventional name, so a directory can be named instead of a file."""

_SERVER_KEYS = frozenset(
    {
        "name",
        "module",
        "backend",
        "plugins",
        "controls_column",
        "enabled",
        "description",
        "device",
    },
)
_DEVICE_KEYS = frozenset({"target", "served_as", "description", "enabled"})
_TOP_LEVEL_KEYS = frozenset({"schema", "name", "description", "site", "server"})


class InstrumentConfigError(ValueError):
    """
    An instrument configuration could not be read, or does not make sense.

    One exception for both, because the operator's next move is the same
    - open the file - and the message says which part of it to look at.
    """


@dataclass(frozen=True)
class DeviceConfig:
    """
    One piece of hardware, and what the instrument calls it.

    Attributes
    ----------
    target : str
        The name clients see on the broker: ``scanner``,
        ``eels_camera``, ``spectrum_detector``, or anything else - an
        out-of-tree adapter's target is arbitrated like any other (see
        :func:`~miainwoodpecker.devices.rpc.target_kind`). Unique across
        the whole instrument, which is what stops two adapters that both
        serve ``camera`` from overwriting one another.
    served_as : str
        The name its own server serves it under, which is often not
        :attr:`target`. The DECTRIS server serves ``camera`` because
        that is all it knows; on a column where the ELA sits behind the
        spectrometer, the instrument's name for it is ``eels_camera``.
        Defaults to :attr:`target`.
    description : str
        What the hardware actually is, for the operator reading the file
        and for the startup log. Free text, and worth writing: "DECTRIS
        ELA on the Nion IRIS spectrometer" is the sentence that stops
        somebody wiring it up as a diffraction camera.
    enabled : bool
        Whether to serve it this session. ``false`` keeps a device
        listed - the hardware is still on the instrument - while leaving
        it out of the broker's target map, which is what an adapter that
        does not exist yet, or a detector that is away being repaired,
        needs.
    """

    target: str
    served_as: str
    description: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class ServerConfig:
    """
    One device-server process, and the hardware it is responsible for.

    Attributes
    ----------
    name : str
        This server's name within the file. Not a target name and not
        seen by clients: it names the *process*, so that a log line, an
        error about a device that did not appear, and a future
        supervisor restarting one adapter can all say which.
    module : str
        The module to run with ``python -m``, as
        :func:`~miainwoodpecker.devices.remote.remote_instrument` takes
        it. In-tree adapters are
        ``miainwoodpecker.devices.nion_server``, ``camera_server``,
        ``dectris_server`` and ``spectrum_server``; anything importable
        that speaks the same protocol works, which is how a vendor SDK
        that cannot live in this repository is reached.
    backend : str
        ``"simulated"`` or ``"hardware"``, per
        :data:`~miainwoodpecker.devices.rpc.BACKENDS`. Defaults to
        ``"simulated"``, matching every other entry point in this
        project: hardware is never what you get by leaving something
        out.
    plugins : tuple[str, ...]
        The server's ``--plugin`` values, in order, meaning whatever
        that server takes them to mean - ``nionswift_plugin`` module
        names for the Nion server, a camera index or device path for the
        commodity camera server, a control-unit address for DECTRIS.
        Deliberately not translated into per-adapter keys here: this
        module would then have to know every adapter, including the ones
        it cannot import.
    devices : tuple[DeviceConfig, ...]
        The hardware this process serves. May be empty only for a server
        that exists purely for :attr:`controls_column`, which nothing
        has needed yet but is not worth refusing.
    controls_column : bool
        Whether this server's ``instrument`` target is the *microscope's*
        controls - stage, defocus, blanker - rather than the adapter's
        own control channel. Exactly one server owns the column, and
        every server has an ``instrument`` target regardless, which is
        why this cannot be inferred: a DECTRIS server's ``instrument``
        answers ``describe`` and ``shutdown`` and knows nothing about a
        stage.
    enabled : bool
        Whether to start this process at all. ``false`` is how an
        adapter gets commented out without deleting the record of what
        the instrument has.
    description : str
        Free text about the process, for the file's reader.
    """

    name: str
    module: str
    backend: str = SIMULATED_BACKEND
    plugins: tuple[str, ...] = ()
    devices: tuple[DeviceConfig, ...] = ()
    controls_column: bool = False
    enabled: bool = True
    description: str = ""

    def enabled_devices(self) -> tuple[DeviceConfig, ...]:
        """
        Return the devices to serve from this process this session.

        Returns
        -------
        tuple[DeviceConfig, ...]
            In file order, which is the order they are logged and
            checked in - so the file reads like the startup output.
        """
        return tuple(device for device in self.devices if device.enabled)


@dataclass(frozen=True)
class InstrumentConfig:
    """
    One microscope: what it has, and which processes serve it.

    Attributes
    ----------
    name : str
        The instrument's name, as an operator says it - "SuperSTEM 2".
        Goes in the startup log and is the obvious thing for a session's
        recorded context to carry.
    description : str
        What the microscope is. Free text.
    site : str
        Where it is. Free text, and empty for the simulator, which is
        nowhere.
    servers : tuple[ServerConfig, ...]
        Every adapter process, in file order.
    source : pathlib.Path | None
        The file this was read from, or None when it was built in
        memory. Carried so that an error raised three layers away can
        name the file to open. Excluded from equality: two readings of
        the same instrument describe the same instrument.
    """

    name: str
    description: str = ""
    site: str = ""
    servers: tuple[ServerConfig, ...] = ()
    source: pathlib.Path | None = field(default=None, compare=False)

    def enabled_servers(self) -> tuple[ServerConfig, ...]:
        """
        Return the processes to start this session.

        Returns
        -------
        tuple[ServerConfig, ...]
            In file order.
        """
        return tuple(server for server in self.servers if server.enabled)

    def controlling_server(self) -> ServerConfig | None:
        """
        Return the server whose ``instrument`` target is the column's.

        Returns
        -------
        ServerConfig | None
            The one server with :attr:`ServerConfig.controls_column`, or
            None if there is none enabled this session - a detector-only
            rig, which is legitimate and which the broker handles by
            serving no ``instrument`` target rather than by refusing to
            start.
        """
        for server in self.enabled_servers():
            if server.controls_column:
                return server
        return None

    def devices(self) -> dict[str, tuple[ServerConfig, DeviceConfig]]:
        """
        Return every device to be served, by the target name clients use.

        Returns
        -------
        dict[str, tuple[ServerConfig, DeviceConfig]]
            Target name to the process serving it and its entry.
            Enabled servers and enabled devices only, so this is the
            session's target map rather than the instrument's inventory.
        """
        return {
            device.target: (server, device)
            for server in self.enabled_servers()
            for device in server.enabled_devices()
        }

    def describe(self) -> str:
        """
        Return a line for the startup log.

        Returns
        -------
        str
            The instrument, how many processes it takes, and what they
            serve - which is the sentence an operator checks against the
            microscope in front of them before letting anyone connect.
        """
        servers = self.enabled_servers()
        targets = ", ".join(self.devices()) or "no devices"
        plural = "" if len(servers) == 1 else "s"
        return f"{self.name}: {len(servers)} server{plural} serving {targets}"


def load_instrument_config(path: str | os.PathLike[str]) -> InstrumentConfig:
    """
    Read an instrument configuration from a TOML file.

    Parameters
    ----------
    path : str | os.PathLike[str]
        The file, or a directory containing :data:`DEFAULT_FILENAME`.

    Returns
    -------
    InstrumentConfig
        The parsed configuration, carrying the path it came from.

    Raises
    ------
    InstrumentConfigError
        If the file is missing, is not valid TOML, or does not describe
        an instrument this code can serve. One exception type for all
        three: the operator opens the same file either way.
    """
    source = pathlib.Path(path)
    if source.is_dir():
        source = source / DEFAULT_FILENAME
    try:
        raw = source.read_bytes()
    except OSError as error:
        message = f"could not read the instrument configuration {source}: {error}"
        raise InstrumentConfigError(message) from error
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        message = f"{source} is not valid TOML: {error}"
        raise InstrumentConfigError(message) from error
    return parse_instrument_config(data, source=source)


def parse_instrument_config(
    data: Mapping[str, object],
    *,
    source: pathlib.Path | None = None,
) -> InstrumentConfig:
    """
    Build an instrument configuration from already-parsed TOML data.

    Separate from :func:`load_instrument_config` so that every rule
    below is testable against a dictionary, without a file - the same
    split :mod:`miainwoodpecker.broker.app` makes between
    ``serve_instrument`` and ``main``, for the same reason.

    Parameters
    ----------
    data : Mapping[str, object]
        The parsed file.
    source : pathlib.Path | None
        Where it came from, for error messages and for
        :attr:`InstrumentConfig.source`.

    Returns
    -------
    InstrumentConfig
        The validated configuration.

    Raises
    ------
    InstrumentConfigError
        If anything is missing, malformed, duplicated, or unknown.
    """
    where = f"{source}: " if source is not None else ""
    _refuse_unknown(data, _TOP_LEVEL_KEYS, where, "the file")
    schema = data.get("schema")
    if schema != SCHEMA_VERSION:
        message = (
            f"{where}schema is {schema!r}, not {SCHEMA_VERSION}; this file was "
            f"written for a different version of miainwoodpecker"
        )
        raise InstrumentConfigError(message)
    name = _string(data, "name", where, "the file", required=True)
    servers = tuple(
        _server(entry, where, index)
        for index, entry in enumerate(_tables(data, "server", where, "the file"))
    )
    if not servers:
        message = (
            f"{where}an instrument needs at least one [[server]]; without one "
            f"the broker has nothing to start"
        )
        raise InstrumentConfigError(message)
    _refuse_duplicates([server.name for server in servers], where, "server names")
    _refuse_duplicates(
        [device.target for server in servers for device in server.devices],
        where,
        "device targets",
    )
    _refuse_two_columns(servers, where)
    return InstrumentConfig(
        name=name,
        description=_string(data, "description", where, "the file"),
        site=_string(data, "site", where, "the file"),
        servers=servers,
        source=source,
    )


def _server(entry: object, where: str, index: int) -> ServerConfig:
    """
    Build one server entry.

    Parameters
    ----------
    entry : object
        A ``[[server]]`` table.
    where : str
        Message prefix naming the file, possibly empty.
    index : int
        Its position, used to name it before its own name is read.

    Returns
    -------
    ServerConfig
        The parsed entry.

    Raises
    ------
    InstrumentConfigError
        If it is not a table, or a key is missing, malformed or unknown.
    """
    position = f"[[server]] #{index + 1}"
    if not isinstance(entry, dict):
        message = f"{where}{position} is {type(entry).__name__}, not a table"
        raise InstrumentConfigError(message)
    _refuse_unknown(entry, _SERVER_KEYS, where, position)
    name = _string(entry, "name", where, position, required=True)
    label = f"server {name!r}"
    backend = _string(entry, "backend", where, label) or SIMULATED_BACKEND
    if backend not in BACKENDS:
        message = (
            f"{where}{label} asks for backend {backend!r}; the broker can start "
            f"{' and '.join(BACKENDS)}"
        )
        raise InstrumentConfigError(message)
    devices = tuple(
        _device(device, where, label, at)
        for at, device in enumerate(_tables(entry, "device", where, label))
    )
    _refuse_duplicates(
        [device.served_as for device in devices],
        where,
        f"served_as names within {label}",
    )
    return ServerConfig(
        name=name,
        module=_string(entry, "module", where, label, required=True),
        backend=backend,
        plugins=_strings(entry, "plugins", where, label),
        devices=devices,
        controls_column=_flag(entry, "controls_column", where, label, default=False),
        enabled=_flag(entry, "enabled", where, label, default=True),
        description=_string(entry, "description", where, label),
    )


def _device(entry: object, where: str, server: str, index: int) -> DeviceConfig:
    """
    Build one device entry.

    Parameters
    ----------
    entry : object
        A ``[[server.device]]`` table.
    where : str
        Message prefix naming the file, possibly empty.
    server : str
        The owning server, for error messages.
    index : int
        Its position within that server.

    Returns
    -------
    DeviceConfig
        The parsed entry.

    Raises
    ------
    InstrumentConfigError
        If it is not a table, a key is missing, malformed or unknown, or
        it claims the reserved ``instrument`` target.
    """
    label = f"device #{index + 1} of {server}"
    if not isinstance(entry, dict):
        message = f"{where}{label} is {type(entry).__name__}, not a table"
        raise InstrumentConfigError(message)
    _refuse_unknown(entry, _DEVICE_KEYS, where, label)
    target = _string(entry, "target", where, label, required=True)
    if target.split(":", 1)[0] == INSTRUMENT_TARGET:
        message = (
            f"{where}{label} claims the target {target!r}, which is reserved. "
            f"Every server has an instrument target; controls_column says whose "
            f"is the microscope's"
        )
        raise InstrumentConfigError(message)
    return DeviceConfig(
        target=target,
        served_as=_string(entry, "served_as", where, label) or target,
        description=_string(entry, "description", where, label),
        enabled=_flag(entry, "enabled", where, label, default=True),
    )


def _refuse_two_columns(servers: Sequence[ServerConfig], where: str) -> None:
    """
    Insist that at most one server owns the column.

    None is allowed: a detector-only rig has no column to own, and
    refusing to describe one would be this module inventing a
    requirement the device layer does not have (see
    :class:`~miainwoodpecker.devices.remote.RemoteInstrumentDevices`,
    whose scanner is optional for the same reason). Two is not: the
    broker serves one ``instrument`` target, and a file naming two
    candidates has not said which stage moves.

    Parameters
    ----------
    servers : Sequence[ServerConfig]
        Every server in the file, enabled or not.
    where : str
        Message prefix naming the file, possibly empty.

    Raises
    ------
    InstrumentConfigError
        If more than one server sets ``controls_column``.
    """
    owners = [server.name for server in servers if server.controls_column]
    if len(owners) > 1:
        message = (
            f"{where}servers {', '.join(repr(name) for name in owners)} all set "
            f"controls_column; exactly one server's instrument target is the "
            f"microscope's"
        )
        raise InstrumentConfigError(message)


def _tables(
    data: Mapping[str, object],
    key: str,
    where: str,
    label: str,
) -> Iterator[object]:
    """
    Yield the entries of an array of tables, or nothing if it is absent.

    Parameters
    ----------
    data : Mapping[str, object]
        The containing table.
    key : str
        The array's key.
    where : str
        Message prefix naming the file, possibly empty.
    label : str
        What contains it, for error messages.

    Yields
    ------
    object
        Each entry, unvalidated - the callers know what shape they want.

    Raises
    ------
    InstrumentConfigError
        If the key is present and is not an array.
    """
    entries = data.get(key, [])
    if not isinstance(entries, list):
        message = (
            f"{where}{key} in {label} is {type(entries).__name__}; write it as "
            f"[[{key}]] tables"
        )
        raise InstrumentConfigError(message)
    yield from entries


def _string(
    data: Mapping[str, object],
    key: str,
    where: str,
    label: str,
    *,
    required: bool = False,
) -> str:
    """
    Read a string key.

    Parameters
    ----------
    data : Mapping[str, object]
        The table to read from.
    key : str
        The key.
    where : str
        Message prefix naming the file, possibly empty.
    label : str
        What is being read, for error messages.
    required : bool
        Whether absence is an error. When it is not, absence is ``""``.

    Returns
    -------
    str
        The value, or ``""``.

    Raises
    ------
    InstrumentConfigError
        If it is required and missing or empty, or present and not a
        string.
    """
    value = data.get(key)
    if value is None:
        if required:
            message = f"{where}{label} has no {key}"
            raise InstrumentConfigError(message)
        return ""
    if not isinstance(value, str):
        message = f"{where}{key} in {label} is {type(value).__name__}, not a string"
        raise InstrumentConfigError(message)
    if required and not value:
        message = f"{where}{label} has an empty {key}"
        raise InstrumentConfigError(message)
    return value


def _strings(
    data: Mapping[str, object],
    key: str,
    where: str,
    label: str,
) -> tuple[str, ...]:
    """
    Read a list-of-strings key, absent meaning empty.

    Parameters
    ----------
    data : Mapping[str, object]
        The table to read from.
    key : str
        The key.
    where : str
        Message prefix naming the file, possibly empty.
    label : str
        What is being read, for error messages.

    Returns
    -------
    tuple[str, ...]
        The values, in order.

    Raises
    ------
    InstrumentConfigError
        If it is present and is not a list of strings.
    """
    value = data.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        message = (
            f"{where}{key} in {label} must be a list of strings, passed to the "
            f"server as its --plugin values"
        )
        raise InstrumentConfigError(message)
    return tuple(typing.cast("list[str]", value))


def _flag(
    data: Mapping[str, object],
    key: str,
    where: str,
    label: str,
    *,
    default: bool,
) -> bool:
    """
    Read a boolean key.

    Parameters
    ----------
    data : Mapping[str, object]
        The table to read from.
    key : str
        The key.
    where : str
        Message prefix naming the file, possibly empty.
    label : str
        What is being read, for error messages.
    default : bool
        What absence means.

    Returns
    -------
    bool
        The value.

    Raises
    ------
    InstrumentConfigError
        If it is present and is not a boolean. A quoted ``"false"`` is
        refused rather than accepted as truthy, which is the whole
        reason this is not a one-line ``bool()``.
    """
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        message = f"{where}{key} in {label} is {value!r}; write true or false"
        raise InstrumentConfigError(message)
    return value


def _refuse_unknown(
    data: Mapping[str, object],
    known: frozenset[str],
    where: str,
    label: str,
) -> None:
    """
    Refuse keys this schema does not define.

    Parameters
    ----------
    data : Mapping[str, object]
        The table to check.
    known : frozenset[str]
        The keys it may have.
    where : str
        Message prefix naming the file, possibly empty.
    label : str
        What is being checked, for error messages.

    Raises
    ------
    InstrumentConfigError
        If any key is not in ``known``. Silently ignoring one means a
        misspelt ``plugins`` starts a hardware server with no arguments
        and says nothing about it.
    """
    unknown = sorted(set(data) - known)
    if unknown:
        plural = "s" if len(unknown) > 1 else ""
        message = (
            f"{where}{label} has unknown key{plural} "
            f"{', '.join(repr(key) for key in unknown)}; known keys are "
            f"{', '.join(sorted(known))}"
        )
        raise InstrumentConfigError(message)


def _refuse_duplicates(names: Sequence[str], where: str, what: str) -> None:
    """
    Refuse a repeated name.

    Parameters
    ----------
    names : Sequence[str]
        The names, in file order.
    where : str
        Message prefix naming the file, possibly empty.
    what : str
        What they are, for the error message.

    Raises
    ------
    InstrumentConfigError
        If any name appears twice. Target names in particular: the
        broker's target map is a dictionary, so a duplicate would not
        fail, it would quietly serve one detector and drop the other.
    """
    seen: set[str] = set()
    for name in names:
        if name in seen:
            message = f"{where}{what} must be unique; {name!r} appears twice"
            raise InstrumentConfigError(message)
        seen.add(name)
