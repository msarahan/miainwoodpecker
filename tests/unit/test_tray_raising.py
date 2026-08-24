"""
Unit tests: showing an operator the client they already have.

The interesting part is not the platform call - it is what happens when
there is nothing to raise, because that is the common case rather than
the edge one: a napari process has no window for the first several
seconds of its life, and a desktop without a way to raise anything is
the ordinary state of Linux and macOS here. Both must report that they
did nothing rather than pretend, since the caller's fallback is to tell
the operator where to look in words.
"""

from __future__ import annotations

import os

from miainwoodpecker.tray import raising


def test_a_process_with_no_window_reports_that_it_raised_nothing():
    """
    Which is a napari process for the first seconds of its life.

    Reported rather than swallowed, because the caller's answer to "no"
    is a notification saying a viewer is already open - and the answer
    to a silent success would be nothing at all.
    """
    # This process: a pytest run has no visible titled window of its
    # own, and on a platform that cannot raise anything the answer is
    # the same for a different reason.
    assert raising.raise_window(os.getpid()) is False


def test_an_address_is_opened_in_a_browser_rather_than_raised(monkeypatch):
    """
    A dashboard's window is a browser tab this process never opened.

    It cannot be enumerated and it cannot be raised, so the whole of
    what can be done is to ask the browser for that address again -
    which it usually answers by focusing the tab that already has it.
    """
    asked = []
    monkeypatch.setattr(
        raising.webbrowser,
        "open",
        lambda url: asked.append(url) or True,
    )

    assert raising.open_page("http://localhost:2718?access_token=abc") is True
    assert asked == ["http://localhost:2718?access_token=abc"]


def test_a_browser_that_will_not_open_is_reported_rather_than_raising(monkeypatch):
    """
    A headless machine has no browser, and that is not a crash.

    This runs from a menu click on the way to helping somebody find a
    window; an exception here would be a tray application falling over
    because it could not do a favour.
    """

    def refuse(url: str) -> bool:
        """
        Stand in for a machine with no browser configured.

        Parameters
        ----------
        url : str
            The address that was not opened.

        Returns
        -------
        bool
            False, always.
        """
        del url
        return False

    monkeypatch.setattr(raising.webbrowser, "open", refuse)

    assert raising.open_page("http://localhost:2718") is False


def test_a_url_takes_precedence_over_a_window(monkeypatch):
    """
    Both are "show me the one that is running", and only one can be.

    A front end that printed an address is a server; its process may
    well have no window at all, and raising it would show an operator a
    console rather than their dashboard.
    """
    asked = []
    monkeypatch.setattr(raising, "open_page", lambda url: asked.append(url) or True)
    monkeypatch.setattr(
        raising,
        "raise_window",
        _refuse_to_raise,
    )

    class _Process:
        """A stand-in with the one attribute this reads."""

        pid = os.getpid()

    assert raising.show(_Process(), "http://localhost:2718") is True
    assert asked == ["http://localhost:2718"]


def _refuse_to_raise(pid: int) -> bool:
    """
    Fail the test if a window raise was attempted for a URL-bearing client.

    Parameters
    ----------
    pid : int
        The process that should not have been raised.

    Returns
    -------
    bool
        Never returns.

    Raises
    ------
    AssertionError
        Always.
    """
    message = f"raised the window of {pid} instead of opening its page"
    raise AssertionError(message)
