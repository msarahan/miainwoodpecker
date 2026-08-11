"""
Read legacy Nion Swift ``.ndata`` files so existing data is not orphaned.

Read with the standard library, **deliberately not with Nion's own
``NDataHandler``**, and that is a license decision rather than a
preference. This module is part of the MIT application, and
docs/migration-plan.md §6's whole architecture rests on the invariant that
the MIT application never imports ``nion.*`` in-process — an in-process
import of a GPL-3.0 library is the case that section exists to avoid. An
earlier version of this module imported ``NDataHandler`` on the reasoning
that reusing a vendor reader beats re-implementing a container; correct in
general, and the wrong trade here, because it quietly breached the
boundary the rest of the project is built around.

Re-implementing is cheap because the format is documented and trivial.
``NDataHandler``'s own docstring: "ndata files are a zip file consisting of
data.npy file and a metadata.json file. Both files must be uncompressed."
Nion hand-rolls a zip parser because *writing* in-place needs byte offsets
for uncompressed members; reading needs none of that, so
:mod:`zipfile` plus :func:`numpy.load` and :func:`json.load` is the whole
implementation. Verified against genuine Nion-written files — the tests
build their fixtures with Nion's *writer* precisely so this reader is
exercised against the real container rather than against our own
assumptions about it.

Note this is not the "invent a bespoke format" that §3 warns against; it is
reading a documented one. RosettaSciIO would have been the natural reuse
candidate — §3 already names it for file I/O — but it has no ``.ndata``
reader (checked: 38 IO plugins, none handles the extension).

Frames come back as the vendor-neutral
:class:`~miainwoodpecker.devices.interface.Frame`, so they can be written
straight into the new NeXus format by :mod:`miainwoodpecker.storage.nexus`.

Unlike the rest of the legacy path's history, importing this module needs
**no** optional dependency group: it is now standard library plus numpy.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
import typing
import zipfile

import numpy as np

from miainwoodpecker.devices.interface import Frame

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Iterator

_DATA_MEMBER = "data.npy"
_METADATA_MEMBER = "metadata.json"

# Swift records acquisition time under a few different keys depending on
# the version that wrote the file; try them in order of preference.
_TIMESTAMP_KEYS = ("created", "datetime_original", "timestamp")


_LOGGER = logging.getLogger("miainwoodpecker.storage.legacy")


def _parse_timestamp(properties: typing.Mapping[str, typing.Any]) -> datetime.datetime:
    """
    Recover an aware UTC timestamp from Swift metadata, falling back to now.

    Parameters
    ----------
    properties : typing.Mapping[str, typing.Any]
        The ``metadata.json`` contents from the ``.ndata`` file.

    Returns
    -------
    datetime.datetime
        A timezone-aware timestamp; ``datetime.now`` if none was parseable.
    """
    for key in _TIMESTAMP_KEYS:
        raw = properties.get(key)
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.datetime.fromisoformat(raw)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            # Swift writes naive UTC timestamps.
            return parsed.replace(tzinfo=datetime.UTC)
        return parsed
    return datetime.datetime.now(tz=datetime.UTC)


def read_ndata(path: os.PathLike[str] | str) -> Frame:
    """
    Read one ``.ndata`` file into a vendor-neutral frame.

    Parameters
    ----------
    path : os.PathLike[str] | str
        The ``.ndata`` file to read.

    Returns
    -------
    Frame
        The array plus its recovered timestamp and original metadata. The
        metadata mapping additionally carries ``source_path``.

    Raises
    ------
    ValueError
        If the file is not a zip container, or contains no data array.
    """
    file_path = pathlib.Path(path)
    try:
        with zipfile.ZipFile(file_path) as archive:
            names = set(archive.namelist())
            if _DATA_MEMBER not in names:
                msg = f"no data array found in {file_path}"
                raise ValueError(msg)
            with archive.open(_DATA_MEMBER) as member:
                # allow_pickle stays at its default False: these are plain
                # numeric arrays, and a .ndata file is untrusted input as
                # far as this reader is concerned.
                data = np.load(member)
            properties: dict[str, typing.Any] = {}
            if _METADATA_MEMBER in names:
                # Swift has written .ndata files with no metadata member;
                # the array is still worth recovering.
                with archive.open(_METADATA_MEMBER) as member:
                    properties = dict(json.load(member))
    except zipfile.BadZipFile as error:
        msg = f"{file_path} is not a valid .ndata (zip) file"
        raise ValueError(msg) from error
    properties["source_path"] = str(file_path)
    return Frame(
        data=data,
        timestamp=_parse_timestamp(properties),
        metadata=properties,
    )


def iter_ndata_directory(
    directory: os.PathLike[str] | str,
    *,
    recursive: bool = True,
    skip_unreadable: bool = False,
) -> Iterator[Frame]:
    """
    Yield frames for every ``.ndata`` file in a directory, sorted by path.

    The migration path for a whole Swift library, so the failure mode
    matters more than it looks. By default one corrupt file still aborts
    the run, which is the right default — a migration that silently
    dropped data would be worse than one that stops and says why, and the
    caller can see exactly which file it stopped on.

    But "abort on the first bad file" is a poor answer for a ten-thousand
    file library where one member is truncated, and it contradicted this
    function's own promise of "one frame per *readable* file". Passing
    ``skip_unreadable=True`` makes that promise true: unreadable files are
    logged and skipped, and the rest of the library migrates.

    Parameters
    ----------
    directory : os.PathLike[str] | str
        Directory to scan, e.g. an exported Swift library.
    recursive : bool
        Whether to search subdirectories.
    skip_unreadable : bool
        Skip files that cannot be read instead of raising. Each skipped
        file is logged at ``WARNING`` with the reason, so a migration
        never drops data silently.

    Yields
    ------
    Frame
        One frame per ``.ndata`` file — per *readable* one when
        ``skip_unreadable`` is set.
    """
    root = pathlib.Path(directory)
    pattern = "**/*.ndata" if recursive else "*.ndata"
    for path in sorted(root.glob(pattern)):
        if not skip_unreadable:
            yield read_ndata(path)
            continue
        try:
            frame = read_ndata(path)
        except (ValueError, OSError) as error:
            _LOGGER.warning("skipping %s: %s", path, error)
            continue
        yield frame
