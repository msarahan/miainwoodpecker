"""
Icon buttons, so a device's actions cost one row instead of six.

The dock's device groups were a vertical stack of full-width labelled
buttons and a form row per setting — a two-camera instrument with a scan
unit ran to well over a screen's height, and the panel earned a scroll
area to cope. That is a lot of the window spent on controls, in an
application whose whole job is showing the operator an image.

DigitalMicrograph's answer, and this module's, is that the things done
*often* — start, stop, acquire, record — are a row of small icons, and
the things changed *rarely* are behind a settings dialog
(:mod:`miainwoodpecker.viewer.panels.settings`). An icon with a tooltip
is not less discoverable than a button reading "Save displayed frame"
once the operator has met it twice, and it is a tenth of the space.

Glyphs rather than image files, deliberately. Qt's standard pixmaps
cover play, stop and save and nothing else this needs — there is no
standard "acquire one frame" or "record" — so a mixed set would be
half platform icons and half something else. A single geometric glyph
set is consistent, scales with the font, needs no asset pipeline, and
cannot go missing from a wheel. They are drawn in the button's own text
colour, so they follow the operator's theme like every other label.
"""

from __future__ import annotations

import typing

from qtpy import QtCore, QtWidgets

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: Height and width of an icon button, in pixels. Large enough to hit
#: without aiming, small enough that a row of six is narrower than one
#: full-width button used to be.
BUTTON_SIZE = 30

#: How much larger than the surrounding text the glyphs are drawn.
_GLYPH_SCALE = 1.6

#: The glyphs, named for what they do rather than what they look like.
#: Geometric shapes rather than emoji: the emoji forms render in colour
#: on Windows and would fight the theme.
START = "▶"  # black right-pointing triangle
STOP = "■"  # black square
PREVIEW = "◐"  # circle with left half black
ACQUIRE = "◉"  # fisheye - a shutter
SPECTRUM_IMAGE = "▦"  # square with orthogonal crosshatch fill
SAVE = "⤓"  # downwards arrow to bar
RECORD = "⬤"  # black large circle
# Not the gear (U+2699). Windows renders that from Segoe UI Emoji in
# full colour, and a lilac flower among seven monochrome shapes reads as
# a rendering fault rather than as a button - the text-presentation
# selector U+FE0E does not reliably override it through Qt's font
# fallback. This trigram has no emoji form to fall into.
SETTINGS = "☰"  # trigram for heaven - three bars, a settings menu


def action_button(
    parent: QtWidgets.QWidget,
    glyph: str,
    tooltip: str,
    *,
    on_click: Callable[[], None] | None = None,
) -> QtWidgets.QToolButton:
    """
    Build one icon button.

    Parameters
    ----------
    parent : QtWidgets.QWidget
        The widget owning the button.
    glyph : str
        One of this module's glyph constants.
    tooltip : str
        What the button does, in words. **Required**, because the glyph
        is not self-explanatory on first meeting and a tooltip is the
        only thing standing between an icon toolbar and a guessing game.
    on_click : Callable[[], None] | None
        Connected to ``clicked`` if given.

    Returns
    -------
    QtWidgets.QToolButton
        The button.
    """
    button = QtWidgets.QToolButton(parent)
    button.setText(glyph)
    # The glyphs are drawn as text, so at the surrounding label size they
    # come out as small grey marks rather than as icons. Scaled up they
    # read as buttons.
    font = button.font()
    font.setPointSizeF(font.pointSizeF() * _GLYPH_SCALE)
    button.setFont(font)
    button.setToolTip(tooltip)
    # The accessible name carries the words for a screen reader, which
    # the glyph alone would not.
    button.setAccessibleName(tooltip.split(".", maxsplit=1)[0])
    button.setAutoRaise(True)
    button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
    if on_click is not None:
        button.clicked.connect(lambda *_: on_click())
    return button


def set_action(
    button: QtWidgets.QToolButton,
    glyph: str,
    tooltip: str,
) -> None:
    """
    Change what an icon button does, keeping it an icon.

    The start controls flip between starting and stopping, and used to
    do it by setting the button's *text* — which on a labelled button
    read "Stop scan" and on a 30-pixel icon button reads "...", the
    glyph replaced by an elided word. Caught in a screenshot rather than
    by a test, which is the argument for looking at the thing.

    Parameters
    ----------
    button : QtWidgets.QToolButton
        The button to repurpose.
    glyph : str
        Its new glyph, from this module's constants.
    tooltip : str
        What it now does, for the pointer and the screen reader alike.
    """
    button.setText(glyph)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip.split(".", maxsplit=1)[0])


def toolbar(
    parent: QtWidgets.QWidget,
    buttons: Sequence[QtWidgets.QWidget],
) -> QtWidgets.QWidget:
    """
    Lay out buttons in one left-aligned row.

    Parameters
    ----------
    parent : QtWidgets.QWidget
        The widget owning the row.
    buttons : Sequence[QtWidgets.QWidget]
        The buttons, in order. A ``None`` entry is skipped, so a caller
        can build one list for a device that may or may not have every
        action.

    Returns
    -------
    QtWidgets.QWidget
        The row, for adding to a form.
    """
    row = QtWidgets.QWidget(parent)
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    for button in buttons:
        if button is not None:
            layout.addWidget(button)
    layout.addStretch(1)
    layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
    return row
