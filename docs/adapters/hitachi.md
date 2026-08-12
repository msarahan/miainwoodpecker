---
orphan: true
---

# Hitachi: what it would take to drive SuperSTEM 4

[docs/vendor-support.md](../vendor-support.md) has, until now, said this
about Hitachi:

> No public API. Step one is a vendor conversation. If the answer is "no
> API", the honest options are file-watching whatever EM Flow Creator
> writes, or nothing.

That was a placeholder written without a search. This page is the search,
aimed at one specific instrument: **SuperSTEM 4 at Daresbury, a Hitachi
SU9000II** — an ultra-high-resolution cold-FEG **FE-SEM/STEM**, 1–30 kV.

**A note on how this page was produced**, because it matters for reading
it. It was first researched against a Hitachi **HF5000** (a 200 kV
TEM/STEM), and re-targeted mid-task when the facility owner confirmed the
actual machine. Findings that were specific to the TEM line have been
discarded. Findings about **Hitachi's software stack as a whole** have
been kept, and are marked as generalisations wherever they are not
citable against the SU9000II itself. As it turns out the correction
improves the answer considerably: the strongest evidence found is about
Hitachi's **SEM** line, which is the SU9000II's lineage and was not the
HF5000's.

## The headline

**The placeholder was wrong, and in a useful direction.** Two findings
change it.

**1. A Python control interface for Hitachi FE-SEMs exists, is used by
third parties in the wild, and is undocumented.** Public code drives a
Hitachi SU7000 field-emission SEM through modules named `MfExtCont`,
`MfKeyMouse` and `MfCommon`, calling `EXT.SetHv()`,
`EXT.GetStagePosition()`, `EXT.RunStageMove()`, `EXT.RunAutoAfc()`,
`EXT.RunScan()` and `EXT.RunCapture()`. The SU7000 is an FE-SEM on the
same product lineage as the SU9000II. That is **not proof** it is present
on an SU9000II, but it moves the question from "does Hitachi have
anything?" to "is the thing they have fitted to *this* machine?" — and
that question is answered by looking at the instrument PC, not by a
vendor negotiation.

**2. On a SEM, the scan is buyable from someone else.** This is the
single biggest thing the re-target changes. SEMs have carried a dedicated
**external scan connector** for decades — X and Y beam-position voltages
in, video out — originally so EDS and X-ray mapping systems could drive
the beam. Third-party scan generators plug into it and are a product
category, not a hack: point electronic's **DISS6** acquires **4 analog
and 12 digital inputs simultaneously** and ships an SDK with
documentation and demo source. On a 200 kV TEM that route barely exists.
On this instrument, a scanned image is reachable *even if Hitachi says
no*.

So the finding is: **partial and undocumented, with an unusually good
fallback.** Confidence that *some* programmatic control surface exists for
this product family: **high**. Confidence it is present, licensed and
usable on SuperSTEM 4: **unknown**, and cheap to settle.

**Scope note**, same as the parent page: nothing here is a commitment to
build any of it, and the migration plan's rule holds — no second vendor
adapter until someone has that instrument. This is a map and a list of
questions.

**Out of scope, deliberately.** The instrument carries an **Oxford
Instruments Ultim® Extreme EDS detector**. The EDX/spectrum-detector
interface — Oxford and Bruker both — is owned by a separate piece of
work, and nothing about it is researched or costed here. Where an EDS
path is relevant to a decision below it is named and handed off, not
analysed.

## The instrument

From the facility owner, and treated here as authoritative:

- 1–30 kV **cold FEG**, 0.3 eV native energy spread
- **EELS spectrometer and diffraction camera**, EELS across 3 kV–30 kV
- **BF and segmented HAADF** STEM detectors; **SE, LA-BSE and HA-BSE**
  SEM detectors
- **Oxford Instruments Ultim® Extreme** silicon-drift EDS
- STEM resolution 0.2 nm; SE resolution 0.4 nm at 30 kV
- Heating and biasing holder; Hitachi in-situ side-entry holders

Corroborating the platform: SuperSTEM describes SuperSTEM 4 as a Hitachi
SU9000II with an EELS spectrometer for 3–30 kV EELS, a diffraction
camera, an Oxford Ultim Extreme EDS and a range of detectors and holders
*(reported — [superstem.org](https://www.superstem.org/news))*. Hitachi's
own SU9000II page describes a side-entry stage of the kind found in
high-end TEMs, simultaneous bright-field and annular dark-field detection
with the dark-field detector settable to 56 positions, and an upper
detector using a Super ExB filter to separate SE from LA-BSE while a top
detector takes HA-BSE *(reported —
[Hitachi High-Tech SU9000II](https://www.hitachi-hightech.com/eu/en/products/microscopes/sem-tem-stem/fe-sem/su9000.html),
[SU9000II brochure PDF](https://milexia.com/products/wp-content/uploads/sites/7/2022/08/Hitachi-SU9000%E2%85%A1.pdf))*.

**Low-kV EELS is unusual, and the spectrometer's maker is the important
unknown.** Published low-voltage STEM-EELS on this platform used a
**Hitachi** EEL spectrometer — described as "a Hitachi electron
energy-loss spectrometer", with a CCD of 1024 (dispersion) × 256
(integration) pixels — on an SU9000EA *(reported — Ultramicroscopy 2024,
["Elemental quantification using electron energy-loss spectroscopy with a
low voltage scanning transmission electron microscope
(STEM-EELS)"](https://www.sciencedirect.com/science/article/pii/S0304399124000561))*.
Separately, CEOS states that its **CEFID** energy filter is compatible
with microscopes from JEOL, Hitachi High Technology and Thermo Fisher
*(reported — [CEOS CEFID](https://www.ceos-gmbh.de/en/produkte/cefid))*,
so a third-party spectrometer on a Hitachi column is a real
configuration. **Which of these SuperSTEM 4 has is unverified**, and it
matters more than almost anything else on this page: a Hitachi
spectrometer is another thing behind the vendor's wall, and a CEOS or
Gatan one is a detector this project could reach on its own terms.

## How to read the confidence markers

This vendor publishes very little, so the difference between "I read
this" and "I inferred this" carries most of the weight.

- **Verified** — I read the primary artefact (source code, a repository
  listing, a spec sheet) and can point at it.
- **Reported** — a search index returned it and the URL is real, but the
  page was **not reachable from this environment**: outbound HTTPS here
  is whitelist-based, and `hitachi-hightech.com`, `superstem.org`,
  `pointelectronic.de`, `sciencedirect.com`, `arxiv.org` and
  `forum.image.sc` are all blocked. Quoted phrasing is as returned by
  search, so treat it as approximate.
- **Generalising** — established for Hitachi's SEM line or software stack
  broadly, not cited against the SU9000II.
- **Unverified** — asserted somewhere without a checkable source, or
  inferred by me. Every one is listed again at the bottom with what would
  settle it.

A confidently wrong claim about a vendor's API is worse than an admitted
gap. The list at the bottom is as much the point of this page as the
findings are.

## 1. What drives an SU9000II

**The column, the GUI and the automation product are Hitachi's.** The
SU9000II offers automated adjustment of the optical system and can be
equipped with **EM Flow Creator** as an option for automated data
acquisition *(reported — Hitachi's SU9000II page)*. EM Flow Creator is a
recipe engine: users pick procedural blocks from a list — accelerating
voltage, stage translation, autofocus — set parameters, and drag them
into order, with sequencing, looping and condition-based branching. For
steps the block list does not cover, *"EM Flow Creator can execute
scripts written in Python"* *(reported — Hitachi SI-NEWS,
["Automation of SEM Observation Workflow Using EM Flow
Creator"](https://www.hitachi-hightech.com/global/en/sinews/technical_explanation/130328/),
[Japanese PDF](https://www.hitachi-hightech.com/file/jp/pdf/sinews/technology/6220327.pdf))*.
Note that this article is about **SEM** workflows specifically, which is
this instrument's lineage — the one piece of the earlier HF5000 research
that landed better after the correction than before it.

The same product is named as an option across the current FE-SEM range —
SU8600/SU8700 *(reported —
[SI-NEWS](https://www.hitachi-hightech.com/global/en/sinews/technical_explanation/130316/))*
and the October 2025 SU9600 *(reported —
[Hitachi press release](https://www.hitachi.com/en/press/articles/2025/10/1031a/))*,
which also ties the SEM line into Hitachi's "HMAX for Industry" /
Lumada 3.0 data-service story. That last point is worth watching but is
a data-platform play, not a control API.

**I could not find a product name for the SU9000II's day-to-day control
GUI.** Thermo Fisher, JEOL and Zeiss all name their control software in
public; Hitachi appears not to. *(Unverified — searches for "PC-SEM" and
similar returned nothing authoritative.)*

**One search hazard, recorded so it does not cost anyone else an hour**:
on Hitachi TEM spec sheets, **"API" means "auto pre-irradiation"** — the
HT7800's standard feature list reads "…Low dose, API (auto
pre-irradiation), Image navigation function…" *(reported —
[HT78 series listing](https://www.medicalexpo.com/prod/hitachi-high-technologies/product-80782-1075154.html);
generalising — this is a TEM spec sheet, but the abbreviation habit is
the vendor's)*. Searching "Hitachi API" surfaces it, and it is not what
anyone means.

## 2. Does an API exist?

### The hard evidence, and it is on the right product line

Two public GitHub repositories drive a **Hitachi SU7000 field-emission
SEM** from Python. Both import the same three modules:

```python
from MfKeyMouse import *
from MfExtCont import *
from MfCommon import *
```

and call a single object `EXT` with methods including
`EXT.GetStagePosition()`, `EXT.RunStageMove()`, `EXT.SetMagnification()`,
`EXT.SetHv()`, `EXT.RunAutoAbc()` (auto brightness/contrast),
`EXT.RunAutoAfc()` (autofocus), `EXT.RunAutoAsc()` (auto stigmation),
`EXT.GetPhotoSize()`, `EXT.RunScan()` and `EXT.RunCapture()`. *(Verified
— I read
[`wilgardner/sem-scripts/autoMontage.py`](https://github.com/wilgardner/sem-scripts/blob/main/autoMontage.py),
whose README describes itself as using "the Python API for the Hitachi
SU7000 Field Emission SEM", and
[`Ajay-Talbot/high-throughput-AM`'s `sample03_SU7000mod`](https://github.com/Ajay-Talbot/high-throughput-AM/blob/main/Auto-SEM/sample03_SU7000mod%20-%20Copy.txt).)*

Read the method names against `devices/interface.py`
and the fit is uncomfortably good: `SetHv`/`GetStagePosition`/
`RunStageMove` are `InstrumentController` almost verbatim; `RunScan` and
`GetPhotoSize` are the bones of `Scanner`. If this exists for the
SU9000II, this is a **level 2** interface (see below) and a normal
adapter.

That second filename carries more weight than its contents.
**"sample03"** is how a vendor names the third file in a shipped examples
folder, not how a researcher names their own script; `MfExtCont` reads as
*external control*, with `Mf` a prefix across a module set. The strong
reading — **unverified** — is that Hitachi ships a Python external-control
layer with sample scripts to FE-SEM customers.

Three things this does **not** establish:

- **It is not documented anywhere public.** A GitHub-wide code search for
  `MfExtCont` returns exactly three files, two of which are the ones
  above. *(Verified.)* No reference manual, no method list, no units, no
  error model, no statement about threading or reentrancy.
- **It is not on PyPI and there is no community wrapper**, in contrast
  with `temscript`, `deapi`, `pymmcore` and `fibsem`. *(Reported — an
  easy negative for a reader to falsify, which is the best kind.)*
- **It is SU7000 evidence, not SU9000II evidence.** Same vendor, same
  FE-SEM family, different model and possibly a different software
  generation. *(This is the central unverified claim on the page.)*

### The scriptability trap

The most useful distinction this page can offer is one the vendor's
marketing does not draw. There are three levels, and only the second is
an adapter.

**Level 1 — a recipe engine with a Python escape hatch.** EM Flow
Creator. The instrument owns the control loop; your code is a block the
engine calls when it reaches it. You can compute inside the block, but
the *experiment* is the recipe, not your program.

**Level 2 — a callable control library on the instrument PC.** What
`MfExtCont` looks like. *Your* process owns the loop and calls the
microscope. This is what `temscript`, PyJEM, AutoScript and SmartSEM's
OCX all are, and it is the only level at which this project's `Scanner`,
`Camera` and `InstrumentController` protocols can be satisfied.

**Level 3 — a network protocol.** DECTRIS's SIMPLON, Merlin's TCP
interface. Nobody needs this from a column vendor: the parent page's
[transport section](../vendor-support.md#transport-why-every-adapter-is-a-subprocess)
already establishes that an adapter is a subprocess, which can run on the
instrument PC with the application on an operator's laptop. Level 2 plus
a subprocess *is* remote operation.

**Level 1 is the Gatan problem, and the parent page already diagnosed
it.** GMS 3's Python runs inside DigitalMicrograph and cannot be executed
from outside, so a Gatan adapter is "not a subprocess this client
launches — it is a bridge running inside DM that connects *out*. Same
wire protocol, opposite direction, and the one detector case the current
design does not fit." A Python block inside an EM Flow Creator recipe has
exactly that topology. It is buildable; it is a different architecture
from every other adapter here; and it carries a limitation no engineering
removes: **a recipe engine cannot give you an interactive session.** The
recipe has to be running, and while it is, the operator has whatever
control the recipe left them.

### Where else I looked, and found nothing

Absence of evidence is a finding only if the search is stated. All
**verified** unless marked.

| Where | Hitachi support? | What is there instead |
|---|---|---|
| [RosettaSciIO](https://github.com/hyperspy/rosettasciio) reader directories | **None** | `jeol`, `bruker`, `tia` (Thermo/FEI), `phenom`, `edax`, `digitalmicrograph`, `quantumdetector`, `arina` (DECTRIS), `pantarhei` (CEOS), `tvips`, `hamamatsu`, `delmic`, `semper`, `impulse` |
| RosettaSciIO's TIFF reader, vendor branches | **None** | `_is_zeiss`, `_is_fei`, `_is_tvips`, `_is_olympus_sis`, `_is_jeol_sightx` — no Hitachi predicate |
| Micro-Manager [`mmCoreAndDevices/DeviceAdapters`](https://github.com/micro-manager/mmCoreAndDevices/tree/main/DeviceAdapters) | **None** | …and none for Zeiss, FEI, Thermo or JEOL either. **Weak evidence**: Micro-Manager's adapter set is a light-microscopy set, so its silence says little about EM columns. Recorded rather than omitted, because leaving it out would overstate the rest. |
| [`instamatic`](https://github.com/instamatic-dev/instamatic) | **None** | JEOL via TEMCOM, FEI via its scripting interface |
| [OpenFIBSEM / `fibsem`](https://github.com/DeMarcoLab/fibsem) | **None** | "OpenFIBSEM currently supports ThermoFisher and TESCAN hardware"; others "planned" |
| [Open Beam Interface](https://github.com/nanographs/Open-Beam-Interface) supported list | **None** | JEOL 35C/840/63xx/T330 and FEI xT fully; some FEI xP partially |
| The autonomous-microscopy literature and [AEcroscopy](https://pubmed.ncbi.nlm.nih.gov/38639016/) | **None found** *(reported)* | The ML-driven and autonomous-experiment work runs on Nion Swift and Thermo Fisher AutoScript; the vendor scripting layers it names are AutoScript/TEMScripting, Nion Swift, JEOL TEMCON |

The last row is the one I most wanted to find and did not. People doing
closed-loop microscopy have solved this repeatedly — on Nion and on
Thermo columns. **I found no published work driving a Hitachi FE-SEM/STEM
programmatically beyond the two SU7000 script repositories above.**
*(Reported — a negative from search, not a systematic literature review.)*

The community's own read, from a 2020 thread asking exactly this
question, is that Zeiss publishes an API (SmartSEM, VB.NET or C++) and
that automating a Hitachi FE-SEM means getting Hitachi to supply
something or writing it yourself against their software libraries
*(reported —
[ResearchGate](https://www.researchgate.net/post/How-can-I-automate-image-collection-on-the-Hitachi-FESEM))*.
That is consistent with everything above: the interface exists, and it is
not published.

## 3. What is reachable without a control API

The parent page argues that on a real instrument the camera is usually
**not** the column vendor's, and that going through the column vendor to
reach it is often the harder path. On a SEM that argument extends further
than it does on a TEM — because on a SEM you can also buy the *scan*.

### The scan is a purchasable part

Every signal this project most wants from SuperSTEM 4 — BF, segmented
HAADF, SE, LA-BSE, HA-BSE — is a **scanned** signal: the detector output
is meaningless until something rasters the probe and digitises in step.
On a TEM/STEM that scan is almost always the column vendor's. On a SEM it
need not be:

- **The external scan connector.** SEMs carry a dedicated connector for
  external beam control — X and Y position voltages into the scan
  amplifier, video out — historically provided so EDS and X-ray mapping
  systems could drive the beam. *(Reported —
  [Hackster's write-up of the Open Beam Interface](https://www.hackster.io/news/the-open-beam-interface-offers-digital-image-capture-from-almost-any-scanning-electron-microscope-17bb72c5d250);
  generalising — this is a SEM-industry norm, not an SU9000II
  datasheet claim.)*
- **point electronic DISS6** is a commercial scan generator and image
  acquisition system for SEMs: rectangular and vector scans, **4 analog
  and 12 digital inputs acquired simultaneously**, 10 ns minimum dwell,
  and **an SDK with documentation and demo source code for integration
  into control and acquisition applications**. point electronic describe
  themselves as vendor-independent and supply electronics and software to
  upgrade SEMs from all major manufacturers. *(Reported —
  [DISS6](https://www.pointelectronic.de/en/parts/smart-controllers/diss6-imaging-scanner-sem/),
  [digital image scanning](https://www.pointelectronic.de/en/systems/digital-image-acquisition/).
  Whether they have fitted an SU9000II specifically is **unverified**.)*
- **Open Beam Interface** is the open-hardware version: Glasgow-based
  FPGA, two DACs for beam position and **one** ADC for detector signal,
  CERN-OHL-W. *(Verified — repository README.)* One analog channel is the
  catch: it cannot capture a segmented HAADF, so it is an interesting
  proof of the concept rather than a fit for this instrument.

This is the most consequential difference between the instrument I was
first asked about and the one that is actually there. **Even the worst
vendor answer leaves a viable route to scanned imaging on SuperSTEM 4** —
a documented third-party SDK with enough simultaneous channels for the
whole detector set, plugged into a connector the column already has. It
costs hardware and it costs a channel-mapping calibration, and it is
real.

Two honest caveats. First, an external scan generator gives you beam
position and digitised video; it gives you **nothing** about lens
excitation, working distance, stage or high tension, so
`InstrumentController` still needs the column. Second, driving a beam
through an analog connector on a 30 kV instrument without the vendor's
blessing is a conversation to have with the facility before it is a
conversation to have with an engineer.

### The rest of the instrument

- **EELS spectrometer.** If it is a CEOS or Gatan unit, it has its own
  software and its own interface, and the parent page already charts
  Gatan's awkward topology. If it is Hitachi's own — which is what the
  published low-kV work on this platform used — it is behind the same
  wall as the column. **Unverified, and the highest-value unknown after
  the control API itself.**
- **Diffraction camera.** Vendor not stated by the owner. If it is a
  Merlin, a DECTRIS, or a TVIPS, it is reachable today: the parent page
  estimates each, LiberTEM-live already wraps two of them, and the
  detector-only server path works end to end
  (`tests/unit/test_out_of_tree_server.py`). **Unverified.**
- **Oxford Ultim Extreme EDS.** Out of scope here by assignment; see the
  spectrum-detector work. The one thing worth flagging across the
  boundary is that an EDS stream is not a `Camera` — it is the
  spectrum/map protocol the parent page says Bruker ESPRIT would want,
  and which does not exist in this project yet.
- **Stage, high tension, focus, blanker.** The column, always. Nothing
  bolted to the side of a SEM moves its stage.

**The conclusion.** Without Hitachi, this project could plausibly drive
*a scan and its detectors* on SuperSTEM 4 — which is more than could be
said for a TEM — but not the optics or the stage. With Hitachi, it is a
normal second-vendor adapter. The vendor conversation is worth having
first, because scenario A is much cheaper than scenario C.

## 4. A concrete framework finding: the `Scanner` protocol cannot express this instrument

This one needs no vendor answer and is checkable against the code today.

SuperSTEM 4 produces **BF, segmented HAADF (several segments), SE, LA-BSE
and HA-BSE simultaneously from a single pass of the beam** — Hitachi
markets simultaneous BF/DF detection and simultaneous SE/STEM acquisition
as features of this platform *(reported — Hitachi SU9000II page; and
["Simultaneous secondary electron microscopy in the scanning transmission
electron microscope"](https://academic.oup.com/jmicro/article/73/2/169/7604380),
Microscopy 2024, on why simultaneity is the point)*.

The protocol says *(verified — `devices/interface.py`)*:

```python
def scan_frame(self, parameters: ScanParameters, channel: int = 0) -> Frame: ...
```

one frame, one channel, per call. And `Frame`'s docstring justifies the
absence of a grouping id explicitly:

> **No `scan_id`.** Nion carries one to group the channels of a single
> simultaneous multi-channel scan. This interface has no such call — a
> second channel is a second `scan_frame`, and therefore a second pass of
> the beam — so an id claiming to group them would be a fiction.

That reasoning is sound *given the current call shape*, and this
instrument is where the premise underneath it stops being true. The Nion
adapter matches the protocol faithfully — `NionScanner.scan_frame` calls
`self._device.get_scan_data(frame_parameters, channel)` once per call and
increments `_frame_index` per call *(verified — `devices/nion_server.py`)*
— so on a five-channel instrument, five channels of one pass would arrive
as five unrelated frames with five different frame indices.

Three consequences, in increasing order of seriousness:

- **Dose.** *k* channels costs *k* passes. On a beam-sensitive specimen
  at low kV that is not a performance detail, it is the difference
  between a valid measurement and a damaged one — and it throws away data
  the hardware already produced in the first pass.
- **Provenance.** Nothing marks five frames as one scan. `frame_index` is
  per-device-per-call, so a reader cannot reconstruct which frames were
  the same region.
- **Correctness, for the segmented detector specifically.** Segmented-
  detector methods — DPC, iDPC, centre-of-mass — take *differences
  between segments at the same probe position*. Segments acquired on
  different passes differ by drift as well as by signal, and the
  difference of two drifted images is drift, not a phase gradient. The
  protocol can carry the arrays; it cannot produce data those methods are
  valid on.

**The fix is not `scan_id`** — adding an id to frames from different
passes would be exactly the fiction the docstring rightly refuses. The
fix is the *call*: a multi-channel form that performs one pass and
returns one frame per requested channel, with the single-channel
`scan_frame` kept as the convenience it is. Something like:

```python
def scan_frames(
    self, parameters: ScanParameters, channels: typing.Sequence[int]
) -> typing.Sequence[Frame]: ...
```

with each returned frame carrying a shared scan identity **only when the
device really did acquire them together** — a device that internally
loops must not claim otherwise, or the metadata becomes the lie the
current design was avoiding. Nion's own scan layer supports simultaneous
multi-channel acquisition, so the existing adapter can implement this
honestly rather than by looping.

**A second, smaller gap**: a segmented detector needs per-segment
*geometry* — which segment sits where, and over what collection angles —
or the segment arrays cannot be combined. `channel_names` is a flat list
of strings and carries none of it. That belongs in per-channel frame
metadata (detector name, segment index, azimuthal range, inner/outer
collection angle) rather than in a new type, in the same spirit as the
existing vocabulary.

Neither should be built speculatively — the parent page's rule is that
interface changes arrive with a caller that needs them. But unlike the
target-name redesign, this one has a named instrument behind it, and it
is worth recording that **a segmented-detector SEM is the caller.**

Estimate: **3–5 d**, of which about a day is deciding the shape.

## 5. What file-watching actually gives you

The placeholder floated this in one clause. It is better than that
suggests, and it is not a device adapter.

**The format is known and already parsed — and this time it is the SEM
format, which is the one that matters.** Hitachi microscopes write a
**TIFF plus a `.txt` metadata sidecar with the same stem**, the sidecar
opening with a `[SemImageFile]` (or `[TemImageFile]`) section followed by
key/value pairs. FAIRmat's `pynxtools-em` has a parser for exactly this —
[`image_tiff_hitachi.py`](https://github.com/FAIRmat-NFDI/pynxtools-em/blob/main/src/pynxtools_em/parsers/image_tiff_hitachi.py),
docstring "Parser for harmonizing Hitachi-specific content in TIFF
files", which refuses to run without both files ("Parser needs TIF and
TXT file!"). *(Verified — I read the parser and its
[configuration](https://github.com/FAIRmat-NFDI/pynxtools-em/blob/main/src/pynxtools_em/configurations/image_tiff_hitachi_cfg.py).)*

The mapped keys line up with this project's frame-metadata vocabulary,
which is less of a coincidence than it looks — both were derived from
what an electron microscope actually reports:

| Hitachi sidecar key | pynxtools-em NeXus target | This project's `Frame` metadata |
|---|---|---|
| `AcceleratingVoltage` | `instrument/ebeam_column/electron_source/voltage` | `high_tension_v` — **the same NeXus path named in `HIGH_TENSION_V_KEY`'s docstring** |
| `PixelSize` | converted to metres | `calibration` scale |
| `Magnification` | `instrument/optics/magnification` | no home; converts to `fov_nm` given a per-instrument constant |
| `WorkingDistance` | `instrument/optics/working_distance` | no home |
| `EmissionCurrent`, `FilamentCurrent` | `…/electron_source/…` | `beam_current_a` is nearest, and is not the same quantity |
| `InstructName`, `SerialNumber` | `model`, `serial_number` | session/instrument identity |

**RosettaSciIO does not read it** — no `hitachi` reader directory, and no
Hitachi branch in the TIFF reader *(verified)* — so a Hitachi file
arriving in a HyperSpy workflow today gets its pixels through `tifffile`
and loses its calibration and instrument state. That is the gap this
closes.

**What it costs here is not zero, for a reason already documented in this
repo.** `pyproject.toml` deliberately installs plain `pynxtools`, not
`pynxtools[em]`, because the `[em]` extra pulls
`pynxtools-em → kikuchipy → numba 0.53.1 → llvmlite 0.36.0`, which is
both the ~70-package dependency Phase 3 avoided and unbuildable on Python
3.12. So this path either **re-implements the sidecar parse** — it is an
INI-ish key/value file, so this is small — or isolates `pynxtools-em` in
its own environment the way the schema-validation env already isolates
`pynxtools`. Re-implementing is the better answer, and the parser above
is the specification to re-implement against.

**What it gives you:** calibrated images with instrument state, ingested
into sessions and written as NeXus like everything else, with no vendor
cooperation, no licence and no risk to a live column.

**What it cannot give you, and the design conclusion.** No control of any
kind, no per-frame timing beyond what the sidecar records, no knowledge
of what the operator did between two files, and no closed loop — which is
what this project is for. And a subtler point, because getting it wrong
would put a lie in the protocol: **a file-watcher must not be dressed up
as a `Camera`.** `acquire_frame()` is documented to return the next
available frame; a watcher's version blocks until a human clicks Capture,
which is a different contract and would silently break the acquisition
sequences, the live viewer and `park()`. A file-watcher belongs in the
**storage/ingest** layer as a reader, not in `devices/` as a device. That
keeps it honest and keeps it useful the day a real adapter arrives.

One caveat inherited from the parser, quoted because it is the kind of
thing that silently corrupts a dataset: it assumes *"a very specific
coordinate system"* without external confirmation, and flips every image
vertically. Verify the handedness against a known specimen before
trusting a stage coordinate. *(Verified — the comment is in the source.)*

It is also worth expecting that **EELS and EDS will not arrive this way.**
The spectrometer and the Oxford EDS write their own vendors' formats, so
"file-watching the Hitachi output" covers images and not spectroscopy.

## 6. The vendor conversation

Sendable as-is. Ordered so that a "no" to the first few ends the call
early, which is a feature.

**Ask the site first — these are not Hitachi's to answer:**

0a. **Who makes the EELS spectrometer and the diffraction camera on
SuperSTEM 4?** Make, model, and which software drives each.
→ *Unlocks:* whether spectroscopy and diffraction are reachable without
Hitachi at all. A CEOS or Gatan spectrometer and a Merlin/DECTRIS camera
are already-charted work; a Hitachi spectrometer is behind the same wall
as the column.

0b. **Is anything already plugged into the external scan connector**, and
is the connector present and available?
→ *Unlocks:* scenario C, and whether a third-party scan generator is a
retrofit or a re-plug.

0c. **What is on the instrument PC?** Specifically: are there files named
`MfExtCont*`, `MfKeyMouse*`, `MfCommon*`, or a folder of Hitachi Python
sample scripts? Is EM Flow Creator installed and licensed?
→ *Unlocks:* the entire question below, in about five minutes, without
talking to anyone. **This is the cheapest experiment on this page and it
should be done before the vendor call, not after.**

**To Hitachi High-Tech:**

1. Is there a **programmatic control interface** for the SU9000II that a
   customer's own program can call — as opposed to a recipe the
   microscope's software runs? We are aware of Python modules named
   `MfExtCont`, `MfKeyMouse` and `MfCommon` used against an SU7000 FE-SEM.
   **Is there an equivalent for the SU9000II, and what is it called?**
   → *Unlocks:* everything. This is the whole call in one question.
2. If yes: is it a **library we import into our own Python process**, or
   does our code run inside Hitachi's application? Which Python version
   and architecture, and can it be loaded by a process we launch
   ourselves on the instrument PC?
   → *Unlocks:* whether this is scenario A or scenario B, and whether
   this project's subprocess model works unchanged.
3. **How is it licensed and obtained?** A purchasable option, a
   support-contract entitlement, or a developer agreement? Is there an
   NDA, and does it permit publishing an **MIT-licensed adapter that
   calls it** without redistributing any Hitachi code?
   → *Unlocks:* whether the work can live in the open or must be a
   site-local package.
4. What is the **documented surface** — is there a reference manual with
   a method list, units, error behaviour and threading rules, and may we
   see it (or a redacted version) **before** committing to purchase?
   → *Unlocks:* whether the estimates below are estimates or guesses.
5. Is there an **offline or simulator mode**? Thermo Fisher's `temscript`
   ships a dummy implementation, which is what made this project's Nion
   adapter testable without hardware.
   → *Unlocks:* CI. Without it, every change costs instrument time.
6. Which of these can be **read and written**: stage position (units and
   axis convention), working distance / focus, accelerating voltage, beam
   current or probe current, beam blanking, magnification or field of
   view, scan rotation, scan centre, pixel dwell time, scan resolution,
   and per-detector gain and offset?
   → *Unlocks:* a direct map onto `ScanParameters`,
   `InstrumentController` and the `Frame` vocabulary. Gaps here become
   documented gaps in the adapter, not commissioning-day surprises.
7. Can a program **request a scan and receive the pixel data** for named
   detector channels, or does image acquisition only ever write a file?
   And critically: **can it receive several channels from one pass** —
   BF, each HAADF segment, SE, LA-BSE, HA-BSE — with the channels
   pixel-aligned?
   → *Unlocks:* the difference between a real `Scanner` and a
   file-watcher in a costume, and it is the question §4 above exists to
   make precise. A one-channel-at-a-time interface would not support the
   segmented detector correctly no matter what we build on top.
8. For the **segmented HAADF**, what is the segment geometry — how many
   segments, at what azimuthal positions, and over what collection
   angles at a given camera length and working distance? Is that
   reported by the software, or is it a constant we must record?
   → *Unlocks:* whether segment metadata can be read or must be
   configured per site. Without it the segment arrays cannot be combined.
9. Does the instrument report **magnification, field of view, or both**,
   and is there a documented conversion? Our scan geometry is a field of
   view in nanometres spanning the longer axis.
   → *Unlocks:* calibration — the expensive-to-get-wrong conversion for
   any vendor that thinks in magnification.
10. **Is EM Flow Creator licensed for this instrument**, and what can its
    Python blocks do — can a block open a network socket and hold a
    session open while an external program steers the recipe, or is a
    block a short-lived non-interactive step?
    → *Unlocks:* scenario B. A block that can hold a socket is a bridge;
    one that cannot is a batch job.
11. Is the **external scan connector** present, specified, and supported
    for third-party scan generators, and does using one void anything?
    Which detector outputs are available as analog video?
    → *Unlocks:* scenario C, and turns it from "probably possible" into
    a supported configuration or a refusal.
12. What **file format and metadata** does the SU9000II write for SEM and
    STEM images, and is the `.txt` sidecar key set documented?
    → *Unlocks:* the fallback, and whether we implement the sidecar parse
    against a specification or against samples.
13. Do you have **customers automating an SU9000-series instrument
    programmatically**, and could you put us in touch?
    → *Unlocks:* the fastest route of all — one existing integrator saves
    the whole discovery phase.

### The licensing pattern to expect

The parent page describes three comparable vendors, and Hitachi is likely
to be a blend:

- **Zeiss** — SmartSEM's `CZEMApi.ocx` **requires an agreement with
  Zeiss** before you may develop against it. The pattern that dominates
  schedule.
- **JEOL** — PyJEM's docs are public but the package is not on PyPI; it
  lives on the TEM control PC. The pattern where the code is real, usable
  and simply not distributed.
- **Thermo Fisher** — COM scripting ships with the microscope; AutoScript
  is a paid add-on. The pattern where money, not permission, is the
  barrier.

Hitachi publishes neither documentation nor package, and its FE-SEM
Python layer appears to arrive on the instrument with sample scripts. The
most likely shape is **JEOL's, with Zeiss's paperwork**: a real
interface, delivered on the control PC, under an option and possibly an
NDA. The parent page's estimate preamble already says vendor agreements
"for Zeiss and Hitachi are likely to dominate everything else", and
nothing found here contradicts it.

**One thing to settle explicitly**, because this project's licence makes
it load-bearing: an adapter that *calls* a vendor library while
distributing none of it is MIT-publishable, which is exactly why the
device layer is a subprocess with an out-of-tree server module
(`tests/unit/test_out_of_tree_server.py`). If Hitachi's terms forbid
publishing even the calling code, the adapter is a site-local package and
this project's contribution reduces to the protocol it already publishes.
That is a fine outcome; it is one to know in advance.

## 7. Task estimates

Same conventions as the parent page: **engineering days, not calendar
days**, assuming the framework as it stands plus the target-name
redesign, and someone with access to the instrument. Procurement, site
access and vendor agreements are **excluded and are likely to dominate**.

"Not estimable" is replaced with one scenario per thing the vendor can
say, plus the shared work that any of them needs.

### Shared prerequisites

| Task | Size | Needed by |
|---|---|---|
| The parent page's "common to any second vendor" block: target-name redesign, adapter template, scan-geometry conversion, metadata mapping, conformance suite | 4–7 d | A, B, C |
| **Simultaneous multi-channel scan call + per-channel segment geometry metadata** (§4) | 3–5 d | A, B, C — anything that produces a scanned image on this instrument |

The second row is new, and it is the one piece of framework work this
instrument makes non-optional. A segmented HAADF acquired one channel per
pass is not a segmented HAADF.

### Scenario A — a callable control library exists (level 2) — 12–18 days

The good case: an `MfExtCont`-equivalent for the SU9000II, importable
into a process we launch on the instrument PC.

| Task | Size |
|---|---|
| Obtain the interface, the licence and whatever documentation exists; establish which interpreter it loads in | 1–2 d |
| Write down the surface if the documentation is thin — method list, units, axis conventions, error behaviour, threading | 2–4 d |
| `Scanner` over the scan generator and the full channel set (BF, HAADF segments, SE, LA-BSE, HA-BSE) | 3–4 d |
| `InstrumentController`: stage, focus/working distance, accelerating voltage, blanker, `park()` | 2–3 d |
| Calibration: magnification-or-field-of-view conversion plus the per-instrument constant someone must measure | 1–2 d |
| `Camera` for anything reachable through Hitachi rather than its own SDK — likely the EELS spectrometer if it is Hitachi's | 1–2 d |
| Hardware validation against the checklist | 1–2 d |

**Wider than the Thermo Fisher estimate (6–10 d) for two specific
reasons**, not pessimism: there is no permissively-licensed community
wrapper to start from, and — unless question 5 comes back yes — **no
offline dummy**, so every iteration costs instrument time. The 2–4 day
documentation row is the honest one: with a reference manual it is nearer
2; reverse-engineering an undocumented surface attached to a live column
is nearer 4 and carries real risk of being wrong in ways that only appear
on hardware.

### Scenario B — EM Flow Creator only, with Python blocks (level 1) — 10–16 days, plus a design decision

The awkward case, and architecturally the Gatan case.

| Task | Size |
|---|---|
| Settle the topology: a bridge running **inside** the recipe engine that connects **out** to this client, mirroring the Gatan design question the parent page already raises | 2–3 d |
| Establish what a Python block may actually do — process lifetime, whether it can hold a socket, what it can read back | 1–2 d |
| Bridge server: same wire protocol, opposite direction | 3–4 d |
| Map the block vocabulary onto `Scanner` / `InstrumentController`, and document honestly what has no equivalent | 2–3 d |
| Ownership interlock: who has the instrument while a recipe runs, and what the operator sees | 1–2 d |
| Hardware validation | 1–2 d |

**Do the Gatan adapter first if this is the outcome.** The bridge
topology is unbuilt, and it is much cheaper to get it wrong against a
detector than against a live column. Two adapters would then share one
design instead of inventing it twice — the same argument the parent page
makes for doing the shared `CameraParameters` work before the first
camera.

Record the limitation on day one rather than discovering it in month
three: **a recipe engine cannot give you an interactive session.** Focal
series and parameter sweeps run as recipes; the live viewer watches
rather than drives.

### Scenario C — no vendor cooperation, external scan generator — 10–16 days plus hardware

The scenario the SEM makes possible and a TEM would not. Only worth
starting if the site is willing to plug something into the scan
connector.

| Task | Size |
|---|---|
| Procurement and fitting of a DISS6 or equivalent, and channel mapping — which physical detector is on which input | **hardware cost + site time, excluded** |
| `Scanner` over the scan generator's SDK, multi-channel from one pass | 4–6 d |
| Calibration: beam-position volts to nanometres is a **measured** per-instrument mapping, not a vendor constant, and it changes with working distance and accelerating voltage | 3–4 d |
| `InstrumentController` reporting **no controls** — `available_controls()` returns an empty list, which the interface already supports | 0.5 d |
| Metadata honesty: nothing here knows the accelerating voltage or magnification, so those keys are **absent rather than guessed** | 0.5 d |
| Hardware validation, including that the scan is not silently distorted | 2–3 d |

The calibration row is the risk. An external scan generator knows volts,
not nanometres, and the conversion depends on optics state it cannot
read. Everything downstream — field of view, pixel size, stored NeXus
axes — rests on a number someone measures against a calibration specimen
and re-measures whenever conditions change. That is a real operational
burden and should be stated to users, not buried.

### Scenario D — files only — 3–5 days, and it is not a device adapter

| Task | Size |
|---|---|
| Sidecar parser: the `[SemImageFile]` key/value format, re-implemented against `pynxtools-em`'s parser as the specification, so nothing pulls the `[em]` dependency stack | 1–2 d |
| Calibration and metadata mapping into `FrameCalibration` and the `Frame` vocabulary — most keys already have homes | 1 d |
| Watcher that ingests into a session as files land, in the **storage** layer rather than `devices/` | 1–2 d |
| Verify the coordinate convention and the vertical flip against a known specimen before trusting a stage coordinate | 0.5 d |

Cheap, useful, no vendor cooperation, no risk — and the only scenario
that provides **nothing this project is fundamentally for**, since the
closed loop is the point and this path has none. Worth building anyway;
worth being clear that building it is not "Hitachi support".

### Detector-only work, in parallel with any of the above — see the parent page

If the diffraction camera turns out to be a Merlin, a DECTRIS or a TVIPS,
that is 2–5 days against the parent page's existing estimates, needs no
Hitachi answer, and can start immediately. It gives frames and not maps —
a camera without a scan is not an image of anything — but it is real work
on this instrument that nothing blocks. The EELS spectrometer is the same
question with a worse expected answer, and the Oxford EDS belongs to the
spectrum-detector work.

### What each answer is worth

| Answer | Scenario | Days, after shared prerequisites | Does this project drive the microscope? |
|---|---|---|---|
| Callable control library exists | A | 12–18 | **Yes** |
| EM Flow Creator with Python blocks only | B | 10–16 | Partly — batch, not interactive |
| Nothing programmatic, but the scan connector is usable | C | 10–16 + hardware | Scan and detectors yes; optics and stage no |
| Nothing, and no scan connector | D | 3–5 | No — offline ingest only |

## Everything marked unverified

Listed once, with what would settle each. Nothing here should be repeated
as fact.

1. **Whether the `MfExtCont` Python control layer exists on the
   SU9000II.** The evidence is SU7000 — same vendor and same FE-SEM
   family, different model. This is the central unverified claim on the
   page. *Settled by:* question 0c — looking on the instrument PC —
   which costs five minutes, or vendor question 1.
2. **That `MfExtCont` is a Hitachi-supplied product** rather than a
   site-local wrapper. Inferred from the `sample03_SU7000mod` filename
   and the `Mf` prefix. *Settled by:* vendor question 1, or asking either
   repository's author.
3. **Who makes SuperSTEM 4's EELS spectrometer.** Published low-kV work
   on this platform used a Hitachi spectrometer; CEOS states CEFID is
   Hitachi-compatible. *Settled by:* question 0a. High value — it decides
   whether spectroscopy is reachable independently.
4. **Who makes the diffraction camera.** Not stated by the owner.
   *Settled by:* question 0a.
5. **Whether the external scan connector is present, available and
   supported** on this instrument, and whether point electronic have
   fitted an SU9000-series machine. The connector is a SEM-industry norm
   (generalising), not an SU9000II datasheet claim. *Settled by:*
   questions 0b and 11.
6. **Whether EM Flow Creator is licensed and installed here.** Hitachi
   names it as an option on the SU9000II. *Settled by:* question 0c.
7. **What a Python block inside EM Flow Creator may actually do** —
   process lifetime, sockets, imports, reading back from the engine.
   Scenario B's feasibility rests entirely on this. *Settled by:* vendor
   question 10, or ten minutes with the software.
8. **Whether the vendor interface can deliver several channels from one
   pass.** §4 establishes that *this project* cannot ask for it yet; it
   does not establish that the instrument can supply it. *Settled by:*
   vendor question 7.
9. **The segmented HAADF's segment count and geometry.** *Settled by:*
   vendor question 8, or the instrument's own documentation.
10. **The name of Hitachi's SU9000II control GUI.** Searches returned
    nothing authoritative. *Settled by:* looking at the instrument.
11. **That no published work has driven a Hitachi FE-SEM/STEM
    programmatically** beyond the two SU7000 repositories. A negative
    from search, not a systematic literature review, and arxiv.org was
    unreachable from here. *Settled by:* a proper literature search, or
    vendor question 13 — Hitachi knows its own reference customers.
12. **Whether the SU9000II writes TIFF-plus-sidecar** like other Hitachi
    SEMs. `pynxtools-em`'s parser handles `[SemImageFile]`, which is
    suggestive but not proof for this model, and EELS and EDS will arrive
    in their own vendors' formats regardless. *Settled by:* vendor
    question 12, or one sample file.
13. **Exact wording of every Hitachi-hosted, SuperSTEM-hosted,
    point-electronic-hosted and ScienceDirect-hosted source on this
    page.** All were egress-blocked from this environment; phrasing is as
    returned by a search index. *Settled by:* opening the links.

## What survived the re-target

For the record, since this page was researched twice. Discarded: HF5000
detector fit-outs, TEM-line remote-operation details, TEM-specific
estimates. Kept, and now on the correct product line: the `MfExtCont`
finding (SU7000 is an FE-SEM), the EM Flow Creator analysis (the primary
source is a **SEM** workflow article), the file-format finding (the
`[SemImageFile]` section is the SEM one), the negative-space search
across RosettaSciIO / instamatic / OpenFIBSEM / Micro-Manager / the
autonomous-microscopy literature, the level-1/2/3 taxonomy, and the
licensing-pattern comparison. Added by the correction: the external scan
connector and third-party scan generators, the segmented-detector
protocol finding, and the low-kV EELS spectrometer question.

## Sources

Verified — read directly:

- [`wilgardner/sem-scripts`](https://github.com/wilgardner/sem-scripts) —
  Python automation of a Hitachi SU7000 FE-SEM
- [`Ajay-Talbot/high-throughput-AM`](https://github.com/Ajay-Talbot/high-throughput-AM) —
  `sample03_SU7000mod`
- [`FAIRmat-NFDI/pynxtools-em`](https://github.com/FAIRmat-NFDI/pynxtools-em) —
  `image_tiff_hitachi.py` and its configuration
- [`hyperspy/rosettasciio`](https://github.com/hyperspy/rosettasciio) —
  reader directories and the TIFF reader's vendor branches
- [`DeMarcoLab/fibsem`](https://github.com/DeMarcoLab/fibsem),
  [`instamatic-dev/instamatic`](https://github.com/instamatic-dev/instamatic),
  [`micro-manager/mmCoreAndDevices`](https://github.com/micro-manager/mmCoreAndDevices/tree/main/DeviceAdapters),
  [`nanographs/Open-Beam-Interface`](https://github.com/nanographs/Open-Beam-Interface) —
  supported-vendor lists
- This repository: `src/miainwoodpecker/devices/interface.py`,
  `src/miainwoodpecker/devices/nion_server.py`,
  `tests/unit/test_out_of_tree_server.py`, `pyproject.toml`

Reported — URL is real, page unreachable from this environment:

- [Hitachi High-Tech: SU9000II](https://www.hitachi-hightech.com/eu/en/products/microscopes/sem-tem-stem/fe-sem/su9000.html)
  and the [SU9000II brochure](https://milexia.com/products/wp-content/uploads/sites/7/2022/08/Hitachi-SU9000%E2%85%A1.pdf)
- [SuperSTEM news](https://www.superstem.org/news) — SuperSTEM 4's
  configuration
- [Hitachi SI-NEWS: Automation of SEM Observation Workflow Using EM Flow Creator](https://www.hitachi-hightech.com/global/en/sinews/technical_explanation/130328/)
  ([Japanese PDF](https://www.hitachi-hightech.com/file/jp/pdf/sinews/technology/6220327.pdf))
- [Hitachi SI-NEWS: SU8600 and SU8700](https://www.hitachi-hightech.com/global/en/sinews/technical_explanation/130316/)
  and the [SU9600 press release](https://www.hitachi.com/en/press/articles/2025/10/1031a/)
- [Ultramicroscopy 2024: low-voltage STEM-EELS](https://www.sciencedirect.com/science/article/pii/S0304399124000561)
  — the Hitachi EEL spectrometer on an SU9000EA
- [CEOS CEFID](https://www.ceos-gmbh.de/en/produkte/cefid) —
  Hitachi compatibility
- [point electronic DISS6](https://www.pointelectronic.de/en/parts/smart-controllers/diss6-imaging-scanner-sem/)
  and [digital image scanning](https://www.pointelectronic.de/en/systems/digital-image-acquisition/)
- [Hackster: the Open Beam Interface](https://www.hackster.io/news/the-open-beam-interface-offers-digital-image-capture-from-almost-any-scanning-electron-microscope-17bb72c5d250)
  — the external scan connector's history
- [Microscopy 2024: simultaneous secondary electron microscopy in the STEM](https://academic.oup.com/jmicro/article/73/2/169/7604380)
- [HT78 series specification listing](https://www.medicalexpo.com/prod/hitachi-high-technologies/product-80782-1075154.html)
  — the "API (auto pre-irradiation)" entry
- [ResearchGate: "How can I automate image collection on the Hitachi FESEM?"](https://www.researchgate.net/post/How-can-I-automate-image-collection-on-the-Hitachi-FESEM)
- [AEcroscopy](https://pubmed.ncbi.nlm.nih.gov/38639016/) — the
  autonomous-experiment framework and the vendor scripting layers it names
