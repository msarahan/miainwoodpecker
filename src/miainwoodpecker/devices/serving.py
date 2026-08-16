"""
The device-server half of the RPC protocol, with no vendor in it.

:mod:`miainwoodpecker.devices.rpc` defines what goes over the wire and
:mod:`miainwoodpecker.devices.remote` is the client. This is the other
end: accept connections for a target, dispatch
:class:`~miainwoodpecker.devices.rpc.Call` objects against an object that
satisfies one of the protocols in
:mod:`miainwoodpecker.devices.interface`, and route large frames around
the pickle channel.

It exists because it was written twice. Every line of it started in
``nion_server.py``, where it sat next to a GPL-3.0 vendor stack while
being entirely vendor-free — and the moment a second adapter appeared
(``camera_server.py``) the choice was to copy it or to move it. Anything
out-of-tree faces the same choice, which is the better argument: an
adapter author should be writing device code, not a socket loop, and
``docs/vendor-support.md`` measures the difference at about a hundred
lines.

**Dispatch is here; lifecycle is not.** Deliberately. Which devices exist,
what a safe parked state is, whether an orphaned server should shut itself
down — those are instrument-specific, and a webcam has no beam to blank.
This module knows how to answer calls; what a server *is* stays with the
adapter.

MIT, like the rest of the package outside ``nion_server``, so a
proprietary or copyleft adapter can import it without either licence
reaching the other.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import typing

from miainwoodpecker.devices.interface import Frame, Spectrum
from miainwoodpecker.devices.rpc import (
    SHARED_MEMORY_THRESHOLD_BYTES,
    Call,
    Result,
    disable_nagle,
    target_kind,
)

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from multiprocessing.connection import Listener

    from miainwoodpecker.devices.shared_frame import SharedFrameWriter

__all__ = [
    "accept_loop",
    "bind_targets",
    "endpoint_map",
    "invoke",
    "serve_connection",
]

_LOGGER = logging.getLogger("miainwoodpecker.devices.serving")



def bind_targets(
    names: Sequence[str],
    ports: typing.Mapping[str, int],
    authkey: bytes,
) -> tuple[list[Listener], dict[str, int]]:
    """
    Bind one listener per target, letting the OS choose unnamed ports.

    The client allocates and passes a port only for the targets it can
    name in advance. Anything else — a second commodity camera, a
    detector list this client has never heard of — binds on **port 0**,
    which is the OS choosing a free ephemeral port, and the port it
    actually got is reported back through ``describe()`` for the client
    to dial.

    That is what lets a server serve targets whose names the client
    does not know, and it removes a race as a side effect: a
    client-allocated port is probed free and bound later, so something
    else on the machine can take it in between (the
    :data:`PORT_UNAVAILABLE_EXIT_STATUS` retry exists for exactly that
    window). A port the OS assigns at bind time cannot be lost that way,
    because there is no gap between choosing and binding.

    Parameters
    ----------
    names : Sequence[str]
        Target names to bind, in the order the listeners are returned.
    ports : typing.Mapping[str, int]
        Ports the client allocated, by target name. A name absent from
        this mapping binds on an OS-assigned port.
    authkey : bytes
        Shared secret for every listener.

    Returns
    -------
    tuple[list[Listener], dict[str, int]]
        The listeners, and the port each one actually bound.

    Notes
    -----
    A listener that cannot bind raises ``OSError`` from ``Listener``
    itself; callers translate that into
    :data:`PORT_UNAVAILABLE_EXIT_STATUS` so the client respawns.
    """
    from multiprocessing.connection import Listener  # noqa: PLC0415 - see nion_server

    listeners: list[Listener] = []
    bound: dict[str, int] = {}
    for name in names:
        listener = Listener(("localhost", ports.get(name, 0)), authkey=authkey)
        listeners.append(listener)
        bound[name] = int(listener.address[1])
    return listeners, bound


def endpoint_map(
    bound: typing.Mapping[str, int],
    labels: typing.Mapping[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    """
    Build the ``endpoints`` map ``describe()`` reports, from bound ports.

    Every server in this tree builds it the same way — port from
    :func:`bind_targets`, kind from
    :func:`~miainwoodpecker.devices.rpc.target_kind`, label from the
    device if it has one — so it is built here once. An out-of-tree
    adapter is not obliged to use this function, only to report a map of
    the same shape; using it is the way to be sure the ``kind`` strings
    match what the client looks for.

    Parameters
    ----------
    bound : typing.Mapping[str, int]
        Target name to the port its listener actually bound, as
        :func:`bind_targets` returns.
    labels : typing.Mapping[str, str] | None
        Human-readable label per target — a ``camera_id``, a
        ``scanner_id`` — for the client to name a device by what it is
        rather than by which slot it landed in. A name absent here is
        labelled with itself.

    Returns
    -------
    dict[str, dict[str, object]]
        Target name to its ``port``, ``kind`` and ``label``.
    """
    labels = labels or {}
    return {
        name: {
            "port": port,
            "kind": target_kind(name),
            "label": labels.get(name, name),
        }
        for name, port in bound.items()
    }


def _stackable_frames(value: object) -> list[Frame] | None:
    """
    Return the frames of a multi-frame result worth the shared-memory path.

    A simultaneous multi-channel scan returns several frames from one
    pass, and the reused-segment transport allows exactly one publish per
    request/response cycle — so a qualifying set crosses as **one stacked
    block** (:class:`~miainwoodpecker.devices.shared_frame.SharedFrameSetRef`)
    rather than as N publishes that would overwrite each other. Qualifying
    means: a non-empty list or tuple of nothing but frames, agreeing on
    shape and dtype (true by construction for one pass, and required for
    stacking), whose combined size clears the same threshold single
    frames use. Anything else — a small set, or a heterogeneous one from
    some future adapter — stays on the pickle path, which is correct for
    every size and merely slower for large ones.

    Parameters
    ----------
    value : object
        A call's return value.

    Returns
    -------
    list[Frame] | None
        The frames to publish as one block, or ``None`` to leave the
        value on the pickle path.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if not all(isinstance(item, Frame) for item in value):
        return None
    frames = typing.cast("list[Frame]", list(value))
    first = frames[0].data
    if any(
        frame.data.shape != first.shape or frame.data.dtype != first.dtype
        for frame in frames[1:]
    ):
        return None
    if sum(frame.data.nbytes for frame in frames) < SHARED_MEMORY_THRESHOLD_BYTES:
        return None
    return frames


def invoke(
    target: object,
    call: Call,
    writer: SharedFrameWriter | None,
    name: str,
) -> Result:
    """
    Run one call against a target and package the outcome as a Result.

    The dispatch rules live here rather than in the socket loop:
    properties are read rather than invoked, large frames go around the
    pickle channel, and a device's ``close`` retires its segment.

    Parameters
    ----------
    target : object
        The device or instrument the call dispatches to.
    call : Call
        The client's request.
    writer : SharedFrameWriter | None
        This target's reused shared-memory writer, or ``None`` for a
        target that never returns a ``Frame``.
    name : str
        Server-side target name, for log attribution.

    Returns
    -------
    Result
        The value, or a stringified error — never a raised exception, so
        one failing call cannot take down the connection or the server.
    """
    if not hasattr(target, call.method):
        _LOGGER.warning("target %s: unknown method %r requested", name, call.method)
        return Result(error=f"unknown method {call.method!r} on {call.target!r}")
    try:
        # getattr already evaluates properties (camera_id, scanner_id,
        # channel_names): call the result only when it's a bound
        # method, or a property's value gets invoked as a function.
        attribute = getattr(target, call.method)
        value = (
            attribute(*call.args, **call.kwargs) if callable(attribute) else attribute
        )
        # Only arrays worth the round trip are routed around the
        # pickle-over-socket channel; everything else returned here
        # is small (a string, a list of names, None). A spot spectrum is
        # normally under the threshold and stays on the pickle path; a
        # spectrum image is the case this branch exists for, since a
        # 256x256 map of 4096 channels is a gigabyte.
        if (
            writer is not None
            and isinstance(value, (Frame, Spectrum))
            and value.data.nbytes >= SHARED_MEMORY_THRESHOLD_BYTES
        ):
            value = (
                writer.publish(value)
                if isinstance(value, Frame)
                else writer.publish_spectrum(value)
            )
        elif writer is not None and (frames := _stackable_frames(value)) is not None:
            # The frames of one simultaneous scan, as one stacked block:
            # see _stackable_frames for why one publish rather than N.
            value = writer.publish_frames(frames)
        # The device's own close() just stopped its acquisition
        # thread; retire its shared-memory segment too, or it leaks
        # in /dev/shm - named segments aren't reclaimed when a
        # process dies, unlike its threads or anonymous memory.
        if call.method == "close" and writer is not None:
            writer.close()
    except Exception as exc:
        # The client gets the message; the log gets the traceback,
        # which is the only place it survives - Result carries a
        # string, so a stringified error is all that crosses the
        # boundary. This is the per-call diagnostic the whole
        # logging setup exists for.
        _LOGGER.exception("target %s: call %s() raised", name, call.method)
        return Result(
            error=f"{type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
        )
    return Result(value=value)


def serve_connection(  # noqa: PLR0913 - one dispatch loop, not a call signature
    connection: object,
    name: str,
    target: object,
    writer: SharedFrameWriter | None,
    stop_event: threading.Event,
    *,
    rejected: str | None = None,
    release: threading.Lock | None = None,
    on_close: Callable[[], None] | None = None,
) -> None:
    """
    Handle Calls on one accepted connection until the client disconnects.

    Logging here is deliberately failure-only. A successful call is not
    logged at all, at any level: the frame path runs through this loop, so
    even a disabled ``debug()`` call would sit between the client's request
    and its frame, and the shared-memory benchmarks are the standard this
    must not move.

    Parameters
    ----------
    connection : object
        The accepted connection, typed loosely to avoid importing
        ``multiprocessing.connection`` for a type-only reference.
    name : str
        This connection's server-side target name, used to attribute log
        records. Taken from the server's own binding rather than from
        ``call.target``, which is whatever the client claimed.
    target : object
        The device (or instrument) this connection's calls dispatch to.
    writer : SharedFrameWriter | None
        This target's reused shared-memory writer, or ``None`` for a
        target that never returns a ``Frame``.
    stop_event : threading.Event
        Set after a ``shutdown`` call's reply has been sent, to let the
        server's main loop return. Setting it only *after* the send is
        what keeps the acknowledgement from racing the listener teardown.
    rejected : str | None
        When set, this connection lost the race for an exclusive target:
        every call is answered with this message instead of reaching the
        device. Answering rather than hanging up means the client raises
        ``RemoteCallError`` naming the real reason on its first call.
    release : threading.Lock | None
        The exclusivity lock this connection holds, released when it ends
        so the next client can take the target over.
    on_close : Callable[[], None] | None
        Called when this connection ends, so the server can notice when
        the last one has gone.
    """
    try:
        while True:
            try:
                call: Call = connection.recv()
            except (EOFError, ConnectionError):
                # ConnectionError (reset/broken pipe) is an abortive close
                # rather than a clean one; both mean "the client is gone",
                # and letting it escape would kill this thread with a
                # traceback on the stderr the parent shares.
                _LOGGER.debug("target %s: client disconnected", name)
                return
            result = (
                Result(error=rejected)
                if rejected is not None
                else invoke(target, call, writer, name)
            )
            connection.send(result)
            if result.error is None and call.method == "shutdown":
                stop_event.set()
                return
    except OSError:
        # A client that vanished mid-reply breaks send() the same way.
        # Same reasoning as the recv() guard above: this is the client's
        # departure, not a server fault worth a thread-death traceback.
        _LOGGER.debug("target %s: connection broke while replying", name)
    finally:
        with contextlib.suppress(OSError):
            connection.close()
        if release is not None:
            release.release()
        if on_close is not None:
            on_close()


def accept_loop(  # noqa: PLR0913 - a thread entry point, not a call signature
    listener: Listener,
    name: str,
    target: object,
    writer: SharedFrameWriter | None,
    stop_event: threading.Event,
    *,
    on_open: Callable[[], None] | None = None,
    on_close: Callable[[], None] | None = None,
) -> None:
    """
    Accept connections for one target, one handler thread per connection.

    A frame-producing target (one with a ``writer``) admits **one**
    connection at a time. Its ``SharedFrameWriter`` reuses a single
    segment per shape, which is safe only while exactly one
    request/response is in flight: two clients interleaving calls would
    have the second's publish overwrite the segment while the first is
    still copying out of it, silently splicing two frames together. That
    invariant used to rest on client convention alone — one remote device
    per target — which nothing server-side could check and a second
    viewer pointed at these ports would break. A rejected connection is
    served, not dropped, so the client gets a diagnosis on its first call
    rather than a bare EOF.

    Parameters
    ----------
    listener : Listener
        This target's bound listener. Closing it elsewhere is what ends
        this loop.
    name : str
        The target's server-side name, for log attribution.
    target : object
        The device or instrument connections dispatch to.
    writer : SharedFrameWriter | None
        This target's shared-memory writer; ``None`` marks a target that
        never returns a frame, and therefore one that admits any number
        of connections.
    stop_event : threading.Event
        Passed to each handler, set once a ``shutdown`` is acknowledged.
    on_open : Callable[[], None] | None
        Called for each accepted connection.
    on_close : Callable[[], None] | None
        Called when each connection ends.
    """
    in_use = threading.Lock() if writer is not None else None
    while True:
        try:
            connection = listener.accept()
        except OSError:
            _LOGGER.debug("target %s: listener closed, no longer accepting", name)
            return  # listener.close() from elsewhere unblocks accept() this way.
        rejected: str | None = None
        if in_use is not None and not in_use.acquire(blocking=False):
            rejected = (
                f"target {name!r} is already driven by another connection; "
                f"a frame-producing device admits one client at a time"
            )
            _LOGGER.warning("target %s: refused a second connection", name)
        else:
            _LOGGER.info("target %s: accepted a connection", name)
        disable_nagle(connection)
        if on_open is not None:
            on_open()
        threading.Thread(
            target=serve_connection,
            args=(connection, name, target, writer, stop_event),
            kwargs={
                "rejected": rejected,
                "release": None if rejected is not None else in_use,
                "on_close": on_close,
            },
            daemon=True,
        ).start()
