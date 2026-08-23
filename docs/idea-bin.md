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
than end to end. No slides were viewed, so figures, numbers on plots and
anything said only visually are *not* captured here.

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

### What this means here, concretely

`ScanParameters` is a height, a width, a dwell and a field of view, and
`SynchronisedScanner.scan_synchronised` takes exactly that. **Every
pattern above is inexpressible in it.** A spiral has no rows; a skipped
scan has a different visiting order over the same grid; multipass has a
pass count and an accumulation rule. Closing this is not a matter of
adding a `pattern="spiral"` enum either — the visiting *order* has to
reach the device, and the storage layer has to record which order was
used or a reader cannot undo it.

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

## An MCP server driving a working microscope

**Source:** [`foundry-mcp/team05-mcp-server`](https://github.com/foundry-mcp/team05-mcp-server)
— MIT, from LBNL/NCEM (Ercius, Pattison, Wall, Ribet), the MCP layer for
the **TEAM 0.5** microscope: an FEI/Thermo column with a CEOS corrector,
TIA, Gatan DigitalMicrograph and the 4D Camera. Actively developed; last
commit 2026-08-18. Found 2026-08-23.
**Examined:** the repository cloned and read. All of `README.md`,
`CLAUDE.md` and `TEAM0.5_Parameters.md`; every tool signature and about
half the bodies of `mcp_library.py` (1,312 lines); the server loop, stage
handlers and focus-metric menu of `microscope_server.py` (1,500 lines);
the heads of `gatan_server.py` and `dm_scripts.py`; the tool list of
`mcp_ncempy.py`. **Nothing was run** — none of the vendor software, and
no hardware. Everything below is read off the source rather than
observed, so statements about *behaviour* are claims about what the code
says it does.

It is three PCs, ZeroMQ REQ/REP carrying pickled dicts between them, and
`fastmcp` over SSE at the top:

```
Claude → mcp_library.py (support PC, FastMCP)
           ├─ZMQ→ microscope_server.py  → TEMScripting COM, TIA, CEOS RPC
           └─ZMQ→ gatan_server.py       → writes .s scripts, runs DigitalMicrograph.exe
```

That is the same shape as this project — vendor-specific server
processes behind one neutral client — arrived at independently. The
reason is stated outright in their `CLAUDE.md`: the microscope PC runs
**WinPython 3.4.4**, and the file instructs contributors to keep that
server's code compatible with it. This is the strongest external
evidence yet that our subprocess isolation is a structural necessity
rather than a licensing convenience: the vendor PC is simply a different
Python, and no amount of packaging discipline makes that go away.

What they do *not* have is the vendor-neutral interface. `mcp_library.py`
is a hand-written passthrough, one function per command, with
`{'type': 'get_mag'}` dict literals inlined at every call site.

### Three things worth taking

**A facility's calibration tables, exposed as an MCP resource.**
`TEAM0.5_Parameters.md` is registered at `file://TEAM0.5_Parameters.md`
and contains what the operators actually need to look up: optimal lens
settings by accelerating voltage, **HAADF collection semi-angles per
camera length at 80/200/300 kV**, 4D Camera camera-length calibrations,
and STEM-rotation-versus-diffraction-offset values for DPC. That
semi-angle table is precisely the gap
[`scripting-and-automation.md`](scripting-and-automation.md) already
admits to — we tell users that eXSpy needs convergence and collection
semi-angles for quantification and that nothing we record supplies them.
Their table is the missing lookup, keyed on `(voltage, camera length)`,
both of which are values an instrument can be asked for. A
per-instrument calibration file is a small thing to add and would let
those land in metadata without an operator typing them.

**Dwell times are quantized, and ours pretend otherwise.** Their
parameters file records a flyback of 3.6 ms against a 60 Hz line
trigger, so usable dwell times satisfy `(d·pL + f)·n/60` — at 1k×1k the
good values are 13 µs or 29 µs, and the tabulated frame times step
17 s → 35 s → 52 s → 69 s. Choosing 14 µs instead of 13 doubles the
frame time for nothing. `ScanParameters.pixel_time_us` accepts any
float, so on real hardware a large fraction of the values a caller might
reasonably pick are silently the wrong side of a step.

**Handles, not payloads.** `mcp_ncempy.py` loads a file, keeps the array
server-side and returns an id; `calculate_image_statistics(file_id)`,
`retrieve_metadata(file_id)` and `plot_data_fft(file_id)` then operate
on the id. `acquire_image` likewise returns
`(path, calx, caly, unit, min, max, std)` and never the array itself.
This is the concrete answer to "how does an agent work with a 4D dataset
it cannot fit in a context window", and it is the convention any MCP
wrapper over our API should start from.

### Smaller things

- **`calculate_optimal_defocus`** — about ten lines, pure function, no
  hardware: convergence angle, reciprocal sampling and desired probe
  overlap in, defocus and step size for defocused ptychography out. The
  transferable idea is *computed advice as a tool*, so the agent is not
  doing probe geometry in its head. MIT, liftable as written.
- **Their tool list as a coverage checklist.** Controls they expose that
  our `InstrumentController` does not: STEM rotation angle,
  magnification, camera length and camera length index, convergence
  angle, beam tilt, diffraction shift, condenser stigmator, high
  tension, and **holder type** (single versus double tilt, which decides
  whether β tilt is legal at all). Not all of those belong in a neutral
  interface; STEM rotation and camera length are hard to argue against.
- **Bayesian autofocus.** `focus_stem_image` drives BEACON: upper
  confidence bound over C1 within ±range, *n* seed values then *n*
  samples, `normvar` as the focus metric, an explicit `noise_level`
  parameter documented as ~1e-4 for HAADF. The focus-metric menu in
  `microscope_server.py` (`std`, `normstd`, `var`, `normvar`,
  `roughness`, `varlaplace`) is a useful reference list. BEACON itself
  is reached through a hardcoded `sys.path.insert` into a local
  directory — it is not public and not obtainable from this repository.

### What it does not do, which is the other half of the value

- **Safety lives in docstrings, enforced by the model.**
  `move_stage_delta`'s docstring says the maximum value that should be
  allowed is 10 microns, and no code anywhere enforces it.
  `open_column_valve`, `close_column_valve` and `unblank_beam` are bare
  tools with no interlock. This is the clearest possible illustration of
  why [`scripting-and-automation.md`](scripting-and-automation.md)
  insists the dangerous paths must refuse rather than misbehave: a limit
  that exists only in prose holds only while the model chooses to
  honour it.
- **No arbitration — solved in hardware.** There is an NCEM "button
  pusher": a physical device, with `push_gatan_button()` and
  `push_tia_button()` tools, that presses a box to hand scan control
  between Gatan and TIA. That is the same contention the broker lease
  solves in software, and a useful picture of the alternative.
- **Transport.** The servers bind `tcp://*:<port>` and unpickle whatever
  arrives, with no authentication. We also use pickle, but over
  `localhost` with a `multiprocessing` `authkey`
  ([`devices/serving.py`](../src/miainwoodpecker/devices/serving.py)) —
  worth keeping that distinction explicit if anyone ever proposes
  widening our bind. Separately, ZMQ REQ/REP is strictly lockstep: their
  50 s receive timeout (commented "5 seconds") leaves the socket
  unusable once it fires, and a long 4D scan blocks the whole server
  with no way to cancel.
- **Agent-facing bugs that no test would catch.**
  `get_beam_tilt(tilt: tuple)` is a *getter* with a required argument it
  never uses, so a model must invent a value in order to read anything.
  `center_region` unpacks two values from `acquire_image`'s seven-tuple
  and sets `cenetered = True`, so the `centered` it checks stays `False`
  and the function always raises — it is dead, never called. Both are
  the failure mode of hand-writing a large tool surface.

### What this means here, concretely

Nothing above argues for changing the architecture; it independently
confirms it. Three things are actionable, in rough order of how cheaply
they close a gap we have already written down:

1. **A per-instrument calibration table.** Not a code change so much as
   a decision about where facility constants live and how they reach a
   file's metadata. The semi-angle case is already documented as a hole
   in EELS quantification, and the shape of the fix is now known.
2. **Dwell-time validation.** At minimum a function that, given a line
   length and a flyback time, says which nearby dwell times sit on a
   step boundary. Whether the flyback is discoverable from the device or
   has to be configured per instrument is the open question.
3. **The handle-not-payload convention**, for whenever an MCP server is
   actually written. Our doc already says that server is a thin wrapper;
   this is the first concrete evidence of what the *return* side of that
   wrapper has to look like.

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
