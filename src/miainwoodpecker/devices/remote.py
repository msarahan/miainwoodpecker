"""
MIT-licensed client for the GPL-3.0 device server.

Implements :class:`~miainwoodpecker.devices.interface.Camera`,
:class:`~miainwoodpecker.devices.interface.Scanner` and
:class:`~miainwoodpecker.devices.interface.InstrumentController` by
sending :class:`~miainwoodpecker.devices.rpc.Call` objects to a
:mod:`miainwoodpecker.devices.nion_server` subprocess and waiting for
:class:`~miainwoodpecker.devices.rpc.Result` objects back. This module
imports nothing from ``nion.*`` — that is the entire point: everything
above the device layer (acquisition, viewer, storage) can depend on this
module, and by extension on Nion hardware, without the running
application ever linking GPL-3.0 code into its own process (see
docs/migration-plan.md, §6).

``remote_instrument(backend=...)`` chooses which devices the server
builds; ``remote_simulated_instrument()`` is the unchanged
nionswift-usim entry point, kept as a thin delegation because the viewer
and the benchmark scripts are written against it.

Process lifecycle — the interesting part, and load-bearing rather than
polite. A named POSIX shared-memory segment is *not* reclaimed when the
process that created it dies (unlike its threads, sockets, or anonymous
memory): it is a persistent tmpfs entry until explicitly ``unlink()``-ed,
so the server must be given the chance to retire the reused segments
:mod:`miainwoodpecker.devices.shared_frame` hands out. Teardown therefore
goes, in order:

1. Ask the server to shut down (``instrument.shutdown()``). It stops the
   cameras, parks the instrument (blanks the beam if a blanker exists),
   closes every device — which unlinks that device's segment — and
   acknowledges with a report of what it did. Only then does it exit.
2. Wait a bounded time for the process to exit on its own.
3. If either step times out or errors, fall back to closing each device
   individually (the pre-handshake behaviour, which unlinks the same
   segments over a different connection) and then ``Popen.terminate()``,
   escalating to ``kill()``. A hung server must never hang the
   application, and it must not be allowed to leak segments either.
4. Once the process is gone, whatever killed it, unlink the segments this
   client attached to — via ``SharedFrameReader.unlink_orphan``, which is
   safe precisely because the writer's process is dead.

Step 4 is belt and braces, and the measurement behind that is worth
knowing rather than rediscovering. A ``SIGKILL``-ed server normally leaks
nothing anyway, because ``multiprocessing``'s ``resource_tracker`` is a
separate child process that registers every segment the writer creates and
unlinks them when the server's end of its pipe closes. That is a CPython
implementation detail, not a guarantee, and it fails exactly when the
tracker dies alongside the server — a process-group kill, a cgroup OOM
kill, a container stop. Step 4 covers that, for every segment a frame was
actually read from; ``unlink_orphan`` documents the narrow window even it
cannot reach.

Liveness — three conditions, three answers. Before this, the application
had no way to tell a working device server from a dead or wedged one, and
no defined behaviour when one died mid-session; a call either blocked
forever or surfaced a bare ``EOFError``.
:meth:`RemoteInstrument.check_health` now separates them:

- **Responsive.** ``instrument.health()`` answered. Cheap by construction
  (the server reads process state only, no device, no vendor object) and
  carried on its *own* connection to the instrument target, so it neither
  queues behind an in-flight control call nor perturbs an acquisition.
- **Exited.** ``Popen.poll()`` says so. Reported with the exit status, or
  the signal that killed it.
- **Unresponsive.** The bounded wait elapsed with the process still
  alive. Because ``health`` takes no device lock, this genuinely means
  wedged rather than busy — which is exactly why the bound belongs here
  and not on ordinary device calls, where a real acquisition takes as long
  as it takes and a wrong timeout would abort a good exposure (§6).

**No reconnect, deliberately.** A device server is not a stateless web
backend: a fresh subprocess is a fresh instrument construction, so a
started camera, the scan settings in use, and every instrument control
(defocus, stage, blanker) revert. Worse, the server most likely to need
reconnecting is one that died without parking, leaving the *column* in a
state nothing on this side knows. Silently re-establishing a connection
would therefore hand the operator a plausible-looking session whose
device state is quietly wrong, and any recording in progress would keep
appending frames from a differently-configured instrument to the same
file — a corrupted scientific record, which is worse than a stopped one.
A dedicated ``reconnect()`` was rejected for the same reason plus a
simpler one: :func:`remote_instrument` already *is* the way to get a
session, so a second entry point could only duplicate it while implying a
continuity it cannot provide. What this module does instead is fail fast
and say what happened, and let the caller decide to start a new session.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import typing
from dataclasses import dataclass

from miainwoodpecker.devices.rpc import (
    BACKENDS,
    HARDWARE_BACKEND,
    SIMULATED_BACKEND,
    Call,
    RemoteCallError,
    RemoteCallTimeoutError,
    RemoteConnectionLostError,
    disable_nagle,
    send_call,
)
from miainwoodpecker.devices.rpc import (
    TARGET_NAMES as _TARGET_NAMES,
)
from miainwoodpecker.devices.shared_frame import SharedFrameReader, SharedFrameRef

# The one server-side constant the client must agree on beyond the wire
# protocol: which exit status means "retry with different ports".
PORT_UNAVAILABLE_EXIT_STATUS = 4

if typing.TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from multiprocessing.connection import Connection

    from miainwoodpecker.devices.interface import (
        CameraParameters,
        Frame,
        ScanParameters,
    )

# Re-exported from rpc.py so callers of this client keep importing the
# backend vocabulary from the module whose API they are using, while
# rpc.py stays the one place both peers agree on it.
__all__ = [
    "BACKENDS",
    "HARDWARE_BACKEND",
    "SERVER_EXITED",
    "SERVER_RESPONSIVE",
    "SERVER_UNRESPONSIVE",
    "SIMULATED_BACKEND",
    "DeviceServerStartupError",
    "RemoteCamera",
    "RemoteInstrument",
    "RemoteInstrumentDevices",
    "RemoteScanner",
    "ServerHealth",
    "remote_instrument",
    "remote_simulated_instrument",
]

# Set by the test suite to arm the device server's test hooks. Read
# here rather than passed as a parameter so no shipped call site can
# reach it: remote_instrument() has no argument that turns them on.
_TEST_HOOKS_ENV_VAR = "MIAINWOODPECKER_ENABLE_TEST_HOOKS"

# How many times to re-pick ports when the server reports one was taken
# between the client's probe and the server's bind.
_PORT_RETRY_ATTEMPTS = 3
# How long to watch a freshly spawned server for that specific early
# exit. Short: it happens before the Nion import finishes, and a server
# still alive after it is simply starting normally.
_PORT_COLLISION_GRACE_S = 0.4

_CONNECT_TIMEOUT_S = 15.0
_TERMINATE_TIMEOUT_S = 5.0
# How long to wait for the graceful-shutdown acknowledgement. Generous
# because the server is stopping detectors and blanking the beam, which on
# real hardware is a physical operation, not a flag flip - but bounded,
# because the whole point of the handshake is to not hang the application.
_SHUTDOWN_TIMEOUT_S = 10.0
# How long to wait for a health reply. Small on purpose, and safe to be
# small for one reason: the server's health handler reads process state
# only - no device, no vendor object, no lock - and runs on its own
# connection's handler thread, so nothing an acquisition does can delay it.
# A server that misses this really is wedged.
_HEALTH_TIMEOUT_S = 5.0
# Grace given to a process whose connection just broke, before concluding
# it is alive-but-unresponsive rather than dead. A server dying mid-call
# closes its socket a moment before the kernel finishes reaping it, so
# polling immediately can misclassify the most common failure there is.
# Only ever paid on the failure path.
_EXIT_GRACE_S = 0.5


# The three conditions a caller must be able to tell apart, because the
# right response differs: retry/continue, start a new session, or
# investigate a server that is running but not working.
SERVER_RESPONSIVE = "responsive"
SERVER_EXITED = "exited"
SERVER_UNRESPONSIVE = "unresponsive"


class DeviceServerStartupError(RuntimeError):
    """Raised when the device server process died before it served anything."""


@dataclass(frozen=True)
class ServerHealth:
    """
    The outcome of one liveness check on a device server.

    Returned rather than raised: this is the call an application polls
    routinely, and control flow through exceptions for the ordinary
    "everything is fine" answer would be the wrong shape. Ordinary device
    calls against a dead server still raise — see
    :class:`~miainwoodpecker.devices.rpc.RemoteConnectionLostError`.

    Attributes
    ----------
    state : str
        One of :data:`SERVER_RESPONSIVE`, :data:`SERVER_EXITED`,
        :data:`SERVER_UNRESPONSIVE`.
    detail : str
        One human-readable line naming what was observed, suitable for a
        status bar or a log. Always populated, including when healthy.
    report : dict[str, object] | None
        The server's own health report (pid, backend, uptime, served
        targets, still-open devices, whether a shutdown has begun), or
        ``None`` if it never answered.
    exit_status : int | None
        ``Popen.returncode`` when the process has exited: negative means
        killed by that signal. ``None`` while it is still running.
    latency_ms : float | None
        Round-trip time of the health call, or ``None`` if it did not
        complete. Worth watching over a session: a server on its way to
        wedged usually gets slow first.
    """

    state: str
    detail: str
    report: dict[str, object] | None = None
    exit_status: int | None = None
    latency_ms: float | None = None

    @property
    def is_responsive(self) -> bool:
        """Return whether the server answered its health check."""
        return self.state == SERVER_RESPONSIVE


def _exit_description(status: int | None) -> str:
    """
    Describe a ``Popen.returncode`` in the terms an operator needs.

    Parameters
    ----------
    status : int | None
        ``Popen.returncode``: ``None`` if still running, negative if the
        process was killed by ``-status``.

    Returns
    -------
    str
        A clause naming what happened to the process.
    """
    if status is None:
        return "the device server process is still running"
    if status < 0:
        try:
            name = signal.Signals(-status).name
        except ValueError:  # pragma: no cover - defensive, all real signals resolve
            name = f"signal {-status}"
        return f"the device server process was killed by {name}"
    return f"the device server process exited with status {status}"


_UNRECOVERABLE = (
    "this session cannot be recovered - a new device server is a new "
    "instrument construction, so acquisition and control state do not carry "
    "over; start a fresh remote_instrument() session"
)


def _lost_server_message(
    target: str,
    method: str,
    error: RemoteConnectionLostError,
    process: subprocess.Popen[bytes] | None,
) -> str:
    """
    Describe a connection-lost failure with the server's fate named.

    :mod:`miainwoodpecker.devices.rpc` cannot say this itself: it knows the
    socket broke, but only this module owns the ``Popen`` handle that says
    whether the process exited, and with what.

    Parameters
    ----------
    target : str
        The RPC target the failed call was made on.
    method : str
        The method that was being called.
    error : RemoteConnectionLostError
        The error raised by :func:`~miainwoodpecker.devices.rpc.send_call`,
        used verbatim when there is no process handle to consult.
    process : subprocess.Popen[bytes] | None
        The server process, or ``None`` if this handle does not own one.

    Returns
    -------
    str
        A message naming the call, the process's fate, and the fact that
        the session is unrecoverable.
    """
    if process is None:
        return str(error)
    status = process.poll()
    if status is None:
        # Give the kernel a moment: a server dying mid-call closes its
        # socket just before it finishes exiting, and "connection broke but
        # the process is fine" is a much rarer and stranger claim to make.
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_EXIT_GRACE_S)
        status = process.poll()
    return (
        f"remote call {target}.{method}() failed: {_exit_description(status)}. "
        f"{_UNRECOVERABLE}"
    )


def _free_port() -> int:
    """Return a currently-unused localhost port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("localhost", 0))
        return probe.getsockname()[1]


def _connect_with_retry(
    port: int,
    authkey: bytes,
    deadline: float,
    process: subprocess.Popen[bytes],
) -> Connection:
    """
    Connect to a Listener that may not have started accepting yet.

    Checks the server process on every attempt rather than only the clock.
    Without that, the most likely real-world failure — asking for the
    hardware backend on a machine with no instrument attached, where the
    server prints what it looked for and exits — would show up here as a
    silent 15-second wait ending in ``ConnectionRefusedError``, throwing
    away the one diagnostic that matters.

    Parameters
    ----------
    port : int
        Port the target's Listener was told to bind.
    authkey : bytes
        Shared secret for the connection.
    deadline : float
        ``time.monotonic()`` value after which to give up.
    process : subprocess.Popen[bytes]
        The server process, watched for early exit.

    Returns
    -------
    Connection
        The connected client end, with Nagle disabled.

    Raises
    ------
    DeviceServerStartupError
        If the server exited before accepting connections, or if the
        deadline passed with a connection attempt still blocked inside
        the authentication handshake — the wedged-at-startup case below.
    ConnectionRefusedError
        If ``deadline`` passed with the server still alive but not listening.
    OSError
        For any other socket-level failure past the deadline.
    """
    while True:
        if process.poll() is not None:
            msg = (
                f"the device server exited with status {process.returncode} "
                f"before accepting connections. Its own diagnostic went to "
                f"stderr (inherited from this process): a hardware backend "
                f"with no instrument present reports which nion Registry "
                f"components it looked for, which plug-ins it loaded or "
                f"skipped, and how to name the right one."
            )
            raise DeviceServerStartupError(msg)
        try:
            connection = _connect_once(port, authkey, deadline)
        except (ConnectionRefusedError, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)
        else:
            disable_nagle(connection)
            return connection


def _connect_once(port: int, authkey: bytes, deadline: float) -> Connection:
    """
    Make one connection attempt, bounded by the caller's deadline.

    ``multiprocessing.connection.Client`` blocks with **no timeout**
    through both the TCP connect and the authentication handshake, so
    the retry loop's deadline could never fire while an attempt was in
    flight. Against a server that accepts the TCP connection but never
    completes the handshake, the client hung forever inside its first
    attempt — not a hypothetical: a server whose instrument accept
    thread crashed at startup produced exactly this, hanging every test
    run that touched it until something external killed it.

    The attempt therefore runs on a scrap daemon thread and is abandoned
    if the deadline passes. Abandoning is safe precisely because the
    result is discarded: the thread holds only its own socket, which dies
    with it or the process, and nothing else ever learns the connection
    existed. A thread rather than a socket timeout, deliberately —
    bounding it with ``socket.setdefaulttimeout`` would be process-global
    state, and this project has already been bitten once by fd-mode side
    effects on this exact path (see ``rpc.disable_nagle``).

    Parameters
    ----------
    port : int
        Port to connect to on localhost.
    authkey : bytes
        Shared secret for the connection handshake.
    deadline : float
        ``time.monotonic()`` value after which to abandon the attempt.

    Returns
    -------
    Connection
        The connected, authenticated client end.

    Raises
    ------
    DeviceServerStartupError
        If the deadline passed with the attempt still blocked — a server
        that is alive but not completing handshakes, which retrying will
        not fix.
    ConnectionRefusedError
        If nothing is listening on the port yet (the ordinary
        during-startup case the retry loop exists for).
    OSError
        For other socket-level failures.
    """
    from multiprocessing.connection import Client  # noqa: PLC0415

    outcome: list[object] = []
    done = threading.Event()

    def _attempt() -> None:
        try:
            outcome.append(Client(("localhost", port), authkey=authkey))
        except BaseException as error:  # noqa: BLE001 - re-raised on the caller's thread
            outcome.append(error)
        done.set()

    thread = threading.Thread(target=_attempt, name="device-connect", daemon=True)
    thread.start()
    if not done.wait(timeout=max(0.05, deadline - time.monotonic())):
        msg = (
            f"a connection attempt to the device server (port {port}) was "
            f"still blocked in the handshake when the connect deadline "
            f"passed - the server process is alive but not completing "
            f"connections, which retrying will not fix"
        )
        raise DeviceServerStartupError(msg)
    # Failures cross the thread boundary as objects and are re-raised here
    # as fresh instances of the concrete classes the retry loop dispatches
    # on, with the original chained as __cause__ so no diagnostic is lost.
    result = outcome[0]
    if isinstance(result, ConnectionRefusedError):
        raise ConnectionRefusedError(*result.args) from result
    if isinstance(result, OSError):
        raise OSError(*result.args) from result
    if isinstance(result, BaseException):
        msg = f"the connection attempt failed unexpectedly: {result!r}"
        raise DeviceServerStartupError(msg) from result
    return typing.cast("Connection", result)


class _RemoteDevice:
    """
    Shared plumbing for a device driven over one RPC connection.

    Holds the connection, the lock serializing round trips on it, the
    shared-memory reader that large frames arrive through, and a handle on
    the server process — the last one only so a failed call can say
    *whether the server died* rather than raising a bare ``EOFError``.
    """

    def __init__(
        self,
        connection: Connection,
        target: str,
        process: subprocess.Popen[bytes] | None = None,
    ) -> None:
        self._connection = connection
        self._target = target
        self._lock = threading.Lock()
        # Serializes a frame call *and* its shared-memory copy-out as one
        # unit. self._lock alone is not enough: send_call releases it as
        # soon as the reply is in hand, so a second thread could send the
        # next scan_frame while this one is still copying out of the
        # segment the server is about to overwrite. The reused-segment
        # design is safe only while exactly one request/response is in
        # flight per target (shared_frame.py's module docstring), and
        # "the caller promises to use one thread" is not something this
        # class can check. Always acquired *before* self._lock, never the
        # reverse, so the two cannot deadlock.
        self._frame_lock = threading.Lock()
        self._reader = SharedFrameReader()
        self._process = process

    def _call(self, method: str, *args: object, **kwargs: object) -> object:
        """
        Send one call to this device's target and return its value.

        No timeout, deliberately: an acquisition takes as long as it takes,
        and a wrong guess would abort a good exposure (§6). A *dead* server
        needs no timeout to be detected — the socket closes and the call
        fails at once — so the only thing this adds is naming the cause.

        Parameters
        ----------
        method : str
            Method name on the remote target.
        *args : object
            Positional arguments.
        **kwargs : object
            Keyword arguments.

        Returns
        -------
        object
            The call's return value.

        Raises
        ------
        RemoteConnectionLostError
            If the server process died or the connection broke, with the
            exit status or signal named.
        """
        try:
            return send_call(
                self._connection,
                self._lock,
                Call(self._target, method, args, kwargs),
            )
        except RemoteConnectionLostError as error:
            raise RemoteConnectionLostError(
                _lost_server_message(self._target, method, error, self._process),
            ) from error

    def _frame(self, method: str, *args: object) -> Frame:
        """
        Make a call that returns a frame, following a shared-memory reference.

        The call and the copy-out are one critical section: see
        ``self._frame_lock``'s comment for why splitting them silently
        corrupts frames when two threads drive one device.
        """
        with self._frame_lock:
            result = self._call(method, *args)
            if isinstance(result, SharedFrameRef):
                return self._reader.read(result)
            return typing.cast("Frame", result)

    def detach(self) -> None:
        """
        Detach from the server's shared segment without unlinking it.

        Used on the graceful-shutdown path, where the server has already
        retired the segment itself and there is no device left to call
        ``close`` on.
        """
        self._reader.close()

    def unlink_orphaned_segment(self) -> str | None:
        """
        Detach and unlink this device's segment, the server having exited.

        Only correct once the server process is known dead — see
        :meth:`~miainwoodpecker.devices.shared_frame.SharedFrameReader.unlink_orphan`
        for why the reader is allowed to break the writer's ownership in
        that one case, and for what it still cannot reach.

        Returns
        -------
        str | None
            The segment name unlinked, or ``None`` if this device never
            used shared memory (every frame stayed under the threshold).
        """
        return self._reader.unlink_orphan()

    def close(self) -> None:
        """Release the remote device and detach from its shared segment."""
        self._call("close")
        self._reader.close()


class RemoteCamera(_RemoteDevice):
    """A ``Camera`` implementation that delegates over IPC to a device server."""

    @property
    def camera_id(self) -> str:
        """Return the remote device's camera id."""
        return typing.cast("str", self._call("camera_id"))

    @property
    def binning_values(self) -> typing.Sequence[int]:
        """Return the binning factors the remote device supports."""
        return typing.cast("typing.Sequence[int]", self._call("binning_values"))

    def parameters(self) -> CameraParameters:
        """Return the settings the remote device's next frame will use."""
        return typing.cast("CameraParameters", self._call("parameters"))

    def configure(self, parameters: CameraParameters) -> CameraParameters:
        """
        Apply settings to the remote device and return what it accepted.

        Parameters
        ----------
        parameters : CameraParameters
            The requested exposure and binning.

        Returns
        -------
        CameraParameters
            What the device took, which is not necessarily what was asked.
        """
        return typing.cast("CameraParameters", self._call("configure", parameters))

    def start(self) -> None:
        """Begin continuous acquisition on the remote device."""
        self._call("start")

    def stop(self) -> None:
        """Pause continuous acquisition on the remote device."""
        self._call("stop")

    def acquire_frame(self) -> Frame:
        """Return the next available frame from the remote device."""
        return self._frame("acquire_frame")


class RemoteScanner(_RemoteDevice):
    """A ``Scanner`` implementation that delegates over IPC to a device server."""

    @property
    def scanner_id(self) -> str:
        """Return the remote device's scan device id."""
        return typing.cast("str", self._call("scanner_id"))

    @property
    def channel_names(self) -> typing.Sequence[str]:
        """Return the remote device's detector channel names."""
        return typing.cast("typing.Sequence[str]", self._call("channel_names"))

    def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame:
        """Scan and return a single frame from the remote device."""
        return self._frame("scan_frame", parameters, channel)


class RemoteInstrument:
    """
    An ``InstrumentController`` that delegates over IPC to a device server.

    Also carries the three calls that are about the *server* rather than
    the instrument — :meth:`describe`, :meth:`check_health` and
    :meth:`shutdown` — because the server serves all of them on the same
    ``instrument`` target, and because the client needs one place to ask
    what the instrument has, whether it is still there, and for a clean
    exit.

    The health check runs over a *second* connection to that same target,
    for two reasons that both matter in practice. It must not queue behind
    an in-flight control call on the shared lock — a real stage move takes
    seconds, and a status poll that blocks for it is useless. And a health
    call that times out leaves its connection poisoned (a late reply would
    be mistaken for the next call's answer), which must not cost the
    session its instrument controls.
    """

    def __init__(
        self,
        connection: Connection,
        target: str = "instrument",
        *,
        health_connection: Connection | None = None,
        process: subprocess.Popen[bytes] | None = None,
    ) -> None:
        self._connection = connection
        self._target = target
        self._lock = threading.Lock()
        self._process = process
        self._health_connection = health_connection
        self._health_lock = threading.Lock()
        self._health_poisoned = False

    def _call(
        self,
        method: str,
        *args: object,
        timeout_s: float | None = None,
        **kwargs: object,
    ) -> object:
        """
        Send one call to the instrument target and return its value.

        Parameters
        ----------
        method : str
            Method name on the instrument target.
        *args : object
            Positional arguments.
        timeout_s : float | None
            Bounded wait, or ``None`` to wait indefinitely (the default,
            for the same reason device calls do).
        **kwargs : object
            Keyword arguments.

        Returns
        -------
        object
            The call's return value.

        Raises
        ------
        RemoteConnectionLostError
            If the server process died or the connection broke, with the
            exit status or signal named.
        """
        try:
            return send_call(
                self._connection,
                self._lock,
                Call(self._target, method, args, kwargs),
                timeout_s=timeout_s,
            )
        except RemoteConnectionLostError as error:
            raise RemoteConnectionLostError(
                _lost_server_message(self._target, method, error, self._process),
            ) from error

    def check_health(self, *, timeout_s: float = _HEALTH_TIMEOUT_S) -> ServerHealth:
        """
        Classify the device server as responsive, exited, or unresponsive.

        Never raises: this is meant to be called routinely, including from
        a UI timer, and the answer "the server is gone" is information
        rather than an error. Cheap on the server side (process state only,
        no device touched) and carried on its own connection, so it neither
        perturbs an acquisition nor waits on one.

        A timed-out check retires the probe connection permanently, because
        the request was already sent and a late reply would be mistaken for
        a later check's answer. Subsequent calls then report
        :data:`SERVER_UNRESPONSIVE` from that fact alone — which is sound,
        since nothing about this design lets a wedged server recover.

        Parameters
        ----------
        timeout_s : float
            Bounded wait for the reply.

        Returns
        -------
        ServerHealth
            The verdict, with a one-line ``detail`` naming what was seen.
        """
        already_known = self._health_without_asking()
        if already_known is not None:
            return already_known
        assert self._health_connection is not None  # noqa: S101 - _health_without_asking checked
        started = time.monotonic()
        try:
            report = send_call(
                self._health_connection,
                self._health_lock,
                Call(self._target, "health"),
                timeout_s=timeout_s,
            )
        except RemoteCallTimeoutError:
            self._health_poisoned = True
            return ServerHealth(
                state=SERVER_UNRESPONSIVE,
                detail=(
                    f"the device server did not answer a health check within "
                    f"{timeout_s}s while still running (pid "
                    f"{self._process.pid if self._process else '?'}). Because a "
                    f"health check touches no device, this means wedged rather "
                    f"than busy. {_UNRECOVERABLE}"
                ),
            )
        except RemoteConnectionLostError as error:
            return self._health_after_lost_connection(error)
        except RemoteCallError as error:
            # The server answered, and said the call failed: it is alive
            # enough to talk, so this is a bug rather than a liveness
            # problem, and saying "exited" would be wrong.
            return ServerHealth(
                state=SERVER_UNRESPONSIVE,
                detail=f"the device server rejected its own health check: {error}",
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
        latency_ms = (time.monotonic() - started) * 1000.0
        if not isinstance(report, dict):
            # Documented as never raising, so a server that answered
            # with something unexpected is reported as unresponsive
            # rather than raising AttributeError out of a call meant to
            # be safe to poll from a UI timer.
            return ServerHealth(
                state=SERVER_UNRESPONSIVE,
                detail=f"device server answered health with {type(report).__name__}",
            )
        answer = report
        try:
            uptime_s = float(answer.get("uptime_s", 0.0))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            uptime_s = 0.0
        open_devices = typing.cast(
            "Sequence[str]",
            answer.get("open_devices") or (),
        )
        return ServerHealth(
            state=SERVER_RESPONSIVE,
            detail=(
                f"device server pid {answer.get('pid')} responsive on the "
                f"{answer.get('backend')} backend, up {uptime_s:.0f}s, "
                f"devices open: {', '.join(open_devices) or 'none'}"
            ),
            report=answer,
            latency_ms=latency_ms,
        )

    def _health_without_asking(self) -> ServerHealth | None:
        """
        Answer a health check from what is already known, if that is enough.

        Three cases need no round trip: the process has exited (the
        strongest possible evidence, and free), no probe connection was
        opened, or an earlier probe timed out and was retired.

        Returns
        -------
        ServerHealth | None
            The verdict, or ``None`` if the server must actually be asked.
        """
        status = self._process.poll() if self._process is not None else None
        if status is not None:
            return ServerHealth(
                state=SERVER_EXITED,
                detail=f"{_exit_description(status)}. {_UNRECOVERABLE}",
                exit_status=status,
            )
        if self._health_connection is None:
            return ServerHealth(
                state=SERVER_UNRESPONSIVE,
                detail=(
                    "no health connection was opened for this instrument, so "
                    "its liveness cannot be checked"
                ),
            )
        if self._health_poisoned:
            return ServerHealth(
                state=SERVER_UNRESPONSIVE,
                detail=(
                    "an earlier health check timed out and retired this probe; "
                    f"the server has not answered since. {_UNRECOVERABLE}"
                ),
            )
        return None

    def _health_after_lost_connection(
        self,
        error: RemoteConnectionLostError,
    ) -> ServerHealth:
        """
        Classify a health check whose connection broke rather than timing out.

        Parameters
        ----------
        error : RemoteConnectionLostError
            The failure raised by the probe.

        Returns
        -------
        ServerHealth
            :data:`SERVER_EXITED` if the process has (or shortly does) exit,
            which is the overwhelmingly likely cause; otherwise
            :data:`SERVER_UNRESPONSIVE`, since a live server whose socket
            broke cannot answer either.
        """
        self._health_poisoned = True
        if self._process is None:
            return ServerHealth(state=SERVER_UNRESPONSIVE, detail=str(error))
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=_EXIT_GRACE_S)
        status = self._process.poll()
        return ServerHealth(
            state=SERVER_EXITED if status is not None else SERVER_UNRESPONSIVE,
            detail=f"{_exit_description(status)}. {_UNRECOVERABLE}",
            exit_status=status,
        )

    def describe(self) -> dict[str, object]:
        """Return the server's report of its backend, targets, and controls."""
        return typing.cast("dict[str, object]", self._call("describe"))

    def stage_size_nm(self) -> float:
        """Return the stage extent, in nanometres."""
        return typing.cast("float", self._call("stage_size_nm"))

    def available_controls(self) -> Sequence[str]:
        """Return the neutral control names the remote instrument implements."""
        return typing.cast("Sequence[str]", self._call("available_controls"))

    def stage_position_nm(self) -> tuple[float, float]:
        """Return the stage position as ``(y, x)``, in nanometres."""
        position = typing.cast(
            "tuple[float, float]",
            self._call("stage_position_nm"),
        )
        return (float(position[0]), float(position[1]))

    def set_stage_position_nm(self, y_nm: float, x_nm: float) -> None:
        """
        Move the stage to an absolute ``(y, x)`` position, in nanometres.

        Parameters
        ----------
        y_nm : float
            Target position along the slow scan axis, in nanometres.
        x_nm : float
            Target position along the fast scan axis, in nanometres.
        """
        self._call("set_stage_position_nm", y_nm, x_nm)

    def defocus_nm(self) -> float:
        """Return the defocus, in nanometres."""
        return typing.cast("float", self._call("defocus_nm"))

    def set_defocus_nm(self, defocus_nm: float) -> None:
        """
        Set the defocus, in nanometres.

        Parameters
        ----------
        defocus_nm : float
            Target defocus, in nanometres.
        """
        self._call("set_defocus_nm", defocus_nm)

    def energy_offset_ev(self) -> float:
        """Return the spectrometer energy offset, in electronvolts."""
        return typing.cast("float", self._call("energy_offset_ev"))

    def set_energy_offset_ev(self, offset_ev: float) -> None:
        """
        Set the spectrometer energy offset, in electronvolts.

        Parameters
        ----------
        offset_ev : float
            Target offset, in electronvolts.
        """
        self._call("set_energy_offset_ev", offset_ev)

    def is_beam_blanked(self) -> bool:
        """Return whether the beam is currently blanked."""
        return bool(self._call("is_beam_blanked"))

    def set_beam_blanked(self, *, blanked: bool) -> None:
        """
        Blank or unblank the beam.

        Parameters
        ----------
        blanked : bool
            ``True`` to blank the beam, ``False`` to unblank it.
        """
        self._call("set_beam_blanked", blanked=blanked)

    def park(self) -> None:
        """Put the instrument in a safe state (blank the beam, if it can)."""
        self._call("park")

    def shutdown(self) -> dict[str, object]:
        """
        Ask the server to park, release its devices, and exit; return its report.

        Returns
        -------
        dict[str, object]
            The server's park report: what was stopped, whether the beam
            ended up blanked, which devices were released, and any errors
            it hit while doing so.
        """
        return typing.cast(
            "dict[str, object]",
            self._call("shutdown", timeout_s=_SHUTDOWN_TIMEOUT_S),
        )


@dataclass(frozen=True)
class RemoteInstrumentDevices:
    """
    Handles to the devices of one microscope, running in a server subprocess.

    Same shape as the in-process ``nion_server.InstrumentDevices``
    (deliberately: code written against one works against the other
    unchanged).

    Attributes
    ----------
    ronchigram_camera : RemoteCamera | None
        The Ronchigram camera, if this instrument has one.
    eels_camera : RemoteCamera | None
        The EELS camera, if this instrument has one.
    scanner : RemoteScanner
        The scan device (HAADF/MAADF channels on the simulator).
    instrument : RemoteInstrument
        Stage/defocus/blanker controls.
    stage_size_nm : float
        The stage extent, useful for choosing a sensible
        ``ScanParameters.fov_nm``.
    """

    ronchigram_camera: RemoteCamera | None
    eels_camera: RemoteCamera | None
    scanner: RemoteScanner
    instrument: RemoteInstrument
    stage_size_nm: float


# Historical name, kept because the migration plan and README refer to it.
RemoteSimulatedInstrument = RemoteInstrumentDevices


def _start_server(
    backend: str,
    plugin_names: Sequence[str],
) -> tuple[dict[str, int], bytes, subprocess.Popen[bytes]]:
    """
    Spawn a device server, retrying if a chosen port was taken meanwhile.

    :func:`_free_port` picks ports by binding to port 0 and *releasing*
    the socket, so the port is only reserved by convention until the
    child binds it seconds later — after the subprocess has started and
    imported the whole Nion stack. Anything else on the machine can claim
    it in that window, and the more sessions start at once the likelier
    that is: a parallel test run is the realistic case, and the failure
    it produced was an anonymous traceback and a dead server.

    Retrying with fresh ports is the fix that fits the existing design.
    The alternative — binding in the parent and passing inherited fds —
    removes the window entirely but changes how the server is launched,
    which is a bigger change than the problem warrants for a race that
    resolves on the next attempt.

    Only :data:`~miainwoodpecker.devices.nion_server.PORT_UNAVAILABLE_EXIT_STATUS`
    is retried. A missing instrument or a crash would just fail again, so
    those surface immediately with their own diagnostics.

    Parameters
    ----------
    backend : str
        ``"simulated"`` or ``"hardware"``.
    plugin_names : Sequence[str]
        ``nionswift_plugin`` modules for the hardware backend.

    Returns
    -------
    tuple[dict[str, int], bytes, subprocess.Popen[bytes]]
        The bound ports, the shared authkey, and the running process.

    Raises
    ------
    DeviceServerStartupError
        If every attempt lost its ports to another process.
    """
    for _attempt in range(_PORT_RETRY_ATTEMPTS):
        ports = {name: _free_port() for name in _TARGET_NAMES}
        authkey = secrets.token_bytes(32)
        process = _spawn_server(ports, authkey, backend, plugin_names)
        # The server exits this way *before* serving anything, so a short
        # wait either catches the collision or finds it still starting up;
        # a still-running server is the normal case and falls straight
        # through to the caller's own connect-with-retry.
        try:
            status = process.wait(timeout=_PORT_COLLISION_GRACE_S)
        except subprocess.TimeoutExpired:
            return ports, authkey, process
        if status != PORT_UNAVAILABLE_EXIT_STATUS:
            # Some other startup outcome; hand it back so the connect path
            # raises with the server's own diagnostic rather than this
            # function inventing one.
            return ports, authkey, process
    msg = (
        f"the device server could not bind its ports on "
        f"{_PORT_RETRY_ATTEMPTS} attempts; something on this machine is "
        f"claiming localhost ports faster than they can be used"
    )
    raise DeviceServerStartupError(msg)


def _spawn_server(
    ports: typing.Mapping[str, int],
    authkey: bytes,
    backend: str,
    plugin_names: Sequence[str],
) -> subprocess.Popen[bytes]:
    """
    Launch the device server subprocess.

    Parameters
    ----------
    ports : typing.Mapping[str, int]
        Port to bind for each target name.
    authkey : bytes
        Shared secret for every Listener.
    backend : str
        ``"simulated"`` or ``"hardware"``.
    plugin_names : Sequence[str]
        ``nionswift_plugin`` modules for the hardware backend.

    Returns
    -------
    subprocess.Popen[bytes]
        The running server process.
    """
    # os.environ already carries PYTHONWARNINGS from shared_frame's
    # module-level setdefault (imported above), so the server subprocess
    # inherits the same resource_tracker warning suppression.
    env = {**os.environ, "MIAINWOODPECKER_AUTHKEY": authkey.hex()}
    plugin_arguments = [
        argument for name in plugin_names for argument in ("--plugin", name)
    ]
    # The server ignores its wedge/delay hooks unless explicitly armed,
    # so a test that wants one has to say so here. Setting the
    # environment variable alone no longer does anything, which is the
    # point: the environment is inherited wholesale, and an operator who
    # happened to have one set would otherwise get an instrument that
    # could not be shut down.
    hook_arguments = (
        ["--enable-test-hooks"] if os.environ.get(_TEST_HOOKS_ENV_VAR) else []
    )
    return subprocess.Popen(  # noqa: S603 - fixed argv shape, no shell
        [
            sys.executable,
            "-m",
            "miainwoodpecker.devices.nion_server",
            "--backend",
            backend,
            *plugin_arguments,
            *hook_arguments,
            *(str(ports[name]) for name in _TARGET_NAMES),
        ],
        env=env,
        # Its own process group, so a Ctrl-C in the launching terminal
        # does not SIGINT the server alongside the application. Sharing
        # one meant an interrupt raced the client's orderly teardown: the
        # server could die mid-acquisition before anything parked the
        # instrument, and a process-group kill is also the one case where
        # the resource_tracker cannot reclaim the shared-memory segments
        # (it dies in the same sweep). Teardown reaches the server by
        # signalling the process directly, which is unaffected.
        start_new_session=True,
    )


def _release_segments_of_a_dead_server(
    devices: Sequence[_RemoteDevice],
) -> None:
    """
    Detach from an exited server's segments, unlinking any it left behind.

    Unlinking unconditionally rather than only for an abnormal exit, which
    is both simpler and strictly safer. The rule that only the writer may
    unlink exists to protect a *live* writer that keeps recreating the
    segment; this function's precondition is that the writer's process is
    dead, so there is nothing left to protect, and an already-unlinked name
    costs one failed ``shm_open``. Making it conditional on the exit status
    would leave a real gap: a server can exit ``0`` having *recorded* a
    shared-memory error in its shutdown report, and detaching would then
    strand exactly the segment that needs reclaiming.

    Belt and braces rather than the only line of defence, and worth being
    precise about which: measurement showed the server's
    ``resource_tracker`` child normally survives a ``SIGKILL`` of the
    server and unlinks those segments itself. This covers the case where it
    does not — the tracker killed alongside the server, as a process-group
    or cgroup OOM kill does — and it is still not a complete guarantee, for
    the reason
    :meth:`~miainwoodpecker.devices.shared_frame.SharedFrameReader.unlink_orphan`
    spells out.

    Parameters
    ----------
    devices : Sequence[_RemoteDevice]
        Every device handle of the finished session.
    """
    for device in devices:
        with contextlib.suppress(Exception):
            device.unlink_orphaned_segment()


def _shut_down_server(
    instrument: RemoteInstrument,
    devices: Sequence[_RemoteDevice],
    process: subprocess.Popen[bytes],
) -> None:
    """
    Tear the server down gracefully if it answers, by force if it does not.

    Parameters
    ----------
    instrument : RemoteInstrument
        The instrument target carrying the shutdown handshake.
    devices : Sequence[_RemoteDevice]
        Every device handle, for the fallback path's per-device ``close()``
        (which is what unlinks the shared-memory segments when the
        handshake did not get that far).
    process : subprocess.Popen[bytes]
        The server process.
    """
    if process.poll() is not None:
        # Already gone: either a caller ran the handshake explicitly (exit
        # 0, nothing left to ask and nothing left to kill) or it died on
        # its own, in which case its segments need reclaiming from here.
        # Either way there is nobody to talk to, and the sweep is safe.
        _release_segments_of_a_dead_server(devices)
        return
    graceful = False
    try:
        report = instrument.shutdown()
    except Exception:  # noqa: BLE001, S110 - any failure means "fall back", not "raise"
        pass
    else:
        # A report listing errors means the server got *part* way through
        # releasing devices. Treat that as ungraceful so the per-device
        # fallback below gets a second attempt at the shared-memory
        # segments, which are the one resource killing the process cannot
        # reclaim.
        graceful = not report.get("errors")
        for device in devices:
            with contextlib.suppress(Exception):
                device.detach()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_TERMINATE_TIMEOUT_S)
    if not graceful or process.poll() is None:
        if process.poll() is not None:
            # It died while we were asking, so there is nobody to close the
            # devices over IPC and its segments are orphaned.
            _release_segments_of_a_dead_server(devices)
            return
        for device in devices:
            with contextlib.suppress(Exception):
                device.close()
        process.terminate()
        try:
            process.wait(timeout=_TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_TERMINATE_TIMEOUT_S)
        # The per-device close() above is what normally unlinks the
        # segments, and it did so over live connections. Sweeping again
        # costs nothing when they are already gone, and catches the case
        # where a close() failed against a server too wedged to serve it.
        _release_segments_of_a_dead_server(devices)


@contextlib.contextmanager
def remote_instrument(
    backend: str = SIMULATED_BACKEND,
    plugin_names: Sequence[str] = (),
) -> Iterator[RemoteInstrumentDevices]:
    """
    Spawn the device server subprocess for a backend and connect to it.

    The ``instrument`` target is connected first and asked to
    ``describe()`` itself, so the client only connects to the device
    targets the instrument actually has. usim always has both cameras; a
    real instrument need not.

    Parameters
    ----------
    backend : str
        ``"simulated"`` for nionswift-usim, ``"hardware"`` for real
        devices. Anything else raises :class:`ValueError` here, before a
        subprocess is spawned.

        This used to say the server rejected it and reported the error
        back, which was not true: ``open_instrument`` raises,
        ``main`` catches only ``HardwareNotAvailableError``, so the
        server died with a traceback and exit 1 and the client surfaced a
        ``DeviceServerStartupError`` whose message is about missing
        *hardware* — thoroughly misleading for a typo. The names were
        already defined here; nothing was checking against them.
    plugin_names : Sequence[str]
        ``nionswift_plugin`` modules providing hardware devices; ignored
        by the simulated backend.

    Yields
    ------
    RemoteInstrumentDevices
        Handles talking to the cameras, scanner, and instrument over IPC.

    Raises
    ------
    ValueError
        If ``backend`` is not one of :data:`BACKENDS`.
    """
    if backend not in BACKENDS:
        msg = (
            f"unknown backend {backend!r}; expected one of "
            f"{', '.join(sorted(BACKENDS))}"
        )
        raise ValueError(msg)
    ports, authkey, process = _start_server(backend, plugin_names)
    connections: dict[str, Connection] = {}
    try:
        deadline = time.monotonic() + _CONNECT_TIMEOUT_S
        connections["instrument"] = _connect_with_retry(
            ports["instrument"], authkey, deadline, process,
        )
        # A second connection to the same target, reserved for health
        # checks: see RemoteInstrument for why sharing the control
        # connection would make the check both slower and riskier. The
        # server accepts any number of connections per target, one handler
        # thread each, so this costs one socket and one idle thread.
        connections["instrument:health"] = _connect_with_retry(
            ports["instrument"], authkey, deadline, process,
        )
        instrument = RemoteInstrument(
            connections["instrument"],
            health_connection=connections["instrument:health"],
            process=process,
        )
        description = instrument.describe()
        served = typing.cast("Sequence[str]", description["targets"])
        for name in served:
            connections[name] = _connect_with_retry(
                ports[name], authkey, deadline, process,
            )

        cameras = {
            name: RemoteCamera(connections[name], name, process)
            for name in ("ronchigram_camera", "eels_camera")
            if name in connections
        }
        scanner = RemoteScanner(connections["scanner"], "scanner", process)
        devices: list[_RemoteDevice] = [*cameras.values(), scanner]
        try:
            yield RemoteInstrumentDevices(
                ronchigram_camera=cameras.get("ronchigram_camera"),
                eels_camera=cameras.get("eels_camera"),
                scanner=scanner,
                instrument=instrument,
                stage_size_nm=float(
                    typing.cast("float", description["stage_size_nm"]),
                ),
            )
        finally:
            _shut_down_server(instrument, devices, process)
    finally:
        for connection in connections.values():
            with contextlib.suppress(Exception):
                connection.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_TERMINATE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_TERMINATE_TIMEOUT_S)


@contextlib.contextmanager
def remote_simulated_instrument() -> Iterator[RemoteInstrumentDevices]:
    """
    Spawn the device server subprocess for nionswift-usim and connect to it.

    Yields
    ------
    RemoteInstrumentDevices
        Handles talking to the simulated cameras and scanner over IPC.
    """
    with remote_instrument(SIMULATED_BACKEND) as devices:
        yield devices
