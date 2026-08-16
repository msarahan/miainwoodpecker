"""
The camera probe's reporting, which is the part that has to be right.

Opening a real capture device is not testable here and never will be —
that is the whole reason the script exists and the whole reason it has
to be run on the operator's machine. What *is* testable is everything
around the open: which diagnosis each outcome produces, and whether the
command it prints would actually work. Those are where a wrong answer
sends someone chasing a cable when the problem is a flag.

The reporting is driven with hand-built results rather than a mocked
``cv2``, because the thing under test is the branching, and a fake
``VideoCapture`` would only re-state the shape of the data this already
passes in directly.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_cameras.py"


@pytest.fixture(scope="module")
def probe() -> dict[str, object]:
    """
    Load the script as a module without installing it.

    Returns
    -------
    dict[str, object]
        The script's namespace.
    """
    return runpy.run_path(str(_SCRIPT))


def _working(index: int) -> dict[str, object]:
    """
    Return a probe result for a camera that opened and delivered a frame.

    Parameters
    ----------
    index : int
        The capture index it was found at.

    Returns
    -------
    dict[str, object]
        One entry in the shape ``probe_with_opencv`` returns.
    """
    return {
        "index": index,
        "width": 1280,
        "height": 720,
        "backend": "TEST",
        "frame": (720, 1280, 3),
    }


def test_a_device_that_never_enumerated_is_not_blamed_on_permissions(probe, capsys):
    """
    "Nothing opened" and "the OS saw nothing" is a cable, not a group.

    The distinction this test exists for: telling someone to check their
    ``video`` group membership when the kernel never bound the device
    sends them at the wrong problem entirely, and it is the wrong
    problem that costs the afternoon.
    """
    probe["report_probe"]([], os_saw_device=False)
    printed = capsys.readouterr().out

    assert "not enumerating" in printed
    assert "cable" in printed
    assert "group" not in printed
    assert "Privacy" not in printed


def test_a_device_the_os_saw_but_could_not_be_opened_is_a_permission(probe, capsys):
    """
    The same "nothing opened", with the OS having seen it, is the opposite advice.

    Each platform gets its own sentence because they fail for entirely
    different reasons; what is asserted here is that *some* permission
    diagnosis is offered and the enumeration one is not.
    """
    probe["report_probe"]([], os_saw_device=True)
    printed = capsys.readouterr().out

    assert "not enumerating" not in printed
    expected = {
        "darwin": "Privacy",
        "win32": "Privacy",
    }.get(sys.platform, "video")
    assert expected in printed


def test_the_printed_command_names_both_flags_that_default_elsewhere(probe, capsys):
    """
    The command has to carry ``--server-module``, or it cannot possibly work.

    This is the failure the script was written for. The viewer defaults
    to ``nion_server``, which serves no USB camera at all, so a
    suggested command missing that flag would be confidently wrong — it
    would run, open a window, and show nothing.
    """
    probe["report_probe"]([_working(0)], os_saw_device=True)
    printed = capsys.readouterr().out

    assert "--server-module miainwoodpecker.devices.camera_server" in printed
    assert "--backend hardware" in printed
    assert "--plugin 0" in printed


def test_every_working_camera_is_offered_not_only_the_first(probe, capsys):
    """
    Two cameras produce two ``--plugin`` flags, which is the two-device case.

    A laptop with its own webcam and a microscope plugged in is the
    ordinary situation rather than an exotic one, and the whole point of
    the probe is that the microscope is usually *not* index 0.
    """
    probe["report_probe"]([_working(0), _working(2)], os_saw_device=True)
    printed = capsys.readouterr().out

    assert "--plugin 0 --plugin 2" in printed
    assert "camera:2" in printed


def test_a_camera_that_opens_but_delivers_nothing_is_reported_as_such(probe, capsys):
    """
    Opening is not succeeding, and the advice differs.

    A device that opens and then delivers no frame is a driver or
    bandwidth problem — the classic symptom of a microscope behind a
    hub — and suggesting a viewer command for it would be useless.
    """
    dead = {**_working(0), "frame": None}
    probe["report_probe"]([dead], os_saw_device=True)
    printed = capsys.readouterr().out

    assert "delivered NO frame" in printed
    assert "hub" in printed
    assert "miainwoodpecker-viewer" not in printed


def test_an_absent_system_tool_is_not_an_error(probe):
    """
    The OS half degrades rather than failing when a tool is missing.

    ``v4l2-ctl`` and ``lsusb`` are not installed everywhere, and a probe
    that traced back because a helper was absent would lose the answers
    it had already collected.
    """
    assert probe["_run"](["definitely-not-a-real-command-xyz"]) is None


def test_the_os_half_opens_nothing(probe, capsys):
    """
    Reporting what the OS knows must not trigger a permission prompt.

    On macOS the prompt is attached to opening a device, and the point
    of the ``--skip-open`` path is that an operator can ask "is it even
    plugged in?" without granting anything. Asserted through the return
    contract and the absence of probe output rather than by watching for
    a dialog.
    """
    saw = probe["report_operating_system_view"]()
    printed = capsys.readouterr().out

    assert isinstance(saw, bool)
    assert "What OpenCV could actually open" not in printed
