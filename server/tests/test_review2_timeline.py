"""Review 2 (AGEDS-TASK-20261016-R2) finding #4 and P4: a non-default timeline projection evicted by
another request between the refresh and the reads must never yield a silently empty analysis or
timeline page. Reads happen in one snapshot; an eviction before it is fixed means another refresh,
and repeated evictions fail loudly. Temporary synthetic databases only."""
from __future__ import annotations

from contextlib import closing

import pytest

from server.app import db as dbmod
from server.app.analysis import source
from server.app.api import timeline as timeline_api
from server.app.timeline import analysis_items
from server.app.timeline.ruleset import display_params, load_rule
from server.tests.test_review_fixes_support import env  # noqa: F401 (fixture)
from server.tests.test_review_fixes_timeline import _events

ZONE, RULE = 'Europe/Warsaw', 'operator-zone'


def _evicting(monkeypatch, evictions: int | None):
    """ensure_projection that lets another client build a different zone (evicting ours, cap 1) right after it
    returns, for the first ``evictions`` calls (None = every call). Patched where the pre-fix code called it too."""
    real = analysis_items.ensure_projection
    state = {'left': evictions, 'evictions': 0}

    def wrapper(db, rule=None, params=None, *args, **kwargs):
        out = real(db, rule, params, *args, **kwargs)
        if params and (state['left'] is None or state['left'] > 0):
            if state['left'] is not None:
                state['left'] -= 1
            with closing(dbmod.connect()) as other:
                real(other, load_rule(None, kind='display_rule'), display_params('Asia/Tokyo', 'other-client'))
            state['evictions'] += 1
        return out
    monkeypatch.setenv('EW_TIMELINE_MAX_PROJECTIONS', '1')
    monkeypatch.setattr(analysis_items, 'ensure_projection', wrapper)
    monkeypatch.setattr(timeline_api, 'ensure_projection', wrapper, raising=False)
    return state


def test_review2_4_eviction_between_refresh_and_read_is_retried(env, monkeypatch):  # noqa: F811
    _events(50)
    state = _evicting(monkeypatch, 1)
    with dbmod.session() as db:
        loaded = source.load_items(db, assume_zone=ZONE, assumption_rule=RULE)
        assert not db.in_transaction  # the read snapshot opened by the seam is closed again
    assert state['evictions'] == 1
    assert len(loaded['items']) == 50 and not loaded['truncated']  # pre-fix: 0 items, no error
    assert env.one('SELECT COUNT(*) FROM timeline_projection_evictions') >= 2  # evicted, rebuilt, other evicted


def test_review2_4_eviction_after_the_snapshot_is_fixed_does_not_empty_the_reads(env, monkeypatch):  # noqa: F811
    _events(50)
    monkeypatch.setenv('EW_TIMELINE_MAX_PROJECTIONS', '1')
    rule = load_rule(None, kind='display_rule')
    with closing(dbmod.connect()) as db:
        projection = analysis_items.open_snapshot(db, rule, display_params(ZONE, RULE))
        assert db.in_transaction
        with closing(dbmod.connect()) as other:  # another request evicts our projection now
            analysis_items.ensure_projection(other, rule, display_params('Asia/Tokyo', 'other-client'))
            assert other.execute('SELECT COUNT(*) FROM timeline_items WHERE projection_id=?',
                                 (projection['id'],)).fetchone()[0] == 0
        inside = db.execute('SELECT COUNT(*) FROM timeline_items WHERE projection_id=?', (projection['id'],)).fetchone()[0]
        assert inside == 50  # the snapshot still sees the projection it was opened on
        db.rollback()


def test_review2_4_repeated_evictions_fail_loudly(env, monkeypatch):  # noqa: F811
    _events(20)
    _evicting(monkeypatch, None)
    with dbmod.session() as db:
        with pytest.raises(analysis_items.ProjectionNotFresh):  # pre-fix: returned 0 items silently
            source.load_items(db, assume_zone=ZONE, assumption_rule=RULE)


def test_review2_4_budget_and_event_study_inputs_survive_an_eviction(env, monkeypatch):  # noqa: F811
    from server.app.analysis import budget, categories, eventstudy, preregistration
    _events(50)
    _evicting(monkeypatch, 1)
    config = categories.parse_config({'schema': categories.CONFIG_SCHEMA, 'version': 't', 'categories': [{'id': 'a'}],
                                      'rules': [{'id': 'r', 'kind': 'event_type', 'values': ['whatsapp_message'],
                                                 'weights': {'a': 1}}]})
    with dbmod.session() as db:
        prepared = budget.prepare_inputs(db, config, window_from='2023-12-31', window_to='2024-01-03', zone=ZONE,
                                         assume_zone=ZONE, assumption_rule=RULE, persist=False)
    assert prepared['input']['items_in_window'] == 50
    _evicting(monkeypatch, 1)
    definition = preregistration.normalize_definition({
        'title': 't', 'text': 't', 'stimulus': {'event_types': ['whatsapp_message']},
        'outcome': {'event_types': ['whatsapp_message']}, 'windows': [{'label': 'w', 'start_s': 0, 'end_s': 60}],
        'analysis': {'assume_zone': ZONE, 'assumption_rule': RULE, 'permutations': 9}})
    params, _ = eventstudy.effective_parameters(definition, None)
    with dbmod.session() as db:
        loaded, _, _, _ = eventstudy.load_inputs(db, definition, params)
    assert len(loaded['items']) == 50


def test_review2_p4_timeline_http_reads_survive_an_eviction(env, monkeypatch):  # noqa: F811
    _events(50)
    query = {'assume_zone': ZONE, 'assumption_rule': RULE, 'limit': 200}
    _evicting(monkeypatch, 1)
    with env.client() as http:
        page = http.get('/api/timeline/window', params=query)
        assert page.status_code == 200, page.text
        assert len(page.json()['items']) == 50  # pre-fix: an empty page
    _evicting(monkeypatch, None)
    with env.client() as http:
        busy = http.get('/api/timeline/window', params=query)
        assert busy.status_code == 503 and 'evicted' in busy.json()['detail']
        density = http.get('/api/timeline/density', params={'assume_zone': ZONE, 'assumption_rule': RULE})
        assert density.status_code == 503
