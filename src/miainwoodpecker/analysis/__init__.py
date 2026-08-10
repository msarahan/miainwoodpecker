"""
Analysis integration: adapters into community analysis libraries.

Phase 4 of the migration plan (docs/migration-plan.md, §5) wires
existing analysis tools (HyperSpy, py4DSTEM, LiberTEM) in as thin
adapters rather than reimplementing analysis code. The HyperSpy adapter
in :mod:`miainwoodpecker.analysis.hyperspy_bridge` needs the ``analysis``
optional dependency group, and the LiberTEM adapter in
:mod:`miainwoodpecker.analysis.libertem_bridge` needs the ``libertem``
optional dependency group, so neither is re-exported here — import
whichever one is needed directly.
"""
