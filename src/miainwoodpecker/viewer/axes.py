"""
Putting a frame's calibration onto the napari layer that displays it.

``storage/calibration.py`` has modelled per-axis calibration for a while —
kind, scale, offset, units, per axis, resolved per acquisition — and the
storage and analysis layers use it. The *viewer* did not: layers were
added with no ``scale`` and no ``units`` at all, so every panel was in
bare pixels and there was nothing for a scale bar to read. This module is
the bridge, and it is deliberately the only place that decides how a
:class:`~miainwoodpecker.storage.calibration.FrameCalibration` becomes
napari layer properties.

One panel per image is what makes this possible
-----------------------------------------------
napari applies units per *layer* but draws the scale bar per *viewer*,
and it refuses to render units at all when the layers in one viewer
disagree — it says so, out loud::

    WARNING: Inconsistent units across layers; units will not be used
    for rendering.

So two images with different calibration cannot share a canvas and both
keep their units. Since every image here has its own calibration — a
HAADF map in nm, a Ronchigram in 1/nm, an EEL spectrum in eV — the
one-window-per-dataset arrangement in ``viewer/documents.py`` is not a
preference this feature works around but the thing that makes it
possible at all.


A spectrum has no layer, and is here anyway
-------------------------------------------
:func:`spectrum_axis` answers the same question for a rank-1 readout,
whose display is a plot rather than a napari layer (see
:mod:`miainwoodpecker.viewer.plots`). It lives here because the question
is this module's — "what does this axis measure, and what should the
display say it is" — and because the alternative was for the plot to
open ``storage/calibration.py`` itself and re-decide it. The 2D path
answers with layer keywords and the 1D path with an axis, for the plain
reason that a curve has no ``scale``: it is drawn *at* its coordinates
rather than stretched to them.

Geometry is applied only where the axes are commensurable
---------------------------------------------------------
``layer.scale`` is a *geometric* claim: it says how long each pixel is,
and napari draws the picture accordingly. That is exactly right when
both axes measure the same thing — a real-space image sampled more
finely across than down should be drawn wider than it is tall, and
drawing it on square pixels would be the distortion.

It is meaningless when they do not. A 2D EELS readout is energy in one
direction and position in the other, and there is no rate of exchange
between an electronvolt and a nanometre: drawing 512 eV against 6.4 nm
as though they shared a ruler would produce an 80:1 sliver whose shape
asserted something no instrument measured. Domain practice agrees —
DigitalMicrograph and HyperSpy both show such a readout in pixel
geometry with the axes *labelled*, not scaled against each other.

**Anisotropic detectors are the case this gets right, not the case it
breaks on**, and they are ordinary in a spectrometer — binning 1x along
the dispersive direction to keep energy resolution while binning hard
across it for signal gives a stored pixel several times taller than it
is wide. Both axes still measure the same thing, so the two scales are
comparable numbers and the frame is drawn to its *physical* shape: a
64x256 readout from a detector binned four times across draws square,
because it is square, and its 4:1 pixel count is a fact about the
readout rather than about the specimen. Note which of these two words is
doing the work here — an anisotropic *detector* is exactly why per-axis
scale must be applied, whereas the *view* transform (napari's "camera",
a different sense of the word) must stay isotropic, since that is what
stops a window resize from distorting anything.

So :func:`layer_axes` applies ``scale`` and ``translate`` only when both
axes share a kind (converting one to the other's unit where they differ),
and otherwise leaves the geometry in pixels. Units and axis labels are
set either way, and the resolved calibration is attached to the layer
under :data:`CALIBRATION`, so nothing is lost: a readout, and the ROI
work that has to convert a selection into probe positions, take the
model from there rather than trying to read it back out of ``scale``.

This also keeps the promise the viewing area was built under. Calibration
changes the shape a panel draws — that is the data's true geometry — but
it is not a *viewing* change, and no resize, tile or rearrange moves it
afterwards. ``test_documents.py`` measures against the calibrated aspect
for that reason, rather than against the pixel count.
"""

from __future__ import annotations

import typing

from miainwoodpecker.storage.calibration import (
    PIXEL_UNITS,
    AxisCalibration,
    FrameCalibration,
    resolve_frame_calibration,
)

if typing.TYPE_CHECKING:
    from collections.abc import Mapping

    import numpy as np

#: Layer-metadata key holding the :class:`FrameCalibration` a panel is
#: displaying. Public because this is where a readout or an ROI takes the
#: authoritative model from — ``layer.scale`` carries it only when the
#: axes are commensurable, and is identity otherwise.
CALIBRATION = "miainwoodpecker_calibration"

#: Axis label for the leading axis of a frame *stack*, which napari gives
#: a slider rather than drawing.
_FRAME_AXIS_LABEL = "frame"


def frame_calibration(
    data: np.ndarray,
    metadata: Mapping[str, object] | None,
) -> FrameCalibration:
    """
    Resolve the calibration of a frame about to be displayed.

    A thin wrapper over
    :func:`~miainwoodpecker.storage.calibration.resolve_frame_calibration`
    that takes the shape from the array, so a display path never has to
    restate the ``(height, width)`` convention.

    Parameters
    ----------
    data : np.ndarray
        The frame, whose last two axes are height and width.
    metadata : Mapping[str, object] | None
        The frame's acquisition metadata.

    Returns
    -------
    FrameCalibration
        The calibration, uncalibrated if the metadata claims none.
    """
    height, width = data.shape[-2:]
    return resolve_frame_calibration((height, width), metadata=metadata)


def spectrum_axis(
    data: np.ndarray,
    metadata: Mapping[str, object] | None,
) -> AxisCalibration:
    """
    Resolve the axis a spectrum's counts are plotted against.

    The 1D counterpart of :func:`frame_calibration`, and it exists
    because that function cannot be it: a
    :class:`~miainwoodpecker.storage.calibration.FrameCalibration` is
    exactly two axes, so asking it about a rank-1 readout raises rather
    than answering. A projecting detector delivers one axis of counts,
    and this says what that axis measures.

    **The dispersion is read from where a projecting detector already
    writes it** — ``metadata["calibration"]["x"]`` — by resolving the
    readout as the single row it is. That is not a convention invented
    here: a detector that sums its non-dispersive direction keeps the
    fast axis, which is ``x``, and leaves ``y`` uncalibrated.

    **An axis that is not energy is reported as bare channels**, rather
    than as whatever else it claims to be. One axis of counts against a
    real-space or angular ruler is not a spectrum — it is the "line of
    numbers on an angular axis" a projected Ronchigram would give, which
    :class:`~miainwoodpecker.devices.interface.Spectrum` refuses to be
    built from — so plotting it against electronvolts it never had would
    put an energy label on a number that is not one. Channels are the
    honest fallback and they are still a usable x-axis.

    Parameters
    ----------
    data : np.ndarray
        The spectrum, with counts on its **last** axis — the invariant
        :class:`~miainwoodpecker.devices.interface.Spectrum` states, so
        this is equally the right question to ask of a rank-1 readout
        and of one position taken out of a spectrum image.
    metadata : Mapping[str, object] | None
        The frame's acquisition metadata.

    Returns
    -------
    AxisCalibration
        The dispersive axis, or the uncalibrated channel axis when the
        metadata describes no energy calibration.
    """
    length = int(data.shape[-1])
    # Resolved as a one-row frame: the calibration model's smallest unit
    # is a pair of axes, and a height of 1 is what a projected readout
    # is. Only a 'centered' spec consults a length, and the length that
    # matters - the dispersive one - is passed truthfully.
    calibration = resolve_frame_calibration((1, length), metadata=metadata)
    energy = calibration.energy_axis_name()
    if energy is None:
        return AxisCalibration()
    return calibration.axis(energy)


def commensurable(calibration: FrameCalibration) -> bool:
    """
    Report whether the two axes can be drawn against a shared ruler.

    True when both axes are calibrated and measure the same kind of
    thing, which is the only case in which one pixel's length in y and
    one pixel's length in x are comparable numbers. Differing units of
    the same kind — nanometres against angstroms — are commensurable;
    :func:`layer_axes` converts rather than refusing.

    Parameters
    ----------
    calibration : FrameCalibration
        The frame's calibration.

    Returns
    -------
    bool
        Whether geometry may be applied.
    """
    return (
        calibration.y.is_calibrated
        and calibration.x.is_calibrated
        and calibration.y.kind is calibration.x.kind
    )


def _matched(calibration: FrameCalibration) -> tuple[AxisCalibration, AxisCalibration]:
    """
    Return both axes expressed in one unit, for a commensurable frame.

    Parameters
    ----------
    calibration : FrameCalibration
        A frame calibration whose axes share a kind.

    Returns
    -------
    tuple[AxisCalibration, AxisCalibration]
        The y and x axes, both in y's unit.
    """
    y = calibration.y
    x = calibration.x
    if x.units != y.units:
        x = x.converted_to(typing.cast("str", y.units))
    return y, x


def layer_axes(
    calibration: FrameCalibration,
    *,
    ndim: int = 2,
) -> dict[str, object]:
    """
    Build the napari layer keywords that express a calibration.

    Every key returned is an ordinary ``add_image`` keyword, so the
    result is as valid against a plain :class:`napari.Viewer` as against
    ``documents.DocumentBoard``.

    Parameters
    ----------
    calibration : FrameCalibration
        The frame's calibration.
    ndim : int
        Dimensions of the array being displayed. Anything above two is a
        stack of frames; the leading axes get identity geometry and a
        ``frame`` label, since napari gives them a slider rather than
        drawing them.

    Returns
    -------
    dict[str, object]
        ``scale``, ``translate``, ``units``, ``axis_labels`` and
        ``metadata``. A caller with metadata of its own must merge rather
        than replace, or the calibration will not reach the layer.
    """
    if commensurable(calibration):
        y, x = _matched(calibration)
        scale: tuple[float, ...] = (y.scale, x.scale)
        translate: tuple[float, ...] = (y.offset, x.offset)
    else:
        # Not a refusal to record the calibration - it is in the metadata
        # below either way - but a refusal to make a geometric claim out
        # of two quantities that do not convert into one another.
        y, x = calibration.y, calibration.x
        scale = (1.0, 1.0)
        translate = (0.0, 0.0)
    units: tuple[str, ...] = (
        typing.cast("str", y.units),
        typing.cast("str", x.units),
    )
    labels: tuple[str, ...] = (y.long_name, x.long_name)
    leading = max(0, ndim - 2)
    return {
        "scale": (1.0,) * leading + scale,
        "translate": (0.0,) * leading + translate,
        "units": (PIXEL_UNITS,) * leading + units,
        "axis_labels": (_FRAME_AXIS_LABEL,) * leading + labels,
        "metadata": {CALIBRATION: calibration},
    }


def scale_bar_unit(calibration: FrameCalibration) -> str | None:
    """
    Return the unit a panel's scale bar should show, or None for no bar.

    napari draws the bar horizontally, so it measures the x axis, and it
    can only be truthful where the geometry it is measuring is truthful.
    That makes this exactly the commensurable case: an uncalibrated panel
    gets no bar rather than one reading "pixel", and a panel whose axes
    do not convert gets none rather than a length drawn across an energy.

    Parameters
    ----------
    calibration : FrameCalibration
        The frame's calibration.

    Returns
    -------
    str | None
        The unit for :attr:`napari.Viewer.scale_bar`, or None if the
        panel should not show one.
    """
    if not commensurable(calibration):
        return None
    _, x = _matched(calibration)
    return typing.cast("str", x.units)
