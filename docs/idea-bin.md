# Idea bin

Things worth looking at that nobody has evaluated yet.

This file exists so that a reference mentioned in passing does not have
to be either acted on immediately or lost. Nothing here is a commitment,
a design, or a recommendation — the migration plan and the vendor
support notes are where evaluated decisions live. An entry graduates out
of this file when someone has actually looked at it and written down
what they concluded.

**Entries are unevaluated unless they say otherwise.** Where an entry is
a link nobody here has opened, it says so, and it records who suggested
it and what they said it was about — because a link with a
second-hand summary attached is worse than a link with none: the summary
reads as knowledge and is really a guess.

## Scan patterns and strategies

Suggested by @msarahan, 2026-08-17, as "fun scan ideas". **Not yet
watched** — the descriptions below are the suggester's framing, not a
summary of the content.

- <https://www.youtube.com/watch?v=Tf_oR3L1ans>
- <https://www.youtube.com/watch?v=GaNe-x4sydY>

Relevant to a gap this project has already recorded: the scan interface
drives rectangular raster grids and nothing else.
:meth:`SynchronisedScanner.scan_synchronised` takes a
:class:`ScanParameters` — a height, a width and a field of view — so a
spiral, a random or low-discrepancy sampling, an adaptive or
sparse pattern, or a hand-drawn region has no way to be expressed.
Compressed-sensing and dose-limited work all live in that gap, and so
does the "other useful ways to position beam patterns than simple
rectangles" that was explicitly deferred when the pass concept landed.

## Interaction with linked multimodal data

Suggested by @msarahan, 2026-08-17, as "interaction examples with linked
multimodal data". **Not yet watched**, same caveat as above.

- <https://www.youtube.com/watch?v=AZXnFnmCeW0>
- <https://www.youtube.com/watch?v=XVggvbaEYCQ>

Relevant to what a stored :class:`ScanPass` now makes possible and
nothing yet uses. A pass holds several signals that share probe
positions by construction — image channels, a 4D diffraction cube,
spectrum images — which is exactly the precondition for linked views:
click a beam position in the survey image and see its diffraction
pattern, drag a virtual aperture in the diffraction plane and watch the
real-space image it forms, brush a region in one signal and have it
highlight in the others. The correlation is in the file
(`storage/passes.py`); the viewer does not yet read a pass back at all.
