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

import contextlib
import socket
import typing
from dataclasses import dataclass, field

if typing.TYPE_CHECKING:
    import threading
    from multiprocessing.connection import Connection


def disable_nagle(connection: Connection) -> None:
    """
    Disable Nagle's algorithm (set ``TCP_NODELAY``) on a connection's socket.

    Measured with ``scripts/ipc_overhead_benchmark.py``: two scan sizes
    among eleven tested (64x64 and 90x90, both on the plain-pickle path)
    showed a strikingly consistent ~44ms overhead spike - p95 within
    0.2ms of the median, not the kind of spread ordinary scheduling noise
    produces. 44ms is close enough to Linux's ~40ms delayed-ACK timer to
    be the signature of Nagle's algorithm (buffer a small write, hoping to
    coalesce it with the next one) interacting with the receiver's
    delayed ACK (wait, hoping to piggyback the ACK on outgoing data) -
    each waiting on the other. Confirmed directly: a plain
    ``multiprocessing.connection.Listener``/``Client`` pair has
    ``TCP_NODELAY`` unset (0) on both ends by default; this module does
    not do it for you. The exact message sizes/timing that trigger it are
    inherently data-pattern-dependent (that unpredictability is what
    Nagle+delayed-ACK stalls are known for), so this is applied to every
    connection this project opens, not just the sizes that happened to
    reproduce it in one benchmark run.

    The socket is put back into blocking mode before it is detached, and
    that is load-bearing rather than defensive. ``socket.socket(fileno=)``
    applies the process-wide :func:`socket.setdefaulttimeout` at
    construction, and setting a timeout puts the *underlying fd* into
    non-blocking mode — which ``detach()`` does not undo, because detach
    only releases Python's ownership of the fd, not the flags already set
    on it. So without the restore, any library anywhere in the process
    calling ``setdefaulttimeout`` (plausible in a napari/Qt application
    with HTTP-capable plugins) would silently switch every connection this
    project opens to non-blocking. ``Connection.recv()`` would then raise
    ``BlockingIOError`` — an ``OSError``, so :func:`send_call` wraps it as
    ``RemoteConnectionLostError`` and the operator is told the device
    server died when it is perfectly healthy. Verified directly rather
    than reasoned about: with a default timeout set, wrapping a blocking
    fd and detaching left it non-blocking and the next ``recv`` raising
    ``EAGAIN``.

    ``setblocking(True)`` rather than saving and restoring
    ``os.get_blocking``: those two are Unix-only, so reading the old state
    raised ``AttributeError`` on Windows — outside this function's
    ``except`` clause, which would have made *every* connection setup
    fail there. Restoring unconditionally is also correct rather than
    merely portable, because a ``multiprocessing.connection`` endpoint is
    always blocking to begin with.

    Parameters
    ----------
    connection : Connection
        A connection returned by ``Client(...)`` or ``Listener.accept()``.
        Must be a real socket (``AF_INET``/``AF_UNIX``); silently a no-op
        for other families (e.g. Windows named pipes, which don't have
        this concept and don't need it).
    """
    try:
        raw = socket.socket(fileno=connection.fileno())
    except (OSError, ValueError):
        return
    try:
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            raw.setblocking(True)  # noqa: FBT003 - stdlib signature is positional
        raw.detach()  # we don't own this fd; multiprocessing.connection does


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


class RemoteCallTimeoutError(RemoteCallError):
    """
    Raised client-side when a :class:`Call` produced no reply in time.

    A subclass of :class:`RemoteCallError` so callers that only care
    "the call did not succeed" need no new ``except`` clause.

    The connection is left **poisoned**: the request was sent, so a reply
    may still arrive later and would be mistaken for the answer to the
    *next* call on the same connection. Anything that catches this must
    stop using the connection. Both callers today
    (:mod:`miainwoodpecker.devices.remote`'s shutdown handshake and its
    health check) honour that — the first is about to kill the server
    process anyway, and the second retires its own dedicated connection.
    """


class RemoteConnectionLostError(RemoteCallError):
    """
    Raised client-side when the connection broke around a :class:`Call`.

    Exists because the raw symptom is unhelpful: a device server that dies
    mid-call leaves the client's ``connection.recv()`` raising a bare
    ``EOFError`` with no message, no indication of which target was being
    called, and nothing to distinguish "the server process is gone" from
    a bug in this code. This names the call and the underlying socket
    error instead; :mod:`miainwoodpecker.devices.remote` re-raises it with
    the server process's exit status added, since only the client-side
    owner of the subprocess can look that up.

    A subclass of :class:`RemoteCallError` for the same reason
    :class:`RemoteCallTimeoutError` is — "the call did not succeed" needs
    only one ``except`` clause — but worth catching separately when the
    distinction matters, because this one means the session is over
    rather than that one call failed.
    """


def send_call(
    connection: Connection,
    lock: threading.Lock,
    call: Call,
    *,
    timeout_s: float | None = None,
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
    timeout_s : float | None
        Seconds to wait for the reply, or ``None`` (the default) to wait
        indefinitely. Waiting forever is right for ordinary device calls
        — an acquisition genuinely takes as long as it takes, and a
        wrong guess would abort a good exposure — and wrong only for
        calls whose whole purpose is to not hang the application, which
        is why this is opt-in per call rather than a global setting.

    Returns
    -------
    object
        The call's return value.

    Raises
    ------
    RemoteCallError
        If the call raised on the server side.
    RemoteCallTimeoutError
        If ``timeout_s`` elapsed with no reply.
    RemoteConnectionLostError
        If the connection broke while sending the call or awaiting its
        reply — which is what a device server dying mid-call looks like
        from here.
    """
    with lock:
        try:
            connection.send(call)
            if timeout_s is not None and not connection.poll(timeout_s):
                msg = (
                    f"remote call {call.target}.{call.method}() did not reply "
                    f"within {timeout_s}s"
                )
                raise RemoteCallTimeoutError(msg)
            result = connection.recv()
        except (EOFError, OSError) as error:
            # EOFError is what recv() raises when the peer's socket closed,
            # which for a subprocess means "it died"; OSError covers the
            # send side (a broken pipe) and an already-closed connection.
            # Both arrive with no message at all, hence the wrapping.
            detail = str(error) or type(error).__name__
            msg = (
                f"remote call {call.target}.{call.method}() lost its "
                f"connection to the device server ({detail})"
            )
            raise RemoteConnectionLostError(msg) from error
    if result.error is not None:
        msg = f"remote call {call.target}.{call.method}() failed: {result.error}"
        raise RemoteCallError(msg)
    return result.value
