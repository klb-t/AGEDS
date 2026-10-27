"""Ecosystem embedding: versioned read API, local tokens, partner adapters.

Modules:

- ``tokens``: local bearer tokens stored as salted digests; scope checks;
  audit without token values; opt-in enforcement for all ``/api/*`` routes.
- ``read_api``: read-only projections (search, timeline window, artifact,
  citation packet, reports stub) with explicit limits and coverage flags.
- ``intents``: a typed intent contract for conversational clients; the
  executor returns anchored items only, never synthesized prose; a separate
  labelled interpretive contract flags unreferenced claims as unsupported.
- ``proposals``: annotation proposals from clients, quarantined until a human
  accepts or rejects them.
- ``loom_graph``: typed graph export with provenance on every node and edge;
  imported relations stay quarantined hypotheses until promoted.

No module calls a model or a network. Originals are never read; only database
rows are projected. ``schema/integrations.py`` owns the tables.
"""
