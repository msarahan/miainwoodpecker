"""
The in-process broker: device handles held directly, no transport.

This is the reference implementation of
:class:`~miainwoodpecker.broker.interface.InstrumentBroker`, and the one
the conformance tests run against. It is not a stand-in for a remote
broker - it is what a remote broker runs *inside*. The transport version
is this object plus an
:mod:`~miainwoodpecker.devices.rpc` server in front of it, which is why
the arbitration lives here rather than in the wire protocol: a Qt viewer
that never opens a socket gets the same guarantees as a notebook two
machines away.

Two implementation choices are worth stating, because a reader will
otherwise assume the easier ones.

**Expiry is swept lazily, not by a timer thread.** Every public method
begins by reclaiming leases whose deadline has passed. A background timer
would add a thread whose only job is to race the one it is protecting,
and it would make the behaviour untestable without sleeping; a clock
parameter and a lazy sweep make "the kernel died four hundred seconds
ago" an assertion rather than a wait.

**The broker's own lock is never held across a device call.** Stopping a
live loop can take as long as one exposure, and a watcher polling
:meth:`LocalBroker.targets` while a lease is being granted must not block
for it. Claims are taken under the lock, which is fast; the joins that
follow happen outside it, and a failed join undoes the claim.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import typing
import uuid
from dataclasses import dataclass, replace

from miainwoodpecker.acquisition.live import (
    LiveAcquisition,
    MultiChannelLiveAcquisition,
)
from miainwoodpecker.broker.interface import (
    DEFAULT_LEASE_TIMEOUT_S,
    DEFAULT_LEASE_TTL_S,
    DeviceBusyError,
    Lease,
    LeaseExpiredError,
    NotLiveError,
    TargetDescription,
    TargetState,
    TargetView,
    lease_order,
)
from miainwoodpecker.devices.interface import (
    BEAM_BLANKER_CONTROL,
    DEFOCUS_CONTROL,
    ENERGY_OFFSET_CONTROL,
    STAGE_POSITION_CONTROL,
)
from miainwoodpecker.devices.rpc import INSTRUMENT_TARGET, target_kind

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from miainwoodpecker.acquisition.live import LiveStats
    from miainwoodpecker.devices.interface import (
        Camera,
        Frame,
        InstrumentController,
        ScanParameters,
        Scanner,
        SpectrumDetector,
    )


_LOGGER = logging.getLogger("miainwoodpecker.broker.local")

_PASS_SLACK = 2.0
"""
How much longer than the dwell arithmetic a scan pass is allowed to take.

Flyback, readout and thread scheduling are all real and none of them are
in ``height x width x dwell``. This is a bound on *waiting*, which costs
nothing when the worker returns early, so it is generous on purpose.
"""

_CAMERA_KIND = "camera"
_SCANNER_KIND = "scanner"
_SPECTRUM_KIND = "spectrum"


@dataclass(frozen=True)
class _LiveSpec:
    """
    How a live loop was started, kept so it can be restarted identically.

    Attributes
    ----------
    parameters : ScanParameters | None
        The scan geometry, for a scanner.
    channels : tuple[int, ...]
        The detectors read out per pass, for a scanner.
    """

    parameters: ScanParameters | None
    channels: tuple[int, ...]


class _LeaseGuard:
    """
    A device wrapper that refuses calls once its lease has been released.

    The device handles themselves know nothing about leases, and giving
    them out bare would mean a client whose lease expired mid-script
    keeps driving a device the broker has already handed to somebody
    else - silently, because the call would succeed. Attribute access
    goes through the check, so the refusal lands at the call site that
    would have moved the probe.

    Parameters
    ----------
    device : object
        The device-layer object being guarded.
    check : Callable[[], None]
        Raises :class:`~miainwoodpecker.broker.interface.LeaseExpiredError`
        if the lease is no longer valid.
    """

    def __init__(self, device: object, check: Callable[[], None]) -> None:
        object.__setattr__(self, "_device", device)
        object.__setattr__(self, "_check", check)

    def __getattr__(self, name: str) -> object:
        """
        Return the guarded device's attribute, if the lease still holds.

        Parameters
        ----------
        name : str
            The attribute being read.

        Returns
        -------
        object
            Whatever the device has under that name.
        """
        object.__getattribute__(self, "_check")()
        return getattr(object.__getattribute__(self, "_device"), name)


@dataclass(frozen=True)
class _LeasedDevices:
    """
    The devices of one lease, guarded against expiry.

    Attributes
    ----------
    broker : LocalBroker
        The broker that granted the lease.
    lease : Lease
        The lease itself. Replaced wholesale by :meth:`renew`, which is
        why the broker holds the authoritative copy and this is a
        snapshot for reading.
    """

    broker: LocalBroker
    lease: Lease

    def _guard(self, target: str, kind: str) -> object:
        """
        Return the guarded device for a target held by this lease.

        Parameters
        ----------
        target : str
            The target name, or None resolved by the caller already.
        kind : str
            The kind the caller asked for, for the error message.

        Returns
        -------
        object
            The device, wrapped in a :class:`_LeaseGuard`.

        Raises
        ------
        KeyError
            If this lease does not hold that target.
        """
        if target not in self.lease.targets:
            message = f"lease does not hold a {kind} named {target}"
            raise KeyError(message)
        device = self.broker.device(target)
        return _LeaseGuard(device, lambda: self.broker.check_lease(self.lease.lease_id))

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
            the caller named none. Guessing between a Ronchigram and an
            EELS camera is how a recording ends up labelled as the wrong
            detector.
        """
        if target is not None:
            return target
        held = [name for name in self.lease.targets if target_kind(name) == kind]
        if len(held) != 1:
            message = f"lease holds {len(held)} targets of kind {kind}; name one"
            raise KeyError(message)
        return held[0]

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
            The device-layer camera protocol.
        """
        name = self._sole(_CAMERA_KIND, target)
        return typing.cast("Camera", self._guard(name, _CAMERA_KIND))

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
            The device-layer scanner protocol.
        """
        name = self._sole(_SCANNER_KIND, target)
        return typing.cast("Scanner", self._guard(name, _SCANNER_KIND))

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
            The device-layer spectrum-detector protocol.
        """
        name = self._sole(_SPECTRUM_KIND, target)
        return typing.cast("SpectrumDetector", self._guard(name, _SPECTRUM_KIND))

    @property
    def instrument(self) -> InstrumentController:
        """
        The leased instrument controls.

        Returns
        -------
        InstrumentController
            The device-layer controller protocol.
        """
        guarded = self._guard(INSTRUMENT_TARGET, INSTRUMENT_TARGET)
        return typing.cast("InstrumentController", guarded)

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
        return self.broker.renew(self.lease.lease_id, ttl_s)


class LocalBroker:
    """
    Arbitrate one instrument's targets between clients, in this process.

    Parameters
    ----------
    targets : Mapping[str, object]
        The device handles, keyed by target name - the same names the
        device server serves, so ``instrument``, ``scanner``,
        ``eels_camera`` and so on. Their kinds come from
        :func:`~miainwoodpecker.devices.rpc.target_kind`, which means an
        out-of-tree adapter's target is arbitrated like any other rather
        than being refused for being unfamiliar.
    holder : str
        The name recorded against leases granted through this object.
        In a remote broker this is per-connection and filled in from the
        connection; in process there is one caller, so it is one string.
    clock : Callable[[], float]
        Monotonic seconds, injectable so that lease expiry is a test
        assertion rather than a sleep.
    """

    def __init__(
        self,
        targets: Mapping[str, object],
        *,
        holder: str = "local",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._targets = dict(targets)
        self._holder = holder
        self._clock = clock
        self._lock = threading.RLock()
        self._loops: dict[str, LiveAcquisition] = {}
        self._specs: dict[str, _LiveSpec] = {}
        self._leases: dict[str, Lease] = {}
        self._claims: dict[str, str] = {}
        self._controls: dict[str, float | bool] = {}
        # Last, and while nothing is running: see _describe_all.
        self._descriptions = self._describe_all()

    # -- watching ---------------------------------------------------------

    def device(self, target: str) -> object:
        """
        Return the raw device handle for a target.

        Not part of the broker protocol: this exists for the lease's own
        wrapper, and for a remote server that has to dispatch a call
        onto the handle. A client reaching for it has bypassed the
        arbitration this class exists to provide.

        Parameters
        ----------
        target : str
            The target name.

        Returns
        -------
        object
            The device handle.

        Raises
        ------
        KeyError
            If this instrument serves no such target.
        """
        if target not in self._targets:
            message = f"no such target: {target}"
            raise KeyError(message)
        return self._targets[target]

    def targets(self) -> Mapping[str, TargetState]:
        """
        Return every target this instrument serves, and its state.

        Returns
        -------
        Mapping[str, TargetState]
            Keyed by target name.
        """
        self._reclaim_expired()
        with self._lock:
            return {name: self._state(name) for name in self._targets}

    def describe(self) -> Mapping[str, TargetDescription]:
        """
        Return what each target is, from the cache built at construction.

        Returns
        -------
        Mapping[str, TargetDescription]
            Keyed by target name.
        """
        return dict(self._descriptions)

    def _describe_all(self) -> dict[str, TargetDescription]:
        """
        Read every target's static facts, once, before anything runs.

        Called from ``__init__`` deliberately. These are device calls,
        and this is the only moment at which making them is unambiguously
        safe: no live loop exists yet, so there is no second caller to
        interleave with. Reading them lazily on first use would put a
        call on a device whose loop is mid-pass.

        A device that does not answer a given question simply does not
        have it - a detector-only server has no channel names, an
        unsynchronised scan unit no targets - so each read is guarded
        rather than required. A device that *raises* is treated the same
        way: a description is chrome, and failing to build a window
        because a camera would not list its binning factors would be a
        poor trade.

        Returns
        -------
        dict[str, TargetDescription]
            Keyed by target name.
        """
        described: dict[str, TargetDescription] = {}
        for name, device in self._targets.items():
            refused: list[str] = []
            described[name] = TargetDescription(
                name=name,
                kind=target_kind(name),
                label=str(
                    self._ask(device, "camera_id", refused)
                    or self._ask(device, "scanner_id", refused)
                    or self._ask(device, "detector_id", refused)
                    or name,
                ),
                channel_names=tuple(
                    self._ask(device, "channel_names", refused) or (),
                ),
                binning_values=tuple(
                    self._ask(device, "binning_values", refused) or (),
                ),
                synchronised_targets=tuple(
                    self._ask(device, "synchronised_targets", refused) or (),
                ),
                controls=tuple(
                    self._ask(device, "available_controls", refused) or (),
                ),
                error=(
                    f"{name} would not answer: {', '.join(refused)}"
                    if refused
                    else None
                ),
            )
        return described

    @staticmethod
    def _ask(device: object, attribute: str, refused: list[str]) -> object:
        """
        Read one descriptive attribute, or None if the device lacks it.

        Properties are read and methods are called, the same distinction
        :func:`~miainwoodpecker.devices.serving.invoke` makes and for the
        same reason: ``channel_names`` is a property and
        ``available_controls`` is a method, and invoking the first would
        call a tuple.

        Parameters
        ----------
        device : object
            The device handle.
        attribute : str
            What to read.
        refused : list[str]
            Appended to when the device *raises*, so the description can
            say it is incomplete. A device that simply lacks the
            attribute is not recorded here: "this is not a camera" is an
            answer, and only "this camera would not say" is a gap.

        Returns
        -------
        object
            The value, or None if absent or unreadable.
        """
        try:
            value = getattr(device, attribute, None)
            return value() if callable(value) else value
        except Exception as error:  # noqa: BLE001 - see the docstring
            # Deliberately every exception. A description is chrome, and
            # what a device raises when asked a question it half-supports
            # is not something this can enumerate - a vendor adapter is
            # free to raise anything at all. Losing a window because a
            # camera would not list its binning factors is the worse
            # outcome by a distance.
            _LOGGER.warning("could not read %s from a device: %s", attribute, error)
            refused.append(attribute)
            return None

    def snapshot(self) -> Mapping[str, TargetView]:
        """
        Return every target's state and latest frames, in one pass.

        One sweep of expiry, one acquisition of this broker's lock, and
        one of each loop's - against the several that asking
        :meth:`targets` and then :meth:`latest_frames` per source costs.
        The difference is not academic on a fast source: the loop's lock
        is retaken by its worker on every grab, so each extra
        acquisition here is another queue behind a thread that never
        stops asking.

        Returns
        -------
        Mapping[str, TargetView]
            Keyed by target name.
        """
        self._reclaim_expired()
        with self._lock:
            return {
                name: TargetView(
                    state=self._state(name),
                    frames=self._frames_of(self._loops.get(name)),
                )
                for name in self._targets
            }

    @staticmethod
    def _frames_of(loop: LiveAcquisition | None) -> tuple[Frame, ...]:
        """
        Return a loop's latest pass, whether or not it is still running.

        Parameters
        ----------
        loop : LiveAcquisition | None
            The loop, or None if the target never had one.

        Returns
        -------
        tuple[Frame, ...]
            The pass's frames, empty before the first one arrives.
        """
        if loop is None:
            return ()
        if isinstance(loop, MultiChannelLiveAcquisition):
            return loop.latest_frames()
        frame = loop.latest()
        return () if frame is None else (frame,)

    def controls(self) -> Mapping[str, float | bool]:
        """
        Return the instrument controls' current values, read only.

        Refreshed from the instrument only while nothing holds a lease
        on it. That is not an optimisation: the device protocol is one
        request at a time, so reading the defocus out from under a lease
        holder mid-sweep is the interleaving the whole class exists to
        prevent. While the instrument is leased, the last values stand.

        Returns
        -------
        Mapping[str, float | bool]
            Keyed by the ``*_CONTROL`` names this instrument implements.
            Empty if it has no controller target.
        """
        self._reclaim_expired()
        with self._lock:
            leased = INSTRUMENT_TARGET in self._claims
            instrument = self._targets.get(INSTRUMENT_TARGET)
        if instrument is not None and not leased:
            self._refresh_controls(typing.cast("InstrumentController", instrument))
        with self._lock:
            return dict(self._controls)

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
            The latest frame, or None if none has ever arrived. A
            *stopped* loop still answers with its last one: stopping the
            scan is not a reason to blank the screen.
        """
        loop = self._loop_or_none(target)
        return None if loop is None else loop.latest()

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
        return self._frames_of(self._loop_or_none(target))

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

        Raises
        ------
        NotLiveError
            If no live loop is running on the target.
        """
        loop = self._loop_or_none(target, running=True)
        if loop is None:
            raise NotLiveError(target)
        return loop.stats

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

        A target another client has leased raises
        :class:`~miainwoodpecker.broker.interface.DeviceBusyError`.

        Parameters
        ----------
        target : str
            The target to start.
        parameters : ScanParameters | None
            Scan geometry and dwell, for a scanner.
        channels : Sequence[int]
            Which detectors to read out per pass, for a scanner.
        """
        self._reclaim_expired()
        with self._lock:
            device = self.device(target)
            self._refuse_if_leased(target)
            existing = self._loops.get(target)
            if existing is not None and existing.is_running:
                return
            spec = _LiveSpec(parameters=parameters, channels=tuple(channels))
            self._specs[target] = spec
            self._start_locked(target, device, spec)

    def reconfigure_live(
        self,
        target: str,
        parameters: ScanParameters,
        *,
        channels: Sequence[int] = (0,),
    ) -> None:
        """
        Change a running scan's geometry without stopping it.

        The alternative is stop-and-restart, and on a scan unit that is
        not a neutral pair of operations: between the stop and the start
        the probe stands still on one spot. An operator dragging a field
        of view or ticking a detector on would pay that every time.

        Takes effect on the next pass, because
        :meth:`_scan_pass` reads the spec per grab. The pass already in
        flight finishes under the settings it started with, which is the
        only honest answer - half a frame at one dwell and half at
        another is not a frame of either.

        Also valid before the loop is running, in which case it is just
        the settings the next :meth:`start_live` will use.

        A target another client has leased raises
        :class:`~miainwoodpecker.broker.interface.DeviceBusyError`.

        Parameters
        ----------
        target : str
            The scan unit to reconfigure.
        parameters : ScanParameters
            The geometry and dwell to use from the next pass on.
        channels : Sequence[int]
            Which detectors to read out per pass.

        Raises
        ------
        ValueError
            If the target is not a scan unit. A camera's live settings
            are exposure and binning, which are
            :meth:`~miainwoodpecker.devices.interface.Camera.configure`
            under a lease rather than a property of the loop.
        """
        self._reclaim_expired()
        with self._lock:
            self.device(target)
            self._refuse_if_leased(target)
            if target_kind(target) != _SCANNER_KIND:
                message = f"{target} is not a scan unit; there is no geometry to change"
                raise ValueError(message)
            self._specs[target] = _LiveSpec(
                parameters=parameters,
                channels=tuple(channels),
            )

    def stop_live(self, target: str) -> bool:
        """
        Stop a target's live loop.

        A target another client has leased raises
        :class:`~miainwoodpecker.broker.interface.DeviceBusyError`.

        Parameters
        ----------
        target : str
            The target to stop.

        Returns
        -------
        bool
            True if the worker finished, False if a pass or exposure is
            still in flight - in which case the loop is left running.
        """
        self._reclaim_expired()
        with self._lock:
            self.device(target)
            self._refuse_if_leased(target)
        return self._stop(target, DEFAULT_LEASE_TIMEOUT_S)

    # -- leasing ----------------------------------------------------------

    @contextlib.contextmanager
    def lease(
        self,
        *targets: str,
        reason: str = "",
        timeout_s: float = DEFAULT_LEASE_TIMEOUT_S,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
    ) -> Iterator[_LeasedDevices]:
        """
        Take exclusive control of one or more targets for a block.

        Parameters
        ----------
        *targets : str
            The targets to hold.
        reason : str
            What the lease is for, shown to other clients.
        timeout_s : float
            Seconds to wait for each target's worker to join.
        ttl_s : float
            Seconds the lease survives without renewal.

        Yields
        ------
        _LeasedDevices
            The leased devices, as the device-layer protocols.
        """
        granted = self.grant(targets, reason=reason, timeout_s=timeout_s, ttl_s=ttl_s)
        try:
            yield _LeasedDevices(broker=self, lease=granted)
        finally:
            self.release(granted.lease_id)

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

        Raises
        ------
        LeaseExpiredError
            If it has expired or already been released.
        """
        self._reclaim_expired()
        with self._lock:
            lease = self._leases.get(lease_id)
        if lease is None:
            raise LeaseExpiredError(lease_id)
        return lease

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
        lease = self.check_lease(lease_id)
        with self._lock:
            renewed = replace(lease, expires_at=self._clock() + ttl_s)
            self._leases[lease_id] = renewed
        return renewed

    def close(self) -> None:
        """
        Stop every live loop and park the instrument.

        The teardown the device layer already promises: whatever else
        happens, the instrument does not stay in an unattended state
        nobody chose. Parking blanks the beam where one exists, which is
        why stopping the loops here is safe when stopping them anywhere
        else would not be.

        A park that *fails* is logged and not re-raised, which is the
        one place this module swallows anything. The case is real and
        was measured rather than imagined: a console interrupt on
        Windows reaches every process in the group, so the device server
        can already be gone by the time this runs, and ``park`` then
        raises about a lost connection. Re-raising replaces a clean
        shutdown line with a traceback about a server that is beyond
        reach either way - there is no action left for a caller to take,
        and burying the reason for the shutdown is a real cost. It is
        logged at error level for the same reason it is not raised: an
        instrument that could not be parked is worth knowing about.
        """
        for target in list(self._loops):
            self._stop(target, DEFAULT_LEASE_TIMEOUT_S)
        instrument = self._targets.get(INSTRUMENT_TARGET)
        if instrument is None:
            return
        try:
            typing.cast("InstrumentController", instrument).park()
        except Exception:
            _LOGGER.exception("could not park the instrument on shutdown")

    # -- internals --------------------------------------------------------

    def _state(self, name: str) -> TargetState:
        """
        Return one target's state, with the broker lock already held.

        Parameters
        ----------
        name : str
            The target name.

        Returns
        -------
        TargetState
            Its current state.
        """
        loop = self._loops.get(name)
        running = loop is not None and loop.is_running
        error = None if loop is None or loop.error is None else str(loop.error)
        lease_id = self._claims.get(name)
        return TargetState(
            name=name,
            kind=target_kind(name),
            is_live=running,
            stats=loop.stats if running and loop is not None else None,
            lease=self._leases.get(lease_id) if lease_id else None,
            error=error,
        )

    def _loop_or_none(self, target: str, *, running: bool = False) -> (
        LiveAcquisition | None
    ):
        """
        Return a target's loop, or None if it never had one.

        **A stopped loop still answers with its last frame.** Watching is
        for putting a picture on a screen, and stopping the scan is not a
        reason to blank the screen - the last image is the most recent
        thing that was true, and an operator who stops to look at it is
        the ordinary case. It is also what makes a lease invisible to a
        watcher: the display keeps the pre-lease frame while an
        acquisition runs, rather than flickering to nothing and back.

        Whether the picture is still *advancing* is
        :attr:`~miainwoodpecker.broker.interface.TargetState.is_live`'s
        job, and asking a stopped loop for a rate is
        :class:`~miainwoodpecker.broker.interface.NotLiveError`'s - which
        is what ``running`` is for.

        Parameters
        ----------
        target : str
            The target name.
        running : bool
            Require the loop to be running, for the callers that are
            asking about progress rather than about pixels.

        Returns
        -------
        LiveAcquisition | None
            The loop, or None.
        """
        self._reclaim_expired()
        with self._lock:
            self.device(target)
            loop = self._loops.get(target)
        if loop is None or (running and not loop.is_running):
            return None
        return loop

    def _refuse_if_leased(self, target: str) -> None:
        """
        Raise if somebody holds a lease on a target.

        Parameters
        ----------
        target : str
            The target name.

        Raises
        ------
        DeviceBusyError
            If a lease is held.
        """
        lease_id = self._claims.get(target)
        if lease_id is None:
            return
        lease = self._leases[lease_id]
        raise DeviceBusyError(target, holder=lease.holder, reason=lease.reason)

    def _refresh_controls(self, instrument: InstrumentController) -> None:
        """
        Read the instrument's controls into the cache.

        Parameters
        ----------
        instrument : InstrumentController
            The controller to read.
        """
        available = set(instrument.available_controls())
        values: dict[str, float | bool] = {}
        if DEFOCUS_CONTROL in available:
            values[DEFOCUS_CONTROL] = instrument.defocus_nm()
        if ENERGY_OFFSET_CONTROL in available:
            values[ENERGY_OFFSET_CONTROL] = instrument.energy_offset_ev()
        if BEAM_BLANKER_CONTROL in available:
            values[BEAM_BLANKER_CONTROL] = instrument.is_beam_blanked()
        if STAGE_POSITION_CONTROL in available:
            y_nm, x_nm = instrument.stage_position_nm()
            values["stage_position_y_nm"] = y_nm
            values["stage_position_x_nm"] = x_nm
        with self._lock:
            self._controls = values

    def _start_locked(self, target: str, device: object, spec: _LiveSpec) -> None:
        """
        Build and start a loop for a target, with the lock held.

        Parameters
        ----------
        target : str
            The target name.
        device : object
            Its device handle.
        spec : _LiveSpec
            How the loop should run.

        Raises
        ------
        ValueError
            If a scanner was asked to run live with no scan geometry.
            Defaulting one would put a field of view and a dwell of this
            module's choosing onto the specimen.
        """
        kind = target_kind(target)
        if kind == _SCANNER_KIND:
            scanner = typing.cast("Scanner", device)
            if spec.parameters is None:
                message = f"{target} is a scanner; start_live needs parameters"
                raise ValueError(message)
            loop: LiveAcquisition = MultiChannelLiveAcquisition(
                lambda: self._scan_pass(scanner, target),
            )
        else:
            camera = typing.cast("Camera", device)
            camera.start()
            loop = LiveAcquisition(camera.acquire_frame)
        self._loops[target] = loop
        loop.start()

    def _scan_pass(self, scanner: Scanner, target: str) -> Sequence[Frame]:
        """
        Grab one pass, reading the geometry that is current *now*.

        Runs on the loop's worker thread. The spec is read per grab
        rather than captured when the loop was built, which is what lets
        :meth:`reconfigure_live` change the field of view or the enabled
        detectors without stopping the scan - and a scan stopped to be
        re-parameterised is a probe parked on one spot for as long as
        that takes.

        Safe without the broker's lock, and deliberately so: this is one
        dict lookup returning a frozen value, and holding the lock here
        would put the whole of a scan pass inside it. The worker sees
        either the old spec or the new one, never a half-written mixture,
        because a :class:`_LiveSpec` is replaced whole.

        The arity dispatch is per grab for the same reason. A
        single-channel pass goes through ``scan_frame``, which carries
        neither ``scan_pass_id`` nor ``simultaneous_channels`` - a lone
        frame shared its pass with nothing, and an id claiming otherwise
        would be a fiction in every recording.

        Parameters
        ----------
        scanner : Scanner
            The scan unit.
        target : str
            Its target name, for looking the current spec up.

        Returns
        -------
        Sequence[Frame]
            The pass's frames, in channel-request order.
        """
        spec = self._specs[target]
        parameters = typing.cast("ScanParameters", spec.parameters)
        if len(spec.channels) > 1:
            return scanner.scan_frames(parameters, list(spec.channels))
        return [scanner.scan_frame(parameters, spec.channels[0])]

    def _join_deadline(self, target: str, timeout_s: float) -> float:
        """
        Return how long to wait for a target's worker, given what it is doing.

        A fixed timeout is a guess about the instrument, and the guess
        gets worse the bigger the instrument is. Stopping a loop means
        waiting for the pass already in flight, and a pass is
        ``height x width x dwell``: 262 ms for 512x512 at 1 microsecond,
        but **42 s** for 2048x2048 at 10 microseconds and nearly three
        minutes at 4096x4096. Against a five-second default the second
        and third are simply "still finishing a scan - try again",
        forever, on exactly the instruments this project exists for.

        So the deadline is derived rather than assumed. The scan spec is
        already here, and it says how long a pass takes; the caller's
        ``timeout_s`` becomes a *floor* under that rather than a ceiling
        over it, because a caller cannot know the geometry a loop was
        started with and should not have to.

        A camera has no equivalent here yet - its exposure lives on the
        device, and asking for it would put a call on a connection whose
        loop is mid-exposure. Cameras therefore still get the floor, and
        a long-exposure detector is the case that will want this next.

        Parameters
        ----------
        target : str
            The target whose worker is being joined.
        timeout_s : float
            The caller's floor, in seconds.

        Returns
        -------
        float
            Seconds to wait.
        """
        spec = self._specs.get(target)
        if spec is None or spec.parameters is None:
            return timeout_s
        parameters = spec.parameters
        pass_s = parameters.height * parameters.width * parameters.pixel_time_us / 1e6
        # Flyback, readout and scheduling are all real and none of them
        # are in the dwell arithmetic, so the pass is a lower bound
        # rather than an estimate. Doubling it keeps this a bound on
        # *waiting*, which costs nothing when the worker returns early.
        return max(timeout_s, pass_s * _PASS_SLACK)

    def _stop(self, target: str, timeout_s: float) -> bool:
        """
        Stop a target's loop and pause its device, outside the lock.

        Parameters
        ----------
        target : str
            The target name.
        timeout_s : float
            Seconds to wait for the worker to join, as a floor - see
            :meth:`_join_deadline` for what is actually waited.

        Returns
        -------
        bool
            True if the worker finished.
        """
        timeout_s = self._join_deadline(target, timeout_s)
        with self._lock:
            loop = self._loops.get(target)
        if loop is None or not loop.is_running:
            return True
        if not loop.stop(timeout_s):
            return False
        if target_kind(target) == _CAMERA_KIND:
            typing.cast("Camera", self.device(target)).stop()
        return True

    def grant(
        self,
        targets: Sequence[str],
        *,
        reason: str = "",
        timeout_s: float = DEFAULT_LEASE_TIMEOUT_S,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
        holder: str | None = None,
    ) -> Lease:
        """
        Claim targets, stop their loops, and return the granted lease.

        The transport's half of :meth:`lease`. A ``with`` block cannot
        cross a socket, so a remote client sends this, then
        :meth:`release`, and runs its own context manager around the
        pair; in process, :meth:`lease` does exactly that. Prefer
        :meth:`lease` wherever a block will do - it is the version that
        cannot leak a lease by forgetting the second call.

        Claims are taken together under the lock so that two clients
        cannot each get half a lease. The joins that follow happen
        outside it, because one of them can take as long as an exposure
        and a watcher must not block on that.

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
        holder : str | None
            Who to record as holding it, or None for this broker's own
            identity. A server passes the *connection's* identity here,
            which is why it is a parameter rather than read off the
            client's request: a client that names itself can name itself
            anything.

        Returns
        -------
        Lease
            The granted lease.

        Raises
        ------
        DeviceBusyError
            If any target is held, or its worker did not join. Nothing
            is left stopped: loops already stopped for this attempt are
            restarted before the error is raised.
        """
        self._reclaim_expired()
        ordered = lease_order(targets)
        lease_id = uuid.uuid4().hex
        with self._lock:
            for name in ordered:
                self.device(name)
                self._refuse_if_leased(name)
            for name in ordered:
                self._claims[name] = lease_id

        stopped: list[str] = []
        for name in ordered:
            with self._lock:
                loop = self._loops.get(name)
                was_running = loop is not None and loop.is_running
            if not was_running:
                continue
            if not self._stop(name, timeout_s):
                self._restart(reversed(stopped))
                with self._lock:
                    for held in ordered:
                        self._claims.pop(held, None)
                raise DeviceBusyError(name)
            stopped.append(name)

        now = self._clock()
        lease = Lease(
            lease_id=lease_id,
            targets=ordered,
            holder=holder if holder is not None else self._holder,
            reason=reason,
            granted_at=now,
            expires_at=now + ttl_s,
            restarts=tuple(stopped),
        )
        with self._lock:
            self._leases[lease_id] = lease
        return lease

    def release(self, lease_id: str) -> None:
        """
        Drop a lease's claims and restart the loops it paused.

        Restarting is unconditional for the targets that were running at
        grant time, and the scanner comes back first because release
        walks :data:`~miainwoodpecker.broker.interface.LEASE_ORDER`
        backwards. A scan that is not scanning is a stationary probe, so
        the shortest possible parked interval is the whole point.

        Parameters
        ----------
        lease_id : str
            The lease to release. Already-released is not an error -
            expiry and block exit both arrive here.
        """
        with self._lock:
            lease = self._leases.pop(lease_id, None)
            if lease is None:
                return
            for name in lease.targets:
                if self._claims.get(name) == lease_id:
                    del self._claims[name]
        self._restart(reversed(lease.restarts))

    def _restart(self, targets: Iterable[str]) -> None:
        """
        Start loops again for targets that were running before a lease.

        Parameters
        ----------
        targets : Iterable[str]
            Target names, already in release order.
        """
        for name in targets:
            with self._lock:
                spec = self._specs.get(name)
                if spec is None:
                    continue
                try:
                    self._start_locked(name, self.device(name), spec)
                except RuntimeError:
                    # Restarting means starting a thread, and there is
                    # one moment when that is impossible however correct
                    # the request is: interpreter shutdown, which raises
                    # PythonFinalizationError from Thread.start. It is
                    # reached whenever a lease is released by a
                    # generator being finalised rather than closed - an
                    # abandoned recording, a process on its way out.
                    # Raising there turns a tidy exit into a traceback
                    # from a generator nobody is watching, and there is
                    # no live loop to want on an interpreter that is
                    # ending anyway.
                    _LOGGER.warning("could not restart the live loop on %s", name)

    def _reclaim_expired(self) -> None:
        """
        Release every lease whose deadline has passed.

        Swept here rather than by a timer thread: a lease outlives the
        process that took it only when that process died, and the next
        caller is the first moment anyone can observe the difference.
        """
        now = self._clock()
        with self._lock:
            expired = [
                lease_id
                for lease_id, lease in self._leases.items()
                if lease.expires_at <= now
            ]
        for lease_id in expired:
            self.release(lease_id)
