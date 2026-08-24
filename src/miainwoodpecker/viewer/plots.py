"""
The 1D display: a spectrum is a plot, and napari has no plot.

Every other panel in this application is an array on a canvas, which is
what napari is for and why it was adopted. A spectrometer breaks that.
Its projected readout is one axis of counts against energy, and the two
things a display of it must do — put a value on a *y* axis and label the
*x* axis in electronvolts — are the two things an image layer cannot do
at all. Pushed into one, an EEL spectrum is a picture one pixel high: it
says where the counts are bright, and never how many there are or where
the edge is.

The display path could not even reach that. A napari layer is added with
a :class:`~miainwoodpecker.storage.calibration.FrameCalibration`, which
is exactly two axes, so the viewer's own calibration helper raised
``ValueError: not enough values to unpack`` on the first rank-1 frame
that arrived. Putting a spectrometer into ``projected`` and starting it
was, until this module, a way to stop the live view.

Why pyqtgraph
-------------
It is a plotting widget over the same Qt this application already runs,
so a spectrum panel is a ``QWidget`` that goes into an MDI sub-window
beside the napari ones — one dataset, one window, as
:mod:`miainwoodpecker.viewer.documents` has it — rather than a second
toolkit's window or a second event loop. And it is built for the case
here, which is not "draw a chart" but *one curve replaced at display
rate*, sixty times a second, while an acquisition fills in behind it.
The alternative in the scientific-Python reflex, a Matplotlib canvas,
redraws by rasterising the whole figure and is a well-known order of
magnitude slower at exactly that.

What this module decides, and what it refuses to
-------------------------------------------------
It draws counts against an axis it is *handed*. It does not resolve the
calibration itself — :func:`miainwoodpecker.viewer.axes.spectrum_axis`
does, because deciding what an axis measures is that module's job for
the 2D case too and having two places decide it is how they come to
disagree. It does not slice a spectrum image down to a spectrum either:
which beam position an operator is looking at is a question about the
acquisition, and the caller is what knows the answer.

What it does refuse is a rank it cannot draw. Handed a spectrum image,
it raises rather than plotting ``data.ravel()`` — 4096 positions of 1340
channels laid end to end is a curve, and it would be a curve of nothing.

Theme, and why the colours are not chosen here
-----------------------------------------------
The curve, the axes and the labels are all drawn in the window's own
text colour on its own background, read from the Qt palette. napari sets
that palette for the whole application from its theme, so the panel
follows the operator's choice of light or dark without this module
knowing napari exists. It is also what a spectrum display has looked
like since DigitalMicrograph: one ink colour on one ground. A palette
that picked its own colours would be legible in exactly one theme, and
the wrong one half the time.

Not a static plot
-----------------
Auto-ranging stays on until the operator pans or zooms — pyqtgraph
latches it off itself when they do, which is the same bargain
:class:`~miainwoodpecker.viewer.documents.Document` strikes with napari's
camera: an automatic view until you choose one, and then it is yours.
pyqtgraph's own ``A`` button in the corner and its right-click menu are
the way back, so there is no button here duplicating them. A *different*
axis — another detector, or a dispersion the operator just changed —
does reset the view, because it is no longer the same picture to have
had an opinion about.
"""

from __future__ import annotations

import typing

import numpy as np
import pyqtgraph as pg
from qtpy import QtGui, QtWidgets

if typing.TYPE_CHECKING:
    import numpy.typing as npt

    from miainwoodpecker.storage.calibration import AxisCalibration

#: The y axis. Every detector this draws reports counts — a spectrum is
#: a histogram of them — and none reports anything else, so this is a
#: constant rather than something a caller passes.
COUNTS_LABEL = "counts"

#: The x axis when nothing calibrated it. "channel" rather than the
#: calibration model's own "pixel index": a spectrum's bins are channels
#: in every vocabulary this project reads or writes, from EMSA's
#: ``XPERCHAN`` to Oxford's ``Start Channel``.
CHANNEL_LABEL = "channel"

#: How strongly the grid shows through. Faint on purpose: it is there to
#: let an edge be read off the energy axis, not to be looked at.
_GRID_ALPHA = 0.15

#: Curve width in pixels. One, because a spectrum with 1340 channels in
#: a panel 600 wide already draws several channels per pixel, and a
#: heavier line merges them.
_CURVE_WIDTH = 1

#: The rank this draws. Rank 2 is a line scan and rank 3 a spectrum
#: image; both are *collections* of this, and which one to draw is the
#: caller's question - see the module docstring.
_SPECTRUM_RANK = 1


class SpectrumPlot(QtWidgets.QWidget):
    """
    One spectrum, drawn as a curve against the axis it was measured on.

    Holds a single pyqtgraph curve and replaces its data on every
    update. Single because that is what makes it affordable at display
    rate: adding an item per frame would leave the scene graph carrying
    every spectrum ever shown, and clearing and re-adding one costs the
    allocation this exists to avoid.

    Parameters
    ----------
    parent : QtWidgets.QWidget | None
        Parent widget, or None for a free-standing panel.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._plot = pg.PlotWidget()
        layout.addWidget(self._plot)
        palette = self.palette()
        ink = palette.color(QtGui.QPalette.ColorRole.WindowText)
        paper = palette.color(QtGui.QPalette.ColorRole.Base)
        self._ink = ink
        self._plot.setBackground(paper)
        item = self._plot.getPlotItem()
        for edge in ("bottom", "left"):
            axis = item.getAxis(edge)
            axis.setPen(ink)
            axis.setTextPen(ink)
            # **The unit is the detector's, and is not re-prefixed.**
            # pyqtgraph reads a unit as a *base* SI quantity and adds a
            # prefix to keep the tick numbers small, which is right for
            # volts and wrong for every unit this project writes that
            # already carries one. A real monochromated EELS spectrum
            # calibrated in meV came back labelled "energy (kmeV)" -
            # kilo-milli-electronvolts - and its axis divided by a
            # thousand to match. eV, meV and keV are what
            # storage/calibration.py accepts for an energy axis and an
            # adapter converts once, on the way in; the display's job is
            # to say which of them arrived, not to pick a fourth.
            axis.enableAutoSIPrefix(False)  # noqa: FBT003 - pyqtgraph's own signature
        item.showGrid(x=True, y=True, alpha=_GRID_ALPHA)
        item.setLabel("left", COUNTS_LABEL, color=ink.name())
        self._curve = item.plot(pen=pg.mkPen(ink, width=_CURVE_WIDTH))
        # Both are pyqtgraph's answer to a curve with more points than
        # the panel has pixels, and 'peak' rather than the cheaper
        # 'subsample' because a spectrum's whole content is its peaks: a
        # subsampled curve drops the top of an edge, which is the one
        # value an operator is looking at.
        self._curve.setDownsampling(auto=True, method="peak")
        self._curve.setClipToView(True)
        #: The axis the curve is currently drawn against, and its length.
        #: Kept so the coordinates are built when they change rather than
        #: on every frame, and so a change can reset the view.
        self._axis: AxisCalibration | None = None
        self._coordinates: npt.NDArray[np.float64] = np.empty(0)
        self._title = ""
        #: The counts object last drawn, for the identity check in
        #: :meth:`show_spectrum`. Held here rather than by the caller
        #: because a panel the operator closed and asked back for is a
        #: *new* plot, which has drawn nothing and must not skip - and
        #: only this side knows which it is.
        self._drawn: object = None

    def show_spectrum(
        self,
        counts: npt.ArrayLike,
        axis: AxisCalibration,
        *,
        title: str = "",
    ) -> None:
        """
        Draw one spectrum, replacing whatever was on the plot.

        Parameters
        ----------
        counts : npt.ArrayLike
            The spectrum: one value per channel.
        axis : AxisCalibration
            What its channels measure, from
            :func:`miainwoodpecker.viewer.axes.spectrum_axis`. An
            uncalibrated axis is drawn against channel number and
            labelled as such, which is honest and still readable.
        title : str
            A line above the plot saying which spectrum this is — the
            beam position during a pass, normally. Empty for none.

        Raises
        ------
        ValueError
            If ``counts`` is not rank 1. A spectrum image is a
            collection of spectra and this draws one; flattening it
            would produce a curve with a shape and no meaning.
        """
        if counts is self._drawn and title == self._title and axis == self._axis:
            # Nothing new since the last tick. The display timer runs at
            # 60 Hz and a stopped detector goes on handing out the same
            # object, so most ticks are this one; setData would repaint
            # the curve, the axes and the grid for it. Identity, like the
            # image path's own skip: a live loop hands out the same array
            # until it grabs another.
            # The axis is compared too, and not because a live frame's
            # can change under it - it cannot, since a new dispersion
            # arrives on a new frame. It is because *this* is the check
            # that decides whether the labels below run at all, and a
            # caller redrawing one array against a corrected calibration
            # would otherwise be answered with the old one.
            return
        values = np.asarray(counts)
        if values.ndim != _SPECTRUM_RANK:
            msg = (
                f"a spectrum plot draws one spectrum, so it needs a rank-1 "
                f"array of counts; got shape {values.shape}. A line scan or "
                f"a spectrum image holds many, and which one to draw is the "
                f"caller's to decide"
            )
            raise ValueError(msg)
        if axis != self._axis or values.size != self._coordinates.size:
            self._recalibrate(axis, values.size)
        self._curve.setData(self._coordinates, values)
        self._drawn = counts
        if title != self._title:
            self._title = title
            self._plot.getPlotItem().setTitle(title or None, color=self._ink.name())

    def _recalibrate(self, axis: AxisCalibration, length: int) -> None:
        """
        Rebuild the x coordinates and the label, and reset the view.

        Both are done here rather than per frame because they change
        when the *detector* does — a dispersion the operator just set, or
        another camera's panel — and not when a frame arrives. Resetting
        the view belongs with them for the same reason: a zoom the
        operator chose is theirs to keep across the next thousand frames
        of the same spectrum, and means nothing once the axis under it
        has moved.

        Parameters
        ----------
        axis : AxisCalibration
            The new axis.
        length : int
            How many channels the spectrum has.
        """
        self._axis = axis
        self._coordinates = axis.values(length)
        item = self._plot.getPlotItem()
        if axis.is_calibrated:
            item.setLabel(
                "bottom",
                axis.long_name,
                units=axis.units,
                color=self._ink.name(),
            )
        else:
            # No units keyword: pyqtgraph reads one as an SI quantity and
            # prefixes it, so a 4096-channel axis would be labelled in
            # "kchannel". A bare label says the same thing and cannot.
            item.setLabel("bottom", CHANNEL_LABEL, color=self._ink.name())
        item.enableAutoRange()

    def fit_to_panel(self) -> None:
        """
        Bring the whole spectrum back into view, undoing a zoom.

        The plot's answer to the View menu's "Fit panel to data", which
        every document is asked for. Auto-ranging is switched back *on*
        rather than the range being set once, so the view goes on
        following the data as the next frames arrive — which is the
        state it was in before the operator zoomed.
        """
        self._plot.getPlotItem().enableAutoRange()

    def spectrum(self) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """
        Return the coordinates and counts the plot is holding.

        Read back off the curve rather than from a copy kept beside it,
        so what this reports is what the plot has rather than what this
        class remembers handing it.

        **The curve's original dataset, not its displayed one.**
        ``getData`` returns what survived downsampling and clipping to
        the visible range — a rendering of the spectrum, whose values
        depend on how wide the panel happens to be. This is the
        spectrum; the peak-preserving reduction of it is pyqtgraph's
        business and nobody's question.

        Returns
        -------
        tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]
            The x coordinates and the counts, both empty before the
            first spectrum is shown.
        """
        x, y = self._curve.getOriginalDataset()
        if x is None or y is None:
            return np.empty(0), np.empty(0)
        return np.asarray(x), np.asarray(y)

    def axis_label(self) -> str:
        """
        Return the x axis's label, as drawn.

        Returns
        -------
        str
            The label text, without the unit pyqtgraph appends to it.
        """
        return str(self._plot.getPlotItem().getAxis("bottom").labelText)

    def title(self) -> str:
        """
        Return the line above the plot.

        Returns
        -------
        str
            The title, empty when there is none.
        """
        return self._title
