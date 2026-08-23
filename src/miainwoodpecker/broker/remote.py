"""
The broker's client half: an instrument two processes away.

:class:`RemoteBroker` implements
:class:`~miainwoodpecker.broker.interface.InstrumentBroker` over the
:mod:`miainwoodpecker.devices.rpc` wire, so a notebook kernel, a browser
dashboard's back end or an agent gets the same object the Qt viewer holds
in process. The conformance tests run against both.

Two things are worth knowing about how it is built.

**The leased devices are the device layer's own remote classes.** A lease
yields :class:`~miainwoodpecker.devices.remote.RemoteCamera` and its
siblings, stamped with the lease id and sharing this connection's lock -
not broker-flavoured reimplementations. That is what makes the promise in
:mod:`miainwoodpecker.broker.interface` true across the wire as well as
in process: every generator in :mod:`miainwoodpecker.acquisition` works
inside a remote lease because it is calling the same class it would call
through :func:`~miainwoodpecker.devices.remote.remote_instrument`.

**Refusals arrive as names and leave as exceptions.** The wire carries a
:class:`~miainwoodpecker.devices.rpc.Result` with an ``error_type``
string, because shipping a live exception object would mean unpickling
the server's classes here. :func:`_translate` turns the two names that
matter back into
:class:`~miainwoodpecker.broker.interface.DeviceBusyError` and
:class:`~miainwoodpecker.broker.interface.LeaseExpiredError`, so a caller
writes one ``except`` clause whichever side of the socket it is on. A
device object is wrapped in :class:`_Translated` for the same reason: an
expired lease must raise ``LeaseExpiredError`` at the call site that
would have moved the probe, not a generic remote-call failure.
"""

from __future__ import annotations

import contextlib
import threading
import typing

from miainwoodpecker.broker.interface import (
    DEFAULT_LEASE_TIMEOUT_S,
    DEFAULT_LEASE_TTL_S,
    DEFAULT_PREVIEW_EDGE,
    DeviceBusyError,
    Lease,
    LeaseExpiredError,
    NotLiveError,
)
from miainwoodpecker.broker.server import BROKER_TARGET
from miainwoodpecker.devices.preview import decimation_stride
from miainwoodpecker.devices.remote import (
    RemoteCamera,
    RemoteScanner,
    RemoteSpectrumDetector,
)
from miainwoodpecker.devices.rpc import (
    INSTRUMENT_TARGET,
    Call,
    RemoteCallError,
    send_call,
    target_kind,
)

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from multiprocessing.connection import Connection

    from miainwoodpecker.acquisition.live import LiveStats
    from miainwoodpecker.broker.interface import (
        TargetDescription,
        TargetPreview,
        TargetState,
        TargetView,
    )
    from miainwoodpecker.devices.interface import (
        Camera,
        CameraParameters,
        Frame,
        InstrumentController,
        ScanParameters,
        Scanner,
        SpectrumDetector,
    )

_CAMERA_KIND = "camera"
_SCANNER_KIND = "scanner"
_SPECTRUM_KIND = "spectrum"

_ERRORS = {
    "DeviceBusyError": DeviceBusyError,
    "LeaseExpiredError": LeaseExpiredError,
    "NotLiveError": NotLiveError,
}
"""
Server-side exception names this client rebuilds, by name.

Only the broker's own refusals. Everything else stays a
:class:`~miainwoodpecker.devices.rpc.RemoteCallError`, which is the
honest answer for a failure that has no meaning on this side.
"""


def _detail(error: RemoteCallError) -> str:
    """
    Peel the wire's wrapping off a server-side error message.

    ``send_call`` wraps the server's ``"TypeName: message"`` in
    ``"remote call x.y() failed: ..."``, so there are two layers between
    the caller and the sentence the broker actually wrote.

    Parameters
    ----------
    error : RemoteCallError
        What ``send_call`` raised.

    Returns
    -------
    str
        The broker's own message, or the whole thing if it does not have
        the expected shape.
    """
    _, separator, detail = str(error).partition("failed: ")
    detail = detail if separator else str(error)
    _, separator, stripped = detail.partition(f"{error.error_type}: ")
    return stripped if separator else detail


def _raise_refusal(error: RemoteCallError) -> typing.NoReturn:
    """
    Re-raise a remote failure as the exception it was on the other side.

    What survives the crossing is the type name and the message, so that
    is what is rebuilt: the type, and the message verbatim. The
    exception's identity field - ``target``, or ``lease_id`` - is
    recovered from the message's first word, which every
    :class:`~miainwoodpecker.broker.interface.BrokerError` begins with
    for exactly this reason.

    ``holder`` and ``reason`` are **not** recovered, and are left None
    and empty rather than guessed out of the prose. A caller that needs
    them should read
    :attr:`~miainwoodpecker.broker.interface.TargetState.lease`, which
    crosses as data. A caller that only branches on the type gets what
    it would in process.

    Parameters
    ----------
    error : RemoteCallError
        What ``send_call`` raised.

    Returns
    -------
    typing.NoReturn
        Never. Every path out of this function raises.

    Raises
    ------
    DeviceBusyError
        If the broker refused because a target was held or would not
        join.
    LeaseExpiredError
        If the lease the call was made under is no longer valid.
    NotLiveError
        If a live-loop reading was asked of a target with no loop.
    KeyError
        If the target does not exist. It is a ``KeyError`` in process
        and has to stay one here: a caller telling "no such camera"
        apart from "the camera refused" branches on the type, not on
        the text.
    ValueError
        If the call was wrong rather than refused - asking a camera for
        a scan geometry, say. Kept as itself for the same reason.
    RemoteCallError
        For anything else. Rebuilt rather than re-raised so that every
        exit from this function names the class it raises - the message
        and ``error_type`` are carried over intact, and the original is
        chained as the cause.
    """
    kind = _ERRORS.get(error.error_type or "")
    detail = _detail(error)
    subject = detail.split(" ", 1)[0]
    if kind is DeviceBusyError:
        raise DeviceBusyError(subject, message=detail) from error
    if kind is LeaseExpiredError:
        raise LeaseExpiredError(subject, message=detail) from error
    if kind is NotLiveError:
        raise NotLiveError(subject, message=detail) from error
    if error.error_type == "KeyError":
        raise KeyError(detail.strip("'")) from error
    if error.error_type == "ValueError":
        # Same reasoning as KeyError: asking a camera for a scan
        # geometry is a ValueError in process and stays one here,
        # because a caller separating "wrong argument" from "the device
        # refused" branches on the type rather than on the text.
        raise ValueError(detail) from error
    raise RemoteCallError(str(error), error_type=error.error_type) from error


class _Translated:
    """
    A remote device whose broker refusals arrive as broker exceptions.

    Wraps one of the device layer's remote classes. Attribute reads go
    through because a device protocol's properties (``camera_id``,
    ``channel_names``) are calls in disguise and can fail the same way;
    callables are wrapped so the refusal lands when the method is
    invoked, not when it is looked up.

    Parameters
    ----------
    device : object
        The remote device to wrap.
    """

    def __init__(self, device: object) -> None:
        object.__setattr__(self, "_device", device)

    def __getattr__(self, name: str) -> object:
        """
        Return the wrapped device's attribute, translating refusals.

        A failure goes through :func:`_raise_refusal`, so an expired
        lease surfaces as
        :class:`~miainwoodpecker.broker.interface.LeaseExpiredError`
        rather than as a generic remote failure.

        Parameters
        ----------
        name : str
            The attribute being read.

        Returns
        -------
        object
            The value, or a callable that translates on invocation.
        """
        device = object.__getattribute__(self, "_device")
        try:
            value = getattr(device, name)
        except RemoteCallError as error:
            _raise_refusal(error)
        if not callable(value):
            return value

        def call(*args: object, **kwargs: object) -> object:
            """
            Invoke the wrapped method, translating a broker refusal.

            A failure goes through :func:`_raise_refusal`, so the
            refusal lands here - at the call that would have moved the
            probe - rather than where the method was looked up.

            Parameters
            ----------
            *args : object
                Positional arguments.
            **kwargs : object
                Keyword arguments.

            Returns
            -------
            object
                Whatever the method returned.
            """
            try:
                return value(*args, **kwargs)
            except RemoteCallError as error:
                _raise_refusal(error)

        return call


class _RemoteControls:
    """
    An ``InstrumentController`` reached through a broker lease.

    Every member of that protocol is a method - there are no properties
    to tell apart from calls - so one forwarder covers all of it, and
    there is nothing to keep in step as the controller grows a control.

    Parameters
    ----------
    broker : RemoteBroker
        The connection to send on.
    lease_id : str
        The lease these controls are held under.
    """

    def __init__(self, broker: RemoteBroker, lease_id: str) -> None:
        self._broker = broker
        self._lease_id = lease_id

    def __getattr__(self, name: str) -> Callable[..., object]:
        """
        Return a callable that forwards one control call.

        Parameters
        ----------
        name : str
            The control method being called.

        Returns
        -------
        Callable[..., object]
            The forwarder.
        """

        def call(*args: object, **kwargs: object) -> object:
            """
            Forward one instrument-control call under the lease.

            Parameters
            ----------
            *args : object
                Positional arguments.
            **kwargs : object
                Keyword arguments.

            Returns
            -------
            object
                The control's return value.
            """
            return self._broker.call(
                INSTRUMENT_TARGET,
                name,
                args,
                kwargs,
                lease_id=self._lease_id,
            )

        return call


class RemoteLeasedDevices:
    """
    The devices of one remote lease.

    Parameters
    ----------
    broker : RemoteBroker
        The connection the devices are reached over.
    lease : Lease
        The granted lease.
    """

    def __init__(self, broker: RemoteBroker, lease: Lease) -> None:
        self._broker = broker
        self._lease = lease
        self._devices: dict[str, object] = {}

    @property
    def lease(self) -> Lease:
        """
        The lease these devices are held under.

        Returns
        -------
        Lease
            The lease as the broker granted it.
        """
        return self._lease

    def _sole(self, kind: str, target: str | None) -> str:
        """
        Resolve a target name, refusing to guess between two of a kind.

        Parameters
        ----------
        kind : str
            The device kind wanted.
        target : str | None
            The caller's choice, or None to mean "the only one".

        Returns
        -------
        str
            The resolved target name.

        Raises
        ------
        KeyError
            If the lease holds no target of that kind, or several and
            the caller named none.
        """
        if target is not None:
            if target not in self._lease.targets:
                message = f"lease does not hold a {kind} named {target}"
                raise KeyError(message)
            return target
        held = [name for name in self._lease.targets if target_kind(name) == kind]
        if len(held) != 1:
            message = f"lease holds {len(held)} targets of kind {kind}; name one"
            raise KeyError(message)
        return held[0]

    def _device(self, target: str, build: Callable[[], object]) -> object:
        """
        Return this lease's handle for a target, building it once.

        Parameters
        ----------
        target : str
            The target name.
        build : Callable[[], object]
            Makes the handle if there is not one yet.

        Returns
        -------
        object
            The wrapped device.
        """
        device = self._devices.get(target)
        if device is None:
            device = _Translated(build())
            self._devices[target] = device
        return device

    def camera(self, target: str | None = None) -> Camera:
        """
        Return a leased camera.

        Parameters
        ----------
        target : str | None
            Which camera, by target name. None means the only one.

        Returns
        -------
        Camera
            The device-layer camera protocol, over the wire.
        """
        name = self._sole(_CAMERA_KIND, target)
        built = self._device(
            name,
            lambda: self._broker.device(RemoteCamera, name, self._lease.lease_id),
        )
        return typing.cast("Camera", built)

    def scanner(self, target: str | None = None) -> Scanner:
        """
        Return the leased scan unit.

        Parameters
        ----------
        target : str | None
            Which scanner, by target name. None means the only one.

        Returns
        -------
        Scanner
            The device-layer scanner protocol, over the wire.
        """
        name = self._sole(_SCANNER_KIND, target)
        built = self._device(
            name,
            lambda: self._broker.device(RemoteScanner, name, self._lease.lease_id),
        )
        return typing.cast("Scanner", built)

    def spectrum_detector(self, target: str | None = None) -> SpectrumDetector:
        """
        Return a leased spectrum detector.

        Parameters
        ----------
        target : str | None
            Which detector, by target name. None means the only one.

        Returns
        -------
        SpectrumDetector
            The device-layer spectrum-detector protocol, over the wire.
        """
        name = self._sole(_SPECTRUM_KIND, target)
        built = self._device(
            name,
            lambda: self._broker.device(
                RemoteSpectrumDetector,
                name,
                self._lease.lease_id,
            ),
        )
        return typing.cast("SpectrumDetector", built)

    @property
    def instrument(self) -> InstrumentController:
        """
        The leased instrument controls.

        Returns
        -------
        InstrumentController
            The controls, over the wire.

        Raises
        ------
        KeyError
            If the lease does not include the ``instrument`` target.
        """
        if INSTRUMENT_TARGET not in self._lease.targets:
            message = f"lease does not hold {INSTRUMENT_TARGET}"
            raise KeyError(message)
        controls = _RemoteControls(self._broker, self._lease.lease_id)
        return typing.cast("InstrumentController", controls)

    def renew(self, ttl_s: float = DEFAULT_LEASE_TTL_S) -> Lease:
        """
        Extend the lease's deadline.

        Parameters
        ----------
        ttl_s : float
            Seconds from now that the lease should expire.

        Returns
        -------
        Lease
            The lease, with its new deadline.
        """
        renewed = typing.cast(
            "Lease",
            self._broker.broker_call("renew", (self._lease.lease_id, ttl_s)),
        )
        self._lease = renewed
        return renewed


class RemoteBroker:
    """
    One instrument's broker, reached over a socket.

    Parameters
    ----------
    connection : Connection
        An open connection to a :class:`~miainwoodpecker.broker.server.BrokerServer`.
        Every call this object makes travels over it, broker calls and
        leased device calls alike, serialised by one lock.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._lock = threading.Lock()

    def call(
        self,
        target: str,
        method: str,
        args: tuple = (),
        kwargs: Mapping[str, object] | None = None,
        *,
        lease_id: str | None = None,
    ) -> object:
        """
        Send one call over this broker's connection.

        A failure goes through :func:`_raise_refusal`, which rebuilds
        the broker's own refusals as themselves and leaves everything
        else a :class:`~miainwoodpecker.devices.rpc.RemoteCallError`.

        Parameters
        ----------
        target : str
            The target name, or ``"broker"`` for the broker itself.
        method : str
            The method to call.
        args : tuple
            Positional arguments.
        kwargs : Mapping[str, object] | None
            Keyword arguments.
        lease_id : str | None
            The lease the call is made under, for a device target.

        Returns
        -------
        object
            The call's return value.
        """
        call = Call(target, method, args, dict(kwargs or {}), lease_id=lease_id)
        try:
            return send_call(self._connection, self._lock, call)
        except RemoteCallError as error:
            _raise_refusal(error)

    def broker_call(
        self,
        method: str,
        args: tuple = (),
        kwargs: Mapping[str, object] | None = None,
    ) -> object:
        """
        Send one call to the broker itself.

        Parameters
        ----------
        method : str
            The broker method.
        args : tuple
            Positional arguments.
        kwargs : Mapping[str, object] | None
            Keyword arguments.

        Returns
        -------
        object
            The call's return value.
        """
        return self.call(BROKER_TARGET, method, args, kwargs)

    def device(self, kind: type, target: str, lease_id: str) -> object:
        """
        Build one of the device layer's remote classes on this connection.

        Parameters
        ----------
        kind : type
            The remote device class to construct.
        target : str
            The target name it addresses.
        lease_id : str
            The lease every call it makes is stamped with. Required, not
            optional: an unstamped device call is refused by the server,
            and a handle that could be built without one would be a
            handle that looks usable and is not.

        Returns
        -------
        object
            The device, sharing this connection and its lock.
        """
        return kind(self._connection, target, lease_id=lease_id, lock=self._lock)

    # -- watching ---------------------------------------------------------

    def targets(self) -> Mapping[str, TargetState]:
        """
        Return every target this instrument serves, and its state.

        Returns
        -------
        Mapping[str, TargetState]
            Keyed by target name.
        """
        return typing.cast("Mapping[str, TargetState]", self.broker_call("targets"))

    def describe(self) -> Mapping[str, TargetDescription]:
        """
        Return what each target is, rather than what it is doing.

        Static and cached server-side, so calling it per client is one
        pickled dictionary rather than a burst of device calls. This is
        what lets a client in another process offer detector checkboxes
        and a binning menu instead of only a picture.

        Returns
        -------
        Mapping[str, TargetDescription]
            Keyed by target name.
        """
        described = self.broker_call("describe")
        return typing.cast("Mapping[str, TargetDescription]", described)

    def snapshot(self) -> Mapping[str, TargetView]:
        """
        Return every target's state and latest frames, in one pass.

        Note what this costs over a wire that :class:`LocalBroker` does
        not pay: every target's pixels, at full size, pickled, on every
        call. It is the right call for a viewer sharing a process with
        its broker, and for a client that wants the *measurement* -
        calibrated pixels in the device's own dtype.

        A client that wants a live *picture* wants :meth:`previews`,
        which is this call with the decimation done before the pixels
        are put on the wire rather than after they arrive. That is the
        difference between roughly 19 MB and roughly 200 kB per poll on
        an ordinary instrument, and therefore the difference between two
        frames a second and ten.

        Returns
        -------
        Mapping[str, TargetView]
            Keyed by target name.
        """
        return typing.cast("Mapping[str, TargetView]", self.broker_call("snapshot"))

    def previews(
        self,
        max_edge: int = DEFAULT_PREVIEW_EDGE,
    ) -> Mapping[str, TargetPreview]:
        """
        Return every target's state and latest frames, as pictures.

        The one watch call written for the wire rather than in spite of
        it: the decimation happens in the server process, so what
        crosses is what a tile draws. Everything :meth:`snapshot`
        promises about reading state and frames together holds, because
        the server answers this by calling
        :meth:`~miainwoodpecker.broker.local.LocalBroker.previews` under
        the same lock.

        A ``max_edge`` below 1 raises ``ValueError`` **here**, before the
        call is sent, rather than after a round trip: a caller that got
        this wrong gets the same message whichever transport it is on,
        and an instrument that is busy does not have to answer a call
        that was never going to succeed.

        Parameters
        ----------
        max_edge : int
            Longest edge, in pixels, to reduce each frame to. Sent to
            the server, which is where it takes effect.

        Returns
        -------
        Mapping[str, TargetPreview]
            Keyed by target name.
        """
        decimation_stride((1,), max_edge)
        return typing.cast(
            "Mapping[str, TargetPreview]",
            self.broker_call("previews", (max_edge,)),
        )

    def controls(self) -> Mapping[str, float | bool]:
        """
        Return the instrument controls' current values, read only.

        Returns
        -------
        Mapping[str, float | bool]
            Keyed by control name.
        """
        return typing.cast("Mapping[str, float | bool]", self.broker_call("controls"))

    def camera_parameters(self, target: str) -> CameraParameters | None:
        """
        Return what a detector is currently configured to do.

        Parameters
        ----------
        target : str
            The detector to read.

        Returns
        -------
        CameraParameters | None
            Its exposure, binning and readout mode, or None for a target
            that has none.
        """
        return typing.cast(
            "CameraParameters | None",
            self.broker_call("camera_parameters", (target,)),
        )

    def latest(self, target: str) -> Frame | None:
        """
        Return the most recent frame from a target's live loop.

        Parameters
        ----------
        target : str
            The target to read.

        Returns
        -------
        Frame | None
            The latest frame, or None if no loop is running or none has
            arrived. It crosses on the pickle channel - see the server
            module's docstring for why watching does not use shared
            memory and leasing does.
        """
        return typing.cast("Frame | None", self.broker_call("latest", (target,)))

    def latest_frames(self, target: str) -> tuple[Frame, ...]:
        """
        Return every frame of a target's most recent pass.

        Parameters
        ----------
        target : str
            The target to read.

        Returns
        -------
        tuple[Frame, ...]
            The pass's frames, empty before the first pass completes.
        """
        frames = self.broker_call("latest_frames", (target,))
        return tuple(typing.cast("Sequence[Frame]", frames))

    def stats(self, target: str) -> LiveStats:
        """
        Return a target's live-loop frame count and recent rate.

        Parameters
        ----------
        target : str
            The target to read.

        Returns
        -------
        LiveStats
            Passes rather than frames for a multichannel scan loop.
        """
        return typing.cast("LiveStats", self.broker_call("stats", (target,)))

    # -- live loops -------------------------------------------------------

    def start_live(
        self,
        target: str,
        parameters: ScanParameters | None = None,
        *,
        channels: Sequence[int] = (0,),
    ) -> None:
        """
        Start a live loop on a target, if one is not already running.

        Parameters
        ----------
        target : str
            The target to start.
        parameters : ScanParameters | None
            Scan geometry and dwell, for a scanner.
        channels : Sequence[int]
            Which detectors to read out per pass, for a scanner.
        """
        self.broker_call(
            "start_live",
            (target, parameters),
            {"channels": tuple(channels)},
        )

    def reconfigure_live(
        self,
        target: str,
        parameters: ScanParameters,
        *,
        channels: Sequence[int] = (0,),
    ) -> None:
        """
        Change a running scan's geometry without stopping it.

        Parameters
        ----------
        target : str
            The scan unit to reconfigure.
        parameters : ScanParameters
            The geometry and dwell to use from the next pass on.
        channels : Sequence[int]
            Which detectors to read out per pass.
        """
        self.broker_call(
            "reconfigure_live",
            (target, parameters),
            {"channels": tuple(channels)},
        )

    def stop_live(self, target: str) -> bool:
        """
        Stop a target's live loop.

        Parameters
        ----------
        target : str
            The target to stop.

        Returns
        -------
        bool
            True if the worker finished.
        """
        return bool(self.broker_call("stop_live", (target,)))

    # -- leasing ----------------------------------------------------------

    def grant(
        self,
        targets: Sequence[str],
        *,
        reason: str = "",
        timeout_s: float = DEFAULT_LEASE_TIMEOUT_S,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
    ) -> Lease:
        """
        Claim targets and return the granted lease.

        The holder recorded on the lease is this *connection's* identity,
        assigned by the server. There is deliberately no parameter for
        it here.

        Parameters
        ----------
        targets : Sequence[str]
            The targets to hold.
        reason : str
            What the lease is for.
        timeout_s : float
            Seconds to wait for each worker to join.
        ttl_s : float
            Seconds the lease survives without renewal.

        Returns
        -------
        Lease
            The granted lease.
        """
        granted = self.broker_call(
            "grant",
            (tuple(targets),),
            {"reason": reason, "timeout_s": timeout_s, "ttl_s": ttl_s},
        )
        return typing.cast("Lease", granted)

    def check_lease(self, lease_id: str) -> Lease:
        """
        Return a lease, or refuse if the broker has reclaimed it.

        Parameters
        ----------
        lease_id : str
            The lease to check.

        Returns
        -------
        Lease
            The live lease.
        """
        return typing.cast("Lease", self.broker_call("check_lease", (lease_id,)))

    def release(self, lease_id: str) -> None:
        """
        Drop a lease's claims and restart the loops it paused.

        Parameters
        ----------
        lease_id : str
            The lease to release.
        """
        self.broker_call("release", (lease_id,))

    def renew(self, lease_id: str, ttl_s: float = DEFAULT_LEASE_TTL_S) -> Lease:
        """
        Extend a lease's deadline.

        Parameters
        ----------
        lease_id : str
            The lease to extend.
        ttl_s : float
            Seconds from now that it should expire.

        Returns
        -------
        Lease
            The lease, with its new deadline.
        """
        return typing.cast("Lease", self.broker_call("renew", (lease_id, ttl_s)))

    @contextlib.contextmanager
    def lease(
        self,
        *targets: str,
        reason: str = "",
        timeout_s: float = DEFAULT_LEASE_TIMEOUT_S,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
    ) -> Iterator[RemoteLeasedDevices]:
        """
        Take exclusive control of one or more targets for a block.

        The two calls :meth:`grant` and :meth:`release` with a ``try``
        between them, which is the whole difference between the remote
        broker and the in-process one at this level.

        Parameters
        ----------
        *targets : str
            The targets to hold.
        reason : str
            What the lease is for.
        timeout_s : float
            Seconds to wait for each target's worker to join.
        ttl_s : float
            Seconds the lease survives without renewal.

        Yields
        ------
        RemoteLeasedDevices
            The leased devices, as the device-layer protocols.
        """
        granted = self.grant(
            targets,
            reason=reason,
            timeout_s=timeout_s,
            ttl_s=ttl_s,
        )
        try:
            yield RemoteLeasedDevices(self, granted)
        finally:
            # A lease the server has already reclaimed is not an error to
            # release: expiry and block exit both arrive here, and the
            # second one must not raise on the way out of a `with`.
            with contextlib.suppress(LeaseExpiredError):
                self.release(granted.lease_id)

    def close(self) -> None:
        """
        Close the connection, releasing anything this client still holds.

        The server does the releasing, on seeing the socket close - see
        its ``_serve`` for why that is not left to the time to live.
        """
        with contextlib.suppress(OSError):
            self._connection.close()


def connect_broker(
    address: tuple[str, int],
    authkey: bytes,
) -> RemoteBroker:
    """
    Connect to a running broker server.

    Parameters
    ----------
    address : tuple[str, int]
        Host and port the server is listening on.
    authkey : bytes
        The shared secret the server was started with.

    Returns
    -------
    RemoteBroker
        The connected client.
    """
    from multiprocessing.connection import Client  # noqa: PLC0415 - see nion_server

    from miainwoodpecker.devices.rpc import disable_nagle  # noqa: PLC0415 - local use

    connection = Client(address, authkey=authkey)
    disable_nagle(connection)
    return RemoteBroker(connection)
