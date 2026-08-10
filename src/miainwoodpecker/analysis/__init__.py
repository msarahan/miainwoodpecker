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
"""
