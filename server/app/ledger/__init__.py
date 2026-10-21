"""Ledger area: financial, transport and health/fitness records (task AGEDS-TASK-20261017-L3).

``money`` (exact amounts), ``textio`` (encoding hypotheses, delimited text),
``timeparse`` (format-driven dates), ``store`` (append-only rows, signal
metadata) and ``health`` (opt-in health samples and sessions). Importers live in
``server/app/importers`` (``tabular_profiles``, ``camt053``, ``mt940``,
``health_*``). See docs/LEDGER.md.
"""
