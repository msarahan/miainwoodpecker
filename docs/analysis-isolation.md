# Analysis isolation: what HyperSpy buys, and whether it needs a boundary

Two questions, asked by the project owner and taken in order.

**What does HyperSpy actually serve here?** Rather little of what it
*could*. Every HyperSpy call in this repository is a container
constructor, an axis assignment, or one `.mean()`; the value is not the
computation but the **object handed to whoever called `load_as_*`**.

**Should the analysis side have an IPC methodology like the device
side?** For the viewer's three buttons, yes — on crash containment,
thread control and dependency separation, all three of which are
measurable and none of which is about licensing. As an answer to the
*licence* question, no: it cannot be one, and the reason is the same
finding as the first answer.

This page is the audit, the measurements, and the thing that was built.
It follows [vendor support](vendor-support.md)'s rule about legal
matters: state the technical facts precisely, lay out the options, and
leave the decision where it belongs.

## Where this sits

[Analysis parity](analysis-parity.md) closed by observing that
`viewer/live.py` imports GPL-3.0 libraries in the application's own
process, "the same shape §6 went to considerable trouble to avoid for the
device layer", and said explicitly that it was not the document to
resolve it. [Migration plan §6](migration-plan.md) is where the device
layer's boundary was drawn and measured. This page picks up the thread
those two left, and adds nothing to either — proposed edits to §6 are
listed at the bottom rather than made here.

## The licence facts, from installed metadata

Read from the distributions actually installed in this environment, not
from project home pages:

| Package | Version | Declared licence |
|---|---|---|
| `hyperspy` | 2.4.0 | GPL-3.0 (`COPYING.txt` is the plain GPLv3 text — 674 lines, no added linking exception) |
| `exspy` | 0.3.2 | GPL-3.0 |
| `rosettasciio` | 0.14.0 | GPL-3.0-or-later |
| `py4dstem` | 0.14.16 | GPL-3.0 |
| `libertem` | 0.16.0 | **MIT** |
| `napari` | — | BSD-3-Clause |
| `numpy`, `scipy`, `dask`, `scikit-image`, `h5py` | — | BSD-3-Clause |
| `threadpoolctl` | 3.6.0 | BSD-3-Clause |

`niondata` and `nionutils` are **Apache-2.0**, verified three ways in
[analysis parity](analysis-parity.md) and not re-checked here.

## Part 1 — what HyperSpy actually buys

### Every call site, counted

The starting hypothesis was that the in-tree usage is thin. It is
thinner than that. Grepped across `src/`, the complete HyperSpy surface
this project touches is:

| Call | Where | What it does |
|---|---|---|
| `import hyperspy.api as hs` | `hyperspy_bridge.py` module scope | the import itself |
| `hs.signals.Signal2D(data)` | `hyperspy_signal_from_frames` | wraps an `(n, h, w)` array |
| `hs.signals.Signal1D(data.sum(axis=...))` | `hyperspy_spectrum_from_frames` | wraps a flattened stack |
| `hs.signals.Signal1D(recording.data)` | `_spectrum_recording_as_signal` | wraps spectra |
| `signal.axes_manager.navigation_axes` / `.signal_axes` | `_apply`, three callers | selects an axis object |
| `axis.name / .units / .scale / .offset = ...` | `_apply` | four attribute writes |
| `signal.set_signal_type(...)` | `load_as_eds_signal` | eXSpy registration |
| `signal.metadata.set_item(...)` | `_apply_eds_metadata` | writes a metadata tree |
| `import exspy` | `load_as_eds_signal` | for its signal-type registration |
| `signal.mean(axis=nav_axes[0]).data` | `operations.mean_projection` | **the only computation** |

Ten call sites. Nine are the object model. One computes, and it computes
`data.mean(axis=0)`.

py4DSTEM's surface is four names — `Calibration`, `DiffractionSlice`,
`Q_pixel_size`/`Q_pixel_units`, and `get_probe_size` — of which exactly
one, `get_probe_size`, computes anything. LiberTEM's is `Context`,
`InlineJobExecutor`, `ctx.load`, `SumUDF` and `ctx.run_udf`.

**RosettaSciIO has zero call sites.** It is here only because HyperSpy
depends on it. Nothing in this repository reads a `dm3`, a `bcf` or an
`msa` today; the project reads its own NeXus layout with `h5py` and
Nion's `.ndata` with `zipfile`.

### So what is the dependency for?

Not for the ten calls. It is for what the caller does *next*. The
documented API in
[scripting and automation](scripting-and-automation.md) is:

```python
signal = load_as_hyperspy_signal("series.nxs")   # a live Signal2D
```

and the point of that line is that the operator then has a HyperSpy
signal — to fit a model to, to `remove_background` on, to `align2D`, to
plot, to save as `.hspy`. **The library is the destination, not the
implementation.** The adapter's own docstring says as much: "Nothing here
reimplements anything HyperSpy itself provides."

That single observation decides most of this page, because a destination
cannot be moved to another process. A `Signal2D` pickled back across a
boundary needs HyperSpy in the receiving interpreter to unpickle it, and
a caller who wanted a plain array would not have called `load_as_*`.

### What dropping each package would cost

The "permissive alternative" column is what was found by checking, not by
reputation.

| Capability | Package today | Licence | Permissive alternative | Cost of dropping it |
|---|---|---|---|---|
| Calibrated-axes signal container (`Signal1D`/`Signal2D` + `AxesManager`) | `hyperspy` | GPL-3.0 | **Yes** — `niondata`'s `DataAndMetadata` (Apache-2.0, four packages total); or plain NumPy plus this project's own `FrameCalibration`, which already models per-axis scale/offset/units with a closed unit vocabulary | Nothing in-tree. The bridge transfers a calibration it already has onto an object it does not otherwise use |
| Mean/sum projection, arithmetic, spatial filters, FFT | `hyperspy` | GPL-3.0 | **Yes** — NumPy/SciPy (BSD-3), `niondata`'s `xd.*` (Apache-2.0) | Nothing. The one in-tree use is `data.mean(axis=0)` wearing a signal |
| Handoff into the wider HyperSpy ecosystem — model fitting, `remove_background`, `align2D`/`estimate_shift2D`, `find_peaks`, interactive plotting, `.hspy`/`.emd` writing | `hyperspy` | GPL-3.0 | **No** | This is the whole value. Large, real, and not substitutable — and it is the *user's* session, not this application's |
| EELS/EDS science: background models, ZLP alignment and calibration, log-ratio thickness, `EELSCLEdge` quantification against GOSH DFT and Dirac databases, elemental mapping | `exspy` | GPL-3.0 | **No.** `niondata` has no EELS science; Nion's own `eels-analysis` is GPL-3.0 *and* weaker (K-shell hydrogenic cross sections only — [analysis parity](analysis-parity.md)) | Total loss. There is no permissive EELS quantification stack, and saying otherwise would be inventing one |
| Vendor readers: Gatan `dm3`/`dm4`, FEI `ser`, Velox `emd`, `mrc`, `smv`, DECTRIS | `rosettasciio` (transitive; **unused**) | GPL-3.0-or-later | **Yes** — `ncempy.io`. openNCEM is dual-licensed and states the position exactly: "``ncempy`` is dual licensed under GPLv3 and MIT. **The io module is the only part released under MIT to improve interoperability with other packages.**" Its `io` package is `dm`, `ser`, `emd`, `emdVelox`, `mrc`, `smv`, `dectris` | Nothing today, because nothing calls it. When vendor import is built, the MIT reader covers Gatan and FEI |
| Bruker `bcf`/`spx`, EDAX, JEOL, and 30 other formats | `rosettasciio` | GPL-3.0-or-later | **No** for `bcf` — no permissive reader was found. `msa` is EMSA/MAS, a documented ASCII format, and cheap to write | Bruker composite files would need the GPL reader or new code. `msa` is a day's work |
| Single-pattern diffraction: central-disk fit (`get_probe_size`) | `py4dstem` | GPL-3.0 | **Partly.** LiberTEM (MIT) has `masks.circular`/`ring`, `CoMUDF`, `ApplyFFTMask` — **no** central-disk fit. `niondata`'s `xd.radial_profile` (Apache-2.0) covers radial integration | Losing the probe-size fit specifically. Radial profiles are covered permissively; the disk fit is not |
| 4D-STEM: virtual detectors, dark correction, centre of mass, DPC→iDPC reconstruction, ptychography | `py4dstem` | GPL-3.0 | **Partly** — LiberTEM (MIT) covers virtual detectors, corrections, and CoM (with descan correction, divergence and curl, which py4DSTEM does not do). Not iDPC reconstruction, not ptychography | Most of the routine 4D work is available permissively; the reconstructions are not |
| Thread-bounded UDF execution over frame stacks | `libertem` | **MIT** | n/a | n/a |
| NeXus/HDF5 reading and writing | this project's `storage/nexus.py` over `h5py` | BSD-3 | n/a | n/a |

Read down the "permissive alternative" column and a clear ordering falls
out, and it is the same one [analysis parity](analysis-parity.md) arrived
at from the other direction: **`niondata` (Apache-2.0) and `ncempy.io`
(MIT) cover the container, the core operations and the vendor readers;
LiberTEM (MIT) covers most 4D work; and eXSpy's EELS/EDS science has no
permissive substitute at all.** If the project ever wanted to be
GPL-free, EELS is where it would stop — and EELS is often why a Nion is
in the room.

## Part 2 — the licence question

**Not legal advice, and no legal conclusion is drawn here.** What follows
is what can be established from source, metadata and published positions,
plus the options that follow. The decision needs the project owner,
probably with advice this document cannot supply.

### First, the distinction that is *not* there

The obvious argument is that the analysis extras are optional and the
device layer is not, so the two cases differ. **Check that against
`pyproject.toml` and it does not hold:** `device` is an optional extra
too. `nionswift`, `niondata` and the rest are installed by the user with
`pip install miainwoodpecker[device]`, exactly as `hyperspy` is installed
with `[analysis]`. Neither is in the wheel. Optionality alone separates
nothing.

The *mechanism* is also identical. §6's technical premise is that "a
Python `import` of a GPL-3.0 library into the same process is generally
treated as linking under the FSF's own interpretation", and that premise
does not care which library is being imported. If it is right for
`nion.*` it is right for `hyperspy`, on any machine where the `analysis`
extra is installed.

Anyone arguing that analysis is different has to argue it on something
other than "it's optional" or "it's only an import". Those two do not
survive contact with the repository.

### What the FSF and the packages say

- On plug-ins loaded by a program: *"If the program dynamically links
  plug-ins, and they make function calls to each other and share data
  structures, we believe they form a single program, which must be
  treated as an extension of both the main program and the plug-ins"*
  ([GNU licences FAQ](https://www.gnu.org/licenses/gpl-faq.en.html)).
  That describes `import hyperspy.api` accurately.
- On what stays separate: *"Pipes, sockets and command-line arguments are
  communication mechanisms normally used between two separate programs,
  and when they are used for communication, the modules normally are
  separate programs. However, if the semantics of the communication are
  intimate enough, exchanging complex internal data structures, that too
  could be a basis to consider the two parts as combined into a larger
  program"* (same FAQ, "mere aggregation"). This is the sentence the
  device layer's subprocess boundary rests on, and it is the sentence any
  analysis boundary has to be checked against too — a worker exchanging
  live `Signal2D` objects would be a poor fit for it; one exchanging a
  method name, a file path and a NumPy array is a better one.
- On use without distribution: *"The GPL does not require you to release
  your modified version... you are free to make modifications and use
  them privately, without ever releasing them"* (same FAQ). The GPL's
  obligations attach on conveying, not on running.
- **The packages themselves add nothing.** `hyperspy` 2.4.0's
  `COPYING.txt` is the unmodified GPLv3 text with no linking exception;
  `exspy`, `py4dstem` and `rosettasciio` likewise declare plain
  GPL-3.0(-or-later). Nobody upstream has published a carve-out for being
  imported by permissively-licensed code. `ncempy` is the one project in
  this space that *has* taken an explicit position, and it took the
  opposite one: it dual-licensed its `io` module MIT specifically "to
  improve interoperability with other packages".

### The distinctions that are real

Four, and they are about consequence rather than mechanism.

**1. MIT is GPL-compatible, so nothing here is forbidden.** Worth stating
plainly because it is often lost. There is no conflict to resolve: an MIT
program combined with GPL-3.0 code produces a combined work distributable
under GPL-3.0, with the MIT parts still MIT. The question is never "may
this be done" — it is "what licence governs the combination, and who is
doing the combining".

**2. This project distributes no GPL code, and does not distribute the
combination.** The wheel contains this project's MIT source and a
dependency declaration. The combination comes into existence on the end
user's machine, when they choose to install an extra, and — per the FAQ
above — the GPL does not reach a combination that is merely used. That is
equally true of the `device` extra, which is why it cannot by itself be
the distinction; what it does mean is that for *both* layers the copyleft
question is about downstream redistributors, not about this repository's
own uploads.

**3. Necessity, not optionality, is where the two layers actually
differ.** The device layer is what this project is *for*. Its default
backend — `remote_simulated_instrument()`, the one every demo, test and
first run uses — is served by `nion_server.py`. Without the `device`
extra the application controls no microscope and simulates none. So a
working installation is, in practice, always the combined case, and an
MIT licence that is only true of an installation nobody runs is a nominal
one. The analysis extras are not like that: the viewer records, displays,
calibrates, sessions and writes NeXus with none of them installed, and
the three buttons say "install the 'analysis' extra" and carry on. A
facility can run this project for a full shift and never form the
combination at all.

**4. Isolating analysis cannot answer the licence question anyway, and
this is the finding that matters most.** Suppose the worker built in Part
3 were switched on by default. `viewer/live.py` would then hold no
analysis import — and `load_as_hyperspy_signal` would still be a
documented public function that imports HyperSpy into whatever process
calls it. That function exists to hand back a live library object; that
is its entire purpose; and a process boundary either destroys the purpose
or is not crossed. **A boundary that covers the buttons and not the API
is a boundary with a documented hole in it.** Anyone reaching for
isolation as the licence answer has to be told that up front, because the
alternative is a subprocess layer that buys confidence it has not earned.

### Conclusion, and its confidence

**The analysis case is materially different from the device case, and the
difference is one of degree and consequence rather than of mechanism.**
Same import, same FSF reading, very different stakes: the device layer
was the difference between an MIT project and an MIT-in-name-only one,
while the analysis extras are three interchangeable back ends of which
the most useful for 4D work is already MIT and the most likely container
replacement is already Apache-2.0.

**Confidence: high on the technical facts.** The call-site audit, the
installed licence metadata, the absence of any linking exception, the
`ncempy.io` MIT position, and the fact that `device` is itself an
optional extra were all checked directly and are reproducible from this
repository. **Confidence: none offered on the legal question**, which is
not one this document is competent to answer and which no amount of
measurement converts into an engineering question.

**What this page recommends**: do not isolate the analysis extras *for
licence reasons*, because isolation does not deliver what that reason
wants. Do isolate the viewer's analysis path for the three engineering
reasons below, which stand on their own and were measured. And if the
owner wants the licence exposure genuinely reduced rather than relocated,
the lever is the dependency ordering [analysis parity](analysis-parity.md)
already identified — Apache-2.0 `niondata` first, MIT LiberTEM second,
MIT `ncempy.io` for vendor formats, GPL-3.0 only where nothing else
exists, which is EELS/EDS and only EELS/EDS.

## Part 3 — what was built, and why

Isolation was built, and **not** for the licence. Three independent
arguments carry it, all of them checkable.

**Crash containment.** py4DSTEM, LiberTEM and HyperSpy sit on numba,
compiled SciPy kernels and native HDF5. A segfault in any of them
currently takes the napari window, the live feed and — worst — a
recording in progress, which is a corrupted scientific record rather than
an interrupted one. In a worker it costs one result.
`test_a_worker_killed_between_calls_is_replaced_transparently` kills the
worker mid-session and asserts the next click works.

**Thread control that actually works.** `analysis/threads.py` is unusually
honest about its own ceiling: `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`
and `MKL_NUM_THREADS` "are read **once, when the native library loads**",
so the in-process cap has to be a `threadpoolctl` runtime call whose
scope is *time*, not a thread — process-global while an analysis runs. A
worker's environment is set before its interpreter starts, so the cap
becomes a property of the process, it covers numba (which
`threadpoolctl` cannot reach), and nothing in the GUI process is
throttled at all.
`test_the_worker_runs_under_the_thread_budget_it_was_given` reads the
worker's `/proc/<pid>/environ` and asserts the budget is there.

**Dependency separation.** `pyproject.toml`'s own measurements: HyperSpy
~35 packages, py4DSTEM 65, LiberTEM ~102, all pinning overlapping
scientific stacks that can conflict with each other and with napari's.
One worker per target does not by itself fix that — a worker in the same
virtualenv shares the same resolution — but it is the precondition for
fixing it, because a worker is launched by `sys.executable` and a future
`--python` pointing at a separate environment is a one-line change to
`WorkerRunner._spawn`. The device layer already relies on exactly this
being possible; it is why `rpc.COMPATIBLE_PICKLE_PROTOCOL` exists.

### Is an analysis worker shaped like a device server?

Same in the ways that let it reuse the device layer wholesale, different
in four ways that all follow from what an analysis is.

**The same:**

- Strictly synchronous request/response. The viewer already refuses a
  second analysis while one is running, which is the same invariant that
  makes `shared_frame.py`'s single reused segment safe without
  double-buffering.
- One connection per target, enforced by the same code —
  `serving.accept_loop` is handed a writer purely so its
  one-client-at-a-time check applies.
- Long-lived. Measured cold imports in this environment: `hyperspy.api`
  248 ms, `libertem.api` 2 602 ms, `py4DSTEM` 5 205 ms, `exspy` 3 439 ms.
  A process per job would put five seconds in front of every py4DSTEM
  click, so the worker is spawned lazily on first use and kept.

**Different:**

1. **Bulk travels both ways.** A device request is a method name and two
   numbers; only the reply is large. An analysis request can carry a
   whole frame stack — five 2048² float32 frames is 84 MB — and returns a
   full frame. So each side owns a `SharedFrameWriter` *and* a
   `SharedFrameReader`, where the device layer has a writer on the server
   and a reader on the client.
2. **A target is a library, not a device.** One worker per extra, because
   that is the granularity of "installed or not", of import cost, and of
   dependency conflict. `ANALYSIS_TARGETS` is therefore its own table and
   not an addition to `rpc.TARGET_NAMES`, whose *order* is part of the
   device protocol's argv.
3. **A path is a payload, and the cheaper one.** For a fresh burst the
   viewer has just written a NeXus file, so the worker is sent a string
   and opens it — nothing crosses. The array transport is only for the
   already-opened-recording case, which exists precisely so a 2048²
   recording is not read twice.
4. **Dying is survivable, so the client restarts.** §6 rejected reconnect
   for the device layer with a good reason: a fresh server is a fresh
   instrument construction, and a recording in progress would keep
   appending frames from differently-configured hardware. None of that is
   true here — the input is still on disk or still in the client's memory
   and the only loss is one result — so `WorkerRunner` starts a new
   process on the next call. The inversion is deliberate; its
   precondition is stated in `analysis/worker.py` so it does not read as
   an inconsistency.

### What the pieces are

| Module | Licence position | What it is |
|---|---|---|
| `analysis/operations.py` | MIT; imports analysis libraries **inside functions** | The three analyses the buttons run, lifted out of `viewer/live.py`'s closures so both transports call one implementation |
| `analysis/transfer.py` | MIT; imports neither side | The wire vocabulary: `ANALYSIS_TARGETS`, `AnalysisSource`, and the publish/read pair. The analysis-side analogue of `rpc.py`, and much smaller because it reuses it |
| `analysis/worker.py` | the module that imports HyperSpy/py4DSTEM/LiberTEM | `python -m miainwoodpecker.analysis.worker <target> <port>`. Binds one listener, dispatches through `serving.accept_loop` |
| `analysis/remote.py` | MIT; imports no analysis library | `InProcessRunner`, `WorkerRunner`, and `open_runner`, which picks between them |
| `devices/shared_frame.py` | unchanged for the device layer | Gained `SharedArrayRef` / `publish_array` / `read_array` — three lines each, because `_store`/`_copy_out` were already the array-shaped core — and an opt-in `stop_tracking` flag (see below) |

Reused unchanged: `rpc.Call`, `rpc.Result`, `rpc.send_call`,
`rpc.disable_nagle`, `rpc.SHARED_MEMORY_THRESHOLD_BYTES`,
`serving.invoke`, `serving.serve_connection`, `serving.accept_loop`, and
the whole reused-segment transport.

### The bug reuse found

The device layer's reader is the long-lived application and its writer is
the subprocess. The analysis layer's roles are the other way round for
requests, and that inversion has teeth: `multiprocessing`'s
`resource_tracker` registers every segment a process *attaches to* by
name, not only ones it creates, and unlinks them when that process exits.
So the worker's tracker cheerfully unlinked the client's live segment on
the way out.

Found by running the path, not by reading it — the first end-to-end run
failed with the client's own `SharedFrameWriter.close()` raising
`FileNotFoundError` for a segment it had created and never released.

The fix is `resource_tracker.unregister`, which `shared_frame.py`'s
header records as having been *tried and made things worse*. Both records
are correct and they are not about the same call: that attempt
unregistered a name from a daemon which had never registered it (writer
and reader being independent `Popen` processes with a tracker each), and
landed a `KeyError` in the wrong daemon's main loop. This unregisters, in
the attaching process's own daemon, a name that same daemon registered a
line earlier. It is opt-in (`SharedFrameReader(stop_tracking=True)`) so
the device layer's measured behaviour does not move, and
`test_no_shared_memory_segments_survive_a_worker_session` is what keeps
it fixed.

### What is preserved exactly

- `viewer/jobs.py`'s `AnalysisJob` contract, untouched: the worker thread
  runs `compute` and never touches Qt, the GUI thread runs `display`. The
  handlers still resolve every widget value before starting the job.
- The three buttons' behaviour: same layers, same names, same colormaps,
  same ellipse, same status strings.
- The lazy imports and the per-extra messages. `open_runner` refuses with
  `ImportError` so each handler keeps its own `except ImportError:` and
  its own literal `"install the 'analysis' extra"`. In-process it does
  *the same imports the handler used to do itself* — including
  `libertem.udf.sum`, because `libertem_bridge` defers its own library
  imports and would import fine with no LiberTEM installed. Isolated it
  uses `importlib.util.find_spec`, which resolves a top-level name
  without executing it: importing py4DSTEM in the GUI process to find out
  whether the *worker* can import it would defeat the whole exercise.
- The public `load_as_*` / `*_from_frames` API that
  [scripting and automation](scripting-and-automation.md) documents. Not
  one signature changed, and — per Part 2's fourth point — those
  functions still import HyperSpy into the caller's process, by design.

### It is on by default — the owner's decision, for crash containment

`MIAINWOODPECKER_ANALYSIS_ISOLATION=inprocess` opts out. Unset means
isolated.

It *shipped* off by default, as a deliberate refusal rather than an
unfinished edge: turning it on unasked would have answered Part 2's
question on the owner's behalf, in the direction of "yes, isolate" —
and Part 2's conclusion is that isolation is not the answer to that
question. The owner then made both calls explicitly: **isolation on, for
crash containment** (a segfault in a native analysis dependency used to
take the session, the UI, and any in-flight recording; now it costs one
result and a worker restart), and **the licensing posture left where it
is** — not a major concern at present, and expressly not the reason the
switch is on. The opt-out is the whole word `inprocess` rather than
"anything that is not `process`", so a typo cannot silently disable the
protection it looks like it is configuring.

Both paths stay covered: `tests/integration/test_analysis_worker.py`
asserts the isolated results equal the in-process ones,
`test_the_buttons_work_with_the_analysis_libraries_in_a_worker_process`
drives all three real buttons through the shipped default, and the rest
of the widget suite pins `inprocess` so its assertions stay about the
widget rather than the transport.

## Measurements

`scripts/analysis_ipc_benchmark.py`, same structure and reporting as the
device side's `scripts/ipc_overhead_benchmark.py`. Five-frame bursts of
float32 noise, seven timed calls per configuration after a warm-up, on a
4-core container (so the thread budget is 2), Python 3.11.

**The two arms are interleaved call by call, and that is not a
refinement.** Running one arm to completion and then the other produced a
*reproducible* 400–560 ms "isolation overhead" for py4DSTEM at 2048²,
across two runs — which vanished when the isolated arm was run on its
own. It was the container getting slower over a long run, not the
boundary. Alternating removes it, and the fact that a plausible,
repeatable number was wrong is the reason this paragraph exists.

### Worker startup, paid once per target per session

| Target | Spawn → first result |
|---|---|
| `hyperspy` | 0.25–0.86 s |
| `libertem` | 1.05–2.30 s |
| `py4dstem` | 2.55–4.34 s |

Dominated by the library import (cold: `hyperspy.api` 248 ms,
`libertem.api` 2.6 s, `py4DSTEM` 5.2 s), which is why the worker is
long-lived. A session that never clicks an analysis button never pays it.

### Per call: the file case, which is what a fresh burst takes

Nothing but a path crosses. Medians, isolated minus in-process:

| Stack | `hyperspy` | `libertem` | `py4dstem` |
|---|---|---|---|
| 1.3 MB (256²×5) | +2.8 ms (1.24×) | +1.8 ms (1.14×) | +2.3 ms (1.16×) |
| 5.2 MB (512²×5) | +1.6 ms (1.04×) | +0.6 ms (1.02×) | +4.0 ms (1.07×) |
| 21.0 MB (1024²×5) | +3.2 ms (1.03×) | −23.3 ms (0.84×) | −0.9 ms (1.00×) |
| 83.9 MB (2048²×5) | −9.6 ms (0.98×) | +244.8 ms (1.33×) | +188.5 ms (1.19×) |

Read the first three rows as "a few milliseconds, i.e. nothing", and the
last row as noise: at 2048² the p95s reach 1.2–2.5 s against medians of
0.5–1.2 s, because a five-frame 84 MB gzip-compressed HDF5 read on a
4-core container is not a quiet measurement. The honest statement is
**the file path costs single-digit milliseconds up to 21 MB and is not
resolvable above it on this machine.**

### Per call: the in-memory case, which is what the transport really costs

The already-opened-recording path, where the whole stack crosses in and a
projection crosses back:

| Stack | in → out | `hyperspy` | `libertem` | `py4dstem` |
|---|---|---|---|---|
| 1.3 MB | 1.3 + 0.03 MB | +1.4 ms (1.34×) | +1.1 ms (1.17×) | +3.6 ms (1.58×) |
| 5.2 MB | 5.2 + 1.0 MB | +3.0 ms (1.58×) | +3.7 ms (1.45×) | +4.9 ms (1.18×) |
| 21.0 MB | 21.0 + 4.2 MB | +8.9 ms (1.96×) | +14.1 ms (1.72×) | +19.1 ms (1.18×) |
| 83.9 MB | 83.9 + 16.8 MB | +75.5 ms (4.02×) | +81.0 ms (2.29×) | +367.1 ms (1.83×) |

**About 0.75–0.9 ms per megabyte moved**, which is the same figure §6
measured for the device layer's reused-segment transport (+25 ms at
33.6 MB, or 0.74 ms/MB) — as it should be, since it is the same code
doing the same memcpy. The 84 MB row's HyperSpy and LiberTEM numbers
(+75 and +81 ms for 101 MB round-tripped) land exactly on that line.

py4DSTEM's +367 ms at 84 MB does not, and it is not explained here. It
reproduces; it is not the thread budget (a worker given a budget of 4
instead of 2 measured 995 ms against 945 ms — no effect); and it is not
the result transport (HyperSpy returns the same 16.8 MB array for
+75 ms). Recorded as an open item rather than papered over. It is bounded
in practice: **the py4DSTEM button acquires one frame, not five**, so its
real payload is 16.8 MB and not 84 MB.

### Ratios look worse than they are, and the reason matters

The 4.02× on the HyperSpy in-memory row is 25 ms becoming 100 ms. Every
one of these operations is a button an operator clicks and then looks at
a result; the numbers that decide whether that feels broken are absolute,
and the largest absolute cost measured for a realistic burst is under a
tenth of a second.

**Which paths this is unacceptable for**: none of the three buttons, at
any size measured. It *would* be unacceptable for anything on the frame
path — a per-frame analysis in a live loop at 30 fps has a 33 ms budget
and 84 MB would eat it whole — and nothing of that shape exists today. If
it ever does, it should stream frames to a *resident* worker rather than
round-trip a stack per frame, and this transport is the wrong shape for
it.

## If the owner wants the default flipped

Two changes, both small, listed so the decision is costed rather than
open-ended.

1. `analysis/remote.py`'s `isolation_enabled()` inverts its default, so
   the variable becomes an opt-*out*.
2. Two tests in `tests/integration/test_live_widget.py` —
   `test_analysis_reuses_frames_already_read` and the sibling that
   asserts the file *is* read when frames are not in hand — monkeypatch
   `hyperspy_bridge.read_frames` in the *test's own* process to count
   reads. With the work in a subprocess that patch no longer observes
   anything, so both would need to pin the switch off explicitly. They
   are testing a property of the operations, not of the transport, so
   pinning it is honest rather than a weakening.

Nothing else changes: the button handlers, the job contract, the public
API and the status messages are transport-independent already.

## What is unverified

- **No legal opinion was sought, and none is offered.** Every licence
  statement here is metadata, licence text, or a published FSF FAQ
  answer. Whether any particular arrangement complies with any particular
  licence is a question for someone qualified to answer it.
- **The dependency-separation argument is a precondition, not a delivered
  feature.** Nothing yet launches a worker from a different virtualenv;
  `WorkerRunner._spawn` uses `sys.executable`. The claim made is only
  that a boundary makes it a one-line change, and that claim is not
  tested.
- **The 2048² measurements are noisy on this hardware.** p95s reach two
  to five times their medians for the file path at that size. The smaller
  sizes are stable, and the per-megabyte figure is drawn from them plus
  the two clean 84 MB rows.
- **py4DSTEM's in-memory overhead at 84 MB is unexplained**, as recorded
  above. Two hypotheses were tested and eliminated (thread budget,
  result-array transport); a third — that `DiffractionSlice` copies its
  input differently under the worker's allocator — was not.
- **`ncempy.io`'s MIT status is read from its own README** and its
  dual-licence declaration (`License: GPLv3+, MIT` in the distribution
  metadata, with the README naming `io` as the MIT part). The individual
  module files carry no per-file headers, so the README is the whole of
  the evidence. Anyone depending on it should confirm with the project.
- ~~**No 4D-STEM dataset was analysed.**~~ **One has been now.** See
  [Real 4D-STEM data](#real-4d-stem-data-and-what-it-did-and-did-not-change)
  below: the transport conclusion survives unchanged, and a separate
  problem turned up that only real data could expose.

## Real 4D-STEM data, and what it did and did not change

The measurements above were taken on synthetic noise. A real dataset is
now reachable and was used, so the caveat can be replaced with a result.

**The data.** Zenodo record [8233585](https://zenodo.org/records/8233585),
"Mixed Phase Test Datasets for py4dstem", CC-BY-4.0, file
`20210306_084059.hdf5`: a **(254, 255, 384, 384) `uint8`** datacube — a
genuine 2D scan of 2D diffraction patterns, 152 MB on disk and 9.55 GB
raw. Sparse, as real fast-scan 4D-STEM is: a five-pattern burst has
**1.9 % of pixels non-zero** and a mean of **0.03 counts**.

### The transport conclusion survives

Real frames and synthetic frames of *identical shape and dtype*, 142 ×
384 × 384 `float32` = 83.8 MB — the same 84 MB payload as the rows above
— interleaved over fifteen rounds so machine drift cancels, on 4 cores:

| Frames | in-process median | worker median | delta |
|---|---|---|---|
| **Real** | 15.2 ms | 51.4 ms | **+36.3 ms** |
| **Synthetic** | 16.5 ms | 60.7 ms | **+44.2 ms** |

Indistinguishable, and expected to be: the shared-memory path does no
compression, so it cannot care what the bytes mean. Real data was never
going to move this number, and it did not. The p95s — 678 ms real,
665 ms synthetic against ~50 ms medians — corroborate the noise caveat
above rather than contradicting anything.

An earlier non-interleaved run produced a 2337 ms real median against
46 ms synthetic. That was machine noise, and it is recorded because it
is the shape of mistake this page could have made: one uninterleaved
pass would have "found" a 50× real-data penalty that does not exist.

### What real data did break

`fit_central_disk` — py4DSTEM's `get_probe_size`, the viewer's "Fit
central disk" button. On synthetic input it returns a confident,
centred, **entirely fictitious** disk:

| Input (384 × 384) | fitted radius | fitted centre |
|---|---|---|
| uniform noise `U(0,1)` | 157.31 px | (191.7, 191.8) |
| all ones | 216.65 px | (191.5, 191.5) |
| Gaussian noise | 46.52 px | (190.5, 193.7) |
| Poisson, λ = 0.03 | 20.94 px | (190.1, 193.3) |
| all zeros | 0.00 px | (nan, nan) — with a divide warning |

Threshold-and-centroid on a structureless field always lands on the
array centre. **So every synthetic-data exercise of that button was
vacuous**: it could not have failed, whatever the code did. Nothing on
this page claimed otherwise — no scientific claim was made from the
noise frames — but "no claim made" and "the check could not fail" are
different admissions, and the second is the true one.

On the real cube the operation behaves, given enough electrons:

| Real input | fitted radius | fitted centre |
|---|---|---|
| 255 patterns summed (one scan row) | 3.99 px | (190.8, 192.5) |
| 256 patterns sampled across the scan | 5.19 px | (191.7, 191.5) |
| single pattern, ×5 consecutive | 1.80–3.05 px | drifts to (181.2, 203.5) |
| 16 × 16 block from mid-scan, summed | **0.64 px** | **(128.0, 266.0)** |

Two findings there, neither visible in noise:

1. **One sparse pattern is not enough.** The button fits the *first*
   frame by deliberate design, and the adapter's docstring gives a good
   reason ("averaging several first would fit something that was never
   acquired"). On this data that design returns a radius varying by 70 %
   between consecutive patterns and a centre 15 px off. The reasoning is
   still sound; the input is simply too sparse for it, and an operator
   pointing the button at a fast scan will get a number that looks fine
   and is not.
2. **Strong diffraction defeats it outright.** The mid-scan block sums
   256 patterns — more than the row that worked — and fits a 0.64 px
   "disk" at (128, 266), which is exactly that block's brightest pixel.
   This is a mixed-phase specimen; on a strongly diffracting grain a
   Bragg disk outshines the direct beam and `get_probe_size` locks onto
   the wrong one. More data does not help when the data is structured
   the wrong way.

Neither is a defect in this repository — `fit_central_disk` calls
py4DSTEM correctly and returns what py4DSTEM returns. Both are reasons
the button's output should not be trusted without an operator looking at
the fitted disk drawn on the pattern, which is, to its credit, exactly
what the adapter already returns the pattern for. Recorded here so that
[the hardware validation checklist](hardware-validation-checklist.md)
can carry it: **check the central-disk fit against a summed pattern from
a real specimen before believing a single-frame fit.**

### What is still unverified about this

- One dataset, one specimen, one detector geometry. Nothing here is a
  survey.
- The file path was not re-measured on the real cube; this is
  frames-already-in-memory only. The cube is HyperSpy-format HDF5, not
  the NeXus this project writes, so a like-for-like file-path row would
  have needed a conversion whose cost is not the thing being measured.
- `drive.google.com` and `web.archive.org` are still egress-blocked, so
  py4DSTEM's own downloader still cannot run here. Zenodo can.
