"""
Read legacy Nion Swift ``.ndata`` files so existing data is not orphaned.

An ``.ndata`` file is a zip containing ``data.npy`` and
``metadata.json``. Rather than re-implement that container, this uses
Nion's own ``NDataHandler`` (already present with the ``device`` extra) to
read it, and converts the result into the vendor-neutral
:class:`~miainwoodpecker.devices.interface.Frame` so it can be written
straight into the new NeXus format by
:mod:`miainwoodpecker.storage.nexus`.

Importing this module requires the ``device`` optional dependency group.
"""

from __future__ import annotations

import datetime
import pathlib
import typing

from nion.swift.model import NDataHandler

from miainwoodpecker.devices.interface import Frame

if typing.TYPE_CHECKING:
    import os
    from collections.abc import Iterator

# Swift records acquisition time under a few different keys depending on
# the version that wrote the file; try them in order of preference.
_TIMESTAMP_KEYS = ("created", "datetime_original", "timestamp")


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
        If the file contains no data array.
    """
    file_path = pathlib.Path(path)
    handler = NDataHandler.NDataHandler(file_path)
    try:
        data = handler.read_data()
        properties = dict(handler.read_properties())
    finally:
        handler.close()
    if data is None:
        msg = f"no data array found in {file_path}"
        raise ValueError(msg)
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
) -> Iterator[Frame]:
    """
    Yield frames for every ``.ndata`` file in a directory, sorted by path.

    Parameters
    ----------
    directory : os.PathLike[str] | str
        Directory to scan, e.g. an exported Swift library.
    recursive : bool
        Whether to search subdirectories.

    Yields
    ------
    Frame
        One frame per readable ``.ndata`` file.
    """
    root = pathlib.Path(directory)
    pattern = "**/*.ndata" if recursive else "*.ndata"
    for path in sorted(root.glob(pattern)):
        yield read_ndata(path)
