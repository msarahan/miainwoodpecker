# Change Log

## Unreleased

### Added

- **The instrument as an application in the notification area: right-click
  to open a window on it, to see how its device servers are doing, or to
  stop everything.** `pixi run serve` already holds a microscope open for
  whoever connects. What it also does is occupy a terminal and end when
  that terminal is closed — a poor fit for the machine it is meant to run
  on, where the instrument is served all day, the person at the console is
  not the person who started it, and "close the black window" is a thing
  that happens. `pixi run tray` (`miainwoodpecker-tray`) is the same
  session with somewhere to live: the broker outlives every window, and
  the things anybody needs are on a right-click — a window on the
  instrument, the browser dashboard on it, how its device servers are
  doing, and stopping the lot. The window and the dashboard are separate
  entries rather than a choice made at startup, because they are
  separate programs wanting separate environments — Qt in one, marimo
  and no Qt in the other — so `pixi run tray` runs three: the vendor
  stack in one, the window in another that demonstrably does not contain
  it, and marimo in a third that contains neither.
  **One of each kind, and the second click goes and finds it.** An entry
  pressed again because a window went behind a browser is not a request
  for two windows, and answering it with one is how a column ends up
  with four viewers on it by four o'clock — each with its own session
  directory, one of them the one somebody was recording into. So the
  entry reads "Show the viewer" once one is up, and does: on Windows,
  the window is found by process id and brought to the front; a
  dashboard is a page rather than a window, so its address is opened
  again and the browser focuses the tab that already has it. That
  address is read from what the process printed on its way up — nothing
  else knows the port marimo chose or the access token it minted, and a
  link without the token opens a login page. Where a platform has no way
  to raise another process's window, which is everywhere but Windows,
  the tray says a viewer is already open rather than pretending. Anyone
  who wants a second window still starts one by hand against the
  published invitation; what this declines to do is start one by
  accident. It publishes to
  `~/.miainwoodpecker` by default — beside the instrument configuration,
  where a notebook already looks when it is told nothing at all — because
  an instrument held open for people to join has to be findable, which is
  the same reason `--serve` refuses a temporary directory.
  **The broker is still a subprocess, and that is not an implementation
  detail.** Everything `miainwoodpecker.launcher` says about it applies
  unchanged: the vendor device stack is GPL-3.0 and lives in its own pixi
  environment, and parking the column depends on the broker exiting
  cleanly under a signal it handles rather than being killed. A tray icon
  that imported the device layer would give up both. So this process
  spawns, watches and asks — it never opens a device — and `--broker-env`
  and `--ui-env` still put the two children wherever they belong.
  **Quitting asks first, and the question is not a formality.** That one
  menu item ends everybody's session: a notebook halfway through a
  spectrum image and a dashboard on a wall are clients of this broker and
  neither gets a say. The confirmation therefore names what is running at
  the moment it is asked — read from the broker, not from what this
  process happens to have spawned — says where the instrument is
  published, and defaults to Cancel. Confirmed, it stops the windows,
  then the broker, and the column is parked, in that order, because a
  probe must not be parked out from under a window still driving it.
  **Health, and what it can honestly claim.** A broker over a configured
  microscope is a broker over several device servers — a Nion column, a
  DECTRIS ELA on the spectrometer, a camera server — and from outside
  them the only evidence that the spectrometer did not come up is a menu
  one item short. The health panel puts one row per device under the
  server that was supposed to bring it, from three things the broker
  already knows: `describe()`, whose per-target `error` is an adapter
  half-answering; `targets()`, whose `error` is a live loop that died
  mid-session and left a tile blank; and the instrument configuration,
  which is the only thing that knows which target came from which
  process. **Nothing polls the hardware, deliberately.** The obvious
  health check — ask each detector for a frame every few seconds — is a
  second caller on a device whose live loop may be mid-pass, which is the
  interleaving the broker exists to prevent, and it would need a lease or
  fail for want of one. So the strongest claim made anywhere in the panel
  is "the broker says it is there and nothing has reported it broken",
  and the wording sticks to it.
  **Split by what needs a display, so the interesting parts are testable
  without one.** `tray/session.py` supervises the broker and the windows
  and imports no Qt: the launcher's blocking sequence turned inside out,
  so `start()` returns immediately and `poll()` reports what changed —
  because the caller is an event loop that must keep drawing a menu while
  a cold device server spends tens of seconds importing a vendor stack.
  `tray/health.py` turns two broker reads into a per-server verdict and
  imports no Qt either. Both have unit tests that run on a machine with
  no display, against real child processes rather than fakes, since
  everything at stake is process lifetime. `tray/app.py` is the icon, and
  its integration tests drive a real tray over a real broker over the
  camera server's synthetic instrument.
  **The launcher gained `--config` on the way past.** The tray and the
  launcher build the broker's command line with one shared function, and
  the launcher could not previously start a configured instrument at all
  — so `pixi run instrument --config …` now works for the same reason the
  tray does.

- **A spectrometer's readout, displayed as a spectrum.** Every panel in
  this application was an array on a napari canvas, and a projecting
  detector's readout is not one: it is counts against energy, and the
  two things a display of it must do — put a number on a *y* axis and
  label the *x* axis in electronvolts — are the two an image layer
  cannot. Pushed into one, an EEL spectrum is a picture one pixel high.
  It never got that far. A napari layer is added with a
  `FrameCalibration`, which is exactly two axes, so the viewer's own
  helper raised `ValueError: not enough values to unpack` on the first
  rank-1 frame that reached it — putting a spectrometer into `projected`
  and starting it was a way to stop the live view.
  **`viewer/plots.py` is a pyqtgraph curve**, and
  `documents.PanelDocument` is what puts a plain widget into the same
  MDI area the napari viewers live in, so the spectrum is a panel like
  any other: it tiles, closes, raises and takes the View menu's "Fit
  panel to data" alongside the images. Which display a frame gets is
  decided by the **rank of the array and nothing else** — a camera's
  readout mode changes it between one frame and the next, so the shape
  is the fact where the detector's label would be a guess — and a camera
  switched between imaging and projecting keeps one window rather than
  accumulating two.
  **`axes.spectrum_axis` is the 1D answer to the question
  `axes.frame_calibration` cannot be asked.** It reads the dispersion
  from where a projecting detector already writes it, and reports
  anything that is *not* an energy axis as bare channels: one axis of
  counts against a real-space ruler is not a spectrum, and labelling it
  in electronvolts it never had would be worse than saying "channel".
  **The unit is the detector's, and is not re-prefixed.** pyqtgraph
  reads a unit as a base SI quantity and adds a prefix to keep tick
  numbers small, which is right for volts and wrong for two of the three
  spellings this project accepts for energy. A real monochromated EELS
  spectrum calibrated in meV came back labelled `energy (kmeV)` —
  kilo-milli-electronvolts — with its axis divided by a thousand to
  match. Auto-prefixing is off; an adapter converts units once, on the
  way in.
- **A spectrum image, watched in the dimension it is acquired in.** A
  pass already opened a panel showing a virtual-detector image as it
  filled, which is one number per beam position — and that is the whole
  of what it can be. A spectrometer parked off the edge of the loss, or
  a drift tube that never moved, produces a map indistinguishable from a
  good acquisition: every pixel sums to a plausible number. Minutes
  later there is a file, and the acquisition has to be done again.
  `PassPreview` now keeps the **last 1D readout written and the beam
  position it came from**, and a second panel draws it as a curve
  captioned `position 23, 17 of 64x64`. It costs no device change and no
  second write: the preview already tees `scan_synchronised`'s
  destinations, so this is the same write, on the same thread, reduced a
  second way. Rank 1 only — a spectrum is a few kilobytes and copying one
  per position is a memcpy nobody can measure, where a 512x512
  diffraction pattern is a megabyte and doing the same would be a
  gigabyte a second to feed a preview; a 4D pass therefore gets no
  spectrum panel, reported by the readout being absent rather than by the
  display second-guessing the mode the operator set.
  The copy is taken **at write time**, so what is drawn is one position's
  readout rather than whatever a device's scratch buffer holds by the
  time the display gets to it.

- **A live view that keeps up: 2 fps to 10, which is the ceiling of the
  thing it runs in.** The browser dashboard's fastest refresh option was
  `0.5s`, so two frames a second was not what the stack managed — it was
  the fastest an operator could ask for. Underneath it, two things were
  in the way, and only one of them was in the notebook.
  **`previews(max_edge)`, a watch verb that decimates before it sends.**
  `snapshot()` ships every target's pixels at full size, which is right
  for a viewer sharing a process with its broker and impossible over a
  socket at any rate worth calling live: on an instrument serving a
  2048×2048 camera beside a scan unit it is **19 MB a call**, so even the
  old one-second tick was 160 Mbit/s. `previews()` reads the same state
  and the same frames under the same lock — a tile still cannot show a
  rate from one pass beside pixels from another — and reduces the pixels
  in the process that holds the device. Measured on the simulator: 834 kB
  at a 256-pixel edge and 210 kB at 128, against 19 MB, and 1.1 ms a call
  against 24.5 ms.
  **What comes back is a `FramePreview`, not a `Frame`, on purpose.** A
  decimated frame is not a measurement and must not be able to pass as
  one. The specific trap is `metadata["calibration"]`, which is units
  *per pixel*: carried unchanged past a stride of 8 it claims a pixel
  size eight times too small, and every distance measured off it is wrong
  by that factor with nothing anywhere saying so. A preview therefore has
  no calibration to be wrong, records the `stride` it was reduced by and
  the `source_shape` it came from, and keeps the metadata that describes
  the *detector* rather than the grid — `channel_name` above all, which
  is what captions a multichannel tile. Look at previews; measure and
  record frames.
  **The decimation is one function, shared.** `devices/preview.py` holds
  it, and `dashboard.images` re-exports `decimate` from there rather than
  keeping a second copy: two implementations of "every nth pixel" would
  eventually disagree about the rounding, and a dashboard would then draw
  one picture in process and a different one over a socket. A
  parametrised test asserts the two routes produce **byte-identical**
  PNGs across six frame shapes and three tile sizes, odd sizes included.
  **The notebook offers the trade rather than choosing it.** Refresh now
  goes down to `0.1s`, and the tile edge is a control beside it (128,
  256, 512, 1024) that sets both what the broker sends and what the
  browser draws. Measured end to end against the simulator — display
  timer to new pixels on screen, three tiles — `0.1s` sustains 9.7 fps at
  128 px, 9.0 at 256 and 7.0 at 512.
  **10 fps is marimo's floor, not ours.** Its front end clamps a refresh
  interval to 0.1 s, so that is the ceiling of this mechanism however
  fast the kernel answers. Pushing from a running cell with
  `mo.output.replace` does reach 38 fps, and is not used: the cell holds
  the kernel for as long as it runs, so every other cell — Acquire
  included — is frozen until it stops. Measured: a button pressed two
  seconds into a twenty-second stream did not respond for thirty.
- **Every dashboard log entry says where its data is, and can put it
  somewhere if it is nowhere.** The session log recorded one path per
  acquisition and, for anything acquired with no session directory,
  nothing at all: the frames went into a thumbnail and were released, so
  the shot that turned out to matter could not be rescued. Now an entry
  carries one row per **signal**, each with its own picture, its own
  metadata and its own answer to "where is it" — a path if it was
  written, a **Save** button if it was not.
  **An entry is an item, and an item is several signals — one file
  each.** The unit an operator thinks in is not one array: it is a
  spectrum image *with* the survey that says where it was taken and the
  follow-up image that says what the beam did to the specimen while the
  map ran, or one scan pass read out on HAADF and BF at once.
  `Session.record_datasets` writes each of those to its own NeXus file,
  streaming all of them at once so nothing is buffered waiting for a
  sibling to finish, and the files of one item share a sequence number
  and timestamp — `0007-scan-haadf-…nxs` beside `0007-scan-bf-…nxs`.
  Separate files rather than one combined entry, which the format would
  allow (`storage/passes.py` does it for a pass, whose signals are
  pixel-aligned by construction): an item's parts are acquired at
  different times with different geometry, a person opens one at a time,
  and `hs.load`, a NeXus viewer and a file manager all take a file and
  give back a signal. It makes "send me the HAADF" a copy rather than an
  extraction. Each file also records `session_dataset`, so one renamed
  or copied out of the session still says which signal it is.
  **The acquisition names its own signals**, so nothing in the log or
  the storage layer had to learn what a recipe is.
  `AcquisitionRequest.build` yields `(name, frame)` pairs; `by_channel`
  splits a simultaneous multi-detector series by the detector each frame
  reports, and `named` labels a step, so a survey → SI → follow-up
  recipe is `itertools.chain` of three `named` calls and lands as one
  item with three files. Beside the frame rather than in its metadata,
  because frame metadata is the *device's* vocabulary and a survey HAADF
  and a follow-up HAADF carry byte-identical `channel_name`.
  **`dashboard.saving` is the way out for data with no file.** One
  signal at a time through the browser (`mo.download` over a NeXus file
  rendered on demand — a callable, so nothing is encoded on the display
  tick, which the live view now runs at up to ten a second, for a button
  nobody pressed), or every held signal at once into a directory named
  at the bottom of the page. The flush
  opens that directory as a session, so what comes out is
  indistinguishable from data acquired into it in the first place, and
  stamps each file with the time the data was *acquired* rather than the
  time it was saved — an 18:00 flush of an afternoon's work must not
  collapse its only ordering. Writing a signal releases its frames,
  which is also how a long shift stops filling the kernel up.
  **Failures are per signal.** A projected frame carrying no energy
  calibration cannot be stored as a spectrum, and that is no reason to
  lose the HAADF acquired beside it; each signal of an item is written
  on its own into a shared item, so the report names what went and what
  did not, and anything that failed is still in memory to try elsewhere.
  `SessionLog` gains exactly one mutation for this, `mark_stored`, which
  can only move a signal from memory to a path and can say nothing about
  what was acquired — where the bytes live is not part of the account of
  the shift, and a log that could not record a save would go on telling
  an operator their data was unsaved after they had saved it.

- **A file per microscope that says what hardware it has, and starts
  it.** An instrument is not one device server: SuperSTEM 3 is a Nion
  column whose scan unit and Ronchigram camera come from Nion's stack
  *plus* a DECTRIS ELA on the spectrometer that speaks SIMPLON and knows
  nothing about Nion, and SuperSTEM 2 is a Nion column plus a Bruker EDX
  detector behind a vendor SDK. Each adapter is a separate process, and
  the broker could start exactly one of them — named on its command line
  — so an instrument with two adapters could not be served whole at all.
  `instrument_config.py` is the missing description and
  `miainwoodpecker-broker --config <file>` reads it: every adapter the
  file enumerates is started, checked, and served as one instrument with
  one target map.
  **The enumeration is authoritative, which is the whole reason it is
  worth writing.** A device server reports what it found; nothing above
  it can tell "this column has no EELS camera" from "this column's EELS
  camera did not come up" — a spectrometer left switched off or a plug-in
  that failed to load produces a perfectly consistent instrument with one
  fewer camera, and every layer above serves it happily. Checked against
  a file that says the camera exists, the same startup is an error
  naming the device and listing what the server did serve. A device the
  file does *not* list is not served either, and is logged by name: an
  enumeration is all of the hardware or it is decoration.
  **Naming happens in the file, because no server can do it.** The
  DECTRIS adapter serves one detector called `camera`, since identical
  firmware behind an identical API is an EELS detector on SuperSTEM 3 and
  a 4D-STEM camera on somebody else's column. `served_as` is how the file
  says which, and clients see `eels_camera`.
  **`--config` replaces `--backend`, `--plugin` and `--server-module`
  rather than combining with them**, and passing both is refused: honour
  the file and `--backend hardware` silently does nothing, honour the
  flag and one word overrides the backend of every server in the file at
  once. Neither reading is one an operator should have to discover.
  Servers start in file order on an `ExitStack` and are torn down in
  reverse, so a failure to start the third adapter still parks the first
  two, and the column — conventionally first — is parked last.
  TOML, and unknown keys are refused rather than ignored, because
  `plugin` where the key is `plugins` would otherwise start a hardware
  server with no arguments and say nothing about it.
  Four worked examples ship in `instruments/`: the simulator (scan unit,
  Ronchigram and EELS cameras, plus a second simulated process that can
  be switched on to rehearse the multi-adapter path with nothing plugged
  in), and SuperSTEM 1, 2 and 3. The instrument files mark every line
  that is a hypothesis rather than a record — nobody has run this project
  on those microscopes yet, and
  [the survey runbook](docs/superstem-survey.md) is what settles them.
  `tests/integration/test_configured_instrument.py` runs the shipped
  simulator file for real: two device-server subprocesses, one broker,
  one client that cannot tell which process anything came from.
  **`pixi run -e device broker`** serves whatever
  `$HOME/.miainwoodpecker/instrument.toml` describes and publishes the
  invitation beside it, because a control computer drives the same
  microscope every day and that belongs in a fixed place rather than in
  a command line somebody has to remember.

- **A viewing area that shows more than one dataset at a time.** Every
  source became a napari layer on one shared canvas, and napari puts
  every layer at the world origin — so a 512x512 HAADF image and a
  128x1024 EELS readout landed *on top of each other*, and the only way
  to see either was to switch the other one's visibility off. Comparing
  two detectors read out of the same pass is the ordinary case on a
  scanned instrument, not an advanced one. `viewer/documents.py` now
  gives each dataset its own window in a `QMdiArea`: each enabled
  detector, each running camera, each opened recording, each analysis
  result.
  **A window per dataset rather than tiles on one canvas, because that
  is what makes zoom and pan per panel.** Each document is a real
  `napari.Viewer`, so focusing on one corner of the scan image does not
  drag the diffraction pattern beside it out of view, and every panel
  keeps napari's own contrast, colormap and gamma controls because it
  *is* napari rather than a reimplementation of it.
  **Nothing can be stretched by any of it.** napari's camera is
  isotropic — one zoom scalar drives both axes — so a panel whose shape
  does not suit its data letterboxes and the picture keeps its geometry.
  `tests/integration/test_documents.py` measures drawn aspect against
  data aspect after resizing panels to deliberately hostile shapes
  (900x200, 200x700, 1000x120) for square, 64x1024 and 1024x64 data,
  rather than trusting it.
  **New datasets are tiled into place; a layout you arranged is yours.**
  Nothing opens hidden underneath something already on screen, but the
  first time a window is moved or resized by hand the area stops
  rearranging things and later documents go wherever is clearest
  instead. **View → Tile documents** hands tiling back.
  **Closing a panel means it stays closed**, since a running detector
  would otherwise reopen its window on the next frame and make the close
  button useless. Starting that source again brings the panel back and
  raises it — the same request as raising one that is merely buried,
  which is a legitimate way to arrange the area while a source runs.
  `LiveInstrumentWidget` is unchanged in shape: `DocumentBoard` presents
  the slice of `napari.Viewer` the widget already used and routes each
  call to a document, so a plain viewer still works everywhere the board
  does and every existing widget test builds one directly.

- **Calibrated axes and a per-panel scale bar.**
  `storage/calibration.py` has modelled per-axis calibration for a while
  and the storage and analysis layers used it; the viewer did not, so
  every panel was in bare pixels with nothing for a scale bar to read.
  `viewer/axes.py` is the bridge, and each panel now carries its own
  units — measured against the preview instrument, three at once: the
  HAADF scan in nm, the Ronchigram in mrad, the EEL spectrometer in eV.
  Changing the field of view mid-scan rescales the running panel.
  **This is what one canvas could not have done.** napari applies units
  per layer but draws the scale bar per viewer, and says so out loud when
  a viewer's layers disagree — *"Inconsistent units across layers; units
  will not be used for rendering."* A window per dataset turns out to be
  a requirement of per-image calibration, not a preference.
  **Geometry is applied only where the axes are commensurable.** A
  real-space frame sampled four times more finely across than down is
  drawn four times wider, because that is the specimen's shape rather
  than the detector's pixel count. A 2D EELS readout is *not*: there is
  no rate of exchange between an electronvolt and a nanometre, so it
  keeps pixel geometry with its axes labelled, as DigitalMicrograph and
  HyperSpy both show it, and gets no scale bar rather than a length drawn
  across an energy. The calibration is attached to the layer either way,
  for the readout and ROI work that need the model rather than the
  drawing.

- **Binning that can differ between a detector's two axes.**
  `CameraParameters.binning` was one integer documented as applying "in
  each direction", which cannot describe a spectrometer camera. Binning
  the non-dispersive direction trades dynamic range for signal-to-noise
  and is what an EELS camera is routinely run with; binning the
  dispersive one spends the energy resolution the instrument exists to
  provide. Two settings with opposite costs were being held in one
  number.
  `binning` now takes an `int` *or* a `(y, x)` pair, read back through
  `binning_yx`. **A scalar still means both directions**, so every
  adapter and every caller written before this keeps working unchanged —
  and `validate_binning` refuses an asymmetric pair to any camera that
  has not published `binning_values_yx`, so a webcam, a Dectris detector
  or a replayed recording can never be handed a pair it would apply to
  only one axis. Cameras advertise per axis, since the limits genuinely
  differ: the preview spectrometer offers 1–8x down and 1–2x across, and
  a refusal names the axis it applies to.
  The camera panel follows — one **Image binning** control for a
  symmetric detector, **Binning (rows)** and **Binning (channels)** for
  one that distinguishes them. Measured on the preview spectrometer:
  binning rows 8x takes the readout from 64x1024 to 8x1024 with the
  dispersion held at 0.500 eV/channel, while binning channels 2x halves
  them to 512 and widens it to 1.000 eV/channel.

- **A spectrum image can be watched while it is acquired, and no longer
  freezes the window.** The pass ran *inline on the GUI thread*: for a
  64x64 grid at a realistic dwell that is minutes with no repaint, no
  live view, no answer from "Stop scan", and an acquisition the operating
  system reports as an application that has stopped responding. It is
  the same failure the recording, loading and analysis paths each already
  had a job class for; this was the fourth and last handler doing its
  work where it was called. `PassJob` moves it to a worker thread and
  `_poll_pass` reports from the display timer.
  **And there is now something to watch.** `viewer/progress.py` wraps
  each pass destination and forwards every write through to it
  unchanged, forming a **virtual detector image** on the way — the
  signal summed at each beam position, exactly as one is formed offline.
  A panel named `Acquiring (…)` fills in as the probe goes and the
  status line counts positions. Drift, contamination and a probe
  scanning vacuum are all visible in it minutes before the file exists.
  **No device change was needed for any of it.**
  `scan_synchronised(into=...)` documents a destination as "a shape, and
  assignment at a beam position" so that an `h5py` dataset satisfies it;
  the preview is one too. Every adapter honouring that contract gets a
  progress view without knowing this exists, which is why it is done
  here rather than by adding a callback each future adapter would have
  to remember to fire. The data is written through **first and in full**
  — the summary is formed afterwards, and a write the preview cannot
  summarise still lands.
  The summary subsamples anything large, so a 512x512 diffraction
  pattern does not cost a quarter of a million adds per position in a
  fast pass; the display range tracks the positions actually visited, so
  the unwritten zeros do not crush every real value to white.

- **Recordings say which of their axes are the signal, so RosettaSciIO
  reads them back as images and spectra rather than as a stack of
  navigation axes.** Measured against rsciio 0.14.0's NeXus plugin: a
  recording read through `file_reader` arrived with its array
  bit-identical and its calibration exact — 4.0 nm/pixel out, 4.0
  nm/pixel back, and 10 eV per channel at -480 eV for a spectrum
  recording, straight off `NXspectrum`'s own field names — but with
  *every* axis marked navigation, because nothing in the file said which
  axes were the signal. HyperSpy would build a `BaseSignal` where the
  analysis adapters expect a `Signal2D`. Both writers now emit NeXus's
  `interpretation`: `"image"` for a frame stack, `"spectrum"` for either
  `NXspectrum` layout, since the energy axis is last in all of them.
  **Written in two places, because the spec and the reader disagree
  about where it lives.** The NeXus manual defines `interpretation` as a
  *field* attribute and it appears nowhere in `NXdata`'s base class;
  rsciio reads it from the NXdata *group*, which is where its own writer
  puts it, and ignores it on the field. Both are written, so the file
  carries the spelling the manual gives and the one the reader looks
  for.
  **And withheld from a file that claims an application definition**,
  which is the part that had to be measured rather than assumed.
  `pynxtools` rejects any attribute the NXDL does not document, and NXem
  documents no `interpretation` anywhere: checked one placement at a
  time, group-only fatal, field-only fatal, and the same file with
  neither valid. The two goods cannot both be had in one file, so the
  rule is the one `definition` already follows — a file that makes a
  schema claim keeps it, and a file that claims nothing carries the
  hint. Every recording this project writes today claims nothing, so in
  practice the hint is always there, and an NXem file loses only the
  navigation/signal split rather than the ability to be read. Asked
  upstream as FAIRmat-NFDI/pynxtools#834.

### Changed

- **`acquisition.record` is now a loop over a `FrameSink`, and the sink
  is public.** Same file either way — it dispatches on the first frame's
  rank exactly as before, so a projected readout still lands in
  `NXspectrum`'s layout and an empty series still writes the empty,
  finalized frame file. What is new is that the loop can be turned
  inside out: a caller may hold *several* recordings open and push
  frames at whichever one each belongs to, which is what writing a
  simultaneous two-detector scan to two files requires. `record` still
  pulls its first frame before the sink exists, so a device that fails
  on its very first frame leaves no file at all.
- **`Session` gained `open_item`, `record_datasets` and `reserve_named`,
  and `storage.nexus.DEFAULT_FLUSH_EVERY` is public.** The first three
  are the multi-signal write path above; the last removes a duplicated
  constant — the flush interval was spelled separately in `nexus.py` and
  `spectra.py` with one measurement behind both, and `sequence.py` now
  needs it too.
- **The dashboard's `AcquisitionRequest.build` yields `(name, frame)`
  pairs rather than frames**, and `SessionLogEntry` carries
  `datasets: tuple[LoggedDataset, ...]` in place of its single `shape`,
  `dtype`, `thumbnail`, `metadata` and `recording_path` fields.
  `highlights()` takes a `LoggedDataset` rather than an entry. A
  multi-detector scan is now labelled `scan` with a signal per detector,
  where it used to be one entry called `scan-HAADF-MAADF` holding both
  detectors' frames interleaved in one stack.
- **The live view refreshes at 60 fps, up from 30.** The display timer
  was a hard-coded 33 ms with nothing behind the number. Measured end to
  end — through `refresh_display`, the document board and napari, with a
  zero-cost frame source so the display path is what is being timed —
  the whole thing costs **9.4 ms at 512², 9.7 ms at 1024² and 10.2 ms at
  2048²**. Near enough flat, because the pixels go to the GPU, so the
  path already sustained ~100 fps and the old timer was discarding two
  thirds of it.
  **Raising it is close to free when nothing is arriving**, which is what
  makes it safe on a slower machine than the one measured: a tick that
  finds the frame it already drew costs **4.4 microseconds** — 0.026% of
  a core at 60 Hz — because the identity check skips before any upload or
  contrast pass. Cost tracks frames produced, not ticks.
  The two per-frame passes the calibration work added are not a factor
  either: resolving a frame's calibration is 0.01 ms at every size, and
  the autocontrast min/max is 0.5 ms at 2048² — 5% of the frame budget,
  and it runs for the scan path only.

- **A window is sized to its picture, so no panel has black bars in
  it.** The frame takes the data's own shape and the picture fills it
  edge to edge — measured at 1.000 fill on *both* axes for square, very
  wide, very tall, tiny and oversized data alike. Blank space in a panel
  spends screen on nothing, and two panels of different shapes padded to
  the same shape look like the same panel.
  **Small data is magnified rather than shown as a stamp**: anything
  whose longest side is under 256 pixels opens scaled up to it, so a
  64x64 spectrum-image map opens as a 256-pixel window. The floor and
  the target are one number deliberately — magnifying to 512 while
  leaving 256 alone would open a 128-pixel scan in a *larger* window
  than a 256-pixel one, and window size would stop meaning anything
  about the data. Anything already that large opens at one screen pixel
  per acquired pixel and shrinks only if it will not otherwise fit.
  A panel a few pixels too wide for a row it nearly fits is shrunk into
  it rather than wrapped: two panels came to a handful of pixels more
  than a dock-narrowed workspace, and wrapping sent the second to the
  overlap fallback, so two that all but fitted side by side ended up
  stacked. Packing resets each window to its content size first, so
  tiling repeatedly does not erode them.
  **Tiling means beside, not filling.** `tileSubWindows` divides the
  whole area between the windows, which gives each the *area's* shape
  rather than its picture's and puts the bars straight back, so panels
  are packed at their own sizes instead. **No part of a window is ever
  outside the workspace** — too big is shrunk, overhanging is moved in,
  and shrinking the application brings the panels in with it. Overlap is
  allowed when there is no room, but staggered so a covered window keeps
  a corner to raise it by.
  **View → Actual resolution** (`Ctrl+1`) gives one screen pixel per
  acquired pixel; **View → Fit panel to data** (`Ctrl+0`) returns, and
  hands the panel back to automatic fitting. A panel the operator has
  zoomed is never refitted by anything automatic.
  Two timing traps are recorded in the code because both produced
  visible faults: the refit has to happen on the panel's *resize event*
  rather than straight after the resize call, since Qt resizes the
  canvas afterwards — fitting immediately fits to the size the panel is
  about to stop being, and drew an image 44% taller than its own panel;
  and the window can only be sized once it *has* data, since a document
  is created before its first layer is added.

- **The NeXus schema check is a pixi environment rather than a hatch-only
  job.** `hatch run validate:schema` was the only way to run the one check
  that guards the on-disk format, so on a machine with pixi and nothing
  else it could not be run at all — getting an answer out of it once took
  a pip-install of pynxtools into a scratch directory. It is now
  `pixi run -e validate schema`, and CI's `nexus-schema` job installs that
  one environment from `pixi.lock` instead of hatch. The hatch environment
  is gone; hatch still owns the wheel and sdist, the Python 3.11–3.13
  matrix, the integration suite and the docs.
  **The environment sits outside the "main" solve group**, because
  pynxtools brings a large pypi tree and this check has nothing to be
  consistent with — the same isolation the hatch env stated as its reason
  for existing. Its Python is no longer pinned: that pin existed because
  hatch resolved each environment's interpreter independently of CI's
  `setup-python` and picked one that failed, and pixi locks the
  interpreter with everything else. The lock currently records 3.13, on
  which pynxtools 0.15 runs the whole check green.
  **One honest difference.** The hatch env resolved afresh every run, so a
  pynxtools release tightening the schema showed up on the next CI run;
  `pixi.lock` pins it, so that signal now arrives when someone runs
  `pixi lock`.

### Fixed

- **A pass's display stopped one tick short of the end.** `_poll_pass`
  drew the previews and *then* asked whether the worker was still
  running, so the acquisition that finished in between was drawn from
  the state it had a few beam positions earlier — and nothing drew again,
  because the next poll returns at the top. The finished map was missing
  its last positions and the spectrum was from the second-to-last one.
  Asked first, drawn second.
- **A frame too small for blosc2 took the interpreter down, several
  test files later.** `tests/unit/test_storage_nexus.py`'s plugin-codec
  test failed in the `replay` environment with `Unable to synchronously
  flush file (buffer size is too small after filter callback)`, and the
  process then died of a Windows access violation inside an unrelated
  test that ran afterwards — from a handle the garbage collector
  happened to reach there. The two symptoms are one bug and neither is
  an environment mismatch: `pixi list` shows `default` and `replay` on
  **byte-identical** hdf5 2.2.0, h5py 3.16.0, blosc 1.21.6 and numpy
  builds, as their shared `solve-group` requires. The only difference is
  that `replay` has `hdf5plugin` at all, pulled in beside RosettaSciIO,
  so `default` never ran the test — `importorskip` skipped it.
  **A blosc2 chunk is a self-contained cframe, and its container costs
  ~304 bytes whatever the payload is.** Measured with hdf5plugin 7.0.0
  on constant float32 data, where the ideal ratio is unbounded: a
  336-byte chunk stores as 309 bytes, a 512-byte one as 304, a 16 KiB
  one as 307. The floor is flat, so below roughly 336 bytes the
  "compressed" chunk is *larger* than the raw one, and hdf5plugin's
  filter does not grow HDF5's pipeline buffer to fit it. The test wrote
  4×6 float32 frames — 96-byte chunks, an order of magnitude under.
  Bisected, 332 bytes fails and 336 passes; the exact edge drifts a few
  bytes with `cname` and `filters`, and 336 is the smallest size at
  which every combination measured succeeded. It is specific to blosc2:
  `hdf5plugin.Blosc` (blosc1), `Bitshuffle`, `Zstd` and the built-in
  `gzip` all round-trip the same 96-byte chunk.
  **The filter error cannot be recovered from, which is why the fix is a
  precondition rather than a `try`.** It surfaces on flush, and the
  chunk stays dirty and unwritable afterwards, so every route that later
  releases the dataset id runs the filter again and faults the process.
  An explicit `File.close()`, a bare `id.close()`, closing the dataset
  id first, dropping the last reference and collecting, holding a strong
  reference forever, and plain interpreter shutdown were each measured
  to end in an access violation — on a backing-store file and on an
  in-memory `driver="core"` one alike. There is nothing left to catch by
  then. `storage.nexus.reject_unwritable_chunk` therefore refuses such a
  dataset *before* `create_dataset`, raising a `ValueError` that names
  the chunk size, the floor and the codecs that do not have one; both
  writers call it, `spectra.py` by importing it rather than respelling
  it, because a short spectrum is the one shape in this project that can
  land under the floor without anyone contriving it — a 64-channel
  float32 spot spectrum is a 256-byte chunk. The guard is deliberately
  narrow: rejecting every plugin codec at that size would refuse three
  that work to stop one that does not.
  The plugin-codec test now uses a 32×32 frame, since what it exists to
  pin is that an `hdf5plugin` filter object passes straight through as a
  `compression=` mapping, not that blosc2 works on 96-byte chunks. Two
  tests were added beside it, for the refusal and for the guard's
  narrowness.
- **A writer whose `close()` raised stayed wedged half-finalized.**
  Both `NexusWriter.close` and `SpectrumWriter.close` reset their
  per-acquisition state in a `finally`, but with `self._file.close()` on
  the line above the reset rather than in a nested `try` — and h5py's
  `File.close()` closes the open dataset ids first, which pushes their
  dirty chunks through the filter pipeline, so it is a step that can
  genuinely fail. When it did, the writer kept a non-`None` handle and
  every other field stale, the file stayed locked, and the frames
  already on disk were unreachable through an object that looked open.
  Found while chasing the blosc2 fault above, and fixed independently of
  it: any raise from `close()` now leaves the writer reset.

- **A device server that lost its port could be reported as one that
  was running and wedged.** The client bounds every connection attempt
  by its 15 s connect deadline and polls the spawned server between
  attempts — but that bound is the *whole* deadline, so one attempt that
  blocks spends all of it and the child is never looked at again. On
  Linux and Windows nothing blocks that long, because a port with no
  listener refuses the connection at once. On macOS a port that is
  *bound* by something which is not listening neither refuses the
  connection nor completes it, and that is exactly the port a server
  reports a collision over: the client spent its whole deadline dialling
  a port its own server had already given up on, then said "the server
  process is alive but not completing connections, which retrying will
  not fix" about a process that had exited a second in with
  `PORT_UNAVAILABLE_EXIT_STATUS`. The one startup failure a respawn
  cures, reported as the one it cannot.
  **`_connect_once` now polls the child while an attempt is in flight**
  and abandons the attempt the moment it finds it gone, so the retry
  loop gets back to the exit status it should have been dispatching on.
  A healthy connect returns on the first 50 ms slice and pays nothing
  for the vigilance, and the diagnosis this is easily confused with is
  untouched: a server that really is alive and not handshaking still
  fails on the deadline with the same message, which is what that
  message is for.
  It surfaced as `tests/unit/test_out_of_tree_server.py`'s
  port-collision test failing on `macos-latest` — both 3.11 and 3.13
  have hit it — and on no other platform, on `main` as well as on
  branches, with the same job passing on the pushes either side. That
  test holds a bound, unlistening socket to make the child's bind fail,
  which it does everywhere; what is not portable is what the same socket
  then does to a *connect*, so the deterministic pin for the fix is in
  `tests/unit/test_remote_connect.py` instead — a `Client` that never
  answers, over a child that exits underneath it, which is the same
  situation from the client's side on every platform. Reverting the poll
  reproduces the CI failure message verbatim against it, which is how
  the cure was checked rather than by watching a green run.
  **The analysis worker's client had the same hole in a plainer form**,
  and had never had either half of the cure: `analysis/remote.py`'s
  `_connect_with_retry` called `Client()` on the calling thread with no
  bound at all, so its advertised 30 s budget bounded how *often* it
  tried rather than how long it waited, and `while time.monotonic() <
  deadline` could not end an attempt it had already started. A worker
  that accepted the connection and then stopped short of the handshake
  hung the application outright — on that path the caller is whichever
  thread an analysis was requested from, because a worker is started
  from a button rather than once at session start. Measured against a
  socket that listens and never accepts, that call is still blocked
  after five seconds and has no timeout to reach. It now runs on the
  same kind of scrap thread, polls the worker underneath the attempt,
  and names the exit status rather than the timeout when one dies mid-
  attempt. The two connect loops stay separate, as their docstrings say:
  what differs between them is the sentence each produces, not the
  plumbing. `tests/unit/test_analysis_connect.py` pins both halves — a
  never-accepting socket for the bound, a `Client` that never answers
  over a worker that exits for the poll.

- **The out-of-tree adapter stand-in did not survive losing a port, so
  the test file that documents the adapter contract was intermittent.**
  `_free_port()` picks a port by binding to port 0 and *releasing* the
  socket, so the port is reserved by convention only until the spawned
  server binds it an interpreter start and a vendor stack's imports
  later. Every server in this tree answers a lost port by exiting with
  `PORT_UNAVAILABLE_EXIT_STATUS`, which the client cures by re-picking
  ports and respawning. The stand-in server in
  `tests/unit/test_out_of_tree_server.py` did not: it let the `OSError`
  escape as a traceback, so a collision reached the client as an
  unexplained exit status, and every test in that file asserting on the
  client's *own* startup diagnostic failed with a message about a port
  instead. It surfaced on CI as the "a target served without an endpoint
  is named rather than a key error" test failing with `AssertionError:
  Regex pattern did not match`, over captured server output reading
  `OSError: [Errno 98] Address already in use`.
  **The stub now translates the bind failure like the real servers do**,
  in a `bind()` function whose docstring is the reason rather than the
  mechanism — the point of that file being that the stub is an honest,
  copyable example of what an out-of-tree adapter must implement.
  A new test provokes the collision deterministically, by handing the
  client a port the test process is holding on the first spawn and a
  real one afterwards, and pins that the session opens and that it cost
  two spawns. Reverting the stub's `sys.exit` to `raise` reproduces the
  original CI traceback and failure against that test, which is how the
  cure was checked rather than by watching a green run.
  `tests/unit/test_port_collision.py`'s `serve_everything()` stub had
  the identical hole — in the file whose subject is port collisions —
  and now exits the same way.
  **`docs/vendor-support.md` did not state the requirement anywhere**,
  so it now does, next to the port and `--instrument-port` contract it
  belongs to: a server that cannot bind a listener must catch the
  `OSError` and exit with status 4 rather than let the traceback escape.
  An adapter that skips it does not have a bug, it has a flake.

- **Every workflow ran twice per push.** `docs`, `lint`, `pixi` and
  `test` each triggered on `pull_request` *and* on `push` to
  `'claude/**'`, so a branch with an open PR produced eight runs where
  four would do — confirmed on one commit, four `push` runs and four
  `pull_request` runs against the same SHA. Three of the four carried a
  comment claiming the `concurrency` group collapsed the pair, and it
  could not: `github.ref` is `refs/heads/…` for the push event and
  `refs/pull/N/merge` for the pull-request event, so the two never share
  a group. `push` is now `main` only, `pull_request` covers branches,
  and the comment says what is actually true. The cost is that a branch
  pushed before its PR exists gets no CI until the PR is opened; the
  `pixi` job on a branch is also now subject to its own `paths` filter,
  which is what that filter is for.
  **This is not why the flake happened.** Both runs are GitHub-hosted,
  so they are separate VMs with separate loopback and cannot contend for
  a port; the collision was inside one job. Halving the runs is worth
  doing for the bill, not for the race.

- **The aspect-ratio test was measuring nothing.** It computed drawn
  width and height by multiplying each by the same `camera.zoom`, which
  cancels — so it compared the array's pixel ratio with itself and would
  have passed however the image was drawn. It now maps unit steps along
  each world axis through the live VisPy scene transform and compares the
  screen distances, which is a measurement an anisotropic camera would
  fail, and a separate test pins that isotropy directly.

- **A device that replays a recorded session, at the speed it was
  taken.** `devices/replay.py` opens a DigitalMicrograph spectrum-image
  session and serves it through `Scanner`, `SynchronisedScanner` and
  `Camera`, so the viewer, `PassWriter` and the NeXus layout run against
  real data unchanged. It is deliberately a *device* and not a file
  reader: a reader answers "what is in this file", and the question this
  answers is "does the acquisition path work" — the synchronisation, the
  pass, the streaming writer, the refusals, and the timing an operator
  actually sits through.
  **The correlation a `ScanPass` asserts is, for the first time,
  historical fact rather than a property of the adapter.** The HAADF
  channel in these sets was read out by the instrument *during* the
  spectrum image's own acquisition, so `scan_sync = "detector"` is a
  statement about what GMS did in 2011. Measured on the first set:
  a carbon map integrated above the C-K edge correlates with the HAADF
  of the same pass at **+0.995**.
  **What it will not do is resample.** A recording is the grid the
  operator chose — 22x25 beam positions — and any other geometry is
  refused rather than interpolated to, because a cube of the requested
  shape whose every pixel was invented looks exactly like a real one.
  The viewer therefore asks a device for its geometry before using the
  panel's: `native_scan_parameters` returns None for every device that
  has no such constraint, and the square Positions spin box cannot even
  express 22x25.
  Everything is read from the file rather than assumed: the energy axis
  by its *units* (this reader returns an SI as `(energy, y, x)` and a
  line scan from the same session as `(x, energy)`, so neither the axis
  order nor the `navigate` flag can be trusted); the dwell from
  `SI.Acquisition.Pixel time (s)`; the binning by comparing the
  acquisition's dispersion against the spectrometer's own; the
  accelerating voltage and drift-tube setting from the microscope tags.
  Timing is the recording's own unless `--speed` is passed, and a
  compressed replay says so in the metadata of everything it produces —
  as does the `replay` backend name, on every frame and every spectrum,
  because by the time anyone reads a log the session has happened.
  `pixi run -e replay replay <session> --list` says what a directory
  holds; without `--list` it opens the viewer against one acquisition.
  The reader is the new `replay` extra (RosettaSciIO alone, not
  HyperSpy) and gets its own pixi environment, so a microscope PC does
  not need the analysis stack to replay a session.

- **A spectrum image can be gathered without hardware.** The preview
  instrument grows an EEL spectrometer, `PreviewEELSCamera`, and a
  synchronised pass can now read a detector out *as spectra* at every
  beam position — so the whole path from device to `NXspectrum` on disk
  is exercisable on a laptop with no microscope and no vendor SDK.
  **What makes a detector a spectrometer is that one of its axes is
  calibrated in energy rather than in space**, and nothing here treats
  its *rank* as the distinguishing thing. The ordinary readout is the 2D
  dispersed image, which is what the camera delivers by default and what
  an operator aligns on; summing the non-dispersive direction to 1D is a
  mode they choose, and keeping the whole 2D readout per beam position
  is a real experiment rather than a misconfiguration.
  So what a pass does with a target follows its **readout mode** and
  never its type: projecting, it contributes a rank-3 spectrum image;
  imaging, it contributes a 4D stack of whole detector readouts — the
  same container a Ronchigram camera fills, because at that point the
  two are the same shape of data. Which is why the axes now travel with
  the stack: a `DiffractionStack` carries the detector's own
  calibration, and `PassWriter` prefers it over its own default, since
  otherwise the one fact separating a 2D EELS cube from a diffraction
  cube would be absent from the file.
  The spectral model encodes something checkable, which is the whole
  reason it is more than decoration: a zero-loss peak whose *channel*
  moves when the spectrometer's energy offset is driven while its
  *energy* stays at zero, silicon and carbon plasmons, the power-law
  background every quantification subtracts, the Si L2,3 and C K edges
  at their real onsets with complementary heights, and Poisson noise. A
  silicon map integrated out of a spectrum image therefore tracks the
  HAADF channel **of the same pass** — a test asserts it, and a cube of
  one repeated spectrum would pass a shape check and fail that.
  The edge heights were sized against the background at their own
  onsets rather than in isolation; the first attempt buried the silicon
  edge in its own background, which is a fair model of a *bad*
  acquisition and no use as the thing a demonstration points at.
  The energy axis is nionswift-usim's — 0.5 eV per channel, channel 0 at
  −20 eV — so the preview's spectrometer lands on the same axis as the
  backend this project validated its calibration path against.
  **The target name decides the detector**, which is a correction as
  much as a feature: before this, a preview asked for two cameras served
  a *Ronchigram* on the `eels_camera` target.
  `PassWriter` gained `spectra=`, allocating a rank-3 dataset chunked
  one beam position per chunk — deliberately not `SpectrumWriter`'s row
  chunking, because that writer receives a finished map and this one is
  filled position by position as the device acquires. Each signal is
  spelled in the vocabulary its own kind already has: image channels and
  cubes keep `data`, spectrum images use `NXspectrum`'s `intensity`,
  `axis_energy`, `axis_j` and `axis_i`, exactly as a standalone spectrum
  recording does. `read_pass` therefore asks each `NXdata` group what
  its signal is called instead of assuming — which is what that
  attribute is for, and a reader that assumed `data` would silently omit
  every spectrum image from the list of what a file holds.
  In the window: a **Detector readout** control per camera, and a
  **Per-position detector** selector beside the positions count. The
  readout control configures the device the moment it changes, unlike
  the exposure and binning beside it — readout decides the rank of every
  frame the detector produces, so a camera whose live view imaged while
  its next acquisition projected would be in two states at once. It is
  offered on every camera and refused by the ones that cannot project,
  because `Camera` has no capability to ask and widening a
  `runtime_checkable` protocol would break every adapter that passes its
  check today; the refusal reaching the operator is the point, since one
  who never sees it learns nothing about why their Ronchigram camera is
  not a spectrometer. The target selector is honoured rather than
  falling back to the first target, which would acquire against a
  detector nobody chose and store it under that detector's name.
  Known limit, unchanged and still stated: the grid is square. The
  acquisition no longer blocks the GUI thread — see the entry above on
  watching a spectrum image build — which mattered most here, since a
  spectrum image is what an operator will want a large grid of.
- **A live dashboard in the browser, and the descriptions that let it
  exist.** `notebooks/instrument_dashboard.py` is a marimo app over a
  running `miainwoodpecker-broker`: fixed-placement tiles polling
  `snapshot()`, detector checkboxes and a binning menu built from the
  broker, and an acquire button whose lease is taken on the job's own
  thread and renewed per frame. Acquisitions append to a session log
  panel rather than writing new cells - marimo forbids a notebook
  rewriting itself, and an append-only log is the shape that survives
  contact with a reactive runtime.
  The judgement lives in `miainwoodpecker.dashboard`, not in cells, so
  the unit suite covers the tile rules, the PNG encoding, the log and the
  acquisition job with marimo uninstalled. `marimo` is an optional extra
  that nothing else implies.
  **What made it possible is `broker.describe()`.** `channel_names`,
  `binning_values`, `synchronised_targets` and `available_controls` were
  read straight off device handles, and a client in another process has
  no device handle to read - which is what had kept any out-of-process
  front end to showing a picture and nothing else. They are read once
  when the broker is built and cached from then on: not for speed, since
  it is four calls, but because that is the only honest moment. A read
  issued later would be a second caller on a device whose live loop is
  mid-pass, which is the interleaving the broker exists to prevent.
  A device that *refuses* one of those questions gets empty fields and an
  `error` naming what it would not answer. The distinction is
  load-bearing: "this camera supports no binning but 1x" and "this camera
  would not say what binning it supports" are otherwise the same empty
  tuple, and they want opposite responses - offer the one value, or tell
  the operator the device is not answering.
  The viewer reads descriptions rather than device handles for its
  detector names, its synchronised targets and its instrument controls.

- **One command for the whole arrangement: `pixi run instrument`.**
  `miainwoodpecker-instrument` starts a broker over the instrument,
  waits for it to publish where it is listening, starts a front end
  against it, and stops the broker when that front end goes. The
  sequence was four manual steps with a port to copy between two of
  them; it is a supervisor and nothing else — it talks to no device and
  no broker, and every option either child takes it passes through.
  **The broker is asked to stop, never killed**, because being asked is
  what parks the instrument. That constraint decided the design: the
  obvious `pixi run -e device miainwoodpecker-broker` spelling was
  measured and rejected — a broker started that way exits with
  `0xC000013A` and never runs its shutdown, because the signal reaches
  `pixi.exe` and the broker is taken down under it. So `--broker-env`
  and `--ui-env` *resolve* an environment through `pixi shell-hook
  --json` and spawn that environment's own interpreter directly, which
  is the only arrangement where "ask it to stop, and it parks" holds.
  It also means the shipped task runs the broker in the environment
  with the GPL-3.0 device stack and the window in one without it, so
  the licensing boundary is something you can watch happen.
  Anything after `--` replaces the window and is given
  `$MIAINWOODPECKER_BROKER` — the variable the dashboard already reads,
  and now the default for the viewer's `--broker` too — so the same
  command serves an instrument to a marimo dashboard or to a script.
  `pixi run dashboard` is that spelled out, in the environment that has
  marimo and no Qt.
  **`--serve` is the other shape of session**, and the difference is
  about the instrument rather than the software: no front end, held
  open until Ctrl-C, for a column that people attach to and leave all
  day — a window this morning, a notebook after lunch, both at once.
  The default mode ends when *its* front end closes, which is right for
  one sitting and wrong for a shared microscope. `pixi run serve` is
  that with the vendor environment chosen and the invitation published
  into the working directory, where a notebook looks by default.
  **A front end is given five seconds to close, not thirty.** Found by
  an operator pressing Ctrl-C a second after the window was launched:
  "pid 24332 did not stop within 30s; terminating it", with the
  instrument sitting unparked behind a front end that was never going
  to answer. Measured afterwards - a napari process asked to stop
  during its own startup stops responding to console control events
  altogether, and keeps not responding however many times it is asked,
  while the same window once it is up closes in 0.1s. The thirty
  seconds belong to the broker, which spends them parking; a window has
  nothing to park, and the worst it loses by being terminated is a
  recording the storage layer already reports as unfinalized.
  The launcher installs the same three signal handlers the broker does
  (`SIGINT`, `SIGTERM`, `SIGBREAK`), because a supervisor stopping a
  process whose defaults terminate the interpreter without unwinding
  would leave the broker orphaned and the instrument unparked — the
  failure the broker's own handlers exist to prevent, which putting a
  launcher in front of it would otherwise reintroduce.

- **A device server is in its own process group on Windows too.**
  `devices/remote.py` has always said it puts the server in its own
  group, and gave the reason: an interrupt in the launching terminal
  must not race the client's teardown and kill the server before
  anything parks the instrument. It was spelled `start_new_session`,
  which is a `setsid` call and is **silently ignored on Windows** — so
  on this project's first platform the stated intention had never held.
  Measured while building the launcher: a Ctrl-Break to a broker's
  group killed the device server with `0xC000013A` first, and the
  broker's park then failed with a lost connection. Fixed, and the
  analysis worker gets the same treatment for the same reason.

- **The window can be pointed at an instrument in another process.**
  `miainwoodpecker-viewer --broker PATH` connects to a broker that is
  already running - at the invitation it published - and launches no
  device server, holds no device handle, and imports no vendor code.
  It is the same window: every control is there, because everything the
  window needed to *build* one now crosses the wire.
  Five reads stood between the description and that, and each is now a
  fact rather than a device call. `TargetDescription` gained
  `binning_values_yx` (the per-axis answer the previous entry left
  behind, which is what gives a spectrometer two binning menus rather
  than one), `backend` (the identity row, so an operator can tell the
  simulator from the microscope), `native_scan` (the one geometry a
  replay device will accept), and `synchronises` - separate from
  `synchronised_targets` because "this backend has no synchronised
  mode" and "it has one with nothing wired to it" are the same empty
  tuple and want different things done about them. `camera_parameters()`
  joins `controls()` as a watch call: what a detector is set to, read
  without a lease, because a window that had to lease a camera to fill
  in an exposure field would hold one from the moment it opened.
  **Driving a control is now a lease, and can be refused.** Setting the
  defocus or the readout mode used to be a direct device call from the
  GUI thread; it takes the target for the duration now, so a notebook
  sweeping the same control gets an error naming the holder instead of
  two writers interleaving on a one-request-at-a-time connection. The
  wait is bounded at half a second and the instrument target runs no
  live loop, so the window does not stop repainting for it.
  **A spectrum image sizes its file inside the lease it acquires
  under.** Allocating the datasets means taking one frame from the
  detector at the settings the pass will use - an acquisition, which was
  being done on the GUI thread against an unleased handle. It moved into
  the job with the pass it sizes.
  Reading the instrument's controls is one broker call now rather than
  four device calls, and the broker guards each control separately so
  that one it cannot read costs its own row rather than the whole
  reading — the window had that property by reading each control
  itself, and going through the broker should not have lost it for
  every other client too. A control the instrument published and did
  not report is named as "not reported" beside the field that is still
  showing its last value.

- **"Analysis extras" was printed under a camera's name, so it read as a
  property of that camera.** The row naming which analysis libraries are
  installed was built into the *first* camera's section: a panel headed
  "Camera - usim_ronchigram_camera" said "Analysis extras / enabled:
  none" underneath, which reads as this camera having no analysis rather
  than this machine having no libraries — and on a two-camera instrument
  it read as a difference between the two cameras, since only one of
  them carried the row. What `pip install` decided is the same answer
  for every camera served, so it is a top-level **Analysis extras**
  section of its own now, beside Instrument, Recordings and Devices,
  built only when an extra is missing exactly as the row was. The three
  buttons stay in a Camera section, because those do run against that
  camera.
  **The move gave the summary an answer in a case where it had none.**
  An instrument serving no camera builds no camera section, so the
  summary had nowhere to appear and an operator on a scanner-only
  instrument could not find out what was installed at all.
  `viewer/panels/analysis.py` is the new home of both halves and says
  which is which; `viewer/panels/devices.py` is 125 lines shorter for
  it. `_analyze_button`, `_analyze_status` and their siblings are reset
  to `None` there, before and regardless of the loop over cameras — they
  used to be set only inside it, so a camera-less widget left them unset
  entirely and the handlers that read them would have raised
  `AttributeError` rather than taking their "nothing to analyze" branch.
  A new test builds a two-camera widget with nothing installed and pins
  the summary outside every device section and inside the new one, so
  the row cannot quietly move back under a camera — either camera.

### Changed

- **The viewer is a client of the broker now, not the owner of the
  instrument.** `viewer/live.py` used to enforce "one driver per device"
  itself, in pieces: a `stop_scan` whose return value every caller had to
  check, a stop-the-loop dance before each acquisition, and two "still
  busy - try again" strings. That worked exactly as long as this window
  was the only program touching the microscope, which is the assumption a
  notebook or a dashboard breaks. The rule now lives in one place they
  all share, and what is left in the window is a window: the live loops
  are `start_live`/`stop_live`/`reconfigure_live`, a display tick is one
  `snapshot()`, and every acquisition is a lease.
  **No lease is taken on the GUI thread**, and that is the change an
  operator will actually feel. Taking one means waiting out the pass
  already in flight, and a pass is `height x width x dwell` - 262 ms at
  512x512 and 1 microsecond, but 42 s at 2048x2048 and 10. The old code
  called `stop_scan` in the click handler and refused if it did not
  return promptly, so on a large scan "record" simply never worked. Every
  acquisition now takes its lease *inside* the generator its worker
  consumes, where waiting costs nothing but the wait; the lease is
  renewed per frame, so a recording of any length outlives no deadline
  and a wedged one still hands the instrument back. Preview and the
  spectrum image still block, as they did before, and say so.
  Two consequences are visible in the window. A paused source says who is
  holding it (`held: recording 20 scan frames`) rather than looking
  stopped, and the broker restarts it afterwards unasked. And **closing a
  window that was given a shared broker touches nothing** - not the
  loops, not the instrument. Stopping a shared scan on the way out would
  park the probe on one spot for whoever else is connected, and a
  notebook watching the feed would have it go dark because somebody
  closed a window they were not using.
  Passing devices still works and still builds a private broker, so every
  existing caller is unchanged. One limit is worth stating rather than
  discovering: the window still reads `channel_names` and `binning_values`
  off its device handles directly, so it cannot yet be pointed at a
  broker in *another process*. Those reads produce no frames and so
  cannot interleave on the segment the arbitration protects; moving them
  onto `TargetState` is what a second screen will need.
- **`LocalBroker.snapshot()`, because a display tick is one question.**
  Asking `targets()` and then `latest_frames()` per source re-enters the
  broker once per call and re-takes each loop's lock once per call - and
  that lock is being reacquired by the acquisition worker on every grab,
  so a viewer polling at 30 Hz spends its time queueing behind the thread
  it is trying to watch. `snapshot()` answers state and frames together,
  under one acquisition of each. The remote version carries a health
  warning in its docstring: it ships every target's pixels, so a
  dashboard on another machine should keep asking `targets()` for the
  chrome and `latest()` for the one source it is showing.
- **A stopped live loop still answers with its last frame.** `latest()`
  used to return `None` once a loop stopped, which blanked the display
  the moment an operator stopped the scan to look at what they had.
  Whether the picture is still *advancing* is `TargetState.is_live`'s
  job. This is also what makes a lease invisible to a watcher: the
  display holds the pre-lease frame while an acquisition runs instead of
  flickering to nothing and back.
- **The lease deadline is derived from the scan, not guessed.** It was a
  flat five seconds, justified against a 512x512 scan at 1 microsecond
  dwell. That justification does not survive contact with a real
  instrument: the same arithmetic gives 42 s at 2048x2048 and 10
  microseconds and nearly three minutes at 4096x4096, so every lease on a
  large scan would have been refused, forever, with "still finishing a
  scan - try again". The broker already holds the scan spec, so it
  computes the pass and treats the caller's `timeout_s` as a *floor*
  instead. A camera keeps the floor - its exposure lives on the device,
  and asking for it would put a call on a connection whose loop is
  mid-exposure - and a long-exposure detector is the case that will want
  this next.
- **Several detectors can be live at once, and the panel says so.** The
  Scan group offered a drop-down: a choice of *one* detector. That
  describes serial acquisition, which on a scanned instrument is the
  special case — one pass of the probe reads out every fitted detector,
  and HAADF and MAADF arrive together. It also disagreed with what the
  application already did, since `acquire_scan_image` had been reading
  every channel out of one pass since it landed.
  Checkboxes replace it, and every checked detector gets its own napari
  layer fed from the **same** `scan_frames` call. `MultiChannelLiveAcquisition`
  is what makes that true: one loop whose grab returns a whole pass,
  published under the lock as a set. The obvious alternative — one
  single-channel loop per detector — would have looked identical on
  screen while costing twice the dose, letting the specimen drift
  between the two images, and making a per-pixel difference of them
  meaningless. Unchecking the last detector is refused with an
  explanation rather than allowed: a scan with none reads nothing out,
  so it is not a state an operator could mean.
- **Dwell and resolution belong to a profile now.** View, Preview and
  Acquire each carry their own, and all three are visible at once
  because "what will Preview do" is asked as often as "change Preview".
  Preview is the one whose purpose is least obvious from its numbers: it
  sits between the other two so focus and astigmatism can be judged *by
  eye* at a signal-to-noise the live view cannot reach, and its action
  shows a scan and records nothing — a focus check that littered the
  session with files would stop being used.
  **The field of view is shared and deliberately outside the profiles.**
  It is the region the operator navigated to, and a profile carrying its
  own would move the specimen out from under them at the moment they
  were happiest with it. `Acquire scan image` and `Record frames` use
  the Acquire profile rather than whatever the live view happened to be
  running at; changing Preview or Acquire never disturbs a running live
  view, since each is read when its own action is taken.
  Both the detector selection and the profiles persist between launches.
  Two under-specified test doubles surfaced on the way, the same shape
  of problem as `_FakeCamera` earlier: `_FakeScanner` never implemented
  `scan_frames`, though the `Scanner` protocol has required it since
  simultaneous multi-channel scanning landed. It was completed rather
  than worked around.
  And a fixture was added that the suite should have had already: tests
  now write preferences to a temporary file. Without it the suite
  mutated the developer's own config — a test that reaches outside its
  `tmp_path` is a test with a side effect, whatever it asserts — and one
  test's checkboxes leaked into the next, which is how the problem was
  found.

### Added

- **One instrument, many clients: `miainwoodpecker.broker`.** Everything
  in `devices` assumes a single driver, and `shared_frame` reuses one
  segment per source *because* of that — the server cannot publish frame
  N+1 until the client has copied frame N out. Two clients on one device
  therefore do not merely contend; they interleave on a reused buffer and
  produce a frame that is half pass N and half pass N+1, with no
  exception raised anywhere. Until now that rule was upheld by there
  being one application: the viewer owns the connection, owns every
  `LiveAcquisition`, and stops the loop before it acquires. A notebook, a
  browser dashboard, a second screen or an agent is a *second* client,
  and there was nowhere for the rule to live.
  It lives here now, as two verbs. **Watching** (`latest`, `stats`,
  `targets`, `controls`) reads what the broker already has: no device
  call, cannot start or stop anything, and safe to leave open on a
  beamline PC — a caller asking what is on screen must not be able to
  move the probe by asking. **Leasing** is exclusive control for the
  duration of a block, and the only way to acquire.
  A lease yields the *same* `Camera`, `Scanner` and `InstrumentController`
  protocol objects the device layer already defines, so every generator
  in `acquisition`, every `Session` recording and every analysis bridge
  works inside one unchanged. The broker decides *who* may call, never
  *what* they may call: the moment it grows acquisition verbs of its own
  there are two acquisition APIs to keep in step, and the property that
  the viewer is built *on* the scripting API rather than beside it is
  gone.
  Three decisions are worth stating because they are not the obvious
  ones. **A paused live loop is always restarted on release, with no
  opt-out** — the opposite of DigitalMicrograph, and for a reason that
  inverts the usual intuition: the beam is on regardless, that being a
  control outside this software, so a scan that is not scanning is a
  stationary probe putting the whole dose into one spot. Restarting is
  the conservative choice. That same fact fixes the **order** of a
  multi-target lease: targets are taken in `LEASE_ORDER` rather than
  argument order (which also removes the deadlock two clients could
  otherwise reach), and the scanner is taken *last* and released *first*,
  so the probe stands still only for the grant itself. And **contention
  is refused, not queued** — a queue invites two clients to each believe
  they are next, and a lease has no bounded duration for a queue to
  reason about; the honest answer is who holds it and why, which
  `DeviceBusyError` carries.
  A lease is granted whole or refused whole, and a refusal restarts
  whatever it had already stopped: leaving the scan dark in exchange for
  a camera the lease did not get would park the probe for nothing.
  Leases expire, because a notebook kernel that dies mid-lease would
  otherwise hold the beam forever, and the broker releases on a client
  disconnecting rather than waiting the time out.
  Implemented twice against one conformance suite — in process
  (`broker.local`), for the viewer and for tests, and over the existing
  `devices.rpc` wire (`broker.server`, `broker.remote`) for a notebook or
  a dashboard. The remote half reuses `RemoteCamera` and its siblings
  rather than reimplementing them, which cost `_RemoteDevice` two
  arguments (a lease id, and an injectable lock so several targets can
  share one connection) and `Call` one optional field. Frames from a
  *leased* device take the shared-memory path, since a lease means one
  caller by construction; watched frames stay on the pickle channel,
  which is the right trade for a dashboard tile and the wrong one for a
  33MB detector frame — a watcher that needs those should lease.
  `miainwoodpecker-broker` runs one over a device server and publishes
  where it is listening, since the port is the OS's choice and nobody can
  be told it in advance.
- **A scan pass can be stored, and acquired from the window.**
  `storage/passes.py` writes one `NXentry` per pass holding one `NXdata`
  per signal — `entry/data` the default and plottable one, the rest
  `entry/data_<name>`. NeXus allows an entry many `NXdata` groups and
  uses `default` to name the one to plot, so this is the vocabulary's
  own answer to "where does a second signal go" rather than a private
  convention.
  `NXem`'s `measurement/eventID*` hierarchy is designed for exactly this
  and was **not** adopted, for a costed reason rather than a lazy one:
  reaching it additionally requires an `NXem_instrument` carrying
  `ebeam_column` and `fabrication` under a `measurement` group, i.e.
  restructuring the entry every reader in this package and every file
  already written depends on. That is a migration, and it should be made
  once when the hierarchy is needed for its own sake — not smuggled in
  behind the first feature that could use it.
  **Streaming is structural.** `PassWriter` creates the file and its
  datasets first and hands them out through `destinations()`, to be
  passed as `scan_synchronised(into=...)`. The device fills the final
  on-disk datasets as it acquires, chunked one beam position per chunk
  so each write lands as a single chunk write. A test asserts the pass's
  array *is* the file's dataset, because a version that filled a buffer
  and then copied it in would pass any value-based check while doing
  exactly the work this avoids.
  `Session.reserve` exposes the naming so a caller that writes its own
  file still gets the session's index and slug; the file is created
  empty, which makes the name a reservation rather than a suggestion.
  **Acquire spectrum image (4D)** in the Scan group drives it, and most
  of that method is refusals: no session, no synchronised mode, no
  camera wired to the column, scanner or camera busy. That is the
  feature on every backend but the preview — a fabricated cube has the
  same shape as a real one, so naming the missing capability is the only
  outcome an operator can act on.
  Fixed while building it: the preview's `into` shape check validated
  only the navigation axes, so a wrong detector size reached the write
  loop and surfaced as an h5py broadcast `TypeError` naming neither the
  target nor the acquisition. It now checks the whole shape and says
  which of the two numbers is wrong.
  Known limits, stated rather than papered over: the grid is square
  (it waits on the target-area UI), the acquisition ran on the GUI
  thread until the entry above moved it off, and a stored pass appears
  in the Recordings list
  as `0 frames` because that list only understands frame stacks — a test
  pins that it does not *break* the list, which is the part that matters
  meanwhile.

- **The cross-device pass, and one device that really performs it.**
  `docs/adapters/spectrum-detectors.md` §2.3 named the missing concept
  and said it was "one missing concept, not three": a spectrum image
  collected alongside HAADF, multi-detector scanning, and 4D-STEM are
  the same absence seen from three sides. The unit that was absent is a
  **pass** — one traversal of the probe yielding a *set* of correlated
  outputs sharing one geometry and one identifier.
  `ScanPass` and `DiffractionStack` are that unit. A pass carries its
  image channels, its per-camera 4D datacubes and its spectrum images,
  and refuses to exist without an identity or without anything to
  identify. `DiffractionStack` is a type rather than a bare array
  because the array alone does not say which axes are which, and a
  caller that guesses wrong silently transposes a dataset that still
  analyses; its shape is navigation-first, matching both
  `Spectrum.navigation_shape` and py4DSTEM's `DataCube`.
  `SynchronisedScanner` is a **separate** protocol rather than two more
  methods on `Scanner`, for the reason `Instrument` already records: a
  `runtime_checkable` protocol's `isinstance` is all-or-nothing, so
  widening `Scanner` would make every adapter that cannot synchronise
  fail a check it passes today — testing for a capability instead of
  for conformance. Here the check and the capability are the same
  question, which is what makes `isinstance` honest.
  The same document rejected adding a bare `pass_id` to `Frame` and
  `Spectrum` as a correlation hint, because an identifier nothing
  establishes is a *claim* that two results share a pass — the shape of
  mistake `probe_position` already made here. So a `ScanPass` is
  produced only by `scan_synchronised`, one call to one device that
  really did traverse once; an adapter that cannot guarantee that must
  refuse rather than build one.
  The preview instrument implements it for real, which is the point of
  having built it: the diffraction pattern at each beam position is
  deflected by the local gradient of the specimen field, so a
  centre-of-mass across the datacube reconstructs that gradient. A test
  asserts the correlation, which means a centre-of-mass or DPC
  implementation run against this fixture can be *wrong* — where a cube
  of identical patterns would let any analysis "succeed". The
  nionswift-usim backend implements none of it, and that is not an
  oversight: `analysis/py4dstem_bridge.py` records the measurement
  showing it cannot produce scan-position-varying diffraction without
  the `HardwareSource` layer Phase 0 rejected.
  **Synchronisation is recorded, not assumed.** `ScanPass.scan_sync` is
  required with no default, from `SCAN_SYNC_MODES` — `detector` when the
  camera or analyser drove the column's scan input (the usual
  arrangement for a fast spectrum image, and what Gatan's GMS and Nion's
  Swift each expose), `scanner` when the column drove, `none` when
  nothing did. It is the same question `metadata["scan_sync"]` already
  asked of a spectrum map, promoted to a field because a pass exists to
  assert its outputs share probe positions and *how* that was arranged
  is the whole evidence. A default would have been this module guessing
  about hardware wiring; a detector-mastered acquisition and an
  unsynchronised one produce datasets of identical shape, and only this
  field tells them apart afterwards.
  **Pre-allocation and streaming are one mechanism, not two.**
  `scan_synchronised` takes `into`: destination arrays the caller owns,
  filled in place, so the data never moves twice — which is how the
  vendor SDKs work and the only sane shape for a cube measured in
  gigabytes. That looks like it conflicts with streaming a large pass to
  disk, and does not: `into` is defined by what it supports (a shape,
  and assignment at a beam position) rather than as `numpy.ndarray`, so
  an `h5py` dataset satisfies it. Chunked one beam position per chunk,
  the per-position writes land as single chunk writes and the cube is on
  disk as it is acquired, never whole in RAM. A test pins this by
  acquiring into a real HDF5 dataset and asserting the pass carries that
  dataset rather than a copy.
  Not yet done, and the next steps: the NeXus layout for a stored pass
  (the test above writes a bare cube, not a calibrated `NXdata`), an
  adapter that writes *through* the buffer so the write overlaps the
  next exposure rather than following the acquisition, and the
  target-area UI. The vendor specifics — which GMS and Swift calls take
  a caller-owned destination, and what each requires of the trigger
  wiring — are recorded here as the shape to look for, not as verified
  fact: neither has been exercised, and both need hardware or a vendor
  conversation.

- **Acquiring an image is now its own thing, separate from recording a
  series.** Until now the window could make exactly one kind of
  recording: a burst of N repeats from one device — a *time* series.
  That is a real acquisition, but it is not the everyday one, and it was
  standing in for two that the application could not express at all.
  **Acquire scan image** takes one pass of the probe and reads out every
  detector channel the scanner has, rather than the one currently on
  display. The pass happens either way, so the second detector costs no
  extra dose and no extra time, and the channels come out registered to
  each other by construction; the alternative is scanning the same area
  twice to get the channel you wish you had kept. The frames carry the
  shared `scan_pass_id` that `scan_frames` already established, so a
  reader can do per-pixel arithmetic between channels without inferring
  from timestamps whether the probe moved.
  **Acquire image** on a camera takes one exposure at that camera's own
  image exposure and binning, kept apart from the live view's. The two
  are different jobs — the feed runs short and often binned to stay
  responsive at thirty frames a second, and a kept image is worth a long
  unbinned exposure — and one shared pair of settings would force a
  choice between a usable live view and a usable acquisition. The new
  `acquisition.camera_image` generator restores the live settings
  afterwards, including when the consumer abandons it early, so one long
  acquisition cannot leave the feed crawling. Binning is offered from
  the camera's own `binning_values`, so a detector that only does 1×
  does not show a 4× it would refuse.
  Neither reads the "Frames" count beside it: an image is one
  acquisition whatever that says, or the two controls would silently
  interact.
  Building the exposure controls also surfaced an under-specified test
  double: `test_live_widget.py`'s `_FakeCamera` never implemented
  `binning_values`, `parameters` or `configure`, though the `Camera`
  protocol requires all three. It got away with it while nothing in the
  widget asked at build time. The fake was completed rather than the
  panel made defensive — a fallback for a camera missing half its
  interface would be shipping around a protocol violation.
  This is the first of three steps. Spectrum imaging and 4D-STEM — a
  scan combined with a camera or spectrometer readout at each beam
  position — remain unbuilt, and are blocked below this layer rather
  than by it: `analysis/py4dstem_bridge.py` records the measurement
  showing that the Nion simulator cannot produce scan-position-varying
  diffraction without the `HardwareSource` layer Phase 0 rejected.

### Fixed

- **Closing the preview window no longer ends in a traceback**, and
  `shutdown()` is now safe to call at any point rather than only the
  right one. `miainwoodpecker-preview` called `widget.shutdown()` after
  `napari.run()` returned, which is after Qt has destroyed the widget
  tree: the call died on `RuntimeError: Internal C++ object (QTimer)
  already deleted`. `viewer/app.py` had already learned this and carried
  a comment saying so; the preview entry point diverged from it and has
  been brought back into line.
  The traceback was the visible half. The damaging half was that
  shutdown aborted at its first statement, skipping the teardown that
  matters — the acquisition loops, the analysis workers, and the
  recording writer whose `finalize` is what leaves an operator a
  readable file rather than a truncated one. `shutdown()` is now
  idempotent, and each teardown step tolerates a widget Qt has already
  destroyed. Per step rather than around the whole method, so one dead
  widget cannot skip the steps after it; that is safe because each step
  stops its machinery before it touches a label. Only the
  "already deleted" flavour of `RuntimeError` is swallowed — a device
  refusing to stop raises the same class and is still worth hearing.
- **Shutdown stops every camera, not just the first.** `stop_camera()`
  with no argument acts on the first binding, and `shutdown` called it
  bare, so a two-camera instrument exited with its second camera still
  running — a device left held by a process on its way out. Found while
  fixing the crash above, in the same method.

- **The dock's lower sections are reachable again.** The panel was a
  plain vertical stack of four group boxes, so its *minimum* height was
  its natural height: measured on a 1409-pixel-high screen, the stack
  demanded 1499. Qt will not shrink a widget past its minimum, and there
  was nothing to scroll, so Devices and everything below it ran off the
  bottom of the display — not merely out of view but unreachable, with
  no resize, drag or scroll that would bring them back.
  The stack now lives in a `QScrollArea`, which drops the panel's
  minimum height from 1499 pixels to 72 and makes reachability
  independent of what is open. Each of the top-level groups also folds,
  behind the same disclosure triangle the per-device sections already
  used — that widget moved to `viewer/panels/sections.py`, since it is
  no longer a device-only idea.
  The two are deliberately not the same fix. Folding is for putting away
  groups you are not using; scrolling is the guarantee. A layout that
  only fitted when folded would put the same content out of reach the
  moment an operator opened it, and several groups open at once is the
  ordinary case — watching a camera while a scan runs.

### Changed

- **Session context moved into a dialog, and a status bar took over the
  two facts worth watching.** Where data goes, who is on the instrument,
  what the sample is and the shift's standing notes are set once at the
  start of a session and then left alone, but they were spending four
  form rows and two text boxes in the dock to say so — on the panel that
  did not fit on the screen.
  They are now behind **Session settings...** at the top of the
  Recordings group, and the two continuously useful facts — the
  destination and the free space — moved into the main window's own
  status bar, to the left of napari's "Ready". That is the line a user
  already reads for status, rather than a second status line of ours a
  few hundred pixels above it, and being outside the dock entirely it
  cannot be scrolled out of view — which is exactly what used to happen
  to it. The path is elided from the left, because every session under
  one root shares its leading components, and it takes the bar's spare
  width so it elides as little as it has to.
  The fields are divided by vertical rules, drawn with an explicit
  colour rather than as plain sunken frames — Qt draws those from the
  palette's shadow colours, which on napari's dark theme sit within a
  few percent of the bar behind them, so the dividers were present and
  invisible. A field with nothing to say takes its rule with it: with no
  session the "Saving to" label disappears and the line reads simply
  `No session - data is not being kept`, and the free-space field is
  absent rather than empty, since a divider beside a blank reads as a
  value that failed to load instead of one that does not apply.
  Both are labels rather than controls, deliberately: a status bar that
  quietly doubled as a button would mean the session directory could be
  changed from two places, one of them invisible until someone happened
  to click the text. `insertWidget(0, ...)` rather than `addWidget` puts
  them left of "Ready"; that area is normally a poor place for anything
  lasting, since `showMessage` hides widgets added to it, but napari
  draws "Ready" with its own `StatusBarWidget` and never calls it. They
  are removed again on `shutdown`, or a second widget docked into one
  viewer would stack a second copy of every label.
  Two things stayed behind, for the same reason the rest moved. The
  **note for the next recording** is answered per burst rather than per
  shift, so it sits with the recording controls; and the live recording
  state (running, stop, what has been written) joined the **Recordings**
  group, which was already named for recordings — the alternative was a
  second group whose name differed from that one's by a letter.

### Added

- **An in-process preview instrument, so the UI can be iterated on
  without a device server** (`miainwoodpecker-preview`, implemented in
  `src/miainwoodpecker/viewer/preview.py`). The viewer could already open
  against synthesised frames via `--server-module camera_server`, and
  that path stays the honest end-to-end exercise: real frames over a real
  socket from a real subprocess, proving the IPC, the handshake and the
  shutdown. This one deliberately gives all of that up. The scanner,
  cameras and instrument are ordinary Python objects in the viewer's own
  process, which buys three things the subprocess path cannot.
  Startup is an import — no server to spawn, no port to bind, no
  handshake to wait out — so the edit-run loop on a panel's layout is as
  long as napari takes to draw. The window is reachable in whatever shape
  is needed: `--no-scan`, `--no-camera`, `--cameras 2`, and `--controls`
  to publish a subset, so the Instrument and Devices panels' "an
  unpublished control gets no row" branches are reachable without owning
  a microscope that lacks a blanker. And with no transport underneath, a
  widget that misbehaves here misbehaves in code that was just edited.
  The controls are wired to the image on purpose: blanking collapses the
  signal, defocus damps the contrast, and moving the stage moves the
  field of view. A preview whose dials did nothing would let a broken
  Instrument panel — a signal never connected, a setter called on the
  wrong object — look exactly like a working one, which is the panel this
  exists to iterate on. The specimen is an atomic-scale lattice
  (0.3 nm columns, ~50 across the panel's default 15 nm field of view)
  rather than a smooth gradient, because judging a colormap or an
  autocontrast pass against two grey blobs proves nothing.
  It reports its backend as `preview`, never `simulated`. The panel's top
  line shows that verbatim, so a screenshot taken from this window cannot
  be mistaken for one taken from the microscope simulator, let alone from
  an instrument. Requires the `viewer` extra and nothing else — no vendor
  SDK, no `device` extra.
- **A camera probe, so "nothing shows up" names its own cause**
  (`scripts/probe_cameras.py`). A USB microscope that does not appear in
  the viewer has four possible reasons, and from the viewer they look
  identical: the device never enumerated; it enumerated but this process
  may not open it; it opens fine but is not index 0 (a laptop's built-in
  webcam usually is); or it opens fine and the viewer was pointed at
  `nion_server`, which serves no USB camera at all, so no microscope can
  appear no matter what is plugged in.
  The script asks the operating system what it sees — per platform,
  because the three do not agree on what a camera is — and then asks
  OpenCV to actually open each candidate and read a frame. Opening is
  the only honest test: a device can be listed and still refuse to open,
  held by another application or not permitted to this process, and that
  is exactly the distinction a listing cannot make. It finishes by
  printing the command that would serve whatever worked, including both
  flags that default to something else.
  Two details separate an answer from a red herring. "Nothing opened"
  branches on whether the operating system saw anything, so a device
  that never enumerated is not blamed on group membership. And OpenCV's
  per-index backend warnings are silenced, because probing absent
  devices is what this does and a wall of them buried the result.
  Read-only with respect to the instrument, and stdlib-only except for
  the open-a-frame half — without OpenCV it still runs and says so,
  since "the operating system can see it" is already worth knowing.
- **A read-only instrument survey to hand to a facility**
  (`scripts/superstem_survey.py`, with `docs/superstem-survey.md` as its
  runbook). Several design decisions rest on guesses about instruments
  nobody here can reach: whether Nion drives SuperSTEM 2's Enfina
  (`eels_camera` plus a `ZLPoffset` control would mean the spectrometer
  is already supported with no Gatan code at all), whether an SU9000II
  has the `Mf*` external-control modules, and which SIMPLON version and
  configuration keys a HERMES ELA actually publishes.
  The script asks those questions and nothing else. Its safety is
  structural rather than careful, which is what makes it handable: the
  Nion section reads a registry Nion Swift populated and **never loads a
  device plug-in**, because this project's own device server does load
  them and a second process doing so would claim hardware the running
  Swift already owns; the DECTRIS section is `GET`-only, so it never
  arms, triggers, disarms or configures, and is safe to run
  mid-experiment; the Hitachi section resolves modules with
  `importlib.util.find_spec`, which locates without executing, because
  importing a vendor control module may open a connection to the column.
  Each of those three properties is pinned by a test rather than left as
  intent — the `GET`-only one asserts at the transport, against the
  simulated control unit, so a probe added later cannot quietly break
  it.
  Stdlib-only and parses on **Python 3.7**, the floor Gatan Microscopy
  Suite sets by embedding it. A `--check` mode reports the interpreter
  and what it could answer while touching nothing, because for the Nion
  and Hitachi sections the interpreter matters more than the machine:
  run from the wrong Python, an empty registry or a missing `MfExtCont`
  is a confident wrong answer rather than an error.
- **A hosted download page for that survey script**
  (`scripts/build_survey_page.py`, its template
  `scripts/superstem_survey_page.html.in`, and the link recorded in
  `docs/superstem-survey.md`). The runbook assumed whoever runs the
  survey can get the script out of this repository; on an instrument
  control PC that is the wrong assumption, since there is often no git,
  no GitHub account and no route to a package index. The page carries a
  download button, the full source to read first, and a condensed
  runbook.
  It **embeds** the script rather than linking to it — which is what
  lets it work on an isolated machine, and is also the thing that could
  go stale. The builder is how it does not go stale silently: it reads
  `scripts/superstem_survey.py`, escapes it into the template, and
  stamps the revision into the page footer, so the downloaded bytes are
  the repository's bytes by construction rather than by anyone
  remembering to re-paste them. What the builder cannot do is publish,
  so a changed script still needs the page republished, and the footer
  revision is how to tell which one is live.
  One wrinkle is recorded rather than left to be discovered at the
  instrument: the file downloads as `superstem_survey.txt`, because the
  host permits only an allowlist of extensions and `.py` is not on it.
  **No rename is needed** — Python runs a file whatever its extension —
  and the page says so in those words, because a scientist handed a
  `.txt` will otherwise reasonably conclude it is broken.

### Changed

- **The camera server finds its own cameras.** With no `--plugin`, the
  hardware backend now probes the capture indices, keeps every one that
  delivers a frame, and serves all of them — each in its own section,
  as `camera`, `camera:2`, `camera:3`. It used to open index 0 and
  nothing else.
  That default was wrong in the common case rather than merely
  conservative: a USB microscope's index is not knowable in advance and
  is usually *not* 0, because a laptop's built-in webcam takes it. So
  "plug it in and run the viewer" showed the webcam, which looks exactly
  like a broken microscope and sends an operator after a cable.
  **Naming still beats finding.** `--plugin` given is taken literally,
  in order, and nothing is discovered — an operator who says which
  camera to open has answered the question, and quietly adding a webcam
  they did not ask for would be worse than useless on an instrument. It
  also remains the only way to reach a device no probe could find: a
  path, or a video file replayed as a fixture. The simulated backend
  never probes, since it has nothing to discover and needs nothing
  installed.
  Two details are load-bearing. A frame is the test, not an open — a
  device that opens and delivers nothing is an ordinary outcome behind
  an underpowered hub, and serving it would put a section in the viewer
  that can never show an image. And the scan stops after three
  *consecutive* misses rather than one, because a laptop whose built-in
  camera is disabled or already held leaves exactly that hole at index
  0, and stopping there would miss the microscope at index 1.
  Finding nothing is its own diagnosis rather than a failure to open
  `"0"`: the message says what was searched and gives the platform's
  likely reason, because there is nothing to correct in the command
  line.
- **A device server's target list is now the server's to decide.** The
  client used to allocate one localhost port per name in
  `rpc.TARGET_NAMES` and pass them *positionally* on the server's
  command line, which made that tuple part of the wire protocol: a
  server could serve only names this client already knew, the tuple was
  append-only, and a reordering on either side bound the scanner's port
  to the EELS camera without `strict=True` noticing (the count still
  agreed). A vendor with three detectors, or a SEM with SE and BSE and
  no camera, had to map onto Nion's list, so a file's `device_id` was
  honest while its target name was a fiction.
  The client now passes **one** port, `--instrument-port`. Every other
  target binds on port 0 — the OS choosing — and the server reports
  where each one landed, with its kind and a device label, in
  `describe()`'s `endpoints` map. The client connects to what it is
  told. `camera_server` uses that immediately: `--plugin 0 --plugin 1`
  serves a webcam and a USB microscope at once, as `camera` and
  `camera:2`, and the second is reachable despite being a name the
  client could not have allocated a port for.
  Which handle a target gets is read from the endpoint's `kind` rather
  than guessed from its name, so a name written after this client
  shipped still arrives as a camera. `TARGET_NAMES` survives as the list
  of names that get a *named attribute* on `RemoteInstrumentDevices`;
  it is no longer append-only and no longer reaches any command line.
  Binding at port 0 also closes a race rather than only a limitation: a
  client-allocated port is probed free and bound seconds later, which is
  what `PORT_UNAVAILABLE_EXIT_STATUS` and the respawn retry exist for.
  One port instead of six shrinks that window; a port the OS assigns at
  bind time does not have one at all.
  **This is a flag day for out-of-tree adapters.** A server written
  against the positional shape will fail argument parsing against this
  client, and there is no version negotiation in the protocol to soften
  it — a real limitation, and the honest reason to do this while there
  are no adapters in the field rather than after. The migration is about
  ten lines, and `tests/unit/test_out_of_tree_server.py` makes exactly
  those edits in a complete vendor-free server. The attach path
  (`attached_instrument()`, `gatan_bridge`) is unchanged and still
  carries an explicit port per target: the client is not the end that
  binds there, so it cannot learn a port the far end chose.
- **EDX and EELS run together on SuperSTEM 2** — confirmed by the
  facility, and they do not physically block one another. This was an
  open question in `docs/adapters/spectrum-detectors.md` about dose,
  dead time and whether the Enfina's acquisition takes the scan; the
  blocking half is answered outright, so simultaneous EDX + EELS is a
  workflow to support rather than a configuration this project may
  reject. The consequence for the design recorded there is that the
  **pass** concept now has a confirmed user rather than a projected one:
  two spectrometers of different kinds reading out during one traversal
  is the correlated-output set a pass exists to group, and
  `metadata["simultaneous_with"]` has a device that genuinely knows. It
  does not change the recommendation — the device shape still ships
  first — but it removes "we may never need this" as a reason to defer
  the pass indefinitely.
- **The simultaneous multi-channel scan**, which is what a scanned
  instrument actually does: one pass of the probe, every enabled
  detector reading out at once.
  `Scanner.scan_frames(parameters, channels)` returns one frame per
  requested channel from **one** pass, so two channels cost one pass of
  dose rather than two, nothing drifts between them, and DPC / iDPC /
  centre-of-mass differences are taken between segments at the same
  probe position. `scan_frame` is untouched; the new call is additive,
  and `acquisition.multichannel_scan_series` is its series form.
  Frames of a pass carry `scan_pass_id` (the identity of that one
  traversal) and `simultaneous_channels` (the siblings that shared it).
  **The identity is produced by `scan_frames` and by nothing else** —
  `scan_frame` attaches neither key, so the id can never claim an
  acquisition that did not happen, which is exactly why a bare `scan_id`
  was refused before (this project having been bitten by
  `probe_position`, an identifier nothing established). The old
  `Frame` docstring premise that "a second channel is a second pass of
  the beam" was false on real hardware and is now corrected there.
  The Nion adapter uses the vendor's own mechanism rather than an
  emulation — `set_channel_enabled`, one `start_frame`, `read_partial`
  until complete, which is the loop `scan_base.ScanAcquisitionTask`
  runs, and which returns one data element per enabled channel — and
  verifies what the device did (no bad frame, the frame number did not
  move, every requested channel reported) before stamping a shared id.
  Nion mints a per-frame `uuid4` for the same purpose one layer up
  (`stem.scan.scan_id`), so the concept is the vendor's; the rule that a
  call must establish it is this project's.
  Across the device-server boundary a pass crosses as **one stacked
  block** in the source's existing shared-memory segment
  (`SharedFrameSetRef`): the reused-segment design allows exactly one
  publish per request/response cycle, so N publishes would overwrite
  each other, N segments would double every source's `/dev/shm`
  footprint, and N sequential replies would make the server hold a
  finished pass between calls. Below the shared-memory threshold a pass
  stays on the pickle path as an ordinary list. Storage needed no
  change: `NexusWriter` persists each frame's metadata whole, so a
  recorded series says which frames shared a pass with no new NeXus
  layout invented for it.
  `scan_frames` is part of the `Scanner` protocol rather than an
  optional extra, so an out-of-tree adapter written before it no longer
  satisfies `isinstance` and must add it — twenty lines, as
  `tests/unit/test_out_of_tree_server.py`'s example server shows.
- Process isolation for the viewer's analysis buttons, **on by
  default** (opt out with `MIAINWOODPECKER_ANALYSIS_ISOLATION=inprocess`).
  HyperSpy, eXSpy, py4DSTEM and LiberTEM run in a lazily-spawned worker
  (`python -m miainwoodpecker.analysis.worker`) that reuses the device
  layer's `Call`/`Result` protocol, its dispatch loop, and its
  reused-shared-memory transport. Buys crash containment, a thread
  budget set *before* `import numpy` (which `analysis/threads.py`
  documents as the one thing its runtime cap cannot do), and a
  precondition for separate dependency environments. Measured at about
  0.8 ms per megabyte moved and nothing at all when the input is a file.
- `docs/analysis-isolation.md`: what HyperSpy actually buys this project
  (ten call sites, one of which computes), a capability/licence table
  with permissive alternatives, and a precise statement of the licence
  question — including why isolation cannot answer it, since the
  documented `load_as_*` API returns live library objects by design. It
  shipped off by default so the decision would be the project owner's,
  and the owner made both calls: isolation on, for crash containment —
  a segfault in a native analysis library used to take the session and
  any in-flight recording, and now costs one result and a worker
  restart — and the licensing posture left as it is, expressly not the
  reason for the switch. The opt-out is the whole word `inprocess`, so
  a typo cannot silently disable the protection.
- `scripts/analysis_ipc_benchmark.py`, the analysis-side counterpart to
  `scripts/ipc_overhead_benchmark.py`. Interleaves the two transports
  call by call, because measuring them sequentially produced a
  reproducible 400–560 ms "overhead" that was the container slowing down.
- `miainwoodpecker.analysis.operations`: the three analyses the viewer's
  buttons run, as plain functions taking an `AnalysisInput`, so the
  in-process and isolated paths call one implementation rather than two.

- Device server backend selection (`--backend {simulated,hardware}`, repeatable
  `--plugin MODULE`, with `MIAINWOODPECKER_BACKEND` /
  `MIAINWOODPECKER_HARDWARE_PLUGINS` defaults), so a real instrument can be
  driven without editing code. Real-hardware discovery uses Nion's own
  `PlugInManager`/`Registry` mechanism; `remote_simulated_instrument()` keeps
  its exact signature.
- Graceful shutdown with an instrument park (beam blanked where a blanker
  exists), with SIGTERM demoted to a bounded-timeout fallback.
- Vendor-neutral `InstrumentController` protocol: stage position, defocus, and
  beam blanker, in operator units. `focal_series(..., instrument=...)` now
  sweeps real defocus and records both requested and read-back values.
- Device-server liveness: `instrument.check_health()` distinguishes responsive,
  exited, and unresponsive; device calls raise `RemoteConnectionLostError`
  naming the signal once the process is gone. Structured server-side logging
  via `MIAINWOODPECKER_DEVICE_LOG_LEVEL` / `MIAINWOODPECKER_DEVICE_LOG_FILE`,
  deliberately absent from the frame path.
- `Session`: a recording directory, collision-free naming, operator/sample/notes
  context, and enumeration — wired into the viewer so acquired data can actually
  be kept. Recording and loading run off the GUI thread.
- Session read-back: open recordings from the session or an arbitrary path, and
  run the analysis actions against a file on disk instead of a fresh burst.
- Per-axis, per-acquisition frame calibration (`storage/calibration.py`):
  real space (nm), reciprocal space (1/nm), energy (eV/meV), angle (mrad), and
  an honest uncalibrated (pixel) state, written as real NeXus units and carried
  into the HyperSpy and py4DSTEM adapters. Includes
  `load_as_hyperspy_spectrum` → `Signal1D` for flattened spectra.
- `NexusWriter`: `flush()`, `dtype=`, and `sample=`/`user=`/`notes=` writing
  real `NXsample`/`NXuser`/`NXnote` groups.
- NXem schema validation as a CI job (`hatch run validate:schema`), using
  `pynxtools`' programmatic API because `pynx validate` exits 0 even when it
  reports a file invalid.
- `docs/hardware-validation-checklist.md`: the ordered procedure for what
  cannot be verified against the simulator.

- Two user-facing guides, cross-linked as the documentation's front door:
  [Using the viewer](docs/using-the-viewer.md) for operating from the
  screen (with a translation table for Nion Swift and DigitalMicrograph
  habits) and [Scripting and automation](docs/scripting-and-automation.md)
  for driving the same capabilities from Python, including what it takes
  to put an AI agent at the controls.

- `docs/pre-hardware-work.md`: the counterpart to the hardware checklist —
  what can be built before an instrument is available, sourced from the
  device-layer contracts in Nion's own public acquisition test suites.
  Records that the calibration plumbing §7 lists as missing already exists
  on the GPL side (`calibration_controls` resolved by
  `camera_base.build_calibration`, with real values published by the
  simulator), so it needs neither hardware nor a reimplementation.

- Camera axis calibration resolved from the instrument. A Nion camera
  publishes the *names* of the instrument controls holding its calibration
  rather than the values; the device server resolves them with Nion's own
  `camera_base.build_calibration` and puts per-axis
  `{kind, scale, offset, units}` into the frame metadata as plain data.
  Ronchigram frames arrive with radian axes centred on the optic axis and
  EELS frames with an eV axis — and the dispersive axis is now the one the
  *device* reports, so `dispersive_axis="x"` is no longer an assumption to
  confirm on hardware.

- Every acquired frame now carries the acquisition metadata Nion's own
  required-metadata tests enumerate: device id, gapless `frame_index`,
  high tension, defocus, beam current, and per device either channel and
  scan geometry (rotation, centre, flyback, derived line and frame times)
  or the camera's type, name, and gain. The vocabulary is documented on
  `Frame`. The accelerating voltage is additionally written as
  `NXsource.voltage`, the one piece of it NeXus specifies a home for.

- Camera exposure and binning control: `CameraParameters(exposure_ms,
  binning)`, `Camera.binning_values`/`parameters()`/`configure()`, through
  the device server and over IPC. `configure` reports what the device took
  rather than echoing the request, and refuses a binning the camera does
  not advertise instead of rounding it. Binning multiplies the calibration
  scale, and the binning a *frame* reports is recovered from its shape, so
  a camera reconfigured mid-acquisition cannot mislabel the frame already
  in flight.

- `energy_offset_series`: step the spectrometer's energy offset across a
  series of EELS frames, recording the read-back offset beside the request
  and restoring the original afterwards — the acquisition half of Nion's
  multiple-shift EELS acquire. The camera is stopped around each step,
  because a running camera returns a frame generated before the control
  changed, which would mislabel the whole series by one. Brings a fourth
  control to `InstrumentController` (`energy_offset_ev`), which the
  camera's own calibration already tracks, so the recorded energy axis
  follows the sweep for free.

- Session context adopts Nion Swift's own documented vocabulary:
  `instrument`, `site`, `sample_area`, and `task` join `operator`,
  `sample`, and `notes`. Four map onto real NeXus fields — sample and
  sample area to `NXsample`, the operator to `NXuser`, the microscope to
  `NXinstrument/name`, which had been sitting empty — and are
  schema-checked in CI. `site` and `task` deliberately do not: no NeXus
  field means what they mean, and an approximate one would be a
  confidently wrong claim.

- `remote_instrument(server_module=...)`: the client can launch a device
  server it did not ship, so a vendor adapter can be an out-of-tree
  package rather than a fork. `tests/unit/test_out_of_tree_server.py`
  writes a complete vendor-free server and drives the whole client
  against it with no `device` extra installed, which is both the
  regression test and the specification an adapter writes against. The
  startup diagnostic now names the module it failed to launch.

- A detector-only device server is now supported: `scanner` is optional
  on `RemoteInstrumentDevices`, `cameras()` enumerates what is actually
  served, and the live viewer says so plainly instead of failing deep. A
  Direct Electron, DECTRIS, or Hamamatsu camera driven through its own
  SDK has no scan unit, and `connections["scanner"]` used to be
  unconditional — so "vendor-neutral" quietly meant "must have a scan
  unit shaped like Nion's".

- `docs/vendor-support.md`: what Thermo Fisher, JEOL, Zeiss, Hitachi and
  Bruker actually expose, what the direct detector vendors expose
  (Direct Electron, DECTRIS, Hamamatsu, Merlin, ASI, Gatan), what
  commodity cameras need (pymmcore reaches every UVC microscope in one
  adapter; a DSLR body is its own small gphoto one), and costed tasks for
  each. Also records why every adapter is a subprocess even where no
  licence requires it, and the one place the framework is still
  Nion-shaped — the device target names are a fixed positional tuple —
  and why that redesign should land with the second column adapter rather
  than before it.

- `docs/analysis-parity.md`: every analysis Nion Swift offers — roughly
  ninety operations across `nionswift`, `nionswift-eels-analysis`,
  `nionswift-experimental` and the instrumentation kit — mapped onto
  HyperSpy, LiberTEM and py4DSTEM, with the genuine gaps costed and the
  ones not worth porting argued rather than listed. Closes the last open
  Phase 4 item. Three findings change what that item meant: `niondata` is
  **Apache-2.0**, so Swift's whole core processing menu is a dependency
  declaration on the MIT side rather than fifty ports (four packages
  installed, against HyperSpy's ~35 and LiberTEM's ~102, and it runs
  standalone on plain NumPy arrays); **HyperSpy 2.x contains no EELS** —
  it moved to `exspy` at the 2.0 split — so the `analysis` extra covers
  none of Swift's EELS menu, which is the largest real gap and is this
  project's rather than Swift's; and only five gaps are genuinely
  Swift-specific and worth porting, at 9–15 days in total. Also records
  that `hyperspy` and `py4dstem` are themselves GPL-3.0 and imported
  in-process, which §6 does not currently speak to.

- **A device-layer shape for spectrum-producing detectors.** `Spectrum`,
  `SpectrumParameters` and the `SpectrumDetector` protocol in
  `devices/interface.py`; a `spectrum_detector` RPC target; a simulated
  EDX device server (`devices/spectrum_server.py`, needing nothing
  installed); NeXus storage in `NXspectrum`'s layout
  (`storage/spectra.py`); and `analysis.hyperspy_bridge.load_as_eds_signal`,
  producing an eXSpy `EDSTEMSpectrum` with a real energy axis. Designed
  against the two detectors actually fitted at SuperSTEM — a Bruker
  XFlash 6T-100 and an Oxford Ultim Extreme — without being an adapter
  for either. See `docs/adapters/spectrum-detectors.md`.

  **A spectrum is its own type rather than a `Frame` with a 1D array**,
  and the deciding reason is the calibration model: `FrameCalibration` is
  exactly two axes named `y`/`x`, so a one-axis spot spectrum and a
  three-axis map would each have to lie about one. The false economy
  would have been a document-only invariant on `Frame` plus a rank branch
  in `NexusWriter` choosing between two different NeXus layouts.

  **Storage placement was measured, not read.** `NXem` documents
  `NXspectrum` only under `measurement/eventID*/spectrumID*`, and putting
  it there fails validation for the same reason `ebeam_column` did. Four
  layouts were validated with `pynxtools`; the one that passes is
  `NXdata` at `entry/data` in `NXspectrum`'s field names beside an
  `NXdetector`. `NXfluo` was rejected on evidence rather than taste — it
  requires `NXsource/probe = "x-ray"` and a monochromator wavelength, and
  electron-excited EDX has neither. There is no `NXxrf` in the
  definitions at all.

  **`load_as_hyperspy_spectrum` now reads both storage layouts**, so an
  EELS camera stack and an EDX recording reach one `Signal1D` through one
  function, dispatching on what the file holds rather than on what the
  caller believes. EELS behaviour is unchanged and asserted so. That
  follows from the physics: EELS disperses onto a *camera* and arrives as
  a 2D frame, EDX is natively 1D, and both are a spectrum by the time
  anyone analyses them.

  **`exspy` joins the `analysis` extra.** HyperSpy 2.x moved its EELS and
  EDS classes out; measured on 2.4.0, `hs.print_known_signal_types()`
  returns an empty table, so `set_signal_type("EDS_TEM")` silently leaves
  a plain `Signal1D`. An extra that could not load an EDS signal would be
  claiming something false.

  **No `scan_id` was added.** Concurrency composes already — each target
  has its own connection and thread, and a test shows the detector
  integrating while another target is driven — but *correlation* does
  not, and no transport work fixes it. An identifier nothing establishes
  is a claim, which this project has been bitten by before
  (`probe_position`, accepted and echoed and silently dropped).
  `metadata["simultaneous_with"]` is in the vocabulary instead, absent by
  default, meaning nothing claimed.

  `TARGET_NAMES` gained `spectrum_detector` immediately before
  `instrument`, so every existing name keeps its argv position and
  `instrument` stays last. That is the minimal change, not an endorsement:
  the tuple is now Nion's device list *plus a detector class Nion does not
  have*, which is the clearest evidence yet that a fixed positional tuple
  is the wrong mechanism.

- **An EELS analysis path: `analysis.hyperspy_bridge.load_as_eels_signal`.**
  The project had none, on an instrument class where EELS is often the
  reason the instrument exists — `docs/analysis-parity.md` found the
  cause (HyperSpy 2.x contains no EELS classes; they moved to eXSpy at
  the 2.0 split) and eXSpy was already in the `analysis` extra from the
  EDX work. An EELS camera recording now reaches an
  `exspy.signals.EELSSpectrum`, with the recording's own energy axis,
  through the same shared `load_as_hyperspy_spectrum` loader an EDX
  recording uses. Exactly the thin layer `load_as_eds_signal` is, and
  the two now refuse each other's on-disk layout — once loaded a
  spectrum is a spectrum, so nothing downstream would catch eXSpy
  fitting X-ray lines to electron energy losses or ionisation edges to
  X-ray lines.

  **The energy axis is normalized to eV, and that is not cosmetic.**
  eXSpy's EDS code validates its axis unit (`_get_line_energy` takes eV
  or keV and raises otherwise); its EELS code checks nowhere while
  assuming eV everywhere — tabulated edge onsets in eV,
  `align_zero_loss_peak`'s ±3 eV subpixel window,
  `kramers_kronig_analysis` in eV. This project's vocabulary also admits
  meV (the natural unit for a monochromated vibrational spectrum), so the
  exact within-kind conversion happens on this side or not at all.

  **Two eXSpy items are deliberately left unset**: the convergence and
  collection semi-angles. Nothing here records either — the collection
  angle comes from the spectrometer entrance aperture and camera length,
  which no device reports, and the only convergence angle in the whole
  stack is a control that exists in usim and in no other Nion package,
  so reading it would dress a simulator detail as an instrument
  convention. Absent is also the safe state: eXSpy checks for exactly
  these and *refuses* the operations that need them, where a plausible
  wrong angle would have produced a number that looks like a result.
  Set, from what the recordings do carry: `beam_energy` (volts→keV),
  `beam_current` (amps→nA), and `Detector.EELS.exposure` (ms→s).

  **The axis is proved end to end against the simulator, not against our
  own array.** `tests/integration/test_eels_round_trip.py` sweeps the
  spectrometer with `energy_offset_series` and asserts that eXSpy finds
  the zero-loss peak at 0 eV in every step while the peak itself moves
  160 channels across the detector. The zero-loss peak is at zero by
  definition and the simulator plots it there independently; an adapter
  that lost the offset would report +100/+60/+20 eV, one that halved the
  dispersion would report −50/−30/−10 eV (verified by mutating the
  adapter), and one that lost the calibration would report a channel
  index. Only carrying both numbers through puts it at zero three times.

  **No viewer button**, and that is a decision. The analysis buttons act
  on the viewer's single camera, which `_choose_camera` resolves to the
  **Ronchigram** camera wherever there is one — so the button would
  refuse every click on the default configuration. And every EELS
  operation past loading needs a parameter an operator must choose
  (`estimate_thickness` raises without a threshold or a ZLP; background
  removal needs a fit window; mapping needs edges), while the one with
  usable defaults returns a recalibrated copy of the input rather than
  anything to draw. Reasoning recorded in `docs/analysis-parity.md`.

- **The spectrum server's playback is `--backend replay`, and
  `--backend hardware` refuses.** Playback was originally the `hardware`
  backend, honestly labelled — every spectrum carried `backend: "replay"`
  and the file it came from. That is not enough. `viewer/app.py` names
  the two failures its backend selector exists to prevent: driving a
  microscope you meant to simulate, and *believing you are on hardware
  when you are not*. A `hardware` backend that opens a file is the
  second, and per-spectrum metadata does not undo it — by the time anyone
  reads that metadata the session has already happened. `hardware` is
  still accepted by the parser, so asking for it gets a sentence saying
  there is no in-tree vendor backend, where an adapter goes, and what to
  use instead — rather than an argparse error. It refuses even when
  handed a perfectly good recording, which is the case the rename exists
  for and which a test pins.

- **Anisotropic binning: investigated, specified, deliberately not
  built.** EELS is run with vertical binning to trade dynamic range
  against SNR, and `CameraParameters.binning` is a scalar that cannot say
  so. The reason for not fixing it here is a measurement rather than
  scope: Nion's `CameraFrameParameters` has no per-axis binning at all —
  `get_expected_dimensions(binning)` and `build_calibration(..., binning,
  ...)` both take a scalar multiplying both axes — and what Nion offers
  for "bin vertically" is `processing = "sum_project"`, a full projection
  to 1D that its own tests mark as sequence/SI only. So a `(y, x)` tuple
  would be a field the only adapter behind it must refuse, which is the
  "vendor-neutral in name only" failure `ScanParameters.fov_nm` already
  warns about, and it would break `_binning_of(shape)`, which recovers a
  frame's real binning by matching `get_expected_dimensions` per scalar
  factor — the thing that keeps an in-flight frame correctly labelled.
  The specification models the *readout mode* instead, and routes a
  projected readout into `SpectrumWriter` so it lands in the same
  `NXspectrum` layout as EDX. 2–4 days, spec in
  `docs/adapters/spectrum-detectors.md` §6.

- **`remote.attached_instrument()`: drive a device server this client did
  not launch.** The subprocess rule has one structural exception — an
  adapter whose SDK exists only inside another running application cannot
  be spawned by us. Gatan is the first case (GMS 3's Python cannot be
  executed from outside DigitalMicrograph); anything living on a vendor's
  own control PC is the general one. Both socket directions are supported
  (`ACCEPT_TRANSPORT`, we listen; `CONNECT_TRANSPORT`, the bridge
  listens and the recommended default, since GMS outlives many client
  runs), sharing the whole existing protocol, authkey handshake, device
  contracts and `RemoteInstrumentDevices` shape. `AttachInvitation`
  publishes the rendezvous as a `0600` JSON file plus printed operator
  instructions, with the authkey only in the file.

  Liveness is deliberately *weaker* rather than faked: there is no
  `Popen`, so a new `SERVER_DISCONNECTED` health state reports what is
  actually known instead of reusing `SERVER_EXITED`, and attached errors
  name the bridge's origin and point at its log rather than inventing an
  exit status. Teardown is graceful with a per-device fallback and no
  forced terminate — killing the peer would kill DigitalMicrograph
  mid-acquisition. Shared memory is not used on attached links, because
  the peer may be on another machine. All process knowledge moved behind
  `_ServerLifecycle`; the spawn path's behaviour, messages and states are
  unchanged.

- **`rpc.COMPATIBLE_PICKLE_PROTOCOL`** caps outgoing calls on attached
  links at pickle protocol 4. `multiprocessing.Connection.send` pickles
  with the *sender's* default protocol — 5 since Python 3.8 — while GMS's
  embedded interpreter is Python 3.7, whose `HIGHEST_PROTOCOL` is 4. Every
  `Call` would have been unreadable and every `Result` fine, which
  presents as a broken server rather than a version mismatch. Found by
  reading CPython's source rather than by running it, since the failure
  needs the vendor's interpreter to appear at all. The authkey handshake
  is already cross-version safe: `_create_response` keeps the legacy MD5
  path for an unprefixed challenge.

- **`devices/gatan_bridge.py`** — a device server that runs *inside*
  Gatan Microscopy Suite, with a `simulated` backend that needs no Gatan
  software and is what CI exercises. DM-Script command names are
  constructor parameters with placeholders rather than hard-coded
  constants, because the imaging-filter energy-offset commands could not
  be verified from this environment and guessing them into the source
  would look like knowledge.

- **`docs/adapters/gatan.md`**, and a correction to this project's own
  recorded reasoning. `vendor-support.md` said a Gatan adapter "inverts
  the topology … a bridge running inside DM that connects *out*". Only
  half of that holds: what inverts is *ownership*, not direction.
  DM-Script has both `TCPSocketBind` and `TCPSocketConnect`, SerialEM's
  plug-in has listened inside DM for twenty years, and a published
  DM-SDK/ZeroMQ bridge already exists (Lei, Weber, Clausen & Wilbrink,
  *M&M* 30(S1), 2024). Direction is a firewall question.

  The document also leads with the finding that matters most to the
  facility that prompted it: **a Gatan spectrometer on a Nion column is
  probably already supported.** Nion's instrumentation kit models an
  optional `eels_camera` on the STEM controller, which `nion_server.py`
  already reads, and already maps `energy_offset_ev` onto Nion's
  `ZLPoffset` — the spectrometer drift-tube offset. Nion would not
  publish that control for a spectrometer it does not drive. So the
  SuperSTEM 2 case (UltraSTEM 100 + UHV Enfina) may need no Gatan code at
  all, and settling it costs one `describe()` call on the instrument PC.

- **A DECTRIS device server** (`miainwoodpecker.devices.dectris_server`),
  MIT and in-tree, driving ARINA/ELA/QUADRO/SINGLA-class detectors over
  the SIMPLON REST API — the third adapter, and the first *scientific
  detector with no column around it*. Served on the neutral `camera`
  target with no scanner. Control needs no vendor library at all:
  `urllib` and `json` are the whole dependency.

  The doubt that prompted it was well founded and turned out the right
  way. Gatan sells the ELA as the *Stela* and markets it as the only
  hybrid-pixel detector fully integrated with Gatan Microscopy Suite,
  which makes it look like a GMS peripheral. It is not: the detector
  control unit serves SIMPLON over HTTP itself, and the Nion/DECTRIS
  characterisation paper (Plotkin-Swing et al., *Ultramicroscopy* 217,
  113067) records a complete acquisition path — detector, fibre, DCU,
  10 GbE — with no Gatan software in it. GMS is a second front end.

  SIMPLON also settled where the pull-per-frame line falls, because the
  API draws it itself: the **stream** subsystem pushes every frame over
  ZeroMQ and is the recording path LiberTEM-live already consumes, while
  the **monitor** subsystem serves the latest image as TIFF over HTTP and
  drops frames by design. The adapter is built on `monitor` and does not
  touch `stream`. An ELA runs to 2250 fps full-frame; this path is tens
  of fps and says so. That is not a compromise — aligning a spectrometer
  while watching the zero-loss peak is what LiberTEM-live does not do,
  and a spectrum image is what a `Camera` should not.

  The simulated backend is a mock **control unit** rather than a stub
  camera: a real `http.server` serving the documented resource tree, the
  `na`/`idle`/`ready` state machine, 403 for read-only parameters and for
  configuring an armed detector, 404 for a wrong API version, 408 for an
  empty monitor buffer, and monitor images as real TIFF — with the same
  client pointed at it, so the tests exercise URL construction, JSON
  shapes, HTTP statuses and TIFF decoding rather than a stub that always
  succeeds. New `dectris` extra (`tifffile`) for the hardware backend.

  Metadata is deliberate where it would be easy to guess:
  `photometrically_linear: True` (threshold-discriminated integer counts,
  no gain curve or demosaic; the only real nonlinearity is count-rate
  paralysis, recorded beside it), no `high_tension_v` (the DCU's
  `incident_energy` is what the detector was *configured for* to set its
  discriminator, not a column reading), and **no calibration published**
  — the detector knows its 75 µm pitch and nothing about the optics
  between it and the specimen, so the axis stays honestly uncalibrated.

- **`docs/adapters/dectris.md`** — whether the ELA is reachable without
  Gatan's software, which SIMPLON subset an adapter needs, where the
  pull-per-frame line falls for a 2250 fps detector, what LiberTEM-live
  already covers, and an itemised list of what stays unverified until a
  detector is present. The eleven checklist entries it generates are in
  `docs/hardware-validation-checklist.md`, led by the one most likely to
  be wrong: this adapter's `ints` trigger arithmetic is the inverse of
  LiberTEM-live's, and only hardware settles which is right.

- **Hitachi is estimable after all.** `docs/vendor-support.md` said "no
  public API … not estimable", written without a search. The search found
  undocumented Python external-control modules (`MfExtCont`,
  `MfKeyMouse`, `MfCommon`) driving a Hitachi SU7000 FE-SEM in public
  code — `SetHv`, `GetStagePosition`, `RunStageMove`, `RunAutoAfc`,
  `RunScan`, which is `InstrumentController` and `Scanner` in everything
  but spelling — on the same product lineage as SuperSTEM 4's SU9000II.
  And the SEM external scan connector, which makes the scan purchasable
  from a third party even if the vendor says no, so unlike every other
  column vendor here a refusal still leaves a route to a scanned image.
  `docs/adapters/hitachi.md` has the full working: what drives the
  instrument, where the search looked and found nothing, what is
  reachable without vendor cooperation, what file-watching can and cannot
  do, a sendable vendor question list, and costed estimates for all four
  answers the vendor could give. Every claim is marked verified,
  reported, generalising or unverified — thirteen are unverified, each
  with what would settle it, and the central one (whether `MfExtCont` is
  on an SU9000II rather than an SU7000) is settled by looking at the
  instrument PC rather than by a negotiation.

- **`InstrumentController` was all-or-nothing to `isinstance`.**
  `available_controls()` exists so an instrument can serve some controls
  and not others, but the protocol was `runtime_checkable`, and that
  check demands every method regardless of what the instrument says it
  supports. Two adapters failed it while working perfectly
  (`camera_server.ServerInstrument`, `gatan_bridge.BridgeInstrument`), so
  the check tested for Nion-shapedness rather than conformance. Found
  independently by two adapters, which was the signal that it was the
  abstraction rather than the adapters. Now fixed — see the split into
  `Instrument` and `InstrumentController` under Fixed below.

- **A protocol gap in the instrument we already drive.** `Scanner`
  produces one channel per `scan_frame` call, on the stated premise that
  "a second channel is a second pass of the beam". That premise is false
  in general: a scanned instrument delivers one or more signals
  *simultaneously* from a single pass — HAADF and MAADF together on a
  Nion UltraSTEM, and BF plus each HAADF segment plus SE plus both BSE
  signals on a segmented-detector SEM. Requesting them serially costs a
  pass of dose per channel, takes as many times longer, lets the specimen
  drift between them, and makes DPC/iDPC/centre-of-mass **invalid**,
  since those difference segments at the same probe position. Recorded
  under "What is still the wrong shape" and sized (3–5 d). The fix is a
  multi-channel call, not a `scan_id`: an identifier alone would assert a
  shared acquisition that did not happen, which is the fiction the
  `Frame` docstring was right to refuse. First written down as a Hitachi
  finding and corrected — it is not vendor-specific and should not wait
  for a vendor.

- **A device server for commodity cameras**
  (`miainwoodpecker.devices.camera_server`): USB microscopes, webcams, and
  recorded video files, over OpenCV's `VideoCapture` — no vendor SDK, MIT,
  in-tree. Two backends like the Nion server: `simulated` synthesises
  moving frames and needs nothing installed, `hardware` opens a real
  device (the `camera` extra). Frames carry `photometrically_linear:
  False` and name their `colour_conversion`, because a UVC camera's pixels
  have already been through demosaicing, gamma and white balance and are
  an image rather than a measurement. Binning other than 1 is refused,
  since consumer sensors crop. The camera arrives on a new neutral
  `camera` target rather than being called a Ronchigram camera.

- `devices/serving.py`: the vendor-free half of the server protocol —
  dispatch, the connection loop, and the accept loop — extracted from
  `nion_server` and shared with the camera server. Lifecycle deliberately
  stays with each adapter: a webcam has no beam to park.

- **The viewer runs against a camera-only device server.**
  `--server-module MODULE` names the module the client launches, so the
  shipped application can drive `miainwoodpecker.devices.camera_server`
  (a USB microscope, a webcam, or a video file replayed as a fixture) or
  an out-of-tree vendor adapter, instead of only the Nion server it ships
  with. This does not weaken the licence boundary: the named module is
  launched as a subprocess, never imported by the application.
  `--backend simulated --server-module
  miainwoodpecker.devices.camera_server` needs nothing installed beyond
  the viewer and exercises the whole live path — display, recording,
  session, analysis buttons — which makes it the cheapest end-to-end test
  of the application.

  `LiveInstrumentWidget` accordingly takes an *optional* scanner, and
  requires a scanner or a camera rather than assuming both. The Scan
  group is not built at all without a scanner, so the absent device is
  missing from the window rather than present and broken, and every scan
  entry point is inert instead of raising — `stop_scan()` in particular
  returns True, because its callers read False as "the device is still
  busy, do not proceed". The camera is chosen through `cameras()` with
  the Ronchigram still preferred, so a server this viewer has never heard
  of can supply the live view. `app.py` previously exited with "this
  device server serves no scanner"; it now exits only when there is
  neither a scanner nor a camera.

  The disk-space warning follows the same rule. A scan's frame shape is
  set by the operator before anything is acquired, so it can be estimated
  up front; a commodity camera's is whatever the driver negotiated, so
  without a scanner the estimate waits for a real frame and stays silent
  until one arrives. Free space is still reported throughout — inventing
  a shape would put a number on screen that no acquisition would produce.

### Fixed

- **The viewer's integration tests no longer skip themselves on macOS and
  Windows.** The display guard in `tests/integration/conftest.py` asked
  only whether `$DISPLAY` or `$WAYLAND_DISPLAY` was set. Those are X11
  and Wayland variables; on macOS Qt uses the cocoa platform plugin and
  on Windows the windows one, and neither sets either. So on both, all
  69 `test_live_widget` tests were quietly marked skipped — not failing,
  not passing, simply not running, which is the one outcome a guard
  should never produce silently, and the reason it went unnoticed. The
  guard now treats a platform with its own window server as having a
  display, and asks the environment only where the answer is actually in
  the environment. All 46 runnable widget tests pass on macOS unchanged;
  the remaining skips are the analysis-extra ones, which are a different
  guard. The `xvfb-run` instruction still stands for Linux, where it is
  still needed.

- **The instrument runtime check no longer demands controls an
  instrument does not serve.** The `isinstance` question every call site
  was actually asking — "is this an instrument target I can hold a
  session against" — is now its own `runtime_checkable` protocol,
  `Instrument`: identity (`stage_size_nm`), capability
  (`available_controls`), lifecycle (`park`). `InstrumentController` is
  that core plus the per-control methods, for static typing, and is
  deliberately no longer `runtime_checkable`, so the old all-or-nothing
  question raises `TypeError` instead of quietly failing partial
  adapters. `camera_server.ServerInstrument` (zero controls) and
  `gatan_bridge.BridgeInstrument` (one control) both pass the runtime
  check now, each pinned by a test; which *controls* exist is asked
  through `available_controls()`, and the sweep generators' graceful
  "control not available" refusals are unchanged.

- **The port-collision retry no longer depends on a stopwatch.**
  `_free_port()` probes a port and releases it, so the server binds it
  seconds later and can find it taken; the loser exits with status 4 and
  the client is meant to re-pick and respawn. Detection was
  `process.wait(0.4)` immediately after the spawn, which was wrong in
  both directions: a healthy server never exits, so **every** good
  startup paid the full 0.4 s for an answer already known, and a machine
  loaded enough to take longer than 0.4 s to reach its bind — the machine
  most likely to collide in the first place — turned a curable collision
  into an anonymous startup error. Seen once in CI after `TARGET_NAMES`
  grew to five ports.

  The fixed wait is gone and the retry now spans the whole
  spawn-and-connect. `_connect_with_retry` already polls the child on
  every attempt, so when it finds it dead with the port-collision status
  it raises a distinguishable internal error rather than the generic
  diagnostic, and `_spawn_and_connect` answers that with fresh ports, a
  fresh child, and a fresh connect deadline — releasing any connections
  the doomed attempt had already made, since the health connection and
  the per-target connects come *after* the first one. Every other exit
  status keeps its diagnostic verbatim, because a missing instrument or
  an unimportable adapter module would only fail again. A persistent
  collision still ends at the existing attempt budget with the "claiming
  localhost ports faster than they can be used" message. Net: healthy
  startups are 0.4 s faster, and a collision noticed at any point before
  the session is connected is retried rather than fatal.

- **EDS beam current reached eXSpy a billion times too small.**
  `load_as_eds_signal` wrote `beam_current_a` straight into
  `Acquisition_instrument.TEM.beam_current`, which eXSpy reads as
  **nanoamps** — `exspy/signals/eds_tem.py`'s dose calculation multiplies
  it by 1e-9 to reach coulombs, and says so in a comment. A 200 pA probe
  therefore arrived as 2e-10 nA, making every dose-based quantification
  wrong by 1e9 with nothing saying so. Found while mapping the same
  metadata tree for EELS. Now converted in the one place all the other
  unit conversions live, and `docs/adapters/spectrum-detectors.md` §4's
  units table gained the row that would have prevented it.

- **Phase 2's napari-versus-`ndv` question is closed: keep napari.**
  Measured on an M2 Pro across a 16× range of frame sizes, display cost
  is flat — 12.2 / 11.2 / 11.4 ms at 512² / 1024² / 2048² — so it is
  napari's fixed per-update overhead rather than upload or draw. That
  diagnosis is the one `ndv` addresses, and it inverts the conclusion: a
  fixed cost is amortised exactly where it would hurt, since every real
  workload's frame time scales with data and this does not. Display is
  4.7% of a 512² scan frame's beam time and 0.27% of a 2048² one. The
  one regime where 11 ms would bite — small frames at high rate — is
  already routed to LiberTEM-live.

- **Display responsiveness under analysis load is measured, and the fix
  is ours rather than the viewer's.** On the M2 Pro, acquire is unmoved
  by CPU contention (5.5 → 5.7 ms median from zero to eight competing
  numpy workers) because a grab is IPC and a shared-memory read, not
  computation — so contention lands on display alone. Display degrades
  in the tail a full load level before the median: at four workers the
  median *improves* to 3.1 ms while p95 triples to 23.0 ms, which makes
  the benchmark's median-derived frame rate misleading exactly where a
  user first notices trouble. At eight workers the worst update is 4031
  ms — the GUI thread descheduled outright, which no per-update
  efficiency addresses. The conclusion is a scheduling constraint on our
  own code, fixed in the next entry: our `viewer/jobs.py` already runs one
  job at a time, and it is the numpy/BLAS and LiberTEM threads inside it
  that take every core. Also confirms by measurement what `viewer/live.py`
  implied: the camera path costs half the scan path (5.6 ms against 11–12
  ms), because only the scan view autocontrasts every frame.

- **Analysis no longer takes every core out from under the GUI thread.**
  New `analysis/threads.py` resolves one number — `os.cpu_count()` minus
  two, floored at one — and two places apply it: `AnalysisJob` runs every
  analysis inside a `threadpoolctl` limit of that many threads, and the
  LiberTEM button's `Context` now comes from `analysis_context()`, which
  passes the same number to `InlineJobExecutor(inline_threads=...)`. The
  executor knob is needed separately because "inline" bounds the
  *executor*, not the numerics: unconfigured, it still asks for one
  fine-grained thread per physical core and hands that to numba, which
  `threadpoolctl` cannot reach. The floor matters most on the machines
  least able to absorb the problem — a two-core laptop would otherwise get
  a zero-thread limit, which BLAS reads as "use everything" and numba
  refuses outright.

  `OMP_NUM_THREADS` and friends are deliberately not set: they are read
  when the native library loads, so writing them from inside a running
  application is a no-op that looks like a fix. Two limitations stated
  rather than papered over, both also in the module docstring: the runtime
  setters underneath `threadpoolctl` are process-global, so the cap is
  scoped to the *duration* of an analysis rather than to the worker
  thread; and `os.cpu_count()` reports the machine rather than a
  container's CPU quota, since `os.process_cpu_count()` needs Python 3.13
  and this package supports 3.11. New dependency: `threadpoolctl` in the
  `analysis` extra only — `libertem` and `py4dstem` already carried it
  transitively.

- `scripts/phase2_live_benchmark.py` compared display cost against the
  *simulator's* acquire time, which is not what gates a live view. On the
  first hardware-accelerated run that denominator produced "display
  dominates … the empirical argument for ndv" from a 2.05× ratio that
  meant nothing of the sort: the simulator makes a 512² frame in 5.4 ms
  where a real 1 µs-dwell scan takes 262 ms, against which display is
  4.2% of a frame. The verdict now divides by the scan's physical
  duration, reports the sustainable frame rate separately as the ceiling
  a camera-rate source actually faces, and says what experiment would
  decide the remaining question.

### Changed

- `SharedFrameReader` gained an opt-in `stop_tracking=` flag that
  unregisters each attached segment from *its own* process's
  `resource_tracker`. The device layer's behaviour is unchanged (the flag
  defaults off); the analysis worker needs it because it inverts the
  device layer's lifetimes — there the reader is the long-lived
  application, here it is a subprocess whose tracker was unlinking the
  client's live segment on exit. Not the cross-process `unregister` that
  `shared_frame.py` records as having made things worse: this one talks
  to the daemon that did the registering.
- `SharedFrameWriter`/`SharedFrameReader` gained `publish_array` /
  `read_array` and a `SharedArrayRef`, for payloads that are arrays with
  no `Frame` or `Spectrum` around them.

- Analyzing a recording opened in the viewer now reads it **once**, not twice.
  Each analysis adapter grew an in-memory entry point beside its
  file-reading one — `hyperspy_signal_from_frames`,
  `hyperspy_spectrum_from_frames`, `libertem_dataset_from_frames`,
  `diffraction_slice_from_frames` — and the viewer hands them the frames the
  load already read rather than pointing them at the path. The path-taking
  functions are unchanged and are now one call to their in-memory half, so
  the two cannot drift and no script or document has to change.

  Separate names rather than one function accepting a path *or* an array:
  whether a call decompresses a 2048×2048 recording is exactly what the
  caller is choosing, so it belongs in the name rather than in an
  `isinstance` check at the bottom of the stack.

  What made this an adapter API change rather than a wiring change is the
  calibration. Frames handed over without it produce a signal whose axes
  silently claim bare pixels — a worse bug than the duplicated read — so the
  carrier is `FrameStack`, the `(data, frame_time, calibration)` triple
  `read_frames` has always returned, now a named tuple so every existing
  unpacking still works. `LoadedRecording` carries the file's calibration
  too, and `LoadedRecording.frames` declines to offer the frames at all when
  they are not the whole recording: a truncated read, or an unfinalized file
  that never wrote its axes. The viewer additionally re-checks the frame
  count against the file, and falls back to the path when they disagree.

  LiberTEM's in-memory form is `MemoryDataSet` via
  `ctx.load("memory", data=..., sig_dims=2)`, which measured the same
  navigation/signal shape its HDF5 reader infers from the same recording.
  `sig_dims` is explicit because the same call on a 2D array yields a
  dataset with *no frames to navigate* rather than an error, so a flat
  single frame is refused with a sentence. LiberTEM's own note that
  `MemoryDataSet` suits a distributed executor poorly is a reason to keep
  the file-reading form, not to avoid this one: the viewer runs an inline
  executor, where there is no worker to ship an array to.

  A fresh analysis burst deliberately still reads the file it just wrote:
  its frames' calibration is only resolved when `NexusWriter` writes them,
  and short-circuiting that would mean a second implementation of the rule
  that decides what a recording's axes are.

- CI's `integration` job runs its tests in parallel (`pytest -n auto`),
  cutting that suite from ~140s to ~52s. The worker count had to be
  measured against the whole command: without coverage the suite is
  bound by waiting on subprocesses and `-n 8` wins, but coverage makes
  it CPU-bound and the ordering inverts (`-n 8` is 132s against `-n 4`'s
  52s on four cores). The base `test` matrix stays serial, where xdist
  measured slower than the 3.4s it would save.

- Default HDF5 compression is now gzip + byte shuffle, which measured smaller,
  faster to write, *and* faster to read than plain gzip on every dataset
  (Ronchigram frames 0.694 → 0.532 ratio with write time roughly halved). Faster
  blosc2 codecs are opt-in behind a new `compression` extra, because a
  plugin-compressed file cannot be read without `hdf5plugin` installed.
- `storage/legacy.py` reads `.ndata` with the standard library instead of Nion's
  `NDataHandler`, so the MIT application no longer imports GPL-3.0 code
  in-process. This module now needs no optional dependency group.

### Fixed

- The client re-picks ports and respawns when the device server reports
  one was already bound. `_free_port()` probes a port and *releases* it,
  so anything on the machine can claim it before the server binds
  seconds later; the collision previously surfaced as an anonymous
  traceback and a dead server. Rare serially, and likely enough under a
  parallel test run to matter.
- The device server crashed at startup: the connection-accounting methods
  added for orphan detection landed on `NionInstrument` rather than
  `_ServerSession`, so its accept thread and watchdog died with
  `AttributeError`. Nothing caught it before CI, because no test runnable
  without the `device` extra executes `serve()`.
- A client connecting to a half-dead server hung forever rather than
  failing: the crashed accept thread left the port open, so TCP connected
  but the authentication handshake never completed, and
  `multiprocessing.connection.Client` has no timeout. Connection attempts
  are now bounded by the existing connect deadline even mid-handshake.
- `NexusWriter` no longer declares `definition = "NXem"` by default. Validation
  showed the files did not conform (a required `NXsample` group was missing), so
  they now claim no application definition unless real specimen metadata is
  supplied.
- `--plugin` precedence: argparse's `append` action added to its
  environment-seeded default, so an explicit flag extended
  `MIAINWOODPECKER_HARDWARE_PLUGINS` rather than replacing it.
- The shared-memory leak test named its own segments instead of diffing
  whole-directory `/dev/shm` snapshots, which was order-dependently flaky.
- `scripts/phase2_live_benchmark.py` imported the long-removed
  `devices.nion_adapter` and could not run.
