"""
The analyses the viewer's three buttons run, as plain functions.

These bodies used to live inside closures in
``LiveInstrumentWidget._analyze_camera_in_hyperspy`` and its two siblings.
Nothing about what they compute has changed; what changed is that they
are now callable from *two* places — the viewer's own worker thread, and
an isolated analysis worker process
(:mod:`miainwoodpecker.analysis.worker`) — and a closure cannot be called
from another process.

One implementation per operation, deliberately. The alternative was a
copy in the worker, which would have made "the isolated path computes the
same thing as the in-process path" a claim maintained by hand instead of
a fact about the call graph. The transport is the only difference between
the two, which is exactly what docs/analysis-isolation.md sets out to
measure.

Every operation takes one :class:`AnalysisInput` and nothing else. That
uniformity is what lets :data:`OPERATIONS` dispatch by name from either
side of a process boundary without a per-operation argument table on each
side — and the fields it carries are the ones the viewer already
resolves for itself: a file to open, *or* frames already read, and what
to call them in an error message. The first two are a pair rather than a
union because
:mod:`miainwoodpecker.analysis.hyperspy_bridge` gives them two entry
points precisely so nobody reads a 2048x2048 recording twice by accident.

**Imports are inside the functions, and that is load-bearing rather than
tidy.** This module is imported by
:mod:`miainwoodpecker.analysis.remote`'s in-process runner, which must
work in an install with no analysis extra at all, and by the worker,
which must not pay py4DSTEM's 5.2 s import to answer a HyperSpy call.

Returns are plain arrays and numbers, never library objects. That is what
makes these three isolable at all, and it is **not** true of this
package's documented API: ``load_as_hyperspy_signal`` and its siblings
hand back a live ``Signal2D``/``DataSet``/``DiffractionSlice`` for the
caller to go on working with, which cannot cross a process boundary in
any useful form. See docs/analysis-isolation.md — that asymmetry is the
single most important thing the isolation work found.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass

if typing.TYPE_CHECKING:
    from collections.abc import Callable

    import numpy.typing as npt

    from miainwoodpecker.storage.nexus import FrameStack

# A DiffractionSlice built from a one-frame recording is the pattern
# itself; from a multi-frame one it is the labelled stack. Named for the
# same reason viewer/live.py named it: `ndim == 2` at a call site reads as
# a magic number.
_SINGLE_PATTERN_NDIM = 2


@dataclass(frozen=True)
class AnalysisInput:
    """
    What one analysis runs against, decoded and ready to use.

    The in-process form of
    :class:`miainwoodpecker.analysis.transfer.AnalysisSource`: same three
    facts, with the frames already resolved out of shared memory when
    they came that way. Kept as a small object rather than three
    positional parameters so that every operation has the same signature
    and neither the worker nor the in-process runner needs to know which
    of them cares about which field.

    Attributes
    ----------
    path : str | None
        A NeXus file to read, when the caller does not already hold the
        frames.
    frames : FrameStack | None
        Frames already in memory, when it does. Takes precedence: the
        whole point of having both is to avoid a second read.
    origin : str | None
        What to call this input if an adapter refuses it. The viewer
        passes the recording's name or "a fresh burst", because arrays
        carry no provenance of their own and a refusal that cannot name
        what it refused is a worse refusal. ``None`` leaves each adapter's
        own wording.
    """

    path: str | None = None
    frames: FrameStack | None = None
    origin: str | None = None

    def naming(self) -> dict[str, str]:
        """
        Return the ``source=`` keyword to pass on, or nothing.

        Returns
        -------
        dict[str, str]
            ``{"source": origin}`` when this input was named, and an empty
            mapping otherwise — so an unnamed input gets the adapter's own
            default wording rather than the string ``"None"``.
        """
        return {} if self.origin is None else {"source": self.origin}


def mean_projection(job: AnalysisInput) -> npt.NDArray[typing.Any]:
    """
    Mean of a recording's frames, computed through HyperSpy.

    One real HyperSpy operation over a calibrated signal
    (``Signal2D.mean`` across the navigation axis), which is what the
    viewer's "Analyze in HyperSpy" button has always run. The mean is
    taken over the *navigation* axis by name rather than over axis 0 by
    index, so it stays a statement about the signal's axis model rather
    than about the array's memory layout.

    Parameters
    ----------
    job : AnalysisInput
        The file or frames to analyse. ``origin`` is unused here and that
        is a fact about the adapter rather than an oversight:
        :func:`~miainwoodpecker.analysis.hyperspy_bridge.hyperspy_signal_from_frames`
        takes no source name, because an image stack has no shape it can
        refuse.

    Returns
    -------
    npt.NDArray[typing.Any]
        The mean frame.
    """
    from miainwoodpecker.analysis.hyperspy_bridge import (  # noqa: PLC0415
        hyperspy_signal_from_frames,
        load_as_hyperspy_signal,
    )

    signal = (
        hyperspy_signal_from_frames(job.frames)
        if job.frames is not None
        else load_as_hyperspy_signal(typing.cast("str", job.path))
    )
    return signal.mean(axis=signal.axes_manager.navigation_axes[0]).data


def sum_projection(
    job: AnalysisInput,
    cpu_count: int | None = None,
) -> npt.NDArray[typing.Any]:
    """
    Sum of a recording's frames, computed through a LiberTEM UDF.

    ``libertem.udf.sum.SumUDF`` on the thread-bounded inline ``Context``
    from
    :func:`~miainwoodpecker.analysis.libertem_bridge.analysis_context`,
    which is what the viewer's "Sum in LiberTEM" button has always run.
    The executor stays inline for the same reason as before: one UDF over
    one small burst, with no cluster worth standing up.

    Parameters
    ----------
    job : AnalysisInput
        The file or frames to analyse. A file is opened by LiberTEM's own
        HDF5 reader; frames become a ``MemoryDataSet``.
    cpu_count : int | None
        Cores to size the executor against; defaults to the machine's.

    Returns
    -------
    npt.NDArray[typing.Any]
        The summed frame.
    """
    from libertem.udf.sum import SumUDF  # noqa: PLC0415

    from miainwoodpecker.analysis.libertem_bridge import (  # noqa: PLC0415
        analysis_context,
        libertem_dataset_from_frames,
        load_as_libertem_dataset,
    )

    with analysis_context(cpu_count) as ctx:
        dataset = (
            libertem_dataset_from_frames(ctx, job.frames, **job.naming())
            if job.frames is not None
            else load_as_libertem_dataset(ctx, typing.cast("str", job.path))
        )
        return ctx.run_udf(dataset=dataset, udf=SumUDF())["intensity"].data


def fit_central_disk(
    job: AnalysisInput,
) -> tuple[npt.NDArray[typing.Any], float, float, float]:
    """
    Fit the central diffraction disk of one pattern, through py4DSTEM.

    ``py4DSTEM.process.calibration.get_probe_size`` on a single
    diffraction pattern, which is what the viewer's "Fit central disk"
    button has always run. A multi-frame recording has its *first*
    pattern fitted, unchanged and for the reason the button's own
    docstring gives: ``get_probe_size`` fits one pattern, and averaging
    several first would fit something that was never acquired.

    Parameters
    ----------
    job : AnalysisInput
        The file or frames to analyse. ``origin`` matters here, because a
        scan or spectrum recording is refused rather than mislabelled as a
        diffraction plane and the refusal names the recording.

    Returns
    -------
    tuple[npt.NDArray[typing.Any], float, float, float]
        The pattern that was fitted, then the fitted radius and centre
        ``(radius, x0, y0)`` in pixels — the pattern travels back with the
        numbers because the caller draws the fit *on* it, and a
        multi-frame recording's first pattern is not something the caller
        otherwise has to hand.
    """
    from py4DSTEM.process.calibration import get_probe_size  # noqa: PLC0415

    from miainwoodpecker.analysis.py4dstem_bridge import (  # noqa: PLC0415
        diffraction_slice_from_frames,
        load_as_diffraction_slice,
    )

    diffraction_slice = (
        diffraction_slice_from_frames(job.frames, **job.naming())
        if job.frames is not None
        else load_as_diffraction_slice(typing.cast("str", job.path))
    )
    stack = diffraction_slice.data
    pattern = stack if stack.ndim == _SINGLE_PATTERN_NDIM else stack[0]
    radius, x0, y0 = get_probe_size(pattern)
    return pattern, float(radius), float(x0), float(y0)


OPERATIONS: dict[str, Callable[[AnalysisInput], object]] = {
    "mean_projection": mean_projection,
    "sum_projection": sum_projection,
    "fit_central_disk": fit_central_disk,
}
"""
Every analysis the isolation layer can run, by wire name.

The one place either side of the boundary turns a method name into a
callable. The worker does not need it — ``serving.invoke`` dispatches by
``getattr`` on the target object — but the in-process runner does, and
having both read the same table is what keeps "the two paths run the same
code" true by construction.
"""
