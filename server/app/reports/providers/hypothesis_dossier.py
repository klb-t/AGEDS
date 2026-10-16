"""Hypothesis dossier (area E2): operationalization, evidence for/against,
event study, limitations, with the mandatory hashed disclaimer label.

Upstream contract (wired by the integrator, docs/REPORTS.md): a function
``(db, hypothesis_id) -> dict`` (for example ``analysis.dossier.list_dossier``)
returning the preregistered version, dossier items with anchors and status
typology, and event-study runs. Rendered as given. Until wired: unavailable;
the disclaimer is printed regardless.
"""
from __future__ import annotations

from .. import disclaimers
from ._upstream_view import upstream_section

VERSION = 'hypothesis_dossier.v1'
UPSTREAM = (('server.app.analysis.dossier', 'report_payload'), ('server.app.analysis.dossier', 'list_dossier'))


def provide(ctx) -> dict:
    return upstream_section(ctx, section_name='hypothesis_dossier', version=VERSION,
                            title_key='section_hypothesis_dossier', upstream=UPSTREAM, id_param='hypothesis_id',
                            labels=[disclaimers.interpretive(ctx.lang),
                                    disclaimers.fixed(disclaimers.CORRELATION_ID, disclaimers.CORRELATION, ctx.lang)])
