# Architecture review — coherence and optimization (2026-08-11)

A full-stack read of `src/`, `tests/`, packaging, and CI against the
architecture stated in [`migration-plan.md`](migration-plan.md). Every
finding below was verified against the code (file:line cited); the one
claim that needed vendor-side evidence was checked against Nion's own
published source rather than assumed.

## Verdict

**The architecture is coherent.** The load-bearing decisions all hold
mechanically, not just on paper:

- The license boundary is real: `nion.*` is imported in `nion_server.py`
  and nowhere else in `src/`; `rpc.py` imports nothing from either peer;
  the shipped viewer reaches devices only through `devices/remote.py`.
- The layering is clean: viewer and acquisition depend only on the
  protocols in `devices/interface.py`; storage imports `Frame` only under
  `TYPE_CHECKING`; acquisition depends on storage, never the reverse.
- The threading contract (no Qt in workers, one QTimer, latest-frame-wins,
  settings via an immutable tuple rebind) is followed everywhere it is
  stated.
- Units and axis order — `(y, x)`, nanometres, operator units — are
  consistent across the device, acquisition, and calibration layers, with
  one exception that is the top finding below.

The problems found are not architectural drift; they are seams where two
correct-looking halves meet with mismatched assumptions, plus a handful of
lifecycle bugs and hot-path waste. They are listed in priority order:
first what can corrupt the scientific record, then coherence debt, then
performance, then robustness for hardware day.

## Status

Everything in §1 (data corruption and loss), the §4 items that matter on
hardware day, and the cheap half of §3 have since been **fixed**; each
finding below is marked. What deliberately remains is listed in
[§7](#7-what-was-not-done-and-why) with the reasoning, so an unfixed item
is a decision on record rather than an oversight.

Two of the fixes are worth calling out because writing them changed what
they claimed. The `fov_nm` convention was settled by reading Nion's own
`get_scan_calibrations` in the pinned release rather than by picking the
reading that looked tidier — it computes `fov_nm / max(scan_shape)` and
applies it to both axes, so **three** of this project's own tests were
asserting the bug (the third surfaced only in CI, in the HyperSpy
adapter's round trip). And the first three display-optimization tests
**passed with the optimization removed**: the fake scanner returns
zero-filled frames, so the autocontrast they observed through never
fired. They were rewritten against a gradient frame, and each is now
verified to fail with its own clause disabled.

**Three of the fixes were themselves wrong on the first attempt, and CI
is what said so** — worth recording, because each failure was specific
to an environment this container cannot reproduce:

- *The `disable_nagle` fix broke Windows outright.* Saving the fd's mode
  with `os.get_blocking` reintroduced the same class of bug it was fixing:
  that function is Unix-only, and the `AttributeError` fell outside the
  function's own `except` clause, so **every** connection setup on
  Windows would have raised. It now calls `raw.setblocking(True)` before
  detaching — cross-platform, and correct rather than merely portable,
  since a `multiprocessing.connection` endpoint is always blocking.
- *The per-frame metadata dataset broke NXem validation.* Putting it in
  `NXdetector`, next to the frames it describes, is the obvious place and
  the wrong one: a detector's contents are specified by the standard, so
  the file stopped validating. It belongs in the `NXcollection` that
  already exists for exactly this — data no base class describes.
- *The orphan-watchdog commit hung every device-server startup — and
  reached `main` before anything executed it.* A text-anchored patch
  meant to add the connection-accounting methods to `_ServerSession`
  anchored on `def health(...)`, which exists on **two** classes, and
  injected them into `NionInstrument` instead. Every static check passed
  — methods on the wrong class are perfectly well-formed — and no test
  runnable without the Nion stack executes `serve()`, so 242 local tests,
  ruff, and pydoclint stayed green while the server's instrument accept
  thread and watchdog crashed at startup with `AttributeError`. The PR
  merged while its `integration` job (the only executor of that path) was
  still running; the job then hung rather than failed, because the
  half-dead server accepted TCP but never completed the handshake, and
  `multiprocessing`'s `Client()` blocks with no timeout — so the
  client-side connect deadline could never fire. Three fixes: the methods
  moved to the right class; connection attempts are now bounded by the
  connect deadline even mid-handshake (`_connect_once`, with a
  base-environment regression test that simulates the half-dead server
  with a bare socket); and the Nion stack is now installed in the dev
  container, so the device suites run before a push instead of first
  running in CI. The lesson generalizes: a check that has never executed
  the code it guards is not a check, and merging while it runs is
  betting it will pass.
- *The SIGTERM handler made a wedged server harder to kill, not easier.*
  Parking straight from the handler meant that when the park itself was
  what had wedged — the case SIGTERM most needs to reach — the handler
  blocked forever and the client escalated to `SIGKILL`. The park now
  runs on its own thread with a deadline inside the client's escalation
  window, and the process exits with a distinct status if it expires: a
  termination request that cannot be honoured gracefully must still be
  honoured. An existing test caught this, which is what it was for.

## 1. Findings that can corrupt or lose recorded data

### 1.1 ✅ Fixed — `fov_nm` means different things to Nion and to our calibration — non-square scans get wrong axes

**Verified against Nion's own source** (nionswift-instrumentation 23.6.2,
the exact pin in `pyproject.toml`), `nion/instrumentation/scan_base.py`:

```python
pixel_size_nm = fov_nm / max(scan_shape)   # get_scan_calibrations
```

Nion applies `fov_nm` to the **longer** axis with **square pixels**; the
shorter axis spans proportionally less. Our
`FrameCalibration.from_field_of_view` (`storage/calibration.py:628-631`)
divides `fov_nm` by **each axis independently**:

```python
y=AxisCalibration(AxisKind.REAL_SPACE, fov_nm / height, units="nm"),
x=AxisCalibration(AxisKind.REAL_SPACE, fov_nm / width, units="nm"),
```

`interface.py:80-82` defines `fov_nm` only as "field of view of the
scanned region" — it never says which axis — and `nion_server.py:301-313`
passes it through to `ScanFrameParameters` and records the bare scalar in
metadata. So for a non-square scan (which the integration tests
deliberately exercise for *shape*, e.g. 32×48, but never for
*calibration*), the NeXus file's slow-axis scale is wrong by the aspect
ratio. Every downstream consumer — the HyperSpy adapter, any NeXus-aware
tool — inherits the error silently. Square scans agree by coincidence,
which is why nothing has failed.

**Fix**: pin the convention in `ScanParameters` (document `fov_nm` as the
longer-axis extent, add a derived `fov_size_nm -> (y_nm, x_nm)`), have the
scanner emit the per-axis extent in metadata, and make
`from_field_of_view` consume that instead of re-deriving. Add a
non-square-scan calibration round-trip test. Sanity-check against usim
data on the next `device`-extra run (the convention above is from Nion's
calibration code; the data path should be confirmed once, on principle).

### 1.2 ✅ Fixed — `LiveAcquisition.stop()` reports success on a timed-out join → two threads on one device → torn frames

`acquisition/live.py:86-92`: `thread.join(timeout)` discards its result
and `self._thread = None` runs unconditionally, so after a 5 s timeout
`is_running` is `False` **while the worker is still inside the grab**.
The viewer trusts that flag: `record_scan_frames`
(`viewer/live.py:723-733`) calls `stop_scan()` and immediately hands the
same scanner to a `RecordingJob` thread.

That is not mere contention. The single-buffer shared-memory reuse is
sound *only* because the protocol is strictly synchronous
(`shared_frame.py:22-26`), and the client's copy-out happens **outside**
the connection lock (`remote.py:413-418` — `send_call` releases the lock
before `self._reader.read(result)` runs). Two client threads on one
device let the server overwrite the segment while the first thread is
mid-copy: a silently torn frame, half scan N and half scan N+1, written
to disk as a recording. A long-dwell scan or a long camera exposure can
genuinely exceed the 5 s default.

**Fix** (three small pieces, all worth doing):
1. `stop()` checks `thread.is_alive()` after the join and reports failure
   (return `bool` or raise) instead of nulling the handle;
2. `remote.py:_frame` moves the shared-memory copy-out inside the
   connection lock, making the client safe against this class of bug
   regardless of caller discipline;
3. `record_scan_frames`/`record_camera_frames` refuse to start when the
   stop reports failure.

Related: `start_scan` constructs a fresh `LiveAcquisition` each time
(`viewer/live.py:507`), so a timed-out stop also *orphans* a live worker
holding a bound method of the widget (keeping the whole Qt graph alive
and the device driven, unreachable and unstoppable). Fix 1 removes the
trigger; reusing one loop instance (its restart path is implemented and
tested but dead in production) would remove the pattern.

### 1.3 ✅ Fixed — the single-writer shared-memory invariant was unenforced server-side

The server accepts **any number** of connections per target, each with a
handler thread (`nion_server.py:1209-1222`), and all of them share one
`SharedFrameWriter`. The reuse design's safety argument assumes exactly
one synchronous client per target — true today by convention only. A
second client (a second viewer pointed at the same ports, a debugging
session) reproduces the 1.2 tear without any bug in this codebase.
**Fix**: accept one connection per frame-producing target (refuse or
queue subsequent ones), or make writers per-connection.

### 1.4 ✅ Fixed — per-frame metadata is discarded — a recorded focal series keeps only frame 0's defocus

`nexus.py:441` stores `frame.metadata` only for the **first** frame;
`close()` writes only that. But `focal_series`
(`acquisition/sequence.py:173-180`) exists precisely to record per-frame
`defocus_nm` / `requested_defocus_nm` — "what the instrument did rather
than what it was asked to do" — and the writer throws all but the first
away. No per-frame metadata dataset exists in the layout. The writer also
holds a *reference* (not a copy) until `close()`.
**Fix**: persist per-frame metadata (a per-frame NXcollection JSON column
is enough to stop the loss; a typed dataset for numeric keys like
`defocus_nm` is the better end state), and copy what's held.

### 1.5 ✅ Fixed — `flush()` is the designed answer to the worst crash mode — and no production path calls it

The migration plan measured that a per-append `flush()` converts
"file does not open at all" into "short but readable", at a cost within
noise — and made `NexusWriter.flush()` public on that basis. Grep of
`src/`: it is **never called** outside its own definition. `write_frames`
(`nexus.py:568-571`), `sequence.record`, `Session.record`, and
`RecordingJob` all stream without it, so every real acquisition still has
the unbounded worst case the measurement was done to eliminate.
**Fix**: flush in `write_frames`' append loop (optionally
`flush_every=1` as a parameter). One line restores the designed
guarantee.

### 1.6 ✅ Fixed — `NexusWriter.close()` has no `try/finally`

`nexus.py:480-501`: any failure while finalizing (writing `end_time`, the
NXdata group, the metadata JSON) leaves `self._file` open and non-`None`
— the handle leaks, the file stays locked, and `__exit__` propagates with
the writer unusable. A concrete trigger exists in-repo: `interface.py:50`
allows 1D frames ("may be 1D for binned spectra"), and
`_write_nxdata` (`nexus.py:506`) does `self._data.shape[1], self._data.shape[2]`
— `IndexError` at close for a 1D series; a 3D frame instead writes a
silently malformed NXdata (rank-4 signal, 2 axes). **Fix**: wrap the
finalization in `try/finally` around the file close; validate frame rank
at `append` (first frame) so the failure lands where the caller can act —
the same principle the calibration path already follows
(`nexus.py:442-446`). Also: `append` documents a dtype check
(`nexus.py:427-431`) that doesn't exist — add it; h5py silently casts
today. And `close()` doesn't reset `_count`/`_first_metadata`/`_frame_zero`,
so a reused writer instance writes phantom zero-filled frames — guard or
support reuse, not the middle state.

### 1.7 ✅ Fixed — `rpc.disable_nagle` can silently flip the connection to non-blocking

`rpc.py:62-71` wraps the connection's fd in `socket.socket(fileno=...)`.
That constructor applies the process-wide `socket.setdefaulttimeout()`,
and setting a timeout marks the **underlying fd** `O_NONBLOCK` — which
`detach()` does not undo (verified empirically). If anything in the
application process sets a default socket timeout — plausible in a
napari/Qt app with HTTP-capable plugins — every RPC connection goes
non-blocking and `recv()` starts failing in ways that would be extremely
hard to trace here. **Fix**: record `os.get_blocking(fd)` before
wrapping and restore it before `detach()` (two lines), plus a unit test
that sets a default timeout and asserts blocking survives.

## 2. Coherence debt

These don't corrupt data; they are places where the same concept has
grown two or more implementations that will drift.

- **The NeXus layout is known by five modules.** `nexus.py` owns it, but
  `session.py:896-900` and `session.py:1067-1070` open HDF5 and hard-code
  `entry/...` paths directly (against its own stated rule at
  `session.py:64-70`), and all three analysis bridges know
  `/entry/data/data`. The message *"has no /entry/data group; it recorded
  no frames"* is byte-identical in four files (`nexus.py:632`,
  `hyperspy_bridge.py:113`, `libertem_bridge.py:143`,
  `py4dstem_bridge.py:189`). **Fix**: a small layout-constants +
  `open_recording()` helper in `storage/nexus.py`; the bridges and
  session read through it.
- **Session context has three write paths and a mismatched read
  surface.** Real NeXus groups (`NXsample`/`NXuser`/`NXnote`) are written
  but nothing in the codebase reads them; `read_session_context` reads
  only the `session_*` JSON-blob keys, which are lost exactly when the
  sidecar and NX groups would survive (unfinalized files). `Recording`
  carries no operator/sample fields. Pick one authority for read-back
  (the NX groups are the honest one) and make `read_session_context`
  read it.
- **Three worker-job classes, one pattern, divergent contracts.**
  `LiveAcquisition`, `RecordingJob`, `LoadJob` differ in stop/cancel
  vocabulary, timeout (5 s vs 30 s, unexplained), locking of `.error`
  (unlocked in `LiveAcquisition` — `live.py:101-103,126` — locked in
  both siblings), restart semantics (`RecordingJob._cancelled` is never
  cleared by `start()`; `LoadJob` cannot be cancelled at all). Extract
  one `_Job` base with a stated contract; the divergences are each a
  latent bug.
- **Protocol constants live on the wrong side of the boundary or are
  duplicated.** `_SHARED_MEMORY_THRESHOLD_BYTES` — a wire-protocol
  decision — lives in GPL `nion_server.py:126`, and the MIT test suite
  imports the GPL module to learn it (`test_remote_nion.py:42`).
  `_TARGET_NAMES` and the backend names are duplicated verbatim in
  `remote.py` and `nion_server.py`, and the positional-port argv ordering
  is an undocumented protocol contract (`strict=True` catches count, not
  order). Move all three into `rpc.py`/`shared_frame.py`, which is what
  "the entire license boundary" should mean.
- **Camera vs scanner metadata are different species**
  (`nion_server.py:248` raw vendor dict vs `:310-315` hand-built neutral
  keys), which is the concrete shape of the plan's open "nothing feeds
  calibration from the instrument" item. Worth normalizing when that
  plumbing lands, not before.
- **Analysis bridges diverged in shape**: two of three are named after
  their library, one (`load_as_diffraction_slice`) after its return type;
  LiberTEM's takes an extra `ctx` first (justified in its docstring);
  only py4DSTEM's refuses wrong axis kinds; none is re-exported from
  `analysis/__init__`. The viewer's three handlers are three hand-written
  ~60-line blocks sharing ~80% structure. One naming rule, one shared
  validation helper, one handler template.
- **Contract mismatches worth a one-line fix each**: `Camera.start()`
  documents "blocks until the first frame is available"
  (`interface.py:113-114`) but no implementation blocks;
  `remote_instrument`'s docstring says a bad backend is "reported back"
  (`remote.py:1004-1006`) but the server actually dies with exit 1 and
  the client raises a *hardware*-flavoured startup error — validate the
  backend name client-side against the constants `remote.py` already
  defines; `check_health` is documented "never raises" but has an
  unguarded cast (`remote.py:635-640`).
- **The health machinery has no production consumer** — ~120 client
  lines, a dedicated connection, and a server thread, exercised only by
  tests, while the viewer never polls it. Either wire it into the status
  bar (it was designed for exactly that) or mark it ahead-of-demand.

## 3. Optimization

The frame transport itself is in good shape — the measured shared-memory
redesign, TCP_NODELAY, and streaming HDF5 writes are all real wins, and
the live path has no gratuitous array copies. The waste is concentrated
in the display/UI layer and a few hot-path details:

- ✅ **Fixed — the viewer re-uploaded and re-normalized the same frame every 33 ms.**
  `_refresh_source` (`viewer/live.py:1236-1245`) has no "is this frame
  new?" check: at 10 fps acquisition and a 30 Hz timer, every frame is
  assigned to `layer.data` three times and `min()`/`max()` recomputed
  three times on the GUI thread (~1 GB/s of pointless traffic at
  Ronchigram size). The benchmark script already does the identity check
  the widget omits (`phase2_live_benchmark.py:141-145`). ~3 lines.
  Similarly, status labels are rewritten 30×/s with unchanged text, and
  each `loop.stats` call copies a 30-element list to use its two
  endpoints.
- ✅ **Fixed on `main` (#11) — the analysis buttons did acquire → compress →
  write → re-read on the GUI thread** (`viewer/live.py:984-1041`), against the module's own
  stated reason for `LoadJob` existing; the burst is also materialized
  via `list(camera_series(...))`, the exact pattern the streaming design
  exists to avoid, and the "working..." label never paints. Give them
  the `RecordingJob` treatment (the plan already lists this as the
  obvious next candidate).
- ✅ **Fixed — `Session.recordings()` opened every HDF5 file in the directory** and
  the viewer calls it on the GUI thread after every recording and in
  `_analysis_input`'s `finally` (`viewer/live.py:646,995` →
  `session.py:1067`). 200 recordings into a shift on a network mount is
  a multi-second freeze per click. Cache descriptions by (path, mtime).
  Also `Session.record` discards `record_frames`' return value and
  reopens the file it just wrote merely to recount frames
  (`session.py:393-399`).
- ✅ **Fixed — shared-memory segment thrash on shrink or alternating shapes.**
  `shared_frame.py:158-165` replaces the segment on any shape/dtype
  change, even when the existing segment is big enough — alternating
  512²/1024² pays create/unlink per frame, the exact regime the redesign
  measured as worse than pickle. Keep the segment when
  `nbytes <= segment.size`; shape/dtype already travel in the ref.
- **Immutable identities cost an RPC round trip per access.**
  `RemoteCamera.camera_id`, `RemoteScanner.scanner_id`, `channel_names`
  (`remote.py:456-485`) — and the widget reads `channel_names` twice at
  construction. Cache at connect; `stage_size_nm` already is, so this
  also removes an inconsistency. Server-side, the `hasattr`+`getattr`
  pair (`nion_server.py:1151,1163`) evaluates vendor properties twice per
  call and misreports a *raising* property as "unknown method"; use one
  `getattr` with a sentinel.
- **Writer/reader per-frame overheads**: `nexus.py:439` traverses
  `entry/instrument/detector` on every append but uses it only on the
  first; two `resize` calls plus a scalar `frame_time` write per frame
  (batch the times, or grow geometrically); `load_recording` peaks at 2×
  its byte budget (`np.stack` on top of the list — preallocate) and
  reads one over-budget frame before checking.

## 4. Robustness for hardware day

These matter little against usim and a lot against a real column:

- ✅ **Fixed — no signal handling in the server, so the SIGTERM fallback
  never parked the beam.** `park_and_release` is reachable only via the
  `shutdown` RPC; the wedged-server path — the one the fallback exists
  for — kills an unparked instrument. The server also shares the
  terminal's process group (no `start_new_session=True` at
  `remote.py:873-884`), so Ctrl-C SIGINTs it mid-acquisition, which is
  additionally the one scenario where the resource-tracker segment
  cleanup fails too. **Fix**: a SIGTERM/SIGINT handler that runs
  `park_and_release` with a short bound then `os._exit`, plus
  `start_new_session=True`.
- ✅ **Fixed — a dead client left the server holding the instrument
  forever**
  (`serve()` blocks on `stop_event` with no orphan detection). An idle
  watchdog or `PR_SET_PDEATHSIG` is cheap.
- **A failed shared-memory `publish` loses a successfully acquired
  frame** (`nion_server.py:1172-1177` — `/dev/shm` full turns a good
  frame into an error). Fall back to the inline pickle path; an
  optimization should not be load-bearing.
- **`_free_port()` is a TOCTOU race** with a multi-second window (ports
  probed, released, then the server imports the nion stack before
  binding) — two concurrent sessions or xdist workers can collide, and
  the failure surfaces as the misleading hardware-startup error. Bind in
  the parent and inherit fds, or have the server bind port 0 and report
  back.
- ✅ **Fixed — `rpc.py` and `shared_frame.py` had zero unit tests** and are only
  covered via tests gated on the `device` extra — in the base
  environment the license-boundary module and the shared-memory module
  are entirely untested, despite being pure-Python and trivially
  testable (socketpair, two threads). Highest-leverage tests to add;
  they'd also pin fixes 1.2(2), 1.7, and the resize-thrash behaviour.
- **Error identity is lost across the RPC boundary** — every server
  exception arrives as `RemoteCallError` with a stringified message
  (`rpc.py:112-114`); callers cannot distinguish a vendor timeout from a
  bad argument. `Result` is a frozen dataclass with defaults, so adding
  `error_type` is wire-compatible.
- **Test hooks are armed by bare env vars** (`MIAINWOODPECKER_WEDGE_*`,
  `_DELAY_SCAN`) inherited by the server; set in an operator's
  environment they make a real instrument unstoppable or slow. Gate them
  behind an explicit `--enable-test-hooks` flag.
- Smaller: `iter_ndata_directory` aborts a whole migration on the first
  corrupt file (its docstring promises per-readable-file behaviour);
  the session sidecar is truncate-then-write while filename reservation
  is scrupulously atomic — use write-temp + `os.replace`; a grab error
  leaves the viewer's 33 ms timer spinning and formatting the exception
  forever (`viewer/live.py:1233-1235` never stops the timer);
  `closeEvent` can block up to ~70 s with the timer already stopped, so
  nothing repaints; `_inspect` reports a locked/in-flight file with the
  same "damaged" verdict as a truly broken one (`session.py:1071-1075`).

## 5. What is genuinely good and should be preserved

Worth stating so a cleanup pass doesn't flatten it:

- The license boundary is mechanically verifiable, and the
  measure-don't-assume discipline is real — decisions carry their
  benchmarks, including designs that were tried and were worse.
- `interface.py` and `calibration.py` are the two best modules:
  deliberately minimal protocols with the "three controls, not hundreds"
  argument written down; a calibration model with first-class,
  *enforced* uncalibrated state, an invertible unit vocabulary, and CI
  that checks the claim against `pynxtools`' own matcher.
- The no-Qt-in-workers rule is structurally enforced (session jobs live
  outside the viewer package so they *cannot* reach Qt), frames never
  ride Qt signals, and the settings-tuple GUI→worker channel is a
  correct lock-free design.
- Failure modes are measured by *producing* them: tests SIGKILL real
  writer processes, kill the resource tracker to prove the leak before
  proving the mitigation, and assert control changes against a measured
  shot-noise floor. The degraded-file taxonomy
  (readable/finalized/damaged) is first-class UI, not an afterthought.
- `O_EXCL` filename reservation, graceful-shutdown ordering with each
  branch tested, and honest negative results (zstd, Zarr, LiberTEM
  calibration as a canary test) are all patterns worth keeping.

## 6. Priority order

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | 1.1 `fov_nm` convention → wrong non-square axes | data corruption | ✅ fixed |
| 2 | 1.2 `stop()` timeout lie → torn frames (3-part fix) | data corruption | ✅ fixed |
| 3 | 1.4 per-frame metadata discarded (focal series) | data loss | ✅ fixed |
| 4 | 1.5 `flush()` never called in production | data loss | ✅ fixed |
| 5 | 1.6 `close()` no try/finally + rank/dtype validation | data loss | ✅ fixed |
| 6 | 1.7 `disable_nagle` non-blocking flip | latent, hard to trace | ✅ fixed |
| 7 | 1.3 enforce one connection per frame target | latent corruption | ✅ fixed |
| 8 | 4 SIGTERM park + own process group | hardware safety | ✅ fixed |
| 9 | 3 viewer frame-identity check, label churn, timer-on-error | perf/UX | ✅ fixed |
| 10 | 4 unit tests for `rpc.py`/`shared_frame.py` | coverage of the boundary | ✅ fixed |
| 11 | 3 segment shrink/alternate thrash, `recordings()` cache | perf | ✅ fixed |
| 12 | 3 analysis buttons still block the GUI thread | UX | ✅ fixed on `main` (#11) |
| 13 | 4 orphan watchdog for a dead client | robustness | ✅ fixed |
| 14 | 2 layout helper, session-context read path, `_Job` base, boundary constants | maintainability | ✅ fixed |
| 15 | 4 error identity across RPC, test-hook gating, `iter_ndata_directory`, sidecar atomicity, `_inspect` lock/damage | robustness | ✅ fixed |

Every item is now done except the deliberate one in §7 (`Session.record`
re-reading what it wrote, kept for the verification it buys).

**Verification.** 198 tests pass (172 base + 26 viewer under
`xvfb-run`); `ruff check`, `pydoclint`, the NXem schema job, and the
`-W` docs build all clean, each run in a CI-equivalent environment
rather than assumed. Several fixes were checked against the bug they
claim to fix by reintroducing it: the `disable_nagle` tests fail with
the pre-fix body (`BlockingIOError`, as predicted), and each display
test fails with its own clause disabled.

Coverage gaps that remain, stated because they are where the next
surprise will come from: the device-extra integration tests
(`test_remote_nion.py`, including the four new ones for connection
exclusivity and SIGTERM parking) need a Nion stack this container does
not have, so CI is their first real run; and no Windows machine was
available, so that platform is covered by CI alone — which is exactly
where the first round of fixes broke.

## 7. What was not done, and why

Everything §7 originally deferred has since been done, except one entry
that was a decision rather than unfinished work. Kept here with its
reasoning so the record shows what was weighed.

- ~~**The analysis buttons still block the GUI thread**~~ — **done
  independently on `main` (#11)**, and this branch merged it rather than
  duplicating it. Worth recording how the two met: #11 centralised the
  camera stop into a single `_start_analysis`, so this branch's refusal
  to start when the live loop will not release the device — previously
  three copies, one per button — became one guard in one place. The
  merge left the fix strictly smaller than it was written.
- ~~**No orphan watchdog**~~ — **done**, and the policy question that
  had deferred it turned out not to need answering. The worry was
  choosing how long an instrument may sit idle-but-held, where a wrong
  guess parks a column someone is still using. But no idle timeout is
  needed: reconnect is deliberately unsupported (§6), so a live client
  holds its connections for its entire life, and *every connection
  closing* is that life ending. An idle client is still a connected one
  and cannot trip it. The grace period that remains guards a
  disconnect/reconnect race, not a judgement about instrument use.
- ~~**The coherence debt in §2**~~ — **done**: `storage/layout.py` holds
  the on-disk paths and the one `NoFramesError` that four modules used
  to spell out by hand; `nexus.read_frames` is the single reader the
  three analysis adapters share (which also halves the opens they made);
  the protocol vocabulary moved to `rpc.py`; and `BackgroundJob` in
  `miainwoodpecker/jobs.py` is the one threading contract the three
  one-shot jobs now share. `LiveAcquisition` deliberately stayed
  outside it — it is a loop with a latest-frame-wins buffer, not a
  one-shot with a result, and widening the base to cover both would
  have described neither well.
- ~~**The smaller §4 robustness items**~~ — **done**: error identity
  crosses the RPC boundary, the test hooks need `--enable-test-hooks`,
  `iter_ndata_directory` grew `skip_unreadable`, the sidecar write is
  atomic, and `_inspect` tells a locked file from a damaged one.
- **`Session.record` still reopens the file it just wrote** — the one
  item deliberately left. It is one HDF5 open per *recording* against a
  write measured in seconds, and what it buys is reading the frame count
  and finalized flag back from disk rather than trusting what the writer
  reported. That verification is worth more than the open costs. The
  `recordings()` cache addressed the case that actually scaled badly
  (one open per file per UI refresh).

Two findings came out of doing this work rather than out of the original
review, and both are the kind that only surface on contact:

- **`NXnote` is a rendering, not a field.** Reading session context back
  from the real NeXus groups looked like a uniform fix, but
  `_compose_notes` merges the session's standing notes with the
  recording's own and prefixes each with its scope — so recovering
  `notes` from `NXnote/description` hands back `"session: grid 3"` where
  the session holds `"grid 3"`. `NXsample` and `NXuser` round-trip
  faithfully; the note does not, and the group reader covers the two
  that do.
- **A second vacuous test, caught the same way as the first.** The
  locked-file test opened a file twice and asserted the good answer —
  and passed without the fix, because HDF5 locking does not engage in
  this environment at all. It now injects the error the library raises
  elsewhere and tests the classification, which is the part this project
  owns. Both this and the display tests were only found by reverting the
  fix and re-running; that check is now the habit rather than the
  exception.
