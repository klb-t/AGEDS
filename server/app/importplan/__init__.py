"""Catalog -> import plan -> confirmed execution (AGEDS-TASK-20261016-I).

* ``detect``   — versioned, data-driven detectors (``data/detectors.v1.json``)
  that turn catalog entries of pinned scans into candidate import units, with
  bounded prefix probes (:mod:`reader`).
* ``plan``     — stored, append-only plans: immutable items, review versions
  with per-item decisions, dry-run summary.
* ``execute``  — runs approved items through the real importers with the
  entry's collection and virtual locator; archive members are materialized to
  a temporary directory outside every source root and re-verified by SHA-256.
* ``adapters`` — the importer registry (opt-ins, parameters, call shims).
* ``cross``    — the same chat conversation seen in several exports.
* ``abandon``  — audited closing of a catalog scan left ``running``.

Nothing here writes into a scanned source tree.
"""
PLANNER_VERSION = 'importplan/1'
