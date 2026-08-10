"""
Validate this project's own NeXus output against the NXem NXDL schema.

docs/migration-plan.md §5's Phase 3 left this open: files declared
``definition = "NXem"`` to state intent, but the claim was unverified,
because the NeXus/FAIRmat spec sites are unreachable from this
environment. They still are - but ``pynxtools`` is on PyPI, PyPI *is*
reachable, and ``pynxtools`` ships the NXDL definitions inside the
package, so the schema can be checked entirely offline. The Phase 3 note
also said where this belongs: a CI validation step, not the runtime. This
script is that step; ``pynxtools`` appears only in the ``validate`` hatch
environment and is never imported by the shipped package.

What it checks, and why each check exists
-----------------------------------------
1. **A default recording claims no application definition.** Measured
   result: a plain recording is *not* valid NXem, and the entire gap is
   ``NXem``'s required ``sampleID`` group - an ``NXsample`` carrying
   ``is_simulation``, ``preparation_date``, and ``atom_types``. Those are
   facts about the physical specimen that no device API can supply, so the
   writer declines to claim NXem rather than fabricating them.
2. **A recording given real specimen metadata *is* valid NXem.** This is
   the positive check: it proves everything else the writer emits - the
   ``NX_class`` hierarchy, ``definition``, ``start_time``, the
   ``signal``/``axes`` hints, the ``units`` attributes - already satisfies
   the schema, so the only thing standing between this project and a
   verifiable NXem claim is operator-supplied metadata. It writes the
   session context too (``NXuser`` at ``/entry/user``, ``NXnote`` at
   ``/entry/notes``), since that is what a real recording carries. Both are
   *optional* in NXem - measured from the NXDL, where ``userID``/``noteID``
   are ``minOccurs="0"`` while ``sampleID`` is ``minOccurs="1"`` - so only
   ``sampleID`` was ever a required-field failure.
3. **A deliberately-bad file is reported invalid.** Without this the suite
   could pass vacuously: if a ``pynxtools`` upgrade, an import change, or a
   plumbing mistake made the validator stop finding problems, checks 1 and
   2 alone would go quiet and CI would go green on an unvalidated file.
   This check declares ``NXem`` *without* the sample group and requires the
   validator to say so.

Exits non-zero if any check fails, which is the point: ``pynx validate``
itself exits 0 even when it prints "is NOT valid", so a CI step built on
the bare CLI would pass silently forever.

Run with: hatch run validate:schema
"""

from __future__ import annotations

import datetime
import sys
import tempfile
from pathlib import Path

import h5py
import numpy as np
from pynxtools.dataconverter.validation import validate_hdf_group_against

from miainwoodpecker.devices.interface import Frame
from miainwoodpecker.storage import NexusWriter

_APPDEF = "NXem"
# NXem's required sampleID fields, per `pynx inspect-appdef NXem`. Real
# values for a real specimen would come from the operator or an ELN; these
# describe the synthetic frames this script generates, and are only ever
# written to a throwaway file.
_SYNTHETIC_SAMPLE = {
    "is_simulation": True,
    "preparation_date": "2026-01-01T00:00:00+00:00",
    "atom_types": "Si,O",
}
# Session context, which NXem documents at entry level but does *not*
# require (`userID`/`noteID` are minOccurs="0", unlike `sampleID`'s
# minOccurs="1"). Included in the positive check anyway, because this is
# what miainwoodpecker.storage.session will hand the writer, and the point
# of the check is that a real file validates - not a minimal one.
_SYNTHETIC_USER = {"name": "A. Operator", "affiliation": "SuperSTEM"}
_SYNTHETIC_NOTES = "synthetic frames written by the schema validation step"


def _frames() -> list[Frame]:
    """
    Return a small synthetic frame stack to write.

    Synthetic rather than simulator-acquired on purpose: this checks the
    *file layout* against a schema, which does not depend on pixel values,
    and it keeps the validation job free of the ``device`` extra.

    Returns
    -------
    list[Frame]
        Three frames with a scan-like ``fov_nm`` so the calibrated-axis
        path is the one exercised.
    """
    rng = np.random.default_rng(0)
    return [
        Frame(
            data=rng.standard_normal((32, 48)),
            timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
            + datetime.timedelta(seconds=index),
            metadata={"index": index, "fov_nm": 100.0},
        )
        for index in range(3)
    ]


def _is_valid(path: Path) -> bool:
    """
    Report whether a file's ``/entry`` validates against NXem.

    Parameters
    ----------
    path : Path
        The HDF5 file to validate.

    Returns
    -------
    bool
        True if the entry conforms to the NXem application definition.
    """
    with h5py.File(path, "r") as handle:
        return validate_hdf_group_against(_APPDEF, handle["entry"], str(path))


def _check_default_makes_no_claim(directory: Path) -> str | None:
    """
    Check a default recording declares no application definition.

    Parameters
    ----------
    directory : Path
        Scratch directory to write into.

    Returns
    -------
    str | None
        A failure description, or None if the check passed.
    """
    path = directory / "default.nxs"
    with NexusWriter(path, title="default recording") as writer:
        for frame in _frames():
            writer.append(frame)
    with h5py.File(path, "r") as handle:
        if "definition" in handle["entry"]:
            claimed = handle["entry/definition"][()]
            return (
                f"a default recording declares definition={claimed!r}; it must "
                f"claim nothing unless it can satisfy that definition"
            )
    return None


def _check_with_sample_is_valid(directory: Path) -> str | None:
    """
    Check a recording with full session context validates as NXem.

    Parameters
    ----------
    directory : Path
        Scratch directory to write into.

    Returns
    -------
    str | None
        A failure description, or None if the check passed.
    """
    path = directory / "nxem.nxs"
    with NexusWriter(
        path,
        title="NXem recording",
        definition=_APPDEF,
        sample=_SYNTHETIC_SAMPLE,
        user=_SYNTHETIC_USER,
        notes=_SYNTHETIC_NOTES,
    ) as writer:
        for frame in _frames():
            writer.append(frame)
    if not _is_valid(path):
        return (
            f"a recording written with definition={_APPDEF!r}, full specimen "
            f"metadata, and session context does not validate; see the "
            f"reported problems above"
        )
    return None


def _check_validator_still_detects_a_bad_file(directory: Path) -> str | None:
    """
    Check the validator still rejects an NXem claim with no sample group.

    Parameters
    ----------
    directory : Path
        Scratch directory to write into.

    Returns
    -------
    str | None
        A failure description, or None if the check passed.
    """
    path = directory / "false-claim.nxs"
    with NexusWriter(path, title="false claim", definition=_APPDEF) as writer:
        for frame in _frames():
            writer.append(frame)
    if _is_valid(path):
        return (
            f"a file claiming {_APPDEF!r} with no NXsample group validated; the "
            f"validator is not detecting missing required groups, so the other "
            f"checks here prove nothing"
        )
    return None


def main() -> int:
    """
    Run every schema check and report.

    Returns
    -------
    int
        0 if all checks passed, 1 otherwise.
    """
    checks = (
        ("default recording claims no definition", _check_default_makes_no_claim),
        (f"recording with specimen metadata is valid {_APPDEF}",
         _check_with_sample_is_valid),
        (f"validator still rejects a bare {_APPDEF} claim",
         _check_validator_still_detects_a_bad_file),
    )
    failures = []
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch)
        for description, check in checks:
            print(f"\n--- {description}")
            failure = check(directory)
            if failure is None:
                print(f"    PASS: {description}")
            else:
                print(f"    FAIL: {failure}")
                failures.append(failure)

    print()
    if failures:
        print(f"{len(failures)} of {len(checks)} schema checks FAILED")
        return 1
    print(f"all {len(checks)} schema checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
