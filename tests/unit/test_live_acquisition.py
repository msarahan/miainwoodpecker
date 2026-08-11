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


def test_stop_reports_success_when_the_worker_finishes():
    """The ordinary case: the grab returns, the worker joins, stop() says so."""
    loop = LiveAcquisition(lambda: _make_frame(0))
    loop.start()
    assert _wait_until(lambda: loop.stats.frame_count >= 1)
    assert loop.stop() is True
    assert not loop.is_running


def test_stop_reports_failure_while_a_grab_is_still_in_flight():
    """
    A grab that outlasts the timeout must not be reported as stopped.

    This is the contract callers depend on before touching the device
    themselves. stop() used to discard join()'s outcome and clear the
    thread handle unconditionally, so is_running went False while the
    worker was still inside the device call - and the recording path,
    trusting that, drove the same device from a second thread. With the
    client's shared-memory copy-out outside the connection lock, that
    overlap splices two frames together silently rather than raising.
    """
    release = threading.Event()
    entered = threading.Event()

    def slow_grab() -> Frame:
        entered.set()
        release.wait(_DEADLINE_S)
        return _make_frame(0)

    loop = LiveAcquisition(slow_grab)
    loop.start()
    try:
        assert entered.wait(_DEADLINE_S)
        assert loop.stop(timeout=0.05) is False
        # And it tells the truth afterwards, rather than claiming to be idle.
        assert loop.is_running
    finally:
        release.set()
        assert loop.stop() is True
    assert not loop.is_running


def test_stop_can_be_retried_after_reporting_failure():
    """A failed stop keeps the handle, so a later stop joins the same worker."""
    release = threading.Event()
    entered = threading.Event()

    def slow_grab() -> Frame:
        entered.set()
        release.wait(_DEADLINE_S)
        return _make_frame(0)

    loop = LiveAcquisition(slow_grab)
    loop.start()
    assert entered.wait(_DEADLINE_S)
    assert loop.stop(timeout=0.05) is False
    release.set()
    assert loop.stop(timeout=_DEADLINE_S) is True
    assert not loop.is_running


def test_stopping_a_loop_that_never_started_succeeds():
    """Nothing to join is a stopped loop, not a failure."""
    assert LiveAcquisition(lambda: _make_frame(0)).stop() is True


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
