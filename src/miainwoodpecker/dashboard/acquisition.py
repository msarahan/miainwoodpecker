"""
Taking a lease from a notebook without freezing the notebook.

**The whole reason this module exists is one sentence in
:mod:`miainwoodpecker.broker.interface`: taking a lease can block for as
long as a scan pass.** A lease stops each target's live loop and waits
for the pass already in flight, and a pass is ``height x width x dwell``
- 262 ms at 512x512 and one microsecond, 42 seconds at 2048x2048 and
ten, nearly three minutes at 4096x4096. A marimo cell that took a lease
inline would hold the kernel for that long: no tile would refresh, no
control would respond, and the app would look hung at precisely the
moment an operator most wants to see what the instrument is doing.

So the lease is taken on a worker, exactly as the Qt viewer takes its
own (:meth:`~miainwoodpecker.viewer.live.LiveInstrumentWidget._leased_frames`),
and the notebook learns how it went by polling - which it is already
doing, once a second, to draw the tiles. There is no new mechanism here:
:class:`~miainwoodpecker.jobs.BackgroundJob` is the project's one shape
for "run this off the display thread and tell me how it went", and this
is a fourth subclass of it.

**No new acquisition verbs.** The lease yields the device layer's own
:class:`~miainwoodpecker.devices.interface.Scanner` and
:class:`~miainwoodpecker.devices.interface.Camera` protocols, and what
runs inside it is one of
:mod:`miainwoodpecker.acquisition`'s ordinary generators. The request
factories below choose *which* generator and with what arguments; they
do not wrap one. A dashboard that grew its own acquisition call would be
a second acquisition API to keep in step with the first, which is the
thing the broker's own docstring refuses to do and for the same reason.

**The lease is renewed per frame, not granted for a guessed duration.** A
hundred-frame series outlives any fixed time to live, and a kernel that
dies mid-series stops renewing - which lets the broker reclaim the
instrument and restart the loops, which is what a time to live is for.
"""

from __future__ import annotations

import datetime
import time
import typing
from dataclasses import dataclass

from miainwoodpecker.acquisition.sequence import (
    camera_image,
    camera_series,
    multichannel_scan_series,
)
from miainwoodpecker.dashboard.session_log import SessionLogEntry, describe_frames
from miainwoodpecker.jobs import BackgroundJob

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from miainwoodpecker.broker.interface import InstrumentBroker, LeasedDevices
    from miainwoodpecker.dashboard.session_log import SessionLog
    from miainwoodpecker.devices.interface import (
        CameraParameters,
        Frame,
        ScanParameters,
    )
    from miainwoodpecker.storage.session import Session


@dataclass(frozen=True)
class AcquisitionRequest:
    """
    What one Acquire press should do, decided before any device is touched.

    Built on the notebook's thread from the controls on screen, and
    consumed on the worker. Everything in it is plain data plus one
    callable, so the thread that builds it does no device work and the
    thread that runs it reads no widgets - the same split the viewer's
    thread-safety contract states.

    Attributes
    ----------
    targets : tuple[str, ...]
        The targets to lease, named together rather than in nested
        leases. The broker acquires them in its own
        :data:`~miainwoodpecker.broker.interface.LEASE_ORDER`, which is
        what stops two clients asking for one pair in opposite orders
        from deadlocking.
    label : str
        What is being acquired, slugified into a session filename and
        shown in the log.
    reason : str
        Free text every other client sees while the lease is held. Worth
        filling in: it is the difference between an operator elsewhere
        reading "busy" and reading "energy series, 5 steps".
    build : Callable[[LeasedDevices], Iterable[Frame]]
        Turns the leased devices into a frame series. Called on the
        worker, inside the lease.
    """

    targets: tuple[str, ...]
    label: str
    reason: str
    build: Callable[[LeasedDevices], Iterable[Frame]]


def scan_request(
    target: str,
    *,
    parameters: ScanParameters,
    channels: Sequence[int],
    channel_names: Sequence[str],
    count: int = 1,
) -> AcquisitionRequest:
    """
    Acquire scan frames, every enabled detector out of each pass.

    :func:`~miainwoodpecker.acquisition.sequence.multichannel_scan_series`
    rather than a series per detector, because a scanned instrument reads
    every enabled detector out as the probe goes past: two separate
    scans would be twice the dose, twice the time, and two images the
    specimen had drifted between. One pass, and the frames carry a shared
    ``scan_pass_id`` saying so.

    Parameters
    ----------
    target : str
        The scan unit's target name.
    parameters : ScanParameters
        Geometry and dwell for each pass.
    channels : Sequence[int]
        Detector indices to read out, as the checkboxes have them.
    channel_names : Sequence[str]
        Their names, for the label - so a recording is called
        ``scan-HAADF-MAADF`` rather than ``scan-0-1``.
    count : int
        Passes to acquire.

    Returns
    -------
    AcquisitionRequest
        The request, ready to hand to an :class:`AcquisitionJob`.
    """
    wanted = list(channels)
    named = "-".join(channel_names) or "scan"
    return AcquisitionRequest(
        targets=(target,),
        label=f"scan-{named}",
        reason=(
            f"{count} scan pass(es), {parameters.width}x{parameters.height} "
            f"at {parameters.pixel_time_us:g} us, detectors: {named}"
        ),
        build=lambda leased: multichannel_scan_series(
            leased.scanner(target),
            parameters,
            count,
            channels=wanted,
        ),
    )


def camera_request(
    target: str,
    *,
    parameters: CameraParameters | None = None,
    count: int = 1,
) -> AcquisitionRequest:
    """
    Acquire camera frames, at chosen settings for one and live ones for many.

    The split follows the two generators the acquisition layer actually
    has, and mirrors what the Qt viewer's camera group offers.
    :func:`~miainwoodpecker.acquisition.sequence.camera_image` applies an
    exposure and binning of the operator's choosing and **puts the live
    settings back afterwards**, so taking one long unbinned image does
    not leave the feed crawling.
    :func:`~miainwoodpecker.acquisition.sequence.camera_series`
    deliberately touches no settings at all.

    So more than one frame runs at whatever the camera is currently set
    to, and this function refuses to pretend otherwise. Configuring the
    camera and then running a series would acquire correctly and leave
    the live view at acquisition settings on release - and the only
    honest fix is a generator in
    :mod:`miainwoodpecker.acquisition` that configures, yields N, and
    restores, which belongs there rather than invented here.

    Parameters
    ----------
    target : str
        The camera's target name.
    parameters : CameraParameters | None
        Exposure and binning for a single image. ``readout`` on it is
        replaced with the camera's current mode when the acquisition
        runs: an image acquisition is not the place to switch a
        spectrometer between imaging and projecting.
    count : int
        Frames to acquire.

    Returns
    -------
    AcquisitionRequest
        The request, ready to hand to an :class:`AcquisitionJob`.

    Raises
    ------
    ValueError
        If settings were given for a multi-frame acquisition, which
        would silently be ignored - see above for why the alternative is
        not to ignore them quietly.
    """
    if count == 1 and parameters is not None:
        return AcquisitionRequest(
            targets=(target,),
            label="camera-image",
            reason=(
                f"one camera image, {parameters.exposure_ms:g} ms, "
                f"binning {parameters.binning}"
            ),
            build=lambda leased: _configured_image(leased, target, parameters),
        )
    if parameters is not None:
        message = (
            f"camera settings were given for {count} frames, and "
            f"camera_series does not apply any - acquire one frame to use "
            f"them, or clear them to run at the camera's live settings"
        )
        raise ValueError(message)
    return AcquisitionRequest(
        targets=(target,),
        label="camera",
        reason=f"{count} camera frames at the live settings",
        build=lambda leased: camera_series(leased.camera(target), count),
    )


def _configured_image(
    leased: LeasedDevices,
    target: str,
    parameters: CameraParameters,
) -> Iterable[Frame]:
    """
    Acquire one image, keeping the camera's own readout mode.

    Parameters
    ----------
    leased : LeasedDevices
        The devices this lease holds.
    target : str
        The camera's target name.
    parameters : CameraParameters
        Exposure and binning to use.

    Returns
    -------
    Iterable[Frame]
        The single-frame series.
    """
    import dataclasses  # noqa: PLC0415 - one call, in the only place it is used

    camera = leased.camera(target)
    # Read inside the lease, because it is a device call: the readout
    # mode is the camera's own state and an image acquisition must not
    # change it.
    wanted = dataclasses.replace(parameters, readout=camera.parameters().readout)
    return camera_image(camera, wanted)


class AcquisitionJob(BackgroundJob):
    """
    Run one acquisition under a lease, on a worker, and log what happened.

    The lease is taken **inside** :meth:`_work`, on this job's own
    thread, which is the whole point - see the module docstring.

    Both outcomes reach the log. A successful acquisition appends an
    entry with a thumbnail and the first frame's metadata; a refusal
    appends the broker's own sentence and re-raises, so the notebook's
    status line has an error to show and the log has the refusal on
    record. Appending here rather than in the notebook is deliberate: it
    happens exactly once per job whatever the display cell does, and a
    reactive cell that appended on re-render would log the same
    acquisition every time the page ticked.

    Parameters
    ----------
    broker : InstrumentBroker
        The instrument to lease from.
    request : AcquisitionRequest
        What to acquire.
    log : SessionLog
        Where the outcome is recorded.
    session : Session | None
        Where frames are written, or None to hold them in memory only.
        None is a real choice rather than a degraded one - looking at a
        Ronchigram to decide whether it is worth keeping should not
        litter the session with files - and the log entry says which
        happened.
    note : str | None
        A note attached to this recording specifically, when a session
        is attached.
    """

    def __init__(
        self,
        broker: InstrumentBroker,
        request: AcquisitionRequest,
        log: SessionLog,
        *,
        session: Session | None = None,
        note: str | None = None,
    ) -> None:
        super().__init__(f"acquire-{request.label}")
        self._broker = broker
        self._request = request
        self._log = log
        self._session = session
        self._note = note
        self._frames_seen = 0
        self._holder = ""

    def _reset(self) -> None:
        """Clear the frame counter and holder before a fresh run."""
        with self._lock:
            self._frames_seen = 0
            self._holder = ""

    @property
    def frames_acquired(self) -> int:
        """Return how many frames have arrived so far, for a progress line."""
        with self._lock:
            return self._frames_seen

    @property
    def holder(self) -> str:
        """
        Return this client's identity, as the broker assigned it.

        Empty until a lease has been granted. The dashboard keeps it so
        that a tile can tell "leased by me" from "leased by somebody
        else" - the two look identical from outside and mean opposite
        things, and a client cannot know its own name until the broker
        has told it one.

        Returns
        -------
        str
            The holder string from the most recent granted lease.
        """
        with self._lock:
            return self._holder

    @property
    def result(self) -> SessionLogEntry | None:
        """Return the log entry this acquisition produced, or None until it ends."""
        with self._lock:
            return typing.cast("SessionLogEntry | None", self._raw_result)

    def _leased_frames(self) -> Iterator[Frame]:
        """
        Yield the series from inside a lease taken on this thread.

        A generator, so the lease is taken when the *consumer* starts
        pulling - which is either this job's own ``list`` below or the
        session's streaming writer, both on this worker. Nothing here
        runs on the notebook's thread.

        Yields
        ------
        Frame
            Each acquired frame.
        """
        with self._broker.lease(
            *self._request.targets,
            reason=self._request.reason,
        ) as leased:
            with self._lock:
                self._holder = leased.lease.holder
            for frame in self._request.build(leased):
                # Renewed per frame rather than for a guessed duration:
                # a long series outlives any fixed TTL, and a job that
                # wedges stops renewing and lets the broker take the
                # instrument back.
                leased.renew()
                with self._lock:
                    self._frames_seen += 1
                yield frame

    def _work(self) -> SessionLogEntry:
        """
        Acquire, record the outcome in the log, and return the entry.

        Returns
        -------
        SessionLogEntry
            The entry as the log stored it, index included.

        Raises
        ------
        Exception
            Whatever the acquisition raised, re-raised after the refusal
            has been logged, so that
            :attr:`~miainwoodpecker.jobs.BackgroundJob.error` is set as
            every other job in this project sets it.
        """
        started_at = datetime.datetime.now(tz=datetime.UTC)
        started = time.monotonic()
        try:
            frames, path = self._acquire()
        except Exception as error:
            self._log.append(
                self._entry(
                    started_at,
                    time.monotonic() - started,
                    frames=(),
                    path=None,
                    error=f"{type(error).__name__}: {error}",
                ),
            )
            raise
        return self._log.append(
            self._entry(
                started_at,
                time.monotonic() - started,
                frames=frames,
                path=path,
            ),
        )

    def _acquire(self) -> tuple[Sequence[Frame], str | None]:
        """
        Run the series, streaming it to disk when a session is attached.

        Returns
        -------
        tuple[Sequence[Frame], str | None]
            The frames the log needs for its thumbnail and metadata, and
            where they were written. With a session attached only the
            first frame is retained - the rest are streamed to the file
            and released, because a hundred-frame series held in memory
            purely to build one thumbnail is a hundred frames of kernel
            memory for nothing.
        """
        if self._session is None:
            return (list(self._leased_frames()), None)
        kept: list[Frame] = []
        recording = self._session.record(
            self._retaining_first(kept),
            label=self._request.label,
            note=self._note,
        )
        return (kept, str(recording.path))

    def _retaining_first(self, kept: list[Frame]) -> Iterator[Frame]:
        """
        Pass frames through to the writer, keeping only the first.

        Parameters
        ----------
        kept : list[Frame]
            Filled with the first frame, if there is one.

        Yields
        ------
        Frame
            Every frame, unchanged.
        """
        for frame in self._leased_frames():
            if not kept:
                kept.append(frame)
            yield frame

    def _entry(
        self,
        started_at: datetime.datetime,
        duration_s: float,
        *,
        frames: Sequence[Frame],
        path: str | None,
        error: str | None = None,
    ) -> SessionLogEntry:
        """
        Build the log entry for this acquisition, successful or not.

        Parameters
        ----------
        started_at : datetime.datetime
            When the acquisition began.
        duration_s : float
            How long it took, from a monotonic clock.
        frames : Sequence[Frame]
            The frames retained for the thumbnail and metadata.
        path : str | None
            Where they were written, if anywhere.
        error : str | None
            Why it did not happen, if it did not.

        Returns
        -------
        SessionLogEntry
            The entry, without an index - :meth:`SessionLog.append`
            assigns that.
        """
        shape, dtype, thumbnail = describe_frames(frames)
        return SessionLogEntry(
            index=0,
            label=self._request.label,
            reason=self._request.reason,
            targets=self._request.targets,
            holder=self.holder,
            started_at=started_at,
            duration_s=duration_s,
            frame_count=self.frames_acquired,
            shape=shape,
            dtype=dtype,
            metadata=dict(frames[0].metadata) if frames else {},
            thumbnail=thumbnail,
            recording_path=path,
            error=error,
        )
