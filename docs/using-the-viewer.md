# Using the viewer

This is the guide for running the microscope from the screen: launching
the app, looking at the live image, keeping data, and getting a first
look at what you recorded. If you would rather drive everything from
code — a script, a notebook, or an AI agent — see
[Scripting and automation](scripting-and-automation.md); every button
described here is a thin wrapper over one function call there.

## Launching

```shell
uv run --extra device --extra viewer miainwoodpecker-viewer
```

A window opens with an **Instrument** panel docked on the right and a
viewing area that gives every dataset a panel of its own.
By default you are connected to the simulated microscope
(`nionswift-usim`), so everything on this page can be tried without an
instrument. On a real system, launch with `--backend hardware`.

Useful launch options:

| Option | What it does |
|---|---|
| `--session DIR` | Where recordings are saved. Reused if it exists — restarting mid-shift lands you back in the same session. |
| `--operator`, `--sample`, `--notes` | Pre-fill the session context fields. |
| `--backend {simulated,hardware}` | Which microscope to talk to. |
| `--server-module MODULE` | Which device server to launch. Defaults to the Nion one. |
| `--plugin` | Server-specific: for Nion, a plug-in module; for the camera server, which camera to open. |
| `--broker PATH` | Join an instrument somebody is already serving — see below. The three options above are the other case, and are ignored. |

### Joining an instrument somebody else is serving

Everything above launches a device server and takes the microscope for
this window alone. The other way in is to connect to a
[broker](scripting-and-automation.md) that is already running over the
instrument:

```shell
miainwoodpecker-broker --publish ~/instrument      # on the microscope PC
miainwoodpecker-viewer --broker ~/instrument       # here, and anywhere else
```

If both are yours to start, one command does it in the right order and
stops the broker cleanly when the window closes:

```shell
pixi run instrument
```

That is `miainwoodpecker-instrument`, and it takes the same options as
the two commands it runs — `--backend`, `--plugin`, `--server-module`
for the instrument, `--session`, `--operator`, `--sample` and `--notes`
for the window. Add `--publish ~/instrument` and a notebook can join the
session while it runs; without it the connection details live in a
temporary directory that goes away with the session.

Four shapes, and which you want depends on how the microscope is being
used rather than on the software:

| Command | The session is |
|---|---|
| `pixi run instrument` | One sitting. A window, and the instrument put down when you close it. |
| `pixi run dashboard` | The same, with the [browser dashboard](scripting-and-automation.md) as the front end instead of the window. |
| `pixi run serve` | The instrument itself. No front end: it stays served while people attach and detach, and ends when you press Ctrl-C. |
| `pixi run tray` | The same as `serve`, with somewhere to live: an icon in the notification area instead of a terminal to keep open. |

`serve` is the one to reach for when the answer to "napari or the
notebook?" is "both, and not yet". It publishes into the working
directory, so from anywhere else on the machine:

```shell
miainwoodpecker-viewer --broker .                  # a window, now
marimo run notebooks/instrument_dashboard.py       # a dashboard, later
```

Both are ordinary clients: they can be opened and closed in any order
while the instrument stays up, and they take turns at the hardware
through the same leases everything else does. Under `pixi run
instrument`, by contrast, closing the window ends the session for
everyone — which is what you want for one sitting and not for a shared
column.

### The same thing, without a terminal to keep open

`pixi run tray` serves the instrument exactly as `serve` does and puts
the session in the notification area instead of in a console. Right-click
the icon for:

- **Open a viewer** — a window on this instrument, and another one after
  that if you want two. They are ordinary clients, so closing one does
  not end anything.
- **Open a dashboard** — the [browser dashboard](scripting-and-automation.md)
  on the same instrument, in its own environment: it wants marimo and no
  Qt, which is why it is a separate entry rather than a mode of the
  window. Both can be open at once, and each is counted separately on
  its own entry.
- **Instrument health…** — one row per device, under the device server
  that was supposed to bring it, saying whether it is answering, what it
  is acquiring and at what rate, and who is holding it. It is read from
  the broker rather than by poking the hardware, so opening it cannot
  disturb an acquisition — and so a device it calls "answering" is one
  that has not reported itself broken rather than one that has been
  tested.
- **Quit and stop the instrument** — the windows, then the broker, then
  the column parked. It asks first, and the question lists what is
  running at the moment you ask it, because this ends everybody's
  session and not only yours.

It publishes to `~/.miainwoodpecker` by default, which is where a
notebook or a dashboard looks when it is told nothing at all, so
`miainwoodpecker-viewer --broker ~/.miainwoodpecker` joins from anywhere
else on the machine. It takes the same options as the commands above,
including `--config` for a microscope that is more than one device
server — see [Instrument configuration](instrument-configuration.md).

The dashboard entry appears only when you say what opens it, which is
anything after `--`:

```shell
miainwoodpecker-tray --dashboard-env dashboard -- marimo run notebooks/instrument_dashboard.py
```

`pixi run tray` already passes that, along with `--broker-env device`
and `--ui-env default` — three environments, which is the point rather
than an accident: the vendor stack in one, the Qt window in another that
demonstrably does not contain it, and marimo in a third that contains
neither.

The window is the same window: the same panels, the same detectors, the
same controls, built from what the broker reports rather than from
devices this process can reach. What differs is that you are **not the
only one driving**. A notebook, the dashboard and another window can be
on the same microscope, so:

- Watching is always free. The live view, the frame counters and the
  control values cost the instrument nothing however many windows are
  open.
- Driving takes a turn. Setting a control, changing a detector's readout
  or acquiring anything claims the target for the duration. If somebody
  else has it, you get a message naming them and what they said they
  were doing — "defocus refused: instrument is leased by notebook
  (energy series)" — rather than a change that silently does not take.
- Turns are refused, not queued. Nothing waits behind anybody, and
  nothing is taken from anybody: try again when they are done.

If you are working on the window itself rather than using it, there is a
lighter way in: `miainwoodpecker-preview` opens the same panels against
an in-process synthetic instrument, with no device server and no extras
beyond `viewer`. See [Developing the UI](developing-the-ui.md).

### For a USB microscope or a webcam

The default server is the Nion one, which serves no USB camera at all,
so a plugged-in microscope cannot appear until you say otherwise:

```shell
uv run --extra camera --extra viewer miainwoodpecker-viewer \
    --backend hardware \
    --server-module miainwoodpecker.devices.camera_server
```

**No `--plugin` is needed.** Every camera that opens and delivers a
frame is found and served, each in its own section. That is the default
rather than "open camera 0" because a USB microscope's index is not
knowable in advance and is usually *not* 0 — a laptop's built-in webcam
takes that — so a fixed default would show the wrong camera and look
exactly like a broken device.

Name one with `--plugin` (an index, `/dev/video0`, or a video file to
replay) and discovery is skipped entirely: `--plugin 1` serves that
camera and nothing else. Naming beats finding, on purpose — if you say
which camera to open, nothing else is added behind you.

If nothing shows up, `python scripts/probe_cameras.py` reports what the
operating system sees, what actually opens, and the command to run for
what it found.

## If you are coming from Swift or DigitalMicrograph

The layout will feel familiar, with a few deliberate differences:

| You are used to | Here |
|---|---|
| Swift's live display panels / DM's **View** window | A window per source (`Scan (HAADF)`, `Camera`), tiled in the viewing area. Each is a napari canvas with its own zoom, contrast, and colormap controls. |
| Swift's **Record** / DM's **Record** | **Record frames** in the Scan or Camera section — writes straight to disk as frames arrive, rather than into memory first. |
| DM's *Save Display As...* | **Save displayed frame** — keeps exactly the frame on screen, without touching the instrument. |
| Swift's project/library | A **session**: a plain folder of files. No database, no import step — the folder *is* the library, and you can browse it in a file manager. |
| `.ndata` / `.dm3`/`.dm4` files | NeXus HDF5 (`.nxs`) — an open format that HyperSpy, py4DSTEM, and any HDF5 tool can read directly. No export step. |

There is no equivalent of Swift's data-item graph or DM's in-app
processing chains, on purpose: analysis belongs to the scientific Python
tools you already use, and the [scripting guide](scripting-and-automation.md)
shows how recordings load into them in one line.

## The viewing area

Every dataset gets **its own window**, and they are tiled side by side:
each enabled detector, each running camera, each recording you open, and
each analysis result. Comparing two detectors read out of the same pass
is the ordinary case on a scanned instrument, so they are shown next to
each other rather than stacked on one canvas where the only way to see
either is to hide the other.

Each window is a full napari canvas, so **zoom, pan, contrast and
colormap are per panel**. Zooming into one corner of the HAADF image
does not drag the diffraction pattern beside it out of view.

**Nothing is ever stretched.** One scale is used for both axes always,
so a picture keeps its geometry whatever shape its window is. Where the
window does not match it — because you reshaped it — the difference
shows as margin on one side, never as distortion. It is not a convention
but a property of how each panel draws, and the test suite measures it
rather than assuming it.

**A window is sized to its picture, so there are no black bars in it.**
The frame takes the data's own shape and the picture fills it edge to
edge — a panel with blank space in it spends screen on nothing, and two
panels of different shapes padded to the same shape look like the same
panel. That holds for everything: a live detector, a recording you open,
an analysis result, and a spectrum image building in front of you.

**Small data is magnified rather than shown as a stamp.** Anything whose
longest side is under 256 pixels opens scaled up to it, so a 64x64
spectrum-image map is a window worth looking at. Anything already that
large opens at one screen pixel per acquired pixel, and shrinks only if
it will not otherwise fit on screen. The floor and the target are the
same number deliberately: magnifying to 512 while leaving 256 alone
would open a 128-pixel scan in a *larger* window than a 256-pixel one,
and window size would stop telling you anything about the data.

**No part of a window is ever off the edge.** A panel too big for the
workspace is shrunk to it, one that would overhang is moved back in, and
when the application is made smaller the panels follow it in. The part
of a window outside the workspace is the part you cannot click.

**Panels go beside each other, and overlap only when there is no room.**
A panel a few pixels too wide for the row it nearly fits is shrunk
slightly rather than wrapped, and when there is genuinely no room they
are offset rather than stacked, so a covered window keeps a corner to
raise it by. **View → Tile documents**
(<kbd>Ctrl</kbd>+<kbd>T</kbd>) packs them again after you have moved
things about; it never resizes a window to fill the screen, because that
would put the black bars straight back.

**Your own changes are yours.** Reshape a window and the picture refits
to it — still whole and still undistorted, with blank space on one axis
because the frame no longer matches it. Zoom a panel and nothing
automatic will undo it: not tiling, not a new dataset arriving.

**View → Actual resolution** (<kbd>Ctrl</kbd>+<kbd>1</kbd>) switches a
panel to one screen pixel per acquired pixel, to see exactly what the
detector recorded with nothing interpolated; larger data is then cropped
and you pan to see the rest. **View → Fit panel to data**
(<kbd>Ctrl</kbd>+<kbd>0</kbd>) goes back, and hands the panel to
automatic fitting again.

Where the axes are calibrated differently — an anisotropically binned
detector — one scale is used for both, so the window takes the shape the
*specimen* has rather than the shape the array has.

**Arranging.** Drag a window by its title bar to move it, or its edges
to resize it. Until you do, the area packs new datasets in beside what
is already open. The first time you place a window yourself it stops
rearranging things around you, and later datasets are dropped into
whatever space is clearest instead. **View → Tile documents**
(<kbd>Ctrl</kbd>+<kbd>T</kbd>) packs everything again and hands tiling
back; **View → Cascade documents** stacks them offset from one corner.

**Closing a panel is not the same as stopping its source.** A closed
panel stays closed — a running detector would otherwise reopen its
window on the very next frame, which would make the close button
useless. Start that detector or camera again and its panel comes back
and comes to the front. The same is true of a panel that is merely
buried: being covered while it runs is a legitimate way to arrange the
area, and starting the source again is what says you want to look at it.

**View → Show layer controls** turns on napari's contrast, colormap and
gamma sidebar in every panel. It is off by default because at a tile's
size that sidebar is bigger than the image it belongs to.

## The dock

The panel on the right holds three groups — **Instrument**,
**Recordings** and **Devices** — and a fourth, **Analysis extras**, when
an analysis library is not installed (see
[first-look analysis](#first-look-analysis)). Click any group's header
to fold it away; the disclosure triangle shows which way it will go.

The groups **scroll**, so nothing is ever out of reach no matter how
many devices your instrument serves or how many groups you have open.
Folding is for tidiness, not for making the panel fit: watching a camera
while a scan runs means having several groups open at once, and that has
to keep working.

## The Instrument panel

The top of the dock says **what you are connected to** — the backend and
the devices the server serves. That question had no answer in the window
before: if you launched against the wrong backend, you found out from the
images.

Below it are the instrument's controls — **only the ones it publishes**.
A microscope with no beam blanker gets no blanker checkbox, for the same
reason a detector-only instrument gets no Scan section: a dead control
invites you to go looking for hardware that is not fitted.

- **Defocus (nm)**, **Energy offset (eV)** and **Stage (nm)** each have a
  **Set** button. Nothing is sent while you type or click the arrows —
  otherwise a spin box would drive the optics once per click on the way
  to a value, and typing `150` into an empty field would pass through
  `1` and `15`.
- **Beam** blanks or unblanks on click, with no Set button: it is one bit
  and the click *is* the decision.
- **Refresh** re-reads everything. Values are not polled — asking the
  instrument for four controls sixty times a second would put traffic on
  the wire to answer a question nobody asked.

**The viewer applies no range limits, on purpose.** Limits live behind
the instrument's own setters, where the hardware knows them; a second set
of limits in the viewer would be a second source of truth, and one that
had drifted would silently send a different value than the one on your
screen. So the instrument refuses what it will not do, the refusal is
shown in the status line, and **your number stays in the field** to be
corrected rather than retyped.

Nothing here blanks the beam as a side effect of anything else. Blanking
is yours, on the Beam checkbox, and nowhere else.

## Live viewing

Every device the instrument serves gets its own section in the
**Devices** panel, named after the device rather than after its slot —
`Camera - usb_microscope`, not `camera`, which tells you nothing once
there are two of them. Click a section header to fold it away.

### Detectors

The Scan section lists your detectors as **checkboxes**, not a
drop-down, because a scanned instrument reads several of them out of one
pass as a matter of course — HAADF and MAADF arrive together, and on an
EDX-fitted column the X-ray spectra come with them. Serial acquisition
is the special case.

Every checked detector gets its own **window** in the viewing area and
they are all fed from the **same pass**, so you can difference them per
pixel — DPC, ratios — without wondering whether the probe moved in
between. Enabling a second detector costs no extra dose and no extra
time, and puts its image beside the first rather than on top of it.

At least one has to stay checked; a scan with none reads nothing out.
Your selection is remembered between launches — it follows you and the
instrument, not the shift, so it lives in your config directory rather
than in the session.

### Scan profiles

Dwell and resolution belong to a **profile**, and all three are visible
at once:

| Profile | What it is for |
|---|---|
| **View** | The continuous live loop. Short dwell so the display keeps up. |
| **Preview** | A single scan at higher signal-to-noise, for judging focus and astigmatism *by eye*. **Shown, not saved** — a focus check that littered the session with files would stop being used. |
| **Acquire** | The scan that is kept: long dwell, full resolution. Used by **Acquire scan image** and **Record frames**. |

**FOV is shared and deliberately outside the profiles.** It is the
region you navigated to, and switching from checking focus to taking the
picture must not move the specimen out from under you at the moment you
were happiest with it.

Profiles are remembered between launches too. Changing Preview or
Acquire never disturbs a running live view — each is read when its own
action is taken.

Several sections can be open at once, which is the point of folding
rather than tabs: watching a camera while a scan runs is the ordinary
case. The first section starts open and the rest folded, so a
one-device instrument looks exactly as it always did and a five-device
one still fits on a screen.

**A device you do not have has no section.** A detector-only
instrument shows no Scan section and no greyed-out placeholder either —
a disabled control would invite you to go looking for a scanner that is
not fitted.

**Scan section** — pick a detector channel, size, dwell time, and field
of view, then **Start scan**. The image updates continuously; changing
any setting takes effect on the next frame, while a scan is running. The
status line shows the acquisition rate.

**Camera section** — **Start camera** runs that camera continuously.
**Each camera gets its own section, and its own controls.** Start the USB
microscope and the webcam beside it stays off; both can run at once, each
into its own window (`Camera`, `Camera (camera:2)`), so they never
overwrite each other's image. On the camera server you get a section per
camera found, without having named any of them.

**Binning is per axis where the detector says its axes differ.** Most
cameras bin both directions by the same factor and get a single **Image
binning** control. A spectrometer does not: binning the rows together
trades dynamic range for signal-to-noise and is the routine move, while
binning the energy channels together spends the spectral resolution the
instrument exists to provide. Those are two settings with opposite
costs, so an EEL spectrometer gets **Binning (rows)** and **Binning
(channels)** separately, each offering only what that axis will take —
typically a generous range down and very little across. Binning rows
leaves the energy scale untouched; binning channels widens it in
proportion, and the panel's scale bar and the recorded calibration both
follow.

The analysis buttons sit in the *first* camera's section and run against
that camera. Repeating them in every section would offer three buttons
per camera with no way to tell which burst you were about to take.

The display never slows the instrument down: if the scan is faster than
the screen, frames are skipped on screen but acquisition is unaffected.
Acquisition runs on its own thread at whatever the device manages, and
the screen samples it — the two rates are independent by construction.

**The live view refreshes at 60 fps.** Measured end to end through the
whole display path, a frame costs 9.4 ms at 512², 9.7 ms at 1024² and
10.2 ms at 2048² — near enough flat, because the pixels go to the GPU —
so the ceiling is about 100 fps and 60 is comfortably inside it. A
refresh that finds no new frame costs 4 microseconds, so the rate costs
nothing when the source is slower than the screen.

## Keeping data: the status bar and Session settings

Nothing is saved until you ask. Along the very bottom of the window, to
the left of napari's **Ready**, two facts sit where you can always see
them:

- **Saving to** — the destination, shortened from the left so the part
  that differs between sessions stays readable; hover for the full path.
  With no session the label goes away and the line reads simply
  `No session - data is not being kept`.
- **Free space** — an absolute figure, with a warning if the recording
  you are set up to take would not fit. With no session there is no
  figure to give, so the field is not there at all.

Both are **read-only**. They tell you where you stand; they are not
where you change it, so the setting has one home rather than two.

That home is **Session settings...**, at the top of the Recordings
group — the things you set once a shift and then leave alone:

- **Change directory...** points recordings somewhere else, e.g. a new
  folder per sample. An existing folder is *reused, never cleared*:
  numbering continues from what is on disk, and the context fields
  reload from the previous run.
- **Operator / Sample / Session notes** describe the whole shift, are
  saved as you type, and are written into every recording — into real
  NeXus fields other tools can read, not a private sidecar.

**Note for next recording** deliberately did *not* move into that
dialog. It describes the *next file specifically* ("hole 4, after
tilting"), changes with every burst, and so stays in the Recordings
group next to the buttons that start one.

### Acquiring an image

**Acquire scan image** takes one pass of the probe and reads out
**every detector you have checked** — HAADF and MAADF together, not just
the one on display. The pass happens either way, so the second
detector costs no extra dose and no extra time, and the two images are
registered to each other by construction. The alternative is scanning
the same area twice to get the channel you wish you had kept. The
frames carry a shared `scan_pass_id`, so a reader can do per-pixel
arithmetic between the channels — DPC, ratios — without guessing from
timestamps whether the probe moved in between.

**Acquire image** in a Camera section takes one exposure at that
section's own **Image exposure** and **Image binning**, which are
deliberately separate from whatever the live view is running. The two
are different jobs: the feed stays short and often binned so it keeps up
at sixty frames a second, while the image you keep is worth a long
unbinned exposure. The live settings are put back afterwards, so one
long acquisition does not leave the feed crawling. The binning choices
are the camera's own — a detector that only does 1× does not offer a 4×
it would refuse.

Neither is affected by the **Frames** count beside it: an image is one
acquisition, whatever that says.

### Detector readout: images or spectra

**Detector readout** sets what the detector delivers. `image` is the
sensor's own 2D frame. `projected` sums the whole non-dispersive
direction into a 1D spectrum, which is what an EEL spectrometer is set
to for an ordinary spectrum image — vertical binning taken to its limit,
traded for signal-to-noise.

Unlike the exposure and binning above it, **this one applies to the
device as soon as you change it.** Those describe an acquisition;
readout describes the detector, and it decides the rank of every frame
the detector produces. Stop the camera first — the live layer cannot
change its number of axes underneath itself, and the control says so
rather than doing something surprising.

A camera with no dispersive direction refuses `projected` and explains
why. That is not a bug to work around: summing one axis of a Ronchigram
gives a line of numbers on an *angular* axis, which is not a spectrum,
and the file it landed in would be a spectrum recording that is not one.

### Acquiring a spectrum image

**Acquire spectrum image** drives the probe over a grid of
**Positions** beam positions across the current field of view, keeping
the whole readout of one detector at every one — and reading every scan
channel out of the same traversal, so the images you navigate the
dataset with afterwards share its probe positions by construction.

**Per-position detector** chooses which detector that is. What you get
depends on the readout mode it is in, not on which button you pressed:

| Detector readout | What lands on disk |
|---|---|
| `projected` | A **spectrum image**: `(scan_y, scan_x, energy)`, in NeXus's `NXspectrum` vocabulary — the same layout a standalone spectrum recording uses, so the same readers find it. |
| `image` | A **4D stack**: `(scan_y, scan_x, det_y, det_x)`. For a Ronchigram camera that is 4D-STEM; for a spectrometer it is a spectrum image that kept its non-dispersive direction, which is a real experiment rather than a mistake. |

The status line names which of the two it actually wrote, so leaving a
spectrometer imaging by accident is visible immediately rather than at
analysis time.

Either way the data is written straight to disk as it is acquired rather
than assembled in memory first, so its size is bounded by the disk rather
than by RAM.

**You can watch it build.** A panel named `Acquiring (…)` opens when the
first beam positions land and fills in as the probe goes, and the
Recording line counts positions — `acquiring 64x64 pass - 1537/4096
positions (37%)`. What the panel shows is a **virtual detector image**:
the signal summed at each position, formed the same way one is formed
offline. That is what makes it worth looking at rather than a progress
bar — drift, contamination, or a probe scanning vacuum are all visible
in it, minutes before the file exists.

The window stays live throughout. The pass runs on its own thread and
the screen samples it, so the live view keeps running, the panels still
zoom and pan, and the application answers. It used to run inline: a long
spectrum image froze the whole window until it finished, which the
operating system reports as an application that has stopped responding.

The progress panel is sized to its map like any other, so a 64x64 grid
opens as a 512-pixel window rather than as a 64-pixel stamp.

**Most instruments will refuse, and the refusal is the point.** A
spectrum image needs the scan and the detector synchronised in
*hardware* — the column driving the detector's trigger, or the detector
advancing the scan. A backend without that wiring cannot tie a camera
frame to a probe position, and producing a plausible cube anyway would
be worse than refusing: it has the same shape as a real one, and every
number computed per pixel from it would be computed against a position
nothing established. So the button says what is missing instead.

Today only the [preview instrument](developing-the-ui.md) can do it. The
`nionswift-usim` simulator cannot, and that is measured rather than
assumed — moving the simulator's own probe position changes nothing
beyond shot noise.

Known limits, while this is being built out: the grid is square (the
target-area UI that would take its aspect ratio from a region you draw
is not built yet), the acquisition blocks the window while it runs, and
a saved pass appears in the **File** list as `0 frames` because that
list only understands frame stacks.

### Replaying a recorded session

A **replay** backend serves a session someone already recorded on a
microscope, through the same devices as everything else. It is the one
backend whose data is real, and it behaves like an instrument rather
than like a file: a pass takes as long as it took, one beam position at
a time.

```shell
pixi run -e replay replay /path/to/session
```

Two things about it are different from every other backend, and both are
deliberate:

**The grid is not yours to choose.** A recording is the region and
sampling the operator picked at the time, so **Positions** is ignored
and the status line names the grid that was actually acquired. Asking
for another one would mean resampling, and a dataset of the requested
shape whose every pixel was interpolated looks exactly like a real one.

**Nothing about it can be driven.** The exposure, the binning and the
spectrometer's energy offset are all fixed by what was recorded, and
setting them is refused with a sentence rather than accepted and
ignored. That is the same rule the rest of this application follows: a
control that appears to work and does nothing is worse than one that
says it cannot.

Recordings you make from a replay are real NeXus files holding real data
acquired elsewhere. Every frame and every spectrum in them carries the
`replay` backend name and the path of the file it came from, so they
cannot be mistaken later for something taken at your instrument — but
point the session at a directory where that will be obvious anyway.

### Recording a series

To record a time series: set **Frames**, press **Record frames** in the
Scan or Camera section. Recording streams to disk in the background — the window stays
responsive — and **Stop recording** keeps everything captured so far as
a complete, valid file. Filenames are automatic and collision-proof:
`0001-scan-haadf-20260810T182524Z.nxs`.

Recording from a source stops its live view first — one thing drives
the instrument at a time — and you restart it afterwards. **Save
displayed frame** is the exception: it needs no instrument access, so
the live view keeps running.

## Recording and opening: the Recordings group

**Recording** says whether one is running, **Stop recording** ends it
keeping everything captured so far, and **Recorded** lists what this
session has written.

The **File** list shows this session's recordings with their state
("12 frames", "empty", "damaged"). Tick **List every session in the
parent directory** to see the whole day, or **Open from disk...** for
any path. **Open selected** opens the file in a window of its own;
a multi-frame recording gets napari's frame slider.

**Add note / Annotate opened** appends a timestamped note to a recording
*after* the fact — for the observation you only make once you look at
the data.

If an acquisition was interrupted, the file list says exactly what
survived rather than making you find out: a recording whose writer was
stopped abruptly still opens and displays every frame (it is marked
"unfinalized — viewable, not analyzable"), while one from a hard-killed
process is reported as damaged.

## First-look analysis

Three buttons in the Camera section run one real operation each and put
the result into a window of its own:

- **Analyze in HyperSpy** — mean projection over a short burst.
- **Sum in LiberTEM** — sum over the burst.
- **Fit central disk (py4DSTEM)** — fits the bright-field disk on a
  single frame and draws the fitted circle.

By default each button grabs a fresh burst from the camera (saved into
the session, so the analysis input is kept too). Tick **Analysis buttons
use the opened file** to run them against a recording you opened
instead.

**A button only appears when its extra is installed** — `analysis`,
`libertem` and `py4dstem` respectively. In place of the missing ones an
**Analysis extras** section of its own names what is enabled, what is
available, and the command to install it:

```
enabled: analysis
available: libertem, py4dstem
pip install "miainwoodpecker[libertem,py4dstem]"
```

With all three installed the section is not shown, because the three
buttons already say so. A button that cannot work is worse than an
absent one: it teaches you that this application's buttons sometimes do
nothing.

The buttons sit in a Camera section because they run against that
camera; what is *installed* is not a camera's property — it is the same
answer for every camera served, and for an instrument serving none — so
it is not printed under one camera's name.

These are deliberately previews, not an analysis suite. For real
analysis, the same recordings open directly in HyperSpy, LiberTEM, or
py4DSTEM — see
[loading recordings into analysis tools](scripting-and-automation.md#loading-recordings-into-analysis-tools).

## When something says "busy" or "try again"

The viewer refuses a few actions rather than risking your data — for
example, starting a recording while a live acquisition has not finished
its frame yet. Driving one detector from two places at once can corrupt
frames silently, which is worse than the one-line message asking you to
try again a moment later.
