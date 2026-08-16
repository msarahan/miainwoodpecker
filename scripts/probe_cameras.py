"""
Find out whether this machine can see a camera, and which index it is.

"The USB microscope is plugged in and nothing shows up" has four
distinct causes, and they need different fixes, so guessing between them
wastes an afternoon:

1. **The OS does not see the device at all** — a cable, a hub, or a
   device that needs more power than the port gives.
2. **The OS sees it but this process may not open it** — a macOS privacy
   grant, or Linux ``video`` group membership.
3. **It opens fine, but it is not index 0** — a laptop's built-in webcam
   is usually 0, and the microscope is 1. This is the single most common
   cause, and it looks exactly like a broken device from the viewer.
4. **It opens fine and the viewer was pointed at the wrong server** —
   the viewer defaults to ``nion_server``, which serves no USB camera at
   all, so the microscope cannot appear no matter what is plugged in.

This script separates them. It asks the operating system what it knows,
then asks OpenCV to actually open each candidate, and prints the exact
command to run for whatever it finds. It is read-only with respect to
the instrument: it opens capture devices, reads one frame, and closes
them.

**Run it on the machine with the microscope plugged in**, which is the
whole point — nothing about it works remotely.

    python scripts/probe_cameras.py

Only the standard library is needed for the operating-system half. The
open-it-and-read-a-frame half needs OpenCV (this project's ``camera``
extra); without it the script still runs and says so, because "the OS
can see it" is already worth knowing.

**On macOS, opening a camera triggers a privacy prompt**, and the grant
goes to the application *responsible* for the process — your terminal,
not Python. Run this from the same terminal you will run the viewer
from, or the answer will not transfer.
"""

from __future__ import annotations

import argparse
import contextlib
import platform
import shutil
import subprocess
import sys

# Enough to cover a laptop webcam, a USB microscope, a capture card and a
# virtual camera or two, without making a "nothing here" run slow: each
# miss costs a backend probe, and on macOS those are not instant.
_DEFAULT_MAX_INDEX = 8
_RULE = "-" * 68


def _run(command: list[str], *, timeout_s: float = 20.0) -> str | None:
    """
    Run a read-only system command and return its output, or None.

    Parameters
    ----------
    command : list[str]
        The command and its arguments.
    timeout_s : float
        How long to wait before giving up.

    Returns
    -------
    str | None
        Standard output, or None if the tool is absent, failed, or hung.
    """
    if shutil.which(command[0]) is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def report_operating_system_view() -> bool:
    """
    Print what the operating system says is attached, per platform.

    Deliberately three separate answers rather than one abstraction: the
    platforms do not agree on what a camera is, and a wrong-but-uniform
    answer here would be worse than a specific one. Nothing is opened,
    so this half never triggers a permission prompt.

    Returns
    -------
    bool
        Whether the operating system reported a camera. False is not
        "no camera" on every platform — Windows and macOS answers are
        best-effort — but it is enough to tell a device that never
        enumerated from one that enumerated and would not open.
    """
    print(f"System: {platform.platform()}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print(_RULE)
    print("What the operating system reports")
    print(_RULE)

    if sys.platform == "darwin":
        cameras = _run(["system_profiler", "SPCameraDataType"])
        if cameras:
            print(cameras.strip() or "(no cameras listed)")
        else:
            print("system_profiler SPCameraDataType returned nothing.")
        print()
        print("USB devices (look for your microscope by name):")
        usb = _run(["system_profiler", "SPUSBDataType"])
        if usb:
            # The full tree is hundreds of lines; the product names are
            # the part an operator needs to recognise their device.
            names = [
                line.rstrip(":").strip()
                for line in usb.splitlines()
                if line.strip().endswith(":") and line.startswith(" " * 8)
            ]
            print(("  " + "\n  ".join(names)) if names else "  (none found)")
        else:
            print("  (system_profiler SPUSBDataType returned nothing)")
        return bool(cameras and cameras.strip())

    if sys.platform.startswith("linux"):
        import glob  # noqa: PLC0415 - only this branch needs it
        import os  # noqa: PLC0415 - only this branch needs it

        nodes = sorted(glob.glob("/dev/video*"))  # noqa: PTH207 - plain glob
        if nodes:
            print("Video device nodes:")
            for node in nodes:
                readable = "readable" if os.access(node, os.R_OK) else "NOT READABLE"
                print(f"  {node}  ({readable})")
            print()
            print(
                "A node that is not readable is a permissions problem, not a "
                "hardware one: add this user to the 'video' group and log in "
                "again.",
            )
        else:
            print("No /dev/video* nodes. The kernel has not bound a UVC driver.")
        print()
        listing = _run(["v4l2-ctl", "--list-devices"])
        if listing:
            print("v4l2-ctl --list-devices:")
            print(listing.strip())
        usb = _run(["lsusb"])
        if usb:
            print()
            print("lsusb:")
            print(usb.strip())
        return bool(nodes)

    if sys.platform == "win32":
        listing = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-PnpDevice -Class Camera,Image -PresentOnly "
                    "| Format-Table -AutoSize Status,FriendlyName"
                ),
            ],
        )
        print(listing.strip() if listing else "Get-PnpDevice returned nothing.")
        print()
        print(
            "DirectShow admits one consumer at a time: close any other "
            "application holding the camera before probing.",
        )
        return bool(listing and listing.strip())

    print(f"No operating-system probe written for {sys.platform!r}.")
    # Unknown platform: claim nothing, so the probe below is the only
    # thing that gets to say whether a camera is there.
    return True


def probe_with_opencv(max_index: int) -> list[dict[str, object]]:
    """
    Try to open each candidate index and read one frame from it.

    Opening is the only honest test. A device can be listed by the
    operating system and still refuse to open — held by another
    application, or not permitted to this process — and that distinction
    is exactly what the listing above cannot make.

    Parameters
    ----------
    max_index : int
        Highest capture index to try; 0 through this number are probed.

    Returns
    -------
    list[dict[str, object]]
        One entry per index that opened, with what it reported.
    """
    try:
        import cv2  # noqa: PLC0415 - optional, and its absence is a real answer
    except ImportError:
        print("OpenCV is not installed in this interpreter, so nothing was opened.")
        print("Install this project's 'camera' extra to probe further:")
        print(
            "    uv run --extra camera --extra viewer python "
            "scripts/probe_cameras.py",
        )
        return []

    # A miss on every index prints a wall of backend warnings that buries
    # the answer. They are expected here - probing *is* asking about
    # devices that may not exist - so the result below is the report, not
    # the log.
    with contextlib.suppress(AttributeError):  # older OpenCV builds lack it
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)

    found: list[dict[str, object]] = []
    for index in range(max_index + 1):
        capture = cv2.VideoCapture(index)
        try:
            if not capture.isOpened():
                continue
            ok, frame = capture.read()
            found.append(
                {
                    "index": index,
                    "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    "backend": capture.getBackendName(),
                    # Opening without reading proves less than it looks:
                    # some devices open and then deliver nothing.
                    "frame": None if not ok or frame is None else frame.shape,
                },
            )
        finally:
            capture.release()
    return found


def report_probe(found: list[dict[str, object]], *, os_saw_device: bool) -> None:
    """
    Print what opened, and the command that would serve it.

    Parameters
    ----------
    found : list[dict[str, object]]
        What :func:`probe_with_opencv` returned.
    os_saw_device : bool
        Whether the operating-system half found anything. It decides
        which "nothing opened" this is: a device the kernel never bound
        is a cable or a driver, and telling someone to check their group
        membership in that case sends them the wrong way.
    """
    print()
    print(_RULE)
    print("What OpenCV could actually open")
    print(_RULE)
    if not found:
        print("Nothing opened.")
        print()
        if not os_saw_device:
            print(
                "The operating system did not report a camera either, so "
                "this is upstream of any permission: the device is not "
                "enumerating. Try a different cable, a port directly on "
                "the machine rather than through a hub, and check whether "
                "the microscope's own light comes on.",
            )
            return
        if sys.platform == "darwin":
            print(
                "On macOS this is usually the privacy grant rather than the "
                "device: camera access belongs to the application "
                "*responsible* for this process - your terminal, not Python. "
                "Check System Settings > Privacy & Security > Camera, and "
                "look for your terminal application rather than for Python.",
            )
        elif sys.platform.startswith("linux"):
            print(
                "The kernel bound a device node but OpenCV could not open "
                "it, which is usually group membership: /dev/video* is "
                "owned by the 'video' group.",
            )
        else:
            print(
                "On Windows, check Settings > Privacy & security > Camera, "
                "and close any application already holding the device.",
            )
        return

    for entry in found:
        frame = entry["frame"]
        delivered = (
            f"read a {frame} frame" if frame else "opened but delivered NO frame"
        )
        print(
            f"  index {entry['index']}: {entry['width']}x{entry['height']} "
            f"via {entry['backend']} - {delivered}",
        )

    print()
    print(_RULE)
    print("How to see it in the viewer")
    print(_RULE)
    working = [entry for entry in found if entry["frame"]]
    if not working:
        print(
            "Every device opened but none delivered a frame, which is a "
            "driver or bandwidth problem rather than a missing device. On a "
            "USB hub, try a port directly on the machine.",
        )
        return

    indices = " ".join(f"--plugin {entry['index']}" for entry in working)
    print("Two flags matter, and both default to something else:")
    print()
    print(
        "  --server-module: the viewer defaults to nion_server, which serves "
        "no USB camera at all. A microscope cannot appear without this.",
    )
    print("  --plugin: which device to open. Defaults to 0, usually the webcam.")
    print()
    print("Serving everything that worked:")
    print()
    print("    uv run --extra camera --extra viewer miainwoodpecker-viewer \\")
    print("        --backend hardware \\")
    print("        --server-module miainwoodpecker.devices.camera_server \\")
    print(f"        {indices}")
    if len(working) > 1:
        print()
        print(
            f"That serves all {len(working)} as separate targets - camera, "
            "camera:2, ... - each with its own section and its own layer. "
            "Drop the ones you do not want.",
        )


def main(argv: list[str] | None = None) -> int:
    """
    Run both halves of the probe and print what to do next.

    Parameters
    ----------
    argv : list[str] | None
        Arguments after the program name, or None to read ``sys.argv``.

    Returns
    -------
    int
        0 if at least one camera delivered a frame, 1 otherwise, so this
        is usable in a shell conditional.
    """
    parser = argparse.ArgumentParser(
        description="Report which cameras this machine can see and open.",
    )
    parser.add_argument(
        "--max-index",
        type=int,
        default=_DEFAULT_MAX_INDEX,
        help=f"highest capture index to try (default {_DEFAULT_MAX_INDEX})",
    )
    parser.add_argument(
        "--skip-open",
        action="store_true",
        help="report only what the OS says; open nothing, prompt for nothing",
    )
    arguments = parser.parse_args(argv if argv is not None else sys.argv[1:])

    os_saw_device = report_operating_system_view()
    if arguments.skip_open:
        print()
        print("--skip-open given, so nothing was opened.")
        return 1
    if sys.platform == "darwin":
        print()
        print(
            "About to open each candidate; macOS may prompt for camera "
            "access, and the grant goes to this terminal.",
        )
    found = probe_with_opencv(arguments.max_index)
    report_probe(found, os_saw_device=os_saw_device)
    return 0 if any(entry["frame"] for entry in found) else 1


if __name__ == "__main__":
    raise SystemExit(main())
