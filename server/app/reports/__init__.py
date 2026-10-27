"""Reports: computed documents rendered from templates (task AGEDS-TASK-20261016-F).

A report is built in three separated steps:

1. **Providers** (``reports.providers``) read the database in one read-only
   transaction and return section dicts (contract ``ageds.report-section/v1``):
   computed values, the rows they read with their SHA-256, model runs,
   unknowns and exclusions. Nothing is written.
2. **Document** (``reports.document``) adds the method record (layer 1 of the
   standard: version, commit, parameters, template, runs, unknowns, range,
   exclusions, hashes) and the content digest. Templates receive only this
   computed document; every number in the HTML is a value of the document.
3. **Outputs** (``reports.outputs``) render HTML (primary), JSON, CSV/XLSX for
   tables and optionally PDF from the HTML, export the citation bundle and
   write a manifest with SHA-256 and size of every file, into a NEW directory
   below a user-chosen private output directory (never the repository, the
   content store or a scan root). The register row is append-only.

Digests detect change against known values. Nothing here is a signature,
authorship proof, truth check or custody claim.
"""
