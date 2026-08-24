"""
Showing an operator the client they already have, rather than a second one.

The tray starts one of each kind of front end and the second click on an
entry means "where did it go?". Answering that is the awkward part,
because what has to be brought forward belongs to *another process* and
there is no portable way to ask an unrelated process to raise its window.

So this is a best effort with an honest failure, and the two kinds fail
differently on purpose:

- A **window** is raised through the platform. On Windows that is
  ``EnumWindows`` filtered by process id, then restore and set
  foreground - which works from a tray menu because the click that
  opened the menu is what gives this process the right to hand
  foreground away. Nowhere else has an equivalent that does not mean
  shelling out to a window manager's own tool, so elsewhere this
  reports that it could not, and the caller says so in words.
- A **dashboard** is a server with a page on it, and its window is a
  browser tab this process never opened and cannot enumerate. Opening
  its address again is the whole of what can be done - which the
  browser will usually answer by focusing the tab that already has that
  page, and otherwise by opening a second one. A duplicate tab is a far
  smaller cost than a duplicate marimo server, which is what starting
  another dashboard would be.

Nothing here raises. A failure to bring a window forward is a cosmetic
disappointment on the way to telling somebody where their window is, and
it must not be able to take down the tray that was trying to help.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import typing
import webbrowser

if typing.TYPE_CHECKING:
    import subprocess

_LOGGER = logging.getLogger("miainwoodpecker.tray.raising")

_SW_RESTORE = 9
"""``ShowWindow``'s "un-minimise, and leave a normal window alone"."""


def show(process: subprocess.Popen, url: str | None = None) -> bool:
    """
    Bring one already-running client to the operator's attention.

    Parameters
    ----------
    process : subprocess.Popen
        The client that is already running.
    url : str | None
        Where it serves a page, if it does. Given one, that is what is
        opened; a window has none and is raised instead.

    Returns
    -------
    bool
        Whether anything was actually shown. False is not an error, it
        is "tell them in words instead".
    """
    if url is not None:
        return open_page(url)
    return raise_window(process.pid)


def open_page(url: str) -> bool:
    """
    Open an address in the operator's browser.

    Parameters
    ----------
    url : str
        The address, as the front end printed it - including whatever
        access token it put in the query string, without which the page
        would answer with a login rather than the dashboard.

    Returns
    -------
    bool
        Whether a browser took it.
    """
    try:
        opened = webbrowser.open(url)
    except OSError as error:
        _LOGGER.warning("could not open %s: %s", url, error)
        return False
    if not opened:
        _LOGGER.warning("no browser would open %s", url)
    return opened


def raise_window(pid: int) -> bool:
    """
    Bring another process's window to the front, where the platform allows.

    Parameters
    ----------
    pid : int
        The process whose window to raise.

    Returns
    -------
    bool
        Whether a window was found and raised. False on a platform with
        no way to do this, and on one where the process has no window
        yet - which is the ordinary state of a napari process for the
        first several seconds of its life.
    """
    if sys.platform != "win32":
        # No portable equivalent: X11 and Wayland each need a window
        # manager's own tool, and macOS needs the process to cooperate.
        # Saying so beats a dependency and a silent no-op.
        return False
    return _raise_on_windows(pid)


def _raise_on_windows(pid: int) -> bool:
    """
    Find a visible top-level window owned by a process, and raise it.

    Parameters
    ----------
    pid : int
        The process whose window to raise.

    Returns
    -------
    bool
        Whether one was found and the platform accepted the request.
    """
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: list[int] = []

    def visit(handle: int, _parameter: int) -> bool:
        """
        Keep the first visible, titled window this process owns.

        Parameters
        ----------
        handle : int
            The window being offered.
        _parameter : int
            The value passed to ``EnumWindows``, unused.

        Returns
        -------
        bool
            False to stop the enumeration, which is what keeping one
            window means.
        """
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value != pid or not user32.IsWindowVisible(handle):
            return True
        # Untitled top-level windows are the toolkit's own - message
        # sinks, and napari's splash before it has a title - and raising
        # one of those shows the operator nothing.
        if user32.GetWindowTextLengthW(handle) == 0:
            return True
        found.append(handle)
        return False

    enumerator = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    try:
        user32.EnumWindows(enumerator(visit), 0)
    except OSError as error:
        _LOGGER.warning("could not enumerate windows: %s", error)
        return False
    if not found:
        _LOGGER.info("process %s has no window to raise yet", pid)
        return False
    user32.ShowWindow(found[0], _SW_RESTORE)
    # Refused when this process is not entitled to hand the foreground
    # away, which from a tray menu it is - the click that opened the
    # menu is what entitles it. Reported rather than asserted, because a
    # refusal here is exactly the case the caller falls back for.
    if not user32.SetForegroundWindow(found[0]):
        _LOGGER.info("the platform declined to raise the window of %s", pid)
        return False
    return True
