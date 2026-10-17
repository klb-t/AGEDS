"""Area-owned import-plan adapters.

Each module exposes ``IMPORTERS`` (a tuple of ``adapters.Importer``) and may use
the helpers of ``importplan.adapters`` (``RunContext``, ``acquire_entry``,
``_file_importer``-style wrappers). Detector rules for them go into
``importplan/data/detectors.d/<area>.v1.json``. Keys must be unique.
"""
