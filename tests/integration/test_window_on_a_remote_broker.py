"""
Integration tests: the window pointed at a broker in another process.

**No device handle is passed to the widget in this file, anywhere.** That
is the whole claim under test. Every control the window offers has to be
built from what the broker *describes*, every value it displays has to
come from a watch call, and everything it drives has to go through a
lease it was granted - because there is nothing else within reach.

The broker is served over a real socket and reached with a real
``RemoteBroker``, rather than being handed the local one, since the
failure this guards against is precisely a read that works in process
and does not cross: a ``camera.parameters()`` here, an ``isinstance``
there, each of which looks fine until the devices are somewhere else.
The server runs on a thread of this process so the preview devices stay
inspectable from the test - what is remote is the *client*, which is the
side the window is on.

Skipped without a display (see conftest.py).
"""

import time
from collections.abc import Callable, Iterator

import pytest

pytest.importorskip("napari", reason="requires the 'viewer' extra")

import napari

from miainwoodpecker.broker.invitation import BrokerInvitation
from miainwoodpecker.broker.local import LocalBroker
from miainwoodpecker.broker.remote import connect_broker
from miainwoodpecker.broker.server import serve_broker
from miainwoodpecker.devices.interface import (
    DEFOCUS_CONTROL,
    PROJECTED_READOUT,
)
from miainwoodpecker.devices.rpc import INSTRUMENT_TARGET, SCANNER_TARGET
from miainwoodpecker.storage.session import Session
from miainwoodpecker.viewer import app
from miainwoodpecker.viewer.live import LiveInstrumentWidget
from miainwoodpecker.viewer.preview import (
    PREVIEW_BACKEND,
    _EELS_TARGET,
    build_preview_devices,
)

_AUTHKEY = b"window-on-a-remote-broker"
_A_DEFOCUS_NM = 125.0
_TWO_CAMERAS = 2
_DEADLINE_S = 5.0


def _wait_until(
    condition: Callable[[], bool],
    deadline_s: float = _DEADLINE_S,
) -> bool:
    """
    Poll a condition until it is true or the deadline elapses.

    Parameters
    ----------
    condition : Callable[[], bool]
        Called repeatedly; polled rather than waited on because the
        frames arrive on the broker's own threads.
    deadline_s : float
        How long to keep asking.

    Returns
    -------
    bool
        Whether it came true in time.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def instrument() -> Iterator[tuple]:
    """
    Serve a preview instrument on a socket, with nothing else attached.

    Yields
    ------
    tuple
        The preview devices, a connected ``RemoteBroker`` over them, and
        the address a second client can dial - because "somebody else is
        driving" needs somebody else.
    """
    devices = build_preview_devices(scan=True, camera=True, camera_count=2)
    targets = {
        INSTRUMENT_TARGET: devices.instrument,
        SCANNER_TARGET: devices.scanner,
        **devices.cameras,
    }
    served = LocalBroker(targets, holder="instrument-pc")
    server = serve_broker(served, authkey=_AUTHKEY)
    address = ("localhost", server.port)
    client = connect_broker(address, authkey=_AUTHKEY)
    try:
        yield devices, client, address
    finally:
        client.close()
        server.close()
        served.close()


@pytest.fixture
def window(instrument: tuple) -> Iterator[tuple]:
    """
    Open a window on the served instrument, holding no device handle.

    Parameters
    ----------
    instrument : tuple
        The devices and the connected broker.

    Yields
    ------
    tuple
        The preview devices and the widget built over the broker.
    """
    devices, broker, _ = instrument
    viewer = napari.Viewer(show=False)
    widget = LiveInstrumentWidget(viewer, broker=broker)
    try:
        yield devices, widget
    finally:
        widget.shutdown()
        viewer.close()


def test_the_window_is_built_from_the_description_alone(window):
    """
    Every control comes up, with no device within reach to ask.

    The detector checkboxes, the binning menus and the identity row were
    each read off a handle until now. A window that opened with an empty
    Scan group and no binning factors would be the honest result of
    reading them from a device that is in another process - and is what
    this file exists to prevent.
    """
    devices, widget = window

    assert widget._has_scanner  # noqa: SLF001
    assert len(widget._camera_bindings) == _TWO_CAMERAS  # noqa: SLF001
    # The scan unit's detectors, named by the device rather than by slot.
    assert widget.channel_names() == tuple(devices.scanner.channel_names)
    assert list(widget._channel_checks) == list(widget.channel_names())  # noqa: SLF001
    # And the spectrometer's binning menu, which is the one that had to
    # cross as a *pair* of axes rather than one list.
    eels = widget._camera_bindings[_EELS_TARGET]  # noqa: SLF001
    offered = [
        eels.binning_combo.itemText(index)
        for index in range(eels.binning_combo.count())
    ]
    down, _across = devices.cameras[_EELS_TARGET].binning_values_yx
    assert offered == [str(value) for value in down]
    assert eels.binning_across_combo is not None


def test_the_identity_row_names_the_backend_it_is_connected_to(window):
    """
    An operator has to be able to tell the simulator from the microscope.

    It is the one fact in the panel that is not derivable from the target
    names, and it used to be a ``describe()`` on the instrument handle.
    """
    _, widget = window

    assert widget._instrument_backend_label.text() == PREVIEW_BACKEND  # noqa: SLF001
    assert SCANNER_TARGET in widget._instrument_targets_label.text()  # noqa: SLF001


def test_a_control_read_and_written_from_the_panel_crosses_the_wire(window):
    """
    Refresh shows what the instrument reads; Set changes it.

    The read is watch-side and the write takes a lease, which is the
    asymmetry the broker's protocol insists on: a window that had to
    lease the instrument to display a defocus would hold one from the
    moment it opened.
    """
    devices, widget = window

    widget._instrument_controls[DEFOCUS_CONTROL].setValue(_A_DEFOCUS_NM)  # noqa: SLF001
    widget.apply_instrument_control(DEFOCUS_CONTROL)

    assert devices.instrument.defocus_nm() == _A_DEFOCUS_NM
    assert widget._instrument_status.text() == f"{DEFOCUS_CONTROL} set"  # noqa: SLF001

    devices.instrument.set_defocus_nm(_A_DEFOCUS_NM * 2)
    widget.refresh_instrument()

    assert (
        widget._instrument_controls[DEFOCUS_CONTROL].value()  # noqa: SLF001
        == _A_DEFOCUS_NM * 2
    )


def test_a_control_another_client_is_holding_is_refused_not_interleaved(
    instrument,
    window,
):
    """
    Somebody else is driving, and the window says so rather than colliding.

    The failure this whole layer exists to prevent: two clients on a
    one-request-at-a-time device. It is a *new* outcome for this panel -
    writing a control could not be refused when the window owned the
    instrument - and it has to reach the operator as a sentence rather
    than as a value that quietly did not take.
    """
    _, _, address = instrument
    devices, widget = window
    before = devices.instrument.defocus_nm()
    notebook = connect_broker(address, authkey=_AUTHKEY)
    try:
        with notebook.lease(INSTRUMENT_TARGET, reason="notebook sweep"):
            widget._instrument_controls[DEFOCUS_CONTROL].setValue(_A_DEFOCUS_NM)  # noqa: SLF001
            widget.apply_instrument_control(DEFOCUS_CONTROL)
    finally:
        notebook.close()

    assert devices.instrument.defocus_nm() == before
    status = widget._instrument_status.text()  # noqa: SLF001
    assert f"{DEFOCUS_CONTROL} refused" in status
    # Whose it is, not merely that it is somebody's: an operator who can
    # see "notebook sweep" knows what to go and stop.
    assert "notebook sweep" in status


def test_the_readout_mode_reaches_the_detector(window):
    """
    Changing the detector's mode configures it, under a lease, over the wire.

    The readout is the setting that decides the *rank* of every frame the
    detector produces, and the panel showed it by asking the camera
    directly. It is now a watch call and a leased ``configure``.
    """
    devices, widget = window

    widget.set_camera_readout(_EELS_TARGET, PROJECTED_READOUT)

    assert devices.cameras[_EELS_TARGET].parameters().readout == PROJECTED_READOUT
    binding = widget._camera_bindings[_EELS_TARGET]  # noqa: SLF001
    assert binding.readout_combo.currentText() == PROJECTED_READOUT


def test_a_live_view_started_from_the_window_arrives_over_the_wire(window):
    """
    The picture itself, which is what all the chrome above is around.

    Starting the loop is a broker call, the frames are read by the
    display timer's ``snapshot``, and both cross a socket here. A window
    whose controls all worked and whose layer stayed empty would have
    passed every other test in this file.
    """
    _, widget = window

    widget.start_camera(_EELS_TARGET)
    try:
        assert _wait_until(
            lambda: widget._broker.latest(_EELS_TARGET) is not None,  # noqa: SLF001
        )
        widget.refresh_display()
    finally:
        widget.stop_camera(_EELS_TARGET)

    layer = widget._camera_bindings[_EELS_TARGET].layer_name  # noqa: SLF001
    assert layer in widget._viewer.layers  # noqa: SLF001


def test_a_window_on_an_instrument_with_nothing_to_show_is_refused():
    """
    An empty window is worse than a message saying why there is none.

    The check used to be "no scanner and no camera handle"; with a broker
    there are no handles to count, so it is the description that has to
    answer - and an instrument serving only a controller has nothing to
    display either way.
    """
    devices = build_preview_devices(scan=True, camera=True)
    # The controller alone: an aberration corrector with its detectors
    # off is a real configuration, and there is nothing to put on screen.
    served = LocalBroker({INSTRUMENT_TARGET: devices.instrument})
    viewer = napari.Viewer(show=False)
    try:
        with pytest.raises(ValueError, match="nothing to display"):
            LiveInstrumentWidget(viewer, broker=served)
    finally:
        served.close()
        viewer.close()


def test_an_invitation_that_leads_nowhere_is_a_sentence_not_a_traceback(
    tmp_path,
):
    """
    Both ways of launching against nothing fail in words an operator can act on.

    A missing file usually means the broker was never started, or was
    started without ``--publish``; a file whose broker has since stopped
    is the commoner one on an instrument PC, where the invitation
    outlives the process that wrote it.
    """
    with pytest.raises(SystemExit, match="could not read the broker invitation"):
        app._open_on_broker(  # noqa: SLF001
            str(tmp_path / "broker.json"),
            Session(tmp_path),
        )

    BrokerInvitation(host="localhost", port=1, authkey=b"nobody").write_to(tmp_path)

    with pytest.raises(SystemExit, match="no broker answered"):
        app._open_on_broker(str(tmp_path), Session(tmp_path))  # noqa: SLF001
