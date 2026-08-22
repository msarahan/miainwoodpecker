"""
Integration tests: one document per dataset, in a real MDI area.

Skipped unless the ``viewer`` extra is installed and a display is
available (see conftest.py — each document is a real napari viewer, so
these need a real GL canvas; run under ``xvfb-run -a`` on Linux).

The aspect-ratio tests here are the ones worth being fussy about.
"Nothing stretches an image" is the requirement this whole feature was
built under, and it is not a property of code that can be read off by
inspection — it is a property of what napari's camera does with a canvas
of a given shape. So these measure the *drawn* extent against the data's
own and demand equality, at panel shapes chosen to be as hostile as the
arithmetic allows.
"""

from collections.abc import Iterator

import numpy as np
import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

from qtpy import QtCore, QtWidgets

from miainwoodpecker.viewer import documents

# A square scan, an extremely wide EEL spectrum readout, and a tall
# narrow one: the shapes that make a naive fit stretch something.
_SQUARE = (256, 256)
_WIDE = (64, 1024)
_TALL = (1024, 64)
#: One of each awkward shape, for the tests that open several at once.
_ASSORTED = (_SQUARE, _WIDE, _TALL)


@pytest.fixture
def window(
    qapp: QtWidgets.QApplication,  # noqa: ARG001 - requested for its side effect
) -> Iterator[documents.DocumentWindow]:
    """
    Open a document window, and close it however the test ends.

    Parameters
    ----------
    qapp : QtWidgets.QApplication
        The Qt application, requested so one exists before any widget.

    Yields
    ------
    documents.DocumentWindow
        The window, shown at a fixed size so panel geometry is
        predictable.
    """
    opened = documents.open_window("test documents")
    opened.resize(1200, 800)
    opened.show()
    _settle()
    yield opened
    opened.close()


@pytest.fixture
def qapp() -> QtWidgets.QApplication:
    """
    Return the Qt application, creating it if this is the first test.

    Returns
    -------
    QtWidgets.QApplication
        The running application.
    """
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _settle(rounds: int = 20) -> None:
    """
    Let Qt deliver the events a layout change queues.

    Parameters
    ----------
    rounds : int
        How many times to drain the event queue.
    """
    app = QtWidgets.QApplication.instance()
    for _ in range(rounds):
        app.processEvents()


def _drawn_aspect(document: documents.Document) -> float:
    """
    Return the width-to-height ratio of a document's image as drawn.

    Parameters
    ----------
    document : documents.Document
        The document to measure.

    Returns
    -------
    float
        Drawn width divided by drawn height, in canvas pixels.
    """
    height, width = document.viewer.layers[0].data.shape[-2:]
    zoom = document.viewer.camera.zoom
    return (width * zoom) / (height * zoom)


def test_each_dataset_gets_its_own_window(window):
    """Two datasets are two documents, not two layers on one canvas."""
    window.board.add_image(np.zeros(_SQUARE), name="Scan (HAADF)")
    window.board.add_image(np.zeros(_WIDE), name="Camera")
    _settle()

    assert [d.name for d in window.area.documents()] == ["Scan (HAADF)", "Camera"]
    for document in window.area.documents():
        assert len(document.viewer.layers) == 1


def test_new_documents_do_not_cover_each_other(window):
    """
    Tiling puts every panel somewhere visible.

    The point of tiling on open is that a dataset never arrives hidden
    underneath one already on screen, so this asserts the geometries do
    not intersect rather than merely that three windows exist. An
    earlier version tiled each window *before* showing it, which
    tileSubWindows ignores; every panel after the first landed on top of
    its neighbour and looked like a window that had lost its title bar.
    """
    for index, shape in enumerate(_ASSORTED):
        window.board.add_image(np.zeros(shape), name=f"panel {index}")
    _settle()

    boxes = [d.window.geometry() for d in window.area.documents()]
    assert len(boxes) == len(_ASSORTED)
    for first in range(len(boxes)):
        for second in range(first + 1, len(boxes)):
            overlap = boxes[first].intersected(boxes[second])
            assert overlap.isEmpty(), f"panels {first} and {second} overlap"


@pytest.mark.parametrize("shape", [_SQUARE, _WIDE, _TALL])
def test_resizing_a_panel_never_stretches_it(window, shape):
    """
    A panel's image keeps its own aspect ratio at any window shape.

    Dragging a border is the operation the whole feature was asked for,
    and the requirement attached to it was that no viewing change may
    stretch an image. The panel is forced through several deliberately
    wrong shapes — including ones far wider and far taller than the data
    — and the drawn ratio has to equal the data's every time.
    """
    window.board.add_image(np.zeros(shape), name="panel")
    _settle()
    document = window.area.document("panel")
    height, width = shape
    expected = width / height

    for size in ((900, 200), (200, 700), (500, 500), (1000, 120)):
        document.window.resize(*size)
        _settle()
        assert _drawn_aspect(document) == pytest.approx(expected, rel=1e-9)


def test_tiling_many_panels_never_stretches_any(window):
    """Aspect survives the automatic tiling too, not just manual resizes."""
    shapes = {"square": _SQUARE, "wide": _WIDE, "tall": _TALL}
    for name, shape in shapes.items():
        window.board.add_image(np.zeros(shape), name=name)
    _settle()
    window.area.arrange()
    _settle()

    for name, (height, width) in shapes.items():
        document = window.area.document(name)
        assert _drawn_aspect(document) == pytest.approx(width / height, rel=1e-9)


def test_tiling_survives_its_own_layout_changes(window):
    """
    Opening documents programmatically does not count as arranging them.

    The area stops tiling once the operator places a window by hand, and
    it learns that from Qt move and resize events — which its *own*
    tiling also produces. Getting this wrong disabled tiling on the
    second dataset, before anyone had touched anything.
    """
    assert window.area.auto_arrange is True
    for index in range(3):
        window.board.add_image(np.zeros(_SQUARE), name=f"panel {index}")
        _settle()
        assert window.area.auto_arrange is True, f"lost after {index + 1} panels"


def test_moving_a_window_hands_the_layout_over(window):
    """
    Placing a window by hand stops the area rearranging it afterwards.

    And :meth:`DocumentArea.arrange` is the way back, so an operator who
    wants tidy panels again is one menu item from them.
    """
    window.board.add_image(np.zeros(_SQUARE), name="one")
    window.board.add_image(np.zeros(_SQUARE), name="two")
    _settle()

    window.area.note_user_arrangement()
    assert window.area.auto_arrange is False

    moved = window.area.document("one").window
    moved.move(QtCore.QPoint(37, 41))
    _settle()
    window.board.add_image(np.zeros(_SQUARE), name="three")
    _settle()
    assert moved.geometry().topLeft() == QtCore.QPoint(37, 41)

    window.area.arrange()
    _settle()
    assert window.area.auto_arrange is True


def test_an_attached_annotation_shares_its_image_window(window):
    """
    A shape drawn in an image's pixels goes in that image's panel.

    py4DSTEM's fitted-disk ellipse is in the diffraction pattern's own
    coordinates, so a window of its own would show a red circle marking
    nothing at all.
    """
    window.board.add_image(np.zeros(_SQUARE), name="py4DSTEM disk fit (Camera)")
    window.board.add_shapes(
        [[[10, 10], [10, 40], [40, 40], [40, 10]]],
        shape_type="ellipse",
        name="py4DSTEM disk fit",
        metadata={documents.ATTACHED_TO: "py4DSTEM disk fit (Camera)"},
    )
    _settle()

    assert [d.name for d in window.area.documents()] == ["py4DSTEM disk fit (Camera)"]
    shared = window.area.document("py4DSTEM disk fit (Camera)")
    assert [layer.name for layer in shared.viewer.layers] == [
        "py4DSTEM disk fit (Camera)",
        "py4DSTEM disk fit",
    ]


def test_the_board_answers_like_a_viewer(window):
    """
    ``layers`` behaves as the widget expects across several documents.

    The widget was written against ``napari.Viewer`` and is given this
    board instead, so membership, lookup, deletion and length have to
    mean the same things spread over many windows as they did on one.
    """
    opened = {"one": _SQUARE, "two": _WIDE}
    for name, shape in opened.items():
        window.board.add_image(np.zeros(shape), name=name)
    _settle()

    assert "one" in window.board.layers
    assert "nothing" not in window.board.layers
    assert window.board.layers["one"].data.shape == _SQUARE
    assert len(window.board.layers) == len(opened)
    assert {layer.name for layer in window.board.layers} == set(opened)

    with pytest.raises(KeyError):
        window.board.layers["nothing"]


def test_deleting_a_layer_closes_its_window(window):
    """
    Unchecking a detector takes its panel away, not just its picture.

    A window left behind holding the last frame a stopped detector
    produced reads as a live feed that has quietly stopped, which is the
    same failure the layer cleanup already existed to prevent.
    """
    window.board.add_image(np.zeros(_SQUARE), name="Scan (HAADF)")
    window.board.add_image(np.zeros(_SQUARE), name="Scan (MAADF)")
    _settle()

    del window.board.layers["Scan (MAADF)"]
    _settle()

    assert [d.name for d in window.area.documents()] == ["Scan (HAADF)"]
    assert "Scan (MAADF)" not in window.board.layers


def test_a_closed_panel_stays_closed(window):
    """
    Closing a live panel is not undone by the next frame.

    A running detector keeps producing frames after its window is shut.
    Reopening on the next one would make the close button useless — it
    would blink and come straight back — so further frames for a closed
    panel are dropped.
    """
    window.board.add_image(np.zeros(_SQUARE), name="Camera")
    _settle()
    window.area.document("Camera").window.close()
    _settle()
    assert window.area.document("Camera") is None

    # The live loop keeps calling this, once per frame.
    for _ in range(3):
        window.board.add_image(np.zeros(_SQUARE), name="Camera")
    _settle()
    assert window.area.document("Camera") is None
    assert "Camera" not in window.board.layers


def test_restarting_a_source_brings_its_panel_back_in_front(window):
    """
    Starting a detector again reopens its panel and raises it.

    Being covered is a legitimate arrangement while a source runs;
    asking for the source again is what says you want to see it. The
    request is made before the document exists — a detector's window is
    not opened until its first frame arrives — so it has to be held
    until then.
    """
    window.board.add_image(np.zeros(_SQUARE), name="Camera")
    _settle()
    window.area.document("Camera").window.close()
    _settle()

    window.board.raise_document("Camera")
    window.board.add_image(np.zeros(_SQUARE), name="Camera")
    _settle()

    assert window.area.document("Camera") is not None
    active = window.area.activeSubWindow()
    assert active is not None
    assert active.windowTitle() == "Camera"


def test_raising_a_covered_panel_activates_it(window):
    """A panel that is merely buried comes to the front on request."""
    window.board.add_image(np.zeros(_SQUARE), name="first")
    window.board.add_image(np.zeros(_SQUARE), name="second")
    _settle()

    window.board.raise_document("first")
    _settle()
    active = window.area.activeSubWindow()
    assert active is not None
    assert active.windowTitle() == "first"


def test_a_hand_zoomed_panel_is_not_refitted(window):
    """
    Resizing a panel leaves a view the operator chose alone.

    Refitting on resize is what makes dragging a border grow and shrink
    the picture, but doing it to a panel someone has zoomed into would
    throw away the view they were looking at.
    """
    window.board.add_image(np.zeros(_SQUARE), name="panel")
    _settle()
    document = window.area.document("panel")
    assert document.zoomed_by_hand is False

    document.viewer.camera.zoom = 4.0
    _settle()
    assert document.zoomed_by_hand is True

    document.window.resize(400, 300)
    _settle()
    assert document.viewer.camera.zoom == pytest.approx(4.0)


def test_a_document_reuses_its_window_when_its_data_is_replaced(window):
    """
    Replacing a layer keeps the panel where the operator put it.

    Every frame of a live feed replaces the previous one; a panel that
    jumped back to a tiled position each time would be unusable.
    """
    window.board.add_image(np.zeros(_SQUARE), name="panel")
    _settle()
    document = window.area.document("panel")
    document.window.move(QtCore.QPoint(60, 70))
    _settle()

    window.board.add_image(np.ones(_SQUARE), name="panel")
    _settle()

    assert window.area.document("panel") is document
    assert document.window.geometry().topLeft() == QtCore.QPoint(60, 70)
