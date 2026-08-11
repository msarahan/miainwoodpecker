"""
Entry point: the live viewer against a simulated or real microscope.

Run with ``miainwoodpecker-viewer`` (or
``uv run --extra device --extra viewer miainwoodpecker-viewer``).
Requires both the ``device`` and ``viewer`` optional dependency groups.

A session is opened before the window appears, so the app can never be in
the state that blocked a Phase 5 pilot: running, acquiring, and unable to
keep anything. ``--session`` names the directory (reused if it already
exists, so restarting mid-shift resumes the same session); the default is
one directory per day under ``~/miainwoodpecker-data``. Operator, sample,
and notes can be given here or typed into the Session group at any point
during the run, and the directory itself can be changed from there too.

Choosing the backend, and why the default is not configurable away
------------------------------------------------------------------
Phase 1 staged the device layer for hardware:
``remote_instrument(backend=..., plugin_names=...)`` selects between
nionswift-usim and a real instrument's own Nion plug-in. This entry point
passes that through — ``--backend {simulated,hardware}`` and a repeatable
``--plugin MODULE`` — so an operator never has to edit code to reach an
instrument, and both fall back to ``$MIAINWOODPECKER_BACKEND`` /
``$MIAINWOODPECKER_HARDWARE_PLUGINS`` with the command line winning.

The simulator stays the default in every resolution path, deliberately:
the failure this selector exists to prevent is someone driving a
microscope they meant to simulate, and its mirror image is someone
believing they are on hardware when they are not. Asking for ``hardware``
with no vendor plug-in present does not silently fall back to usim — the
device server exits with status 2 and says what it looked for (Phase 1),
and ``remote_instrument`` surfaces that rather than hanging.

Deliberately imports ``miainwoodpecker.devices.remote``, not
``miainwoodpecker.devices.nion_server``: the running application talks to
the device server over IPC and never imports Nion's GPL-3.0 code directly
(see docs/migration-plan.md, §6).
"""

from __future__ import annotations

import argparse
import os

import napari

from miainwoodpecker.devices.remote import (
    HARDWARE_BACKEND,
    SIMULATED_BACKEND,
    remote_instrument,
)
from miainwoodpecker.storage.session import Session, default_root
from miainwoodpecker.viewer.live import LiveInstrumentWidget

_BACKEND_ENV_VAR = "MIAINWOODPECKER_BACKEND"
_PLUGINS_ENV_VAR = "MIAINWOODPECKER_HARDWARE_PLUGINS"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse the viewer's command-line arguments.

    Parameters
    ----------
    argv : list[str] | None
        Argument list, or None to read ``sys.argv``.

    Returns
    -------
    argparse.Namespace
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "session directory for recorded data; reused if it exists. "
            "Defaults to a per-day directory under ~/miainwoodpecker-data."
        ),
    )
    parser.add_argument("--operator", default=None, help="who is on the instrument")
    parser.add_argument("--sample", default=None, help="sample identifier")
    parser.add_argument("--notes", default=None, help="free-text session notes")
    parser.add_argument(
        "--backend",
        choices=(SIMULATED_BACKEND, HARDWARE_BACKEND),
        default=os.environ.get(_BACKEND_ENV_VAR, SIMULATED_BACKEND),
        help=(
            f"device backend (default from ${_BACKEND_ENV_VAR}, else "
            f"{SIMULATED_BACKEND} - never silently hardware)"
        ),
    )
    parser.add_argument(
        "--plugin",
        action="append",
        # Deliberately no list default, unlike the device server's own
        # parser: argparse's "append" adds to whatever default it is given,
        # so seeding it from the environment would make --plugin *extend*
        # $MIAINWOODPECKER_HARDWARE_PLUGINS rather than replace it. The
        # environment is read in _hardware_plugins() instead, where "the
        # command line wins" is a statement rather than a hope.
        default=None,
        metavar="MODULE",
        help=(
            "nionswift_plugin module providing hardware devices; repeatable. "
            f"Defaults to ${_PLUGINS_ENV_VAR} (comma separated), else the "
            "device server's own autodiscovery."
        ),
    )
    return parser.parse_args(argv)


def _hardware_plugins(chosen: list[str] | None) -> tuple[str, ...]:
    """
    Resolve which vendor plug-in modules to ask the device server to load.

    Parameters
    ----------
    chosen : list[str] | None
        ``--plugin`` values, or None if none were given.

    Returns
    -------
    tuple[str, ...]
        The plug-in module names, empty for "let the server autodiscover".
    """
    if chosen:
        # The server also reads $MIAINWOODPECKER_HARDWARE_PLUGINS, and its
        # own parser appends to it, so an explicit choice here has to clear
        # the variable the subprocess inherits or it would be *added to*
        # the environment's list. Resolving the selection in one place is
        # worth the one mutation; a cleaner fix is an env override on
        # remote_instrument(), which lives in a module this change does not
        # own.
        os.environ[_PLUGINS_ENV_VAR] = ""
        return tuple(chosen)
    return tuple(
        name.strip()
        for name in os.environ.get(_PLUGINS_ENV_VAR, "").split(",")
        if name.strip()
    )


def main(argv: list[str] | None = None) -> None:
    """
    Open a napari window with the live instrument dock widget.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments, or None to read ``sys.argv``.

    Raises
    ------
    SystemExit
        If the device server serves no scanner. A detector-only server
        is a supported thing to connect to, just not with this viewer.
    """
    args = _parse_args(argv)
    session = Session(
        args.session if args.session is not None else default_root(),
        operator=args.operator,
        sample=args.sample,
        notes=args.notes,
    )
    plugins = _hardware_plugins(args.plugin)
    with remote_instrument(backend=args.backend, plugin_names=plugins) as microscope:
        if microscope.scanner is None:
            # A detector-only device server is a supported thing to
            # connect to (see docs/vendor-support.md) - just not with
            # this viewer, whose whole layout is a live scan image.
            # Saying so beats an AttributeError three frames deep.
            msg = (
                "this device server serves no scanner, so there is no "
                "scan image to view. The live viewer needs one; a "
                "detector-only server can still be driven from a script."
            )
            raise SystemExit(msg)
        viewer = napari.Viewer(title="miainwoodpecker")
        widget = LiveInstrumentWidget(
            viewer,
            microscope.scanner,
            camera=microscope.ronchigram_camera,
        )
        widget.set_session(session)
        viewer.window.add_dock_widget(widget, area="right", name="Instrument")
        # No explicit widget.shutdown() after this: closeEvent already calls
        # it once the window closes (part of Qt's app-quit teardown), and
        # calling it again here hits an already-destroyed Qt object.
        napari.run()


if __name__ == "__main__":
    main()
