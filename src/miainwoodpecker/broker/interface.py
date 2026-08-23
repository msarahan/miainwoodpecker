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

from miainwoodpecker.devices.preview import (
    DEFAULT_MAX_EDGE as PREVIEW_DEFAULT_MAX_EDGE,
)
from miainwoodpecker.devices.rpc import target_kind

if typing.TYPE_CHECKING:
    import contextlib
    from collections.abc import Mapping, Sequence

    from miainwoodpecker.acquisition.live import LiveStats
    from miainwoodpecker.devices.interface import (
        Camera,
        CameraParameters,
        Frame,
        InstrumentController,
        ScanParameters,
        Scanner,
        SpectrumDetector,
    )
    from miainwoodpecker.devices.preview import FramePreview


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

DEFAULT_PREVIEW_EDGE = PREVIEW_DEFAULT_MAX_EDGE
"""
Longest edge, in pixels, :meth:`InstrumentBroker.previews` reduces to.

Re-exported from :mod:`miainwoodpecker.devices.preview` rather than
restated, so the broker's default and the decimation's cannot drift
apart. A caller that wants smaller tiles - which is what buys the frame
rate over a slow link - passes its own.
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
class TargetDescription:
    """
    What a target *is*, as opposed to what it is doing.

    The facts a client needs before it can offer any controls at all: how
    many detectors a scan unit reads out and what they are called, which
    binning factors a camera supports, which cameras can be synchronised
    to the scan, which controls an instrument actually implements. A
    window cannot build a detector checkbox or a binning menu without
    them.

    They live here because reading them off a device handle is a *device
    call*, and a client in another process has no device handle to read.
    That was the one thing keeping the Qt window from being pointed at a
    broker somewhere else, and it is the same thing that would stop a
    notebook or a dashboard offering more than a picture.

    Read once, when the broker is built and nothing is running, and
    cached from then on. Not because it is expensive - it is four calls -
    but because it is the only honest moment: a read issued later would
    be a second caller on a device whose live loop is mid-pass, which is
    the interleaving the whole module exists to prevent.

    Attributes
    ----------
    name : str
        The target name.
    kind : str
        :func:`~miainwoodpecker.devices.rpc.target_kind` of the name.
    label : str
        What the device calls itself - a ``camera_id``, a ``scanner_id``
        - so a client can name a detector by what it is rather than by
        which slot it landed in. Falls back to the target name.
    channel_names : tuple[str, ...]
        The detectors a scan unit reads out, in channel order. Empty for
        anything that is not a scan unit.
    binning_values : tuple[int, ...]
        The binning factors a camera supports, ascending. Empty for
        anything that is not a camera.
    binning_values_yx : tuple[tuple[int, ...], tuple[int, ...]] | None
        The factors offered *per axis*, slow then fast, for a detector
        whose axes differ - a spectrometer, where binning rows buys
        signal-to-noise and binning channels spends energy resolution.
        None for a detector with one list for both, which is every
        camera that has not said otherwise; a client reads
        :attr:`binning_values` then. The same distinction
        :func:`~miainwoodpecker.devices.interface.axis_binning_values`
        makes against the handle, carried across a process boundary.
    synchronises : bool
        Whether this scan unit has a synchronised scan/camera mode at
        all - whether, in the device layer's terms, it is a
        :class:`~miainwoodpecker.devices.interface.SynchronisedScanner`.

        Separate from :attr:`synchronised_targets` because the empty
        tuple is otherwise two different situations wanting two
        different actions from an operator: a backend that cannot do
        synchronised acquisition (use a different instrument) and one
        that can with nothing wired to it (wire a detector). The
        distinction was an ``isinstance`` against the handle before
        this, which is exactly the kind of read a client in another
        process cannot make.
    synchronised_targets : tuple[str, ...]
        The cameras this scan unit can drive a synchronised pass into.
        Empty when the backend has no such wiring, which is a hardware
        fact rather than a missing feature - see
        :meth:`~miainwoodpecker.devices.interface.SynchronisedScanner.scan_synchronised`.
    controls : tuple[str, ...]
        The ``*_CONTROL`` names an instrument implements. Empty for
        anything that is not an instrument controller.
    backend : str
        What the device server says it is driving - ``simulated``, a
        vendor's name - for an instrument target. Empty for anything
        else, and for a server that does not say.
    native_scan : ScanParameters | None
        The one geometry this scan unit can acquire, when it has only
        one. A replay device does: it holds the grid the probe actually
        visited, and no request makes it another. None means "takes
        whatever grid it is given", which is every real scan unit - see
        :func:`~miainwoodpecker.devices.interface.native_scan_parameters`.

        A client that ignored this would offer an operator a square spin
        box for a recording that is 22x25, and have every acquisition
        refused.
    error : str | None
        Why this description is incomplete, when a device refused to
        answer one of the questions above.

        It exists because the empty tuple is otherwise ambiguous, and
        ambiguous in a way that matters: "this camera supports no
        binning but 1x" and "this camera would not say what binning it
        supports" produce the same empty field and want opposite
        responses from a client - offer the one value, or say the device
        is not answering. A window that silently shows an empty menu for
        the second case has turned a broken adapter into a UI that looks
        merely poor.
    """

    name: str
    kind: str
    label: str
    channel_names: tuple[str, ...] = ()
    binning_values: tuple[int, ...] = ()
    binning_values_yx: tuple[tuple[int, ...], tuple[int, ...]] | None = None
    synchronises: bool = False
    synchronised_targets: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()
    backend: str = ""
    native_scan: ScanParameters | None = None
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


@dataclass(frozen=True)
class TargetPreview:
    """
    One target's state and its latest frames *as pictures*.

    The same pairing :class:`TargetView` makes, and read under the same
    lock for the same reason - a tile must not show a rate from one pass
    beside pixels from another - but carrying
    :class:`~miainwoodpecker.devices.preview.FramePreview` instead of
    :class:`~miainwoodpecker.devices.interface.Frame`.

    A separate type rather than a widened :class:`TargetView` because
    the difference is the one a caller must not get wrong: these pixels
    are decimated and have no calibration, and code that recorded them
    or measured off them would be wrong quietly. A watcher that wants
    the measurement asks :meth:`InstrumentBroker.snapshot`; a watcher
    that wants a picture asks :meth:`InstrumentBroker.previews`, and the
    type it gets back says which one it asked for.

    Attributes
    ----------
    state : TargetState
        What the target is doing. Identical to the one
        :meth:`InstrumentBroker.snapshot` would report at the same
        instant - the reduction is to the pixels only.
    frames : tuple[FramePreview, ...]
        The latest pass's frames, decimated, empty if none has arrived.
    """

    state: TargetState
    frames: tuple[FramePreview, ...] = ()


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

        The caller's job: nothing renews on a timer, deliberately, since
        a timer would keep a wedged client's hold alive precisely when it
        should lapse. A caller whose work outlives
        :data:`DEFAULT_LEASE_TTL_S` renews as that work completes - per
        frame of a long recording, say - so that progress is what extends
        the hold.

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

    def describe(self) -> Mapping[str, TargetDescription]:
        """
        Return what each target *is*, rather than what it is doing.

        Static for the life of the instrument and cached, so a client
        may call it as often as it likes. It is what makes a client in
        another process able to offer controls rather than only a
        picture: detector names, binning factors, which cameras the scan
        unit can synchronise to, which controls exist.

        Returns
        -------
        Mapping[str, TargetDescription]
            Keyed by target name.
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

        Frames travel with it at **full size**, which is what a viewer
        sharing a process with its broker wants and what a watcher on
        the other end of a socket cannot afford at any rate worth
        calling live: see :meth:`previews`, which is this call with the
        pixels reduced to what a tile draws.

        Returns
        -------
        Mapping[str, TargetView]
            Keyed by target name.
        """

    def previews(
        self,
        max_edge: int = DEFAULT_PREVIEW_EDGE,
    ) -> Mapping[str, TargetPreview]:
        """
        Return every target's state and latest frames, as pictures.

        :meth:`snapshot` with the pixels decimated **before** they are
        sent, and the only reason it exists is the wire. A snapshot
        carries every target's frames at full size: on an instrument
        serving a 2048x2048 camera beside a scan unit that is 19 MB per
        call, so two frames a second is 320 Mbit/s and a gigabit link is
        saturated before five. The same view at a 256-pixel edge is
        roughly 200 kB, which is a live view at ten frames a second over
        an ordinary network - and the pixels dropped are ones the client
        was decimating away on arrival anyway.

        In process this saves nothing and is not meant to: a
        :class:`~miainwoodpecker.broker.local.LocalBroker` hands over the
        same arrays either way, and a caller sharing the process with
        its devices should ask :meth:`snapshot`. The reduction is paid
        here so that it happens once per instrument rather than once per
        watcher, and so that it happens on the side of the socket where
        it makes the message smaller.

        Everything :meth:`snapshot` promises about *consistency* holds
        unchanged: state and frames are read together, under one lock,
        so a tile cannot show a rate from one pass beside pixels from
        another.

        **Watching, still.** Costs no device call, starts and stops
        nothing, and needs no lease - the same guarantee every other
        watch verb makes.

        A ``max_edge`` below 1 raises ``ValueError``, and both
        transports raise it before the instrument is asked anything.

        Parameters
        ----------
        max_edge : int
            Longest edge, in pixels, to reduce each frame to. A frame
            already within it is sent whole rather than resampled.

        Returns
        -------
        Mapping[str, TargetPreview]
            Keyed by target name, with the same keys :meth:`snapshot`
            would return.
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
            have - or would not answer for, which is logged and looks
            the same from here on purpose: a client shows the field it
            has no fresh value for as stale either way, and one broken
            control must not cost every client the rest of the reading.
            The stage position appears as ``stage_position_y_nm`` and
            ``stage_position_x_nm``, both or neither.
        """

    def camera_parameters(self, target: str) -> CameraParameters | None:
        """
        Return what a detector is currently configured to do.

        Watch-side, for the same reason :meth:`controls` is: a client
        offering an exposure field or a readout selector has to show
        what the device is set to before it can offer to change it, and
        having to take a lease to find out would mean every window held
        one from the moment it opened.

        Unlike :meth:`describe`, this is not static - an exposure is
        changed under a lease and the next reader should see it - so it
        is read from the device rather than cached at construction. It
        is read only while nothing holds a lease on the target, for the
        reason :meth:`controls` gives: a read issued mid-lease is a
        second caller on a one-request-at-a-time device.

        A name this instrument does not serve raises ``KeyError``.

        Parameters
        ----------
        target : str
            The detector to read.

        Returns
        -------
        CameraParameters | None
            Its exposure, binning and readout mode, or None for a target
            that has no such settings - a scan unit, an instrument
            controller. A detector that *refuses* the question raises
            rather than answering None: unlike a description, this is
            what a client is about to change, and "it would not say" and
            "it has none" want different responses.
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
