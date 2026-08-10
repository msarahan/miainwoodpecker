"""
Storage: write acquired frames to NeXus HDF5, read legacy Swift data.

Import the legacy ``.ndata`` reader from
:mod:`miainwoodpecker.storage.legacy` directly; it needs the ``device``
extra, so it is not re-exported here.
"""

from miainwoodpecker.storage.nexus import NexusWriter, read_series, write_frames

__all__ = [
    "NexusWriter",
    "read_series",
    "write_frames",
]
