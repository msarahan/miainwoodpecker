"""
Rebuild .test_durations from the integration jobs' JUnit reports.

CI runs the integration suite as three jobs, each given a third of it by
pytest-split, and pytest-split divides by *time* using the durations in
.test_durations. This writes that file from measured times:

    gh run download <run-id> --pattern 'integration-junit-*' --dir junit
    python scripts/update_test_durations.py junit/*/junit-*.xml

Run it when the three integration jobs have drifted apart in length -
usually after a batch of new slow tests, which pytest-split can only
assume take the average.

JUnit rather than pytest-split's own ``--store-durations``: the suite
runs under pytest-xdist, and pytest's JUnit writer is the one place its
per-test times are reliably collected across workers. Each test's time
there is setup, call and teardown together, which is also what
pytest-split stores.
"""

from __future__ import annotations

import argparse
import json
import pathlib

# The reports are this project's own CI output, downloaded by the person
# running this, not input from anywhere untrusted.
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent
"""The repository root: node IDs, and the output file, are relative to it."""


def node_id(classname: str, name: str) -> str:
    """
    Return the pytest node ID a JUnit test case was reported under.

    JUnit flattens ``tests/unit/test_x.py::TestY::test_z`` to
    ``classname="tests.unit.test_x.TestY" name="test_z"``, so the module
    path and any class names are joined by the same dot. The split
    between them is found by asking the file system where the module is,
    longest match first.

    Parameters
    ----------
    classname : str
        The test case's ``classname`` attribute.
    name : str
        The test case's ``name`` attribute, parameters included.

    Returns
    -------
    str
        The node ID.

    Raises
    ------
    ValueError
        If no prefix of ``classname`` names a file in the repository.
    """
    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        module = pathlib.Path(*parts[:end]).with_suffix(".py")
        if (ROOT / module).is_file():
            return "::".join([module.as_posix(), *parts[end:], name])
    message = f"no test module in {ROOT} for classname {classname!r}"
    raise ValueError(message)


def main() -> None:
    """Merge the reports given on the command line into .test_durations."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("reports", nargs="+", type=pathlib.Path)
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=ROOT / ".test_durations",
        help="where to write the durations (default: .test_durations)",
    )
    arguments = parser.parse_args()

    durations: dict[str, float] = {}
    for report in arguments.reports:
        for case in ET.parse(report).iter("testcase"):  # noqa: S314 - see import
            classname = case.get("classname", "")
            if not classname:
                # A module skipped whole at import (importorskip) is
                # reported with its dotted path as the *name* and no
                # classname. It has no tests to schedule, so nothing to time.
                continue
            durations[node_id(classname, case.get("name", ""))] = float(
                case.get("time", "0"),
            )

    # Sorted, and one test per line, so a refresh is a readable diff.
    arguments.output.write_text(
        json.dumps(dict(sorted(durations.items())), indent=0) + "\n",
        encoding="utf-8",
    )
    total = sum(durations.values())
    print(f"wrote {len(durations)} tests, {total:.0f}s in all, to {arguments.output}")


if __name__ == "__main__":
    main()
