"""
Integration tests: a spectrometer's readout, displayed as a spectrum.

Every other panel in this application is an array on a napari canvas.
One axis of counts is not, and before the plot existed it was not
displayable at all: the display path resolves a frame's calibration as
two axes, and the first rank-1 frame that reached it raised. So these
run the real thing — a real MDI area, a real pyqtgraph plot, and the
preview instrument's EEL spectrometer in its projected readout — and
ask what an operator would: is the curve the detector's counts, and is
the axis under it the detector's electronvolts.

The second half is the same question of a *pass*. A spectrum image is
watched as a virtual-detector image, which is one number per beam
position: a spectrometer parked off the edge of the loss draws a map
indistinguishable from a good acquisition. What makes that visible is
the spectrum at the position the probe is on, which is drawn from the
same teed write the map is formed from.

Skipped without a display (see conftest.py).
"""

import pathlib
import time
from collections.abc import Iterator

import numpy as np
import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")
pytest.importorskip("pyqtgraph", reason="requires the 'viewer' extra")

import napari
from qtpy import QtWidgets

from miainwoodpecker.devices.interface import IMAGE_READOUT, PROJECTED_READOUT
from miainwoodpecker.storage.calibration import AxisCalibration, AxisKind
from miainwoodpecker.storage.session import Session
from miainwoodpecker.viewer import documents, plots
from miainwoodpecker.viewer.live import LiveInstrumentWidget
from miainwoodpecker.viewer.preview import _EELS_TARGET, build_preview_devices

#: Beam positions per side for the pass tests. Small: these are about
#: what is drawn while it runs, not about the file it produces, which
#: tests/integration/test_spectrum_image.py already covers.
_A_SMALL_GRID = 4

#: Two cameras, because the preview decides a camera's kind from the
#: target name it is served under and a single one takes the neutral one.
_TWO_CAMERAS = 2

_DEADLINE_S = 30.0


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


@pytest.fixture
def spectrometer(
    qapp: QtWidgets.QApplication,  # noqa: ARG001 - requested for its side effect
    tmp_path: pathlib.Path,
) -> Iterator[tuple[documents.DocumentWindow, LiveInstrumentWidget, object]]:
    """
    Open the window on a preview instrument whose spectrometer projects.

    The window is a real :class:`documents.DocumentWindow` rather than a
    plain :class:`napari.Viewer`, which is the point: a single shared
    canvas has nowhere to put a plot, and the widget says so by doing
    nothing there.

    Parameters
    ----------
    qapp : QtWidgets.QApplication
        The Qt application, requested so one exists before any widget.
    tmp_path : pathlib.Path
        Where the session is written.

    Yields
    ------
    tuple[documents.DocumentWindow, LiveInstrumentWidget, object]
        The window, the instrument panel inside it, and the devices it
        is driving — the last so a test can ask the detector itself what
        its dispersion is, rather than restating it.
    """
    devices = build_preview_devices(
        scan=True,
        camera=True,
        camera_count=_TWO_CAMERAS,
    )
    window = documents.open_window("test spectrum plot")
    window.resize(1200, 800)
    widget = LiveInstrumentWidget(
        window.board,
        devices.scanner,
        cameras=devices.cameras,
        instrument=devices.instrument,
    )
    window.set_panel(widget)
    window.show()
    widget.set_session(Session(tmp_path / "shift"))
    widget._positions_spin.setValue(_A_SMALL_GRID)  # noqa: SLF001
    widget._sync_target_combo.setCurrentText(_EELS_TARGET)  # noqa: SLF001
    widget.set_camera_readout(_EELS_TARGET, PROJECTED_READOUT)
    _settle()
    yield window, widget, devices
    widget.shutdown()
    window.close()


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


def _plot(window: documents.DocumentWindow, name: str) -> plots.SpectrumPlot:
    """
    Return the plot in a named panel, failing the test if there is none.

    Parameters
    ----------
    window : documents.DocumentWindow
        The window holding the documents.
    name : str
        The panel's name.

    Returns
    -------
    plots.SpectrumPlot
        The plot inside it.

    Raises
    ------
    AssertionError
        If no such panel is open, or it is not a plot.
    """
    document = window.area.document(name)
    if document is None:
        open_now = [each.name for each in window.area.documents()]
        msg = f"no panel named {name!r}; open panels are {open_now}"
        raise AssertionError(msg)
    if not isinstance(document, documents.PanelDocument):
        msg = f"{name!r} is a {type(document).__name__}, not a plot panel"
        raise AssertionError(msg)  # noqa: TRY004 - a test failure, not a type error
    return document.widget


def _draw_a_live_frame(widget: LiveInstrumentWidget) -> None:
    """
    Run the camera until a *new* frame has been drawn, then stop it.

    Does what the display timer does rather than waiting on it: a test
    that slept would be timing the live loop instead of testing it.

    "New" matters, and cost two of these tests when it was left out: a
    second call sees the frame the first one drew still recorded against
    the panel and returns without the camera having produced anything,
    so a test meant to display a spectrum after changing the readout
    asserted against the picture from before it.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The panel driving the camera.
    """
    widget.start_camera(_EELS_TARGET)
    try:
        _wait_for_a_frame(widget)
    finally:
        widget.stop_camera(_EELS_TARGET)


def _wait_for_a_frame(widget: LiveInstrumentWidget) -> None:
    """
    Drive the display until a new frame has been through it.

    Separate from :func:`_draw_a_live_frame` because starting a camera
    is not neutral: it is a request to *see* that source, and it brings
    a panel the operator closed back. A test about what a closed panel
    does while the detector goes on running therefore has to leave the
    camera alone and only turn the display over.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The panel whose display to drive.

    Raises
    ------
    AssertionError
        If no frame arrives before the deadline.
    """
    panel = f"Camera ({_EELS_TARGET})"
    before = widget._displayed.get(panel)  # noqa: SLF001
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        widget.refresh_display()
        _settle(rounds=2)
        drawn = widget._displayed.get(panel)  # noqa: SLF001
        if drawn is not None and drawn is not before:
            return
        time.sleep(0.005)
    msg = "the camera produced no frame"
    raise AssertionError(msg)


def _finish_pass(widget: LiveInstrumentWidget) -> None:
    """
    Drive the display poll until the synchronised pass has finished.

    Parameters
    ----------
    widget : LiveInstrumentWidget
        The widget whose pass to wait for.

    Raises
    ------
    AssertionError
        If the pass does not finish before the deadline.
    """
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        widget.refresh_display()
        _settle(rounds=2)
        if widget._pass_job is None:  # noqa: SLF001
            return
        time.sleep(0.005)
    msg = "the spectrum image did not finish"
    raise AssertionError(msg)


class TestALiveSpectrum:
    """
    A projecting detector, running, with its own window on the screen.

    This is the case that used to end the live view: the readout control
    let an operator put a spectrometer into ``projected`` while it was
    stopped, and starting it again handed the display a 1D array it
    could not so much as calibrate.
    """

    def test_a_projecting_camera_gets_a_plot_and_not_a_picture(
        self,
        spectrometer,
    ):
        """
        The rank decides the display, and one axis of counts is a curve.

        Named as the camera's own panel, not as a second window beside
        it: a spectrometer switched between imaging and projecting is
        one dataset that changed shape.
        """
        window, widget, _ = spectrometer
        _draw_a_live_frame(widget)

        plot = _plot(window, f"Camera ({_EELS_TARGET})")
        energies, counts = plot.spectrum()
        assert counts.size > 0
        assert counts.size == energies.size

    def test_the_curve_is_the_counts_the_detector_produced(
        self,
        spectrometer,
    ):
        """
        What is on the screen is the frame, not a resampling of it.

        Read back off the curve rather than from a copy kept beside it,
        so this measures the drawing rather than the bookkeeping.
        """
        window, widget, _ = spectrometer
        _draw_a_live_frame(widget)

        frame = widget._displayed[f"Camera ({_EELS_TARGET})"]  # noqa: SLF001
        _, counts = _plot(window, f"Camera ({_EELS_TARGET})").spectrum()
        assert np.array_equal(counts, frame.data)

    def test_the_axis_is_the_detectors_own_electronvolts(
        self,
        spectrometer,
    ):
        """
        The dispersion is the detector's, read off the frame it came in.

        The whole reason a spectrum needs a plot rather than an image
        layer: an EELS panel that could not say where zero loss is would
        be a picture of a spectrum rather than a display of one.
        """
        window, widget, devices = spectrometer
        _draw_a_live_frame(widget)

        camera = devices.cameras[_EELS_TARGET]
        energy = camera.frame_calibration().axis("x")
        assert energy.kind is AxisKind.ENERGY

        plot = _plot(window, f"Camera ({_EELS_TARGET})")
        energies, _ = plot.spectrum()
        assert plot.axis_label() == "energy"
        assert energies[0] == pytest.approx(energy.offset)
        assert energies[1] - energies[0] == pytest.approx(energy.scale)

    def test_the_panel_is_replaced_rather_than_doubled(
        self,
        spectrometer,
    ):
        """
        One dataset keeps one window across a change of readout.

        A spectrometer put back into imaging produces pictures under the
        name its curves were drawn under, and two windows with one title
        is the ambiguity the naming exists to prevent.
        """
        window, widget, _ = spectrometer
        _draw_a_live_frame(widget)
        assert isinstance(
            window.area.document(f"Camera ({_EELS_TARGET})"),
            documents.PanelDocument,
        )

        widget.set_camera_readout(_EELS_TARGET, IMAGE_READOUT)
        _draw_a_live_frame(widget)
        _settle()

        names = [each.name for each in window.area.documents()]
        assert names.count(f"Camera ({_EELS_TARGET})") == 1
        assert isinstance(
            window.area.document(f"Camera ({_EELS_TARGET})"),
            documents.Document,
        )


class TestWatchingASpectrumImageFill:
    """
    The 3D dataset, while it is still being collected.

    A spectrum image is a 1D spectrum at every beam position, and until
    now the only thing on screen while one built was the map — the sum
    at each position. That answers "is the probe finding signal" and
    cannot answer "is it the signal I set the spectrometer to", which is
    the question a curve answers in the first second rather than at
    analysis time.
    """

    def test_the_spectrum_at_the_probe_is_drawn_beside_the_map(
        self,
        spectrometer,
    ):
        """Both panels open while the pass runs, from the same writes."""
        window, widget, _ = spectrometer
        widget.acquire_spectrum_image()
        _finish_pass(widget)

        _, counts = _plot(window, f"Acquiring ({_EELS_TARGET}): spectrum").spectrum()
        assert counts.size > 0
        assert window.area.document(f"Acquiring ({_EELS_TARGET})") is not None

    def test_the_curve_is_the_position_the_probe_last_wrote(
        self,
        spectrometer,
    ):
        """
        Not a running average and not the first position: the last one.

        The preview keeps a copy taken at write time, so what is drawn
        is one beam position's readout rather than whatever a device's
        scratch buffer holds by the time the display gets to it.
        """
        window, widget, _ = spectrometer
        widget.acquire_spectrum_image()
        _finish_pass(widget)

        preview = widget._pass_preview[_EELS_TARGET]  # noqa: SLF001
        position, kept = preview.latest_spectrum
        _, counts = _plot(window, f"Acquiring ({_EELS_TARGET}): spectrum").spectrum()
        assert np.array_equal(counts, kept)
        assert position == (_A_SMALL_GRID - 1, _A_SMALL_GRID - 1)

    def test_the_title_says_which_beam_position_it_is(
        self,
        spectrometer,
    ):
        """
        A curve with no position is a curve from somewhere in the map.

        The map says where the probe has been; the title is what ties
        the spectrum to a place in it.
        """
        window, widget, _ = spectrometer
        widget.acquire_spectrum_image()
        _finish_pass(widget)

        title = _plot(window, f"Acquiring ({_EELS_TARGET}): spectrum").title()
        assert f"{_A_SMALL_GRID}x{_A_SMALL_GRID}" in title
        assert "position" in title

    def test_the_pass_axis_is_energy_not_channels(
        self,
        spectrometer,
    ):
        """
        The dispersion is read once, from the frame that sized the file.

        The destinations the probe writes into are bare arrays and the
        ``Spectrum`` carrying the energy axis is not built until the
        pass finishes — so a plot taking its axis from what arrives
        would be in channels for the whole acquisition.
        """
        window, widget, devices = spectrometer
        widget.acquire_spectrum_image()
        _finish_pass(widget)

        plot = _plot(window, f"Acquiring ({_EELS_TARGET}): spectrum")
        energies, _ = plot.spectrum()
        camera = devices.cameras[_EELS_TARGET]
        energy = camera.frame_calibration().axis("x")

        assert plot.axis_label() == "energy"
        assert energies[0] == pytest.approx(energy.offset)

    def test_a_4d_stack_gets_no_spectrum_panel(
        self,
        spectrometer,
    ):
        """
        There is no spectrum in a diffraction cube, so none is drawn.

        Decided by the rank of what the probe writes rather than by the
        readout the operator set: it is the same fact, and one of the
        two is a guess about the detector.
        """
        window, widget, _ = spectrometer
        widget.set_camera_readout(_EELS_TARGET, IMAGE_READOUT)
        widget.acquire_spectrum_image()
        _finish_pass(widget)

        assert window.area.document(f"Acquiring ({_EELS_TARGET})") is not None
        assert window.area.document(f"Acquiring ({_EELS_TARGET}): spectrum") is None


class TestAPlotIsADocumentLikeAnyOther:
    """
    Tiling, closing and raising a window should not care what is in it.

    The whole argument for putting the plot in the document area rather
    than in a dock of its own: an operator arranges one set of windows,
    and a spectrum panel that had to be managed separately would be a
    second kind of thing to keep track of.
    """

    def test_closing_the_panel_keeps_it_closed(self, spectrometer):
        """
        A running detector must not undo the close button in 16 ms.

        Exactly the rule an image panel follows, and it has to be the
        same one: the frames go on arriving either way.
        """
        window, widget, _ = spectrometer
        name = f"Camera ({_EELS_TARGET})"
        widget.start_camera(_EELS_TARGET)
        try:
            _wait_for_a_frame(widget)
            window.area.document(name).window.close()
            _settle()

            # Still running, and still producing spectra into a panel
            # that is not there.
            _wait_for_a_frame(widget)

            assert window.area.document(name) is None
        finally:
            widget.stop_camera(_EELS_TARGET)

    def test_starting_the_camera_again_asks_the_panel_back(self, spectrometer):
        """Starting a source again is a request to see it."""
        window, widget, _ = spectrometer
        _draw_a_live_frame(widget)
        name = f"Camera ({_EELS_TARGET})"
        window.area.document(name).window.close()
        _settle()

        _draw_a_live_frame(widget)

        assert window.area.document(name) is not None

    def test_the_view_menu_does_not_break_on_a_plot(self, spectrometer):
        """
        "Actual resolution" means nothing here, and must not raise.

        The menu applies to whichever panel is in front, and a plot can
        be in front. Answering by doing nothing is what keeps the menu
        from changing as panels come and go.
        """
        window, widget, _ = spectrometer
        _draw_a_live_frame(widget)
        window.area.raise_document(f"Camera ({_EELS_TARGET})")
        _settle()

        window._on_active("show_at_actual_resolution")  # noqa: SLF001
        window._on_active("fit_to_panel")  # noqa: SLF001

        _, counts = _plot(window, f"Camera ({_EELS_TARGET})").spectrum()
        assert counts.size > 0


class TestASingleCanvasHasNoPlot:
    """
    A plain napari viewer is still a supported way to run this window.

    It has nowhere to put a curve, and the honest answer is to draw
    nothing rather than to push the spectrum into an image layer one
    pixel high — which says where the counts are bright and never how
    many there are. The property to pin is that it does not *raise*,
    since that is what it did before the plot existed.
    """

    def test_a_spectrum_is_skipped_rather_than_raising(self, tmp_path):
        """The display path survives a rank it cannot show."""
        devices = build_preview_devices(
            scan=True,
            camera=True,
            camera_count=_TWO_CAMERAS,
        )
        viewer = napari.Viewer(show=False)
        widget = LiveInstrumentWidget(
            viewer,
            devices.scanner,
            cameras=devices.cameras,
            instrument=devices.instrument,
        )
        try:
            widget.set_session(Session(tmp_path / "shift"))
            widget.set_camera_readout(_EELS_TARGET, PROJECTED_READOUT)
            _draw_a_live_frame(widget)

            assert f"Camera ({_EELS_TARGET})" not in viewer.layers
        finally:
            widget.shutdown()
            viewer.close()


class TestTheUnitOnTheAxis:
    """
    The detector's unit, and not a fourth one the plot invented.

    Built directly rather than through an instrument: this is about what
    pyqtgraph does with a unit string, and the shortest way to ask is to
    hand it every unit this project writes for an energy axis.
    """

    @pytest.mark.parametrize("units", ["eV", "meV", "keV"])
    def test_a_prefixed_unit_is_not_prefixed_again(
        self,
        qapp: QtWidgets.QApplication,  # noqa: ARG002 - for its side effect
        units: str,
    ):
        """
        "kmeV" is not a unit, and an axis divided by 1000 is not the data.

        pyqtgraph reads a unit as a *base* SI quantity and prefixes it to
        keep tick numbers small, which is right for volts and wrong for
        two of the three spellings this project accepts. Caught on a real
        monochromated EELS spectrum calibrated in meV, whose axis came
        back labelled kilo-milli-electronvolts and scaled to match.
        """
        plot = plots.SpectrumPlot()
        try:
            plot.show_spectrum(
                np.zeros(1024, dtype=np.float32),
                AxisCalibration(AxisKind.ENERGY, 1.0, -300.0, units),
            )

            axis = plot._plot.getPlotItem().getAxis("bottom")  # noqa: SLF001
            assert axis.labelUnits == units
            assert axis.labelUnitPrefix == ""
        finally:
            plot.close()

    def test_an_uncalibrated_axis_is_labelled_in_channels(
        self,
        qapp: QtWidgets.QApplication,  # noqa: ARG002 - for its side effect
    ):
        """
        Bare channels, with no unit for pyqtgraph to prefix.

        "channel" rather than the calibration model's "pixel index",
        because that is the word for a spectrum's bins in every
        vocabulary this project reads or writes — and passing it as a
        *unit* would have produced a 4096-channel axis in "kchannel".
        """
        plot = plots.SpectrumPlot()
        try:
            plot.show_spectrum(np.zeros(4096, dtype=np.float32), AxisCalibration())

            assert plot.axis_label() == plots.CHANNEL_LABEL
            axis = plot._plot.getPlotItem().getAxis("bottom")  # noqa: SLF001
            assert axis.labelUnits == ""
        finally:
            plot.close()

    def test_a_spectrum_image_is_refused_rather_than_flattened(
        self,
        qapp: QtWidgets.QApplication,  # noqa: ARG002 - for its side effect
    ):
        """
        4096 positions laid end to end is a curve of nothing.

        Which beam position to draw is a question about the acquisition,
        so the plot says it cannot answer it instead of answering wrongly.
        """
        plot = plots.SpectrumPlot()
        try:
            with pytest.raises(ValueError, match="rank-1"):
                plot.show_spectrum(
                    np.zeros((4, 4, 256), dtype=np.float32),
                    AxisCalibration(AxisKind.ENERGY, 1.0, 0.0, "eV"),
                )
        finally:
            plot.close()
