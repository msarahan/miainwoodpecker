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

from miainwoodpecker.devices.interface import DEFOCUS_CONTROL, ENERGY_OFFSET_CONTROL
from miainwoodpecker.storage.nexus import write_frames

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Iterable, Iterator

    from miainwoodpecker.devices.interface import (
        Camera,
        Frame,
        InstrumentController,
        ScanParameters,
        Scanner,
    )


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
    values_nm: Iterable[float],
    *,
    channel: int = 0,
    instrument: InstrumentController | None = None,
) -> Iterator[Frame]:
    """
    Yield one scanned frame per swept value, sweeping defocus or field of view.

    Without an ``instrument``, this sweeps ``parameters.fov_nm`` — the
    only axis the Phase 1 device interface could reach, and what existing
    callers get, unchanged. With one, it sweeps real defocus and leaves
    the field of view alone: the instrument's defocus is set, a frame is
    scanned, and the *read-back* defocus (not the requested value) is
    recorded in the frame's metadata, so a recording says what the
    instrument did rather than what it was asked to do.

    The original defocus is restored on the way out, including when the
    consumer abandons the generator early — the same discipline
    ``camera_series`` applies to leaving a camera running.

    Parameters
    ----------
    scanner : Scanner
        The scan device to drive.
    parameters : ScanParameters
        Base scan settings. Its ``fov_nm`` is replaced per step when
        sweeping field of view, and used unchanged when sweeping defocus.
    values_nm : Iterable[float]
        Values to step through, in nanometres: fields of view without an
        ``instrument``, defocus values with one.
    channel : int
        Detector channel index.
    instrument : InstrumentController | None
        Instrument to sweep defocus on. ``None`` keeps the historical
        field-of-view sweep.

    Yields
    ------
    Frame
        One frame per requested value.

    Raises
    ------
    ValueError
        If ``instrument`` is given but does not implement defocus.
    """
    if instrument is None:
        for fov_nm in values_nm:
            stepped = dataclasses.replace(parameters, fov_nm=fov_nm)
            yield scanner.scan_frame(stepped, channel)
        return
    if DEFOCUS_CONTROL not in instrument.available_controls():
        msg = (
            "instrument does not implement the "
            f"{DEFOCUS_CONTROL!r} control, so defocus cannot be swept"
        )
        raise ValueError(msg)
    original_defocus_nm = instrument.defocus_nm()
    try:
        for defocus_nm in values_nm:
            instrument.set_defocus_nm(defocus_nm)
            frame = scanner.scan_frame(parameters, channel)
            yield dataclasses.replace(
                frame,
                metadata={
                    **frame.metadata,
                    "defocus_nm": instrument.defocus_nm(),
                    "requested_defocus_nm": defocus_nm,
                },
            )
    finally:
        instrument.set_defocus_nm(original_defocus_nm)


def energy_offset_series(
    camera: Camera,
    values_ev: Iterable[float],
    *,
    instrument: InstrumentController,
) -> Iterator[Frame]:
    """
    Yield one camera frame per spectrometer energy offset.

    The EELS counterpart to :func:`focal_series`, and the same shape: set
    a control, acquire, record the *read-back* value beside the requested
    one, and restore the original on the way out — including when the
    consumer abandons the generator early.

    This is the sweep behind Nion's ``MultipleShiftEELSAcquire``: step the
    energy offset across a series of frames so a spectrum wider than the
    detector can be assembled, or so a drifting zero-loss peak can be
    corrected for afterwards. Summing and cross-correlating the result is
    deliberately left to the analysis side, where HyperSpy already does
    it — this produces the series and records what it was taken at.

    Unlike ``focal_series``, the simulator can actually demonstrate this:
    stepping the offset visibly moves the zero-loss peak across the
    detector, and the energy axis the frames carry follows it, because
    the same control feeds the camera's calibration.

    **The camera is stopped around each step, and that is load-bearing.**
    A running camera is producing frames continuously, so the first one
    returned after a control changes was generated *before* it —
    measured against the simulator: acquiring straight after setting the
    offset yields the previous offset's spectrum, while its metadata and
    energy axis describe the new one. A series built that way is
    mislabelled by one step throughout, which is worse than a slow one.
    Stopping, setting, and restarting makes the very next frame correct,
    also measured. Nion's ``MultipleShiftEELSAcquire`` takes a settling
    delay between shifts for the same underlying reason.

    Parameters
    ----------
    camera : Camera
        The spectrometer camera to acquire from.
    values_ev : Iterable[float]
        Energy offsets to step through, in electronvolts.
    instrument : InstrumentController
        Instrument carrying the energy-offset control. Required, since
        there is no meaningful fallback sweep the camera alone could do.

    Yields
    ------
    Frame
        One frame per requested offset, with ``energy_offset_ev`` (read
        back) and ``requested_energy_offset_ev`` in its metadata.

    Raises
    ------
    ValueError
        If ``instrument`` does not implement the energy-offset control.
    """
    if ENERGY_OFFSET_CONTROL not in instrument.available_controls():
        msg = (
            "instrument does not implement the "
            f"{ENERGY_OFFSET_CONTROL!r} control, so the energy offset "
            f"cannot be swept"
        )
        raise ValueError(msg)
    original_offset_ev = instrument.energy_offset_ev()
    try:
        for offset_ev in values_ev:
            instrument.set_energy_offset_ev(offset_ev)
            camera.start()
            try:
                frame = camera.acquire_frame()
            finally:
                camera.stop()
            yield dataclasses.replace(
                frame,
                metadata={
                    **frame.metadata,
                    "energy_offset_ev": instrument.energy_offset_ev(),
                    "requested_energy_offset_ev": offset_ev,
                },
            )
    finally:
        instrument.set_energy_offset_ev(original_offset_ev)


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
