# miainwoodpecker

Instrument control and data analysis for scanning transmission electron
microscopes — a [Nion Swift](https://github.com/nion-software/nionswift)
replacement built as thin glue over tools the scientific Python
community already maintains: napari for display, NeXus/HDF5 for data,
HyperSpy/LiberTEM/py4DSTEM for analysis.

There are two ways to use it, and they are the same thing underneath —
every button in the viewer is one call into the Python API, and both
write identical files into shared sessions:

::::{grid} 2

:::{grid-item-card} At the microscope
:link: using-the-viewer
:link-type: doc
Launch the live viewer, watch the scan and camera, record data, and get
a first look at it — with a translation table for habits from Nion
Swift and DigitalMicrograph.
:::

:::{grid-item-card} From code
:link: scripting-and-automation
:link-type: doc
Script acquisitions and experiments, manage sessions, load recordings
into HyperSpy/LiberTEM/py4DSTEM, migrate Swift libraries — and what it
takes to put an AI agent at the controls.
:::

::::

Data is saved as standard NeXus HDF5 with calibrated axes: no private
format, no export step, readable by anything that speaks HDF5.

The project is young. Everything works against the bundled microscope
simulator; validation on real hardware is the current frontier (see the
[hardware validation checklist](hardware-validation-checklist.md), and
[what can be built before then](pre-hardware-work.md)).

## For developers

The design history is documented unusually thoroughly, decisions and
measurements included:

- [Migration plan](migration-plan.md) — the architecture, why each
  piece was built or adopted, and the phased record of getting here.
- [Architecture review](architecture-review.md) — a full-stack audit of
  the implementation, findings and fixes.
- [Other vendors](vendor-support.md) — what Thermo Fisher, JEOL, Zeiss,
  Hitachi and Bruker actually expose, and what a second device adapter
  would cost.
- [Analysis parity](analysis-parity.md) — every analysis Nion Swift
  offers, which ones HyperSpy/LiberTEM/py4DSTEM already cover, and a
  costed list of the ones they do not.
- [DECTRIS detectors](adapters/dectris.md) — whether the ELA is reachable
  without Gatan's software, what SIMPLON exposes, and the adapter built
  on it.
- [Gatan](adapters/gatan.md) — the one adapter that cannot be a
  subprocess, the inbound transport built for it, and why the facility
  that prompted it probably does not need it.
- [Development docs](documentation/index.md) — environments, linting,
  publishing.

:::{toctree}
:maxdepth: 2
:hidden:

Using the viewer <using-the-viewer>
Scripting and automation <scripting-and-automation>
Migration plan <migration-plan>
Architecture review <architecture-review>
Hardware validation checklist <hardware-validation-checklist>
Work before hardware <pre-hardware-work>
Other vendors <vendor-support>
Analysis parity <analysis-parity>
Hitachi SU9000II <adapters/hitachi>
DECTRIS detectors <adapters/dectris>
Gatan <adapters/gatan>
Development <documentation/index>
:::
