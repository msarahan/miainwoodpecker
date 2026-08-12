# Gatan: the adapter that cannot be a subprocess

Every other device adapter in this project is a subprocess this client
launches. A Gatan one cannot be, and that turned out to be worth building
for even though — read this part first — **the facility that prompted it
probably does not need it.**

This page is the record: what was established and from where, what was
built, what is tested, and what is still a design that no hardware has
seen.

## The headline, before anything else

**A Gatan spectrometer on a Nion column is very likely already supported,
with no Gatan code at all.**

SuperSTEM 2 is a Nion UltraSTEM 100 with a Gatan UHV Enfina EEL
spectrometer ([SuperSTEM][superstem]). Nion's instrumentation kit models
an **optional `eels_camera`** as one of the two primary cameras on the
STEM controller, reached through `Registry.get_component("stem_controller")`
([nionswift-instrumentation][nion-cameras]) — and that is exactly the
mechanism `devices/nion_server.py` already reads. This project already:

- serves whatever Nion registers as the EELS camera on the `eels_camera`
  target, with its calibration resolved from the device's own
  `calibration_controls`; and
- exposes the **spectrometer energy offset** as an
  `InstrumentController` control, mapped to Nion's `ZLPoffset`.

A Nion control named `ZLPoffset` *is* the spectrometer's drift-tube
offset. It would be a strange thing for Nion's stack to publish on a
machine whose spectrometer it does not drive. So the working hypothesis
is that on SuperSTEM 2 the Enfina is Nion's EELS camera, this project
supports it today over the ordinary spawn path, and none of the code
described below is involved.

That hypothesis is cheap to settle and has **not** been settled here — no
instrument was available. The check is one command on the instrument
control computer, and it belongs in
[the hardware validation checklist](../hardware-validation-checklist.md):

```console
$ python -m miainwoodpecker.devices.nion_server --backend hardware --help
# then, from the application:
>>> with remote_instrument(backend="hardware") as scope:
...     print(scope.instrument.describe())
```

If `describe()` reports `eels_camera` among its targets and
`energy_offset` among its controls, the Gatan question is answered and
this page is about somebody else's microscope.

Two related cautions:

- The Enfina is **old** and this one is a **UHV** variant. Current Gatan
  documentation describes the GIF Quantum/Continuum generation. Nothing
  below should be generalised to an Enfina without saying so, and where
  it is generalised, it says so.
- SuperSTEM 4's Hitachi SU9000II has an EELS spectrometer and a
  diffraction camera of **unidentified vendor**. Nothing here assumes
  they are Gatan.

## Phase 1: what was established, and from where

### The launch inversion is real, and the vendor says so

Gatan's Python FAQ states plainly that *the DigitalMicrograph-Python API
is dependent on the DigitalMicrograph application; therefore it is not
possible to execute DigitalMicrograph functions from Python outside of
the DigitalMicrograph application* ([Gatan Python FAQ][gatan-faq]). Python
integration in GMS 3 runs *inside* DM, with "all native Gatan data objects
and existing hardware controls directly accessible in Python code"
([Gatan][gatan-python-integration]).

So a Gatan adapter cannot be `python -m something` launched by us. That
part of `vendor-support.md`'s claim stands.

### But the **socket** direction is a free choice — the doc conflates two things

`vendor-support.md` says a Gatan adapter "is a bridge running inside DM
that connects *out*. Same wire protocol, opposite direction." The first
half is right; the second is an assumption, and the evidence says it is
optional:

- **A published out-of-process route already exists.** Lei, Weber,
  Clausen and Wilbrink, *DigitalMicrograph and Stand-Alone Python
  Integration*, Microscopy and Microanalysis **30**(Suppl 1), July 2024
  ([doi:10.1093/mam/ozae044.208][mam-2024]) describe a DM-SDK plug-in
  inside DigitalMicrograph communicating with stand-alone Python over
  ZeroMQ and JSON, so that Python can run in a Jupyter notebook while
  driving DM acquisition. Note the authors: two LiberTEM core developers
  and a Gatan engineer.
- **Sockets inside DM go both ways.** `gms-socket-plugin` exposes
  `TCPSocketConnect` *and* `TCPSocketBind` to DM-Script
  ([LaurentRDC/gms-socket-plugin][gms-socket]) — connect out, or listen.
- **Listening inside DM is the established arrangement in this field.**
  SerialEM's `SerialEMCCD` plug-in has listened inside DigitalMicrograph
  for two decades, with SerialEM connecting to it.
- **DM can even launch us.** InsteaDMatic uses DM-Script's
  `LaunchExternalProcess` with netcat to reach the microscope PC
  ([InsteaDMatic][insteadmatic]).

**What actually inverts is process *ownership*, not connection
*direction*.** We cannot launch the server, cannot read its exit status,
and must not kill it. Which end opens the socket is a firewall question.

That correction changes the design: this project now supports **both**
directions over one code path, and recommends the one the doc did not
consider — the bridge listens, we dial in — because GMS is started once in
the morning and outlives many runs of the client.

### What can be driven from GMS's embedded Python

- **Camera acquisition: yes.** Gatan publish example scripts doing Python
  processing of a camera's live view ([Gatan][gatan-python-integration]).
- **Anything DM-Script can do: yes, via `DM.ExecuteScriptString`.** This
  is the escape hatch that matters, and it is well attested — it is what
  `execdmscript` is built on, together with `DM.GetPersistentTagGroup`,
  `DM.NewTagGroup`, `DM.NewTagList` and `DM.NewCancelSignal`
  ([miile7/execdmscript][execdmscript]). So the bridge is never limited to
  whatever the Python API happens to wrap: the camera manager (`CM_*`) and
  imaging-filter families are reachable, with values returned through the
  persistent tag tree.
- **EELS spectrometer control: reachable in principle, spelling
  unverified.** Gatan's script library documents an example that sets the
  GIF drift-tube voltage and lists the imaging-filter control commands,
  and it requires `Gatan IF Interface Plug-in.dll`. The exact command
  names could not be retrieved from this sandbox — `gatan.com`,
  `dmscripting.com`, the archived DM help, and the FELMI/TU Graz script
  database are all unreachable through the egress proxy. **This is the
  single largest unverified item on the page**, and it is why the bridge
  takes those snippets as parameters rather than hard-coding a guess.

### What the embedded interpreter constrains

Three constraints, all of which changed the code:

1. **`sys.argv` may not exist.** LiberTEM documents setting it before
   anything that needs it, specifically for GMS ([LiberTEM tips][libertem-tips]).
   So the bridge's entry point is a *function with keyword arguments*, and
   `main()` reads `sys.argv` defensively.
2. **Long work belongs off the calling thread**, and loading SciPy on a
   GMS background thread does not work ([LiberTEM tips][libertem-tips]).
   Hence `start_bridge()`, which returns a handle instead of blocking.
3. **The interpreter is old, and one consequence is a silent data
   corruption risk.** Gatan's FAQ names Python 3.7.2 and NumPy 1.18.2 for
   the GMS 3.4-era `GMS_VENV_PYTHON` environment (a Miniconda env under
   `C:\ProgramData\Miniconda3\envs\`, with user-created venvs
   unsupported) ([Gatan Python FAQ][gatan-faq]). Against this project's
   `requires-python >= 3.11`, that produces a specific failure:

   > `multiprocessing.connection.Connection.send` serialises with
   > `_ForkingPickler.dumps(obj)` — no protocol argument, so the
   > **sender's** `pickle.DEFAULT_PROTOCOL`, which is 5 on Python 3.8 and
   > later ([cpython][cpython-connection]). Python 3.7's
   > `pickle.HIGHEST_PROTOCOL` is **4** ([cpython 3.7][cpython-pickle37]).
   > Every `Call` this client sent would fail to unpickle in the bridge,
   > while every `Result` coming back (written at protocol 3 or 4) would
   > decode perfectly — which presents as a broken server rather than a
   > version mismatch.

   The client therefore caps outgoing calls on attached links at
   `COMPATIBLE_PICKLE_PROTOCOL = 4`. It costs nothing: protocol 5's
   addition is out-of-band buffers, which a plain `pickle.dumps` without a
   `buffer_callback` never emits.

   The **authkey handshake needs no such care**, and that is worth
   recording because the opposite would have been a design constraint:
   CPython's `_create_response` keeps the legacy HMAC-MD5 path for a
   challenge with no `{digest}` prefix, so the SHA-256 default introduced
   in 3.12 stays compatible with older peers in both directions
   ([gh-61460][gh-61460], [cpython][cpython-connection]).

   Installation is the remaining consequence: `pip install miainwoodpecker`
   into a 3.7 environment will be refused, so on such a GMS the three
   modules the bridge needs (`interface`, `rpc`, `serving`) have to be
   copied in by hand. `gatan_bridge.py` is written to import on 3.8 for
   that reason.

### Licence position

Nothing of Gatan's is redistributed. `DigitalMicrograph` is imported from
the copy GMS installed, on the microscope's own computer, by MIT code.

One deliberate avoidance: `gms-socket-plugin` is **GPL-3.0**
([LaurentRDC/gms-socket-plugin][gms-socket]), so it is not used — pulling
it in would put the project back in the position the subprocess boundary
exists to avoid. It is also unnecessary: Python's own `socket` module
inside the embedded interpreter needs no plug-in. `execdmscript` is
MPL-2.0 and likewise not needed; `DM.ExecuteScriptString` is one call.

## Phase 2: what the framework gained

`attached_instrument()`, beside the unchanged `remote_instrument()`.

```python
from miainwoodpecker.devices.remote import AttachInvitation, attached_instrument

with attached_instrument(publish_to="attach.json", announce=print) as scope:
    frame = scope.eels_camera.acquire_frame()
    scope.instrument.set_energy_offset_ev(50.0)
```

Same `Call`/`Result` protocol, same authkey handshake, same `Camera` and
`InstrumentController` protocols, same `RemoteInstrumentDevices`. It is a
transport direction, not a second device API — a caller changes which
context manager it opens and nothing else.

**Both directions, one code path.**

| | who binds | when to use |
|---|---|---|
| `ACCEPT_TRANSPORT` | this client | the microscope PC permits outbound connections only |
| `CONNECT_TRANSPORT` | the bridge | **preferred where the network allows it** — GMS outlives many client runs, so a listening bridge is simply there |

**The rendezvous is published, because it cannot be passed in argv.**
`AttachInvitation` carries host, one port per target name, the authkey and
the direction; it writes itself to a `0600` JSON file and prints itself as
instructions for the person who has to start the other end. The authkey
appears in the file only, never in the printed instructions.

**Liveness is weaker, and says so.** There is no `Popen`, so:

- a fourth health state, `SERVER_DISCONNECTED`, distinct from
  `SERVER_EXITED`. "Exited" is a claim about a process backed by a
  return code; an attached bridge lives inside a host application that is
  very probably still running, possibly on another machine, so a closed
  socket is all the evidence there is and all the state asserts;
- connection-lost messages name the bridge's origin and point at *its*
  log, instead of reporting an exit status this client never read;
- teardown asks for a graceful shutdown and falls back to closing each
  device — but never terminates anything. The forcible half of the spawn
  path's teardown would mean killing DigitalMicrograph, quite possibly
  mid-acquisition for somebody else.

**Shared memory is off.** An attached server may be on another machine, so
frames travel as ordinary pickles. Measured cost, from the existing
benchmark: within noise below ~500 KB, +1.5 ms at ~1 MB, +9.5 ms at
8.4 MB. A 2k × 2k float32 frame is ~16 MB, so this is real and bounded.

**The spawn path is unchanged.** Its internals were refactored behind a
`_ServerLifecycle` abstraction so that "what became of the server" has one
answer per path instead of a `Popen | None` that degraded silently, but
every existing message, state and test is the same.

## Phase 3: the bridge

`miainwoodpecker/devices/gatan_bridge.py`, MIT, two backends.

**`simulated` needs no Gatan software** and is what CI runs. It is a mock
of the bridge's *behaviour*: same connection choreography, same threading,
same targets, same metadata vocabulary, same shutdown semantics — only the
device objects differ. Its spectrometer camera synthesises a zero-loss
peak that **moves when the energy offset is driven**, which is what makes
the two-target wiring testable rather than merely plausible.

**`hardware` runs inside GMS.** Two decisions in it, both deliberate:

- **It reads DM's front image** rather than driving its own acquisition.
  That is InsteaDMatic's published pattern — synchronise with the live
  view, and the script works with whichever Gatan camera is fitted
  ([InsteaDMatic][insteadmatic]) — and it means DM keeps the detector and
  the operator keeps DM's controls. The cost is stated rather than
  hidden: exposure and binning are DM's, so `binning_values` is `[1]` and
  `configure()` refuses a binning it cannot set instead of accepting one
  and writing a wrong dispersion into every frame.
- **Instrument control goes through `DM.ExecuteScriptString`, and the
  command names are constructor parameters** with placeholder defaults,
  for the reason given in Phase 1. A snippet that fails surfaces as a
  named `RemoteCallError`, not as a wrong number.

**What the bridge honestly cannot do.** A Gatan spectrometer is not the
microscope: no stage, no defocus, no beam blanker — those belong to
whichever column vendor's software is next door. So `available_controls()`
returns one entry, the other methods are simply absent, and `park()` does
nothing. On the Nion path `park()` blanks the beam and is a safety
property; this bridge cannot make a column safe and does not pretend to.
Whatever parks the column must be the column's own adapter.

## Running it

On the client:

```python
with attached_instrument(publish_to=r"\\share\attach.json", announce=print) as scope:
    ...
```

Copy `attach.json` to the microscope PC, then in DM's Python window:

```python
from miainwoodpecker.devices.remote import AttachInvitation
from miainwoodpecker.devices.gatan_bridge import start_bridge

start_bridge(
    AttachInvitation.read_from(r"C:\miainwoodpecker\attach.json"),
    backend="hardware",
    dispersion_ev=0.5,   # eV per unbinned channel; nothing can read this for you
    sessions=0,          # serve until GMS is closed
)
```

`sessions=0` is the arrangement to prefer: the bridge outlives many runs
of the client. `start_bridge` returns immediately, so DM stays usable, and
the handle carries any failure the bridge thread hit.

## Tested versus designed-only

**Tested, in CI, with no Gatan software present**
(`tests/unit/test_attached_server.py`, `tests/unit/test_gatan_bridge.py`):

- the full inbound session end to end — `describe()`, both device
  protocols, frames, instrument control, shutdown;
- the outbound-to-a-listening-bridge direction, same everything else;
- instrument control observably reaching the camera's frames;
- bridge never dials in → bounded, diagnosed timeout;
- bridge dials with a wrong authkey → named as an authentication failure,
  not as "nobody came", on both sides;
- bridge killed mid-session → `SERVER_DISCONNECTED`, a connection-lost
  message that does not claim a process exit, `exit_status is None`;
- invitation file round trip, version refusal, and the refusal to invent
  a listening bridge's ports;
- binning multiplying the energy dispersion; frame-identity contracts;
- the hardware backend refusing to start outside GMS.

**Designed, never executed against Gatan software:**

- every line of `DigitalMicrographCamera` and
  `DigitalMicrographSpectrometer`. `DM.GetFrontImage`, `GetNumArray`,
  `ExecuteScriptString` and `GetPersistentTagGroup` are all attested in
  public code and documentation, but this combination has not been run;
- the imaging-filter command names — the placeholders are almost certainly
  wrong for an Enfina and possibly wrong for anything;
- cross-interpreter operation. The pickle cap and the authkey
  compatibility are both reasoned from CPython source, not observed
  between a 3.7 and a 3.12 peer;
- everything about a UHV Enfina specifically.

## Checklist entries this needs

Proposed for [the hardware validation checklist](../hardware-validation-checklist.md):

1. **Settle the Nion question first.** On SuperSTEM 2's control computer,
   run the existing Nion server with `--backend hardware` and record what
   `describe()` reports. If `eels_camera` and `energy_offset` are there,
   close the Gatan case for this facility and record the Enfina as
   supported through Nion.
2. If it is *not*: identify what does read the Enfina — GMS, a Gatan
   controller with its own interface, or something site-specific.
3. Confirm GMS's embedded Python version (`import sys; sys.version` in
   DM's Python window) and whether `pip install miainwoodpecker` is
   possible in `GMS_VENV_PYTHON`.
4. Run the bridge's `simulated` backend *inside GMS* against a client on
   the same network. This tests the transport, the pickle cap and the
   authkey across the real interpreter pair, with no hardware at risk.
5. Find the real imaging-filter DM-Script commands for this spectrometer
   and record them; replace the placeholder snippets.
6. Confirm `DM.GetFrontImage()` returns the live spectrum image while a
   live view runs, and that `GetNumArray()` gives the shape and dtype
   expected.
7. Measure the pickle-only frame cost at this detector's real frame size.

[superstem]: https://www.superstem.org/facility/superstem-laboratory
[nion-cameras]: https://github.com/nion-software/nionswift-instrumentation-kit/blob/master/docs/cameras.rst
[gatan-faq]: https://www.gatan.com/python-faq
[gatan-python-integration]: https://www.gatan.com/resources/media-library/gms-3-analysis-tools-python-integration
[mam-2024]: https://doi.org/10.1093/mam/ozae044.208
[gms-socket]: https://github.com/LaurentRDC/gms-socket-plugin
[insteadmatic]: https://github.com/instamatic-dev/InsteaDMatic
[execdmscript]: https://github.com/miile7/execdmscript
[libertem-tips]: https://libertem.github.io/LiberTEM/tips.html
[cpython-connection]: https://github.com/python/cpython/blob/main/Lib/multiprocessing/connection.py
[cpython-pickle37]: https://github.com/python/cpython/blob/3.7/Lib/pickle.py
[gh-61460]: https://github.com/python/cpython/pull/20380
