"""
Build the hosted download page for ``scripts/superstem_survey.py``.

The SuperSTEM team need the survey script on machines that have no git,
no GitHub account and no route to a package index, so
``docs/superstem-survey.md`` points them at a hosted page with a download
button instead. That page **embeds a copy of the script**, which is what
lets it work on an isolated control PC and is also the thing that can go
stale.

This builder is how it does not go stale silently. It reads the script,
HTML-escapes it into ``scripts/superstem_survey_page.html.in``, and
stamps the revision, line count and size into the page, so the bytes the
download hands over are the bytes in this repository by construction
rather than by anyone remembering to re-paste them.

What it cannot do is publish. **After running this, the generated file
has to be republished to the same artifact URL**, or the hosted page
keeps serving the previous revision. The page footer shows the revision
it was built from, which is how to tell.

Run with:
    python scripts/build_survey_page.py --out superstem-survey.html

Then republish that file to the artifact URL recorded in
``docs/superstem-survey.md``.
"""

from __future__ import annotations

import argparse
import html
import subprocess
import sys
import typing
from pathlib import Path

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "superstem_survey.py"
_TEMPLATE = _REPO / "scripts" / "superstem_survey_page.html.in"

_UNKNOWN_REVISION = "unknown"


def _revision(path: Path) -> str:
    """
    Return the short commit that last touched a file.

    Parameters
    ----------
    path : Path
        The file to describe.

    Returns
    -------
    str
        The abbreviated commit hash, or ``"unknown"`` outside a git
        checkout — a page built from an unknown revision is still a
        usable page, and refusing to build one would be worse than
        labelling it honestly.
    """
    try:
        finished = subprocess.run(  # noqa: S603
            ["git", "-C", str(_REPO), "log", "-1", "--format=%h", "--", str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return _UNKNOWN_REVISION
    return finished.stdout.strip() or _UNKNOWN_REVISION


def render(script: Path = _SCRIPT, template: Path = _TEMPLATE) -> str:
    """
    Render the page with the script embedded in it.

    Parameters
    ----------
    script : Path
        The survey script to embed.
    template : Path
        The page template carrying the substitution markers.

    Returns
    -------
    str
        The finished HTML.
    """
    source = script.read_text(encoding="utf-8")
    substitutions = {
        "__SCRIPT_SOURCE__": html.escape(source, quote=False),
        "__REV__": _revision(script),
        "__LINES__": str(source.count("\n") + 1),
        "__KIB__": str(round(len(source.encode()) / 1024)),
    }
    page = template.read_text(encoding="utf-8")
    for marker, value in substitutions.items():
        if marker not in page:
            message = f"template is missing the {marker} marker"
            raise ValueError(message)
        page = page.replace(marker, value)
    return page


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """
    Parse the script's command line.

    Parameters
    ----------
    argv : Sequence[str]
        Arguments after the program name.

    Returns
    -------
    argparse.Namespace
        With ``out``.
    """
    parser = argparse.ArgumentParser(description="Build the survey download page.")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("superstem-survey.html"),
        help="where to write the page (default: %(default)s)",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """
    Build the page.

    Parameters
    ----------
    argv : Sequence[str] | None
        Arguments after the program name, or None to read ``sys.argv``.

    Returns
    -------
    int
        Process exit status.
    """
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    page = render()
    arguments.out.write_text(page, encoding="utf-8")
    print(
        f"wrote {arguments.out} ({len(page.encode()) / 1024:.0f} KB) "
        f"from {_SCRIPT.name} at {_revision(_SCRIPT)}\n"
        f"republish it to the artifact URL in docs/superstem-survey.md",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
