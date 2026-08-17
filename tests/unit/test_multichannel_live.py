"""
Unit tests for the multi-channel live loop.

The property worth protecting is that a displayed set of channels comes
from *one* pass. Two single-channel loops would look identical on screen
while costing twice the dose and letting the specimen drift between the
images — so the tests below are mostly about the loop asking the device
once and publishing its frames together.
"""

import datetime
import threading
import time

import numpy as np
import pytest

from miainwoodpecker.acquisition import MultiChannelLiveAcquisition
from miainwoodpecker.devices import Frame

_DEADLINE_S = 5.0
_TWO_CHANNELS = 2


def _frame(value: float, channel: int) -> Frame:
    """
    Return a frame tagged with its channel and a caller-chosen value.

    Parameters
    ----------
    value : float
        Fills the frame, so a test can tell passes apart.
    channel : int
        Recorded in the metadata.

    Returns
    -------
    Frame
        The frame.
    """
    return Frame(
        data=np.full((2, 2), value, dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={"channel_index": channel},
    )


class _PassCountingScanner:
    """Records how many passes it was asked for, and numbers them."""

    def __init__(self, channels: int = _TWO_CHANNELS) -> None:
        self.passes = 0
        self._channels = channels
        self._lock = threading.Lock()

    def grab(self) -> list[Frame]:
        """
        Return one pass's frames, all stamped with the pass number.

        Returns
        -------
        list[Frame]
            One frame per channel.
        """
        with self._lock:
            self.passes += 1
            number = self.passes
        return [_frame(number, channel) for channel in range(self._channels)]


def _wait_for(condition, deadline_s: float = _DEADLINE_S) -> bool:
    """
    Poll a condition until it holds or the deadline passes.

    Parameters
    ----------
    condition : Callable[[], bool]
        What to wait for.
    deadline_s : float
        How long to wait.

    Returns
    -------
    bool
        Whether the condition held.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


def test_one_grab_yields_every_channel():
    """A pass publishes all its channels, not one."""
    scanner = _PassCountingScanner()
    loop = MultiChannelLiveAcquisition(scanner.grab)
    loop.start()
    try:
        assert _wait_for(lambda: len(loop.latest_frames()) == _TWO_CHANNELS)
    finally:
        loop.stop()


def test_the_published_channels_come_from_the_same_pass():
    """
    The set is replaced atomically, so no consumer sees a mixed pair.

    A display showing HAADF from this pass beside MAADF from the last
    would be differencing images taken at different probe positions,
    which is the failure this class exists to prevent.
    """
    scanner = _PassCountingScanner()
    loop = MultiChannelLiveAcquisition(scanner.grab)
    loop.start()
    try:
        assert _wait_for(lambda: scanner.passes > 2)  # noqa: PLR2004
        for _ in range(50):
            frames = loop.latest_frames()
            if not frames:
                continue
            values = {float(np.asarray(frame.data).flat[0]) for frame in frames}
            assert len(values) == 1, f"frames from different passes: {values}"
    finally:
        loop.stop()


def test_channels_stay_in_request_order():
    """Order is the caller's, so a layer cannot be bound to the wrong detector."""
    scanner = _PassCountingScanner()
    loop = MultiChannelLiveAcquisition(scanner.grab)
    loop.start()
    try:
        assert _wait_for(lambda: len(loop.latest_frames()) == _TWO_CHANNELS)
        indices = [
            frame.metadata["channel_index"] for frame in loop.latest_frames()
        ]
        assert indices == [0, 1]
    finally:
        loop.stop()


def test_latest_returns_the_first_channel():
    """
    The single-frame accessor keeps working, and is not arbitrary.

    Code that only wants "a frame from the scan" — a size estimate, a
    saved snapshot — behaves as it did before this class existed.
    """
    scanner = _PassCountingScanner()
    loop = MultiChannelLiveAcquisition(scanner.grab)
    loop.start()
    try:
        assert _wait_for(lambda: loop.latest() is not None)
        assert loop.latest().metadata["channel_index"] == 0
    finally:
        loop.stop()


def test_nothing_is_published_before_the_first_pass():
    """A consumer polling early gets nothing rather than a partial set."""
    loop = MultiChannelLiveAcquisition(list)
    assert loop.latest_frames() == ()
    assert loop.latest() is None


def test_the_rate_counts_passes_not_frames():
    """
    Enabling a second detector must not appear to halve the scan rate.

    The number an operator reads as "how fast is the scan going" is
    passes per second, and a second channel read out of the same pass
    costs no extra time.
    """
    scanner = _PassCountingScanner(channels=4)
    loop = MultiChannelLiveAcquisition(scanner.grab)
    loop.start()
    try:
        assert _wait_for(lambda: loop.stats.frame_count > 2)  # noqa: PLR2004
        assert loop.stats.frame_count <= scanner.passes
    finally:
        loop.stop()


def test_a_failing_grab_stops_the_loop_and_is_reported():
    """Same contract as the single-channel loop: the error reaches the caller."""
    def explode() -> list[Frame]:
        msg = "the scanner fell over"
        raise RuntimeError(msg)

    loop = MultiChannelLiveAcquisition(explode)
    loop.start()
    try:
        assert _wait_for(lambda: loop.error is not None)
        assert isinstance(loop.error, RuntimeError)
    finally:
        loop.stop()


@pytest.mark.parametrize("channels", [1, 3])
def test_any_channel_count_works(channels):
    """One detector and three are the same code path."""
    scanner = _PassCountingScanner(channels=channels)
    loop = MultiChannelLiveAcquisition(scanner.grab)
    loop.start()
    try:
        assert _wait_for(lambda: len(loop.latest_frames()) == channels)
    finally:
        loop.stop()
