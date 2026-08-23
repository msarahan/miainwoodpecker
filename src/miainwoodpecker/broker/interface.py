"""
Arbitration between the several clients of one instrument.

Everything in :mod:`miainwoodpecker.devices` assumes a single driver. The
RPC protocol is strictly one request at a time, and
:mod:`miainwoodpecker.devices.shared_frame` reuses one segment per source
*because* of that: the server cannot publish frame N+1 until the client
has copied frame N out. Two clients on one device therefore do not merely
contend - they interleave on a reused buffer and produce a frame that is
half pass N and half pass N+1, with no exception raised anywhere
(docs/architecture-review.md, section 1.2).

Today that rule is upheld by there being one application. The Qt viewer
owns the connection, owns every
:class:`~miainwoodpecker.acquisition.live.LiveAcquisition`, and stops the
loop before it acquires - ``stop_camera`` returning False is that rule
surfacing as "still finishing an exposure - try again". A notebook, a
browser dashboard, a second screen or an agent is a *second* client, and
there is nowhere for the rule to live.

This module is where it lives. One process holds the device connection
and every live loop; every other participant is a client of it, and gets
exactly two verbs.

**Watch** - :meth:`InstrumentBroker.latest` and its neighbours. Reads a
frame the broker already has. Costs no device call, cannot start or stop
anything, and is what a dashboard tile is made of. A caller asking what
is on screen must not be able to move the probe by asking.

**Lease** - :meth:`InstrumentBroker.lease`. Exclusive control of one or
more targets for the duration of a ``with`` block, which is the only way
to acquire.

A lease yields the *same*
:class:`~miainwoodpecker.devices.interface.Camera`,
:class:`~miainwoodpecker.devices.interface.Scanner` and
:class:`~miainwoodpecker.devices.interface.InstrumentController`
protocol objects the device layer already defines, so every generator in
:mod:`miainwoodpecker.acquisition`, every
:class:`~miainwoodpecker.storage.session.Session` recording and every
analysis bridge works inside one unchanged. The broker decides *who* may
call, never *what* they may call - the moment it grows acquisition verbs
of its own there are two acquisition APIs to keep in step, and the
property that the viewer is built *on* the scripting API rather than
beside it is gone.

Two behaviours here diverge from Digital Micrograph, and both follow from
one fact.

**A paused live loop is restarted on release, always.** A stopped scan is
not a safe idle state: the beam is on regardless - that is a separate
control, outside this software and outside DM - so a scan that is not
scanning is a stationary probe putting the whole dose into one spot.
Scanning spreads it. Restarting is therefore the conservative choice
rather than the convenient one, and there is no opt-out flag for the same
reason there is no flag to skip
:meth:`~miainwoodpecker.devices.interface.Instrument.park`. A caller who
wants the beam off wants
:meth:`~miainwoodpecker.devices.interface.InstrumentController.set_beam_blanked`,
which is a control, not a side effect of stopping a display.

**And it fixes the order of a multi-target lease.** A synchronised
acquisition needs the scanner and a camera together; taking them in
argument order lets two clients asking in opposite orders deadlock. The
order is :data:`LEASE_ORDER`, and the scanner is *last* - so the probe
stands parked only for the time it takes to grant the lease, not for the
time it takes to negotiate every other target. Release runs in reverse,
so the scan is the first thing back.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass

from miainwoodpecker.devices.rpc import target_kind

if typing.TYPE_CHECKING:
    import contextlib
    from collections.abc import Mapping, Sequence

    from miainwoodpecker.acquisition.live import LiveStats
    from miainwoodpecker.devices.interface import (
        Camera,
        Frame,
        InstrumentController,
        ScanParameters,
        Scanner,
        SpectrumDetector,
    )


DEFAULT_LEASE_TIMEOUT_S = 5.0
"""
Floor on how long to wait for a target's live loop to stop.

A **floor**, not a ceiling. What a lease waits for is the pass or
exposure already in flight, and on a scan unit that is
``height x width x dwell`` - 262 ms for 512x512 at 1 microsecond, but 42
seconds for 2048x2048 at 10 microseconds, and nearly three minutes at
4096x4096. A fixed five seconds would refuse every lease on exactly the
instruments this project exists for, forever, with "still finishing a
scan - try again".

So a broker raises this to the pass it can see the loop running (see
``LocalBroker._join_deadline``), and this value is what stands when
there is no geometry to derive one from - a camera, or a loop that is
not running. It is short enough that a wedged device is reported busy
rather than hanging its caller.

The consequence is worth stating where a caller will read it: **taking a
lease can block for as long as a scan pass**, so it does not belong on a
thread that must stay responsive. A GUI takes its leases the way it
records - on a worker, reporting progress - rather than inside a click
handler.
"""

DEFAULT_LEASE_TTL_S = 300.0
"""
Seconds a granted lease survives without being renewed.

Not a nicety. A notebook kernel that dies mid-lease holds the beam
forever otherwise, and the process that would have released it is gone.
On expiry the broker releases the lease exactly as if the block had
exited - restarting the live loops it paused - and any later call through
the dead lease raises :class:`LeaseExpiredError` rather than driving a
device it no longer owns.
"""

LEASE_ORDER = ("instrument", "spectrum", "camera", "device", "scanner")
"""
The order targets are acquired in, by kind.

Kinds are :func:`~miainwoodpecker.devices.rpc.target_kind`'s. Any total
order prevents the deadlock; this particular one also minimises how long
the probe stands still. The scanner is acquired last and released first,
so a lease on the scanner plus a camera parks the probe only for the
grant itself. Targets of one kind are ordered by name.
"""


class BrokerError(RuntimeError):
    """
    Base for every refusal the broker issues.

    Every subclass's message **begins with the subject it is about** - a
    target name, or a lease id. That is a convention with a job: a
    refusal crossing the
    :mod:`~miainwoodpecker.devices.rpc` wire arrives as a type name and a
    string, and the client rebuilds the exception from those two. The
    leading word is the only field it can recover. Anything else a
    caller needs about a refusal - who holds the lease, what for - is on
    :attr:`TargetState.lease`, which crosses as data rather than as
    prose.
    """


class DeviceBusyError(BrokerError):
    """
    Raised when a lease cannot be granted before its deadline.

    Two causes, and the message says which, because the caller's response
    differs: another client holds a lease (wait, or go and ask them), or
    the target's own worker would not join within the timeout - the same
    condition
    :meth:`~miainwoodpecker.acquisition.live.LiveAcquisition.stop`
    reports by returning False, meaning an exposure is still in flight
    and the device is genuinely still in use.

    Nothing has been left stopped when this is raised. A lease is granted
    whole or not at all: leaving the scan dark because the camera could
    not be had would park the probe in exchange for nothing.

    Parameters
    ----------
    target : str
        The target that could not be acquired.
    holder : str | None
        The client holding a conflicting lease, if that is the cause;
        None when the cause is a worker that would not join.
    reason : str
        The conflicting lease's stated reason, if it gave one.
    message : str | None
        The message verbatim, instead of one built from the fields
        above. For a client rebuilding this refusal from the wire, where
        the message survived and the fields did not.
    """

    def __init__(
        self,
        target: str,
        *,
        holder: str | None = None,
        reason: str = "",
        message: str | None = None,
    ) -> None:
        if message is None:
            if holder is None:
                message = f"{target} is still finishing an exposure - try again"
            else:
                held_for = f" ({reason})" if reason else ""
                message = f"{target} is leased by {holder}{held_for}"
        super().__init__(message)
        self.target = target
        self.holder = holder
        self.reason = reason


class LeaseExpiredError(BrokerError):
    """
    Raised when a call is made through a lease the broker has released.

    The lease's time to live elapsed without renewal, so the broker
    reclaimed the targets and restarted the live loops. The devices this
    lease refers to may already be driven by somebody else, so the call
    is refused rather than attempted.

    Parameters
    ----------
    lease_id : str
        The lease that is no longer valid.
    message : str | None
        The message verbatim, for a client rebuilding this refusal from
        the wire.
    """

    def __init__(self, lease_id: str, *, message: str | None = None) -> None:
        super().__init__(message or f"{lease_id} expired and was released")
        self.lease_id = lease_id


class NotLiveError(BrokerError):
    """
    Raised when a live-loop reading is asked of a target with no loop.

    Deliberately an error rather than a zeroed
    :class:`~miainwoodpecker.acquisition.live.LiveStats`, which would
    read as "running at 0 fps" - what a stalled loop looks like, and the
    two must not be confusable. :meth:`InstrumentBroker.latest` keeps
    returning None instead, because "no frame yet" is an ordinary state a
    dashboard tile renders every time one starts.

    Parameters
    ----------
    target : str
        The target that has no live loop running.
    message : str | None
        The message verbatim, for a client rebuilding this refusal from
        the wire.
    """

    def __init__(self, target: str, *, message: str | None = None) -> None:
        super().__init__(message or f"{target} has no live loop running")
        self.target = target


@dataclass(frozen=True)
class Lease:
    """
    A granted claim on one or more targets.

    Attributes
    ----------
    lease_id : str
        Identity of this lease, carried on every device call made
        through it so the broker can refuse one that has expired.
    targets : tuple[str, ...]
        The targets held, in :data:`LEASE_ORDER`.
    holder : str
        Who holds it. Filled in by the broker from the connection rather
        than supplied by the client: a client that names itself can name
        itself anything, and this string is what an operator reads when
        deciding whether to wait or to go and ask.
    reason : str
        What it was taken for, as the client stated it. Free text, shown
        in the status line and written into the session log, so that
        "energy series, 5 steps" appears rather than "busy".
    granted_at : float
        ``time.monotonic()`` at grant. Monotonic rather than wall clock
        because this is only ever used for durations, and a wall clock
        can step.
    expires_at : float
        ``time.monotonic()`` after which the broker reclaims the targets
        unless the lease is renewed.
    restarts : tuple[str, ...]
        The subset of :attr:`targets` whose live loop was running at
        grant time, and which will be restarted on release. Recorded at
        grant because by release time the answer is unknowable - the
        loops are stopped.
    """

    lease_id: str
    targets: tuple[str, ...]
    holder: str
    reason: str
    granted_at: float
    expires_at: float
    restarts: tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetState:
    """
    What one target is doing, as a watching client sees it.

    The whole of what a dashboard needs to render a tile's chrome without
    touching the device: whether a picture is coming, how fast, and
    whether somebody else is driving.

    Attributes
    ----------
    name : str
        The target name, as the device server serves it.
    kind : str
        :func:`~miainwoodpecker.devices.rpc.target_kind` of the name.
    is_live : bool
        Whether a live loop is running on this target.
    stats : LiveStats | None
        The loop's frame count and recent rate, or None when it is not
        running. None rather than zeros, for the reason
        :class:`NotLiveError` gives.
    lease : Lease | None
        The lease held on this target, if any. A client compares
        :attr:`Lease.holder` against its own identity to tell "leased by
        me" from "leased by someone else" - the two look identical from
        outside and mean opposite things.
    error : str | None
        The exception that stopped the live loop, if one did. A loop that
        died leaves ``is_live`` False and this set, which is how a tile
        shows "stopped: camera timed out" instead of going quietly blank.
    """

    name: str
    kind: str
    is_live: bool
    stats: LiveStats | None = None
    lease: Lease | None = None
    error: str | None = None


@dataclass(frozen=True)
class TargetView:
    """
    One target's state *and* its latest frames, read together.

    Exists because a display tick is one question, not seven. Asking
    :meth:`InstrumentBroker.targets` and then
    :meth:`InstrumentBroker.latest` per source re-enters the broker once
    per call and re-takes each loop's own lock once per call - and that
    lock is being reacquired by the acquisition worker on every single
    grab. A viewer polling at 30 Hz against a fast source spends its
    time queueing behind the thread it is trying to watch, which is a
    contention problem rather than a correctness one and therefore shows
    up as "why is everything slow" rather than as a failure.

    Attributes
    ----------
    state : TargetState
        What the target is doing.
    frames : tuple[Frame, ...]
        The latest pass's frames, empty if none has arrived. Read under
        the same lock as :attr:`state`, so a tick cannot show a rate
        from one pass beside pixels from another.
    """

    state: TargetState
    frames: tuple[Frame, ...] = ()


@typing.runtime_checkable
class LeasedDevices(typing.Protocol):
    """
    The devices held by one lease, as the device-layer protocols.

    The accessors return the ordinary
    :class:`~miainwoodpecker.devices.interface.Camera`,
    :class:`~miainwoodpecker.devices.interface.Scanner` and
    :class:`~miainwoodpecker.devices.interface.SpectrumDetector`
    protocols, so what a caller does with them is written against the
    device layer and not against this module - the point of the whole
    design. They are looked up by name rather than exposed as the named
    fields
    :class:`~miainwoodpecker.devices.remote.RemoteInstrumentDevices` has,
    because which targets a lease holds is chosen per call.

    Every call through these objects carries the lease id and raises
    :class:`LeaseExpiredError` if the broker has reclaimed it.
    """

    @property
    def lease(self) -> Lease:
        """The lease these devices are held under."""

    def camera(self, target: str | None = None) -> Camera:
        """
        Return a leased camera.

        A target this lease does not hold raises ``KeyError``, as does
        naming none when the lease holds no camera or several.

        Parameters
        ----------
        target : str | None
            Which camera, by target name. None means the only camera in
            the lease, and refuses if the lease holds more than one -
            guessing between a Ronchigram and an EELS camera is how a
            recording ends up labelled as the wrong detector.

        Returns
        -------
        Camera
            The device-layer camera protocol.
        """

    def scanner(self, target: str | None = None) -> Scanner:
        """
        Return the leased scan unit.

        A scanner this lease does not hold raises ``KeyError``.

        Parameters
        ----------
        target : str | None
            Which scanner, by target name. None means the only one.

        Returns
        -------
        Scanner
            The device-layer scanner protocol.
        """

    def spectrum_detector(self, target: str | None = None) -> SpectrumDetector:
        """
        Return a leased spectrum detector.

        A detector this lease does not hold raises ``KeyError``.

        Parameters
        ----------
        target : str | None
            Which detector, by target name. None means the only one.

        Returns
        -------
        SpectrumDetector
            The device-layer spectrum-detector protocol.
        """

    @property
    def instrument(self) -> InstrumentController:
        """
        The leased instrument controls.

        A lease that does not include the ``instrument`` target raises
        ``KeyError`` here. A parameter sweep needs it alongside the
        device it sweeps - ``focal_series`` moves the defocus and scans -
        so asking for a scanner alone and reaching for the instrument
        through it is the mistake this refuses.
        """

    def renew(self, ttl_s: float = DEFAULT_LEASE_TTL_S) -> Lease:
        """
        Extend the lease's deadline.

        Called by the client transport on a timer for the ordinary case,
        and explicitly by a caller about to do something slower than
        :data:`DEFAULT_LEASE_TTL_S` inside one device call.

        A lease the broker has already reclaimed raises
        :class:`LeaseExpiredError`: renewal is not revival, because the
        live loops have restarted and the targets may be held by
        somebody else.

        Parameters
        ----------
        ttl_s : float
            Seconds from now that the lease should expire.

        Returns
        -------
        Lease
            The lease, with its new deadline.
        """


@typing.runtime_checkable
class InstrumentBroker(typing.Protocol):
    """
    One instrument, arbitrated between every client of it.

    Implemented twice, deliberately: in process, holding the device
    handles directly, for the Qt viewer and for tests; and over the
    :mod:`miainwoodpecker.devices.rpc` wire for a notebook kernel, a
    browser dashboard or an agent. Code written against one works against
    the other, which is the promise
    :class:`~miainwoodpecker.devices.remote.RemoteInstrumentDevices`
    already makes about the in-process device handles.

    Reads of the instrument's controls are watch-side, through
    :meth:`controls` - a dashboard must be able to display the defocus
    without taking a lease, or every dashboard would hold one forever.
    *Writing* a control needs the ``instrument`` target leased like any
    other.
    """

    def targets(self) -> Mapping[str, TargetState]:
        """
        Return every target this instrument serves, and its state.

        Returns
        -------
        Mapping[str, TargetState]
            Keyed by target name, in the order the device server
            reported them.
        """

    def snapshot(self) -> Mapping[str, TargetView]:
        """
        Return every target's state and latest frames, in one pass.

        What a display tick should call, and the only watch method that
        exists for a performance reason rather than a semantic one: it
        answers in one entry to the broker what
        :meth:`targets` plus a :meth:`latest_frames` per source answers
        in many, against locks an acquisition worker is reacquiring on
        every grab.

        Frames travel with it, so a *remote* watcher that only needs
        chrome - is it live, who holds it - should keep asking
        :meth:`targets` and pull pixels for the one source it is showing.

        Returns
        -------
        Mapping[str, TargetView]
            Keyed by target name.
        """

    def controls(self) -> Mapping[str, float | bool]:
        """
        Return the instrument controls' current values, read only.

        Served from the broker's own polling rather than a device call
        per client, so a tile showing the defocus costs the instrument
        nothing however many dashboards are open.

        Returns
        -------
        Mapping[str, float | bool]
            Keyed by the names
            :meth:`~miainwoodpecker.devices.interface.InstrumentController.available_controls`
            reports. An absent key is a control this instrument does not
            have, not one that reads zero.
        """

    def latest(self, target: str) -> Frame | None:
        """
        Return the most recent frame from a target's live loop.

        The latest-frame-wins snapshot the Qt viewer's timer already
        polls, handed to any number of watchers at no cost to the device.
        It never starts a loop: a caller asking what is on screen must
        not be able to move the probe by asking.

        A name this instrument does not serve raises ``KeyError``.

        Parameters
        ----------
        target : str
            The target to read.

        Returns
        -------
        Frame | None
            The latest frame, or None if none has ever arrived. A
            *stopped* loop still answers with its last one - watching is
            for putting a picture on a screen, and stopping the scan is
            not a reason to blank it; whether the picture is still
            advancing is :attr:`TargetState.is_live`'s job. A
            multichannel scan loop returns its first requested channel
            here, matching
            :meth:`~miainwoodpecker.acquisition.live.MultiChannelLiveAcquisition.latest`.
        """

    def latest_frames(self, target: str) -> tuple[Frame, ...]:
        """
        Return every frame of a target's most recent pass.

        A name this instrument does not serve raises ``KeyError``.

        Parameters
        ----------
        target : str
            The target to read.

        Returns
        -------
        tuple[Frame, ...]
            The pass's frames in channel-request order, empty before the
            first pass completes. Never a mixture of two passes - the
            whole set is replaced under the loop's lock, which is what
            makes a per-pixel difference between two channels legitimate.
        """

    def stats(self, target: str) -> LiveStats:
        """
        Return a target's live-loop frame count and recent rate.

        A name this instrument does not serve raises ``KeyError``; a
        target with no loop running raises :class:`NotLiveError`.

        Parameters
        ----------
        target : str
            The target to read.

        Returns
        -------
        LiveStats
            Passes rather than frames for a multichannel scan loop, so
            the number does not change when an operator enables a second
            detector that costs no extra time.
        """

    def start_live(
        self,
        target: str,
        parameters: ScanParameters | None = None,
        *,
        channels: Sequence[int] = (0,),
    ) -> None:
        """
        Start a live loop on a target, if one is not already running.

        A name this instrument does not serve raises ``KeyError``. A
        target another client has leased raises
        :class:`DeviceBusyError`: a lease is exclusive against starting a
        display as much as against acquiring, because a display is a
        second driver like any other.

        Parameters
        ----------
        target : str
            The target to start.
        parameters : ScanParameters | None
            Scan geometry and dwell, for a scanner. Ignored for a camera,
            which is configured through
            :meth:`~miainwoodpecker.devices.interface.Camera.configure`
            under a lease.
        channels : Sequence[int]
            Which detectors to read out per pass, for a scanner. More
            than one produces a multichannel loop - one pass feeding
            every channel, rather than one loop per channel, because two
            loops would be twice the dose and would not share probe
            positions.
        """

    def reconfigure_live(
        self,
        target: str,
        parameters: ScanParameters,
        *,
        channels: Sequence[int] = (0,),
    ) -> None:
        """
        Change a running scan's geometry without stopping it.

        Stop-and-restart is not a neutral pair of operations on a scan
        unit: between them the probe stands still on one spot, and an
        operator dragging a field of view would pay that every time.
        Takes effect on the next pass; the one in flight finishes under
        the settings it started with, since half a frame at one dwell
        and half at another is not a frame of either.

        A name this instrument does not serve raises ``KeyError``; a
        target another client has leased raises
        :class:`DeviceBusyError`; a target that is not a scan unit
        raises ``ValueError``, because a camera's live settings are
        exposure and binning rather than a property of the loop.

        Parameters
        ----------
        target : str
            The scan unit to reconfigure.
        parameters : ScanParameters
            The geometry and dwell to use from the next pass on.
        channels : Sequence[int]
            Which detectors to read out per pass.
        """

    def stop_live(self, target: str) -> bool:
        """
        Stop a target's live loop.

        Rarely what a client wants for a scanner, and a caller should
        know why before reaching for it: a stopped scan is a stationary
        probe. Stopping is for a target about to be left alone, not for
        tidying a display away.

        A name this instrument does not serve raises ``KeyError``; a
        target another client has leased raises
        :class:`DeviceBusyError`.

        Parameters
        ----------
        target : str
            The target to stop.

        Returns
        -------
        bool
            True if the worker finished. False means an exposure or a
            pass is still in flight and the device is still in use - the
            loop is left running rather than declared stopped underneath
            it, which is the distinction that keeps two clients off one
            shared-memory segment.
        """

    def lease(
        self,
        *targets: str,
        reason: str = "",
        timeout_s: float = DEFAULT_LEASE_TIMEOUT_S,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
    ) -> contextlib.AbstractContextManager[LeasedDevices]:
        """
        Take exclusive control of one or more targets for a block.

        Each target's live loop is stopped and joined before the lease is
        granted, and restarted on release if it was running at grant
        time. There is no flag to suppress that restart; this module's
        docstring says why a stopped scan is the state worth avoiding.

        Targets are acquired in :data:`LEASE_ORDER` rather than argument
        order, so two clients asking for one pair in opposite orders
        cannot deadlock, and the scanner - acquired last, released first
        - stands parked for the shortest time the grant allows.

        A lease is granted whole or refused whole. A name this instrument
        does not serve raises ``KeyError``; a target held by another
        client, or whose worker did not join within ``timeout_s``, raises
        :class:`DeviceBusyError` - and every loop already stopped for
        this attempt is restarted first, so a refusal leaves the
        instrument as it found it.

        Contention is refused, not queued. A queue invites two clients to
        each believe they are next, and a lease has no bounded duration
        for a queue to reason about - the honest answer is who holds it
        and why, which :class:`DeviceBusyError` carries.

        Parameters
        ----------
        *targets : str
            The targets to hold. A synchronised acquisition names the
            scanner and the camera together; naming them in two nested
            leases is the deadlock this signature exists to prevent.
        reason : str
            Free text saying what the lease is for, shown to other
            clients and written into the session log. Worth filling in:
            it is the difference between an operator seeing "busy" and
            seeing "energy series, 5 steps".
        timeout_s : float
            Seconds to wait for each target's worker to join.
        ttl_s : float
            Seconds the lease survives without renewal.

        Returns
        -------
        contextlib.AbstractContextManager[LeasedDevices]
            A context manager yielding the leased devices. Released on
            exit, including on an exception - a script that raises
            mid-series must not leave the probe parked.
        """


def lease_order(targets: Sequence[str]) -> tuple[str, ...]:
    """
    Return targets in the order a lease must acquire them.

    Sorted by each name's :func:`~miainwoodpecker.devices.rpc.target_kind`
    position in :data:`LEASE_ORDER`, then by name. Release runs over the
    reverse.

    A kind not in :data:`LEASE_ORDER` sorts where ``"device"`` does - the
    unclassified kind an out-of-tree adapter's target gets - rather than
    raising, because a broker that refuses to arbitrate an unfamiliar
    target has handed the problem straight back to the race it exists to
    prevent.

    Parameters
    ----------
    targets : Sequence[str]
        Target names, in any order, possibly with duplicates.

    Returns
    -------
    tuple[str, ...]
        The distinct names, ordered.
    """
    unclassified = LEASE_ORDER.index("device")

    def rank(name: str) -> tuple[int, str]:
        kind = target_kind(name)
        position = LEASE_ORDER.index(kind) if kind in LEASE_ORDER else unclassified
        return (position, name)

    return tuple(sorted(set(targets), key=rank))
