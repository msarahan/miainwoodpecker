"""
Layout and default-value constants shared by the panels and the widget.

Here rather than in either, because ``live.py`` and the panel modules
both need them and an import in either direction between those two would
be a cycle.
"""

_SCAN_SIZES = (128, 256, 512)
_DEFAULT_SCAN_SIZE_INDEX = 1  # 256
_DEFAULT_DWELL_US = 1.0
_DEFAULT_FOV_NM = 15.0
_DEFAULT_RECORD_FRAME_COUNT = 10
_MAX_RECORD_FRAME_COUNT = 100000
_NO_SESSION_MESSAGE = "no session - data is not being kept"
_NOTES_HEIGHT_PX = 64
_CONTEXT_SAVE_DELAY_MS = 750
# Bounds for an acquired image's exposure. Wide on purpose: the range a
# detector will actually accept is the detector's business, and it
# refuses in configure(). These only keep the spin box from offering
# zero (which CameraParameters rejects) or a value no run would finish.
# Beam positions along the longer side of a spectrum image's region (or
# per side of the whole field of view, when no region is marked). The
# pass runs on its own thread, so the ceiling is about the dataset a
# session can hold rather than about the window: 512 positions across
# an EEL spectrometer's 1340 channels is 1.4 GB of float32 at 512x512.
# The default is sized to finish in seconds against the paced preview.
_MIN_POSITIONS = 2
_MAX_POSITIONS = 512
_DEFAULT_POSITIONS = 16
_MIN_EXPOSURE_MS = 0.01
_MAX_EXPOSURE_MS = 600000.0
_EXPOSURE_DECIMALS = 2
