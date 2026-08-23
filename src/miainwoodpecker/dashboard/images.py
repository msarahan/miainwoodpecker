"""
Turning a frame into something a browser will draw.

A napari layer takes a numpy array. A web page does not: it takes an
``<img>``, and the shortest honest route from an array to one is an
8-bit greyscale PNG in a ``data:`` URI. That is what this module is.

**Why a PNG encoder rather than a library.** The obvious candidates are
Pillow and matplotlib, and both would have to be declared in the
``marimo`` optional-dependency group - marimo itself pulls in neither.
Adding an imaging stack to a group whose only use for it is one call is
a poor trade against the thirty lines below: a greyscale PNG is a fixed
13-byte header, one zlib stream, and three CRCs, all of them in the
standard library. There is nothing here that a future frame type could
outgrow without also outgrowing ``Frame.data``, which is 2D by design.

**Decimate first, then autoscale, and that order is deliberate.** A
2048x2048 frame is 4.2 million pixels; ``np.percentile`` over it is a
full sort, and a dashboard poll would pay that per tile per tick.
Decimating to a 512-pixel edge first cuts it to 262 thousand - and the
percentiles are then computed over exactly the pixels the operator is
looking at, which is the more defensible statistic anyway. The
acquisition path is untouched by any of this: what gets recorded is the
frame, at full size, in its own dtype.

**The order survives the decimation moving to the server.** A client
polling
:meth:`~miainwoodpecker.broker.interface.InstrumentBroker.previews`
receives pixels that have already been reduced, and
:func:`decimate` here then finds nothing left to do. What
:func:`autoscale` sees is the same set of pixels either way - the ones
about to be drawn - so the stretch a tile gets does not depend on which
side of the socket the subsampling happened on. That is a property worth
stating: it is what makes it safe for the dashboard to ask for previews
over a wire and snapshots in process, and get the same picture.

**Nearest-neighbour decimation, not a block mean.** Aliasing on a
downsampled tile is visible and is the honest artefact of a preview; a
block mean would be a *different image* from the one the file holds, at
a cost paid on every tick. A tile is chrome. The measurement is the
recording. The decimation itself lives in
:mod:`miainwoodpecker.devices.preview`, because the broker does it too
and two implementations of "every nth pixel" would eventually disagree
about the rounding.
"""

from __future__ import annotations

import base64
import struct
import typing
import zlib

import numpy as np

from miainwoodpecker.devices.preview import decimate

if typing.TYPE_CHECKING:
    import numpy.typing as npt

__all__ = [
    "DEFAULT_HIGH_PERCENTILE",
    "DEFAULT_LOW_PERCENTILE",
    "THUMBNAIL_MAX_EDGE",
    "TILE_EDGES",
    "TILE_MAX_EDGE",
    "autoscale",
    "decimate",
    "greyscale_png",
    "is_image",
    "png_data_uri",
]
"""
Re-exports :func:`~miainwoodpecker.devices.preview.decimate` deliberately.

It was defined here first, and callers - the notebook among them - import
it from here. Moving it to the device layer for the broker's sake should
not move it out from under them, so the name stays.
"""

TILE_MAX_EDGE = 512
"""
Longest edge, in pixels, a live tile is decimated to by default.

A default rather than a rule, and the frame rate is what it is traded
against. A tile is a few hundred pixels wide in any browser window that
holds several of them, so 512 leaves room to enlarge one without
re-fetching - at 8 bits, 262 kB before zlib and roughly 300 kB once it
is base64 in a ``data:`` URI, per tile per tick.

Measured, on a three-tile grid: at 512 the whole round trip from the
display timer to new pixels on screen is about 120 ms, and at 256 it is
about 98 ms - which is marimo's own floor, since it clamps a refresh
interval to 0.1 s. So the last of the frame rate costs half the tile's
edge, and which of the two an operator wants depends on the screen and
the link. The dashboard offers it as a control rather than choosing.
"""

TILE_EDGES = (128, 256, 512, 1024)
"""
The tile sizes a dashboard offers, ascending.

Powers of two because the decimation is a stride and these are the
values at which it divides a common detector evenly, so a 2048-pixel
camera lands on an exact 8, 4 or 2 rather than on a ragged edge.

The range is bounded at both ends by what the setting is *for*. Below
128 a scan tile stops being readable as an image; above 1024 a tile is
larger than the pane showing it, and the extra pixels are paid for on
every tick by every watcher to be thrown away by the browser. 1024 is
included for the one case that wants it - a single tile, enlarged, on a
link that can afford it - and is not the default for the same reason.
"""

THUMBNAIL_MAX_EDGE = 128
"""
Longest edge, in pixels, a session-log thumbnail is decimated to.

The log is append-only and unbounded, so every entry's picture is kept
for the life of the notebook kernel. At 128 that is a few kilobytes an
entry; at full size a hundred 2048x2048 entries would be 1.6 GB of
kernel memory to show a shift's worth of history.
"""

# The percentiles the display stretch is taken between - deliberately
# not the minimum and maximum. A single hot pixel, which every real
# detector has, sets the maximum, and stretching to it flattens the rest
# of the frame to near-black. Clipping half a percent off each end is a
# *display* decision only: nothing here is written to a file or handed to
# an analysis, so the clipped pixels are lost from the picture and from
# nothing else.
DEFAULT_LOW_PERCENTILE = 0.5
DEFAULT_HIGH_PERCENTILE = 99.5

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_BIT_DEPTH = 8
_COLOUR_TYPE_GREYSCALE = 0
_NO_FILTER = 0
_EXPECTED_RANK = 2
_MAX_LEVEL = 255.0
# zlib's own default, left alone deliberately. This runs once per tile
# per poll on the kernel thread, so the level that matters is the one
# that is fast; squeezing the last few percent out of a picture that is
# replaced a second later is effort spent on the wrong axis. Unlike the
# storage layer's codec choice (storage/nexus.py), nothing downstream
# ever reads these bytes back.
_COMPRESSION_LEVEL = 6


def is_image(data: npt.NDArray[typing.Any]) -> bool:
    """
    Return whether these pixels can be drawn as a greyscale tile.

    Asked rather than assumed because one ordinary camera state answers
    no: a camera configured with
    :data:`~miainwoodpecker.devices.interface.PROJECTED_READOUT` sums the
    non-dispersive direction and delivers a **1D spectrum**, and
    :class:`~miainwoodpecker.devices.interface.Frame` holds it as such.
    Encoding that as a one-pixel-high PNG would be a picture of nothing;
    a spectrum wants a plot, which is a different tile than this module
    makes. A caller that skipped this check would take the ``ValueError``
    from :func:`decimate` through a display loop instead.

    Parameters
    ----------
    data : npt.NDArray[typing.Any]
        The frame's pixels.

    Returns
    -------
    bool
        True when the array is 2D.
    """
    return np.asarray(data).ndim == _EXPECTED_RANK


def autoscale(
    data: npt.NDArray[typing.Any],
    *,
    low_percentile: float = DEFAULT_LOW_PERCENTILE,
    high_percentile: float = DEFAULT_HIGH_PERCENTILE,
) -> npt.NDArray[np.uint8]:
    """
    Stretch a frame's values onto the 0-255 range a greyscale PNG holds.

    Two states render as black rather than as contrast, and both on
    purpose. A frame with **no range** - a blanked beam, a detector
    reading a constant - has nothing to stretch, and amplifying its
    floating-point dust to full scale would draw structure that is not
    in the specimen. A frame with **no finite values at all** is the
    same case with less to go on.

    Parameters
    ----------
    data : npt.NDArray[typing.Any]
        The frame's pixels, of any numeric dtype.
    low_percentile : float
        Percentile mapped to black.
    high_percentile : float
        Percentile mapped to white.

    Returns
    -------
    npt.NDArray[np.uint8]
        The same shape, as 8-bit levels.
    """
    values = np.asarray(data)
    finite = np.isfinite(values)
    if not finite.any():
        return np.zeros(values.shape, dtype=np.uint8)
    low, high = np.percentile(values[finite], (low_percentile, high_percentile))
    if not high > low:
        return np.zeros(values.shape, dtype=np.uint8)
    stretched = (values.astype(np.float64) - float(low)) / (float(high) - float(low))
    # NaN to black and infinities to the end they came from: a detector
    # that reports one must not blank the whole tile through the clip.
    stretched = np.nan_to_num(stretched, nan=0.0, posinf=1.0, neginf=0.0)
    return (np.clip(stretched, 0.0, 1.0) * _MAX_LEVEL + 0.5).astype(np.uint8)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    """
    Wrap one PNG chunk: length, type, payload, CRC.

    Parameters
    ----------
    kind : bytes
        The four-byte chunk type.
    payload : bytes
        The chunk's data.

    Returns
    -------
    bytes
        The encoded chunk.
    """
    return b"".join(
        (
            struct.pack(">I", len(payload)),
            kind,
            payload,
            # The CRC covers the type as well as the data, which is the
            # part of the spec that is easy to get wrong and produces a
            # file every decoder rejects.
            struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF),
        ),
    )


def greyscale_png(
    data: npt.NDArray[typing.Any],
    *,
    max_edge: int = TILE_MAX_EDGE,
    low_percentile: float = DEFAULT_LOW_PERCENTILE,
    high_percentile: float = DEFAULT_HIGH_PERCENTILE,
) -> bytes:
    """
    Encode a frame as an 8-bit greyscale PNG.

    Parameters
    ----------
    data : npt.NDArray[typing.Any]
        The frame's pixels.
    max_edge : int
        Longest edge to decimate to before encoding.
    low_percentile : float
        Percentile mapped to black.
    high_percentile : float
        Percentile mapped to white.

    Returns
    -------
    bytes
        A complete PNG file.
    """
    levels = autoscale(
        decimate(data, max_edge),
        low_percentile=low_percentile,
        high_percentile=high_percentile,
    )
    height, width = levels.shape
    # Every scanline carries a filter byte. Filter 0 (none) keeps this
    # encoder to what it claims to be; zlib is doing the compression and
    # a per-row predictor would buy a few percent for a second loop.
    raw = b"".join(b"\x00" + row.tobytes() for row in levels)
    header = struct.pack(
        ">IIBBBBB",
        width,
        height,
        _BIT_DEPTH,
        _COLOUR_TYPE_GREYSCALE,
        0,  # compression method: deflate, the only one PNG defines
        0,  # filter method: the only one PNG defines
        0,  # interlace: none
    )
    return b"".join(
        (
            _PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(raw, _COMPRESSION_LEVEL)),
            _chunk(b"IEND", b""),
        ),
    )


def png_data_uri(
    data: npt.NDArray[typing.Any],
    *,
    max_edge: int = TILE_MAX_EDGE,
    low_percentile: float = DEFAULT_LOW_PERCENTILE,
    high_percentile: float = DEFAULT_HIGH_PERCENTILE,
) -> str:
    """
    Encode a frame as a ``data:`` URI an ``<img>`` tag can carry.

    Inline rather than served from a URL, because the alternative is the
    notebook kernel running an HTTP endpoint for pixels that are already
    in the page's own render - and a tile that arrives separately from
    the status line beside it can show a different pass than the numbers
    claim.

    Parameters
    ----------
    data : npt.NDArray[typing.Any]
        The frame's pixels.
    max_edge : int
        Longest edge to decimate to before encoding.
    low_percentile : float
        Percentile mapped to black.
    high_percentile : float
        Percentile mapped to white.

    Returns
    -------
    str
        ``data:image/png;base64,...``.
    """
    encoded = base64.b64encode(
        greyscale_png(
            data,
            max_edge=max_edge,
            low_percentile=low_percentile,
            high_percentile=high_percentile,
        ),
    )
    return f"data:image/png;base64,{encoded.decode('ascii')}"
