"""kg_engine — the deterministic graph engine for the creativity-graph Claude Code plugin.

Submodules:
  model       three axes, Node/Edge, span verification, frontmatter I/O
  boundary    P_write validation -> dispositions (span-present, never-forge-a-verdict, dedup)
  canon       crash-safe Markdown canon I/O, git-as-rollback, lease lock
  reconciler  P_reconcile: mtime/size pre-filter + full sweep, OOB-verdict re-quarantine
  scrub       egress PII/secret scrubbing with consistent placeholders
  pack        domain pack + glossary contract/loader/coverage
  projector   canon -> node-link graph.json + SQLite + Leiden + ranks + kg_context
  harness     annotation agreement, specificity metric, ideation scoring
  server      the MCP server (KGEngine facade + FastMCP tool surface)
"""

__version__ = "0.5.2"
