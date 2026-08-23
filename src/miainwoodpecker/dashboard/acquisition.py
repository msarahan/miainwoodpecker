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
doing, several times a second, to draw the tiles. There is no new mechanism here:
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

**One acquisition is several signals, and the request names them.** What
a build function yields is not frames but ``(name, frame)`` pairs, and
the name is the signal the frame belongs to: a detector for a
multi-channel scan, a step for a recipe that acquires more than one
thing. Everything downstream keys off it - one file per name when a
session is attached
(:meth:`~miainwoodpecker.storage.session.Session.record_datasets`), one
row per name in the log, one Save action per name for data with no file.

The pairs are the whole extension mechanism, and they are pairs rather
than a metadata key on purpose. Frame metadata is the *device's*
vocabulary (see :class:`~miainwoodpecker.devices.interface.Frame`), and
which step of a recipe a frame belongs to is not something a detector
reports; a survey HAADF and the follow-up HAADF thirty seconds later
carry byte-identical ``channel_name``. So the name travels beside the
frame, where the acquisition that knows it can put it, and
:func:`named` is all it takes to compose a multi-step item::

    build=lambda leased: itertools.chain(
        named("survey", multichannel_scan_series(...)),
        named("SI", ...),
        named("followup", multichannel_scan_series(...)),
    )

That is labelling, not a new acquisition verb: every generator in that
chain is still one of :mod:`miainwoodpecker.acquisition`'s own.
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
from miainwoodpecker.dashboard.session_log import SessionLogEntry, describe_dataset
from miainwoodpecker.jobs import BackgroundJob

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from miainwoodpecker.broker.interface import InstrumentBroker, LeasedDevices
    from miainwoodpecker.dashboard.session_log import LoggedDataset, SessionLog
    from miainwoodpecker.devices.interface import (
        CameraParameters,
        Frame,
        ScanParameters,
    )
    from miainwoodpecker.storage.session import Session

CHANNEL_NAME_KEY = "channel_name"
"""Frame-metadata key naming the detector a scanned frame was read out of."""

DEVICE_ID_KEY = "device_id"
"""Frame-metadata key naming the device that produced a frame."""


def named(dataset: str, frames: Iterable[Frame]) -> Iterator[tuple[str, Frame]]:
    """
    Tag every frame of a series with the signal it belongs to.

    The composition primitive for a multi-step item: chain several of
    these and the acquisition produces one entry with one file per step.
    It labels, it does not acquire - what it wraps is one of
    :mod:`miainwoodpecker.acquisition`'s own generators, and it is lazy,
    so the device is still driven only as the pairs are pulled.

    Parameters
    ----------
    dataset : str
        The signal's name.
    frames : Iterable[Frame]
        The series to label.

    Yields
    ------
    tuple[str, Frame]
        The name and each frame.
    """
    for frame in frames:
        yield (dataset, frame)


def by_channel(
    frames: Iterable[Frame],
    *,
    fallback: str,
) -> Iterator[tuple[str, Frame]]:
    """
    Split a simultaneous multi-detector series by the detector each frame names.

    One pass of the probe reads every enabled detector out, so a
    multi-channel series arrives interleaved - HAADF, BF, HAADF, BF - and
    this is what turns that into two signals without unpicking the
    acquisition. The name comes from the frame's own ``channel_name``,
    which is the scan unit's word for the detector, so a file ends up
    called ``scan-haadf`` rather than ``scan-0``.

    Parameters
    ----------
    frames : Iterable[Frame]
        The series to split.
    fallback : str
        The name to use for a frame that reports neither a channel name
        nor a device id. An absent key means the instrument did not
        report it, and inventing a channel number would claim it did.

    Yields
    ------
    tuple[str, Frame]
        The detector's name and each frame.
    """
    for frame in frames:
        name = frame.metadata.get(CHANNEL_NAME_KEY) or frame.metadata.get(
            DEVICE_ID_KEY,
        )
        yield (str(name) if name else fallback, frame)


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
    build : Callable[[LeasedDevices], Iterable[tuple[str, Frame]]]
        Turns the leased devices into a series of ``(signal name, frame)``
        pairs. Called on the worker, inside the lease. Pairs rather than
        bare frames because one acquisition may produce several signals
        and each gets its own file and its own row in the log - see the
        module docstring, and :func:`named` and :func:`by_channel` for
        the two ways of producing them.
    """

    targets: tuple[str, ...]
    label: str
    reason: str
    build: Callable[[LeasedDevices], Iterable[tuple[str, Frame]]]


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
    listed = "-".join(channel_names) or "scan"
    return AcquisitionRequest(
        targets=(target,),
        # "scan", not "scan-HAADF-MAADF": the detectors are the item's
        # signals now and each names its own file, so putting them in the
        # item label too would produce scan-haadf-maadf-haadf.nxs.
        label="scan",
        reason=(
            f"{count} scan pass(es), {parameters.width}x{parameters.height} "
            f"at {parameters.pixel_time_us:g} us, detectors: {listed}"
        ),
        build=lambda leased: by_channel(
            multichannel_scan_series(
                leased.scanner(target),
                parameters,
                count,
                channels=wanted,
            ),
            fallback=target,
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
            build=lambda leased: named(
                target,
                _configured_image(leased, target, parameters),
            ),
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
        build=lambda leased: named(target, camera_series(leased.camera(target), count)),
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

    def _leased_pairs(self) -> Iterator[tuple[str, Frame]]:
        """
        Yield the named series from inside a lease taken on this thread.

        A generator, so the lease is taken when the *consumer* starts
        pulling - which is either this job's own loop below or the
        session's streaming writers, both on this worker. Nothing here
        runs on the notebook's thread.

        Yields
        ------
        tuple[str, Frame]
            Each acquired frame and the signal it belongs to.
        """
        with self._broker.lease(
            *self._request.targets,
            reason=self._request.reason,
        ) as leased:
            with self._lock:
                self._holder = leased.lease.holder
            for dataset, frame in self._request.build(leased):
                # Renewed per frame rather than for a guessed duration:
                # a long series outlives any fixed TTL, and a job that
                # wedges stops renewing and lets the broker take the
                # instrument back.
                leased.renew()
                with self._lock:
                    self._frames_seen += 1
                yield (dataset, frame)

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
            datasets = self._acquire(started_at)
        except Exception as error:
            self._log.append(
                self._entry(
                    started_at,
                    time.monotonic() - started,
                    datasets=(),
                    error=f"{type(error).__name__}: {error}",
                ),
            )
            raise
        return self._log.append(
            self._entry(started_at, time.monotonic() - started, datasets=datasets),
        )

    def _acquire(
        self,
        started_at: datetime.datetime,
    ) -> tuple[LoggedDataset, ...]:
        """
        Run the series, streaming each signal to its own file if there is one.

        With a session attached only each signal's **first** frame is
        retained - the rest are streamed to that signal's file and
        released, because a hundred-frame series held in memory purely to
        build one thumbnail is a hundred frames of kernel memory for
        nothing. With no session there is nowhere else for the data to
        be, so every frame is kept and the entry can still offer to save
        it.

        Parameters
        ----------
        started_at : datetime.datetime
            When the acquisition began, so the files carry the time the
            data was taken.

        Returns
        -------
        tuple[LoggedDataset, ...]
            One signal per name the acquisition produced, in the order
            the names first appeared.
        """
        kept: dict[str, list[Frame]] = {}
        counts: dict[str, int] = {}
        pairs = self._collected(kept, counts)
        paths: Mapping[str, str] = {}
        if self._session is None:
            # Consumed here rather than by a writer; the collector is
            # what retains the frames.
            for _ in pairs:
                pass
        else:
            recordings = self._session.record_datasets(
                pairs,
                label=self._request.label,
                note=self._note,
                started_at=started_at,
            )
            paths = {
                dataset: str(recording.path)
                for dataset, recording in recordings.items()
            }
        return tuple(
            describe_dataset(
                dataset,
                frames,
                frame_count=counts[dataset],
                path=paths.get(dataset),
            )
            for dataset, frames in kept.items()
        )

    def _collected(
        self,
        kept: dict[str, list[Frame]],
        counts: dict[str, int],
    ) -> Iterator[tuple[str, Frame]]:
        """
        Pass the named frames through, counting them and keeping what is needed.

        Parameters
        ----------
        kept : dict[str, list[Frame]]
            Filled with each signal's frames - all of them with no
            session attached, the first alone otherwise. Insertion order
            is first-appearance order, which is the order the log shows.
        counts : dict[str, int]
            Filled with each signal's frame count, which is not
            ``len(kept[name])`` once the frames are being streamed away.

        Yields
        ------
        tuple[str, Frame]
            Every pair, unchanged.
        """
        for dataset, frame in self._leased_pairs():
            frames = kept.setdefault(dataset, [])
            if self._session is None or not frames:
                frames.append(frame)
            counts[dataset] = counts.get(dataset, 0) + 1
            yield (dataset, frame)

    def _entry(
        self,
        started_at: datetime.datetime,
        duration_s: float,
        *,
        datasets: tuple[LoggedDataset, ...],
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
        datasets : tuple[LoggedDataset, ...]
            The signals it produced, empty for a refusal.
        error : str | None
            Why it did not happen, if it did not.

        Returns
        -------
        SessionLogEntry
            The entry, without an index - :meth:`SessionLog.append`
            assigns that.
        """
        return SessionLogEntry(
            index=0,
            label=self._request.label,
            reason=self._request.reason,
            targets=self._request.targets,
            holder=self.holder,
            started_at=started_at,
            duration_s=duration_s,
            datasets=datasets,
            error=error,
        )
