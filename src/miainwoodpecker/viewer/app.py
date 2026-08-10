"""
Entry point: the live viewer against the simulated microscope.

Run with ``miainwoodpecker-viewer`` (or
``uv run --extra device --extra viewer miainwoodpecker-viewer``).
Requires both the ``device`` and ``viewer`` optional dependency groups;
real-hardware sources will join once Phase 1's hardware validation lands.

A session is opened before the window appears, so the app can never be in
the state that blocked a Phase 5 pilot: running, acquiring, and unable to
keep anything. ``--session`` names the directory (reused if it already
exists, so restarting mid-shift resumes the same session); the default is
one directory per day under ``~/miainwoodpecker-data``. Operator, sample,
and notes can be given here or typed into the Session group at any point
during the run.

Deliberately imports ``miainwoodpecker.devices.remote``, not
``miainwoodpecker.devices.nion_server``: the running application talks to
the device server over IPC and never imports Nion's GPL-3.0 code directly
(see docs/migration-plan.md, §6).
"""

from __future__ import annotations

import argparse

import napari

from miainwoodpecker.devices.remote import remote_simulated_instrument
from miainwoodpecker.storage.session import Session, default_root
from miainwoodpecker.viewer.live import LiveInstrumentWidget


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """
    Open a napari window with the live instrument dock widget.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments, or None to read ``sys.argv``.
    """
    args = _parse_args(argv)
    session = Session(
        args.session if args.session is not None else default_root(),
        operator=args.operator,
        sample=args.sample,
        notes=args.notes,
    )
    with remote_simulated_instrument() as microscope:
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
