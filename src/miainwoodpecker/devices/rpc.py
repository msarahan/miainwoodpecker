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
import pickle
import socket
import typing
from dataclasses import dataclass, field

if typing.TYPE_CHECKING:
    import threading
    from multiprocessing.connection import Connection

TARGET_NAMES = (
    "ronchigram_camera",
    "eels_camera",
    "camera",
    "scanner",
    "spectrum_detector",
    "instrument",
)
"""
Every named target a device server can serve, in **argv order**.

The order is part of the protocol, not a presentation choice: the client
passes one port per target positionally on the server's command line, and
the server zips them back against this same tuple. Both halves used to
declare their own copy, so reordering one silently bound the scanner's
port to the EELS camera — a mismatch ``strict=True`` cannot catch,
because the *count* still agrees. One definition, on the side of the
boundary that has no vendor dependency, removes the possibility.

**This tuple is append-only, and ``spectrum_detector`` is the first
thing appended to it.** New names go immediately before ``instrument``,
which stays last (pinned by ``tests/unit/test_rpc.py``), so every
existing name keeps its argv position and a server needs no change
beyond agreeing on this tuple — which every server in this tree does by
reading it at run time (``nargs=len(TARGET_NAMES)``), never by counting
for itself. The client allocates one port per name whether or not the
server serves it, and ``describe()`` decides which are connected, so the
cost of a name nobody serves is one unused localhost port.

docs/vendor-support.md proposes replacing this whole mechanism — bind
one well-known port for ``instrument``, let the server choose and report
the rest — and says the redesign should land *with* a second column
adapter, against a real device list. Adding a name here rather than
doing that redesign now is deliberate: an X-ray detector is not a second
column, and settling a protocol change in the same commit as a new
device shape would make both harder to review. It does make the case for
the redesign stronger, since the tuple is now Nion's device list plus a
detector class Nion does not have.
"""

DEVICE_TARGET_NAMES = (
    "ronchigram_camera",
    "eels_camera",
    "camera",
    "scanner",
    "spectrum_detector",
)
"""The targets that are devices, i.e. everything but ``instrument``."""

SPECTRUM_TARGET_NAMES = ("spectrum_detector",)
"""
The targets that produce spectra rather than frames.

One name, and neutral rather than ``eds``: the protocol behind it
(:class:`~miainwoodpecker.devices.interface.SpectrumDetector`) fits a
WDS spectrometer, a micro-XRF head, or a cathodoluminescence
spectrometer as well as an EDX silicon drift detector, and putting the
*technique* in the target name would be the same fiction as serving a
USB microscope on ``ronchigram_camera``. Which technique it is belongs
in the spectrum metadata, where ``camera_type`` already lives for
frames.

A tuple rather than a bare string for the reason
:data:`CAMERA_TARGET_NAMES` is one: the client iterates it to build
handles, and a second spectrum target (an instrument with both an EDX
and a WDS spectrometer is ordinary) then costs a name here rather than a
new code path.
"""

CAMERA_TARGET_NAMES = ("ronchigram_camera", "eels_camera", "camera")
"""
The targets that are cameras, in the order a client reports them.

``ronchigram_camera`` and ``eels_camera`` are Nion's device list showing
through, and they stay because the viewer, the scripts and every existing
recording use them. ``camera`` is the neutral slot: a detector, a webcam,
or anything else that produces frames and is not one of those two. It
exists because the alternative was worse — serving a USB microscope as a
"Ronchigram camera" would put a fiction in every file's target name while
the frame metadata told the truth. See docs/vendor-support.md for the
redesign that would remove the fixed list altogether, and why it is
waiting for a second *column* adapter rather than being done on spec.
"""

SIMULATED_BACKEND = "simulated"
"""Backend name selecting the software simulator."""

HARDWARE_BACKEND = "hardware"
"""Backend name selecting real instrument hardware via vendor plug-ins."""

BACKENDS = (SIMULATED_BACKEND, HARDWARE_BACKEND)
"""Every backend name the server accepts on its command line."""

# Below this, a Frame result travels as plain pickle over the socket
# instead of through shared memory. This is a *protocol* decision - it
# determines whether the client receives a Frame or a SharedFrameRef - so
# it belongs here with the rest of the wire vocabulary rather than in the
# GPL server module, where it lived until the MIT test suite had to
# import that module simply to learn a number.
#
# Measured with scripts/ipc_overhead_benchmark.py against the
# reused-segment writer (shared_frame.py) with Nagle disabled: pickle and
# shared memory are within noise of each other from ~30KB to ~500KB (both
# +0.3-0.5ms over a direct in-process call), and shared memory pulls
# ahead smoothly above that - +1.5ms at ~1MB, +2.8ms at 2.1MB, +9.5ms at
# 8.4MB, +25ms at 33.6MB, against naive pickle's +168ms at that largest
# size. 64KB sits inside the band measured as "doesn't matter much either
# way", so it is kept rather than tuned further.
SHARED_MEMORY_THRESHOLD_BYTES = 64 * 1024

COMPATIBLE_PICKLE_PROTOCOL = 4
"""
The highest pickle protocol an older embedded interpreter can still read.

Only relevant when the two ends of this protocol are **different
interpreters**, which the spawn path never produces (it launches
``sys.executable``) and an attached device server routinely does. The
motivating case is Gatan's: GMS embeds its own Miniconda environment, and
Gatan's own FAQ names Python 3.7.2 for it, while this project requires
3.11 or newer.

That combination breaks silently in one direction, which is why the
number is pinned here rather than left to chance.
``multiprocessing.connection.Connection.send`` serialises with
``_ForkingPickler.dumps(obj)`` — no protocol argument, so the *sender's*
``pickle.DEFAULT_PROTOCOL``. On 3.8 and later that is 5; Python 3.7's
``pickle.HIGHEST_PROTOCOL`` is 4, so it cannot read a protocol-5 stream at
all. Every ``Call`` this client sent would arrive as an unpickling error,
while every ``Result`` coming back (written by the older end at protocol 3
or 4) would decode perfectly — a failure that looks like a broken server
rather than a version mismatch.

Protocol 4 is readable by every Python 3.4 and later, and costs nothing:
protocol 5's addition is out-of-band buffers, which a plain
``pickle.dumps`` without a ``buffer_callback`` never emits anyway.

The *reply* direction needs no such cap. An older peer writes protocol 3
or 4 by default, and a newer interpreter reads both.
"""


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
    error_type : str | None
        The exception's class *name* on the server side, so a caller can
        distinguish a vendor ``TimeoutError`` from a ``ValueError``
        without parsing the message. Deliberately the name rather than
        the class: shipping a live exception object would mean
        unpickling server-defined types in the client, which is exactly
        the kind of coupling this protocol exists to avoid — and would
        make the boundary depend on the GPL side having its classes
        importable here. A name is plain data.

        Added with a default so it is wire-compatible in both directions:
        an older peer's ``Result`` unpickles with ``error_type=None``, and
        a newer one's extra field is simply ignored by code that does not
        read it.
    """

    value: object = None
    error: str | None = None
    error_type: str | None = None


class RemoteCallError(RuntimeError):
    """
    Raised client-side when a :class:`Call` failed on the server.

    Carries :attr:`error_type`, the name of the exception class that
    actually raised on the server, because everything used to arrive here
    as one indistinguishable ``RemoteCallError`` — a vendor timeout, a
    bad argument, and a full ``/dev/shm`` were separable only by matching
    on the message text. A caller that wants to retry a timeout but not a
    programming error now has something to branch on.

    ``error_type`` holds the server-side exception class name, or
    ``None`` when the failure happened client-side (a timeout, a lost
    connection) and so has no server exception behind it.
    """

    def __init__(self, message: str, *, error_type: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type


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
    pickle_protocol: int | None = None,
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
    pickle_protocol : int | None
        Cap the pickle protocol this call is serialised with, or ``None``
        (the default) to use the sending interpreter's own default. Set
        it when the peer is a *different, older* interpreter — see
        :data:`COMPATIBLE_PICKLE_PROTOCOL`. The bytes on the wire are
        otherwise identical: ``Connection.send`` is
        ``send_bytes(pickle.dumps(...))`` with the framing this reuses,
        so a capped call is indistinguishable from an ordinary one except
        in its protocol byte.

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
            if pickle_protocol is None:
                connection.send(call)
            else:
                connection.send_bytes(pickle.dumps(call, protocol=pickle_protocol))
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
        raise RemoteCallError(msg, error_type=result.error_type)
    return result.value
