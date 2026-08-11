"""
The commodity-camera server's OpenCV backend, exercised without hardware.

A media file is a first-class input to that server — it is how a capture
is replayed as a fixture — and OpenCV opens one through exactly the same
``VideoCapture`` path a USB microscope goes through. So this covers the
real hardware code: the colour conversion, the exposure read-back, and
the end-of-stream failure that an unplugged camera also produces.

A separate module from ``test_camera_server.py`` on purpose. The skip
below is module-wide, and folding these in beside the simulated-backend
tests would have skipped *those* too wherever OpenCV is absent — which is
the base environment, and exactly where the simulated backend earns its
keep.

Install this project's ``camera`` extra to run it.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="requires the 'camera' extra")

from miainwoodpecker.devices.camera_server import CameraOpenError, open_camera
from miainwoodpecker.devices.remote import HARDWARE_BACKEND, remote_instrument

_SERVER_MODULE = "miainwoodpecker.devices.camera_server"

_CLIP_FRAMES = 6
_CLIP_SHAPE = (48, 64)  # (height, width), small enough to encode instantly


@pytest.fixture
def colour_clip(tmp_path):
    """Write a short colour video whose frames differ, and return its path."""
    path = tmp_path / "clip.avi"
    height, width = _CLIP_SHAPE
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"MJPG"),
        10.0,
        (width, height),
    )
    try:
        for index in range(_CLIP_FRAMES):
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            # A block that moves, so consecutive frames differ after the
            # greyscale conversion rather than only in colour.
            frame[:, index * 8 : index * 8 + 8] = 255
            writer.write(frame)
    finally:
        writer.release()
    return path


def test_the_opencv_backend_reads_a_file_and_says_what_it_did_to_colour(colour_clip):
    """
    A colour source arrives as a 2D frame, with the conversion named.

    ``Frame.data`` is 2D by design and the camera has already demosaiced,
    so there is no raw Bayer plane left to hand over. Recording *which*
    conversion was applied is what stops a reader guessing, and it is the
    difference between an honest greyscale frame and an unexplained one.
    """
    camera = open_camera(HARDWARE_BACKEND, str(colour_clip))
    try:
        camera.start()
        frame = camera.acquire_frame()
        second = camera.acquire_frame()
    finally:
        camera.stop()
        camera.close()

    expected_ndim = 2
    assert frame.data.ndim == expected_ndim
    assert frame.data.shape == _CLIP_SHAPE
    assert frame.metadata["colour_conversion"] == "bgr_to_grey"
    assert frame.metadata["photometrically_linear"] is False
    assert frame.metadata["device_id"].startswith("opencv:")
    assert frame.metadata["frame_index"] == 0
    assert second.metadata["frame_index"] == 1
    assert not np.array_equal(frame.data, second.data)


def test_the_opencv_backend_reports_a_source_that_ran_out(colour_clip):
    """
    Running off the end of a file fails the same way an unplugged camera does.

    Both are "the device returned no frame", and both must raise rather
    than hand back a stale or empty buffer — a frozen image that keeps
    arriving is the failure mode a viewer cannot see.
    """
    camera = open_camera(HARDWARE_BACKEND, str(colour_clip))
    try:
        camera.start()
        for _ in range(_CLIP_FRAMES):
            camera.acquire_frame()
        with pytest.raises(CameraOpenError, match="no frame"):
            camera.acquire_frame()
    finally:
        camera.close()


def test_the_opencv_backend_serves_over_ipc(colour_clip):
    """
    The hardware backend drives the real client, not just the class.

    Same path a USB microscope takes: spawn, describe, connect to the
    neutral camera target, acquire, shut down. The only difference is
    where the pixels come from.
    """
    with remote_instrument(
        server_module=_SERVER_MODULE,
        backend=HARDWARE_BACKEND,
        plugin_names=[str(colour_clip)],
    ) as devices:
        camera = devices.camera
        assert camera is not None
        camera.start()
        try:
            frame = camera.acquire_frame()
        finally:
            camera.stop()
        assert frame.data.shape == _CLIP_SHAPE
        assert frame.metadata["colour_conversion"] == "bgr_to_grey"


def test_closing_the_opencv_camera_twice_is_harmless(colour_clip):
    """Idempotent close, the same contract every adapter here holds."""
    camera = open_camera(HARDWARE_BACKEND, str(colour_clip))
    camera.close()
    camera.close()
