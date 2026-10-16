"""Time budget (area E1): candles, tables, sensitivity grid, category definitions.

Upstream contract (wired by the integrator, docs/REPORTS.md): a function
``(db, budget_run_id) -> dict`` returning the stored, append-only budget run
(parameters, category definitions, candles with lower/probable/upper bounds,
the ``unobserved`` band, sensitivity results). Its numbers are computed by the
analysis code; this section renders them as given. Until wired: unavailable.
"""
from __future__ import annotations

from .. import disclaimers
from ._upstream_view import upstream_section

VERSION = 'time_budget.v1'
UPSTREAM = (('server.app.analysis.budget', 'report_payload'), ('server.app.analysis.budget', 'get_budget_run'))


def provide(ctx) -> dict:
    return upstream_section(ctx, section_name='time_budget', version=VERSION, title_key='section_time_budget',
                            upstream=UPSTREAM, id_param='budget_run_id',
                            labels=[disclaimers.interpretive(ctx.lang),
                                    disclaimers.fixed(disclaimers.UNOBSERVED_ID, disclaimers.UNOBSERVED, ctx.lang)])
