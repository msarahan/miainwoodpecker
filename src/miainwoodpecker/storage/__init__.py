"""
Storage: write acquired frames to NeXus HDF5, read legacy Swift data.

The axis-calibration model in :mod:`miainwoodpecker.storage.calibration` is
re-exported here because it is part of the writer's public surface — a
caller says what kind of axes an acquisition has by passing one of these to
:class:`~miainwoodpecker.storage.nexus.NexusWriter`.

Import the legacy ``.ndata`` reader from
:mod:`miainwoodpecker.storage.legacy` directly; it needs the ``device``
extra, so it is not re-exported here.
"""

from miainwoodpecker.storage.calibration import (
    PIXELS,
    AxisCalibration,
    AxisKind,
    FrameCalibration,
)
from miainwoodpecker.storage.nexus import (
    NexusWriter,
    read_calibration,
    read_series,
    write_frames,
)
from miainwoodpecker.storage.spectra import (
    SpectrumWriter,
    read_spectra,
    write_spectra,
)

__all__ = [
    "PIXELS",
    "AxisCalibration",
    "AxisKind",
    "FrameCalibration",
    "NexusWriter",
    "SpectrumWriter",
    "read_calibration",
    "read_series",
    "read_spectra",
    "write_frames",
    "write_spectra",
]
