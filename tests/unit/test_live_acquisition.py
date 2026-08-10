"""Tests for the live acquisition loop (no Qt or napari involved)."""

import datetime
import threading
import time

import numpy as np

from miainwoodpecker.acquisition import LiveAcquisition
from miainwoodpecker.devices import Frame

_DEADLINE_S = 10.0


def _make_frame(index: int) -> Frame:
    """Return a small frame whose metadata records its sequence index."""
    return Frame(
        data=np.full((4, 4), index, dtype=np.float32),
        timestamp=datetime.datetime.now(tz=datetime.UTC),
        metadata={"index": index},
    )


def _wait_until(condition, deadline_s: float = _DEADLINE_S) -> bool:
    """Poll a condition until it is true or the deadline elapses."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


def test_loop_delivers_latest_frame_and_counts():
    """The loop grabs repeatedly and latest() reflects the newest frame."""
    counter = {"n": 0}

    def grab() -> Frame:
        counter["n"] += 1
        time.sleep(0.002)
        return _make_frame(counter["n"])

    loop = LiveAcquisition(grab)
    loop.start()
    try:
        min_frames = 3
        assert _wait_until(lambda: loop.stats.frame_count >= min_frames)
        frame = loop.latest()
        assert frame is not None
        assert frame.metadata["index"] >= min_frames
    finally:
        loop.stop()
    assert not loop.is_running
    assert loop.error is None


def test_stop_is_idempotent_and_start_restarts():
    """stop() can be called twice, and start() after stop() runs a fresh loop."""
    loop = LiveAcquisition(lambda: _make_frame(0))
    loop.start()
    assert _wait_until(lambda: loop.stats.frame_count >= 1)
    loop.stop()
    loop.stop()
    assert not loop.is_running

    loop.start()
    try:
        assert _wait_until(lambda: loop.stats.frame_count >= 1)
        assert loop.is_running
    finally:
        loop.stop()


def test_grab_error_stops_the_loop_and_is_exposed():
    """An exception from grab() halts the loop and surfaces via .error."""
    boom = RuntimeError("detector unplugged")
    delivered = threading.Event()

    def grab() -> Frame:
        if delivered.is_set():
            raise boom
        delivered.set()
        return _make_frame(1)

    loop = LiveAcquisition(grab)
    loop.start()
    try:
        assert _wait_until(lambda: loop.error is not None)
        assert loop.error is boom
        assert _wait_until(lambda: not loop.is_running)
        frame = loop.latest()
        assert frame is not None
        assert frame.metadata["index"] == 1
    finally:
        loop.stop()


def test_fps_is_reported_after_two_frames():
    """The fps estimate becomes positive once at least two frames arrive."""
    loop = LiveAcquisition(lambda: _make_frame(0))
    loop.start()
    try:
        min_frames = 2
        assert _wait_until(lambda: loop.stats.frame_count >= min_frames)
        assert _wait_until(lambda: loop.stats.fps > 0.0)
    finally:
        loop.stop()
