# Instrument survey: a runbook for the SuperSTEM team

This project is being built to drive SuperSTEM's instruments, and a
number of design decisions currently rest on guesses about them. Each
guess is cheap to settle at the instrument and expensive to get wrong:
the Gatan question alone is the difference between "already supported"
and several weeks of adapter work.

`scripts/superstem_survey.py` asks those questions. It is a single file
with no dependencies, it reads and never writes, and it produces one
JSON file to send back. Reading it before running it is encouraged — it
is about 850 lines, most of them explaining themselves.

This page is the runbook: what to run, on which machine, in which
interpreter, and what the script deliberately leaves for a human.

## Getting the script to the instrument

**For the SuperSTEM team, there is a hosted page that needs no git, no
GitHub account and no package index:**

> **<https://claude.ai/code/artifact/f60f60c4-8cc4-4a30-8109-427cb53ad4ef>**

It carries a download button, the full source to read first, and a
condensed version of this runbook. Two things about it are worth knowing
before sending the link on:

- **It downloads as `superstem_survey.txt`, not `.py`**, because the
  host only permits an allowlist of extensions and `.py` is not on it.
  **No rename is needed** — Python runs a file whatever its extension is,
  so `python superstem_survey.txt --check` works as-is. The page says so
  too, in those words, because an instrument scientist handed a `.txt`
  will otherwise reasonably assume it is broken.
- **The page is private until shared.** It has to be shared from the
  artifact's own share menu before anyone at Daresbury can open it.

Anyone who can reach GitHub can of course take
`scripts/superstem_survey.py` from the repository instead; it is the same
file, and the hosted page names the revision it was built from so the two
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

No section reads or transmits acquired data. The JSON report contains
instrument capability, versions and file paths — no images, no
spectra, no credentials.

## Python environment

**Nothing needs installing.** No pip, no virtual environment, no
network access to a package index. The script imports only the standard
library, plus the instrument's own `nion` packages when running the Nion
section.

**Python 3.7 or newer.** That floor is set by Gatan Microscopy Suite,
which embeds 3.7 and is therefore the oldest interpreter plausibly
sitting near this hardware. On anything older the file will not parse,
and the failure is a `SyntaxError` rather than a useful message, so
check first:

```
python -c "import sys; print(sys.version)"
```

Then run the preflight, which touches no hardware and opens no socket:

```
python superstem_survey.py --check
```

Every command below names the file `superstem_survey.py`. If you took it
from the hosted page it will be called `superstem_survey.txt` instead —
substitute the name and change nothing else. Python does not care about
the extension.

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

## Run 1 — SuperSTEM 2 (Nion UltraSTEM 100)

On the instrument control computer, **from Nion Swift's Python console**
while Swift is running:

```
python superstem_survey.py --nion --out superstem2.json
```

This is the highest-value run of the three. The question it exists to
answer is whether the stem controller exposes an `eels_camera`, and
whether it publishes a `ZLPoffset` control. If it does both, the UHV
Enfina is reached through Nion, the spectrometer is already drivable,
and no Gatan-side code is needed for SuperSTEM 2 at all. If it does
neither, an adapter that has to live inside Gatan Microscopy Suite comes
back onto the plan.

The run also records the scan channel count and channel names, which
settle how many signals one pass reads out, and reads a list of named
controls to see which of them exist. Every control is read; none is set.

Safe to run during a session. It is a sequence of reads, and the largest
risk it carries is that it reports nothing useful because it was run
from the wrong interpreter.

## Run 2 — HERMES DECTRIS control unit

From any machine on the control network:

```
python superstem_survey.py --dectris 192.168.1.10 --out hermes.json
```

Use the address of the **control unit**, not the detector head — SIMPLON
is served by the DCU on port 80.

This settles which SIMPLON API version answers, which configuration keys
an ELA actually publishes, and whether the `monitor` subsystem is
enabled. The adapter was written against published documentation and
open-source clients; several of its assumptions are guesses that a
single `GET` each can confirm or kill.

Safe to run mid-experiment. Everything is a read, and the detector state
in the report will show if an acquisition is in progress.

## Run 3 — SuperSTEM 4 (Hitachi SU9000II)

On the SU9000II control computer:

```
python superstem_survey.py --hitachi --out superstem4.json
```

This looks for three undocumented external-control modules —
`MfExtCont`, `MfKeyMouse`, `MfCommon` — that are evidenced on an SU7000
and may or may not exist on an SU9000II. Whether they are present is the
single fact that decides between an adapter we can write in a couple of
weeks and a conversation with Hitachi.

Because the answer is only meaningful from the right interpreter, please
run this once per Python installation on that machine if there is more
than one, and send all the reports. A "not found" from an interpreter
the vendor software never installed into is not evidence of absence.

Safe to run: nothing is imported, so nothing connects to the column.

## What the script cannot answer

Two things need an operator, hardware time, and a decision that it is
worth the beam. Neither is urgent, and both are worth scheduling
together.

**The DECTRIS trigger arithmetic.** Our adapter uses SIMPLON's `ints`
mode with `nimages=1` and `ntrigger=65536` — one image per software
trigger. LiberTEM-live uses the same mode with the numbers the other way
round (`nimages` = the whole series, `ntrigger=1`). Both cannot be right
about what one `trigger` does. Settling it requires arming the detector
and sending a trigger, which is precisely the thing the survey refuses
to do, so it has to be a deliberate act with somebody watching. The
outcome is one bit: does one `trigger` in `ints` produce one image, or
start the whole series?

**What drives SuperSTEM 4's EELS spectrometer and diffraction camera.**
Make, model, and which software owns each. The published description
does not say, and it changes which adapter the spectrometer needs.

## Sending results back

One JSON file per run. They are small, they are text, and they contain
no acquired data — reading one through before sending is quick and
worth doing if anything in it looks sensitive.

If a run fails, send the file anyway. Every probe is wrapped so that a
failure is recorded as data rather than stopping the run, and a report
full of errors still says which questions the machine could not answer
and why — which is itself an answer.
