# Instrument survey: a runbook for the SuperSTEM team

This project is being built to drive SuperSTEM's instruments, and a
number of design decisions currently rest on guesses about them. Each
guess is cheap to settle at the instrument and expensive to get wrong:
the Gatan question alone is the difference between "already supported"
and several weeks of adapter work.

`scripts/superstem_survey.py` asks those questions. It is a single file
with no dependencies, it reads and never writes, and it produces one
JSON file to send back. Reading it before running it is encouraged — it
is about 1,500 lines, most of them explaining themselves.

This page is the runbook: what to run, on which machine, in which
interpreter, and what the script deliberately leaves for a human.

## Getting the script to the instrument

**For the SuperSTEM team, there is a hosted page that needs no git, no
GitHub account and no package index:**

> **<https://claude.ai/code/artifact/f60f60c4-8cc4-4a30-8109-427cb53ad4ef>**

It carries a download button, the full source to read first, and a
condensed version of this runbook. One thing about it is worth knowing
before sending the link on:

- **The page is private until shared.** It has to be shared from the
  artifact's own share menu before anyone at Daresbury can open it.

**The download button links straight to `scripts/superstem_survey.py` on
GitHub**, rather than saving a copy through the Claude app — that app-only
save mechanism doesn't exist for someone who opens the shared link without
being signed into Claude, which left the button silently broken for
exactly the audience this page is for. Anyone who can reach GitHub gets
the same file a click away; the hosted page still embeds a full copy for
reading and for copy-pasting on a machine that can reach the shared link
but not GitHub, and it names the revision it was built from so the two
can be told apart.

**When the script changes, the hosted page must be republished** — it
embeds a copy of the source rather than linking to it, which is what lets
it work on a machine with no access to this repository:

```
python scripts/build_survey_page.py --out superstem-survey.html
```

`scripts/build_survey_page.py` reads the script, escapes it into
`scripts/superstem_survey_page.html.in`, and stamps in the revision, so
the bytes the download hands over are the bytes in this repository *by
construction* — the page is never hand-edited and cannot drift from the
script silently. What it cannot do is publish: the generated file has to
be republished to the same artifact URL, or the hosted page keeps serving
the previous revision. The revision marker in the page footer is how to
tell which one is up there.

## What it will not do

The script's safety is structural rather than a matter of care, and it
is worth being precise about what that means for each section.

**Nion.** It reads a component registry that something else populated,
and reads named controls. It never sets a control, never blanks or
unblanks, never moves the stage, never starts a scan — and, the one that
matters most, it **never loads a device plug-in**. Loading plug-ins is
how this project's own device server registers components in a process
of its own; doing it here would mean a second process claiming hardware
a running Nion Swift already owns. If the registry is empty, the script
says so and asks you to run it from Swift's Python console instead of
populating the registry itself.

**DECTRIS.** HTTP `GET` only. It never `PUT`s a configuration, never
arms, triggers, disarms or aborts. It is safe to run while somebody else
is using the detector, and the detector state it reports will say if
somebody is.

**Hitachi.** It resolves module *specifications* with
`importlib.util.find_spec`, which locates a module without executing it,
and it lists directories under a few bounded roots. It never imports a
vendor control module, because importing one may open a connection to
the column.

**Gatan.** It locates the `DigitalMicrograph` module without importing
it, and notices when that module is *already loaded* — which is how it
knows it is inside Gatan Microscopy Suite's own Python window. From
there it lists the module's public names with `dir`, which reads
nothing. It executes no DM script and reads no image; the one
imaging-filter question that needs a script executed is left to the
hardware checklist as a deliberate act.

No section reads or transmits acquired data. The JSON report contains
instrument capability, versions and file paths — no images, no
spectra, no credentials.

## Python environment

**Nothing needs installing.** No pip, no virtual environment, no
network access to a package index. The script imports only the standard
library, plus the instrument's own `nion` packages when running the Nion
section.

**Python 3.7 or newer.** That floor is set by Gatan Microscopy Suite,
which embeds 3.7 from version 3.4 and is therefore the oldest
interpreter plausibly sitting near this hardware. SuperSTEM 1 and 2 run
DigitalMicrograph 1.x and 2.x, which embed no Python at all, so on those
machines any Python 3.7+ that happens to be installed is the one to
use — and if there is none, say so rather than installing one on an
instrument computer. On anything older the file will not parse,
and the failure is a `SyntaxError` rather than a useful message, so
check first:

```
python -c "import sys; print(sys.version)"
```

Then run the preflight, which touches no hardware and opens no socket:

```
python superstem_survey.py --check
```

Every command below names the file `superstem_survey.py`. If you got it
by pasting from the hosted page's "Copy to clipboard" button instead of
downloading it, name the file whatever you like — Python does not care
about the extension.

It prints which interpreter it is in and which sections that interpreter
could answer. If it disagrees with what you expect, the interpreter is
the thing to change, not the machine.

### Which interpreter matters more than which machine

Two of the three sections can only see what is importable *from the
interpreter they run in*, and running from the wrong one produces a
confident wrong answer rather than an error.

- **Nion** wants the interpreter Nion Swift itself runs — ideally
  Swift's own Python console, where the running application has already
  populated the registry. A system Python on the same computer will
  usually report an empty registry. That is a fact about that
  interpreter, not about the instrument.
- **Hitachi** wants whichever interpreter the vendor software installed
  its modules into. Run from the wrong Python, a missing `MfExtCont`
  means nothing at all. If that machine has several Pythons, run the
  section once per interpreter with a different `--out`. It costs a
  minute each and removes a false negative that would otherwise cost
  weeks of misdirected work.
- **DECTRIS** has no such constraint. Any Python 3.7+ on any machine
  that can reach the control unit will do, including a laptop on the
  control network.

If Python 3.7+ is genuinely unavailable somewhere — a legacy control PC
with only 2.7, say — do not fight it. Skip that section and tell us;
the questions it would have asked can be answered by hand.

## What the Nion run records, and why it grew

The first version of this script asked one question of a Nion column:
is there an `eels_camera`? The [acquisition UX survey](acquisition-ux-survey.md)
found that the answers to several more decide the shape of the work,
and each of them is a property read from the same console:

- **Which `nionswift_plugin` modules are installed.** The device server
  has to be told which one to load by name, and each instrument file
  currently leaves that blank. Listed by file, never imported.
- **The energy offset, under two names.** This project's server drives
  `ZLPoffset`, which is the *simulator's* control. Nion's own
  acquisition preferences name it `EELS_MagneticShift_Offset`. The
  simulator publishes both, which is how the difference went unnoticed;
  a real column may publish only one. Both are read.
- **Every registered camera's own account of itself**: sensor shape,
  binning factors, whether the device offers dark subtraction and gain
  normalisation, and the calibration controls its energy axis comes
  from (`eels_x_scale` is the dispersion). All properties.
- **Whether the scan unit and each camera offer Nion's synchronised
  acquisition methods** — `prepare_synchronized_scan` on the scan
  device, `acquire_synchronized_*` on the camera. Those two are what a
  spectrum image through Nion's stack is built on, and a device without
  them cannot take one whatever a window offers. Checked with
  `hasattr`, never called.
- **Each hardware source's saved profiles** — the dwell, size, exposure,
  binning and processing the operators actually acquire with here.
  Only visible from Swift's console; read, never selected.
- **The controller's `stage_size_nm`**, which the server otherwise
  replaces with a 1 µm guess that sets every default field of view.

None of that changes what the run costs: it is still one command, from
Swift's console, during a session.

## Run 1 — SuperSTEM 2 (Nion UltraSTEM 100)

On the instrument control computer, **from Nion Swift's Python console**
while Swift is running:

```
python superstem_survey.py --nion --out superstem2.json
```

This is the highest-value run of the set. The question it exists to
answer is whether the stem controller exposes an `eels_camera`, and
whether it publishes an energy-offset control under either name. If it
does both, the UHV Enfina is reached through Nion, the spectrometer is
already drivable, and no Gatan-side code is needed for SuperSTEM 2 at
all. If it does neither, an adapter that has to live inside Gatan
Microscopy Suite comes back onto the plan — and that plan has a hole
in it: **SuperSTEM 2 runs DigitalMicrograph 2.x, which has no Python**,
so the inbound bridge this project built (which runs inside GMS's own
Python) cannot run there at all. If the Enfina is not Nion's camera, the
remaining routes are a DM-script on the DM side speaking a socket, or a
Gatan-supplied controller — both new work. Run 4 below still applies
here: `--gatan` from any Python records the DM installation.

The run also records the scan channel count and channel names, which
settle how many signals one pass reads out, and everything in the
section above. Every control is read; none is set.

Safe to run during a session. It is a sequence of reads, and the largest
risk it carries is that it reports nothing useful because it was run
from the wrong interpreter.

## Run 2 — SuperSTEM 3 (HERMES): the column, then the control unit

Two runs on this instrument, because it has two drivers.

**From Nion Swift's Python console** on the control computer, exactly as
for SuperSTEM 2:

```
python superstem_survey.py --nion --out superstem3.json
```

Here the question is the mirror image of Run 1's: **does Nion's stack
*also* register an EELS camera on this column?** The ELA is served by
its own process in this project's configuration; if Nion registers it
too, that is two drivers on one detector, and the instrument file has
to choose. The run also records whether the IRIS spectrometer's
dispersion and offset are published as column controls (`eels_x_scale`,
`EELS_MagneticShift_Offset`), which is where the ELA's frames would get
an energy axis from, since the detector itself has none.

**From any machine on the control network:**

```
python superstem_survey.py --dectris 192.168.1.10 --out hermes.json
```

Use the address of the **control unit**, not the detector head — SIMPLON
is served by the DCU on port 80. The address above is an example, not
one anybody has read off the instrument.

This settles which SIMPLON API version answers (asked directly, and by
trial), which configuration keys an ELA actually publishes, the trigger
mode and counts it is left in, and whether the `monitor` and `stream`
subsystems are enabled. The adapter was written against published
documentation and open-source clients; several of its assumptions are
guesses that a single `GET` each can confirm or kill.

Safe to run mid-experiment. Everything is a read, and the detector state
in the report will show if an acquisition is in progress.

## Run 3 — SuperSTEM 4 (Hitachi SU9000II)

On the SU9000II control computer:

```
python superstem_survey.py --hitachi --out superstem4.json
```

This looks for three undocumented external-control modules —
`MfExtCont`, `MfKeyMouse`, `MfCommon` — that are evidenced on an SU7000
and may or may not exist on an SU9000II, and for install directories
named for Hitachi, EM Flow Creator and ElementView. Whether the modules
are present is the single fact that decides between an adapter we can
write in a couple of weeks and a conversation with Hitachi.

Because the answer is only meaningful from the right interpreter, please
run this once per Python installation on that machine if there is more
than one, and send all the reports. A "not found" from an interpreter
the vendor software never installed into is not evidence of absence.

Safe to run: nothing is imported, so nothing connects to the column.

## Run 4 — SuperSTEM 1: find out what drives it

This project knows one thing about SuperSTEM 1 first-hand — its
spectrometer reads out 1340×100 — and its instrument file says so in
its first line. What drives the column and what drives the Enfina are
both unknown, so the run here is a diagnosis rather than a question:

```
python superstem_survey.py --check
python superstem_survey.py --all --out superstem1.json
```

`--all` runs the Nion, Gatan and Hitachi sections and reports which of
them found anything. If `--check` says `nion: yes`, run the Nion section
again from Swift's own console as in Run 1.

**SuperSTEM 1 runs DigitalMicrograph 1.x and SuperSTEM 2 runs 2.x, and
neither has Python built in** — that arrived with GMS 3.4. So there is
no "DM's Python window" to run anything from on either machine, and
the Gatan section's inside-GMS detection will never fire there. What
`--gatan` still records from any Python on the machine is the DM
installation directories and any GMS environment variables, which is
enough to say which DM is installed and where. The facts that matter
most on SuperSTEM 1 are ones for a person: which program drives the
column, which drives the Enfina, and the DM version on the About box.
Those are in the questionnaire below.

If any SuperSTEM machine does turn out to have GMS 3.4 or newer, run
`--check` and `--gatan` from its DM Python window as well; the first
records the interpreter GMS embeds, the second that the script is
inside GMS and what the `DigitalMicrograph` module offers.

## Questions for the people who run them

The script reads what the software publishes. These are the things it
cannot, and they matter as much: they are what the
[acquisition UX survey](acquisition-ux-survey.md) needs to know to make
a window an operator would recognise. A few minutes per instrument,
and a sentence each is plenty.

**Which software, today.** On each column, what do you take each of
these in — a STEM image, an EELS spectrum, a spectrum image — Nion
Swift, DigitalMicrograph, or something site-specific? Which Swift
version, if Swift? Is Swift's own spectrum-imaging panel used, and does
it work? On SuperSTEM 1 and 2, which DigitalMicrograph version exactly
(the About box), and on SuperSTEM 1, what drives the column at all?

**The spectrometer.** How do you set the dispersion (eV per channel),
and where — a Swift control, a Gatan or Nion panel of its own, a knob?
Which dispersions do you actually use, and over what energy ranges?
How do you move the zero-loss peak: which control, in which software?
Is there a drift-tube or aperture/slit setting you adjust during a
session, and where does it live?

**Dark and gain.** How do you take a dark reference — blank the beam
and acquire, or a panel that does it — and how often? Is gain
normalisation used, and where does the reference come from?

**A typical spectrum image.** On each column: the grid size, the
per-position exposure, the dispersion, which images you keep alongside
(HAADF, MAADF), whether drift correction is on and what does it, and
roughly how long a pass takes. Do you draw the region on a live image
or type the size?

**A typical STEM image.** Dwell and size for looking, and for keeping.
Do you use scan rotation routinely? Sub-regions?

**SuperSTEM 4 specifically.** Who makes the EELS spectrometer and the
diffraction camera, and which software drives each. Is EM Flow Creator
installed and licensed? Is ElementView there, and does it have any
scripting, macro, batch or watch-folder option? These are the questions
[the Hitachi page](adapters/hitachi.md) calls "the cheapest experiment
on this page".

**Two things that need hardware time, not a script.** Both need an
operator and a decision that it is worth the beam, and both are worth
scheduling together on HERMES.

- *The DECTRIS trigger arithmetic.* Our adapter uses SIMPLON's `ints`
  mode with `nimages=1` and `ntrigger=65536` — one image per software
  trigger. LiberTEM-live uses the same mode with the numbers the other
  way round. Both cannot be right about what one `trigger` does.
  Settling it requires arming the detector and sending a trigger, which
  is precisely the thing the survey refuses to do. The outcome is one
  bit: does one `trigger` in `ints` produce one image, or start the
  whole series?
- *Whether the scan unit triggers the ELA.* A spectrum image on HERMES
  through this project needs the scan unit's per-pixel trigger output
  wired to the detector's external trigger input. Whether that cable
  exists, and which trigger mode the ELA is put in when Nion Swift
  takes a spectrum image today, is a question for whoever set it up.

## Sending results back

One JSON file per run. They are small, they are text, and they contain
no acquired data — reading one through before sending is quick and
worth doing if anything in it looks sensitive.

If a run fails, send the file anyway. Every probe is wrapped so that a
failure is recorded as data rather than stopping the run, and a report
full of errors still says which questions the machine could not answer
and why — which is itself an answer.
