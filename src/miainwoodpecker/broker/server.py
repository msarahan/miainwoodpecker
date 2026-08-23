"""
The broker's server half: one instrument, many connections.

A :class:`~miainwoodpecker.broker.local.LocalBroker` already holds the
arbitration; this puts a socket in front of it. Everything about *who may
drive what* stays in the broker, so the wire adds exactly two things a
single process did not need - an identity per connection, and a way to
name a lease in a call.

**One connection carries both call families.** A client's connection
carries broker calls (``target="broker"``: watch, and the lease
lifecycle) and, once it holds a lease, ordinary device calls to the
targets it leased. The device leg is the *same* wire a device server
speaks, dispatched through the same
:func:`~miainwoodpecker.devices.serving.invoke`, which is why the client
side reuses :class:`~miainwoodpecker.devices.remote.RemoteCamera` and its
siblings unchanged rather than growing broker-flavoured copies of them.

**Frames from a leased device take the shared-memory path; watched frames
do not.** A leased target has exactly one caller by construction, so its
reused segment is safe under the same argument the device layer makes
(:mod:`miainwoodpecker.devices.shared_frame`): strictly one request and
response in flight. Watching has the opposite shape - many clients
polling the same latest frame, at display rate, with no exclusivity to
lean on - so :meth:`~miainwoodpecker.broker.local.LocalBroker.latest`
answers on the pickle channel. That costs what
``scripts/ipc_overhead_benchmark.py`` measured for pickling: within noise
below ~500KB, and +1.5 ms at ~1MB, which is the size a 512x512 float scan
frame actually is. It is the wrong trade for a 33MB detector frame, and
the right one for the case watching is: a dashboard tile. A watcher that
needs full-rate large frames should lease.

**Two refusals are the server's own, not the broker's.** A device call
whose ``lease_id`` does not match the claim on that target is refused
before it reaches the device - that is what makes the lease real rather
than advisory. And ``close`` is refused on every device: the broker owns
the instrument's lifecycle, and one client hanging up must not be able to
shut a camera down for everybody else.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import threading
import typing

from miainwoodpecker.broker.interface import BrokerError
from miainwoodpecker.devices.rpc import Result, disable_nagle
from miainwoodpecker.devices.serving import invoke
from miainwoodpecker.devices.shared_frame import SharedFrameWriter

if typing.TYPE_CHECKING:
    from multiprocessing.connection import Connection, Listener

    from miainwoodpecker.broker.local import LocalBroker
    from miainwoodpecker.devices.rpc import Call

_LOGGER = logging.getLogger("miainwoodpecker.broker.server")

BROKER_TARGET = "broker"
"""The target name a client addresses the broker itself under."""

BROKER_METHODS = frozenset(
    {
        "targets",
        "describe",
        "snapshot",
        "controls",
        "camera_parameters",
        "latest",
        "latest_frames",
        "stats",
        "start_live",
        "reconfigure_live",
        "stop_live",
        "grant",
        "release",
        "renew",
        "check_lease",
    },
)
"""
The broker calls a connection may make.

An allowlist rather than "whatever the object has", because
:meth:`~miainwoodpecker.broker.local.LocalBroker.device` hands out raw
device handles and :meth:`~miainwoodpecker.broker.local.LocalBroker.close`
parks the instrument. Neither is a thing one client should be able to do
to every other, and neither would survive the pickle channel anyway.
"""

_REFUSED_DEVICE_METHODS = frozenset({"close"})
"""
Device methods a lease holder may not call.

``close`` releases the device and its shared segment for good. A lease is
temporary custody, not ownership, so the broker keeps this one.
"""


class BrokerServer:
    """
    A listener and its handler threads, serving one broker.

    Parameters
    ----------
    broker : LocalBroker
        The arbitration this server puts a socket in front of.
    listener : Listener
        The bound listener to accept on. Its port is reported by
        :attr:`port`, which matters when it was bound on port 0.
    authkey : bytes
        The secret the listener was bound with. Kept so that whoever
        publishes the connection details does not have to have been the
        one who generated them - a caller that let
        :func:`serve_broker` generate a key would otherwise have no way
        to tell a client what it is.
    """

    def __init__(
        self,
        broker: LocalBroker,
        listener: Listener,
        authkey: bytes = b"",
    ) -> None:
        self._broker = broker
        self._listener = listener
        self._authkey = authkey
        self._stop = threading.Event()
        self._clients = itertools.count(1)
        self._writers: dict[str, SharedFrameWriter] = {}
        self._writer_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._accept = threading.Thread(
            target=self._accept_loop,
            name="broker-accept",
            daemon=True,
        )
        self._accept.start()

    @property
    def port(self) -> int:
        """
        Return the port the listener actually bound.

        Returns
        -------
        int
            The port, which the OS chose when the listener was bound on
            port 0.
        """
        return int(self._listener.address[1])

    @property
    def authkey(self) -> bytes:
        """
        Return the secret this server's listener was bound with.

        Returns
        -------
        bytes
            The authkey, for publishing to clients.
        """
        return self._authkey

    def close(self) -> None:
        """
        Stop accepting, end the handler threads, and retire the segments.

        The shared-memory writers are closed explicitly rather than left
        to the process exiting: a named segment is not reclaimed when its
        creator dies, and this object is the creator.
        """
        self._stop.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        for thread in self._threads:
            thread.join(timeout=1.0)
        with self._writer_lock:
            for writer in self._writers.values():
                writer.close()
            self._writers.clear()

    def _accept_loop(self) -> None:
        """Accept connections until the listener is closed."""
        while not self._stop.is_set():
            try:
                connection = self._listener.accept()
            except OSError:
                return
            holder = f"client-{next(self._clients)}"
            thread = threading.Thread(
                target=self._serve,
                args=(connection, holder),
                name=f"broker-{holder}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def _writer_for(self, target: str) -> SharedFrameWriter:
        """
        Return this target's reused shared-memory writer, making it once.

        One writer per target rather than per connection, which is safe
        for exactly the reason the device layer's single writer is: only
        the lease holder can call the target, and that connection is
        strictly one request and response at a time.

        Parameters
        ----------
        target : str
            The target name.

        Returns
        -------
        SharedFrameWriter
            Its writer.
        """
        with self._writer_lock:
            writer = self._writers.get(target)
            if writer is None:
                writer = SharedFrameWriter()
                self._writers[target] = writer
            return writer

    def _serve(self, connection: Connection, holder: str) -> None:
        """
        Handle one client's calls until it disconnects.

        Parameters
        ----------
        connection : Connection
            The accepted connection.
        holder : str
            The identity leases taken over it are recorded under.
        """
        disable_nagle(connection)
        granted: set[str] = set()
        _LOGGER.info("broker: %s connected", holder)
        try:
            while not self._stop.is_set():
                try:
                    call = typing.cast("Call", connection.recv())
                except (EOFError, OSError):
                    return
                result = self._dispatch(call, holder, granted)
                try:
                    connection.send(result)
                except (EOFError, OSError):
                    return
        finally:
            # A client that hangs up has not released its leases, and
            # waiting out the time to live would leave the probe parked
            # for as long as five minutes over a closed socket. The
            # broker restarts the loops on release, so this is the same
            # recovery expiry would eventually make - just at the moment
            # the loss is actually observable.
            for lease_id in granted:
                self._broker.release(lease_id)
            _LOGGER.info("broker: %s disconnected", holder)
            with contextlib.suppress(OSError):
                connection.close()

    def _dispatch(self, call: Call, holder: str, granted: set[str]) -> Result:
        """
        Route one call to the broker or to a leased device.

        Parameters
        ----------
        call : Call
            The client's request.
        holder : str
            This connection's identity.
        granted : set[str]
            Lease ids taken over this connection, updated in place so a
            disconnect can release them.

        Returns
        -------
        Result
            The value, or a stringified error - never a raised
            exception, which would take the connection down with it.
        """
        if call.target == BROKER_TARGET:
            return self._broker_call(call, holder, granted)
        return self._device_call(call)

    def _broker_call(self, call: Call, holder: str, granted: set[str]) -> Result:
        """
        Run one call against the broker itself.

        Parameters
        ----------
        call : Call
            The client's request.
        holder : str
            This connection's identity, which ``grant`` records rather
            than anything the client sent.
        granted : set[str]
            Lease ids taken over this connection.

        Returns
        -------
        Result
            The value, or a stringified error.
        """
        if call.method not in BROKER_METHODS:
            return Result(error=f"unknown broker method {call.method!r}")
        kwargs = dict(call.kwargs)
        if call.method == "grant":
            kwargs["holder"] = holder
        try:
            value = getattr(self._broker, call.method)(*call.args, **kwargs)
        except BrokerError as exc:
            # A refusal, not a fault: the client is expected to handle
            # these, so they are logged at debug and travel by type name.
            _LOGGER.debug("broker: %s refused %s", holder, call.method)
            return Result(
                error=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
            )
        except Exception as exc:  # one bad call must not take the connection down
            _LOGGER.exception("broker: %s call %s() raised", holder, call.method)
            return Result(
                error=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
            )
        if call.method == "grant":
            granted.add(value.lease_id)
        elif call.method == "release":
            granted.discard(call.args[0] if call.args else kwargs.get("lease_id"))
        return Result(value=value)

    def _device_call(self, call: Call) -> Result:
        """
        Run one call against a leased device, having checked the lease.

        The check is what makes a lease real rather than advisory: an
        expired or absent claim is refused here, before the device is
        touched, because by the time an expired lease's holder calls
        again the targets may already be somebody else's.

        Parameters
        ----------
        call : Call
            The client's request, carrying the lease it is made under.

        Returns
        -------
        Result
            The value, or a stringified error.
        """
        if call.method in _REFUSED_DEVICE_METHODS:
            return Result(
                error=f"{call.method}() is the broker's to call, not a lease holder's",
                error_type="PermissionError",
            )
        if call.lease_id is None:
            return Result(
                error=f"{call.target} can only be called under a lease",
                error_type="LeaseExpiredError",
            )
        try:
            lease = self._broker.check_lease(call.lease_id)
        except BrokerError as exc:
            return Result(
                error=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
            )
        if call.target not in lease.targets:
            return Result(
                error=f"lease {call.lease_id} does not hold {call.target}",
                error_type="LeaseExpiredError",
            )
        try:
            device = self._broker.device(call.target)
        except KeyError as exc:
            return Result(error=f"KeyError: {exc}", error_type="KeyError")
        return invoke(device, call, self._writer_for(call.target), call.target)


def serve_broker(
    broker: LocalBroker,
    *,
    host: str = "localhost",
    port: int = 0,
    authkey: bytes,
) -> BrokerServer:
    """
    Bind a listener and start serving one broker on it.

    Parameters
    ----------
    broker : LocalBroker
        The arbitration to serve.
    host : str
        Interface to bind. The default keeps it on this machine, which
        is the right default for something that can move a probe.
    port : int
        Port to bind, or 0 to let the OS choose one - read it back from
        :attr:`BrokerServer.port`.
    authkey : bytes
        Shared secret every client must present.

    Returns
    -------
    BrokerServer
        The running server. Close it to stop.
    """
    from multiprocessing.connection import Listener  # noqa: PLC0415 - see nion_server

    listener = Listener((host, port), authkey=authkey)
    return BrokerServer(broker, listener, authkey)


__all__ = [
    "BROKER_METHODS",
    "BROKER_TARGET",
    "BrokerServer",
    "serve_broker",
]
