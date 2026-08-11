"""
The on-disk layout of the NeXus files this project writes, in one place.

:mod:`miainwoodpecker.storage.nexus` owns the layout, but it was not the
only module that *knew* it: the session layer opened files and hard-coded
``entry/...`` paths of its own, and all three Phase 4 analysis adapters
knew where the frame stack lives. Five modules encoding one format is the
start of the bespoke-format maintenance burden this project exists to
avoid (docs/migration-plan.md, §3) — a layout change would have needed
five coordinated edits, and the message telling an operator a recording
has no frames was byte-identical in four files with nothing keeping it
so.

Deliberately constants and one exception rather than a reader class. The
layout *is* a set of paths; wrapping it in machinery would add a thing to
learn without removing one. Anything that needs to read a recording
should use :func:`miainwoodpecker.storage.nexus.read_frames`, which is
the single reader these constants exist to support.

This module imports nothing from the rest of the package, so it can be
used from any layer without a cycle.
"""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    import os

ENTRY = "entry"
"""The single ``NXentry`` group every file this project writes has."""

DETECTOR_GROUP = "entry/instrument/detector"
"""
``NXdetector`` holding the frames as they are appended.

The durable half of the layout: it exists from the first
:meth:`~miainwoodpecker.storage.nexus.NexusWriter.append`, so it survives
a writer that was abandoned without finalizing. That is why the session
layer's frame count and the replay reader both come from here rather than
from :data:`NXDATA_GROUP`.
"""

DETECTOR_DATA = f"{DETECTOR_GROUP}/data"
"""The frame stack, ``(frames, height, width)``."""

DETECTOR_FRAME_TIME = f"{DETECTOR_GROUP}/frame_time"
"""Seconds elapsed since the first frame, one per frame."""

SOURCE_GROUP = "entry/instrument/source"
"""
``NXsource`` — the electron gun, written when a frame reported its voltage.

Everything else about the instrument's state lives in the per-frame JSON
of :data:`FRAME_METADATA`, because NeXus describes no home for it. The
accelerating voltage does have one, and a file that knew the value and
left the standard field empty would be hiding it from every reader that
speaks NeXus rather than this project.
"""

SOURCE_VOLTAGE = f"{SOURCE_GROUP}/voltage"
"""The accelerating voltage in volts, as ``NXsource``'s own field."""

NXDATA_GROUP = "entry/data"
"""
``NXdata`` carrying the plotting hints, created at ``close()``.

Created last because it needs the final frame shape for its axes, which
makes its *presence* the finalized flag the session layer reports — and
its absence the reason an unfinalized recording displays but cannot be
analyzed, since the analysis adapters read the stack through here.
"""

NXDATA_DATA = f"{NXDATA_GROUP}/data"
"""
The frame stack again, as a hard link rather than a copy.

Spelled absolute (``/entry/data/data``) where a library wants a dataset
path, notably LiberTEM's ``Context.load(..., ds_path=...)``.
"""

ABSOLUTE_NXDATA_DATA = f"/{NXDATA_DATA}"
""":data:`NXDATA_DATA` with a leading slash, for libraries that want one."""

NXDATA_FRAME_TIME = f"{NXDATA_GROUP}/frame_time"
"""The per-frame times, linked into ``NXdata`` as the navigation axis."""

METADATA_GROUP = "entry/metadata"
"""
``NXcollection`` for everything no NeXus base class describes.

Both metadata blobs live here rather than in :data:`DETECTOR_GROUP`,
because a detector's contents *are* specified and a file claiming
``NXem`` stops validating if unknown fields appear there.
"""

FRAME_METADATA = f"{METADATA_GROUP}/frame_metadata_json"
"""One JSON object per frame, in acquisition order."""

VENDOR_METADATA = f"{METADATA_GROUP}/vendor_metadata_json"
"""The first frame's metadata, as one JSON object."""

SAMPLE_GROUP = "entry/sample"
"""``NXsample`` — what was under the beam. Required by ``NXem``."""

USER_GROUP = "entry/user"
"""``NXuser`` — who ran the session."""

NOTES_GROUP = "entry/notes"
"""``NXnote`` — free text about the session and this recording."""

NOTES_FIELD = f"{NOTES_GROUP}/description"
"""The note text itself."""

AXIS_NAMES = ("y", "x")
"""
Frame axis dataset names inside :data:`NXDATA_GROUP`, slow axis first.

Matches the ``(height, width)`` convention the device layer pins and the
``axes = ["frame_time", "y", "x"]`` hint the writer emits.
"""


class NoFramesError(ValueError):
    """
    Raised when a recording holds no frames to read.

    A ``ValueError`` subclass because that is what every caller of these
    readers has always caught, and because "this file has nothing in it"
    genuinely is a bad value rather than a broken file — a hard-killed
    recording raises ``OSError`` from h5py instead, and the two want
    telling apart.

    Exists mainly so the message has one definition: it was written out
    by hand in four modules, which is three opportunities for them to
    drift apart while all claiming to describe the same on-disk state.
    """

    def __init__(self, path: os.PathLike[str] | str) -> None:
        super().__init__(
            f"{path} has no /{NXDATA_GROUP} group; it recorded no frames",
        )
