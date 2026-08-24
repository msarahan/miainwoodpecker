"""
Watching a long acquisition build, instead of waiting for the file.

A spectrum image over a 64x64 grid at a realistic dwell takes minutes.
Until now the window showed a status line for the whole of it and the
data only when it was finished — so the operator could not tell a pass
that was working from one that was drifting, contaminating, or scanning
empty vacuum, and the only way to find out was to spend the whole
acquisition and look. Every instrument this replaces shows the map
filling in as the probe goes.

The sampling this needs is already the shape of the live view:
acquisition runs flat out on its own thread and the screen samples it
periodically. What was missing was anything to sample.

Teeing the writes, rather than changing the device interface
------------------------------------------------------------
``Scanner.scan_synchronised`` takes an ``into`` mapping of destinations
and is documented to write *through* them position by position, so the
cube lands on disk as it is acquired. The contract that documentation
states is deliberately narrow — a destination is defined by "a shape,
and assignment at a beam position" so that an ``h5py`` dataset satisfies
it — and that narrowness is what this module exploits.

:class:`PassPreview` wraps a destination and is one: it has a shape, and
it takes an assignment. It passes each write straight through to the real
destination and, on the way, reduces the value to a single number and
records it in a small in-memory map. So the progress image is a *virtual
detector image*, formed exactly as one is formed offline — the signal
summed at each probe position — and it costs no device change at all.
Any adapter honouring the documented contract gets this for free, which
is why it is done here rather than by adding a progress callback that
every future adapter would have to remember to fire.

Two views of one pass, from the same tee
-----------------------------------------
The map is a *reduction*: one number per beam position, which is what
makes a whole spectrum image watchable in a panel a few hundred pixels
wide. It is also the only thing it can be, and it throws away the axis
the acquisition exists to measure — a spectrum image whose every
position summed to a plausible number would look perfectly healthy on it
with the spectrometer parked off the edge entirely.

So the tee keeps the last 1D readout as well (:attr:`PassPreview.
latest_spectrum`), which the display draws as a curve beside the map.
Same write, same thread, no device change: one is where the probe has
been, the other is what it is seeing now.

The reduction is cheap on purpose
---------------------------------
At 2250 positions a second a full sum over a 512x512 diffraction pattern
would be a quarter of a million adds per position and would start to
matter. The summary therefore reads a strided subsample of anything
large (:data:`_MAX_SUMMARY_SAMPLES`), which is the right trade for a
*preview*: it is being drawn a few hundred pixels wide to answer "is
this working", not measured. What lands on disk is untouched by any of
it — the write through happens first and in full.

Threading
---------
The worker thread writes; the GUI thread reads the map to draw it. That
is deliberately unsynchronised. The map is a preallocated NumPy array of
floats and the only conflict possible is reading a position at the
instant it is written, which yields either the old value or the new one
and is invisible in a preview being redrawn sixty times a second. A lock
here would put the GUI thread in the acquisition's path, which is the one
thing this whole design exists to avoid.
"""

from __future__ import annotations

import typing

import numpy as np

if typing.TYPE_CHECKING:
    import numpy.typing as npt

#: Values sampled from one beam position's readout to summarise it. A
#: whole 512x512 pattern is 262144 numbers; this reads a strided ~4096 of
#: them, which is plenty to form a recognisable virtual-detector image
#: and cheap enough not to slow a fast pass down.
_MAX_SUMMARY_SAMPLES = 4096

#: The rank of a readout this keeps a copy of, as
#: :attr:`PassPreview.latest_spectrum`. One: a spectrum is a few
#: kilobytes and copying it per beam position costs a memcpy nobody can
#: measure, where a 512x512 diffraction pattern is a megabyte and doing
#: the same would be a gigabyte a second of copying to feed a preview.
#: The 2D case already has its display — that is what the map is.
_SPECTRUM_RANK = 1


class PassPreview:
    """
    A pass destination that also builds a live map of what has arrived.

    Wraps the destination ``scan_synchronised`` was going to be given,
    forwards every write to it unchanged, and keeps a summary of each
    beam position so the acquisition can be watched while it runs.

    Parameters
    ----------
    destination : object
        The real destination — an ``h5py`` dataset, or an array. Must
        support ``shape`` and assignment at a beam position.
    """

    def __init__(self, destination: object) -> None:
        self._destination = destination
        shape = tuple(int(size) for size in destination.shape)
        self._grid = shape[:2]
        # float32 and preallocated: the GUI thread reads this while the
        # worker writes it, so it must never be reallocated underneath.
        self._map: npt.NDArray[np.float32] = np.zeros(self._grid, dtype=np.float32)
        self._written = 0
        # Range over the positions actually visited, kept as they arrive.
        # Stretching the display over the whole map instead would stretch
        # it over the zeros the probe has not reached yet, which are the
        # majority early on and would drive everything real to white.
        self._low = float("inf")
        self._high = float("-inf")
        # The most recent 1D readout and the position it was written at,
        # as **one tuple**. Two attributes would be assigned one after
        # the other, and the GUI thread reading between the two would
        # get a spectrum labelled with the previous position - a caption
        # off by one beam position, which is worse than no caption. One
        # assignment of one object cannot be read half-done.
        self._latest: tuple[tuple[int, ...], npt.NDArray[np.float32]] | None = None

    @property
    def limits(self) -> tuple[float, float] | None:
        """
        The range of the summaries recorded so far.

        Returns
        -------
        tuple[float, float] | None
            Low and high, or None while nothing has been written or
            everything written is identical — in both of which cases
            there is no range to stretch a display over.
        """
        if self._high <= self._low:
            return None
        return (self._low, self._high)

    @property
    def shape(self) -> tuple[int, ...]:
        """
        The real destination's shape, which is what the device checks.

        **Asked of the destination every time**, because that is what an
        adapter validating against it needs — and so this is only
        answerable while the destination is open. A display drawing
        after the pass has finished has a closed HDF5 dataset behind it
        and gets an exception; :attr:`grid` is the question it wants.

        Returns
        -------
        tuple[int, ...]
            The wrapped destination's shape, unchanged.
        """
        return tuple(int(size) for size in self._destination.shape)

    @property
    def grid(self) -> tuple[int, ...]:
        """
        The beam positions this pass covers, as rows and columns.

        Read once when the destination was wrapped and kept, so it can
        be asked after the file is closed — which the display does,
        drawing the finished acquisition one last time.

        Returns
        -------
        tuple[int, ...]
            The pass's ``(rows, columns)``.
        """
        return self._grid

    def __getattr__(self, name: str) -> object:
        """
        Forward anything not overridden here to the real destination.

        A destination is documented as needing only a shape and
        assignment, but an adapter may reasonably ask it more —
        ``dtype`` and ``ndim`` both came up the first time this ran
        against the real writer. Implementing them one at a time as each
        surfaced would make this class a list of guesses about every
        adapter; forwarding makes it a proxy, which is what it is.

        Parameters
        ----------
        name : str
            The attribute being looked up.

        Returns
        -------
        object
            The wrapped destination's attribute.

        Raises
        ------
        AttributeError
            For private names, so a partially constructed instance
            cannot recurse through here looking for its own internals.
        """
        if name.startswith("_"):
            msg = f"{type(self).__name__!r} object has no attribute {name!r}"
            raise AttributeError(msg)
        return getattr(self._destination, name)

    def __getitem__(self, key: object) -> object:
        """
        Read straight from the real destination.

        Parameters
        ----------
        key : object
            Whatever the caller is indexing with.

        Returns
        -------
        object
            The wrapped destination's data there.
        """
        return self._destination[key]

    def __len__(self) -> int:
        """
        Return the destination's length.

        Returns
        -------
        int
            The first dimension's size.
        """
        return len(self._destination)

    @property
    def positions(self) -> int:
        """
        How many beam positions have been written so far.

        Returns
        -------
        int
            The count, for a progress line. Read without a lock; an int
            assignment is atomic and a preview that is one behind is not
            wrong about anything.
        """
        return self._written

    @property
    def total(self) -> int:
        """
        How many beam positions the pass will write in all.

        Returns
        -------
        int
            The product of the grid's two dimensions.
        """
        return int(self._grid[0] * self._grid[1])

    @property
    def latest_spectrum(self) -> tuple[tuple[int, ...], npt.NDArray[np.float32]] | None:
        """
        The last spectrum written, and the beam position it came from.

        The other half of watching a spectrum image build. The map says
        *where* the probe has been and how much signal it found there;
        this is what it actually saw at the position it is on now — the
        edge, the shape of the background, whether the spectrometer is
        even on the loss the operator set it to. Neither answers the
        other's question, and until this existed a pass could only be
        watched as a brightness.

        A copy, taken at write time, so what a display draws is one
        position's readout rather than a buffer an adapter may since
        have reused. Only for a 1D readout: see :data:`_SPECTRUM_RANK`.

        Returns
        -------
        tuple[tuple[int, ...], npt.NDArray[np.float32]] | None
            The beam position and its spectrum, or None when nothing 1D
            has been written yet — which is the whole of a 4D-STEM pass.
        """
        return self._latest

    @property
    def map(self) -> npt.NDArray[np.float32]:
        """
        The virtual-detector image built so far.

        Returns
        -------
        npt.NDArray[np.float32]
            One value per beam position, zero where the probe has not
            been yet. The same array every time, updated in place, so a
            caller may hand it to a viewer once and redraw it.
        """
        return self._map

    def __setitem__(self, key: object, value: object) -> None:
        """
        Write one beam position through, and record its summary.

        Parameters
        ----------
        key : object
            The beam position, as the device indexes it.
        value : object
            That position's readout — a spectrum, or a diffraction
            pattern.
        """
        # Through first, and unconditionally. What lands on disk must not
        # depend on anything this class does with it afterwards.
        self._destination[key] = value
        self._record(key, value)

    def _record(self, key: object, value: object) -> None:
        """
        Reduce one position's readout to a number and store it.

        Wrapped so a summary that cannot be formed — an unexpected key
        shape from an adapter indexing some other way — costs the preview
        and not the acquisition. The data is already written by the time
        this runs.

        Parameters
        ----------
        key : object
            The beam position the device wrote.
        value : object
            The readout written there.
        """
        try:
            if not isinstance(key, tuple) or len(key) != len(self._grid):
                return
            array = np.asarray(value)
            summary = _summarise(array)
            self._map[key] = summary
            self._low = min(self._low, summary)
            self._high = max(self._high, summary)
            if array.ndim == _SPECTRUM_RANK:
                self._latest = (key, np.array(array, dtype=np.float32))
        except (IndexError, TypeError, ValueError):
            return
        finally:
            self._written += 1


def _summarise(value: object) -> float:
    """
    Reduce one beam position's readout to a single number.

    The sum of the signal, which is what a virtual bright-field detector
    is — subsampled when the readout is large enough for the full sum to
    cost real time in a fast pass. Subsampling changes the map's absolute
    scale and not its structure, and the structure is the whole question
    a progress view answers.

    Parameters
    ----------
    value : object
        The readout at one beam position.

    Returns
    -------
    float
        Its summary, or 0.0 for something that will not reduce.
    """
    array = np.asarray(value)
    if array.size == 0:
        return 0.0
    flat = array.reshape(-1)
    if flat.size > _MAX_SUMMARY_SAMPLES:
        flat = flat[:: flat.size // _MAX_SUMMARY_SAMPLES]
    return float(flat.sum())


def previews(destinations: dict[str, object]) -> dict[str, PassPreview]:
    """
    Wrap every destination of a pass, keeping the mapping's shape.

    Parameters
    ----------
    destinations : dict[str, object]
        What the writer allocated, by target name.

    Returns
    -------
    dict[str, PassPreview]
        The same mapping, each destination wrapped. Hand this to
        ``scan_synchronised(into=...)`` in place of the original.
    """
    return {name: PassPreview(target) for name, target in destinations.items()}
