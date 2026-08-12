"""
Analysis integration: adapters into community analysis libraries.

Phase 4 of the migration plan (docs/migration-plan.md, §5) wires
existing analysis tools (HyperSpy, py4DSTEM, LiberTEM) in as thin
adapters rather than reimplementing analysis code. The HyperSpy adapter
in :mod:`miainwoodpecker.analysis.hyperspy_bridge` needs the ``analysis``
optional dependency group; the LiberTEM adapter in
:mod:`miainwoodpecker.analysis.libertem_bridge` needs the ``libertem``
optional dependency group; and the py4DSTEM adapter in
:mod:`miainwoodpecker.analysis.py4dstem_bridge` needs the separate
``py4dstem`` group (see that module's docstring for why it is scoped to
single diffraction patterns, not py4DSTEM's 4D ``DataCube``). None of
them is re-exported here — import whichever one is needed directly.

Four newer modules make up an optional process boundary around those
three: :mod:`miainwoodpecker.analysis.operations` (the analyses the
viewer runs, as plain functions),
:mod:`miainwoodpecker.analysis.transfer` (the wire vocabulary),
:mod:`miainwoodpecker.analysis.worker` (the subprocess that imports the
libraries) and :mod:`miainwoodpecker.analysis.remote` (the MIT client
that picks between in-process and isolated). It is **off by default**;
docs/analysis-isolation.md sets out what it buys, what it deliberately
does not settle, and what it costs.
"""
