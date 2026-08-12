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

``attached_instrument()`` is the same client against a device server this
process did **not** launch — one already running inside another
application, or on another machine. Everything below is about the spawn
path, which is the default and is unchanged; the attach path's own
reasoning is gathered at the bottom of this module, next to the code, and
the one thing worth knowing here is that it is a *transport direction*
rather than a second device API: identical ``Call``/``Result`` protocol,
identical device protocols, identical ``RemoteInstrumentDevices``.

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
import json
import os
import pathlib
import queue
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
    COMPATIBLE_PICKLE_PROTOCOL,
    HARDWARE_BACKEND,
    REPLAY_BACKEND,
    SIMULATED_BACKEND,
    Call,
    RemoteCallError,
    RemoteCallTimeoutError,
    RemoteConnectionLostError,
    disable_nagle,
    send_call,
)
from miainwoodpecker.devices.rpc import (
    CAMERA_TARGET_NAMES as _CAMERA_TARGET_NAMES,
)
from miainwoodpecker.devices.rpc import (
    SPECTRUM_TARGET_NAMES as _SPECTRUM_TARGET_NAMES,
)
from miainwoodpecker.devices.rpc import (
    TARGET_NAMES as _TARGET_NAMES,
)
from miainwoodpecker.devices.shared_frame import (
    SharedFrameReader,
    SharedFrameRef,
    SharedFrameSetRef,
    SharedSpectrumRef,
)

# The one server-side constant the client must agree on beyond the wire
# protocol: which exit status means "retry with different ports".
PORT_UNAVAILABLE_EXIT_STATUS = 4

DEFAULT_SERVER_MODULE = "miainwoodpecker.devices.nion_server"
"""
The device server module this client launches unless told otherwise.

Named rather than hard-coded because of who the *other* vendors are.
Nion's stack is GPL-3.0, and the subprocess boundary exists to keep its
copyleft out of this process. The other vendors' SDKs are proprietary,
which is a different constraint pointing the same way, and it is not
uniform: Thermo Fisher's scripting interface is a COM server installed
with the microscope (wrappable by the BSD-licensed ``temscript``,
redistributable by us), JEOL's PyJEM lives on the TEM control PC and is
not on PyPI, and Zeiss's SmartSEM ActiveX requires an agreement with
Zeiss before anyone may develop against it. Only the last of those
categorically cannot be in-tree — but all of them make an adapter a
site-specific thing that a lab should be able to write, install, and run
without forking this project. So the module is a parameter.

The contract such a package implements is the whole of what
``nion_server.py`` does on its command line: accept ``--backend``, accept
one positional port per name in
:data:`~miainwoodpecker.devices.rpc.TARGET_NAMES`, bind a
``multiprocessing.connection.Listener`` on each with the authkey from
``MIAINWOODPECKER_AUTHKEY``, and serve
:class:`~miainwoodpecker.devices.rpc.Call` objects against targets that
satisfy the protocols in :mod:`miainwoodpecker.devices.interface`. See
docs/vendor-support.md for what that costs per vendor, and for the one
part of it that does *not* yet fit an arbitrary instrument: the target
names are a fixed tuple, so a vendor with a different set of detectors
has to map onto them.
"""

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from multiprocessing.connection import Connection

    from miainwoodpecker.devices.interface import (
        CameraParameters,
        Frame,
        ScanParameters,
        Spectrum,
        SpectrumParameters,
    )

# Re-exported from rpc.py so callers of this client keep importing the
# backend vocabulary from the module whose API they are using, while
# rpc.py stays the one place both peers agree on it.
__all__ = [
    "ACCEPT_TRANSPORT",
    "ATTACH_TRANSPORTS",
    "BACKENDS",
    "CONNECT_TRANSPORT",
    "DEFAULT_SERVER_MODULE",
    "HARDWARE_BACKEND",
    "REPLAY_BACKEND",
    "SERVER_DISCONNECTED",
    "SERVER_EXITED",
    "SERVER_RESPONSIVE",
    "SERVER_UNRESPONSIVE",
    "SIMULATED_BACKEND",
    "AttachInvitation",
    "DeviceServerAttachError",
    "DeviceServerAttachTimeoutError",
    "DeviceServerStartupError",
    "RemoteCamera",
    "RemoteInstrument",
    "RemoteInstrumentDevices",
    "RemoteScanner",
    "RemoteSpectrumDetector",
    "ServerHealth",
    "attached_instrument",
    "remote_instrument",
    "remote_simulated_instrument",
]

# Set by the test suite to arm the device server's test hooks. Read
# here rather than passed as a parameter so no shipped call site can
# reach it: remote_instrument() has no argument that turns them on.
_TEST_HOOKS_ENV_VAR = "MIAINWOODPECKER_ENABLE_TEST_HOOKS"

# How many times to re-pick ports when the server reports one was taken
# between the client's probe and the server's bind. There is no fixed
# watch window paired with this any more: the connect loop already polls
# the process, so the collision is recognised whenever it happens rather
# than only within a stopwatch's grace — and a healthy startup pays
# nothing for the vigilance.
_PORT_RETRY_ATTEMPTS = 3

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


# The conditions a caller must be able to tell apart, because the right
# response differs: retry/continue, start a new session, or investigate a
# server that is running but not working.
SERVER_RESPONSIVE = "responsive"
SERVER_EXITED = "exited"
SERVER_UNRESPONSIVE = "unresponsive"
SERVER_DISCONNECTED = "disconnected"
"""
The connection to an *attached* device server is gone, cause unknown.

A fourth state rather than reusing :data:`SERVER_EXITED`, because the
evidence is different and pretending otherwise would mislead. "Exited"
is a claim about a process, backed by a ``Popen.returncode`` this client
read. An attached bridge runs inside somebody else's application — for
Gatan, inside DigitalMicrograph — so there is no such handle, the host
process is very likely still running with the bridge inside it stopped,
and the machine may not even be this one. All that was observed is a
closed socket, and that is all this state asserts.
"""


class DeviceServerStartupError(RuntimeError):
    """Raised when the device server process died before it served anything."""


class _PortsLostError(DeviceServerStartupError):
    """
    A spawned server exited because a chosen port was already bound.

    Internal, and a subclass of :class:`DeviceServerStartupError` on
    purpose: the condition *is* a startup failure, but it is the one
    startup failure that is not the server's fault and resolves on a
    respawn with fresh ports — ``_free_port()`` probes and releases, so
    anything on the machine can claim a port in the seconds before the
    child binds it. The connect loop raises this instead of the generic
    diagnostic whenever the dead server's exit status is
    :data:`PORT_UNAVAILABLE_EXIT_STATUS`, and the orchestration in
    :func:`remote_instrument` catches it and re-picks ports up to
    :data:`_PORT_RETRY_ATTEMPTS` times. Every other exit status keeps its
    existing message, because retrying a missing instrument or an import
    error would just fail again while hiding the diagnostic that matters.
    """


class DeviceServerAttachError(RuntimeError):
    """
    Raised when a session with an already-running device server cannot begin.

    Separate from :class:`DeviceServerStartupError` because there was no
    startup to fail: nothing was launched, so "the server exited with
    status N before accepting connections" — the diagnosis that class
    exists to deliver — has no meaning here. What went wrong instead is
    that two independently started programs did not meet.
    """


class DeviceServerAttachTimeoutError(DeviceServerAttachError):
    """
    Raised when the device server never appeared within the attach timeout.

    The realistic failure of the inbound path, and the reason it gets its
    own type: with nothing spawned there is no process to watch die and no
    stderr to read, so a client waiting for a bridge that will never come
    looks exactly like a client waiting for one that is merely slow. The
    message carries what *was* seen — including connections that arrived
    and failed authentication, which is the single most likely
    misconfiguration.
    """


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

_UNRECOVERABLE_ATTACHED = (
    "this session cannot be recovered from here - re-attaching is a fresh "
    "session, and whatever device state the bridge held (a started camera, a "
    "spectrometer offset) is whatever it is now, not what this session set; "
    "start a fresh attached_instrument() session once the bridge is serving "
    "again"
)


class _ServerLifecycle:
    """
    What this client knows about the fate of the process serving it.

    Exists because the answer differs by *how the session began*, and the
    difference is not cosmetic. A spawned server is a ``Popen`` this
    process owns: its exit status is readable, "it died" is provable, and
    killing it is both possible and this client's responsibility. An
    attached server is a peer that was already running — a bridge inside
    another vendor's application, possibly on another machine — where none
    of those three hold. Every message that used to say "the device server
    process exited with status 1" had a ``Popen`` behind it; saying the
    same thing about a peer we never launched would be a claim this client
    cannot support, and would send an operator looking for a subprocess
    that does not exist.

    Subclasses answer the same four questions with the evidence each
    actually has.
    """

    def poll_exit(self) -> int | None:
        """
        Return the server's exit status if this client can observe one.

        Returns
        -------
        int | None
            ``Popen.returncode`` semantics for an owned process; always
            ``None`` when the server was not launched here, because
            "still running" and "exited unseen" are indistinguishable
            from this side and ``None`` is the one that claims less.

        Raises
        ------
        NotImplementedError
            Always; a subclass answers this.
        """
        raise NotImplementedError

    def settle(self, timeout_s: float) -> None:
        """
        Wait briefly for a server whose connection just broke to finish dying.

        Parameters
        ----------
        timeout_s : float
            Bounded wait.

        Raises
        ------
        NotImplementedError
            Always; a subclass answers this.
        """
        raise NotImplementedError

    def describe_fate(self, status: int | None) -> str:
        """
        Describe, in one clause, what has become of the server.

        Parameters
        ----------
        status : int | None
            The value :meth:`poll_exit` returned.

        Returns
        -------
        str
            A clause an operator can act on.

        Raises
        ------
        NotImplementedError
            Always; a subclass answers this.
        """
        raise NotImplementedError

    def recovery_note(self) -> str:
        """Return the sentence explaining why this session cannot continue."""
        raise NotImplementedError

    def lost_state(self) -> str:
        """Return the :class:`ServerHealth` state for a server that is gone."""
        raise NotImplementedError

    def identity(self) -> str:
        """Return a short phrase naming which server this is, for messages."""
        raise NotImplementedError


class _SpawnedServer(_ServerLifecycle):
    """
    A device server this client launched and therefore owns.

    The original and still the default: everything the module docstring
    says about teardown, orphan watchdogs and bounded SIGTERM parking is
    about this case.
    """

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process

    def poll_exit(self) -> int | None:
        """
        Return the child's exit status, or ``None`` while it runs.

        Returns
        -------
        int | None
            ``Popen.returncode``.
        """
        return self.process.poll()

    def settle(self, timeout_s: float) -> None:
        """
        Give the kernel a moment to finish reaping the child.

        Parameters
        ----------
        timeout_s : float
            Bounded wait.
        """
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=timeout_s)

    def describe_fate(self, status: int | None) -> str:
        """
        Describe the child's exit status or signal.

        Parameters
        ----------
        status : int | None
            ``Popen.returncode``.

        Returns
        -------
        str
            A clause naming what happened to the process.
        """
        return _exit_description(status)

    def recovery_note(self) -> str:
        """Return the spawn path's unrecoverable-session explanation."""
        return _UNRECOVERABLE

    def lost_state(self) -> str:
        """Return :data:`SERVER_EXITED`: an owned process that is gone has exited."""
        return SERVER_EXITED

    def identity(self) -> str:
        """Return the child's pid."""
        return f"pid {self.process.pid}"


class _AttachedServer(_ServerLifecycle):
    """
    A device server that was already running when this client found it.

    The honest position is that this client knows almost nothing about it.
    There is no ``Popen``, so no exit status and no signal; the peer may be
    a thread inside a much larger application (a bridge inside Gatan's
    DigitalMicrograph is the case this was built for), in which case
    "the process exited" would be *false* even when the device server is
    thoroughly gone. It may also be on a different machine, where a broken
    connection is as likely to be the network as the peer.

    So the only evidence is the socket, and the messages say exactly that
    and point at the one place that does know more: the bridge's own log,
    wherever it runs.
    """

    def __init__(self, origin: str) -> None:
        self.origin = origin

    def poll_exit(self) -> int | None:
        """
        Return ``None`` always: this client cannot observe the peer's exit.

        Returns
        -------
        int | None
            Always ``None``.
        """
        return None

    def settle(self, timeout_s: float) -> None:
        """
        Do nothing: there is no child to reap.

        Parameters
        ----------
        timeout_s : float
            Ignored; accepted so the two lifecycles are interchangeable.
        """

    def describe_fate(self, status: int | None) -> str:
        """
        Describe what a broken connection to an unowned peer does and does not mean.

        Parameters
        ----------
        status : int | None
            Always ``None`` here; accepted for interface symmetry.

        Returns
        -------
        str
            A clause that names the loss without inventing a process fate.
        """
        del status
        return (
            f"the connection to the attached device server ({self.origin}) is "
            f"gone. This client did not launch it, so it has no exit status "
            f"to report and cannot tell a stopped bridge from a stopped host "
            f"application or a broken network - the bridge's own log is the "
            f"place that knows"
        )

    def recovery_note(self) -> str:
        """Return the attach path's unrecoverable-session explanation."""
        return _UNRECOVERABLE_ATTACHED

    def lost_state(self) -> str:
        """Return :data:`SERVER_DISCONNECTED`, which claims only what was seen."""
        return SERVER_DISCONNECTED

    def identity(self) -> str:
        """Return where this client met the bridge."""
        return self.origin


def _lost_server_message(
    target: str,
    method: str,
    error: RemoteConnectionLostError,
    lifecycle: _ServerLifecycle | None,
) -> str:
    """
    Describe a connection-lost failure with the server's fate named.

    :mod:`miainwoodpecker.devices.rpc` cannot say this itself: it knows the
    socket broke, but only this module holds the lifecycle handle that says
    what — if anything — is knowable about the peer.

    Parameters
    ----------
    target : str
        The RPC target the failed call was made on.
    method : str
        The method that was being called.
    error : RemoteConnectionLostError
        The error raised by :func:`~miainwoodpecker.devices.rpc.send_call`,
        used verbatim when there is no lifecycle handle to consult.
    lifecycle : _ServerLifecycle | None
        What is known about the server, or ``None`` if nothing is.

    Returns
    -------
    str
        A message naming the call, the server's fate, and the fact that
        the session is unrecoverable.
    """
    if lifecycle is None:
        return str(error)
    status = lifecycle.poll_exit()
    if status is None:
        # Give the kernel a moment: a server dying mid-call closes its
        # socket just before it finishes exiting, and "connection broke but
        # the process is fine" is a much rarer and stranger claim to make.
        # A no-op for an attached peer, which has no exit to wait for.
        lifecycle.settle(_EXIT_GRACE_S)
        status = lifecycle.poll_exit()
    return (
        f"remote call {target}.{method}() failed: "
        f"{lifecycle.describe_fate(status)}. {lifecycle.recovery_note()}"
    )


def _free_port() -> int:
    """Return a currently-unused localhost port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("localhost", 0))
        return probe.getsockname()[1]


def _connect_with_retry(  # noqa: PLR0913, PLR0917 - one call site each, all named
    port: int,
    authkey: bytes,
    deadline: float,
    process: subprocess.Popen[bytes] | None,
    server_module: str = DEFAULT_SERVER_MODULE,
    host: str = "localhost",
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
    process : subprocess.Popen[bytes] | None
        The server process, watched for early exit — or ``None`` when this
        client did not launch one, which is the attach path. There is then
        no process state to consult, so the deadline is the only bound,
        and refusals are retried until it passes: a bridge inside another
        application is *expected* to be slow to start listening, and the
        one thing that must not happen is giving up on the first refusal.
    server_module : str
        The module that was launched, named in the failure message so an
        out-of-tree adapter's import error is diagnosable.
    host : str
        Host to connect to. Not always ``localhost``: an attached device
        server may run on the instrument's own control computer.

    Returns
    -------
    Connection
        The connected client end, with Nagle disabled.

    Raises
    ------
    _PortsLostError
        If the server exited with
        :data:`PORT_UNAVAILABLE_EXIT_STATUS` — a port this client probed
        as free was bound by something else before the server could take
        it. Distinguishable because it is the one startup failure the
        caller can cure, by re-picking ports and respawning.
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
        if process is not None and process.poll() is not None:
            if process.returncode == PORT_UNAVAILABLE_EXIT_STATUS:
                msg = (
                    f"the device server ({server_module}) exited with "
                    f"status {PORT_UNAVAILABLE_EXIT_STATUS}: a port this "
                    f"client probed as free was claimed by another process "
                    f"before the server could bind it"
                )
                raise _PortsLostError(msg)
            msg = (
                f"the device server ({server_module}) exited with status "
                f"{process.returncode} before accepting connections. Its "
                f"own diagnostic went to stderr, inherited from this "
                f"process. Status 1 with nothing else on stderr usually "
                f"means the module could not be imported — an out-of-tree "
                f"vendor adapter has to be installed in the same "
                f"interpreter as this application. This project's own "
                f"server instead reports, for a hardware backend with no "
                f"instrument present, which nion Registry components it "
                f"looked for and which plug-ins it loaded or skipped."
            )
            raise DeviceServerStartupError(msg)
        try:
            connection = _connect_once(port, authkey, deadline, host=host)
        except (ConnectionRefusedError, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)
        else:
            disable_nagle(connection)
            return connection


def _connect_once(
    port: int,
    authkey: bytes,
    deadline: float,
    host: str = "localhost",
) -> Connection:
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
        Port to connect to.
    authkey : bytes
        Shared secret for the connection handshake.
    deadline : float
        ``time.monotonic()`` value after which to abandon the attempt.
    host : str
        Host to connect to; ``localhost`` for a spawned server, which by
        construction runs here.

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
            outcome.append(Client((host, port), authkey=authkey))
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
        lifecycle: _ServerLifecycle | None = None,
        pickle_protocol: int | None = None,
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
        self._lifecycle = lifecycle
        self._pickle_protocol = pickle_protocol

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
                pickle_protocol=self._pickle_protocol,
            )
        except RemoteConnectionLostError as error:
            raise RemoteConnectionLostError(
                _lost_server_message(self._target, method, error, self._lifecycle),
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

    def _frame_set(self, method: str, *args: object) -> list[Frame]:
        """
        Make a call that returns several frames, following a stacked reference.

        The multi-frame sibling of :meth:`_frame`, holding the same lock
        for the same reason: the call and the copy-out are one critical
        section, or a second thread's request overwrites the segment this
        one is still reading. The frames of one pass arrive as **one**
        stacked block (see
        :class:`~miainwoodpecker.devices.shared_frame.SharedFrameSetRef`)
        when they clear the shared-memory threshold together, and as a
        plain pickled sequence when they do not.

        Parameters
        ----------
        method : str
            Method name on the remote target.
        *args : object
            Positional arguments.

        Returns
        -------
        list[Frame]
            The frames, in the order the server produced them, each with
            its own private array.
        """
        with self._frame_lock:
            result = self._call(method, *args)
            if isinstance(result, SharedFrameSetRef):
                return self._reader.read_frames(result)
            return list(typing.cast("Sequence[Frame]", result))

    def _spectrum(self, method: str, *args: object) -> Spectrum:
        """
        Make a call that returns a spectrum, following a shared-memory reference.

        The spectrum-side twin of :meth:`_frame`, holding the same lock
        for the same reason: the call and the copy-out are one critical
        section, or a second thread's request overwrites the segment
        this one is still reading. A spot spectrum usually arrives
        whole (it is under the threshold), and a spectrum image
        usually does not.
        """
        with self._frame_lock:
            result = self._call(method, *args)
            if isinstance(result, SharedSpectrumRef):
                return self._reader.read_spectrum(result)
            return typing.cast("Spectrum", result)

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

    def scan_frames(
        self,
        parameters: ScanParameters,
        channels: Sequence[int],
    ) -> list[Frame]:
        """
        Scan once on the remote device and return one frame per channel.

        One RPC call, because the request is one acquisition: the server
        runs a single pass with every requested channel enabled and
        returns the frames together, stamped with the shared
        ``scan_pass_id`` the device-side adapter minted (see
        :meth:`~miainwoodpecker.devices.interface.Scanner.scan_frames`
        for the contract). Splitting this into per-channel calls would
        reintroduce exactly the k-passes-for-k-channels behaviour the
        call exists to remove.

        Parameters
        ----------
        parameters : ScanParameters
            Scan geometry and timing, shared by every returned frame.
        channels : Sequence[int]
            Detector channel indices to read out during the pass.

        Returns
        -------
        list[Frame]
            One frame per requested channel, in request order, all from
            the same pass.
        """
        return self._frame_set("scan_frames", parameters, tuple(channels))


class RemoteSpectrumDetector(_RemoteDevice):
    """
    A ``SpectrumDetector`` implementation that delegates over IPC to a server.

    Thin, like its two siblings, and for the same reason: transport is
    not the adapter's problem. Worth measuring one thing that is *not*
    thin, though, because it is the question a simultaneous EDX, EELS and
    imaging workflow asks of this transport.

    **Concurrency composes; correlation does not.** This handle owns its
    own connection, and the server gives every connection its own handler
    thread (``serving.accept_loop``), so a caller genuinely may drive
    this detector from one thread while another drives the scanner —
    neither call queues behind the other, and the detector really can be
    integrating while the scan runs. That much of the brief's question
    has a positive answer, and it is worth stating because the protocol
    being "strictly synchronous" makes it sound as though it does not.

    What the transport gives no part of is a shared trigger, a shared
    clock, or an identifier tying two results to one pass of the probe.
    So what two overlapping calls produce is two acquisitions that
    overlapped in *wall-clock time*, which is not the same claim as
    sharing probe positions, and on a scanned instrument sharing probe
    positions is the whole point. Adding a trigger here would not fix it
    either: the correlation is established in hardware at the detector,
    and what is missing above this layer is a unit of acquisition that
    represents one pass with several outputs. See
    :meth:`~miainwoodpecker.devices.interface.SpectrumDetector.acquire_map`
    and docs/adapters/spectrum-detectors.md.
    """

    @property
    def detector_id(self) -> str:
        """Return the remote device's detector id."""
        return typing.cast("str", self._call("detector_id"))

    @property
    def acquisition_modes(self) -> Sequence[str]:
        """Return the acquisition modes the remote device supports."""
        return typing.cast("Sequence[str]", self._call("acquisition_modes"))

    def parameters(self) -> SpectrumParameters:
        """Return the settings the remote device's next spectrum will use."""
        return typing.cast("SpectrumParameters", self._call("parameters"))

    def configure(self, parameters: SpectrumParameters) -> SpectrumParameters:
        """
        Apply settings to the remote device and return what it accepted.

        Parameters
        ----------
        parameters : SpectrumParameters
            The requested live time, channel count, and energy calibration.

        Returns
        -------
        SpectrumParameters
            What the device took, which is not necessarily what was asked.
        """
        return typing.cast("SpectrumParameters", self._call("configure", parameters))

    def start(self) -> None:
        """Begin acquisition on the remote device."""
        self._call("start")

    def stop(self) -> None:
        """Pause acquisition on the remote device."""
        self._call("stop")

    def acquire_spectrum(self) -> Spectrum:
        """Return one spot spectrum from the remote device."""
        return self._spectrum("acquire_spectrum")

    def acquire_map(self, parameters: ScanParameters) -> Spectrum:
        """
        Return a spectrum image from the remote device.

        Parameters
        ----------
        parameters : ScanParameters
            The scan geometry to map over.

        Returns
        -------
        Spectrum
            One spectrum per probe position, energy on the last axis.
        """
        return self._spectrum("acquire_map", parameters)


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
        lifecycle: _ServerLifecycle | None = None,
        pickle_protocol: int | None = None,
    ) -> None:
        self._connection = connection
        self._target = target
        self._lock = threading.Lock()
        self._lifecycle = lifecycle
        self._pickle_protocol = pickle_protocol
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
                pickle_protocol=self._pickle_protocol,
            )
        except RemoteConnectionLostError as error:
            raise RemoteConnectionLostError(
                _lost_server_message(self._target, method, error, self._lifecycle),
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
                pickle_protocol=self._pickle_protocol,
            )
        except RemoteCallTimeoutError:
            self._health_poisoned = True
            identity = (
                self._lifecycle.identity()
                if self._lifecycle is not None
                else "identity unknown"
            )
            note = (
                self._lifecycle.recovery_note()
                if self._lifecycle is not None
                else _UNRECOVERABLE
            )
            return ServerHealth(
                state=SERVER_UNRESPONSIVE,
                detail=(
                    f"the device server did not answer a health check within "
                    f"{timeout_s}s while its connection was still open "
                    f"({identity}). Because a health check touches no device, "
                    f"this means wedged rather than busy. {note}"
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

        Only the first is unavailable on the attach path, and deliberately:
        an attached bridge has no observable exit, so
        :meth:`_ServerLifecycle.poll_exit` answers ``None`` there and the
        server is asked rather than pronounced upon.

        Returns
        -------
        ServerHealth | None
            The verdict, or ``None`` if the server must actually be asked.
        """
        status = self._lifecycle.poll_exit() if self._lifecycle is not None else None
        if status is not None:
            assert self._lifecycle is not None  # noqa: S101 - status came from it
            return ServerHealth(
                state=self._lifecycle.lost_state(),
                detail=(
                    f"{self._lifecycle.describe_fate(status)}. "
                    f"{self._lifecycle.recovery_note()}"
                ),
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
            For a spawned server, :data:`SERVER_EXITED` if the process has
            (or shortly does) exit, which is the overwhelmingly likely
            cause; otherwise :data:`SERVER_UNRESPONSIVE`, since a live
            server whose socket broke cannot answer either. For an attached
            one, :data:`SERVER_DISCONNECTED` — a broken socket is the whole
            of the evidence available, and it is not evidence of an exit.
        """
        self._health_poisoned = True
        if self._lifecycle is None:
            return ServerHealth(state=SERVER_UNRESPONSIVE, detail=str(error))
        self._lifecycle.settle(_EXIT_GRACE_S)
        status = self._lifecycle.poll_exit()
        state = (
            self._lifecycle.lost_state()
            if status is not None or not isinstance(self._lifecycle, _SpawnedServer)
            else SERVER_UNRESPONSIVE
        )
        return ServerHealth(
            state=state,
            detail=(
                f"{self._lifecycle.describe_fate(status)}. "
                f"{self._lifecycle.recovery_note()}"
            ),
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
    unchanged), with one difference that is not an oversight —
    :attr:`scanner` is optional here and not there. The Nion server
    always has a scan unit, so its own type says so; this client has to
    tolerate what *any* server serves, including a detector-only one.

    Attributes
    ----------
    ronchigram_camera : RemoteCamera | None
        The Ronchigram camera, if this instrument has one.
    eels_camera : RemoteCamera | None
        The EELS camera, if this instrument has one.
    camera : RemoteCamera | None
        A camera that is neither of the above — a direct detector, a
        commodity USB microscope, a camera body. ``None`` unless the
        server serves the neutral ``camera`` target.
    scanner : RemoteScanner | None
        The scan device (HAADF/MAADF channels on the simulator), if this
        instrument has one. ``None`` for a detector-only server — a
        Direct Electron, DECTRIS, or Hamamatsu camera driven directly,
        with no scan unit of its own. Optional for the same reason the
        cameras are: what a server serves is what ``describe()`` says,
        not what the simulator happens to have.
    instrument : RemoteInstrument
        Stage/defocus/blanker controls.
    stage_size_nm : float
        The stage extent, useful for choosing a sensible
        ``ScanParameters.fov_nm``.
    spectrum_detector : RemoteSpectrumDetector | None
        A detector that produces energy spectra rather than frames — an
        EDX silicon drift detector is the case in hand. ``None`` unless
        the server serves the ``spectrum_detector`` target.

        Last, and with a default, deliberately: this dataclass is frozen
        and its fields are positional, so anything constructing one by
        position — a test, a script, an out-of-tree tool — keeps working
        untouched. An EELS spectrometer is **not** here; it disperses
        onto a camera and is served as one.
    """

    ronchigram_camera: RemoteCamera | None
    eels_camera: RemoteCamera | None
    camera: RemoteCamera | None
    scanner: RemoteScanner | None
    instrument: RemoteInstrument
    stage_size_nm: float
    spectrum_detector: RemoteSpectrumDetector | None = None

    def spectrum_detectors(self) -> dict[str, RemoteSpectrumDetector]:
        """
        Return every spectrum detector this instrument serves, by target name.

        The counterpart to :meth:`cameras`, and there for the same
        reason: an instrument with both an EDX and a WDS spectrometer is
        ordinary, and a caller should be able to ask what is there
        rather than about named slots.

        Returns
        -------
        dict[str, RemoteSpectrumDetector]
            Target name to detector, omitting those this server lacks.
        """
        return {
            name: detector
            for name, detector in zip(
                _SPECTRUM_TARGET_NAMES,
                (self.spectrum_detector,),
                strict=True,
            )
            if detector is not None
        }

    def cameras(self) -> dict[str, RemoteCamera]:
        """
        Return every camera this instrument serves, by target name.

        The named attributes stay, because the viewer and the scripts are
        written against them. This is what a detector-only caller wants
        instead: whatever is there, without asking about two Nion-shaped
        slots by name.

        Returns
        -------
        dict[str, RemoteCamera]
            Target name to camera, omitting those this server lacks.
        """
        return {
            name: camera
            for name, camera in zip(
                _CAMERA_TARGET_NAMES,
                (self.ronchigram_camera, self.eels_camera, self.camera),
                strict=True,
            )
            if camera is not None
        }


# Historical name, kept because the migration plan and README refer to it.
RemoteSimulatedInstrument = RemoteInstrumentDevices


def _start_server(
    backend: str,
    plugin_names: Sequence[str],
    server_module: str = DEFAULT_SERVER_MODULE,
) -> tuple[dict[str, int], bytes, subprocess.Popen[bytes]]:
    """
    Pick ports and spawn a device server, without waiting on it at all.

    :func:`_free_port` picks ports by binding to port 0 and *releasing*
    the socket, so the port is only reserved by convention until the
    child binds it seconds later — after the subprocess has started and
    imported the whole Nion stack. Anything else on the machine can claim
    it in that window, and the more sessions start at once the likelier
    that is: a parallel test run is the realistic case, and the failure
    it produced was an anonymous traceback and a dead server.

    This function used to watch the fresh child for the port-collision
    exit inside a fixed 0.4 s grace window, which had both costs of a
    stopwatch: every *healthy* startup paid the full window (a healthy
    server never exits, so the wait always timed out), and a loaded
    machine that took longer than the window to reach its bind turned a
    curable collision into an anonymous startup error — observed once in
    CI after the target list grew to five ports. Detection now lives
    where the process is already being polled anyway:
    :func:`_connect_with_retry` raises :class:`_PortsLostError` when it
    finds the child dead with
    :data:`PORT_UNAVAILABLE_EXIT_STATUS`, and :func:`_spawn_and_connect`
    catches it and calls back here for fresh ports and a fresh child, up
    to :data:`_PORT_RETRY_ATTEMPTS` spawns.
    A collision is therefore recognised *whenever* it happens before the
    session is connected, and a healthy startup no longer waits at all.

    Retrying with fresh ports is the fix that fits the existing design.
    The alternative — binding in the parent and passing inherited fds —
    removes the window entirely but changes how the server is launched,
    which is a bigger change than the problem warrants for a race that
    resolves on the next attempt.

    Parameters
    ----------
    backend : str
        ``"simulated"`` or ``"hardware"``.
    plugin_names : Sequence[str]
        ``nionswift_plugin`` modules for the hardware backend.
    server_module : str
        Module to run with ``python -m``; see
        :data:`DEFAULT_SERVER_MODULE`.

    Returns
    -------
    tuple[dict[str, int], bytes, subprocess.Popen[bytes]]
        The chosen ports, the shared authkey, and the running process.
    """
    ports = {name: _free_port() for name in _TARGET_NAMES}
    authkey = secrets.token_bytes(32)
    process = _spawn_server(ports, authkey, backend, plugin_names, server_module)
    return ports, authkey, process


def _spawn_server(
    ports: typing.Mapping[str, int],
    authkey: bytes,
    backend: str,
    plugin_names: Sequence[str],
    server_module: str = DEFAULT_SERVER_MODULE,
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
    server_module : str
        Module to run with ``python -m``; see
        :data:`DEFAULT_SERVER_MODULE`.

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
            server_module,
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


@dataclass(frozen=True)
class _ConnectedSession:
    """
    One spawned device server, fully connected and wrapped.

    The unit :func:`_spawn_and_connect` retries: everything here belongs
    to a single child process, so a port collision discards the whole
    value and a fresh spawn builds a fresh one — including fresh
    connections and a fresh connect deadline. ``connections`` is kept
    alongside the wrapped devices because teardown closes the raw
    connections itself, exactly as it always has.
    """

    process: subprocess.Popen[bytes]
    connections: dict[str, Connection]
    instrument: RemoteInstrument
    description: dict[str, object]
    cameras: dict[str, RemoteCamera]
    scanner: RemoteScanner | None
    spectrum_detectors: dict[str, RemoteSpectrumDetector]
    devices: tuple[_RemoteDevice, ...]


def _connect_session(
    ports: typing.Mapping[str, int],
    authkey: bytes,
    process: subprocess.Popen[bytes],
    server_module: str,
    connections: dict[str, Connection],
) -> _ConnectedSession:
    """
    Connect to a freshly spawned server and wrap everything it serves.

    The connect deadline is computed here rather than by the caller, so a
    respawn after a port collision starts with the full budget — the
    health connection and the per-target connects share one deadline
    within an attempt, and sharing it *across* attempts would make the
    retry a shorter and shorter straw.

    Parameters
    ----------
    ports : typing.Mapping[str, int]
        Port each target's Listener was told to bind.
    authkey : bytes
        Shared secret for every connection.
    process : subprocess.Popen[bytes]
        The freshly spawned server, watched for early exit on every
        connect attempt.
    server_module : str
        The module that was launched, named in failure diagnostics.
    connections : dict[str, Connection]
        Filled in place as connections are made, so the caller can close
        whatever this attempt opened even when it fails partway.

    Returns
    -------
    _ConnectedSession
        The connected session, ready to yield to the caller.

    Every connect here can raise :class:`_PortsLostError`, propagated
    from :func:`_connect_with_retry` at whatever point the child's
    port-collision exit becomes visible — before the first connection or
    between the tenth and the eleventh. Nothing is caught here: the
    caller owns the respawn, and owns ``connections`` so it can release
    whatever this attempt opened to a server that is already gone.
    """
    deadline = time.monotonic() + _CONNECT_TIMEOUT_S
    lifecycle = _SpawnedServer(process)
    connections["instrument"] = _connect_with_retry(
        ports["instrument"], authkey, deadline, process, server_module,
    )
    # A second connection to the same target, reserved for health
    # checks: see RemoteInstrument for why sharing the control
    # connection would make the check both slower and riskier. The
    # server accepts any number of connections per target, one handler
    # thread each, so this costs one socket and one idle thread.
    connections["instrument:health"] = _connect_with_retry(
        ports["instrument"], authkey, deadline, process, server_module,
    )
    instrument = RemoteInstrument(
        connections["instrument"],
        health_connection=connections["instrument:health"],
        lifecycle=lifecycle,
    )
    description = instrument.describe()
    served = typing.cast("Sequence[str]", description["targets"])
    for name in served:
        connections[name] = _connect_with_retry(
            ports[name], authkey, deadline, process, server_module,
        )

    cameras = {
        name: RemoteCamera(connections[name], name, lifecycle)
        for name in _CAMERA_TARGET_NAMES
        if name in connections
    }
    # Optional for the same reason the cameras are. A detector-only
    # server - a camera driven directly, with no scan unit - used to
    # die here with a KeyError, which made "vendor-neutral" quietly
    # mean "must have a scanner shaped like Nion's".
    scanner = (
        RemoteScanner(connections["scanner"], "scanner", lifecycle)
        if "scanner" in connections
        else None
    )
    # Optional in exactly the way the cameras and the scanner are:
    # what a server serves is what describe() says. Most instruments
    # have no X-ray detector, and the ones that do may have it on a
    # separate analyser entirely.
    spectrum_detectors = {
        name: RemoteSpectrumDetector(connections[name], name, lifecycle)
        for name in _SPECTRUM_TARGET_NAMES
        if name in connections
    }
    return _ConnectedSession(
        process=process,
        connections=connections,
        instrument=instrument,
        description=description,
        cameras=cameras,
        scanner=scanner,
        spectrum_detectors=spectrum_detectors,
        devices=(
            *cameras.values(),
            *([scanner] if scanner is not None else []),
            *spectrum_detectors.values(),
        ),
    )


def _spawn_and_connect(
    backend: str,
    plugin_names: Sequence[str],
    server_module: str,
) -> _ConnectedSession:
    """
    Spawn a device server and connect to it, respawning on a port collision.

    The retry spans the *whole* spawn-and-connect rather than a fixed
    watch window after the spawn: a collision is only provable by the
    server exiting with :data:`PORT_UNAVAILABLE_EXIT_STATUS`, and on a
    loaded machine that exit can come after any fixed stopwatch has
    given up. :func:`_connect_with_retry` polls the process on every
    attempt anyway, so the moment the exit is visible — before the first
    connection or between the tenth and the eleventh — it surfaces as
    :class:`_PortsLostError`, this loop closes whatever connections were
    already made to the dead server, and a fresh spawn gets fresh ports
    and a fresh deadline. Healthy startups never wait on any of this.

    Parameters
    ----------
    backend : str
        ``"simulated"`` or ``"hardware"``.
    plugin_names : Sequence[str]
        ``nionswift_plugin`` modules for the hardware backend.
    server_module : str
        Module to run with ``python -m``; see
        :data:`DEFAULT_SERVER_MODULE`.

    Returns
    -------
    _ConnectedSession
        The connected session for the one spawn that succeeded.

    Every startup failure that is *not* a collision propagates unchanged,
    keeping the diagnostic :func:`_connect_with_retry` composed for it —
    a missing instrument or an unimportable adapter module would fail the
    same way on the next attempt, so retrying would only hide the one
    message worth reading.

    Raises
    ------
    DeviceServerStartupError
        If every attempt lost its ports to something else on the machine.
    """
    for _attempt in range(_PORT_RETRY_ATTEMPTS):
        ports, authkey, process = _start_server(
            backend, plugin_names, server_module,
        )
        connections: dict[str, Connection] = {}
        session: _ConnectedSession | None = None
        try:
            session = _connect_session(
                ports, authkey, process, server_module, connections,
            )
        except _PortsLostError:
            # Discard this attempt and take fresh ports on the next pass.
            # The cleanup below is in a finally rather than here because
            # a failure that is *not* a collision has to leave the same
            # nothing behind on its way out.
            pass
        finally:
            if session is None:
                _release_spawned_server(process, connections)
        if session is not None:
            return session
    msg = (
        f"the device server could not bind its ports on "
        f"{_PORT_RETRY_ATTEMPTS} attempts; something on this machine is "
        f"claiming localhost ports faster than they can be used"
    )
    raise DeviceServerStartupError(msg)


def _release_spawned_server(
    process: subprocess.Popen[bytes],
    connections: typing.Mapping[str, Connection],
) -> None:
    """
    Close this client's end of a spawned session and stop the child.

    The last thing a session does, and also what a spawn attempt does
    when it will not become the session. Both need exactly this: release
    every connection the client opened, then stop the child if it is
    still running.

    "If it is still running" is doing real work rather than being
    defensive. After a port collision the child has already exited to
    report it, so there is nothing to terminate and only the connections
    made before that exit became visible need releasing — the same
    branch that covers a server which shut down through the handshake.

    Parameters
    ----------
    process : subprocess.Popen[bytes]
        The spawned device server.
    connections : typing.Mapping[str, Connection]
        Every connection this client opened to it, by target name.
    """
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
    server_module: str = DEFAULT_SERVER_MODULE,
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
    server_module : str
        The device server to launch with ``python -m``. Defaults to this
        project's Nion server; name another to drive a different vendor's
        instrument from an out-of-tree adapter, which is how a
        proprietary SDK that cannot be redistributed here is reached. See
        :data:`DEFAULT_SERVER_MODULE`.

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
    # The spawn and every connection to the child are one retryable unit:
    # a port collision discovered at any point before the session is
    # fully connected is answered with fresh ports and a fresh child
    # rather than surfacing as a startup failure. See _spawn_and_connect.
    session = _spawn_and_connect(backend, plugin_names, server_module)
    try:
        try:
            yield RemoteInstrumentDevices(
                ronchigram_camera=session.cameras.get("ronchigram_camera"),
                eels_camera=session.cameras.get("eels_camera"),
                camera=session.cameras.get("camera"),
                scanner=session.scanner,
                instrument=session.instrument,
                stage_size_nm=float(
                    typing.cast("float", session.description["stage_size_nm"]),
                ),
                spectrum_detector=session.spectrum_detectors.get(
                    "spectrum_detector",
                ),
            )
        finally:
            _shut_down_server(
                session.instrument, session.devices, session.process,
            )
    finally:
        _release_spawned_server(session.process, session.connections)


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


# ---------------------------------------------------------------------------
# Attaching to a device server this client did not launch.
#
# Everything above spawns. That covers every adapter that can be started
# as `python -m something`, which is most of them - but not all, and the
# exception is structural rather than awkward. An adapter whose SDK only
# exists *inside* another running application cannot be a subprocess of
# ours at any price: Gatan's own documentation states that DigitalMicrograph
# functions cannot be executed from Python outside DigitalMicrograph, so a
# Gatan adapter is a bridge living inside GMS, started by the operator, on
# the application's own schedule. The same shape appears whenever the
# device server belongs to somebody else's process or somebody else's
# machine.
#
# What actually inverts is **process ownership**, and only that. Which end
# opens the socket is a separate, free choice, and this module supports
# both because the right answer is a site's firewall policy rather than an
# architectural principle:
#
#   ACCEPT_TRANSPORT   this client listens; the bridge dials in. Needed when
#                      the microscope PC permits outbound connections only,
#                      which is a common instrument-network rule.
#   CONNECT_TRANSPORT  the bridge listens; this client dials in. The better
#                      default when the network allows it, because the host
#                      application (GMS) is started once in the morning and
#                      outlives many runs of this one - a listening bridge is
#                      simply there whenever a session wants it, with no
#                      re-dialling and no ordering constraint. It is also the
#                      long-standing arrangement in this field: SerialEM's
#                      SerialEMCCD plug-in listens inside DigitalMicrograph
#                      and SerialEM connects to it.
#
# Both keep the whole of the existing protocol: the same Call/Result
# objects, the same authkey handshake, the same Camera/Scanner/
# InstrumentController protocols, the same RemoteInstrumentDevices. A
# device server author writes exactly what an out-of-tree spawned adapter
# writes; only who dials changes.
#
# Two things genuinely differ, and both are consequences rather than
# choices. Liveness is weaker - see _AttachedServer - because there is no
# Popen to poll. And shared memory is off the table: an attached server may
# be on another machine, so frames arrive as ordinary pickles. That is a
# real cost above ~1 MB (scripts/ipc_overhead_benchmark.py measures +1.5 ms
# at ~1 MB rising to +168 ms at 33 MB) and it is the price of a link that
# crosses a process this client does not own.
# ---------------------------------------------------------------------------

ACCEPT_TRANSPORT = "accept"
"""This client binds the listeners; the device server dials in to them."""

CONNECT_TRANSPORT = "connect"
"""The device server binds the listeners; this client dials in to them."""

ATTACH_TRANSPORTS = (ACCEPT_TRANSPORT, CONNECT_TRANSPORT)
"""Every transport direction :func:`attached_instrument` accepts."""

# How long to wait for a device server that was never launched here to
# show up. Much longer than the spawn path's 15s connect deadline, and for
# a reason that is not timidity: there is no process to watch die, so this
# bound is the *only* thing between a mistyped port and a hung
# application - but the thing being waited for is a human starting a script
# inside another application, which 15 seconds does not allow for.
_ATTACH_TIMEOUT_S = 120.0

_ATTACH_CONFIG_VERSION = 1


@dataclass(frozen=True)
class AttachInvitation:
    """
    Everything two independently-started programs need in order to meet.

    The spawn path derives ports and an authkey and hands them to a child
    on its command line, where nobody ever sees them. Nothing can do that
    here: the device server is started by an operator, inside another
    application, possibly on another computer. So the same three facts have
    to be **published** instead — which is what this type is, and why it
    knows how to write itself to a file and how to say itself in a sentence
    an operator can act on.

    Deliberately a value object with an explicit serialisation rather than
    "just pass some arguments". The authkey is a shared secret; a file
    written ``0600`` that the operator copies once is a defensible way to
    move one, and an ad-hoc convention re-invented per site is not.

    Attributes
    ----------
    host : str
        Where the listeners are. For :data:`ACCEPT_TRANSPORT` this is the
        interface *this* client binds — ``localhost`` keeps the link on one
        machine, which is what a bridge in the same PC's GMS wants. For
        :data:`CONNECT_TRANSPORT` it is the machine the bridge is on.
    ports : dict[str, int]
        One port per name in
        :data:`~miainwoodpecker.devices.rpc.TARGET_NAMES`. All of them are
        published even though a given server serves only some, because
        which ones it serves is *its* answer (``describe()``), not ours to
        pre-empt.
    authkey : bytes
        The shared secret both ends pass to
        ``multiprocessing.connection``. Note the handshake is
        cross-version safe by construction: CPython's
        ``_create_response`` keeps the legacy HMAC-MD5 path for a
        challenge with no ``{digest}`` prefix, so a bridge on an older
        embedded interpreter authenticates against a modern client.
    transport : str
        :data:`ACCEPT_TRANSPORT` or :data:`CONNECT_TRANSPORT`.
    """

    host: str
    ports: dict[str, int]
    authkey: bytes
    transport: str = ACCEPT_TRANSPORT

    def origin(self) -> str:
        """
        Return a short phrase naming where this client met the device server.

        Returns
        -------
        str
            Used in every liveness and connection-lost message, so an
            operator reading one knows which end to go and look at.
        """
        direction = (
            "dialled in to this client"
            if self.transport == ACCEPT_TRANSPORT
            else "listening for this client"
        )
        return (
            f"{direction} at {self.host}:{self.ports.get('instrument', '?')}"
        )

    def as_config(self) -> dict[str, object]:
        """
        Return the invitation as plain JSON-compatible data.

        Returns
        -------
        dict[str, object]
            Versioned, so a future field can be added without a bridge
            that predates it silently misreading the file.
        """
        return {
            "version": _ATTACH_CONFIG_VERSION,
            "transport": self.transport,
            "host": self.host,
            "authkey": self.authkey.hex(),
            "ports": dict(self.ports),
        }

    @classmethod
    def from_config(cls, config: typing.Mapping[str, object]) -> AttachInvitation:
        """
        Rebuild an invitation from the data :meth:`as_config` produced.

        Parameters
        ----------
        config : typing.Mapping[str, object]
            Parsed configuration.

        Returns
        -------
        AttachInvitation
            The invitation.

        Raises
        ------
        ValueError
            If the version is not one this code understands, or a required
            field is missing. Both are worth failing on rather than
            guessing: the failure mode of guessing is a bridge that
            authenticates against nothing and an operator watching a
            timeout with no idea why.
        """
        version = config.get("version")
        if version != _ATTACH_CONFIG_VERSION:
            msg = (
                f"attach configuration version {version!r} is not "
                f"{_ATTACH_CONFIG_VERSION}; this file was written by a "
                f"different version of miainwoodpecker"
            )
            raise ValueError(msg)
        try:
            ports = {
                str(name): int(port)
                for name, port in typing.cast(
                    "typing.Mapping[str, object]", config["ports"],
                ).items()
            }
            return cls(
                host=str(config["host"]),
                ports=ports,
                authkey=bytes.fromhex(str(config["authkey"])),
                transport=str(config["transport"]),
            )
        except (KeyError, TypeError) as error:
            msg = f"attach configuration is missing or malformed: {error}"
            raise ValueError(msg) from error

    def write_to(self, path: str | os.PathLike[str]) -> pathlib.Path:
        """
        Write the invitation to a file, readable only by this user.

        Parameters
        ----------
        path : str | os.PathLike[str]
            Where to write it.

        Returns
        -------
        pathlib.Path
            The path written.
        """
        destination = pathlib.Path(path)
        destination.write_text(
            json.dumps(self.as_config(), indent=2) + "\n",
            encoding="utf-8",
        )
        # The authkey is in there. 0600 is not security theatre on a
        # shared instrument-control PC, which is exactly where this file
        # gets written.
        with contextlib.suppress(OSError):  # pragma: no cover - Windows ACLs
            destination.chmod(0o600)
        return destination

    @classmethod
    def read_from(cls, path: str | os.PathLike[str]) -> AttachInvitation:
        """
        Read an invitation a client (or an operator) previously published.

        Parameters
        ----------
        path : str | os.PathLike[str]
            The configuration file.

        Returns
        -------
        AttachInvitation
            The invitation.
        """
        return cls.from_config(
            json.loads(pathlib.Path(path).read_text(encoding="utf-8")),
        )

    def operator_instructions(
        self,
        config_path: str | os.PathLike[str] | None = None,
    ) -> str:
        """
        Return the lines to show an operator who must start the other end.

        Parameters
        ----------
        config_path : str | os.PathLike[str] | None
            Where the invitation was written, if it was.

        Returns
        -------
        str
            A short block naming the ports, the direction, and how the
            bridge is told about them. Printed rather than logged by
            default because it is an instruction to a person standing at a
            microscope, not a diagnostic.
        """
        where = (
            f"  configuration file: {config_path}\n"
            if config_path is not None
            else "  configuration file: not written (pass publish_to=...)\n"
        )
        direction = (
            "this client is LISTENING; start the bridge and have it connect here"
            if self.transport == ACCEPT_TRANSPORT
            else "this client will DIAL the bridge; start the bridge first"
        )
        ports = "\n".join(
            f"    {name}: {port}" for name, port in sorted(self.ports.items())
        )
        return (
            f"miainwoodpecker: waiting for an attached device server\n"
            f"  {direction}\n"
            f"  host: {self.host}\n"
            f"  ports:\n{ports}\n"
            f"{where}"
            f"  authkey: {len(self.authkey)} bytes, in the configuration file "
            f"only - do not paste it into a shared script\n"
        )


class _Opener:
    """One end of the attach handshake, whichever way the socket points."""

    def __enter__(self) -> typing.Self:
        """
        Prepare to open connections.

        Returns
        -------
        typing.Self
            This opener.
        """
        return self

    def __exit__(self, *exception: object) -> None:
        """
        Release anything bound for the handshake.

        Parameters
        ----------
        *exception : object
            The exception triple, unused.
        """

    def open(self, name: str, deadline: float) -> Connection:
        """
        Return the next connection for a target.

        Parameters
        ----------
        name : str
            Target name.
        deadline : float
            ``time.monotonic()`` value after which to give up.

        Returns
        -------
        Connection
            The established connection.

        Raises
        ------
        NotImplementedError
            Always; a subclass answers this.
        """
        raise NotImplementedError


class _ConnectOpener(_Opener):
    """
    Dial a device server that is already listening.

    Almost the spawn path with the process taken away, which is the point:
    the only thing a spawned server did that a listening bridge does not is
    exit visibly, and :func:`_connect_with_retry` already takes ``None``
    for that.
    """

    def __init__(self, invitation: AttachInvitation) -> None:
        self._invitation = invitation

    def open(self, name: str, deadline: float) -> Connection:
        """
        Connect to one target's listener, retrying until the deadline.

        Parameters
        ----------
        name : str
            Target name.
        deadline : float
            ``time.monotonic()`` value after which to give up.

        Returns
        -------
        Connection
            The connected client end.

        Raises
        ------
        DeviceServerAttachTimeoutError
            If nothing was listening by the deadline.
        """
        try:
            return _connect_with_retry(
                self._invitation.ports[name],
                self._invitation.authkey,
                deadline,
                None,
                host=self._invitation.host,
            )
        except (OSError, DeviceServerStartupError) as error:
            msg = (
                f"no device server was listening for target {name!r} at "
                f"{self._invitation.host}:{self._invitation.ports[name]} before "
                f"the attach deadline passed ({error}). Nothing was launched "
                f"from here, so there is no process to inspect: check that the "
                f"bridge is running inside its host application, that it is "
                f"serving this port, and that the two ends share an authkey."
            )
            raise DeviceServerAttachTimeoutError(msg) from error


class _AcceptOpener(_Opener):
    """
    Listen for a device server that dials in.

    Binds one listener per target name — every name, not only the ones the
    server turns out to serve, because what it serves is its own answer and
    the client cannot know it before ``describe()``. Each listener runs its
    own accept thread, so a bridge may dial its connections in any order
    across targets; the only ordering the protocol asks for is that the two
    ``instrument`` connections arrive control-first, health-second, which
    mirrors exactly the order the spawn path opens them in.

    A failed handshake is **recorded, not fatal**. A bridge configured with
    the wrong authkey would otherwise present as a plain timeout, which is
    the least informative possible rendering of the most likely mistake;
    the arrivals that failed authentication are counted and named in the
    timeout message instead.
    """

    def __init__(self, invitation: AttachInvitation) -> None:
        self._invitation = invitation
        self._listeners: dict[str, typing.Any] = {}
        self._arrivals: dict[str, queue.Queue[object]] = {}
        self.rejected = 0

    def __enter__(self) -> typing.Self:
        """
        Bind a listener for every target name and start accepting.

        Returns
        -------
        typing.Self
            This opener.

        Raises
        ------
        DeviceServerAttachError
            If a published port could not be bound, which for a
            caller-supplied port list is the realistic mistake.
        """
        from multiprocessing.connection import Listener  # noqa: PLC0415 - see serve()

        try:
            for name in _TARGET_NAMES:
                self._listeners[name] = Listener(
                    (self._invitation.host, self._invitation.ports[name]),
                    authkey=self._invitation.authkey,
                )
        except OSError as error:
            self.__exit__()
            msg = (
                f"could not bind the ports published for an attached device "
                f"server ({error}). With ports chosen automatically this is a "
                f"race worth retrying; with ports given explicitly it usually "
                f"means something else on this machine already holds one."
            )
            raise DeviceServerAttachError(msg) from error
        for name, listener in self._listeners.items():
            self._arrivals[name] = queue.Queue()
            threading.Thread(
                target=self._accept_forever,
                args=(name, listener),
                name=f"attach-accept-{name}",
                daemon=True,
            ).start()
        return self

    def __exit__(self, *exception: object) -> None:
        """
        Close every listener, which is what ends the accept threads.

        Parameters
        ----------
        *exception : object
            The exception triple, unused.
        """
        for listener in self._listeners.values():
            with contextlib.suppress(OSError):
                listener.close()
        self._listeners.clear()

    def _accept_forever(self, name: str, listener: typing.Any) -> None:  # noqa: ANN401 - Listener
        """
        Accept connections for one target until its listener is closed.

        Parameters
        ----------
        name : str
            Target name, for the queue this thread feeds.
        listener : typing.Any
            The bound ``multiprocessing.connection.Listener``.
        """
        while True:
            try:
                connection = listener.accept()
            except OSError:
                return  # listener.close() from __exit__ unblocks accept() this way
            except Exception:  # noqa: BLE001 - an AuthenticationError, almost always
                self.rejected += 1
                continue
            disable_nagle(connection)
            self._arrivals[name].put(connection)

    def open(self, name: str, deadline: float) -> Connection:
        """
        Wait for the next connection on one target's listener.

        Parameters
        ----------
        name : str
            Target name.
        deadline : float
            ``time.monotonic()`` value after which to give up.

        Returns
        -------
        Connection
            The accepted, authenticated connection.

        Raises
        ------
        DeviceServerAttachTimeoutError
            If no authenticated connection arrived in time.
        """
        remaining = deadline - time.monotonic()
        connection: object | None = None
        if remaining > 0:
            with contextlib.suppress(queue.Empty):
                connection = self._arrivals[name].get(timeout=remaining)
        if connection is None:
            raise DeviceServerAttachTimeoutError(self._timeout_message(name))
        return typing.cast("Connection", connection)

    def _timeout_message(self, name: str) -> str:
        """
        Explain a timeout in terms of what did and did not arrive.

        Parameters
        ----------
        name : str
            The target that was being waited for.

        Returns
        -------
        str
            A diagnosis, naming failed authentications when there were any
            because that is the likeliest cause and the hardest to guess.
        """
        port = self._invitation.ports[name]
        if self.rejected:
            return (
                f"no device server completed the handshake for target "
                f"{name!r} on {self._invitation.host}:{port} before the attach "
                f"deadline, but {self.rejected} connection(s) arrived and "
                f"failed authentication. The bridge is running and can reach "
                f"this client; it is using a different authkey. Re-publish the "
                f"invitation and make sure the bridge reads that exact file."
            )
        return (
            f"no device server dialled in for target {name!r} on "
            f"{self._invitation.host}:{port} before the attach deadline. "
            f"Nothing was launched from here, so there is no process to "
            f"inspect and no stderr to read: check that the bridge is running "
            f"inside its host application, that it was given this "
            f"invitation, and that it can reach this host and port."
        )


def _detach_server(
    instrument: RemoteInstrument,
    devices: Sequence[_RemoteDevice],
) -> None:
    """
    End a session with a device server this client must not kill.

    The graceful half is identical to the spawn path: ask the server to
    park and release its devices, and fall back to closing each device
    individually if that fails. The forcible half is deliberately absent,
    and that is the whole difference. ``terminate()`` on the spawn path
    kills a process this client created for this session; the equivalent
    here would be killing whatever application the bridge lives inside —
    on a Gatan system, DigitalMicrograph, quite possibly mid-acquisition
    for somebody else. A bridge that will not shut down cleanly is
    therefore left alone and the connections are simply dropped.

    Shared-memory reclamation is absent for a different reason: an
    attached link never uses shared memory (the peer may be on another
    machine), so there is nothing to unlink.

    Parameters
    ----------
    instrument : RemoteInstrument
        The instrument target carrying the shutdown handshake.
    devices : Sequence[_RemoteDevice]
        Every device handle, for the fallback path's per-device ``close()``.
    """
    graceful = False
    try:
        report = instrument.shutdown()
    except Exception:  # noqa: BLE001, S110 - any failure means "fall back", not "raise"
        pass
    else:
        graceful = not report.get("errors")
    if not graceful:
        for device in devices:
            with contextlib.suppress(Exception):
                device.close()


@contextlib.contextmanager
def attached_instrument(  # noqa: PLR0913 - one entry point, published in full
    invitation: AttachInvitation | None = None,
    *,
    transport: str = ACCEPT_TRANSPORT,
    host: str = "localhost",
    ports: typing.Mapping[str, int] | None = None,
    authkey: bytes | None = None,
    publish_to: str | os.PathLike[str] | None = None,
    announce: Callable[[str], None] | None = None,
    timeout_s: float = _ATTACH_TIMEOUT_S,
    pickle_protocol: int | None = COMPATIBLE_PICKLE_PROTOCOL,
) -> Iterator[RemoteInstrumentDevices]:
    """
    Attach to a device server that is already running, and drive it.

    The counterpart to :func:`remote_instrument` for adapters that cannot
    be launched. Everything above the transport is unchanged — the same
    ``describe()``-first handshake, the same ``Camera`` and
    ``InstrumentController`` protocols, the same
    :class:`RemoteInstrumentDevices` — so code written against one works
    against the other, which is the point: this is a transport direction,
    not a second device API.

    Parameters
    ----------
    invitation : AttachInvitation | None
        A complete, already-agreed rendezvous. Pass one when the operator
        published it (or when this client published it on an earlier run
        and the bridge is still using it); leave it ``None`` to have one
        built from the arguments below.
    transport : str
        :data:`ACCEPT_TRANSPORT` (this client listens) or
        :data:`CONNECT_TRANSPORT` (the bridge listens). Ignored when
        ``invitation`` is given, which carries its own.
    host : str
        Interface to bind, or host to dial.
    ports : typing.Mapping[str, int] | None
        One port per target name. ``None`` picks free ones — allowed only
        for :data:`ACCEPT_TRANSPORT`, because in the other direction the
        ports are the bridge's to choose and cannot be guessed.
    authkey : bytes | None
        The shared secret. ``None`` generates one, on the same condition
        and for the same reason as ``ports``.
    publish_to : str | os.PathLike[str] | None
        Write the invitation here, ``0600``. This is how the far end
        learns the ports and the key when this client chose them.
    announce : Callable[[str], None] | None
        Called once with :meth:`AttachInvitation.operator_instructions`
        before the wait begins — ``print`` for a console session, a status
        bar update in an application. The wait is long and silent
        otherwise, and the person who must start the bridge is usually the
        person watching this.
    timeout_s : float
        How long to wait for the far end at each step.
    pickle_protocol : int | None
        Cap on the pickle protocol for outgoing calls; see
        :data:`~miainwoodpecker.devices.rpc.COMPATIBLE_PICKLE_PROTOCOL`
        for why the default is not the interpreter's own.

    Yields
    ------
    RemoteInstrumentDevices
        Handles talking to whatever the attached server serves.

    Raises
    ------
    ValueError
        If ``transport`` is not one of :data:`ATTACH_TRANSPORTS`, or if a
        :data:`CONNECT_TRANSPORT` attach was asked for without the ports
        and authkey only the far end can supply.
    """
    if invitation is None:
        if transport not in ATTACH_TRANSPORTS:
            msg = (
                f"unknown transport {transport!r}; expected one of "
                f"{', '.join(ATTACH_TRANSPORTS)}"
            )
            raise ValueError(msg)
        if transport == CONNECT_TRANSPORT and (ports is None or authkey is None):
            msg = (
                "a 'connect' attach needs the ports and authkey the device "
                "server chose; this client cannot derive them, because it is "
                "not the end that bound the listeners. Read them from the "
                "invitation the bridge published (AttachInvitation.read_from)."
            )
            raise ValueError(msg)
        invitation = AttachInvitation(
            host=host,
            ports=(
                dict(ports)
                if ports is not None
                else {name: _free_port() for name in _TARGET_NAMES}
            ),
            authkey=authkey if authkey is not None else secrets.token_bytes(32),
            transport=transport,
        )
    published = invitation.write_to(publish_to) if publish_to is not None else None
    if announce is not None:
        announce(invitation.operator_instructions(published))

    lifecycle = _AttachedServer(invitation.origin())
    opener: _Opener = (
        _AcceptOpener(invitation)
        if invitation.transport == ACCEPT_TRANSPORT
        else _ConnectOpener(invitation)
    )
    connections: dict[str, Connection] = {}
    with opener:
        try:
            deadline = time.monotonic() + timeout_s
            connections["instrument"] = opener.open("instrument", deadline)
            # The health probe's own connection, for the reasons
            # RemoteInstrument gives. Second in, always: it is the one
            # ordering constraint the inbound direction imposes on a bridge.
            connections["instrument:health"] = opener.open("instrument", deadline)
            instrument = RemoteInstrument(
                connections["instrument"],
                health_connection=connections["instrument:health"],
                lifecycle=lifecycle,
                pickle_protocol=pickle_protocol,
            )
            description = instrument.describe()
            served = typing.cast("Sequence[str]", description["targets"])
            for name in served:
                deadline = time.monotonic() + timeout_s
                connections[name] = opener.open(name, deadline)
            cameras = {
                name: RemoteCamera(connections[name], name, lifecycle, pickle_protocol)
                for name in _CAMERA_TARGET_NAMES
                if name in connections
            }
            scanner = (
                RemoteScanner(
                    connections["scanner"], "scanner", lifecycle, pickle_protocol,
                )
                if "scanner" in connections
                else None
            )
            devices: list[_RemoteDevice] = [
                *cameras.values(),
                *([scanner] if scanner is not None else []),
            ]
            try:
                yield RemoteInstrumentDevices(
                    ronchigram_camera=cameras.get("ronchigram_camera"),
                    eels_camera=cameras.get("eels_camera"),
                    camera=cameras.get("camera"),
                    scanner=scanner,
                    instrument=instrument,
                    stage_size_nm=float(
                        typing.cast("float", description["stage_size_nm"]),
                    ),
                )
            finally:
                _detach_server(instrument, devices)
        finally:
            for connection in connections.values():
                with contextlib.suppress(Exception):
                    connection.close()
