"""
Wire protocol for talking to a device server process.

This module is the entire license boundary: it is imported by both the
GPL-3.0 device server (:mod:`miainwoodpecker.devices.nion_server`) and the
MIT-licensed client (:mod:`miainwoodpecker.devices.remote`), and it
imports nothing from either side — no ``nion.*``, no server or client
internals. Two separate programs exchanging ``Call``/``Result`` objects
over a socket is "communication at arm's length" between independent
programs, not a combined work, which is the standard reading of what the
GPL's copyleft does and does not reach for Python (an in-process
``import`` of a GPL library is normally treated as linking; a subprocess
talking over a documented protocol is not). See docs/migration-plan.md,
§6, for the reasoning and the alternative considered.

Deliberately minimal rather than a general RPC framework: one call shape,
one result shape, dispatch by looking up ``target`` then ``getattr`` for
``method``. That is all the device layer's protocols (``Camera``,
``Scanner``) need.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass, field

if typing.TYPE_CHECKING:
    import threading
    from multiprocessing.connection import Connection


@dataclass(frozen=True)
class Call:
    """
    A request to invoke one method on one named target.

    Attributes
    ----------
    target : str
        Name of the object to invoke the method on (e.g. ``"scanner"``).
    method : str
        Method name to call on that target.
    args : tuple
        Positional arguments.
    kwargs : dict[str, object]
        Keyword arguments.
    """

    target: str
    method: str
    args: tuple = ()
    kwargs: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    """
    The outcome of a :class:`Call`.

    Exactly one of ``value`` or ``error`` is meaningful: a raised
    exception on the server side is caught, stringified, and returned as
    ``error`` rather than crashing the connection, so one failing call
    doesn't take down the whole device server process.

    Attributes
    ----------
    value : object
        The method's return value, when the call succeeded.
    error : str | None
        ``f"{type(exc).__name__}: {exc}"`` from the server side, when the
        call raised.
    """

    value: object = None
    error: str | None = None


class RemoteCallError(RuntimeError):
    """Raised client-side when a :class:`Call` failed on the server."""


def send_call(
    connection: Connection,
    lock: threading.Lock,
    call: Call,
) -> object:
    """
    Send a call and return its result, raising on a server-side error.

    Parameters
    ----------
    connection : Connection
        An open connection to a device server target.
    lock : threading.Lock
        Serializes concurrent callers sharing one connection; a
        send/recv round trip must not interleave with another.
    call : Call
        The call to make.

    Returns
    -------
    object
        The call's return value.

    Raises
    ------
    RemoteCallError
        If the call raised on the server side.
    """
    with lock:
        connection.send(call)
        result = connection.recv()
    if result.error is not None:
        msg = f"remote call {call.target}.{call.method}() failed: {result.error}"
        raise RemoteCallError(msg)
    return result.value
