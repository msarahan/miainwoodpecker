"""
One poll of the broker, turned into the tiles a dashboard draws.

Everything here is a pure function of two mappings the broker hands over
- :meth:`~miainwoodpecker.broker.interface.InstrumentBroker.describe`
and
:meth:`~miainwoodpecker.broker.interface.InstrumentBroker.snapshot` - and
that is the whole reason it is a module rather than a cell in the
notebook. A marimo cell cannot be unit-tested without marimo's runtime;
this can, and what it decides is the part that would be wrong in ways
nobody notices: which targets get a tile, in what order, and what the
chrome says when a loop has died.

**Placement comes from ``describe``, never from ``snapshot``.** A
description is static and cached for the life of the instrument, so the
order these tiles come back in cannot change between polls. Ordering by
what is currently live would rearrange the grid the moment a camera
stopped - and an operator who has learned that the EELS camera is the
second tile would find a Ronchigram there instead, mid-experiment. It is
also what makes marimo's saved grid layout keep meaning what it meant
when it was saved.

**Watching never drives.** Nothing in this module calls a device or a
live-loop control; it is handed the answers. The dashboard's polling
loop is a read, and a caller looking at what is on screen must not be
able to move the probe by looking.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass, field

if typing.TYPE_CHECKING:
    from collections.abc import Mapping

    from miainwoodpecker.broker.interface import (
        TargetDescription,
        TargetPreview,
        TargetView,
    )
    from miainwoodpecker.devices.interface import Frame
    from miainwoodpecker.devices.preview import FramePreview

FRAME_SOURCE_KINDS = ("scanner", "camera")
"""
The target kinds a live tile can be built for.

Kinds are :func:`~miainwoodpecker.devices.rpc.target_kind`'s. The list is
short because a tile is a picture: an ``instrument`` target has controls
and no pixels, and a ``spectrum`` detector produces
:class:`~miainwoodpecker.devices.interface.Spectrum` objects, which are
natively 1D and want a plot rather than an image. Leaving them out is
not a gap to be filled later by widening this tuple - the broker's own
``start_live`` treats anything that is not a scanner as a camera, so a
tile offered for one would be a control that fails.
"""

_UNKNOWN_STATE = "not reported by the broker"


@dataclass(frozen=True)
class FrameTile:
    """
    One frame-producing target, as a dashboard tile needs it.

    Everything a tile draws, gathered from the two broker calls a poll
    makes, so the cell that renders it does no lookups of its own and
    cannot show a rate from one poll beside pixels from another.

    Attributes
    ----------
    name : str
        The target name, as the device server serves it. This is what a
        lease is taken on.
    label : str
        What the device calls itself - a ``camera_id``, a ``scanner_id``
        - so a tile is titled by what it is rather than by which slot it
        landed in. Falls back to :attr:`name`.
    kind : str
        :func:`~miainwoodpecker.devices.rpc.target_kind` of the name.
    channel_names : tuple[str, ...]
        The detectors a scan unit reads out, in channel order. Empty for
        a camera. This is what the detector checkboxes are built from.
    binning_values : tuple[int, ...]
        The binning factors a camera supports, ascending. Empty for a
        scan unit. This is what the binning menu is built from.
    is_live : bool
        Whether a live loop is running on this target.
    fps : float
        The loop's recent rate, or 0.0 when it is not running.
    frame_count : int
        Frames the loop has produced since it last started, or 0.
    holder : str | None
        Who holds a lease on this target, or None if nobody does.
    held_by_me : bool
        Whether :attr:`holder` is this client. The two look identical
        from outside and mean opposite things: somebody else's lease is
        a reason to wait, and your own is a reason the picture has
        stopped advancing.
    reason : str
        What the lease was taken for, as its holder stated it. Empty
        when there is no lease, or when the holder gave no reason.
    error : str | None
        The exception that stopped the live loop, if one did. A loop
        that died leaves :attr:`is_live` False and this set, which is
        how a tile says "stopped: camera timed out" rather than going
        quietly blank.
    frames : tuple[Frame | FramePreview, ...]
        The latest pass's frames, in channel-request order, empty before
        the first one arrives. A multichannel scan pass has one per
        enabled detector, and they share a probe position - see
        ``scan_pass_id`` in
        :class:`~miainwoodpecker.devices.interface.Frame`.

        Either type, because a tile is built the same way from either
        and the difference is the caller's: a watcher in process asks
        :meth:`~miainwoodpecker.broker.interface.InstrumentBroker.snapshot`
        and gets frames, and one across a socket asks
        :meth:`~miainwoodpecker.broker.interface.InstrumentBroker.previews`
        and gets pictures. What this module reads off them - the pixels,
        and ``channel_name`` - both carry.

        Which one it is matters to anything that would *measure*, and
        nothing here does. A caller holding a tile and wanting the
        calibrated frame behind it must go and ask for it; there is no
        way back to a measurement from a picture, which is the point of
        :class:`~miainwoodpecker.devices.preview.FramePreview` being its
        own type.
    """

    name: str
    label: str
    kind: str
    channel_names: tuple[str, ...] = ()
    binning_values: tuple[int, ...] = ()
    is_live: bool = False
    fps: float = 0.0
    frame_count: int = 0
    holder: str | None = None
    held_by_me: bool = False
    reason: str = ""
    error: str | None = None
    frames: tuple[Frame | FramePreview, ...] = field(default_factory=tuple)


def frame_sources(
    described: Mapping[str, TargetDescription],
) -> tuple[TargetDescription, ...]:
    """
    Pick the targets a dashboard can show a picture of, in a fixed order.

    Separate from :func:`frame_tiles` because the two are wanted at
    different rates, and confusing them is a real bug rather than a
    stylistic one. Tiles are rebuilt on every poll; the *controls* -
    which target to acquire from, which detectors, which binning - must
    not be, or an operator's choices are wiped by the display timer - up
    to ten times a second, which is what the fastest interval means and
    is the rate this argument was already made against at one.
    This is the half that comes from ``describe()``,
    which is static and cached for the life of the instrument.

    Parameters
    ----------
    described : Mapping[str, TargetDescription]
        What each target is, from ``describe()``.

    Returns
    -------
    tuple[TargetDescription, ...]
        The frame-producing targets, in ``describe()`` order.
    """
    return tuple(
        description
        for description in described.values()
        if description.kind in FRAME_SOURCE_KINDS
    )


def frame_tiles(
    described: Mapping[str, TargetDescription],
    viewed: Mapping[str, TargetView | TargetPreview],
    *,
    holder: str | None = None,
) -> tuple[FrameTile, ...]:
    """
    Build the dashboard's tiles from one poll of the broker.

    Parameters
    ----------
    described : Mapping[str, TargetDescription]
        What each target is, from ``describe()``. Iterated in its own
        order, which is what fixes each tile's place in the grid.
    viewed : Mapping[str, TargetView | TargetPreview]
        What each target is doing and its latest frames, from
        ``snapshot()`` or from ``previews()``. Either, and the same code
        either way: both pair a state with the latest pass's pixels, and
        the choice between them is about what crosses the wire rather
        than about what a tile shows.

        A target described but missing here still gets a tile - with
        :attr:`FrameTile.error` saying so - because a grid with a hole
        in it is harder to read than a grid with a tile that explains
        itself.
    holder : str | None
        This client's identity, as the broker assigned it. Learned from
        :attr:`~miainwoodpecker.broker.interface.Lease.holder` on a
        lease this client was granted; None before it has taken one, in
        which case no tile claims to be held by you. Deliberately not
        guessed from a hostname or a process id: the broker fills the
        holder in from the connection precisely so that a client cannot
        name itself whatever it likes.

    Returns
    -------
    tuple[FrameTile, ...]
        One tile per frame-producing target, in ``describe()`` order.
    """
    tiles: list[FrameTile] = []
    for description in frame_sources(described):
        name = description.name
        view = viewed.get(name)
        if view is None:
            tiles.append(
                FrameTile(
                    name=name,
                    label=description.label or name,
                    kind=description.kind,
                    channel_names=description.channel_names,
                    binning_values=description.binning_values,
                    error=_UNKNOWN_STATE,
                ),
            )
            continue
        state = view.state
        lease = state.lease
        stats = state.stats
        tiles.append(
            FrameTile(
                name=name,
                label=description.label or name,
                kind=description.kind,
                channel_names=description.channel_names,
                binning_values=description.binning_values,
                is_live=state.is_live,
                # Zero rather than None when no loop is running: a tile
                # renders a number here, and the *reason* there is no
                # rate is carried by is_live, which is the field a
                # caller must branch on. TargetState keeps them apart
                # for the same reason - see NotLiveError.
                fps=stats.fps if stats is not None else 0.0,
                frame_count=stats.frame_count if stats is not None else 0,
                holder=lease.holder if lease is not None else None,
                held_by_me=(
                    lease is not None and holder is not None and lease.holder == holder
                ),
                reason=lease.reason if lease is not None else "",
                error=state.error,
                frames=tuple(view.frames),
            ),
        )
    return tuple(tiles)


def rate_text(tile: FrameTile) -> str:
    """
    Describe whether a picture is arriving, and how fast.

    A dead loop is reported before anything else, because it is the one
    state where the tile still shows pixels and they are stale: the last
    frame before the failure stays on screen, and without this the tile
    is indistinguishable from a live view of a motionless specimen.

    Parameters
    ----------
    tile : FrameTile
        The tile to describe.

    Returns
    -------
    str
        A short phrase for the tile's status line.
    """
    if tile.error is not None:
        return f"stopped: {tile.error}"
    if not tile.is_live:
        return "not running"
    if tile.fps <= 0.0:
        # LiveStats reports 0.0 until two frames have arrived, so this is
        # "too early to say" rather than "stalled". Printing 0.0 fps
        # would be the one number a stalled loop also shows.
        return f"live - measuring rate - {tile.frame_count} frames"
    return f"live - {tile.fps:.1f} fps - {tile.frame_count} frames"


def lease_text(tile: FrameTile) -> str:
    """
    Say who is driving this target, if anybody is.

    Parameters
    ----------
    tile : FrameTile
        The tile to describe.

    Returns
    -------
    str
        A phrase naming the holder and their reason, or the empty string
        when the target is free. Empty rather than "not leased": an
        unleased target is the ordinary case, and printing a line about
        it on every tile would bury the one tile where it matters.
    """
    if tile.holder is None:
        return ""
    who = "you" if tile.held_by_me else tile.holder
    if tile.reason:
        return f"leased by {who} ({tile.reason})"
    return f"leased by {who}"


def tile_status(tile: FrameTile) -> str:
    """
    Return the tile's whole status line: rate, then lease.

    Parameters
    ----------
    tile : FrameTile
        The tile to describe.

    Returns
    -------
    str
        One line, with the lease appended only when there is one.
    """
    held = lease_text(tile)
    return f"{rate_text(tile)} - {held}" if held else rate_text(tile)


def channel_labels(tile: FrameTile) -> tuple[str, ...]:
    """
    Name each frame of the latest pass, for captioning a multichannel tile.

    The frames of a pass arrive in channel-request order and a tile shows
    the first, but a scan unit reading HAADF and MAADF out together
    produces two - and which one is on screen is not guessable from the
    picture.

    The name is read from **each frame's own metadata**, not from the
    tile's ``channel_names`` by position. Those are every channel the
    scan unit has, in its order; the pass holds only the enabled ones,
    so a loop running channels 1 and 2 would be captioned with channels
    0 and 1's names - two frames labelled as detectors that did not
    produce them. ``channel_name`` is in the frame vocabulary precisely
    so a frame says which detector it came from.

    Parameters
    ----------
    tile : FrameTile
        The tile whose frames to name. Previews carry this metadata as
        frames do - ``channel_name`` describes the detector, not the
        pixel grid, so decimation leaves it alone.

    Returns
    -------
    tuple[str, ...]
        One label per frame currently held. A frame that reports no
        channel - every camera frame, and any adapter that omits the key
        - falls back to its position, since an absent key means "not
        reported" and inventing a detector name would be worse.
    """
    labels: list[str] = []
    for index, frame in enumerate(tile.frames):
        named = frame.metadata.get("channel_name")
        labels.append(str(named) if named else f"frame {index}")
    return tuple(labels)
