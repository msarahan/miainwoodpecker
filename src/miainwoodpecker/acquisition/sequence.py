"""
Acquisition sequences: bounded series of frames from a device.

These are plain generators over the vendor-neutral device interfaces, so
they compose directly with :mod:`miainwoodpecker.storage.nexus` — the
recording helper streams frames to disk as they arrive rather than
collecting them in memory first, which is what makes long series
practical.

Generators are lazy, so a caller can stop early (``itertools.islice``,
``break``) and the device is still released correctly.
"""

from __future__ import annotations

import dataclasses
import typing

from miainwoodpecker.storage.nexus import write_frames

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Iterable, Iterator

    from miainwoodpecker.devices.interface import Camera, Frame, ScanParameters, Scanner


def scan_series(
    scanner: Scanner,
    parameters: ScanParameters,
    count: int,
    *,
    channel: int = 0,
) -> Iterator[Frame]:
    """
    Yield ``count`` scanned frames from one detector channel.

    Parameters
    ----------
    scanner : Scanner
        The scan device to drive.
    parameters : ScanParameters
        Scan geometry and timing, applied to every frame in the series.
    count : int
        Number of frames to acquire.
    channel : int
        Detector channel index.

    Yields
    ------
    Frame
        Each scanned frame, in acquisition order.

    Raises
    ------
    ValueError
        If ``count`` is negative.
    """
    if count < 0:
        msg = f"count must be non-negative, got {count}"
        raise ValueError(msg)
    for _ in range(count):
        yield scanner.scan_frame(parameters, channel)


def camera_series(camera: Camera, count: int) -> Iterator[Frame]:
    """
    Yield ``count`` camera frames, starting and stopping the camera.

    The camera is stopped even if the consumer abandons the generator
    early, so an interrupted series does not leave it acquiring.

    Parameters
    ----------
    camera : Camera
        The camera to acquire from.
    count : int
        Number of frames to acquire.

    Yields
    ------
    Frame
        Each acquired frame, in acquisition order.

    Raises
    ------
    ValueError
        If ``count`` is negative.
    """
    if count < 0:
        msg = f"count must be non-negative, got {count}"
        raise ValueError(msg)
    camera.start()
    try:
        for _ in range(count):
            yield camera.acquire_frame()
    finally:
        camera.stop()


def focal_series(
    scanner: Scanner,
    parameters: ScanParameters,
    fov_values_nm: Iterable[float],
    *,
    channel: int = 0,
) -> Iterator[Frame]:
    """
    Yield one scanned frame per field-of-view value.

    A stand-in for the parameter-sweep shape that real focal and tilt
    series need. Sweeping focus or stage tilt requires instrument controls
    that the Phase 1 device interface deliberately does not expose yet;
    field of view is the one sweepable axis available today, and the
    generator shape is what will carry over.

    Parameters
    ----------
    scanner : Scanner
        The scan device to drive.
    parameters : ScanParameters
        Base scan settings; its ``fov_nm`` is replaced per step.
    fov_values_nm : Iterable[float]
        Field-of-view values to step through, in nanometres.
    channel : int
        Detector channel index.

    Yields
    ------
    Frame
        One frame per requested field of view.
    """
    for fov_nm in fov_values_nm:
        stepped = dataclasses.replace(parameters, fov_nm=fov_nm)
        yield scanner.scan_frame(stepped, channel)


def record(
    frames: Iterable[Frame],
    path: os.PathLike[str] | str,
    **kwargs: object,
) -> int:
    """
    Stream frames to a NeXus HDF5 file as they are produced.

    Parameters
    ----------
    frames : Iterable[Frame]
        Frames to record, typically one of the series generators above.
    path : os.PathLike[str] | str
        Destination HDF5 file.
    **kwargs : object
        Passed through to :class:`~miainwoodpecker.storage.nexus.NexusWriter`.

    Returns
    -------
    int
        The number of frames recorded.
    """
    return write_frames(path, frames, **kwargs)
