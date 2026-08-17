# Idea bin

Things worth looking at that nobody has committed to yet.

This file exists so that a reference mentioned in passing does not have
to be either acted on immediately or lost. Nothing here is a commitment
or a design — the migration plan and the vendor support notes are where
decisions live. An entry graduates out of this file when someone has
acted on it.

Each entry says **how much of it was actually examined**, because "read
the transcript" and "watched the demonstration" support different
claims, and a note that blurs the two invites its reader to trust more
than was checked.

## Scan patterns beyond the raster

**Source:** ["Off the grid: Advanced scanning strategies for modern
STEM"](https://www.youtube.com/watch?v=Tf_oR3L1ans) (57 min) and
["Extremely dose-efficient EELS spectrum image acquisition with Gatan
eaSI"](https://www.youtube.com/watch?v=GaNe-x4sydY) (61 min), suggested
by @msarahan 2026-08-17.
**Examined:** auto-caption transcripts of both, read by keyword rather
than end to end, plus the slides of the first — extracted by ffmpeg
scene detection (51 unique slides over 57 minutes) and read where the
transcript said something interesting was on screen. The second talk's
slides have *not* been looked at, so anything shown only visually there
is still missing.

The first talk is a direct survey of the gap this project already has.
Conventional raster scanning — across a row, then fly back — is
presented as a limitation rather than a given, and the alternatives
compared are:

- **spiral** scans (spiral-in and spiral-out), which avoid flyback
  entirely,
- **skipped-pixel** scans (every *n*-th pixel, e.g. n=2, as a two-pass
  scheme),
- **random** ordering,
- **sequential** variants described as "low-end" and "high-end" by probe
  jump distance,
- **interleaved multipass**, visiting different subsets per pass.

Its own taxonomy slide ("Smart Scanning", 14:56) is worth adopting
wholesale, because it splits the space along the axis that matters for
an interface rather than by name:

- **Continuous scans** — spiral, serpentine — whose point is avoiding
  flyback line times. The trajectory is a path.
- **Pixel jumps** — random, sequential — whose point is *dose
  time-distribution* and controlling probe effects on neighbouring
  areas. The trajectory is a visiting order over a grid.

and lists what the trajectory actually changes: dose and damage, probe
settling times, flyback, drift artefacts, reconstruction stability, and
beam-induced artefacts. Its stated raster limitations are line flyback,
**uneven temporal distribution**, and **probe overlap (4D-STEM)** — that
last one being specific to the acquisition this project just built. The
slide's thumbnails are dose-time maps: the same field coloured by *when*
each part was visited, which is the quantity the pixel-jump family
exists to control.

The comparison is on **drift** and on artefacts: spirals and small-jump
sequential orderings showed better drift behaviour, while random
ordering and large probe jumps were worse, and some patterns left
visible discontinuities in the ADF image. The stated motivation is
beam-sensitive material and low-dose ptychography. Custom patterns are
reached through **scripting** — Python inside Gatan — rather than a
fixed menu, which is a design answer worth noting on its own: the
vendor's own conclusion was that the pattern set should be open.

The eaSI talk adds two things this project has no vocabulary for:

- **Hardware-synchronised subpixel scanning** through DigiScan, at the
  detector's full speed. This is the concrete instance of the
  detector-mastered synchronisation `ScanPass.scan_sync` already has a
  name for (`SCAN_SYNC_DETECTOR`), and confirms that a real
  implementation will need the trigger wiring rather than a software
  loop.
- **Dose fractionation by multipass.** Rather than one long dwell, the
  same region is scanned many times at low dose and accumulated —
  quoted as ~6.7 s per pass, 100 passes, with the same dose per frame as
  a single low-dose experiment but far higher total dose. Dual EELS is
  described as near-mandatory for the ultra-low-dose case.

One slide (22:00, "STEM-SI Custom Scan" on DyScO₃) is the intersection
of everything above and everything this branch just built: a **spectrum
image acquired on a non-raster trajectory**, spiral and scripted side by
side, each yielding simultaneous ADF and EELS. Its parameters are worth
recording because they are the shape a real call would take —
K3 single EELS at 0.9 eV/channel, 48 pA at 300 kV, ~3000 spectra/s
(340 µs per SI pixel), **scripted = 2 passes, spiral = 5 passes**, no
drift correction and no sub-scanning. Two details follow from it:
the pass count is a property of the *pattern*, not a global setting; and
the spiral's usable spectrum image is a small square inset in the
scanned field, because a spiral inscribes in a circle and a rectangular
SI has to fit inside it.

### What this means here, concretely

`ScanParameters` is a height, a width, a dwell and a field of view, and
`SynchronisedScanner.scan_synchronised` takes exactly that. **Every
pattern above is inexpressible in it.** A spiral has no rows; a skipped
scan has a different visiting order over the same grid; multipass has a
pass count and an accumulation rule. Closing this is not a matter of
adding a `pattern="spiral"` enum either — the visiting *order* has to
reach the device, and the storage layer has to record which order was
used or a reader cannot undo it.

The taxonomy also suggests the interface split. A *continuous* scan
needs a path the device follows; a *pixel jump* scan needs an ordering
over the existing grid. Those are different enough that one
`pattern=` enum covering both would be a name standing in for two
unrelated things — and the vendor's own answer, that custom patterns are
reached by scripting, is evidence that the set should not be closed at
all.

Multipass is the nearer of the two, and interacts with something already
built: a `ScanPass` is one traversal, so a hundred accumulated passes
are either a hundred `ScanPass` objects the caller sums, or a new
concept above it. That choice should be made deliberately rather than
discovered.

## Linked views over one dataset

**Source:** ["Spectrum Imaging Picker and Slice
Tool"](https://www.youtube.com/watch?v=XVggvbaEYCQ) (5 min) and
["eaSI EELS"](https://www.youtube.com/watch?v=AZXnFnmCeW0) (57 s),
suggested by @msarahan 2026-08-17.
**Examined:** full transcript of the first; the second is silent, so
four frames were sampled from it and read. Both are DigitalMicrograph.

The first demonstrates two tools that are **each other's inverse**, and
the workflow is the pair used together:

**Picker — space selects energy.** Right-click the spectrum image,
choose the SI picker, then drag to define a region: one pixel or many.
The pane below shows the summed spectrum over that region. Grabbing the
picker's *edge* drags the region around the map, and the spectrum
updates live — the demonstration walks it from a silicon-oxide region
(O-K and Si-K edges) across a tungsten plug to a copper-bearing region,
reading the chemistry off the changing spectrum as it moves.

**Slice — energy selects space.** A `Slice` palette with two sliders,
one for slice *position* and one for slice *width*. The 2D image is the
spectral intensity summed over that energy window, so dragging the
position slider sweeps the displayed map through energy. A `show range`
checkbox draws the window onto the spectrum itself, and the window can
be dragged directly on the spectrum instead of via the sliders.

**The combined loop is the actual technique.** Sweep the slice through
energy watching the map for regions that brighten; when something lights
up, move the *picker* there and read which edge it was — cobalt L,
copper. Neither tool alone finds anything; the round trip does. It is
also a *survey* technique, explicitly for orienting yourself in a
dataset before analysing it.

The 57-second clip shows what this looks like at full tilt: one dataset
driving a row of simultaneously linked views — survey image, several
single-element maps, a false-colour map, a three-colour composite, a
denoised copy — with dual EELS spectra (low-loss and core-loss) below,
each carrying its own picker marker. The left panel carries `Slice` and
`Time Slice`, the latter presumably the multipass axis from the eaSI
work above.

One line in its log is worth more than the rest of the frame:

```
Picker '1' on [Li:EELS HL denoised]
  found valid child image [S:[1] Spectrum of EELS HL denoised].
  Linkage reestablished.
```

The linkage is a **named, first-class, persistent object** — it has an
identity, it survives being broken, and it is re-established rather than
rebuilt. The views are not redrawn from a shared variable; they are
bound.

### What this means here, concretely

The precondition is already met and unused. A stored `ScanPass` holds
signals that share probe positions *by construction* — image channels, a
4D diffraction cube, spectrum images, one `pass_id`, one `scan_sync`
recording how that was guaranteed. That is exactly what a picker and a
slice need underneath them, and no other part of this project has ever
had it.

What is missing is the whole of the reading side: **nothing in the
viewer opens a pass at all.** The Recordings list does not understand
one (it reports a stored pass as `0 frames`), and there is no reader
that turns a pass back into linked napari layers.

Two design questions to settle before building any of it, both raised by
the DM log line above rather than by us:

1. **Is a link an object or a callback?** DM's answer is an object with
   an identity that can be re-established. napari's is closer to event
   subscription. Choosing the weaker one because it is easier to write
   is how "why did my picker stop following the spectrum" becomes
   unanswerable.
2. **Where does a derived view live?** The clip shows denoised copies,
   elemental maps and composites as peers of the raw signal, all linked.
   This project has deliberately kept analysis outside the viewer
   (`docs/analysis-parity.md`), so a linked derived view is either a new
   category or a reason to revisit that boundary.

## Tooling note

The transcripts and frames above came from the `watch` plugin
(`watch@claude-video`). Two things to know before using it again:

- A plugin installed mid-session is not visible to that session; Claude
  Code loads plugins at start, and `/reload-skills` does not pick up new
  plugins.
- Its ffmpeg keyframe path passes `-vsync`, which **ffmpeg 8 removed**,
  so frame extraction fails on an up-to-date Homebrew ffmpeg. Extracting
  frames directly with `ffmpeg -ss <t> -i <file> -frames:v 1` works.
  Note also that `--detail transcript` downloads audio only, so a frame
  pass needs the video fetched separately.
