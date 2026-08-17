"""
Operator preferences that outlive a session.

A session is a per-day directory of data, so it is the wrong home for
"which detectors I use" or "what dwell I preview at" — those follow the
*operator and the instrument*, not the shift's data. They live in the
platform's config directory instead, and this module is the only thing
that reads or writes them.

**Preferences only, never instrument state.** Nothing here is allowed to
influence what a recording contains or claims. What is stored is which
controls the window came up with; what the instrument actually did is
recorded per acquisition in the file, from the device, every time. The
distinction matters because a preference file is editable, copyable and
stale by nature, and a number that reached a dataset from one would be a
measurement with no provenance.

Failure is non-fatal, deliberately. A missing, unreadable, or malformed
file leaves the defaults in place and the application starts normally: a
viewer that will not open because a settings file has a stray brace is
worse than one that forgets a checkbox.
"""

from __future__ import annotations

import json
import logging
import pathlib
import typing

_APP_NAME = "miainwoodpecker"
_FILENAME = "viewer-preferences.json"
_LOG = logging.getLogger(__name__)


def preferences_path() -> pathlib.Path:
    """
    Return the file preferences are stored in.

    ``platformdirs`` is imported here rather than at module scope, and
    that is load-bearing rather than tidy. It ships with the ``viewer``
    extra, but the base test environment installs no extras — and this
    module is imported by the suite's own ``conftest``, so a top-level
    import made *every* test in the repository fail to collect,
    including the several hundred that never touch a config directory.
    Locating the directory is the only thing here that needs the
    dependency, so it is the only thing that asks for it.

    Returns
    -------
    pathlib.Path
        The path, whether or not it exists. Without ``platformdirs``
        this falls back to ``~/.config/miainwoodpecker`` — correct on
        Linux, serviceable elsewhere, and a much smaller loss than
        refusing to start.
    """
    try:
        import platformdirs  # noqa: PLC0415
    except ImportError:
        return pathlib.Path.home() / ".config" / _APP_NAME / _FILENAME
    return pathlib.Path(platformdirs.user_config_dir(_APP_NAME)) / _FILENAME


def load(path: pathlib.Path | None = None) -> dict[str, object]:
    """
    Read stored preferences, returning an empty mapping if there are none.

    Parameters
    ----------
    path : pathlib.Path | None
        Where to read from, or None for :func:`preferences_path`.

    Returns
    -------
    dict[str, object]
        The stored preferences, or ``{}`` when there are none to read.
    """
    target = path if path is not None else preferences_path()
    try:
        with target.open(encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        # Logged rather than raised: see this module's docstring. The
        # operator loses their remembered checkboxes, not their viewer.
        _LOG.warning("ignoring unreadable preferences at %s: %s", target, error)
        return {}
    if not isinstance(stored, dict):
        _LOG.warning("ignoring preferences at %s: not a JSON object", target)
        return {}
    return stored


def save(
    values: dict[str, object],
    path: pathlib.Path | None = None,
) -> bool:
    """
    Write preferences, creating the config directory if needed.

    Parameters
    ----------
    values : dict[str, object]
        The preferences to store.
    path : pathlib.Path | None
        Where to write, or None for :func:`preferences_path`.

    Returns
    -------
    bool
        True if the file was written. False means the failure was
        logged and swallowed — an operator who cannot write their config
        directory should still be able to run the instrument.
    """
    target = path if path is not None else preferences_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(values, handle, indent=2, sort_keys=True)
    except OSError as error:
        _LOG.warning("could not save preferences to %s: %s", target, error)
        return False
    return True


def stored_channels(
    values: dict[str, object],
    available: typing.Sequence[str],
) -> list[str]:
    """
    Return the remembered detector selection, filtered to what exists.

    Filtered rather than trusted, because the file outlives the
    instrument it was written against: a preference naming a detector
    this column does not have must not produce a checkbox for hardware
    that is not fitted, which is the same rule the Instrument panel
    follows for controls.

    An empty or unusable selection falls back to the first available
    detector rather than none. A scan with nothing enabled produces no
    data at all, and coming up in that state would look like a broken
    instrument rather than a remembered choice.

    Parameters
    ----------
    values : dict[str, object]
        Loaded preferences.
    available : typing.Sequence[str]
        The detector names this scanner actually has.

    Returns
    -------
    list[str]
        The names to enable, always at least one when any exist.
    """
    if not available:
        return []
    stored = values.get("scan_channels")
    names = (
        [str(name) for name in stored if str(name) in available]
        if isinstance(stored, list)
        else []
    )
    return names or [available[0]]
