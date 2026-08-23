"""
What the dashboard puts on screen, checked without a browser in the room.

These cover the two decisions a marimo cell would otherwise make
invisibly: which targets get a tile and in what order, and what a frame
looks like once it is 8-bit greyscale in a ``data:`` URI. Neither can be
exercised through marimo's runtime here - it is an optional dependency
the test environments do not install - which is exactly why both live in
:mod:`miainwoodpecker.dashboard` rather than in the notebook.

The PNG assertions decode the bytes rather than trusting them. A picture
that is subtly malformed still *looks* like it was produced, and a tile
that silently fails to render in one browser is the kind of thing nobody
reports for weeks.
"""

import datetime
import struct
import zlib

import numpy as np
import pytest

from miainwoodpecker.acquisition.live import LiveStats
from miainwoodpecker.broker.interface import (
    Lease,
    TargetDescription,
    TargetPreview,
    TargetState,
    TargetView,
)
from miainwoodpecker.dashboard.images import (
    TILE_MAX_EDGE,
    autoscale,
    decimate,
    greyscale_png,
    is_image,
    png_data_uri,
)
from miainwoodpecker.dashboard.tiles import (
    channel_labels,
    frame_sources,
    frame_tiles,
    lease_text,
    rate_text,
    tile_status,
)
from miainwoodpecker.devices.interface import Frame
from miainwoodpecker.devices.preview import preview_of

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR_AT = len(_PNG_SIGNATURE) + 8
_MAX_LEVEL = 255


def _frame(data: np.ndarray, **metadata: object) -> Frame:
    """
    Return a frame carrying the given pixels and metadata.

    Parameters
    ----------
    data : np.ndarray
        The pixels.
    **metadata : object
        Metadata keys to attach.

    Returns
    -------
    Frame
        The frame.
    """
    return Frame(
        data=data,
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata=metadata,
    )


def _png_size(encoded: bytes) -> tuple[int, int]:
    """
    Read a PNG's declared width and height out of its IHDR.

    Parameters
    ----------
    encoded : bytes
        The PNG file.

    Returns
    -------
    tuple[int, int]
        Width and height, in pixels.
    """
    width, height = struct.unpack(">II", encoded[_IHDR_AT : _IHDR_AT + 8])
    return (width, height)


def _png_levels(encoded: bytes, width: int, height: int) -> np.ndarray:
    """
    Decode a greyscale PNG's pixels back out of its IDAT chunk.

    Parameters
    ----------
    encoded : bytes
        The PNG file.
    width : int
        Its width, from :func:`_png_size`.
    height : int
        Its height.

    Returns
    -------
    np.ndarray
        The 8-bit levels, with each scanline's filter byte stripped.
    """
    marker = encoded.index(b"IDAT")
    length = struct.unpack(">I", encoded[marker - 4 : marker])[0]
    raw = zlib.decompress(encoded[marker + 4 : marker + 4 + length])
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(height, width + 1)
    # Column 0 is the per-scanline filter byte, which this encoder always
    # writes as 0 (no filter). Asserting that is asserting the format.
    assert not rows[:, 0].any()
    return rows[:, 1:]


def test_png_round_trips_through_a_real_decode():
    """A written PNG decodes back to the levels autoscale produced."""
    data = np.arange(16, dtype=np.uint16).reshape(4, 4)
    encoded = greyscale_png(data)
    assert encoded.startswith(_PNG_SIGNATURE)
    assert encoded.endswith(b"IEND\xae\x42\x60\x82")
    width, height = _png_size(encoded)
    assert (width, height) == (4, 4)
    np.testing.assert_array_equal(_png_levels(encoded, width, height), autoscale(data))


def test_a_single_hot_pixel_does_not_flatten_the_frame():
    """
    The percentile stretch keeps the specimen visible under a dead pixel.

    Scaled to the maximum instead, the 0-1 ramp below would span levels
    0 and 1 of 255 - a black tile with one white speck, which is what
    every real detector's hottest pixel would produce.
    """
    data = np.linspace(0.0, 1.0, 1024).reshape(32, 32)
    data[0, 0] = 1e6
    levels = autoscale(data)
    assert levels[-1, -1] == _MAX_LEVEL
    assert levels[16, 0] > _MAX_LEVEL // 4


def test_a_constant_frame_renders_black_rather_than_as_noise():
    """A blanked beam has no range, and inventing one would draw fiction."""
    assert not autoscale(np.full((8, 8), 7.0)).any()


def test_non_finite_pixels_do_not_blank_the_tile():
    """A NaN is drawn black; the pixels around it still stretch normally."""
    data = np.linspace(0.0, 1.0, 16).reshape(4, 4)
    data[0, 0] = np.nan
    levels = autoscale(data)
    assert levels[0, 0] == 0
    assert levels[-1, -1] == _MAX_LEVEL


def test_an_all_nan_frame_is_black_rather_than_an_exception():
    """A display path must not raise on a detector reporting nothing usable."""
    assert not autoscale(np.full((4, 4), np.nan)).any()


def test_decimation_bounds_the_longest_edge():
    """A 2048-pixel frame reaches the wire as at most a 512-pixel one."""
    reduced = decimate(np.zeros((2048, 1024)), TILE_MAX_EDGE)
    assert max(reduced.shape) <= TILE_MAX_EDGE


def test_a_small_frame_is_not_copied():
    """Nothing is spent decimating a frame that already fits."""
    data = np.zeros((16, 16))
    assert decimate(data, TILE_MAX_EDGE) is data


def test_a_data_uri_is_what_an_img_tag_takes():
    """The tile's src attribute is inline base64, not a URL to fetch."""
    uri = png_data_uri(np.zeros((4, 4)))
    assert uri.startswith("data:image/png;base64,")


def test_a_projected_readout_is_recognised_as_not_an_image():
    """
    A camera summing onto one axis delivers a spectrum, not a picture.

    An ordinary camera state rather than a corrupt frame, so a display
    has to be able to ask before it encodes - a one-pixel-high PNG would
    be a picture of nothing, and encoding it anyway would raise inside a
    poll loop.
    """
    assert is_image(np.zeros((4, 4)))
    assert not is_image(np.zeros(2048))
    with pytest.raises(ValueError, match="2D frame"):
        greyscale_png(np.zeros(2048))


@pytest.mark.parametrize(
    "shape",
    [(2048, 2048), (512, 512), (100, 1340), (513, 513), (257, 100), (16, 16)],
)
@pytest.mark.parametrize("edge", [512, 256, 128])
def test_decimating_early_draws_the_same_tile_as_decimating_late(shape, edge):
    """
    A preview renders to the same bytes the full frame would have.

    This is what makes it safe for the broker to reduce the pixels
    before sending them. If the two routes disagreed, a dashboard would
    show one picture in process and a different one over a socket, and
    the difference would be invisible until somebody compared them side
    by side.

    Byte-for-byte on the encoded PNG rather than approximately, because
    both routes take the *same* stride through the *same* array: the
    early one has simply done it already. Anything less than equality
    would mean a rounding difference between two implementations of
    "every nth pixel", which is the bug this shares one function to
    avoid.

    Parameters
    ----------
    shape : tuple[int, int]
        Frame size, including sizes the stride does not divide evenly.
    edge : int
        Longest edge to reduce to.
    """
    rng = np.random.default_rng(0)
    frame = _frame(rng.normal(size=shape).astype(np.float32), channel_name="HAADF")

    late = png_data_uri(frame.data, max_edge=edge)
    early = png_data_uri(preview_of(frame, edge).data, max_edge=edge)

    assert early == late


def test_a_preview_drops_the_calibration_it_would_otherwise_lie_about():
    """
    Units per pixel do not survive a stride, so they do not travel.

    A frame subsampled by 8 whose calibration came along unchanged
    claims a pixel size eight times too small. Every distance measured
    off it is then wrong by that factor, with nothing anywhere saying
    so - which is why a preview is not a ``Frame`` and why this key is
    named rather than filtered by guesswork.
    """
    frame = _frame(
        np.zeros((2048, 2048), dtype=np.float32),
        channel_name="HAADF",
        calibration={"x": "nm per pixel, at the full size"},
        fov_nm=100.0,
    )

    preview = preview_of(frame, 256)

    assert "calibration" not in preview.metadata
    # The detector's name describes the detector, not the pixel grid, so
    # it survives - it is what captions a multichannel tile.
    assert preview.metadata["channel_name"] == "HAADF"
    # So does the field of view: decimation subsamples the same region,
    # it does not crop it.
    assert preview.metadata["fov_nm"] == 100.0  # noqa: PLR2004
    assert preview.stride == 8  # noqa: PLR2004
    assert preview.source_shape == (2048, 2048)


def test_a_preview_of_a_projected_readout_is_a_preview_not_an_error():
    """
    A 1D frame reduces rather than raising, unlike ``decimate``.

    A camera in projected readout is an ordinary instrument state, and
    ``previews()`` covers every target at once - so raising on one would
    take the whole grid down. The preview says it is 1D by being 1D, and
    ``is_image`` is still the check a caller makes before encoding.
    """
    preview = preview_of(_frame(np.zeros(4096, dtype=np.float32)), 256)

    assert preview.data.shape == (256,)
    assert preview.stride == 16  # noqa: PLR2004
    assert not is_image(preview.data)


def test_a_tile_is_built_the_same_way_from_previews_as_from_frames():
    """
    Swapping the wire format does not change the grid or its chrome.

    ``frame_tiles`` reads a state and some pixels, and both view types
    carry exactly that. A dashboard moving to previews for the bandwidth
    must not find tiles reordered, captions lost or a rate missing.
    """
    described = _described()
    frames = (
        _frame(np.zeros((512, 512), dtype=np.float32), channel_name="HAADF"),
        _frame(np.zeros((512, 512), dtype=np.float32), channel_name="MAADF"),
    )
    state = TargetState(
        name="scanner",
        kind="scanner",
        is_live=True,
        stats=LiveStats(fps=12.0, frame_count=7),
    )

    from_frames = frame_tiles(described, {"scanner": TargetView(state, frames)})
    from_previews = frame_tiles(
        described,
        {
            "scanner": TargetPreview(
                state,
                tuple(preview_of(frame, 256) for frame in frames),
            ),
        },
    )

    assert [tile.name for tile in from_frames] == [
        tile.name for tile in from_previews
    ]
    scan_frames, scan_previews = from_frames[0], from_previews[0]
    assert scan_previews.is_live == scan_frames.is_live
    assert scan_previews.fps == scan_frames.fps
    assert channel_labels(scan_previews) == channel_labels(scan_frames)
    assert channel_labels(scan_previews) == ("HAADF", "MAADF")


def _described() -> dict[str, TargetDescription]:
    """
    Return a two-camera, one-scanner instrument as ``describe()`` reports it.

    Returns
    -------
    dict[str, TargetDescription]
        Keyed by target name, including the two kinds that must not get
        tiles.
    """
    return {
        "instrument": TargetDescription(
            name="instrument",
            kind="instrument",
            label="instrument",
            controls=("defocus",),
        ),
        "scanner": TargetDescription(
            name="scanner",
            kind="scanner",
            label="scan-unit-1",
            channel_names=("HAADF", "MAADF"),
        ),
        "eels_camera": TargetDescription(
            name="eels_camera",
            kind="camera",
            label="eels",
            binning_values=(1, 2, 4),
        ),
        "spectrum_detector": TargetDescription(
            name="spectrum_detector",
            kind="spectrum",
            label="edx",
        ),
    }


def _viewed(**states: TargetView) -> dict[str, TargetView]:
    """
    Return a snapshot mapping built from the states given.

    Parameters
    ----------
    **states : TargetView
        Target name to view.

    Returns
    -------
    dict[str, TargetView]
        The snapshot.
    """
    return dict(states)


def test_the_controls_are_built_from_describe_not_from_the_tiles():
    """
    The two are wanted at different rates, and mixing them wipes input.

    Tiles are rebuilt on every poll. A target dropdown or a detector
    checkbox built from them would be rebuilt once a second too, and
    would throw away whatever the operator had just chosen - so the
    controls come from ``describe()``, which does not change.
    """
    sources = frame_sources(_described())
    assert [description.name for description in sources] == [
        "scanner",
        "eels_camera",
    ]
    # Same answer whatever the snapshot says, which is the property.
    assert sources == frame_sources(_described())


def test_only_frame_sources_get_a_tile():
    """An instrument has controls and no pixels; a spectrum wants a plot."""
    tiles = frame_tiles(_described(), _viewed())
    assert [tile.name for tile in tiles] == ["scanner", "eels_camera"]


def test_tile_order_follows_describe_not_what_is_live():
    """A camera stopping must not move the tile an operator has learned."""
    described = _described()
    live_first = _viewed(
        eels_camera=TargetView(
            state=TargetState(name="eels_camera", kind="camera", is_live=True),
        ),
    )
    tiles = frame_tiles(described, live_first)
    assert [tile.name for tile in tiles] == ["scanner", "eels_camera"]


def test_a_target_missing_from_the_snapshot_still_gets_a_tile():
    """A hole in the grid is harder to read than a tile that explains itself."""
    tiles = frame_tiles(_described(), _viewed())
    assert tiles[0].error is not None
    assert not tiles[0].is_live


def test_the_description_supplies_the_controls_a_client_can_offer():
    """Detector checkboxes and a binning menu come from describe(), not a device."""
    scan, camera = frame_tiles(_described(), _viewed())
    assert scan.channel_names == ("HAADF", "MAADF")
    assert camera.binning_values == (1, 2, 4)


def test_your_own_lease_is_told_apart_from_someone_elses():
    """The two look identical from outside and mean opposite things."""
    held = Lease(
        lease_id="l1",
        targets=("scanner",),
        holder="notebook-2",
        reason="focal series",
        granted_at=0.0,
        expires_at=1.0,
    )
    viewed = _viewed(
        scanner=TargetView(
            state=TargetState(
                name="scanner",
                kind="scanner",
                is_live=False,
                lease=held,
            ),
        ),
    )
    mine = frame_tiles(_described(), viewed, holder="notebook-2")[0]
    theirs = frame_tiles(_described(), viewed, holder="viewer")[0]
    assert mine.held_by_me
    assert not theirs.held_by_me
    assert lease_text(mine) == "leased by you (focal series)"
    assert lease_text(theirs) == "leased by notebook-2 (focal series)"


def test_no_lease_says_nothing_at_all():
    """An unleased target is the ordinary case; a line about it would bury the rest."""
    tiles = frame_tiles(
        _described(),
        _viewed(
            scanner=TargetView(
                state=TargetState(name="scanner", kind="scanner", is_live=False),
            ),
        ),
    )
    assert lease_text(tiles[0]) == ""


def test_a_dead_loop_is_reported_before_anything_else():
    """The last frame is still on screen, so a stalled tile must say so."""
    viewed = _viewed(
        scanner=TargetView(
            state=TargetState(
                name="scanner",
                kind="scanner",
                is_live=False,
                error="camera timed out",
            ),
        ),
    )
    assert rate_text(frame_tiles(_described(), viewed)[0]) == (
        "stopped: camera timed out"
    )


def test_a_rate_of_zero_reads_as_measuring_not_as_stalled():
    """LiveStats reports 0.0 until two frames arrive; a stalled loop shows it too."""
    viewed = _viewed(
        scanner=TargetView(
            state=TargetState(
                name="scanner",
                kind="scanner",
                is_live=True,
                stats=LiveStats(frame_count=1, fps=0.0),
            ),
        ),
    )
    assert "measuring rate" in rate_text(frame_tiles(_described(), viewed)[0])


def test_a_running_loop_reports_its_rate_and_its_holder_together():
    """One status line answers both questions a tile's chrome is for."""
    viewed = _viewed(
        scanner=TargetView(
            state=TargetState(
                name="scanner",
                kind="scanner",
                is_live=True,
                stats=LiveStats(frame_count=431, fps=12.44),
                lease=Lease(
                    lease_id="l1",
                    targets=("scanner",),
                    holder="viewer",
                    reason="",
                    granted_at=0.0,
                    expires_at=1.0,
                ),
            ),
        ),
    )
    assert tile_status(frame_tiles(_described(), viewed)[0]) == (
        "live - 12.4 fps - 431 frames - leased by viewer"
    )


def test_frames_are_captioned_by_the_detector_that_produced_them():
    """A pass of channels 1 and 2 must not be labelled with channel 0's name."""
    viewed = _viewed(
        scanner=TargetView(
            state=TargetState(name="scanner", kind="scanner", is_live=True),
            frames=(
                _frame(np.zeros((2, 2)), channel_name="MAADF"),
                _frame(np.zeros((2, 2)), channel_name="BF"),
            ),
        ),
    )
    assert channel_labels(frame_tiles(_described(), viewed)[0]) == ("MAADF", "BF")


def test_a_frame_that_names_no_detector_falls_back_to_its_position():
    """An absent key means not reported, so no detector name is invented."""
    viewed = _viewed(
        eels_camera=TargetView(
            state=TargetState(name="eels_camera", kind="camera", is_live=True),
            frames=(_frame(np.zeros((2, 2))),),
        ),
    )
    tiles = frame_tiles(_described(), viewed)
    assert channel_labels(tiles[1]) == ("frame 0",)
