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

**Camera section** — **Start camera** runs the camera continuously.

The display never slows the instrument down: if the scan is faster than
the screen, frames are skipped on screen but acquisition is unaffected.

## Keeping data: the Session group

Nothing is saved until you ask, and the **Saving to** line always tells
you where data will go — or warns `no session - data is not being kept`.

- **Change directory...** points recordings somewhere else, e.g. a new
  folder per sample. An existing folder is *reused, never cleared*:
  numbering continues from what is on disk, and the context fields
  reload from the previous run.
- **Operator / Sample / Session notes** describe the whole shift, are
  saved as you type, and are written into every recording — into real
  NeXus fields other tools can read, not a private sidecar.
- **Note for next recording** describes the *next file specifically*
  ("hole 4, after tilting") and is kept until you change it.
- **Disk** shows free space and roughly how many frames it holds at the
  current scan settings.

To record: set **Frames**, press **Record frames** in the Scan or Camera
section. Recording streams to disk in the background — the window stays
responsive — and **Stop recording** keeps everything captured so far as
a complete, valid file. Filenames are automatic and collision-proof:
`0001-scan-haadf-20260810T182524Z.nxs`.

Recording from a source stops its live view first — one thing drives
the instrument at a time — and you restart it afterwards. **Save
displayed frame** is the exception: it needs no instrument access, so
the live view keeps running.

## Opening recordings: the Recordings group

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
