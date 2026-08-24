"""
Unit tests: watching a synchronised pass build, without waiting for it.

No display and no device: :class:`PassPreview` wraps a destination and is
a destination, so a plain NumPy array stands in for the file and the
tests write into it the way an adapter does.

The property that matters most here is the one that is easy to lose: the
data is written through **first and in full**, and the preview is formed
from it afterwards. A progress view that could alter or drop what lands
on disk would be a much worse bug than having no progress view.
"""

import numpy as np
import pytest

from miainwoodpecker.viewer.progress import PassPreview, previews

_GRID = (8, 6)
_CHANNELS = 32


@pytest.fixture
def destination():
    """
    Return a spectrum-image-shaped destination.

    Returns
    -------
    np.ndarray
        A ``(8, 6, 32)`` cube of zeros, as a writer would allocate.
    """
    return np.zeros((*_GRID, _CHANNELS), dtype=np.float32)


def test_the_data_is_written_through_untouched(destination):
    """
    What lands in the destination is exactly what the device wrote.

    The first thing to pin, because everything else here is a
    convenience and this is the acquisition.
    """
    preview = PassPreview(destination)
    spectrum = np.arange(_CHANNELS, dtype=np.float32)

    preview[3, 2] = spectrum

    assert np.array_equal(destination[3, 2], spectrum)


def test_the_map_fills_as_positions_arrive(destination):
    """
    The map is readable mid-pass, and says how far along it is.

    This is the whole feature: an operator can see a spectrum image
    forming instead of a status line and a wait.
    """
    preview = PassPreview(destination)
    assert preview.positions == 0
    assert preview.total == _GRID[0] * _GRID[1]

    for row in range(4):
        for column in range(_GRID[1]):
            preview[row, column] = np.full(_CHANNELS, row + 1, dtype=np.float32)

    assert preview.positions == 4 * _GRID[1]
    # The visited rows carry signal and the rest are still zero.
    assert np.all(preview.map[:4] > 0)
    assert np.all(preview.map[4:] == 0)


def test_the_map_is_a_virtual_detector_image(destination):
    """
    Each position's value is the signal summed there.

    Which is what makes the map worth looking at rather than a progress
    bar: it is the same image a virtual bright-field detector forms, so
    contamination, drift or vacuum are visible in it.
    """
    preview = PassPreview(destination)
    preview[0, 0] = np.ones(_CHANNELS, dtype=np.float32)
    preview[0, 1] = np.full(_CHANNELS, 3.0, dtype=np.float32)

    assert preview.map[0, 1] == pytest.approx(3 * preview.map[0, 0])


def test_the_map_is_one_array_reused(destination):
    """
    The same array every time, so a viewer can be handed it once.

    Reallocating it would leave the display drawing an array nobody is
    writing to any more, and the map would freeze at whatever it held.
    """
    preview = PassPreview(destination)
    first = preview.map
    preview[1, 1] = np.ones(_CHANNELS, dtype=np.float32)

    assert preview.map is first


def test_the_limits_cover_only_what_has_been_visited(destination):
    """
    The display range ignores the positions the probe has not reached.

    Stretching over the whole map would stretch over its zeros, which
    early in a pass are nearly all of it — and every real value would be
    crushed to the top of the range, showing a white rectangle growing
    rather than an image forming.
    """
    preview = PassPreview(destination)
    assert preview.limits is None

    preview[0, 0] = np.full(_CHANNELS, 10.0, dtype=np.float32)
    # One value so far: no range to stretch over, and saying so beats
    # returning a degenerate one.
    assert preview.limits is None

    preview[0, 1] = np.full(_CHANNELS, 20.0, dtype=np.float32)
    low, high = preview.limits
    assert low < high
    assert low == pytest.approx(preview.map[0, 0])
    assert high == pytest.approx(preview.map[0, 1])


def test_a_large_readout_is_summarised_by_subsampling():
    """
    A big diffraction pattern does not cost a full sum per position.

    At thousands of positions a second a quarter-million adds each would
    start to show. Subsampling changes the map's absolute scale and not
    its structure, and structure is the question a progress view answers.
    """
    cube = np.zeros((2, 2, 512, 512), dtype=np.float32)
    preview = PassPreview(cube)

    preview[0, 0] = np.ones((512, 512), dtype=np.float32)

    # Written through in full regardless of how it was summarised.
    assert cube[0, 0].sum() == pytest.approx(512 * 512)
    # And summarised from a sample, so the map value is far below it.
    assert 0 < preview.map[0, 0] < 512 * 512


def test_the_preview_is_transparent_to_the_device(destination):
    """
    An adapter can ask it whatever it would ask the real destination.

    ``ndim`` and ``dtype`` both came up the first time this ran against
    the real writer, so the proxy forwards rather than implementing a
    list of guesses.
    """
    preview = PassPreview(destination)

    assert preview.shape == destination.shape
    assert preview.ndim == destination.ndim
    assert preview.dtype == destination.dtype
    assert len(preview) == len(destination)


def test_a_write_the_preview_cannot_summarise_still_lands(destination):
    """
    An unexpected index costs the preview, never the acquisition.

    The write goes through before the summary is attempted, so an
    adapter that indexes some way this did not anticipate stores its data
    correctly and merely fails to appear in the progress map.
    """
    preview = PassPreview(destination)

    preview[2] = np.ones((_GRID[1], _CHANNELS), dtype=np.float32)

    assert np.all(destination[2] == 1.0)
    assert np.all(preview.map == 0)


def test_wrapping_a_whole_pass_keeps_its_target_names(destination):
    """The mapping handed to scan_synchronised keeps its shape."""
    wrapped = previews({"eels_camera": destination})

    assert set(wrapped) == {"eels_camera"}
    assert isinstance(wrapped["eels_camera"], PassPreview)
    assert wrapped["eels_camera"].shape == destination.shape


def test_the_last_spectrum_written_is_kept_with_its_position(destination):
    """
    The map's blind spot, covered from the same write.

    A virtual-detector image is one number per position, so a
    spectrometer parked off the edge of the loss produces a map that
    looks exactly like a good acquisition. The spectrum is where that is
    visible, and it is available mid-pass for the same reason the map is.
    """
    preview = PassPreview(destination)
    spectrum = np.arange(_CHANNELS, dtype=np.float32)

    assert preview.latest_spectrum is None
    preview[5, 1] = spectrum

    position, kept = preview.latest_spectrum
    assert position == (5, 1)
    assert np.array_equal(kept, spectrum)


def test_the_kept_spectrum_is_a_copy_of_what_was_written(destination):
    """
    A buffer the adapter reuses cannot change what a display drew.

    An adapter is free to write out of one scratch array — nothing in
    the destination contract forbids it, because the destination has
    copied the values by the time it returns. A preview holding the
    array itself would show whatever the next position overwrote it
    with, one frame late and attributed to the wrong pixel.
    """
    preview = PassPreview(destination)
    scratch = np.ones(_CHANNELS, dtype=np.float32)

    preview[0, 0] = scratch
    scratch[:] = 99.0

    _, kept = preview.latest_spectrum
    assert np.all(kept == 1.0)


def test_a_diffraction_pass_keeps_no_spectrum():
    """
    Nothing 1D is written, so there is nothing to say it saw.

    Reported by being absent rather than by the display checking which
    readout mode the operator set: the rank of what arrives is a fact,
    and a 512x512 pattern copied per position would cost a gigabyte a
    second to feed a curve that could not be drawn from it anyway.
    """
    cube = np.zeros((2, 2, 64, 64), dtype=np.float32)
    preview = PassPreview(cube)

    preview[0, 0] = np.ones((64, 64), dtype=np.float32)

    assert preview.latest_spectrum is None
