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
napari's camera is isotropic: one zoom scalar drives both axes. An image
in a panel of the wrong shape therefore letterboxes — margin at the
sides or top — and cannot be stretched by resizing a window, tiling, or
anything else this module does. ``tests/integration/test_documents.py``
measures drawn aspect against data aspect after a resize rather than
taking that on trust.

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

import napari
import napari.qt
from qtpy import QtCore, QtWidgets

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from napari.layers import Layer

#: Metadata key naming the layer an annotation is drawn in the
#: coordinates of. Such a layer shares that layer's document.
ATTACHED_TO = "miainwoodpecker_attached_to"

_MIN_DOCUMENT_WIDTH = 320
_MIN_DOCUMENT_HEIGHT = 240
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
        # Set while this class drives the camera, so the zoom event that
        # results is not mistaken for the operator zooming by hand.
        self._refitting = False
        # Latched by any zoom this class did not cause. A panel the
        # operator has zoomed stops being refitted when it is resized:
        # refitting it would throw away the view they chose, which is a
        # worse failure than an image that no longer fills its panel.
        self._zoomed_by_hand = False
        # The zoom this class last set. As with window geometry, napari
        # does not promise when a zoom event is delivered, and one that
        # arrives after the refit that caused it would otherwise look
        # like the operator's doing - which it did, latching every panel
        # the moment its first layer was added.
        self._expected_zoom: float | None = None
        viewer.camera.events.zoom.connect(self._on_zoom)

    def _on_zoom(self, _event: object = None) -> None:
        """Latch off automatic refitting once the operator zooms."""
        if self._refitting:
            return
        if self._expected_zoom is not None and (
            self.viewer.camera.zoom == self._expected_zoom
        ):
            return
        self._zoomed_by_hand = True

    @property
    def zoomed_by_hand(self) -> bool:
        """
        Whether this panel's view is the operator's own.

        Returns
        -------
        bool
            Whether the operator has zoomed this panel themselves, which
            stops it being refitted when the window is resized.
        """
        return self._zoomed_by_hand

    def refit(self) -> None:
        """
        Fit the data to the panel again, unless the operator has zoomed it.

        Called when the window is resized, so that dragging a window's
        edge grows and shrinks the picture rather than revealing more
        empty canvas around it. The fit is isotropic — it is napari's
        own ``reset_view`` — so this cannot distort the image.
        """
        if self._zoomed_by_hand:
            return
        self._fit()

    def fit_now(self) -> None:
        """
        Fit the data to the panel, overriding any zoom the operator made.

        Used when a document's data is added or replaced outright — a
        recording opened into the panel, an analysis result written into
        it. Refitting there is not discarding the operator's view of
        this data; it is the first view of *different* data, so the
        panel starts over rather than inheriting a zoom that framed
        something else.
        """
        self._zoomed_by_hand = False
        self._fit()

    def _fit(self) -> None:
        """Reset the view and record the zoom that produced it."""
        self._refitting = True
        try:
            self.viewer.reset_view()
            self._expected_zoom = self.viewer.camera.zoom
        finally:
            self._refitting = False

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
        self._documents: dict[str, Document] = {}
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
        Re-tile when the application window is resized, if tiling is still on.

        Without this the panels keep the sizes they had for the old
        window and leave a growing empty margin. It also keeps every
        window's geometry equal to what this class expects, so resizing
        the application is not mistaken for arranging the documents.

        Parameters
        ----------
        event : QtCore.QEvent
            The resize event.
        """
        super().resizeEvent(event)
        if self._auto_arrange and self._documents:
            self._tile()

    def documents(self) -> tuple[Document, ...]:
        """
        Return every open document.

        Returns
        -------
        tuple[Document, ...]
            Every open document, in the order it was opened.
        """
        return tuple(self._documents.values())

    def document(self, name: str) -> Document | None:
        """
        Return one document by name.

        Parameters
        ----------
        name : str
            The document's name.

        Returns
        -------
        Document | None
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

    def _tile(self) -> None:
        """Tile the sub-windows without mistaking it for the operator's doing."""
        with self._own_arrangement():
            self.tileSubWindows()
        for document in self._documents.values():
            document.refit()

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
        if existing is not None:
            return existing
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
            window.show()
            self._place(window)
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
        Size and position a newly opened sub-window.

        Parameters
        ----------
        window : QtWidgets.QMdiSubWindow
            The window to place.
        """
        size = QtCore.QSize(
            max(_MIN_DOCUMENT_WIDTH, self.width() // 2),
            max(_MIN_DOCUMENT_HEIGHT, self.height() // 2),
        )
        window.resize(size)
        if self._auto_arrange:
            self._tile()
            return
        window.move(self.free_position(size, skip=window))

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
        best = QtCore.QPoint(0, 0)
        best_overlap: int | None = None
        for row in range(_PLACEMENT_STEPS):
            for column in range(_PLACEMENT_STEPS):
                point = QtCore.QPoint(
                    span_x * column // max(1, _PLACEMENT_STEPS - 1),
                    span_y * row // max(1, _PLACEMENT_STEPS - 1),
                )
                candidate = QtCore.QRect(point, size)
                overlap = sum(
                    _area(candidate.intersected(other)) for other in taken
                )
                if best_overlap is None or overlap < best_overlap:
                    best, best_overlap = point, overlap
                if not overlap:
                    return point
        return best

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

    def forget(self, document: Document) -> None:
        """
        Drop a document the operator closed with its own close button.

        Parameters
        ----------
        document : Document
            The document whose window is going away.
        """
        self._pending_raise.discard(document.name)
        if self._documents.pop(document.name, None) is None:
            return
        if self.on_document_closed is not None:
            self.on_document_closed(document.name)
        with contextlib.suppress(Exception):
            document.viewer.close()
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
    document : Document
        The document this watches over.
    """

    def __init__(self, area: DocumentArea, document: Document) -> None:
        super().__init__(area)
        self._area = area
        self._document = document

    def eventFilter(  # noqa: N802 - Qt override
        self,
        watched: QtCore.QObject,
        event: QtCore.QEvent,
    ) -> bool:
        """
        Note operator arrangement, refit on resize, and forget on close.

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
            if self._document.window.geometry() != self._document.expected_geometry:
                self._area.note_user_arrangement()
            if kind == QtCore.QEvent.Type.Resize:
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
            yield from document.viewer.layers

    def __len__(self) -> int:
        """
        Count the layers open across all documents.

        Returns
        -------
        int
            How many layers are open across all documents.
        """
        return sum(
            len(document.viewer.layers)
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
        if name not in document.viewer.layers:
            return None
        return document.viewer.layers[name]

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
        if document is None or name not in document.viewer.layers:
            return False
        del document.viewer.layers[name]
        self._homes.pop(name, None)
        if not len(document.viewer.layers):
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
        document = self.area.open(home)
        if name in document.viewer.layers:
            del document.viewer.layers[name]
        layer = getattr(document.viewer, method)(data, **kwargs)
        self._homes[name] = home
        # fit_now, not refit: adding a layer is napari's own cue to reset
        # the view, and the zoom event that produces would otherwise be
        # read as the operator's - freezing the panel at whatever the
        # first frame happened to need.
        document.fit_now()
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
        controls = view.addAction("Show &layer controls")
        controls.setCheckable(True)
        controls.setChecked(self.area.chrome_visible)
        controls.toggled.connect(
            lambda shown: self.area.set_chrome_visible(visible=shown)
        )

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
