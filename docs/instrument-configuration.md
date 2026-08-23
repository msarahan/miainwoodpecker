# The instrument configuration file

One file per microscope, saying what hardware it has and which processes
drive each piece. The broker reads one and starts them all:

```
miainwoodpecker-broker --config instruments/superstem-3.toml --publish ./instrument
```

Four worked examples ship in `instruments/`: `simulator.toml`,
`superstem-1.toml`, `superstem-2.toml` and `superstem-3.toml`. The
simulator one runs on any machine with the `device` extra installed and
is the one to copy.

## One machine, one instrument

A control computer drives the same microscope every day, so its
configuration belongs in a fixed place rather than in a command line
somebody has to remember. Copy an example to
`$HOME/.miainwoodpecker/instrument.toml`, edit it, and:

```
pixi run -e device broker
```

That serves whatever that file describes and publishes the invitation
beside it, so a notebook or a dashboard reads
`$HOME/.miainwoodpecker/broker.json` and needs to be told nothing at
all. Extra arguments are appended, so `pixi run -e device broker --host
0.0.0.0` still works — knowingly, since that puts an instrument's
controls on the network.

Starting from the simulator is a real check rather than a formality: if
`pixi run -e device broker` serves a scan unit and two cameras, the
broker, the device layer and this file's schema are all working, and
anything that then goes wrong with a hardware file is about the
hardware.

## Why there is a file at all

An instrument is not one device server. SuperSTEM 3 is a Nion column
whose scan unit and Ronchigram camera come out of Nion's stack, plus a
DECTRIS ELA on the spectrometer that speaks SIMPLON over HTTP and knows
nothing about Nion. Each adapter is a separate process — that is the
shape of `miainwoodpecker.devices`, and the [license
boundary](migration-plan.md) is part of why. Before this file the broker
could start exactly one of them, named on its command line, so an
instrument with two adapters could not be served whole at all.

The file is also the only place that knows what the microscope *has*. A
device server reports what it found; nothing above it can tell "this
column has no EELS camera" from "this column's EELS camera did not come
up". A spectrometer left switched off, or a vendor plug-in that failed to
load, produces a perfectly consistent instrument with one fewer camera,
and every layer above serves it happily.

## The enumeration is authoritative

Three consequences, each deliberate, and each visible in the startup
output:

- **A device the file lists and its server does not serve is a startup
  failure**, naming the device and listing what that server did serve.
  The alternative is a session that looks normal until somebody reaches
  for the detector that was never there.
- **A device the file does not list is not served**, even if its server
  offers it — and is logged at warning level by name, so the fix is to
  paste three lines into the file. "Enumerates the hardware" has to mean
  all of it, or the check above means nothing.
- **Naming is done in the file, not by the server.** Two adapters both
  serve a target called `camera`; the file says which one is this
  instrument's `eels_camera`.

## The schema

TOML, because a file describing an instrument is mostly the reasons —
which control unit, why that plug-in, what the detector is mounted on —
and a format that cannot carry comments loses the part a second operator
needs. Unknown keys are refused rather than ignored: `plugin` where the
key is `plugins` would otherwise start a hardware server with no
arguments and say nothing about it.

```toml
schema = 1                      # required; refused if it is not 1
name = "SuperSTEM 3"            # required
site = "SuperSTEM, Daresbury"   # optional, free text
description = "..."             # optional, free text

[[server]]                      # one per adapter process
name = "column"                 # required, unique; names the process in logs
module = "miainwoodpecker.devices.nion_server"   # required; run with python -m
backend = "hardware"            # "simulated" (default) or "hardware"
plugins = []                    # the server's --plugin values, in order
controls_column = true          # at most one server; see below
enabled = true                  # default true
description = "..."             # optional, free text

[[server.device]]               # one per piece of hardware on that process
target = "eels_camera"          # required; the name clients see, unique across the file
served_as = "camera"            # what its own server calls it; defaults to target
description = "DECTRIS ELA on the IRIS spectrometer"
enabled = true                  # default true
```

**`backend` defaults to `simulated`.** Hardware is never what you get by
leaving something out — the same rule every other entry point in this
project follows.

**`plugins` means whatever that server takes `--plugin` to mean**:
`nionswift_plugin` module names for the Nion server, a camera index or
device path for the commodity camera server, a control-unit address for
DECTRIS. Deliberately not translated into per-adapter keys, because the
config layer would then have to know every adapter, including the
out-of-tree ones it cannot import.

**`controls_column` says whose `instrument` target is the microscope's.**
Every adapter has an `instrument` target; on all but one of them it is
that server's own control channel — a DECTRIS server's answers `describe`
and `shutdown` and knows nothing about a stage. At most one server may
set it, and none is legitimate: a detector-only rig has no column, and
the broker then serves no `instrument` target at all.

**`enabled = false` keeps a record of hardware without opening it.**
SuperSTEM 2's Bruker EDX detector is on the microscope and has no adapter
yet; the file should say the detector exists, and the broker should not
try to start it.

## Startup, and what it looks like

Servers start in file order and are torn down in reverse, stacked so
that a failure to start the third adapter still shuts down and parks the
first two. Put the column first and it is parked last, after the
detectors that were watching through it have stopped.

```
INFO Simulator: 2 servers serving scanner, ronchigram_camera, eels_camera, spectrum_detector
INFO starting column (miainwoodpecker.devices.nion_server, simulated backend)
INFO starting edx (miainwoodpecker.devices.spectrum_server, simulated backend)
INFO broker listening on localhost:51234
INFO serving scanner, ronchigram_camera, eels_camera, spectrum_detector, instrument
```

The first line is what the file says the microscope has, printed before
anything is opened; the last is what came up and is actually leasable.
Reading them as a pair is the check worth making before letting anyone
connect.

`--config` **replaces** `--backend`, `--plugin` and `--server-module`
rather than combining with them, and passing both is refused. Either
precedence would be a wrong answer somebody debugs at an instrument:
honour the file and `--backend hardware` silently does nothing, honour
the flag and one word overrides the backend of every server in the file
at once. `$MIAINWOODPECKER_INSTRUMENT` is the environment-variable form
of `--config`.

## What it cannot express yet

- **An adapter this process does not launch.** The Gatan bridge
  ([Gatan](adapters/gatan.md)) runs *inside* DigitalMicrograph and
  connects outward; `attached_instrument()` is the client for it. A
  configuration entry for one would need a transport and an invitation
  rather than a module and a backend.
- **The `replay` backend.** Playing a recording back as a device is real
  and supported, but the client that spawns servers accepts `simulated`
  and `hardware` only, so a file naming `replay` is refused when it is
  parsed rather than failing at launch.
- **Anything about how a device is *used*** — field of view, exposure,
  binning, which channels to record. That is a session's business, and
  putting it here would make the instrument's inventory a place where
  acquisition settings go stale.
