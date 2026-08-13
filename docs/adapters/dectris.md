# DECTRIS: what the ELA actually exposes, and the adapter built on it

The target instrument is a **DECTRIS ELA** on a Nion UltraSTEM 100MC
("HERMES") at SuperSTEM, Daresbury, where it is the detector on a Nion
IRIS spectrometer — an **EELS** detector, not a general 4D-STEM camera.

[The vendor survey](../vendor-support.md) called DECTRIS "the easiest
interface in this whole document" and listed ARINA, QUADRO and EIGER2. The
ELA was not on that list, and the ELA is the one that raises a real doubt,
because the way most people meet it is *inside Gatan Microscopy Suite*.
This page is the check on that claim before any code was written, and then
the record of what was built on the answer.

## 1. Is the ELA reachable directly, or only through GMS?

**Directly. It speaks SIMPLON, the same REST API as the rest of the
family.** The Gatan integration is a second front end onto the same
detector, not the only door.

The doubt is well founded — the GMS integration is real, prominent, and
sometimes the only thing a datasheet mentions. Gatan has "fully integrated
DECTRIS ELA … within the Gatan Microscopy Suite platform", where it is
sold under Gatan's own name as the **Stela** camera, and DECTRIS's own
marketing describes the ELA as "the only fully integrated hybrid-pixel
electron detector with Gatan Microscopy Suite software". A reasonable
reader concludes it is a GMS peripheral.

It is not, and the strongest evidence is a paper in which the ELA is
driven with no Gatan software anywhere in the stack. Plotkin-Swing et al.,
"Hybrid pixel direct detector for electron energy loss spectroscopy"
(*Ultramicroscopy* **217**, 113067, 2020) — the joint Nion/DECTRIS
characterisation of the ELA prototype, mounted on a Nion IRIS spectrometer
on a Nion UltraSTEM 200MC — records the topology plainly: the detector is
connected by two optical fibres to a **detector control unit (DCU)**,
which reaches the microscope PC over a dedicated 10 GbE link, and
"**the data are accessible through the DECTRIS Simplon API**". Images can
be stored on the DCU and retrieved later, or streamed live into Nion Swift.
That is a full acquisition path — configure, acquire, retrieve — over the
published API, on an instrument almost identical to the target one.

Three further points corroborate it:

- **SIMPLON's own documentation covers the ELA by name.** DECTRIS
  publishes the API reference for *EIGER2-chip-based detectors*, and the
  1.8 manual documents features specific to ARINA, ELA, QUADRO and SINGLA.
  The ELA is a member of that family, not an exception to it: it is built
  from eight EIGER2 readout chips of 256×256 75 µm pixels.
- **The architecture makes it unavoidable.** SIMPLON is served by the DCU
  itself over HTTP, needs nothing installed on the DCU, and DECTRIS's
  reference documentation states that access to the detector is not
  restricted. There is no licensing or software gate to pass.
- **DECTRIS advertises it as the integration point.** The product
  literature's pitch is that the ELA's API "enables straightforward
  integration into any modern data pipeline or electron microscopy suite"
  — GMS being one such suite rather than the privileged one.

**Conclusion: build the adapter.** The alternative path (a bridge running
*inside* DigitalMicrograph and connecting outward, as
[the vendor survey](../vendor-support.md) describes for Gatan cameras) is
not needed here and would be strictly worse: it would add a licensed
intermediary, constrain us to the subset GMS chooses to expose, and break
when either side updates.

## 2. Which SIMPLON subset, and what does a frame arrive as?

SIMPLON is a REST-like HTTP/JSON API on the DCU. Every resource is

```
http://<dcu>/<subsystem>/api/1.8.0/<task>/<parameter>
```

with subsystems `detector`, `monitor`, `stream`, `filewriter` and
`system`, and tasks `config`, `status` and `command`. `GET` reads a
parameter as `{"value": …, "value_type": …, "access_mode": …}`; `PUT` with
`{"value": …}` writes one and answers with the list of parameters that
changed as a result.

**Detector configuration** (`/detector/api/1.8.0/config/…`): `count_time`
and `frame_time` (exposure and frame period, seconds — `frame_time` must
not be below `count_time`), `nimages`, `ntrigger`, `trigger_mode`
(`ints`/`inte` internal, `exts`/`exte` external), `bit_depth_image`,
`x_pixels_in_detector`/`y_pixels_in_detector`, `x_pixel_size`/
`y_pixel_size`, `sensor_material`, `sensor_thickness`, `incident_energy`,
`threshold_energy`, the `*_correction_applied` flags, `roi_mode` and
`compression`.

**Arm/trigger/disarm** (`/detector/api/1.8.0/command/…`): `arm` (which
returns a sequence id), `trigger`, `disarm`, `abort`, `cancel`,
`initialize`, `status_update`, `hv_reset`. `/detector/api/1.8.0/status/state`
walks `na` → `idle` → `ready` → `acquire`. The configuration is
**read-only while armed**, which is the single most consequential detail
for an adapter: a `configure()` call during live acquisition has to
disarm, write and re-arm.

**Two ways to get images out, and they are not the same thing:**

| | `stream` subsystem | `monitor` subsystem |
|---|---|---|
| Transport | ZeroMQ PUSH (v1: port 9999; Stream V2: CBOR on port 31001) | HTTP GET |
| Content | every frame of the series | the most recent image only |
| Encoding | raw frames, `lz4` or `bslz4` (bitshuffle+LZ4) per `compression` | TIFF |
| Behaviour under load | back-pressure/loss is the client's problem | **drops frames by design** |
| Purpose | recording | preview |

A frame is therefore an integer array of **counts** — 8, 16 or 32 bits
per pixel depending on `bit_depth_image` and auto-summation — arriving
either as a compressed blob on ZeroMQ or as a TIFF over HTTP.

## 3. Frame rates, and which side of the line this falls on

The ELA is 1024×512 pixels of 75 µm (1030×514 including the inter-chip
gaps the DCU reports), and DECTRIS quotes **2250 full frames per second**
continuously at 16-bit, rising to **>4400 fps** at a 256-pixel-wide
readout and **>10 000 fps** at 100 pixels wide. Dead-time-free readout,
>100 pA of probe current without saturating.

[The vendor survey](../vendor-support.md)'s rule — "do not re-plumb
high-rate streaming" — puts a 120 kHz ARINA on LiberTEM-live's side of the
line and a 10–100 fps survey camera on `acquire_frame()`'s. **The ELA
falls on both sides, and which one depends on the mode, not on the
detector.**

- A **4D-STEM or spectrum-imaging acquisition** at 2250 fps is squarely
  LiberTEM-live's. Routing it through synchronous RPC would be reinventing
  something that exists, and the viewer's measured ~85 fps ceiling makes
  it pointless anyway.
- **Configuration, alignment, exposure setting, a live look at the
  spectrum, single shots and focal/energy series** are pull-per-frame work
  at survey rates, and nothing in LiberTEM-live does them: it is an
  *acquisition* library, not a detector control surface.

The EELS workload at SuperSTEM sharpens this rather than blurring it. An
EELS spectrum image at 5 meV resolution is a scan-synchronised stream —
LiberTEM-live's shape. But *getting there* is an hour of aligning the
spectrometer while watching the zero-loss peak, which is a live view of a
2D frame at tens of frames per second, and that is exactly what this
project's `Camera` is for.

**SIMPLON itself provides the split**, which is what makes it clean rather
than a compromise: the `monitor` subsystem is a documented, frame-dropping
preview channel served over HTTP, and it is *already* pull-per-frame. So
this adapter is built on `monitor` and does not touch `stream` at all.
The cost is honest and stated: one HTTP round trip per frame means tens of
frames per second, not thousands.

## 4. Does LiberTEM-live already support it, and what does that leave?

LiberTEM-live supports "DECTRIS EIGER2-based detectors like ARINA or
QUADRO" through `DectrisConnectionBuilder`, which takes an `api_host`/
`api_port` for the SIMPLON REST interface and a `data_host`/`data_port`
for the ZeroMQ stream, handles `bslz4`/`lz4`, and offers an *active* mode
(it arms and configures) and a *passive* mode (something else does). Its
receiver is Rust, in `libertem-dectris` in the LiberTEM-rs repository, and
it bundles DECTRIS's own `DEigerClient.py` for the REST side.

The ELA is **not named** in its supported list. That is very likely a
documentation and testing gap rather than a technical one — the ELA is
EIGER2-chip-based and speaks the same API — but it is not a claim this
project should make on LiberTEM-live's behalf, and it is the first thing
to check on a hardware day.

*(**Verified** — `libertem-live` 0.3.0's sdist was downloaded from PyPI
and read, so none of the paragraph above rests on documentation any
more. `DectrisConnectionBuilder`'s `api_host`/`api_port`/`data_host`/
`data_port`, the active and passive modes, `bslz4`/`lz4`, the bundled
`DEigerClient.py`, and the Rust receiver via `libertem-dectris>=0.2.14`
are all present as described. The trigger arithmetic in §6 item 2 is
confirmed at `libertem_live/detectors/dectris/controller.py:63-71`:
`ints` sets `ntrigger=1, nimages=prod(nav_shape)`, and `exte`/`exts`
take the mirror image of that. And the string **"ELA" appears nowhere in
the package** — not in the supported list, not in a test, not in a
comment — so this page's refusal to claim ELA support on LiberTEM-live's
behalf was the right call and is now a checked fact rather than a
caution.)*

What that means for scope:

- **The streaming path is not worth building.** If it works, it is
  LiberTEM-live's; if it needs a small fix, the fix belongs upstream where
  every other DECTRIS user gets it.
- **The control path is worth building, and nobody else has it.**
  LiberTEM-live's `DectrisActiveController` configures a detector *for an
  acquisition it is about to run*. It is not a device server, has no
  `Camera` protocol, does not integrate with this project's sessions,
  storage or viewer, and is not what an operator drives while aligning.
- **The ownership interlock is the real design work**, and it is settled
  here rather than deferred: SIMPLON admits configuration from anyone but
  only **one armed series at a time**. This adapter therefore refuses to
  open a detector that is not `idle`, and names the likely culprits
  (GMS/Stela, Nion Swift, a LiberTEM-live session). Symmetrically, its
  `stop()`/`close()` disarm rather than leaving the detector held, so
  handing it to LiberTEM-live is just "stop the live view first".

## 5. What was built

`src/miainwoodpecker/devices/dectris_server.py` — MIT, in-tree, importing
nothing from `nion.*` and nothing from any vendor.

**Control is standard library.** SIMPLON needs no client library at all:
`urllib.request` and `json` are the whole dependency for configuration,
arming and triggering.

**Two backends, and the simulated one is a protocol mock.** `simulated`
starts a small `http.server` on localhost that serves the documented
SIMPLON resource tree — config/status/command for `detector`, `monitor`
and `stream`; the `na`/`idle`/`ready` state machine; 403 for read-only
parameters *and* for configuring an armed detector; 404 for a wrong API
version; 408 for an empty monitor buffer; monitor images as real TIFF
bytes — and then points the *same* `SimplonClient` at it. A stub camera
object would have left URL construction, JSON shapes, HTTP statuses, the
armed-state rules and TIFF decoding entirely untested; this way every
request the hardware backend makes is a request the test suite makes,
parsed by something that can refuse it. `hardware` points that identical
client at a real DCU.

**The `Camera` mapping**, which is the design decision:

| `Camera` | SIMPLON |
|---|---|
| `start()` | `trigger_mode=ints`, `nimages=1`, `ntrigger=65536`, enable `monitor`, `arm` |
| `acquire_frame()` | `trigger`, then `GET /monitor/api/1.8.0/images/next`, decode TIFF |
| `stop()` | `disarm` |
| `configure()` | disarm if armed, write `frame_time`/`count_time` in the order that keeps `frame_time ≥ count_time` at every instant, re-arm, read back |
| `binning_values` | `[1]` |

**Frame metadata**, populated against the `Frame` vocabulary and no
further:

- `photometrically_linear: True` — the opposite of `camera_server`'s, and
  a real claim. A hybrid-pixel detector discriminates each electron
  against a threshold and increments an integer; there is no charge
  integration, gain curve, gamma or demosaic between the physics and the
  pixel. The one genuine departure is count-rate paralysis at very high
  flux, which is why `countrate_correction_applied` is recorded beside it.
- `counts_per_electron: 1.0` — what counting means, and why the flag above
  is allowed to be `True`.
- `camera_type: "hybrid_pixel_counting"`, `camera_name` from the DCU's own
  `description`, plus `detector_number`, `software_version`,
  `sensor_material`, `sensor_thickness_m`, `x_pixel_size_m`,
  `y_pixel_size_m`, `bit_depth_image`, `incident_energy_ev`,
  `threshold_energy_ev`, the correction flags, `roi_mode` and `series_id`.
- **`high_tension_v` is deliberately absent.** The DCU publishes
  `incident_energy`, and on a 100 kV column it will read 100 keV — but it
  is the energy the detector was *configured for* (it sets the
  discriminator threshold), not a reading from the column. The
  vocabulary's rule is to omit rather than substitute, so it is recorded
  under its own name.
- **No `calibration` is published**, and its absence is the answer rather
  than an omission. The detector knows exactly one length: its own 75 µm
  pitch. Turning that into a sample-plane, reciprocal or energy axis needs
  the camera length or the spectrometer dispersion, both of which live in
  the microscope. The pitch is recorded so a caller who knows the optics
  can finish the job with `FrameCalibration`'s explicit constructors.

**Failure diagnostics**, one sentence per cause, following the house
convention set by `camera_server`'s `_open_failure_hint()`:

| Symptom | What the message says |
|---|---|
| Connection refused | Nothing is listening; SIMPLON is served by the *control unit* on port 80, not by the detector head |
| Timed out | A wrong address on a reachable subnet looks like this; a DCU has a control network *and* a 10 GbE data link and only one serves SIMPLON |
| Name does not resolve | A DCU is usually reached by IP on a private network |
| HTTP 404 on the identity read | Not a SIMPLON control unit, or different firmware — the API version is in the URL |
| HTTP 403 | The configuration is read-only while a series is armed |
| State `na` | Powered but not initialised; `initialize` takes minutes and resets the detector, so the adapter will not do it behind your back |
| State `error` | Read `/detector/api/1.8.0/status/error` on the DCU; HV and cooling faults clear there |
| State `ready`/`acquire` | Another client has it armed — GMS/Stela, Nion Swift, or LiberTEM-live |

**Served on the neutral `camera` target**, with no scanner, exercising the
detector-only instrument path. Not `eels_camera`, even though on this
instrument it *is* one: the target name would then assert a microscope
configuration the adapter cannot see, while the metadata told the truth.

## 6. What is verified, and what is not

**Verified against the mock and the real transport:** the whole client
path end to end — the device server's command line, the authkey handshake,
`describe()`, the neutral camera target with no scanner, `Camera`
conformance, frame identity and gapless indices, recording to NeXus and
reading it back, the shutdown handshake. On the SIMPLON side: URL
construction, the config/status/command JSON shapes, the armed-state
refusal that forces `configure()` to disarm, the `frame_time`/`count_time`
ordering in both directions, transparent re-arming when a series is spent,
the 408 empty-buffer case, TIFF encode/decode, and each open-failure
diagnostic. The viewer was driven against the server headlessly with
`--backend simulated --server-module miainwoodpecker.devices.dectris_server`.

**Not verified — no detector was available.** Everything below is
implemented from published documentation and open-source clients that
speak the protocol, and needs a real ELA:

1. That the ELA serves SIMPLON **1.8.0** at that exact path, rather than a
   different version. The version is in the URL, so a mismatch is a clean
   404 with a message saying so — but it is a guess until checked.
2. **The trigger-mode arithmetic.** This adapter uses `ints` with
   `nimages=1` and `ntrigger=65536`, i.e. one image per software trigger.
   LiberTEM-live's controller uses the other arrangement for `ints`
   (`nimages` = the whole series, `ntrigger=1`) — **now read from the
   source rather than the docs**, at
   `libertem_live/detectors/dectris/controller.py:63-71` — which is the
   same series with the numbers the other way round. If the ELA's
   firmware treats one `trigger` in `ints` as starting the *whole*
   series, this must change to `inte`, or to re-arming per frame. Note
   that the disagreement is now known to be a real disagreement between
   two implementations, not a possible misreading of one.
3. **Whether `monitor` is enabled and usable on an ELA at all**, and what
   its buffer semantics are under `mode=enabled` — including whether
   `images/next` really returns 408 rather than blocking when empty.
4. **The monitor TIFF variant**: compression, byte order, bit depth. The
   `dectris` extra exists for exactly this; the built-in decoder handles
   uncompressed little-endian strips and refuses anything else by name.
5. **That an ELA publishes** `description`, `detector_number`,
   `incident_energy`, `threshold_energy` and `roi_mode`. Absent keys are
   tolerated and omitted from the metadata, but which ones are absent is
   unknown.
6. **The read-only-while-armed rule**, which the mock enforces and the
   adapter's `configure()` depends on.
7. **Achievable frame rate through this path.** One round trip per frame
   predicts tens of fps; that number should be measured, not assumed.
8. **Whether LiberTEM-live works against an ELA**, which decides whether
   the streaming half of the story needs an upstream fix.

Proposed entries for
[the hardware validation checklist](../hardware-validation-checklist.md)
are in the pull request description; they follow this list one for one.

## Sources

- Plotkin-Swing, B. et al., "Hybrid pixel direct detector for electron
  energy loss spectroscopy", *Ultramicroscopy* **217**, 113067 (2020).
  <https://doi.org/10.1016/j.ultramic.2020.113067> — the ELA prototype on
  a Nion IRIS/UltraSTEM 200MC; DCU topology and "the data are accessible
  through the DECTRIS Simplon API".
- DECTRIS, *SIMPLON API 1.8 documentation* (EIGER2-chip-based detectors),
  <https://media.dectris.com/210607-DECTRIS-SIMPLON-API-Manual_EIGER2-chip-based_detectros.pdf>
  and the v3.x reprints under `media.dectris.com/filer_public/…/simplon_apireference_v1p8.pdf`
  — resource tree, `arm`/`trigger`/`disarm`, monitor and stream
  subsystems, `lz4`/`bslz4`, ZeroMQ ports 9999 and 31001.
- DECTRIS, ELA product and technical documentation pages,
  <https://www.dectris.com/en/detectors/electron-detectors/for-materials-science/ela/>
  — 1024×512 at 75 µm, eight 256×256 readout chips, 2250 fps full frame at
  16-bit, >4400 fps at 256 wide, >10 000 fps at 100 wide.
- DECTRIS/Gatan announcements on the GMS integration and the Stela camera,
  <https://www.dectris.com/company/news/newsroom/news/gatan-and-dectris-a-joint-venture-into-the-future-of-4d-stem>
  — the integration is real; it is a second front end, not the only one.
- LiberTEM-live, DECTRIS detector documentation and
  `src/libertem_live/detectors/dectris/` (`connection.py`, `controller.py`,
  and DECTRIS's own bundled `DEigerClient.py`),
  <https://github.com/LiberTEM/LiberTEM-live> — supported models, active
  and passive modes, `api_host`/`data_host`, `bslz4`/`lz4`, and the
  `ints` trigger-mode arithmetic this adapter differs from.
- SuperSTEM instrumentation pages,
  <https://www.superstem.org/facility/instrumentation> — the ELA on the
  Nion UltraSTEM 100MC "HERMES" with the IRIS spectrometer.
