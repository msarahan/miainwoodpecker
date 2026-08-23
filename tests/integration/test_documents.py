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

from miainwoodpecker.storage.calibration import (
    AxisCalibration,
    AxisKind,
    FrameCalibration,
)
from miainwoodpecker.viewer import axes, documents

# A square scan, an extremely wide EEL spectrum readout, and a tall
# narrow one: the shapes that make a naive fit stretch something.
_SQUARE = (256, 256)
_WIDE = (64, 1024)
_TALL = (1024, 64)
#: One of each awkward shape, for the tests that open several at once.
_ASSORTED = (_SQUARE, _WIDE, _TALL)

#: Tolerance for "one screen pixel per data pixel". Not 1e-9: the scene
#: transform these are measured through is float32, so a calibration
#: like 15 nm / 256 px comes back a few parts in 10^8 off the number it
#: went in as. That is rounding, not a panel drawing at the wrong scale.
_PIXEL_EXACT = 1e-6

#: How exactly a picture must cover its panel. Not zero: a canvas is an
#: integer number of pixels and the extent it holds rarely divides into
#: it evenly.
_FILL_TOLERANCE = 0.01
#: Slack on a canvas dimension, in pixels, for the same rounding.
_SIZE_TOLERANCE = 4


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


def _canvas_pixels_per_world_unit(
    document: documents.Document,
) -> tuple[float, float]:
    """
    Measure how many screen pixels one world unit covers, per axis.

    Read off the live scene transform rather than inferred from
    ``camera.zoom``: a single zoom scalar cannot express a stretch, so
    computing the drawn size from it would assume the very property
    these tests exist to check. Mapping unit steps along each world axis
    through the actual canvas transform can catch an anisotropic one.

    Reaches into ``_qt_viewer`` because the transform is not otherwise
    exposed. That is acceptable in a test in a way it would not be in
    the application: this is measuring what the user sees.

    Parameters
    ----------
    document : documents.Document
        The document to measure.

    Returns
    -------
    tuple[float, float]
        Canvas pixels per world unit along the slow and fast axes.
    """
    canvas = document.viewer.window._qt_viewer.canvas  # noqa: SLF001
    transform = canvas.view.scene.node_transform(canvas.view)
    origin, along_y, along_x = transform.map(
        [[0, 0, 0], [1, 0, 0], [0, 1, 0]]
    )[:, :2]
    return (
        float(np.linalg.norm(along_y - origin)),
        float(np.linalg.norm(along_x - origin)),
    )


def _drawn_aspect(document: documents.Document) -> float:
    """
    Return the width-to-height ratio of a document's image as drawn.

    Built from the data's own size, the layer's calibration scale, and
    the measured per-axis canvas transform, so it reflects what is on
    screen rather than restating the array's shape.

    Parameters
    ----------
    document : documents.Document
        The document to measure.

    Returns
    -------
    float
        Drawn width divided by drawn height, in canvas pixels.
    """
    layer = document.viewer.layers[0]
    height, width = layer.data.shape[-2:]
    scale_y, scale_x = float(layer.scale[-2]), float(layer.scale[-1])
    pixels_y, pixels_x = _canvas_pixels_per_world_unit(document)
    return (width * scale_x * pixels_x) / (height * scale_y * pixels_y)


def test_each_dataset_gets_its_own_window(window):
    """Two datasets are two documents, not two layers on one canvas."""
    window.board.add_image(np.zeros(_SQUARE), name="Scan (HAADF)")
    window.board.add_image(np.zeros(_WIDE), name="Camera")
    _settle()

    assert [d.name for d in window.area.documents()] == ["Scan (HAADF)", "Camera"]
    for document in window.area.documents():
        assert len(document.viewer.layers) == 1


def test_new_documents_go_somewhere_visible(window):
    """
    A dataset never arrives buried under one already on screen.

    Not "never overlaps": windows are sized to their pictures, so three
    awkward shapes need not fit side by side in one area, and going off
    the edge to avoid overlapping would be worse. What is guaranteed is
    that each lands at its own corner and inside the area, so anything
    covered still shows an edge to click.

    An earlier version tiled each window *before* showing it, which
    ``tileSubWindows`` ignores; every panel after the first landed
    exactly on its neighbour and looked like a window that had lost its
    title bar.
    """
    for index, shape in enumerate(_ASSORTED):
        window.board.add_image(np.zeros(shape), name=f"panel {index}")
        _settle()

    corners = {(d.window.x(), d.window.y()) for d in window.area.documents()}
    assert len(corners) == len(_ASSORTED)
    for document in window.area.documents():
        assert not _outside(window.area, document), document.name


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


def test_one_world_unit_is_the_same_size_on_both_axes(window):
    """
    The *view* transform is isotropic, which is what "never stretches" means.

    Every other aspect assertion here rests on this one: if a world unit
    covered more screen pixels across than down, the picture would be
    stretched no matter what scale or extent it was given. Measured from
    the scene transform at several panel shapes, since it is the panel
    shape that would provoke a non-uniform fit.

    This is about napari's camera in the *viewport* sense. An anisotropic
    *detector* is a different thing entirely and is handled by giving the
    layer per-axis scale — see the calibrated tests below.
    """
    window.board.add_image(np.zeros(_WIDE), name="panel")
    _settle()
    document = window.area.document("panel")

    for size in ((900, 200), (200, 700), (500, 500), (1000, 120)):
        document.window.resize(*size)
        _settle()
        down, across = _canvas_pixels_per_world_unit(document)
        assert down == pytest.approx(across, rel=1e-9), f"anisotropic at {size}"


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


def _panel_fill(document: documents.Document) -> tuple[float, float]:
    """
    Return how much of the panel the drawn image covers, per axis.

    Measured against the canvas widget rather than ``canvas.size``, which
    reports ``(height, width)`` — reading it as width-then-height made a
    correctly framed wide picture look 30% oversized and sent me looking
    for a bug in the framing instead of in the measurement.

    Parameters
    ----------
    document : documents.Document
        The document to measure.

    Returns
    -------
    tuple[float, float]
        Fraction of the panel covered down and across.
    """
    canvas = document.viewer.window._qt_viewer.canvas.native  # noqa: SLF001
    layer = document.viewer.layers[0]
    height, width = layer.data.shape[-2:]
    scale_y, scale_x = float(layer.scale[-2]), float(layer.scale[-1])
    down, across = _canvas_pixels_per_world_unit(document)
    return (
        height * scale_y * down / canvas.height(),
        width * scale_x * across / canvas.width(),
    )


def _assert_no_blank_space(document: documents.Document) -> None:
    """
    Assert the picture covers its whole panel, with no bars anywhere.

    Both axes, not just the limiting one: a window sized to its data has
    the data's shape, so a shortfall on *either* axis is the letterboxing
    this arrangement exists to remove.

    Parameters
    ----------
    document : documents.Document
        The document to check.
    """
    down, across = _panel_fill(document)
    assert down == pytest.approx(1.0, abs=_FILL_TOLERANCE), f"blank above: {down}"
    assert across == pytest.approx(
        1.0, abs=_FILL_TOLERANCE
    ), f"blank beside: {across}"


@pytest.mark.parametrize(
    "shape", [(64, 64), (256, 256), (2048, 2048), _WIDE, _TALL]
)
def test_a_window_is_sized_to_its_picture(window, shape):
    """
    The frame takes the data's shape, so there are no black bars in it.

    A panel of a fixed shape showing data of another puts the difference
    on screen as blank space — and two panels of different shapes then
    look like the same panel. So the window is sized to the picture and
    the picture fills it, at both extreme aspect ratios and at sizes far
    smaller and far larger than the area.
    """
    window.board.add_image(np.zeros(shape), name="panel")
    _settle()

    _assert_no_blank_space(window.area.document("panel"))


@pytest.mark.parametrize("shape", [(64, 64), (128, 128), (200, 200)])
def test_small_data_opens_magnified(window, shape):
    """
    A 64x64 map is shown at a useful size, not as a postage stamp.

    One screen pixel per position would be a thumbnail nobody can read;
    anything whose longest side is under the minimum is scaled up to it.
    """
    window.board.add_image(np.zeros(shape), name="panel")
    _settle()
    document = window.area.document("panel")
    canvas = document.viewer.window._qt_viewer.canvas.native  # noqa: SLF001

    assert max(canvas.width(), canvas.height()) == pytest.approx(
        documents.MIN_LONGEST_SIDE, abs=_SIZE_TOLERANCE
    )
    _assert_no_blank_space(document)


def test_more_data_is_never_shown_in_a_smaller_window(window):
    """
    Window size means something about the data, monotonically.

    The reason the magnification floor and its target are one number: a
    floor of 256 with a target of 512 would open a 128-pixel scan in a
    *larger* window than a 256-pixel one, and then a glance at two panels
    would tell you the opposite of the truth about which held more.
    """
    widths = []
    for side in (64, 128, 256, 512):
        name = f"panel {side}"
        window.board.add_image(np.zeros((side, side)), name=name)
        _settle()
        canvas = (
            window.area.document(name).viewer.window._qt_viewer.canvas.native  # noqa: SLF001
        )
        widths.append(canvas.width())

    assert widths == sorted(widths), widths


def test_a_panel_that_nearly_fits_a_row_is_shrunk_into_it(window):
    """
    A few pixels short of fitting beside its neighbour is not a reason to hide.

    Two panels came to a handful of pixels more than a dock-narrowed
    workspace; wrapping the second onto a row with no vertical room left
    sent it to the overlap fallback, so two panels that all but fitted
    side by side ended up stacked. Giving up a few per cent of one is a
    far better answer than covering it.
    """
    window.resize(548, 700)
    _settle(40)
    for index in range(2):
        window.board.add_image(np.zeros((256, 256)), name=f"panel {index}")
        _settle()

    first, second = (d.window.geometry() for d in window.area.documents())
    assert first.y() == second.y(), "should have stayed on one row"
    assert first.intersected(second).isEmpty()
    for document in window.area.documents():
        _assert_no_blank_space(document)


def test_tiling_twice_gives_the_same_layout(window):
    """
    Packing is idempotent, so repeated tiling does not erode the panels.

    Each pass may shrink a window to close a near miss; without resetting
    to the content size first, every pass would take another slice off
    and panels would dwindle as datasets came and went.
    """
    for index in range(3):
        window.board.add_image(np.zeros((256, 256)), name=f"panel {index}")
        _settle()

    window.area.arrange()
    _settle()
    once = [d.window.geometry() for d in window.area.documents()]
    window.area.arrange()
    _settle()

    assert [d.window.geometry() for d in window.area.documents()] == once


def test_data_larger_than_the_minimum_opens_at_its_own_size(window):
    """
    Something already big enough is shown pixel for pixel, not magnified.

    The magnification exists to rescue tiny data, not to inflate
    everything — a 1340-channel readout is wide enough to read as it is.

    The area is widened first because the fixture's is not: a panel is
    never given more canvas than the workspace has, so on a narrow area
    this would be measuring the clamp rather than the rule.
    """
    window.resize(1600, 800)
    _settle()
    window.board.add_image(np.zeros((100, 1340)), name="panel")
    _settle()
    document = window.area.document("panel")
    canvas = document.viewer.window._qt_viewer.canvas.native  # noqa: SLF001

    assert canvas.width() == pytest.approx(1340, abs=_SIZE_TOLERANCE)
    assert canvas.height() == pytest.approx(100, abs=_SIZE_TOLERANCE)


def test_data_too_big_for_the_area_is_shrunk_to_fit(window):
    """
    A frame larger than the workspace cannot be sized to itself.

    So it shrinks rather than overhanging — still filling its window
    exactly, just at less than one screen pixel per acquired pixel.
    """
    window.board.add_image(np.zeros((4096, 4096)), name="panel")
    _settle()
    document = window.area.document("panel")
    canvas = document.viewer.window._qt_viewer.canvas.native  # noqa: SLF001

    assert canvas.width() <= window.area.width()
    assert canvas.height() <= window.area.height()
    _assert_no_blank_space(document)


def test_a_calibrated_panel_is_sized_to_its_picture_too(window):
    """Calibration decides what a pixel measures, not how it is framed."""
    pixels = (256, 256)
    window.board.add_image(
        np.zeros(pixels),
        name="panel",
        **axes.layer_axes(FrameCalibration.from_field_size((15.0, 15.0), pixels)),
    )
    _settle()

    _assert_no_blank_space(window.area.document("panel"))


def test_an_anisotropic_frame_is_framed_to_its_physical_shape(window):
    """
    The window takes the shape the specimen has, not the array.

    A 64x256 readout from a detector binned four times across is
    physically square, so its window is square and the picture fills it.
    """
    window.board.add_image(
        np.zeros((64, 256)),
        name="panel",
        **axes.layer_axes(
            FrameCalibration(
                y=AxisCalibration(kind=AxisKind.ANGLE, scale=1.6, units="mrad"),
                x=AxisCalibration(kind=AxisKind.ANGLE, scale=0.4, units="mrad"),
            )
        ),
    )
    _settle()
    document = window.area.document("panel")

    _assert_no_blank_space(document)
    assert _drawn_aspect(document) == pytest.approx(1.0, rel=_PIXEL_EXACT)


def test_panels_are_placed_beside_each_other_not_over(window):
    """
    Tiling means side by side, and never resizing a window to the screen.

    ``tileSubWindows`` divides the whole area between the windows, which
    gives each the *area's* shape and puts the bars straight back. So
    the windows keep the size their data asked for and are packed
    instead.
    """
    # Two, not three: no panel is ever narrower than the magnification
    # floor, so three need a larger area than the fixture's and the test
    # would be measuring the overlap fallback instead of the packing.
    # Two fit side by side with room to spare, which is the case this is
    # about.
    sizes = {}
    for index in range(2):
        name = f"panel {index}"
        window.board.add_image(np.zeros((200, 200)), name=name)
        _settle()
        sizes[name] = window.area.document(name).window.size()

    window.area.arrange()
    _settle()

    # The area genuinely has room for both, so
    # overlapping here would mean the packing failed rather than that it
    # ran out of space.
    boxes = [d.window.geometry() for d in window.area.documents()]
    for first in range(len(boxes)):
        for second in range(first + 1, len(boxes)):
            assert boxes[first].intersected(boxes[second]).isEmpty()
    for document in window.area.documents():
        # Same size as it asked for: packing moved it, nothing resized it.
        assert document.window.size() == sizes[document.name]
        _assert_no_blank_space(document)


def _outside(
    area: documents.DocumentArea,
    document: documents.Document,
) -> bool:
    """
    Report whether any part of a document's window is outside the area.

    Parameters
    ----------
    area : documents.DocumentArea
        The area the window should be inside.
    document : documents.Document
        The document to check.

    Returns
    -------
    bool
        True if any edge is beyond the area.
    """
    box = document.window.geometry()
    return (
        box.left() < 0
        or box.top() < 0
        or box.right() > area.width()
        or box.bottom() > area.height()
    )


def test_no_part_of_a_window_is_ever_outside(window):
    """
    A panel off the edge cannot be reached, so none is ever put there.

    More data than the area can hold, at sizes larger than it, so both
    the packing and the clamp are exercised: whatever else happens, every
    edge stays inside.
    """
    for index, shape in enumerate(
        ((512, 512), (400, 1200), (600, 600), (2048, 2048), (64, 64))
    ):
        window.board.add_image(np.zeros(shape), name=f"panel {index}")
        _settle()

    for document in window.area.documents():
        assert not _outside(window.area, document), document.name


def test_shrinking_the_application_brings_panels_back_inside(window):
    """
    Panels follow the application in, rather than being left off the edge.

    Shrinking the window is the way a panel most easily ends up outside,
    and the part outside is the part that cannot be clicked.
    """
    for index in range(3):
        window.board.add_image(np.zeros((512, 512)), name=f"panel {index}")
        _settle()

    window.resize(600, 450)
    _settle()

    for document in window.area.documents():
        assert not _outside(window.area, document), document.name


def test_windows_that_must_overlap_are_offset(window):
    """
    Covering is allowed; hiding a window exactly underneath is not.

    Once the area has no clear space left, panels overlap rather than
    going off the edge — but each is offset from the last, so what is
    underneath still shows a corner and is visibly there to be raised.
    """
    for index in range(5):
        window.board.add_image(np.zeros((600, 600)), name=f"panel {index}")
        _settle()

    corners = {
        (d.window.x(), d.window.y()) for d in window.area.documents()
    }
    assert len(corners) == len(window.area.documents())


def test_resizing_a_panel_by_hand_refits_without_stretching(window):
    """
    A window the operator reshapes keeps the picture whole and undistorted.

    Blank space is unavoidable once the frame stops matching the
    picture — that is their choice — but the picture must still fit
    inside it and keep its aspect.
    """
    window.board.add_image(np.zeros(_SQUARE), name="panel")
    _settle()
    document = window.area.document("panel")

    for size in ((900, 300), (300, 700)):
        document.window.resize(*size)
        _settle()
        down, across = _panel_fill(document)
        assert down <= 1.0 + _FILL_TOLERANCE
        assert across <= 1.0 + _FILL_TOLERANCE
        assert _drawn_aspect(document) == pytest.approx(1.0, rel=_PIXEL_EXACT)


def test_actual_resolution_is_available_on_request(window):
    """One screen pixel per acquired pixel, when that is the question."""
    window.board.add_image(np.zeros((2048, 2048)), name="panel")
    _settle()
    document = window.area.document("panel")

    document.show_at_actual_resolution()
    _settle()
    down, across = _canvas_pixels_per_world_unit(document)
    assert down == pytest.approx(1.0, rel=_PIXEL_EXACT)
    assert across == pytest.approx(1.0, rel=_PIXEL_EXACT)


def test_a_scale_the_operator_chose_survives_a_new_dataset(window):
    """
    Automatic fitting stops the moment the operator picks a scale.

    Zooming into a feature and then starting a second detector would
    otherwise throw the view away at the moment it became interesting.
    """
    window.board.add_image(np.zeros(_SQUARE), name="panel")
    _settle()
    document = window.area.document("panel")
    assert document.scaled_by_hand is False

    document.viewer.camera.zoom = 4.0
    _settle()
    assert document.scaled_by_hand is True

    window.board.add_image(np.zeros(_WIDE), name="another")
    _settle()
    window.area.arrange()
    _settle()

    assert document.viewer.camera.zoom == pytest.approx(4.0)

def test_each_panel_gets_its_own_units_and_scale_bar(window):
    """
    Three panels, three calibrations, three scale bars — the whole point.

    napari applies units per layer but draws the scale bar per viewer,
    and refuses to render units at all when one viewer's layers disagree.
    So this is the case a single shared canvas cannot serve, and the
    reason the viewing area is a window per dataset rather than tiles on
    one canvas: a HAADF map in nanometres, a Ronchigram in milliradians,
    and an EEL spectrum in electronvolts, all on screen together.
    """
    window.board.add_image(
        np.zeros(_SQUARE),
        name="Scan (HAADF)",
        **axes.layer_axes(FrameCalibration.real_space(0.25)),
    )
    window.board.add_image(
        np.zeros(_SQUARE),
        name="Camera",
        **axes.layer_axes(
            FrameCalibration(
                y=AxisCalibration(kind=AxisKind.ANGLE, scale=0.4, units="mrad"),
                x=AxisCalibration(kind=AxisKind.ANGLE, scale=0.4, units="mrad"),
            )
        ),
    )
    window.board.add_image(
        np.zeros(_WIDE),
        name="Camera (eels)",
        **axes.layer_axes(FrameCalibration.spectrum(0.5, dispersive_axis="x")),
    )
    _settle()

    bars = {
        d.name: (d.viewer.scale_bar.visible, d.viewer.scale_bar.unit)
        for d in window.area.documents()
    }
    assert bars["Scan (HAADF)"] == (True, "nm")
    assert bars["Camera"] == (True, "mrad")
    # Energy against position does not convert, so no bar rather than a
    # length drawn across an electronvolt.
    assert bars["Camera (eels)"][0] is False


def test_a_calibrated_panel_is_drawn_to_its_physical_shape(window):
    """
    Anisotropic sampling changes the drawn shape, and resizing does not.

    Calibration is the data's true geometry, so a frame sampled four
    times more finely across than down *should* be drawn four times
    wider — that is not stretching, it is the picture being right. What
    must not change it is a viewing action, so the calibrated ratio is
    re-measured at several deliberately wrong panel shapes.
    """
    pixels = (100, 100)
    window.board.add_image(
        np.zeros(pixels),
        name="panel",
        **axes.layer_axes(FrameCalibration.from_field_size((10.0, 40.0), pixels)),
    )
    _settle()
    document = window.area.document("panel")

    # Square in pixels, four times wider than tall on screen: the drawn
    # shape follows the specimen, not the detector's pixel count.
    assert pixels[1] / pixels[0] == pytest.approx(1.0)
    for size in ((900, 200), (200, 700), (500, 500)):
        document.window.resize(*size)
        _settle()
        assert _drawn_aspect(document) == pytest.approx(4.0, rel=1e-9)


def test_an_anisotropic_detector_draws_square_when_it_is_square(window):
    """
    A 4:1 readout from a 4x-binned-across detector is drawn 1:1.

    The end-to-end version of the unit test: an EELS-style camera binned
    once along the dispersion and four times across it stores a 64x256
    frame of a physically square region. Drawing it 4:1 — the shape of
    the array — would misreport the measurement, and drawing it 1:1 is
    only possible because per-axis scale is applied while the view
    transform stays isotropic.
    """
    window.board.add_image(
        np.zeros((64, 256)),
        name="EELS camera",
        **axes.layer_axes(
            FrameCalibration(
                y=AxisCalibration(kind=AxisKind.ANGLE, scale=1.6, units="mrad"),
                x=AxisCalibration(kind=AxisKind.ANGLE, scale=0.4, units="mrad"),
            )
        ),
    )
    _settle()
    document = window.area.document("EELS camera")

    assert _drawn_aspect(document) == pytest.approx(1.0, rel=1e-9)
    assert document.viewer.scale_bar.visible is True
    assert document.viewer.scale_bar.unit == "mrad"

    # And a resize does not disturb it, as for any other panel.
    document.window.resize(900, 200)
    _settle()
    assert _drawn_aspect(document) == pytest.approx(1.0, rel=1e-9)


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
