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

A napari window opens with an **Instrument** panel docked on the right.
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
| Swift's live display panels / DM's **View** window | The napari canvas. One layer per source (`Scan (HAADF)`, `Camera`), with napari's own zoom, contrast, and colormap controls. |
| Swift's **Record** / DM's **Record** | **Record frames** in the Scan or Camera section — writes straight to disk as frames arrive, rather than into memory first. |
| DM's *Save Display As...* | **Save displayed frame** — keeps exactly the frame on screen, without touching the instrument. |
| Swift's project/library | A **session**: a plain folder of files. No database, no import step — the folder *is* the library, and you can browse it in a file manager. |
| `.ndata` / `.dm3`/`.dm4` files | NeXus HDF5 (`.nxs`) — an open format that HyperSpy, py4DSTEM, and any HDF5 tool can read directly. No export step. |

There is no equivalent of Swift's data-item graph or DM's in-app
processing chains, on purpose: analysis belongs to the scientific Python
tools you already use, and the [scripting guide](scripting-and-automation.md)
shows how recordings load into them in one line.

## The dock

The panel on the right holds three groups — **Instrument**,
**Recordings** and **Devices**. Click any group's header to fold it
away; the disclosure triangle shows which way it will go.

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
  instrument for four controls thirty times a second would put traffic on
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
into its own napari layer (`Camera`, `Camera (camera:2)`), so they never
overwrite each other's image. On the camera server you get a section per
camera found, without having named any of them.

The analysis buttons sit in the *first* camera's section and run against
that camera. Repeating them in every section would offer three buttons
per camera with no way to tell which burst you were about to take.

The display never slows the instrument down: if the scan is faster than
the screen, frames are skipped on screen but acquisition is unaffected.

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
**every detector channel** the scanner has — HAADF and MAADF together,
not just the one on display. The pass happens either way, so the second
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
at thirty frames a second, while the image you keep is worth a long
unbinned exposure. The live settings are put back afterwards, so one
long acquisition does not leave the feed crawling. The binning choices
are the camera's own — a detector that only does 1× does not offer a 4×
it would refuse.

Neither is affected by the **Frames** count beside it: an image is one
acquisition, whatever that says.

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
any path. **Open selected** loads the file into napari as its own layer;
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
the result back into napari as a new layer:

- **Analyze in HyperSpy** — mean projection over a short burst.
- **Sum in LiberTEM** — sum over the burst.
- **Fit central disk (py4DSTEM)** — fits the bright-field disk on a
  single frame and draws the fitted circle.

By default each button grabs a fresh burst from the camera (saved into
the session, so the analysis input is kept too). Tick **Analysis buttons
use the opened file** to run them against a recording you opened
instead.

**A button only appears when its extra is installed** — `analysis`,
`libertem` and `py4dstem` respectively. In place of the missing ones the
Camera section shows a single **Analysis extras** row naming what is
enabled, what is available, and the command to install it:

```
enabled: analysis
available: libertem, py4dstem
pip install "miainwoodpecker[libertem,py4dstem]"
```

With all three installed the row is not shown, because the three buttons
already say so. A button that cannot work is worse than an absent one:
it teaches you that this application's buttons sometimes do nothing.

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
