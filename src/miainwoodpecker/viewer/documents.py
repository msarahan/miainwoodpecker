"""
A document per dataset, so two of them can be looked at side by side.

Every source this application displays — each enabled scan detector,
each running camera, an opened recording, an analysis result — used to
arrive as a napari layer on one shared canvas, and napari puts every
layer at the world origin. Two of them therefore landed *on top of each
other*: a 512x512 HAADF image and a 128x1024 EELS readout overlapped,
and the only way to see either was to switch the other one's visibility
off. Comparing two detectors read out of the same pass is the ordinary
case on a scanned instrument, not an advanced one, so this module gives
each dataset a window of its own inside a
:class:`~qtpy.QtWidgets.QMdiArea`.

One napari viewer per document
------------------------------
A document is a real :class:`napari.Viewer` whose window has been
reparented into an MDI sub-window. That is more expensive than tiling
layers on a single canvas — a canvas and a GL context each — and it buys
the thing a shared canvas cannot: **every panel zooms and pans on its
own**. Focusing on one corner of the HAADF image does not drag the
diffraction pattern beside it out of view. Each panel also keeps
napari's own contrast, colormap and gamma controls, because it *is*
napari, not a reimplementation of it.

It costs one thing worth stating plainly: ``viewer.window._qt_window``
is private napari API. Nothing else exposes the widget, and
``docs/developing-the-ui.md`` already records that napari's layer
lifecycle is delicate at teardown. :meth:`Document.close` therefore
tears down in a fixed order, and the chrome-hiding calls are written so
that a napari upgrade that renames a dock leaves the chrome showing
rather than failing to open the window.

Aspect ratio is preserved, and not by this module
-------------------------------------------------
napari's *view* camera — the transform from world coordinates to the
screen, not the detector — is isotropic: one zoom scalar drives both
axes. An image in a panel of the wrong shape therefore letterboxes,
leaving margin at the sides or top, and cannot be stretched by resizing
a window, tiling, or anything else this module does.

That is the guarantee about *viewing*. What a panel is drawn to is its
calibrated shape, which an anisotropic detector legitimately makes
non-square — see :mod:`miainwoodpecker.viewer.axes`.
``tests/integration/test_documents.py`` measures the two separately:
that a world unit covers the same number of screen pixels on both axes,
and that the drawn shape matches the calibration.

Where a new document goes
-------------------------
New datasets are arranged so that nothing ever opens hidden underneath
something else, but a layout you arranged by hand is yours to keep.
Until you move or resize a window, adding or removing a document
re-tiles the area. The first time you place a window yourself the
auto-tiling latches off (:meth:`DocumentArea.note_user_arrangement`) and
later documents are placed in whatever space is clearest instead
(:meth:`DocumentArea.free_position`). :meth:`DocumentArea.arrange` tiles
everything again and hands control back.

Routing, and why it is duck-typed
---------------------------------
:class:`DocumentBoard` presents the slice of the :class:`napari.Viewer`
API that :class:`~miainwoodpecker.viewer.live.LiveInstrumentWidget`
actually uses — ``add_image``, ``add_shapes``, and membership, lookup
and deletion on ``layers`` — and routes each call to the right
document's viewer. The widget cannot tell the difference, which is the
point: a plain ``napari.Viewer`` still works everywhere the board does,
so every existing test that builds the widget against one keeps passing
and a single-canvas window remains a supported way to run this
application.

Not every dataset is a layer
----------------------------
A spectrometer's projected readout is one axis of counts, and nothing
napari draws can display it (see :mod:`miainwoodpecker.viewer.plots`).
It is a dataset all the same, so it gets a window like the rest:
:class:`PanelDocument` holds a plain widget instead of a viewer,
:meth:`DocumentArea.open_panel` opens one, and
:meth:`DocumentBoard.panel` is how the live display reaches it. Tiling,
placement, raising and closing are shared, because none of those care
what is inside a window — only the layer routing does, and it asks
:func:`_layers_of` rather than assuming there are any.

A camera keeps its name across that boundary: a spectrometer switched
between imaging and projecting is one dataset that changed shape, so the
window is replaced in place rather than a second one opening beside it
under the same title.

One behaviour a plain viewer has no opinion about is asked for through
``metadata``, which napari accepts and ignores on any layer:
:data:`ATTACHED_TO` names the layer an annotation belongs to, so
py4DSTEM's fitted-disk ellipse — drawn in the pixel coordinates of the
image it was fitted to — shares that image's document instead of flying
off into a window of its own.

Closing a panel, and getting it back
------------------------------------
A running detector goes on producing frames after its window is shut, so
reopening the window on the next frame would make the close button
useless — it would blink and come back. A closed panel therefore *stays*
closed: :class:`DocumentBoard` records it and quietly drops later frames
for it. Starting that source again brings it back and puts it in front,
which is the same request as raising a panel that is merely covered
(:meth:`DocumentBoard.raise_document`). A panel is free to be buried
while it runs; asking for the source again is what says you want to see
it.
"""

from __future__ import annotations

import contextlib
import typing
import warnings

import napari
import napari.qt
from qtpy import QtCore, QtWidgets

from miainwoodpecker.viewer import axes

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from napari.layers import Layer

#: Metadata key naming the layer an annotation is drawn in the
#: coordinates of. Such a layer shares that layer's document.
ATTACHED_TO = "miainwoodpecker_attached_to"

#: Smallest longest-side a panel opens at, in screen pixels. A 64x64
#: spectrum-image map at one pixel per position is a postage stamp; this
#: is the size at which it is worth looking at. Data already this large
#: opens at one screen pixel per acquired pixel.
#:
#: **The floor and the target are the same number on purpose.**
#: Magnifying small data to 512 while leaving 256 alone would make a
#: 128-pixel scan open in a *larger* window than a 256-pixel one, which
#: is worse than either extreme: window size would stop meaning anything
#: about the data. Keeping them equal is what makes it monotonic — more
#: data is never shown smaller.
MIN_LONGEST_SIDE = 256

#: The two axes of a frame, however many the array has.
_IMAGE_AXES = 2

#: Allowance for a sub-window title bar and frame, so a canvas sized to
#: the area still fits inside the window holding it.
_WINDOW_CHROME = 40
#: Gap left between tiled windows, so two panels read as two.
_TILE_GAP = 6
#: How far each window is offset from the last when they must overlap,
#: so a covered one still shows a corner and a title bar.
_CASCADE_OFFSET = 28
#: How much of a window the packer may give up to keep it in a row it
#: nearly fits. Small: this closes a near miss, it does not squeeze
#: panels in that have no business being there.
_PACK_SHRINK = 0.15

_MIN_DOCUMENT_WIDTH = 320
_MIN_DOCUMENT_HEIGHT = 240

# The shape a plot opens at: wide, and half as tall. A spectrum is long
# in one direction and read across, so a panel as tall as it is wide
# spends its screen on empty counts above the curve. Two to one is what
# DigitalMicrograph and HyperSpy both open a spectrum in.
_PLOT_WIDTH = 640
_PLOT_ASPECT = 2.0

#: Candidate positions tried per axis when placing a document into a
#: hand-arranged area. Coarse on purpose: this picks a clear spot, it
#: does not solve a packing problem.
_PLACEMENT_STEPS = 8


class Document:
    """
    One dataset's window: a napari viewer inside an MDI sub-window.

    Parameters
    ----------
    name : str
        The dataset's name, used as the window title and as the key the
        board routes layers by.
    viewer : napari.Viewer
        The viewer that draws it.
    window : QtWidgets.QMdiSubWindow
        The sub-window its Qt window was reparented into.
    """

    def __init__(
        self,
        name: str,
        viewer: napari.Viewer,
        window: QtWidgets.QMdiSubWindow,
    ) -> None:
        self.name = name
        self.viewer = viewer
        self.window = window
        # The geometry this application last gave the window. A Move or
        # Resize event that arrives carrying exactly this was caused by
        # tiling or placement, not by the operator dragging anything.
        self.expected_geometry: QtCore.QRect | None = None
        # Set while this class drives the camera, so the zoom that
        # results is not mistaken for the operator's own.
        self._scaling = False
        # Latched by any zoom this class did not cause. A panel whose
        # view the operator chose is not refitted when the area re-tiles
        # around it - otherwise zooming into a feature and then starting
        # a second detector would throw the view away.
        self._chosen_by_hand = False
        # The zoom this class last set. napari does not promise when a
        # zoom event is delivered, and one arriving after the fit that
        # caused it would otherwise look like the operator's doing -
        # which is exactly what it did the first time this was written,
        # latching every panel the moment its first layer landed.
        self._expected_zoom: float | None = None
        viewer.camera.events.zoom.connect(self._on_zoom)

    def _on_zoom(self, _event: object = None) -> None:
        """Latch off automatic fitting once the operator zooms."""
        if self._scaling:
            return
        if self._expected_zoom is not None and (
            self.viewer.camera.zoom == self._expected_zoom
        ):
            return
        self._chosen_by_hand = True

    @property
    def scaled_by_hand(self) -> bool:
        """
        Whether the operator chose this panel's scale themselves.

        Returns
        -------
        bool
            True once they have zoomed it, or asked for a scale from the
            View menu — either of which stops the area refitting it.
        """
        return self._chosen_by_hand

    def refit(self) -> None:
        """
        Fit the data to the panel again, unless the operator set the scale.

        Called when the *application* changes a panel's size — tiling it
        because a dataset arrived or left. Data opens fitted, so it has
        to stay fitted when the thing it was fitted to changes size, or
        the first new detector would leave every panel before it clipped.

        A panel the operator has zoomed is left exactly as they left it.
        Their own resize of a window is left alone too: that is
        :class:`_WindowWatcher`'s business, and it deliberately does not
        call this.
        """
        if self._chosen_by_hand:
            return
        self._fit()

    def show_at_actual_resolution(self) -> None:
        """
        Show the data at one screen pixel per acquired pixel, centred.

        Offered from the View menu rather than done on opening: it is
        what to reach for when the question is "what did the detector
        actually record", since it interpolates nothing and shows the
        acquired pixels as they are. Data larger than the panel is
        cropped and panned to.

        Calibration is respected on the way. The zoom is set from
        ``layer.scale`` so it is one screen pixel per *acquired* pixel
        rather than per world unit, and where the axes are scaled
        differently — an anisotropically binned detector — the finest one
        is the one placed at 1:1, so no direction is drawn smaller than
        it was acquired.

        Counts as the operator's own choice, so the area will not refit
        the panel out from under it when it next tiles.
        """
        zoom = self._actual_resolution_zoom()
        self._scaling = True
        try:
            # reset_view first for the centring, which is wanted; its
            # zoom is then overridden, which is the point of this method.
            self.viewer.reset_view()
            if zoom is not None:
                self.viewer.camera.zoom = zoom
            self._expected_zoom = self.viewer.camera.zoom
        finally:
            self._scaling = False
        self._chosen_by_hand = True

    def fit_to_panel(self) -> None:
        """
        Scale the data to fill the panel it is in, whatever shape that is.

        The fallback when the window cannot be sized to the data — the
        operator has resized it themselves, or it was clamped — and the
        View menu's own action. Fitting to a window of the wrong shape
        leaves margin on one axis, which is why :meth:`size_to_content`
        is what opening does instead.

        Clears any scale the operator had chosen, because it is itself a
        choice they just made.
        """
        self._chosen_by_hand = False
        self._fit()

    def world_extent(self) -> tuple[float, float] | None:
        """
        Return how much world the data covers, slow axis first.

        Returns
        -------
        tuple[float, float] | None
            Height and width in world units — pixels for uncalibrated
            data, nanometres or milliradians for calibrated — or None
            when the panel holds nothing to measure.
        """
        for layer in self.viewer.layers:
            data = getattr(layer, "data", None)
            shape = getattr(data, "shape", ())
            if len(shape) < _IMAGE_AXES:
                continue
            height, width = (int(size) for size in shape[-_IMAGE_AXES:])
            scale = tuple(float(step) for step in layer.scale)[-_IMAGE_AXES:]
            return (height * scale[0], width * scale[1])
        return None

    def content_size(self, available: QtCore.QSize) -> QtCore.QSize | None:
        """
        Return the canvas size that holds the data exactly, no margin.

        **A window is sized to its picture, not the other way round.**
        Letterboxing — a frame with black bars where the data is not —
        spends screen on nothing and makes two panels of different
        shapes look like the same panel. So the window takes the data's
        aspect ratio, and the data fills it.

        Small data is magnified rather than shown in a postage stamp: a
        64x64 spectrum-image map is scaled up so its longest side is
        :data:`MIN_LONGEST_SIDE`, which is a window worth looking at
        rather than a thumbnail. Anything already larger than that opens
        at one screen pixel per acquired pixel, and only shrinks if it
        will not otherwise fit on the screen.

        Parameters
        ----------
        available : QtCore.QSize
            The most canvas this window may take, after the area's own
            margins and the window's chrome.

        Returns
        -------
        QtCore.QSize | None
            The canvas size to aim for, or None when there is nothing to
            size to yet.
        """
        extent = self.world_extent()
        natural = self._actual_resolution_zoom()
        if extent is None or natural is None:
            return None
        height, width = (size * natural for size in extent)
        longest = max(height, width)
        if longest <= 0:
            return None
        if longest < MIN_LONGEST_SIDE:
            magnify = MIN_LONGEST_SIDE / longest
            height, width = height * magnify, width * magnify
        # Only ever shrinks: a frame larger than the screen cannot be
        # "just big enough to contain the image" whatever it is told.
        limit = min(
            1.0,
            max(1, available.width()) / width,
            max(1, available.height()) / height,
        )
        return QtCore.QSize(
            max(1, round(width * limit)), max(1, round(height * limit))
        )

    def size_to_content(self, available: QtCore.QSize) -> None:
        """
        Resize the window so the data fills it exactly, and fit the view.

        Parameters
        ----------
        available : QtCore.QSize
            The most canvas this window may take.
        """
        wanted = self.content_size(available)
        if wanted is None:
            return
        canvas = self._canvas_widget()
        if canvas is None or canvas.width() <= 0:
            return
        # The chrome is whatever the sub-window is bigger than its canvas
        # by — title bar, frame, and any napari furniture still showing.
        # Measured rather than assumed, because hiding the layer controls
        # changes it and a theme could change it again.
        margin_x = self.window.width() - canvas.width()
        margin_y = self.window.height() - canvas.height()
        self.window.resize(wanted.width() + margin_x, wanted.height() + margin_y)
        self.fit_to_panel()

    def _canvas_widget(self) -> QtWidgets.QWidget | None:
        """
        Return the Qt widget the canvas draws into.

        Returns
        -------
        QtWidgets.QWidget | None
            The canvas widget, or None if napari's private layout has
            moved and it cannot be found.
        """
        try:
            return self.viewer.window._qt_viewer.canvas.native
        except Exception:  # noqa: BLE001 - any failure means "cannot size"
            return None

    def _fit(self) -> None:
        """
        Fill the panel edge to edge, and record the zoom it produced.

        ``reset_view`` alone leaves a margin of about 5% all round.
        That is sensible for a window of arbitrary shape, and wrong here:
        the window has already been sized to the picture, so the margin
        is the only blank space left in it. The zoom is therefore
        computed against the canvas and set directly, with ``reset_view``
        called first only for the centring.
        """
        self._scaling = True
        try:
            self.viewer.reset_view()
            zoom = self._filling_zoom()
            if zoom is not None:
                self.viewer.camera.zoom = zoom
            self._expected_zoom = self.viewer.camera.zoom
        finally:
            self._scaling = False

    def _filling_zoom(self) -> float | None:
        """
        Return the zoom that covers the canvas exactly.

        Returns
        -------
        float | None
            Canvas pixels per world unit, or None when the extent or the
            canvas is not measurable yet.
        """
        extent = self.world_extent()
        canvas = self._canvas_widget()
        if extent is None or canvas is None:
            return None
        height, width = extent
        if height <= 0 or width <= 0 or canvas.width() <= 0:
            return None
        return min(canvas.height() / height, canvas.width() / width)

    def _actual_resolution_zoom(self) -> float | None:
        """
        Return the camera zoom that draws one screen pixel per data pixel.

        Returns
        -------
        float | None
            The zoom, or None when no layer says what its pixels are
            worth and the current view should simply be left alone.
        """
        scales = [
            float(step)
            for layer in self.viewer.layers
            for step in tuple(layer.scale)[-2:]
            if float(step) > 0
        ]
        if not scales:
            return None
        # The finest axis at 1:1, so an anisotropically binned frame is
        # drawn at least actual size in both directions rather than
        # having its coarse axis shrunk to meet its fine one.
        return 1.0 / min(scales)

    def set_chrome_visible(self, *, visible: bool) -> None:
        """
        Show or hide napari's own panels around this document's canvas.

        Each document is a whole napari window, and at a window's usual
        size its layer controls are a sidebar. At a tile's size they are
        the greater part of it — measured on a three-document preview,
        the controls took more of each panel than the image did, which
        is the wrong way round for a display whose job is to show data.
        They are therefore off by default and toggled for every document
        at once from the View menu.

        Every call reaches into napari's private Qt objects and is
        wrapped accordingly: failing to hide a dock should cost the
        chrome, not the window.

        Parameters
        ----------
        visible : bool
            Whether to show the layer controls and status bar.
        """
        qt_viewer = getattr(self.viewer.window, "_qt_viewer", None)
        with contextlib.suppress(Exception):
            qt_viewer.dockLayerControls.setVisible(visible)
        with contextlib.suppress(Exception):
            self.viewer.window._qt_window.statusBar().setVisible(visible)

    def refresh_scale_bar(self) -> None:
        """
        Show a scale bar for this panel, in its own image's units.

        Per panel rather than per application, because each image carries
        its own calibration: a HAADF map in nanometres beside a
        Ronchigram in reciprocal nanometres beside an EEL spectrum in
        electronvolts. napari draws the bar per viewer and refuses to
        render units at all when one viewer's layers disagree, which is
        the concrete reason a window per dataset was needed rather than
        merely tidier — see :mod:`miainwoodpecker.viewer.axes`.

        A panel with no calibration gets no bar. A bar reading "pixel" is
        not a measurement, and one drawn where the geometry is in pixels
        while the label claims nanometres would be worse than none.

        **Two napari versions label the bar differently, and this serves
        both.** Up to 0.7 the unit belongs to the scale bar and has to be
        set here. From 0.8 that setter is deprecated to a no-op which
        always reads None, and the label comes from ``Layer.units`` —
        which :func:`~miainwoodpecker.viewer.axes.layer_axes` already
        sets when the layer is added, so 0.8 is labelled correctly
        without this method's help. Setting it anyway costs a suppressed
        deprecation on 0.8 and is what keeps 0.7 working, so it is done
        under a warning filter rather than behind a version check: the
        deprecation is expected here, not something to report to whoever
        is running the instrument.

        CI found this, running 0.8 where the lockfile pins 0.7 — the
        code set the scale bar's unit and the tests read it back, so the
        pair agreed with itself on one version and failed on the other,
        where the bar had in fact been right all along.
        """
        unit = None
        for layer in self.viewer.layers:
            calibration = layer.metadata.get(axes.CALIBRATION)
            if calibration is not None:
                unit = axes.scale_bar_unit(calibration)
                break
        with contextlib.suppress(Exception):
            self.viewer.scale_bar.visible = unit is not None
            if unit is not None:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FutureWarning)
                    self.viewer.scale_bar.unit = unit

    def raise_to_front(self) -> None:
        """
        Bring this panel out from under whatever is covering it.

        A minimized window is restored as well as raised: "start the
        camera again and show me it" is not served by un-hiding an icon.
        """
        with contextlib.suppress(Exception):
            if self.window.isMinimized():
                self.window.showNormal()
            self.window.raise_()

    def close(self) -> None:
        """
        Tear the document down, viewer last.

        Order matters: the sub-window owns napari's Qt window after
        reparenting, and closing the viewer first leaves the MDI area
        holding a destroyed widget.
        """
        with contextlib.suppress(Exception):
            self.viewer.camera.events.zoom.disconnect(self._on_zoom)
        with contextlib.suppress(RuntimeError):
            self.window.close()
        with contextlib.suppress(Exception):
            self.viewer.close()


class PanelDocument:
    """
    A document whose content is a plain widget rather than a viewer.

    The spectrum plot is one (see :mod:`miainwoodpecker.viewer.plots`),
    and it is here because a spectrometer's readout is a dataset like any
    other: it deserves a window of its own, tiled beside the images, and
    closing it should mean the same thing as closing theirs. What it does
    not have is a napari viewer, a camera, layers, or a picture with an
    aspect ratio — so this implements the same small protocol
    :class:`DocumentArea` drives every document through, and answers the
    parts that do not apply by saying so rather than by pretending.

    **Duck-typed rather than a shared base class**, for the reason the
    module docstring gives about :class:`DocumentBoard`: the two have a
    protocol in common and nothing else, and a base class holding one
    would be a place for napari behaviour to leak into a widget that has
    none.

    Parameters
    ----------
    name : str
        The dataset's name; the window title, and the key the area holds
        it under.
    widget : QtWidgets.QWidget
        The widget filling the window.
    window : QtWidgets.QMdiSubWindow
        The sub-window it was put into.
    """

    def __init__(
        self,
        name: str,
        widget: QtWidgets.QWidget,
        window: QtWidgets.QMdiSubWindow,
    ) -> None:
        self.name = name
        self.widget = widget
        self.window = window
        self.expected_geometry: QtCore.QRect | None = None

    @property
    def scaled_by_hand(self) -> bool:
        """
        Whether the operator chose this panel's scale themselves.

        Always False. There is no scale to choose: a plot has no pixels
        that could be shown one for one, and re-applying its one natural
        shape when the area re-tiles gives the same answer every time.
        Zooming *inside* the plot is pyqtgraph's own and survives all of
        this untouched, because nothing here touches its view.

        Returns
        -------
        bool
            False.
        """
        return False

    def size_to_content(self, available: QtCore.QSize) -> None:
        """
        Give the window the shape a curve is read in.

        A plot has no content size the way an image does — no pixel count
        to show one for one and no aspect ratio the data asserts — so it
        gets a shape chosen for what it is: wide, because a spectrum is
        long in one direction and an energy axis is read across, and only
        as wide as there is room for.

        Parameters
        ----------
        available : QtCore.QSize
            The most canvas this window may take.
        """
        canvas = self.widget
        if canvas.width() <= 0:
            # Never laid out. The chrome below is measured as "whatever
            # the window is bigger than its canvas by", and against a
            # zero-width canvas that is the entire window - so the panel
            # would open at its own width plus the default window's, and
            # keep growing every time the area re-tiled.
            return
        margin_x = self.window.width() - canvas.width()
        margin_y = self.window.height() - canvas.height()
        width = max(_MIN_DOCUMENT_WIDTH, min(_PLOT_WIDTH, available.width()))
        height = max(
            _MIN_DOCUMENT_HEIGHT,
            min(round(width / _PLOT_ASPECT), available.height()),
        )
        self.window.resize(width + margin_x, height + margin_y)

    def refit(self) -> None:
        """
        Do nothing; the plot fits itself.

        An image has to be re-fitted when its window changes size,
        because the view transform that filled the old one clips or
        letterboxes the new one. A pyqtgraph view has no such transform
        to go stale: it holds a data *range*, and redraws it into
        whatever rectangle it is given.
        """

    def show_at_actual_resolution(self) -> None:
        """
        Do nothing; there is no such thing here.

        Offered on every document by the View menu, and meaningless for
        this one: "one screen pixel per acquired pixel" is a question
        about a picture, and a spectrum's channels are not pixels. It
        answers by leaving the plot alone rather than by being absent
        from the menu, which would make the menu change as panels came
        and went.
        """

    def fit_to_panel(self) -> None:
        """
        Show the whole spectrum again, undoing a zoom.

        The View menu's "Fit panel to data" does have a meaning here, and
        it is pyqtgraph's own auto-range: the counterpart of taking an
        image's whole extent back into view.
        """
        with contextlib.suppress(Exception):
            self.widget.fit_to_panel()

    def set_chrome_visible(self, *, visible: bool) -> None:
        """
        Do nothing; a panel document has no napari chrome to show.

        Parameters
        ----------
        visible : bool
            Ignored. The setting is napari's layer controls, and this
            window holds no layers.
        """

    def raise_to_front(self) -> None:
        """Bring this panel out from under whatever is covering it."""
        with contextlib.suppress(Exception):
            if self.window.isMinimized():
                self.window.showNormal()
            self.window.raise_()

    def close(self) -> None:
        """Close the window, and with it the widget it holds."""
        with contextlib.suppress(RuntimeError):
            self.window.close()


#: What the area holds. Two kinds, because a dataset's window is
#: whichever kind of window that dataset needs — see
#: :class:`PanelDocument`.
AnyDocument = Document | PanelDocument


def _layers_of(document: AnyDocument) -> object:
    """
    Return a document's napari layers, or nothing for a panel document.

    Every layer lookup on the board runs through here, because a plot
    document holds no layers and answering "none" is the truthful
    version of the ``AttributeError`` the alternative raises.

    Parameters
    ----------
    document : AnyDocument
        The document to ask.

    Returns
    -------
    object
        Its ``viewer.layers``, or an empty tuple.
    """
    viewer = getattr(document, "viewer", None)
    return () if viewer is None else viewer.layers


class DocumentArea(QtWidgets.QMdiArea):
    """
    The MDI area the documents live in.

    Parameters
    ----------
    parent : QtWidgets.QWidget | None
        Parent widget, or None for a free-standing area.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._documents: dict[str, AnyDocument] = {}
        self._auto_arrange = True
        # A depth, not a flag: placing a document tiles the area, so the
        # guard nests and an inner exit must not lower an outer one's.
        self._arranging = 0
        # Documents asked to come to the front before they existed. A
        # detector's window is not opened until its first frame arrives,
        # which is after the start button that wants it raised.
        self._pending_raise: set[str] = set()
        self._chrome_visible = False
        #: Called with a document's name when the operator closes its
        #: window, so the board can stop the next frame from reopening it.
        self.on_document_closed: Callable[[str], None] | None = None
        self.setViewMode(QtWidgets.QMdiArea.ViewMode.SubWindowView)

    @property
    def chrome_visible(self) -> bool:
        """
        Whether napari's layer controls are showing on each document.

        Returns
        -------
        bool
            Whether documents are showing napari's layer controls.
        """
        return self._chrome_visible

    def set_chrome_visible(self, *, visible: bool) -> None:
        """
        Show or hide the layer controls on every document, now and later.

        Parameters
        ----------
        visible : bool
            Whether to show them.
        """
        self._chrome_visible = visible
        for document in self._documents.values():
            document.set_chrome_visible(visible=visible)

    @property
    def auto_arrange(self) -> bool:
        """
        Whether documents are still being tiled for the operator.

        Returns
        -------
        bool
            Whether adding or removing a document still re-tiles the
            area. False once the operator has placed a window by hand.
        """
        return self._auto_arrange

    def note_user_arrangement(self) -> None:
        """
        Record that the operator moved or resized a window themselves.

        From here on the area is theirs: documents are added into
        whatever space is clearest rather than by re-tiling everything
        around them. :meth:`arrange` undoes this.
        """
        if not self._arranging:
            self._auto_arrange = False

    @contextlib.contextmanager
    def _own_arrangement(self) -> Iterator[None]:
        """
        Move windows without mistaking the result for the operator's doing.

        Two guards are needed rather than one, because Qt does not
        promise when a Move or Resize event is delivered. Ones sent
        synchronously, inside ``tileSubWindows``, are caught by the flag;
        ones queued and delivered after this block has exited are caught
        by :attr:`Document.expected_geometry`, which is recorded here
        while the flag is still up. Either guard alone lets programmatic
        tiling latch the area into hand-arranged mode, which is what it
        did before both were in place.

        Yields
        ------
        None
            With the guard raised for the duration.
        """
        self._arranging += 1
        try:
            yield
        finally:
            for document in self._documents.values():
                document.expected_geometry = QtCore.QRect(document.window.geometry())
            self._arranging -= 1

    def resizeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 - Qt override
        """
        Keep every panel inside the application window as it changes size.

        Shrinking the application must not leave panels hanging off the
        edge where they cannot be reached, so they are brought back in —
        and shrunk if the window is now smaller than they are — whether
        or not the area is still arranging itself. Re-packing on top of
        that only happens while it is.

        Parameters
        ----------
        event : QtCore.QEvent
            The resize event.
        """
        super().resizeEvent(event)
        if not self._documents:
            return
        if self._auto_arrange:
            self._tile()
        else:
            self.keep_all_inside()

    def documents(self) -> tuple[AnyDocument, ...]:
        """
        Return every open document.

        Returns
        -------
        tuple[AnyDocument, ...]
            Every open document, in the order it was opened.
        """
        return tuple(self._documents.values())

    def document(self, name: str) -> AnyDocument | None:
        """
        Return one document by name.

        Parameters
        ----------
        name : str
            The document's name.

        Returns
        -------
        AnyDocument | None
            The document, or None if nothing by that name is open.
        """
        return self._documents.get(name)

    def arrange(self) -> None:
        """
        Tile every document, and resume tiling as documents come and go.

        This is the way back from a hand-arranged area — the operator's
        equivalent of "put this straight again".
        """
        self._auto_arrange = True
        self._tile()

    def active_document(self) -> AnyDocument | None:
        """
        Return the document whose window is in front, if any.

        Returns
        -------
        AnyDocument | None
            The active document, or None when the area holds none or
            nothing is focused.
        """
        active = self.activeSubWindow()
        if active is None:
            return None
        for document in self._documents.values():
            if document.window is active:
                return document
        return None

    def raise_document(self, name: str) -> None:
        """
        Bring a document to the front, now or as soon as it opens.

        Starting a detector or camera calls this. A panel is allowed to
        be covered while it runs — that is the operator's arrangement to
        make — but starting a source again is a request to see it, and
        it would be a poor answer to leave the picture underneath
        another window. When the document does not exist yet, because
        its first frame has not arrived, the request is held until
        :meth:`open` creates it.

        Parameters
        ----------
        name : str
            The document's name.
        """
        document = self._documents.get(name)
        if document is None:
            self._pending_raise.add(name)
            return
        with self._own_arrangement():
            document.raise_to_front()
            self.setActiveSubWindow(document.window)

    def settle(self, document: AnyDocument, *, opened: bool) -> None:
        """
        Size a document to the data it has just been given.

        Called by the board once a layer exists, because a document is
        created before its first layer is added and there is nothing to
        size to until then. Sizing on open instead left every panel at
        the same default whatever it held — which is exactly the
        letterboxing this exists to remove, and it is what the first
        attempt did.

        **Only a newly opened document is moved.** Data arriving in a
        panel that is already on screen resizes it in place: re-placing
        it would drag a window the operator had positioned back into the
        pack every time a recording was reopened into it.

        Parameters
        ----------
        document : AnyDocument
            The document whose data just changed.
        opened : bool
            Whether this call follows the document being created, as
            opposed to new data landing in one already open.
        """
        with self._own_arrangement():
            document.size_to_content(self.available_canvas())
            self.keep_inside(document.window)
        if not opened:
            return
        if self._auto_arrange:
            self._tile()
        else:
            with self._own_arrangement():
                document.window.move(
                    self.free_position(document.window.size(), skip=document.window)
                )
                self.keep_inside(document.window)

    def available_canvas(self) -> QtCore.QSize:
        """
        Return the largest canvas one document may take.

        Returns
        -------
        QtCore.QSize
            The area's own size less a margin for the window's title bar
            and frame, so a panel sized to this still fits inside it.
        """
        return QtCore.QSize(
            max(_MIN_DOCUMENT_WIDTH, self.width() - _WINDOW_CHROME),
            max(_MIN_DOCUMENT_HEIGHT, self.height() - _WINDOW_CHROME),
        )

    def _tile(self) -> None:
        """
        Lay the windows out beside each other, at the size their data asked for.

        **Not** ``tileSubWindows``, which divides the whole area between
        the windows and so gives each the *area's* shape rather than its
        picture's — putting black bars in every panel that is not the
        same shape as the screen. Tiling here means what it is wanted
        for: images next to each other rather than on top of each other.

        Packed in rows, in the order the documents were opened: along
        until the next one would overhang, then down by the tallest in
        the row. A window too big for a row of its own is left at the
        left margin and simply overhangs, which is visible and
        recoverable, unlike shrinking it silently.
        """
        with self._own_arrangement():
            available = self.available_canvas()
            x = y = tallest = covered = 0
            for document in self._documents.values():
                window = document.window
                # Back to the size its data asks for before packing, so
                # tiling twice gives the same answer as tiling once —
                # without this the shrink below would take another slice
                # off every window on every pass.
                #
                # Except where the operator has set the scale: resizing
                # such a panel would refit it and throw that away, so
                # tiling moves it and otherwise leaves it alone.
                if not document.scaled_by_hand:
                    document.size_to_content(available)
                size = window.size()
                if x and x + size.width() > self.width():
                    if not self._shrink_into_row(window, self.width() - x):
                        x, y = 0, y + tallest
                        tallest = 0
                    size = window.size()
                if y + size.height() > self.height():
                    # Out of rows. Overlap is allowed from here — a
                    # window may never go off the edge to avoid it — but
                    # each is offset from the last so the one underneath
                    # keeps a visible corner and title bar, rather than
                    # being buried exactly.
                    window.move(self._staggered_position(size, covered))
                    covered += 1
                    continue
                window.move(QtCore.QPoint(x, y))
                x += size.width() + _TILE_GAP
                tallest = max(tallest, size.height() + _TILE_GAP)
            for document in self._documents.values():
                self.keep_inside(document.window)

    def open(self, name: str) -> Document:
        """
        Open a document, or return the one already open under that name.

        Parameters
        ----------
        name : str
            The dataset's name; becomes the window title.

        Returns
        -------
        Document
            The document, newly opened or already there.
        """
        existing = self._documents.get(name)
        if isinstance(existing, Document):
            return existing
        if existing is not None:
            # A dataset that used to be a plot and is now an image: a
            # spectrometer put back into an imaging readout is exactly
            # this. The window is replaced rather than reused, because
            # the two hold different widgets - and closed here rather
            # than left for the operator to find, since two windows with
            # one name is the ambiguity the naming exists to prevent.
            self.close_document(name)
        viewer = napari.Viewer(show=False, title=name)
        window = self._reparent(viewer, name=name)
        document = Document(name, viewer, window)
        document.set_chrome_visible(visible=self._chrome_visible)
        self._documents[name] = document
        window.installEventFilter(_WindowWatcher(self, document))
        with self._own_arrangement():
            # Shown before placed, not after: tileSubWindows only lays out
            # sub-windows that are visible, so tiling a window still
            # hidden left it at a default geometry on top of its
            # neighbour - which looked like a panel that had lost its
            # title bar, because the panel above was covering it.
            # Opened at a modest default and sized properly once it has
            # data: a document is created before its first layer is
            # added, so there is nothing to size to yet. See
            # DocumentArea.settle, which the board calls after the add.
            window.resize(
                max(_MIN_DOCUMENT_WIDTH, self.width() // 2),
                max(_MIN_DOCUMENT_HEIGHT, self.height() // 2),
            )
            window.show()
            self._place(window)
        if name in self._pending_raise:
            self._pending_raise.discard(name)
            self.raise_document(name)
        return document

    def open_panel(
        self,
        name: str,
        widget: QtWidgets.QWidget,
    ) -> PanelDocument:
        """
        Open a document holding a plain widget, or return the one open.

        The counterpart of :meth:`open` for a dataset whose display is
        not a napari canvas — today a spectrum plot. Everything after
        the widget is the same: the window is placed and tiled with the
        others, closing it means what closing theirs means, and the
        widget is what the caller pushes data into afterwards.

        **The widget is only used when a document is actually opened.**
        A caller asking for a panel that already exists gets the one
        that is there and the widget it passed is dropped, so building
        one per frame would be wasteful rather than wrong — see
        :meth:`DocumentBoard.panel`, which is how this is reached and
        which builds one only when it has to.

        Parameters
        ----------
        name : str
            The dataset's name; becomes the window title.
        widget : QtWidgets.QWidget
            The widget to fill the window with.

        Returns
        -------
        PanelDocument
            The document, newly opened or already there.
        """
        existing = self._documents.get(name)
        if isinstance(existing, PanelDocument):
            return existing
        if existing is not None:
            # The mirror of open()'s case: a camera that was imaging and
            # is now projecting keeps its name and stops being a picture.
            self.close_document(name)
        window = self.addSubWindow(widget)
        window.setWindowTitle(name)
        document = PanelDocument(name, widget, window)
        self._documents[name] = document
        window.installEventFilter(_WindowWatcher(self, document))
        with self._own_arrangement():
            # Shown before placed, for the reason open() gives. Sized
            # *after* showing rather than before, which is the one thing
            # this does differently: a plot has its shape from the start
            # and could be sized immediately, but a widget Qt has not
            # laid out yet reports no width, and the sizing measures the
            # window's chrome against it.
            window.resize(
                max(_MIN_DOCUMENT_WIDTH, self.width() // 2),
                max(_MIN_DOCUMENT_HEIGHT, self.height() // 2),
            )
            window.show()
            self._place(window)
            document.size_to_content(self.available_canvas())
            self.keep_inside(window)
        if self._auto_arrange:
            self._tile()
        if name in self._pending_raise:
            self._pending_raise.discard(name)
            self.raise_document(name)
        return document

    def _reparent(
        self,
        viewer: napari.Viewer,
        *,
        name: str,
    ) -> QtWidgets.QMdiSubWindow:
        """
        Put a viewer's window into a sub-window and strip its chrome.

        Each viewer is a full napari main window, which inside a panel
        means a menu bar and a layer list per dataset. The menu bar goes
        because the host window carries one, and the layer list because a
        document holds a single layer and a list of one is furniture.
        The layer controls are hidden by default and toggled from the
        View menu — see :meth:`Document.set_chrome_visible` for why a
        sidebar that is reasonable on a window is not on a tile.

        Every one of those reaches into napari's private Qt objects, so
        each is attempted separately and a failure leaves that piece of
        chrome on screen rather than stopping the window from opening.

        Parameters
        ----------
        viewer : napari.Viewer
            The viewer to reparent.
        name : str
            Window title.

        Returns
        -------
        QtWidgets.QMdiSubWindow
            The sub-window now holding the viewer.
        """
        qt_window = viewer.window._qt_window
        with contextlib.suppress(Exception):
            qt_window.menuBar().setVisible(False)
        with contextlib.suppress(Exception):
            viewer.window._qt_viewer.dockLayerList.setVisible(False)
        window = self.addSubWindow(qt_window)
        window.setWindowTitle(name)
        return window

    def _place(self, window: QtWidgets.QMdiSubWindow) -> None:
        """
        Position a newly opened sub-window, without resizing it.

        **Placed rather than tiled**, and that is the consequence of
        windows being sized to their data. ``tileSubWindows`` divides the
        whole area between the windows, which gives each one the area's
        shape rather than its picture's — and a panel whose shape does
        not match its data is a panel with black bars in it, which is the
        thing sizing to content exists to remove. So a new document is
        dropped where it covers least of what is already open, at the
        size its data asked for.

        Tiling is still available deliberately, from **View → Tile
        documents**, for filling the screen when that is what is wanted.

        Parameters
        ----------
        window : QtWidgets.QMdiSubWindow
            The window to place.
        """
        window.move(self.free_position(window.size(), skip=window))

    def free_position(
        self,
        size: QtCore.QSize,
        *,
        skip: QtWidgets.QMdiSubWindow | None = None,
    ) -> QtCore.QPoint:
        """
        Find the clearest place to drop a window of a given size.

        Used when the operator has arranged the area by hand, where
        re-tiling would undo their work: the new document has to go
        somewhere, and somewhere it does not bury an existing panel. A
        coarse grid of candidate positions is scored by how much of the
        existing windows each would cover, and the least covering one
        wins. Full coverage everywhere means a crowded area, and the
        best of a bad set is still better than dropping the window
        squarely on top of the last one.

        Parameters
        ----------
        size : QtCore.QSize
            The size the window will be.
        skip : QtWidgets.QMdiSubWindow | None
            A window to ignore when scoring, normally the one being placed.

        Returns
        -------
        QtCore.QPoint
            Where to move the window to.
        """
        taken = [
            window.geometry()
            for window in self.subWindowList()
            if window is not skip and window.isVisible()
        ]
        span_x = max(0, self.width() - size.width())
        span_y = max(0, self.height() - size.height())
        for row in range(_PLACEMENT_STEPS):
            for column in range(_PLACEMENT_STEPS):
                point = QtCore.QPoint(
                    span_x * column // max(1, _PLACEMENT_STEPS - 1),
                    span_y * row // max(1, _PLACEMENT_STEPS - 1),
                )
                candidate = QtCore.QRect(point, size)
                if not any(
                    _area(candidate.intersected(other)) for other in taken
                ):
                    return point
        # Nowhere clear. Overlapping is allowed - a window must never go
        # off the edge to avoid it - but stacking one exactly on another
        # hides that anything is under there at all, so it is staggered
        # instead and the covered window keeps a visible corner.
        return self._staggered_position(size, len(taken))

    def _staggered_position(self, size: QtCore.QSize, depth: int) -> QtCore.QPoint:
        """
        Return an offset position, for when overlap cannot be avoided.

        Parameters
        ----------
        size : QtCore.QSize
            The size the window will be.
        depth : int
            How many windows are already open, which sets how far in
            this one starts.

        Returns
        -------
        QtCore.QPoint
            A position inside the area, offset from the corner.
        """
        span_x = max(0, self.width() - size.width())
        span_y = max(0, self.height() - size.height())
        offset = _CASCADE_OFFSET * (depth + 1)
        return QtCore.QPoint(
            offset % (span_x + 1) if span_x else 0,
            offset % (span_y + 1) if span_y else 0,
        )

    def _shrink_into_row(
        self,
        window: QtWidgets.QMdiSubWindow,
        room: int,
    ) -> bool:
        """
        Shrink a window a little, if that is all it needs to stay in the row.

        Two 256-pixel panels come to a few pixels more than a dock-narrowed
        workspace, and wrapping the second onto a row that then has no
        vertical room sends it to the overlap fallback — so two panels
        that all but fit side by side end up stacked. Giving up a few per
        cent of one is a far better answer than covering it.

        Both dimensions are scaled together, so the window still matches
        its picture's shape and the picture still fills it exactly.

        Parameters
        ----------
        window : QtWidgets.QMdiSubWindow
            The window that overhangs.
        room : int
            Pixels left in the current row.

        Returns
        -------
        bool
            Whether it now fits. False means the shortfall was too large
            to close this way and the caller should start a new row.
        """
        size = window.size()
        if room <= 0 or size.width() <= 0:
            return False
        factor = room / size.width()
        if factor < 1 - _PACK_SHRINK:
            return False
        window.resize(
            max(1, int(size.width() * factor)),
            max(1, int(size.height() * factor)),
        )
        return True

    def keep_inside(self, window: QtWidgets.QMdiSubWindow) -> None:
        """
        Shrink and nudge a window until none of it is outside the area.

        **No part of a panel may be off the edge**, because the part that
        is cannot be reached: an MDI area scrolls, but a window half
        outside it reads as a window that has gone wrong. A frame too
        big for the workspace is shrunk to it — the picture refits to
        the smaller canvas by itself — and one merely positioned badly
        is moved back in.

        Parameters
        ----------
        window : QtWidgets.QMdiSubWindow
            The window to bring inside.
        """
        size = window.size()
        width = min(size.width(), max(1, self.width()))
        height = min(size.height(), max(1, self.height()))
        if (width, height) != (size.width(), size.height()):
            window.resize(width, height)
        x = min(max(0, window.x()), max(0, self.width() - width))
        y = min(max(0, window.y()), max(0, self.height() - height))
        if (x, y) != (window.x(), window.y()):
            window.move(x, y)

    def keep_all_inside(self) -> None:
        """Bring every window inside the area, without counting it as arranging."""
        with self._own_arrangement():
            for document in self._documents.values():
                self.keep_inside(document.window)

    def close_document(self, name: str) -> None:
        """
        Close one document and re-tile if the area is still automatic.

        Parameters
        ----------
        name : str
            The document to close. Unknown names are ignored, so a
            caller need not check first.
        """
        # A pending raise deliberately survives this. Removing a layer
        # closes its document, and a request to show that dataset again
        # should still be waiting when the next one arrives - unchecking
        # and rechecking a detector is exactly that sequence.
        document = self._documents.pop(name, None)
        if document is None:
            return
        document.close()
        if self._auto_arrange and self._documents:
            self._tile()

    def forget(self, document: AnyDocument) -> None:
        """
        Drop a document the operator closed with its own close button.

        Parameters
        ----------
        document : AnyDocument
            The document whose window is going away.
        """
        self._pending_raise.discard(document.name)
        if self._documents.pop(document.name, None) is None:
            return
        if self.on_document_closed is not None:
            self.on_document_closed(document.name)
        viewer = getattr(document, "viewer", None)
        if viewer is not None:
            with contextlib.suppress(Exception):
                viewer.close()
        if self._auto_arrange and self._documents:
            self._tile()

    def close_all(self) -> None:
        """Close every document, for application shutdown."""
        self._pending_raise.clear()
        for document in tuple(self._documents.values()):
            document.close()
        self._documents.clear()


def _area(rect: QtCore.QRect) -> int:
    """
    Return a rectangle's area, zero if it is empty.

    Parameters
    ----------
    rect : QtCore.QRect
        The rectangle, possibly an empty intersection.

    Returns
    -------
    int
        Its area in pixels squared.
    """
    if rect.isEmpty():
        return 0
    return rect.width() * rect.height()


class _WindowWatcher(QtCore.QObject):
    """
    Turns a sub-window's Qt events into the area's bookkeeping.

    A :class:`~qtpy.QtWidgets.QMdiSubWindow` reports neither "the
    operator dragged me" nor "I am going away" as a signal, so they are
    read off its event stream instead. Parented to the area so it lives
    exactly as long as it is needed.

    Parameters
    ----------
    area : DocumentArea
        The area to report to.
    document : AnyDocument
        The document this watches over.
    """

    def __init__(self, area: DocumentArea, document: AnyDocument) -> None:
        super().__init__(area)
        self._area = area
        self._document = document

    def eventFilter(  # noqa: N802 - Qt override
        self,
        watched: QtCore.QObject,
        event: QtCore.QEvent,
    ) -> bool:
        """
        Note operator arrangement, and forget the document on close.

        Parameters
        ----------
        watched : QtCore.QObject
            The sub-window.
        event : QtCore.QEvent
            The event it received.

        Returns
        -------
        bool
            False always: these events are observed, never consumed.
        """
        kind = event.type()
        if kind in (QtCore.QEvent.Type.Move, QtCore.QEvent.Type.Resize):
            ours = self._document.window.geometry() == (
                self._document.expected_geometry
            )
            if not ours:
                self._area.note_user_arrangement()
            if kind == QtCore.QEvent.Type.Resize:
                # Refitted whoever resized it, and *here* rather than
                # straight after the resize call, because Qt resizes the
                # canvas afterwards: fitting immediately fits to the size
                # the panel is about to stop being, which showed as an
                # image drawn 44% taller than the panel holding it.
                #
                # A window the operator reshapes gets blank space on one
                # axis - unavoidable once the frame stops matching the
                # picture - but the whole picture stays visible, which
                # not refitting cost: it overflowed by 89%.
                self._document.refit()
        elif kind == QtCore.QEvent.Type.Close:
            self._area.forget(self._document)
        return super().eventFilter(watched, event)


class _LayerIndex:
    """
    ``viewer.layers`` as the widget uses it, across every document.

    Only the four operations
    :class:`~miainwoodpecker.viewer.live.LiveInstrumentWidget` performs —
    ``in``, ``[name]``, ``del [name]``, and iteration — are provided.
    This is deliberately not a general ``LayerList``: anything broader
    would be a promise about napari behaviour spread over many windows
    that this class has no way to keep.

    Parameters
    ----------
    board : DocumentBoard
        The board whose documents are indexed.
    """

    def __init__(self, board: DocumentBoard) -> None:
        self._board = board

    def __contains__(self, name: object) -> bool:
        """
        Report whether any document holds a layer by this name.

        Parameters
        ----------
        name : object
            A layer name.

        Returns
        -------
        bool
            Whether any document holds a layer by that name.
        """
        return self._board.find(str(name)) is not None

    def __getitem__(self, name: str) -> Layer:
        """
        Return a layer by name, from whichever document holds it.

        Parameters
        ----------
        name : str
            A layer name.

        Returns
        -------
        Layer
            The layer.

        Raises
        ------
        KeyError
            If no document holds a layer by that name.
        """
        found = self._board.find(name)
        if found is None:
            raise KeyError(name)
        return found

    def __delitem__(self, name: str) -> None:
        """
        Remove a layer, closing its document if it was the last one in it.

        Parameters
        ----------
        name : str
            The layer to remove.

        Raises
        ------
        KeyError
            If no document holds a layer by that name.
        """
        if not self._board.remove(name):
            raise KeyError(name)

    def __iter__(self) -> Iterator[Layer]:
        """
        Iterate over every layer in every document.

        Yields
        ------
        Layer
            Each layer, document by document.
        """
        for document in self._board.area.documents():
            yield from _layers_of(document)

    def __len__(self) -> int:
        """
        Count the layers open across all documents.

        Returns
        -------
        int
            How many layers are open across all documents.
        """
        return sum(
            len(_layers_of(document))
            for document in self._board.area.documents()
        )


class DocumentBoard:
    """
    Routes layers to documents, presenting a viewer's interface.

    Stands where a :class:`napari.Viewer` used to, so
    :class:`~miainwoodpecker.viewer.live.LiveInstrumentWidget` needs no
    knowledge of documents at all — see the module docstring on why this
    is duck-typed rather than a shared base class.

    Parameters
    ----------
    area : DocumentArea
        The MDI area to open documents in.
    """

    def __init__(self, area: DocumentArea) -> None:
        self.area = area
        self._layers = _LayerIndex(self)
        # Layer name to the document holding it. Usually a layer's own
        # name, differing only for annotations attached to another layer.
        self._homes: dict[str, str] = {}
        # Layers whose window the operator closed. A running detector
        # keeps producing frames after its panel is shut, and reopening
        # the window 33 ms later would make the close button useless;
        # instead the panel stays shut until the source is started
        # again, which is what raise_document undoes.
        self._dismissed: set[str] = set()
        area.on_document_closed = self._on_document_closed

    def _on_document_closed(self, document_name: str) -> None:
        """
        Remember that the operator shut a panel, and what was in it.

        Parameters
        ----------
        document_name : str
            The closed document.
        """
        for layer_name, home in list(self._homes.items()):
            if home == document_name:
                self._dismissed.add(layer_name)
                del self._homes[layer_name]
        self._dismissed.add(document_name)

    @property
    def layers(self) -> _LayerIndex:
        """
        The layers across every document, as a viewer's ``layers`` is.

        Returns
        -------
        _LayerIndex
            The layers across every document.
        """
        return self._layers

    def raise_document(self, name: str) -> None:
        """
        Bring the document holding a layer to the front.

        The widget calls this when a detector or camera is started. It
        exists on the board and not on :class:`napari.Viewer`, which is
        why the widget asks for it by name and does without when it is
        running against a plain single-canvas viewer.

        Parameters
        ----------
        name : str
            The layer's name, which is also its document's unless the
            layer is an annotation attached to another.
        """
        # Asking for a panel is also how a closed one is asked back.
        # Starting a detector again should show it, whether it was
        # buried under another window or shut altogether.
        self._dismissed.discard(name)
        self.area.raise_document(self._homes.get(name, name))

    def panel(
        self,
        name: str,
        factory: Callable[[], QtWidgets.QWidget],
    ) -> QtWidgets.QWidget | None:
        """
        Return the widget in a panel document, opening it if need be.

        The one route to a display that is not a napari layer — today a
        spectrum plot. It is a *method on the board and not on*
        :class:`napari.Viewer`, like :meth:`raise_document`, which is
        what makes it optional: a widget running against a plain
        single-canvas viewer asks for it by name, does not find it, and
        falls back to whatever it can do there.

        The widget is built by ``factory`` only when a document actually
        has to be opened, so a caller may ask on every frame — which is
        exactly how the live display uses this — without building a
        pyqtgraph plot sixty times a second.

        A panel the operator has closed stays closed, and answers None,
        for the reason :meth:`add_image` drops a layer whose panel was
        dismissed: the next frame is 16 ms away and would otherwise make
        the close button a blink. :meth:`raise_document` asks it back.

        Parameters
        ----------
        name : str
            The dataset's name, which is its document's.
        factory : Callable[[], QtWidgets.QWidget]
            Builds the widget, called only when opening.

        Returns
        -------
        QtWidgets.QWidget | None
            The widget to push data into, or None when the operator has
            closed this panel and has not asked for it back.
        """
        if name in self._dismissed:
            return None
        existing = self.area.document(name)
        if isinstance(existing, PanelDocument):
            return existing.widget
        document = self.area.open_panel(name, factory())
        self._homes[name] = name
        return document.widget

    def find(self, name: str) -> Layer | None:
        """
        Return a layer by name, from whichever document holds it.

        Parameters
        ----------
        name : str
            The layer name.

        Returns
        -------
        Layer | None
            The layer, or None if it is not open.
        """
        document = self.area.document(self._homes.get(name, name))
        if document is None:
            return None
        layers = _layers_of(document)
        if name not in layers:
            return None
        return layers[name]

    def remove(self, name: str) -> bool:
        """
        Remove a layer, closing its document once it holds nothing.

        Parameters
        ----------
        name : str
            The layer to remove.

        Returns
        -------
        bool
            Whether there was such a layer to remove.
        """
        home = self._homes.get(name, name)
        document = self.area.document(home)
        if document is None:
            return False
        layers = _layers_of(document)
        if name not in layers:
            return False
        del layers[name]
        self._homes.pop(name, None)
        if not len(layers):
            self.area.close_document(home)
        return True

    def add_image(self, data: object, **kwargs: object) -> Layer | None:
        """
        Add or replace an image layer, in its own document.

        Parameters
        ----------
        data : object
            The array to display.
        **kwargs : object
            Passed to :meth:`napari.Viewer.add_image`; ``name`` decides
            which document the layer belongs to, and ``metadata`` may
            carry :data:`ATTACHED_TO`.

        Returns
        -------
        Layer
            The new layer.
        """
        return self._add("add_image", data, kwargs)

    def add_shapes(self, data: object, **kwargs: object) -> Layer | None:
        """
        Add or replace a shapes layer, normally attached to an image.

        Parameters
        ----------
        data : object
            The shape data.
        **kwargs : object
            Passed to :meth:`napari.Viewer.add_shapes`; see
            :meth:`add_image` on ``name`` and ``metadata``.

        Returns
        -------
        Layer
            The new layer.
        """
        return self._add("add_shapes", data, kwargs)

    def _add(
        self,
        method: str,
        data: object,
        kwargs: dict[str, object],
    ) -> Layer | None:
        """
        Open the right document and add the layer to it.

        Re-adding an existing name replaces the layer *inside* its
        document rather than closing and reopening the window, so a
        panel the operator has placed and sized keeps its place when the
        data in it is replaced.

        Parameters
        ----------
        method : str
            The viewer method to call, ``"add_image"`` or ``"add_shapes"``.
        data : object
            The layer's data.
        kwargs : dict[str, object]
            Keyword arguments for the viewer method.

        Returns
        -------
        Layer | None
            The new layer, or None when the operator has closed this
            panel and has not asked for it back.
        """
        name = str(kwargs.get("name", ""))
        if name in self._dismissed:
            return None
        metadata = kwargs.get("metadata") or {}
        metadata = typing.cast("dict[str, object]", metadata)
        home = str(metadata.get(ATTACHED_TO) or name)
        opened = self.area.document(home) is None
        document = self.area.open(home)
        if name in document.viewer.layers:
            del document.viewer.layers[name]
        layer = getattr(document.viewer, method)(data, **kwargs)
        self._homes[name] = home
        # Now that there is data, the window can take its shape and the
        # picture can fill it with no black bars. Sized here rather than
        # in open() because a document exists before its first layer.
        self.area.settle(document, opened=opened)
        document.refresh_scale_bar()
        # A live layer's calibration can change under it - changing the
        # field of view rescales the panel without replacing the layer -
        # and that arrives as a units event rather than through here.
        with contextlib.suppress(Exception):
            layer.events.units.connect(
                lambda _event=None, panel=document: panel.refresh_scale_bar()
            )
        return layer


class DocumentWindow(QtWidgets.QMainWindow):
    """
    The application window: documents in the middle, instrument on the right.

    Replaces the napari main window both entry points used to open. That
    window *was* the single canvas, so there was nowhere for a second
    dataset to go; here the canvas is one document among several and the
    window itself only holds them and the instrument dock.

    Parameters
    ----------
    title : str
        Window title.
    parent : QtWidgets.QWidget | None
        Parent widget, or None for a top-level window.
    """

    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.area = DocumentArea(self)
        self.board = DocumentBoard(self.area)
        self.setCentralWidget(self.area)
        self._panel: QtWidgets.QWidget | None = None
        view = self.menuBar().addMenu("&View")
        tile = view.addAction("&Tile documents")
        tile.setShortcut("Ctrl+T")
        tile.triggered.connect(self.area.arrange)
        cascade = view.addAction("&Cascade documents")
        cascade.triggered.connect(self.area.cascadeSubWindows)
        view.addSeparator()
        actual = view.addAction("&Actual resolution")
        actual.setShortcut("Ctrl+1")
        actual.triggered.connect(
            lambda: self._on_active("show_at_actual_resolution")
        )
        fit = view.addAction("&Fit panel to data")
        fit.setShortcut("Ctrl+0")
        fit.triggered.connect(lambda: self._on_active("fit_to_panel"))
        view.addSeparator()
        controls = view.addAction("Show &layer controls")
        controls.setCheckable(True)
        controls.setChecked(self.area.chrome_visible)
        controls.toggled.connect(
            lambda shown: self.area.set_chrome_visible(visible=shown)
        )

    def _on_active(self, action: str) -> None:
        """
        Apply a view action to whichever panel is in front.

        **Dispatched by name**, so the menu works on any kind of
        document. A plot has no "actual resolution" and a picture has no
        auto-range, and each answers what it can — see
        :class:`PanelDocument`. Naming the method rather than passing an
        unbound :class:`Document` one is what lets both be in the area
        at once; the alternative applies a napari document's method to a
        widget that is not one.

        Parameters
        ----------
        action : str
            The document method to run. Nothing happens if the area has
            no active panel, or if that panel does not offer it.
        """
        document = self.area.active_document()
        if document is None:
            return
        method = getattr(document, action, None)
        if method is not None:
            method()

    def set_panel(self, panel: QtWidgets.QWidget, *, name: str = "Instrument") -> None:
        """
        Dock the instrument controls on the right.

        Taken after construction rather than in ``__init__`` because the
        panel is built against :attr:`board`, which does not exist until
        this window does.

        Parameters
        ----------
        panel : QtWidgets.QWidget
            The instrument widget.
        name : str
            The dock's title.
        """
        self._panel = panel
        dock = QtWidgets.QDockWidget(name, self)
        dock.setObjectName(name)
        dock.setWidget(panel)
        dock.setAllowedAreas(
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea
            | QtCore.Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def closeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 - Qt override
        """
        Stop the instrument and close every document on the way out.

        napari's main window used to do the first half of this by
        firing ``closeEvent`` at the docked widget during app-quit
        teardown. Nothing does that for a plain
        :class:`~qtpy.QtWidgets.QMainWindow`, so the panel is shut down
        explicitly here; ``LiveInstrumentWidget.shutdown`` is documented
        safe to call twice, which is what makes an explicit call safe
        alongside whatever Qt does.

        Parameters
        ----------
        event : QtCore.QEvent
            The close event.
        """
        panel = self._panel
        shutdown = getattr(panel, "shutdown", None)
        if shutdown is not None:
            with contextlib.suppress(Exception):
                shutdown()
        self.area.close_all()
        super().closeEvent(event)


def open_window(title: str) -> DocumentWindow:
    """
    Build the application window, with a Qt application behind it.

    Both entry points call this before building the instrument widget:
    Qt objects cannot be constructed without a ``QApplication``, and
    napari used to create one as a side effect of ``napari.Viewer()``.
    With the viewers now created per document — none of which exists at
    startup — the application has to be asked for directly.

    Parameters
    ----------
    title : str
        Window title.

    Returns
    -------
    DocumentWindow
        The window, not yet shown.
    """
    napari.qt.get_qapp()
    return DocumentWindow(title)
