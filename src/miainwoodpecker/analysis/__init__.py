"""
Analysis integration: adapters into community analysis libraries.

Phase 4 of the migration plan (docs/migration-plan.md, §5) wires
existing analysis tools (HyperSpy, py4DSTEM, LiberTEM) in as thin
adapters rather than reimplementing analysis code. The HyperSpy adapter
in :mod:`miainwoodpecker.analysis.hyperspy_bridge` needs the ``analysis``
optional dependency group, so it is not re-exported here — import it
directly.
"""
