"""
A frame reduced to something you can look at, and nothing more.

A live tile is a picture. The frame behind it is a measurement, and the
two want opposite things from a wire: the measurement wants every pixel
in its own dtype, and the picture wants to be small enough to send ten
times a second. :class:`FramePreview` is the second one, and it is a
separate type from
:class:`~miainwoodpecker.devices.interface.Frame` on purpose.

**Why not just a smaller Frame.** Because a ``Frame`` is what gets
recorded, measured and handed to an analysis, and a decimated one is
none of those things while looking exactly like all of them. The
specific trap is ``metadata["calibration"]``: it is *units per pixel*,
so a frame subsampled by a stride of 8 whose calibration came along
unchanged claims a pixel size eight times too small, and every distance
anybody measures off it is wrong by that factor with nothing anywhere
saying so. Refusing to be a ``Frame`` is the only version of this that
cannot be misread - a preview has no calibration to be wrong, and
:attr:`FramePreview.stride` says what was done to it.

**Where the decimation happens is the whole point.** Doing it here, in
the process that holds the device, is what makes a remote live view
affordable: ``snapshot()`` ships every target's pixels at full size, and
on an instrument serving a 2048x2048 camera beside a scan unit that is
19 MB per call - 320 Mbit/s at two frames a second, and a gigabit link
saturated before five. The same view as previews at a 256-pixel edge is
roughly 200 kB. The pixels a dashboard actually draws are a few hundred
across; the rest were being sent so that a client could throw them away.

**Nearest-neighbour, not a block mean**, for the reason
:mod:`miainwoodpecker.dashboard.images` gives: aliasing is the honest
artefact of a preview, a block mean is a *different image* from the one
the file holds, and the cost would be paid on every tick of every
watcher. It also keeps this a strided view rather than an average, so
the only pixels that cost anything are the ones being sent.

Nothing here is on the acquisition path. What gets recorded is the
frame, at full size, in its own dtype, and a lease is what produces it.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass, field

import numpy as np

if typing.TYPE_CHECKING:
    import datetime

    import numpy.typing as npt

    from miainwoodpecker.devices.interface import Frame

DEFAULT_MAX_EDGE = 512
"""
Longest edge, in pixels, a preview is decimated to when none is asked for.

Matches :data:`miainwoodpecker.dashboard.images.TILE_MAX_EDGE`, so a
caller that asks for a preview and then renders it with that module's
defaults decimates once rather than twice.
"""

_EXPECTED_RANK = 2

# The metadata keys a preview must not carry, because decimation makes
# them false rather than merely absent. Only one so far, and it is
# listed rather than filtered by guesswork so that adding a
# grid-describing key to the frame vocabulary is a decision made here
# too - see the module docstring for what a stale calibration costs.
_PIXEL_GRID_KEYS = frozenset({"calibration"})

STRIDE_KEY = "preview_stride"
"""
Metadata key recording how far apart a preview's pixels are.

Present on every preview, including an undecimated one, where it is 1.
Always present rather than only when it is interesting: a consumer
reading it has to be able to tell "every pixel" from "this adapter did
not say", and an absent key means the second everywhere else in the
frame vocabulary.
"""


@dataclass(frozen=True)
class FramePreview:
    """
    A decimated copy of a frame's pixels, for looking at.

    Carries the frame's own metadata minus anything decimation would
    make untrue, so the parts a display needs - which detector this
    came from, above all - survive the reduction. See the module
    docstring for why this is not a
    :class:`~miainwoodpecker.devices.interface.Frame`.

    Attributes
    ----------
    data : npt.NDArray[typing.Any]
        The subsampled pixels, in the source frame's dtype. 2D for
        images, and 1D for a camera in projected readout, exactly as
        :attr:`~miainwoodpecker.devices.interface.Frame.data` is.
    source_shape : tuple[int, ...]
        The shape of the frame this came from, so a tile can say what it
        is a preview *of*. Equal to ``data.shape`` when nothing was
        decimated away.
    stride : int
        The step taken through the source, on both axes. 1 when the
        frame already fit. Also recorded in :attr:`metadata` under
        :data:`STRIDE_KEY`.
    timestamp : datetime.datetime
        The source frame's acquisition time, carried through unchanged -
        decimation does not move a frame in time, and a tile that could
        not say how old its picture is would be a worse tile.
    metadata : typing.Mapping[str, typing.Any]
        The frame's metadata, minus the keys that describe the pixel
        grid, plus :data:`STRIDE_KEY`.
    """

    data: npt.NDArray[typing.Any]
    source_shape: tuple[int, ...]
    stride: int
    timestamp: datetime.datetime
    metadata: typing.Mapping[str, typing.Any] = field(default_factory=dict)


def decimation_stride(shape: tuple[int, ...], max_edge: int) -> int:
    """
    Return the step that brings an array's longest edge within a limit.

    Parameters
    ----------
    shape : tuple[int, ...]
        The array's shape.
    max_edge : int
        Longest edge to allow, in pixels.

    Returns
    -------
    int
        The stride to take on every axis; 1 when the array already fits.

    Raises
    ------
    ValueError
        If ``max_edge`` is not positive, which would divide by zero.
    """
    if max_edge < 1:
        message = f"max_edge must be at least 1, got {max_edge!r}"
        raise ValueError(message)
    longest = max(shape) if shape else 0
    if longest <= max_edge:
        return 1
    # Ceiling division: a stride that rounded down would leave the
    # result one pixel over the limit on most shapes.
    return -(-longest // max_edge)


def decimate(
    data: npt.NDArray[typing.Any],
    max_edge: int = DEFAULT_MAX_EDGE,
) -> npt.NDArray[typing.Any]:
    """
    Subsample a 2D array so that neither edge exceeds ``max_edge``.

    Parameters
    ----------
    data : npt.NDArray[typing.Any]
        The frame's pixels.
    max_edge : int
        Longest edge to allow, in pixels.

    Returns
    -------
    npt.NDArray[typing.Any]
        The array itself when it already fits - no copy, because the
        caller only reads it - or a strided view taking every nth pixel.

    Raises
    ------
    ValueError
        If the array is not 2D, or ``max_edge`` is not positive. Both are
        programming errors rather than instrument states: an image frame
        is 2D by design, and a non-positive edge would divide by zero.
    """
    values = np.asarray(data)
    if values.ndim != _EXPECTED_RANK:
        message = f"a display tile needs a 2D frame, got shape {values.shape}"
        raise ValueError(message)
    stride = decimation_stride(values.shape, max_edge)
    if stride == 1:
        return values
    return values[::stride, ::stride]


def preview_of(
    frame: Frame,
    max_edge: int = DEFAULT_MAX_EDGE,
) -> FramePreview:
    """
    Reduce one frame to a preview of it.

    Accepts a **1D** frame as well as a 2D one, unlike :func:`decimate`,
    and that is deliberate rather than incidental: a camera in
    :data:`~miainwoodpecker.devices.interface.PROJECTED_READOUT` delivers
    a spectrum, and a watch call that raised on one would fail for a
    perfectly ordinary instrument state. The preview says it is 1D by
    being 1D, and a caller decides what to draw - which is the check
    :func:`~miainwoodpecker.dashboard.images.is_image` already exists to
    make.

    A ``max_edge`` below 1 raises ``ValueError``, from
    :func:`decimation_stride`, which is where the one check lives.

    Parameters
    ----------
    frame : Frame
        The frame to reduce. Not modified, and not retained.
    max_edge : int
        Longest edge to decimate to, in pixels.

    Returns
    -------
    FramePreview
        The subsampled pixels and the metadata that survives them.
    """
    values = np.asarray(frame.data)
    stride = decimation_stride(values.shape, max_edge)
    # A copy, not the strided view, and on purpose: this is about to be
    # pickled onto a socket, and pickling a view of a 2048x2048 array
    # materialises only the view - but keeping the view alive here would
    # hold the whole source array with it. np.ascontiguousarray on the
    # already-sliced result is the small allocation.
    reduced = values[(slice(None, None, stride),) * values.ndim]
    metadata = {
        key: value
        for key, value in frame.metadata.items()
        if key not in _PIXEL_GRID_KEYS
    }
    metadata[STRIDE_KEY] = stride
    return FramePreview(
        data=np.ascontiguousarray(reduced),
        source_shape=tuple(values.shape),
        stride=stride,
        timestamp=frame.timestamp,
        metadata=metadata,
    )
