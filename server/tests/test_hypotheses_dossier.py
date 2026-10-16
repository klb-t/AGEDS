"""Dossier pointers pinned to source versions, human decisions, candidate proposals and the
provider interface; thesis typology with computed flags; epistemic disclosure."""
import json
import sqlite3

import pytest

from server.app.analysis import dossier, eventstudy, preregistration as pr, theses
from server.app.analysis.hypotheses import DISCLAIMER, HypothesisError, HypothesisNotFound
from server.tests.corpus import hypotheses as corpus


@pytest.fixture
def env(tmp_path, monkeypatch):
    from server.app import citations, db as dbmod, evidence
    from server.app.config import Settings
    s = Settings(data_dir=tmp_path, db_path=tmp_path / 'e.db', store_dir=tmp_path / 'store')
    monkeypatch.setattr(dbmod, 'settings', s)
    monkeypatch.setattr(evidence, 'settings', s)
    dbmod.init_db()
    scenario = corpus.SCENARIOS['unknown_zone']()
    ids = corpus.populate(scenario)
    source = evidence.ensure_source('synthetic', 'dossier fixture')
    path = tmp_path / 'synthetic-note.txt'
    path.write_bytes(b'synthetic recording placeholder')
    artifact = evidence.ingest_file(path, source_id=source)
    transcript = evidence.add_derived_text(artifact, 'transcript', ' wizyta u lekarza <b>raw</b>',
                                           segments=[{'start': 0.0, 'end': 1.5, 'text': ' wizyta u lekarza <b>raw</b>'}])
    anchor = citations.create_citation(artifact, transcript, [0])
    connection = dbmod.connect()
    observation = connection.execute('SELECT id FROM source_observations WHERE artifact_id=?', (artifact,)).fetchone()[0]
    annotation = connection.execute("INSERT INTO annotations(artifact_id,derived_text_id,kind,body) VALUES (?,?,?,?)",
                                    (artifact, transcript, 'note', 'operator note')).lastrowid
    connection.commit()
    version = pr.register_hypothesis(connection, corpus.definition_for(scenario))
    yield {'db': connection, 'ids': ids, 'artifact': artifact, 'transcript': transcript, 'anchor': anchor['id'],
           'observation': observation, 'annotation': annotation, 'version': version, 'hid': version['hypothesis_id'],
           'scenario': scenario}
    connection.close()


def test_items_pin_exact_source_versions_and_detect_changes(env):
    db, hid = env['db'], env['hid']
    event_id = env['ids']['stimulus'][0]
    added = {
        'event': dossier.add_item(db, hid, 'event', event_id, classification='supporting', rationale='letter on file'),
        'artifact': dossier.add_item(db, hid, 'artifact', env['artifact'], observation_id=env['observation']),
        'derived_text': dossier.add_item(db, hid, 'derived_text', env['transcript'], classification='context'),
        'anchor': dossier.add_item(db, hid, 'anchor', env['anchor'], classification='supporting'),
        'annotation': dossier.add_item(db, hid, 'annotation', env['annotation'], classification='contradicting'),
    }
    assert added['event']['locator']['event_id'] == event_id and len(added['event']['locator']['body_sha256']) == 64
    assert added['artifact']['locator']['observation']['id'] == env['observation']
    assert added['derived_text']['locator']['derived_text_id'] == env['transcript']
    anchor = added['anchor']['locator']
    assert (anchor['anchor_id'], anchor['derived_text_id'], anchor['artifact_id']) == (env['anchor'], env['transcript'], env['artifact'])
    assert anchor['selector']['kind'] == 'segments' and added['anchor']['excerpt'] == ' wizyta u lekarza <b>raw</b>'
    assert {i['source_check'] for i in added.values()} == {'matches'}
    assert added['artifact']['classification'] == 'candidate' and added['event']['human_reviewed']
    again = dossier.add_item(db, hid, 'event', event_id)
    assert again['created'] is False and again['item_id'] == added['event']['item_id']
    db.execute("UPDATE annotations SET body='edited later' WHERE id=?", (env['annotation'],))
    db.commit()
    assert dossier.get_item(db, added['annotation']['item_id'])['source_check'] == 'changed'
    with pytest.raises(HypothesisNotFound):
        dossier.add_item(db, hid, 'event', 10**9)
    with pytest.raises(HypothesisNotFound):
        dossier.add_item(db, hid, 'artifact', env['artifact'], observation_id=10**9)
    with pytest.raises(HypothesisError, match='observation_id applies'):
        dossier.add_item(db, hid, 'event', event_id, observation_id=env['observation'])
    with pytest.raises(HypothesisError):
        dossier.add_item(db, hid, 'event', event_id, classification='proven')
    for table in ('hypothesis_dossier_items', 'hypothesis_dossier_decisions'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute(f'DELETE FROM {table}')
        db.rollback()


def test_candidates_stay_candidates_until_a_human_decides(env):
    db, hid = env['db'], env['hid']
    result = dossier.candidate_search(db, hid, limit=50)
    assert result['disclaimer'] == DISCLAIMER and result['provider']['name'] == 'none'
    assert result['fts'][0]['query'] == 'wizyta' and result['rule_hits'] > 0
    listing = dossier.list_dossier(db, hid)
    assert listing['counts']['candidate'] == len(listing['items']) == len(result['added_item_ids'])
    assert {i['origin'] for i in listing['items']} >= {'fts', 'rule'}
    assert all(i['classified_by'] == 'system' and not i['human_reviewed'] for i in listing['items'])
    proposals = json.loads(db.execute('SELECT result_json FROM hypothesis_runs WHERE id=?', (result['run_id'],)).fetchone()[0])['proposals']
    assert {p['hint'].get('role') for p in proposals if p['origin'] == 'rule'} == {'stimulus', 'outcome_in_window'}
    in_window = [p for p in proposals if p['hint'].get('role') == 'outcome_in_window']
    assert all(0 <= p['hint']['lag_s'] < 2 * 86400 for p in in_window) and len(in_window) == 5
    # an event found by FTS and by a rule is one item (same pinned locator), first origin kept
    assert len({p['locator_sha256'] for p in proposals}) == len(result['added_item_ids'])
    run = db.execute('SELECT kind, provider, provider_opt_in FROM hypothesis_runs WHERE id=?', (result['run_id'],)).fetchone()
    assert tuple(run) == ('candidate_search', 'none', 0)
    repeat = dossier.candidate_search(db, hid, limit=50)
    assert repeat['added_item_ids'] == [] and sorted(repeat['already_present_item_ids']) == sorted(result['added_item_ids'])
    item_id = result['added_item_ids'][0]
    dossier.classify(db, item_id, 'supporting', rationale='checked against the letter', actor='analyst')
    final = dossier.classify(db, item_id, 'rejected', rationale='wrong period', actor='analyst')
    assert final['classification'] == 'rejected' and final['human_reviewed']
    assert [d['classification'] for d in final['decisions']] == ['candidate', 'supporting', 'rejected']
    assert [d['decided_by'] for d in final['decisions']] == ['system', 'human', 'human']
    with pytest.raises(sqlite3.IntegrityError):  # the system path can never decide a classification
        db.execute('''INSERT INTO hypothesis_dossier_decisions(item_id,hypothesis_version_id,classification,decided_by,created_at)
                      VALUES (?,?,?,?,?)''', (item_id, env['version']['version_id'], 'supporting', 'system', 'x'))
    db.rollback()
    other = pr.register_hypothesis(db, corpus.definition_for(env['scenario']) | {'title': 'other'})
    with pytest.raises(HypothesisError, match='not a version'):
        dossier.classify(db, item_id, 'context', version_id=other['version_id'])


class FakeProvider(dossier.CandidateProvider):
    name = 'fake-llm'
    model = 'fake-model'
    model_version = 'test'

    def __init__(self, event_id):
        self.event_id = event_id
        self.requests = []

    def propose(self, request):
        self.requests.append(request)
        return [{'target_kind': 'event', 'target_id': self.event_id, 'rationale': 'model says relevant; 99% sure',
                 'proposed_classification': 'supporting'},
                {'target_kind': 'event', 'target_id': 10**9, 'rationale': 'invented'},
                {'target_kind': 'nonsense', 'target_id': 1},
                'not even a dict']


def test_provider_interface_requires_opt_in_and_never_decides(env, monkeypatch):
    db, hid = env['db'], env['hid']
    fake = FakeProvider(env['ids']['outcome'][0])
    monkeypatch.setitem(dossier.PROVIDERS, fake.name, fake)
    with pytest.raises(HypothesisError, match='opt-in'):
        dossier.candidate_search(db, hid, provider='fake-llm', use_rules=False)
    assert fake.requests == []  # nothing was sent without opt-in
    with pytest.raises(HypothesisError, match='unknown candidate provider'):
        dossier.candidate_search(db, hid, provider='remote-paid', opt_in=True)
    result = dossier.candidate_search(db, hid, provider='fake-llm', opt_in=True, use_rules=False, queries=[])
    assert len(fake.requests) == 1 and result['provider'] == {'name': 'fake-llm', 'opt_in': True, 'external': True, 'returned': 4}
    assert len(result['rejected']) == 3
    llm_items = [i for i in dossier.list_dossier(db, hid)['items'] if i['origin'] == 'llm']
    assert len(llm_items) == 1 and llm_items[0]['classification'] == 'candidate'
    hint = llm_items[0]['metadata']['hint']
    assert hint['model_proposed_classification'] == 'supporting' and 'not evidence' in hint['label']
    assert llm_items[0]['excerpt'] == 'wpis zdrowotny wizyta'  # excerpt from the database, not model text
    row = db.execute("SELECT provider, provider_opt_in, parameters_json FROM hypothesis_runs WHERE kind='candidate_search'").fetchone()
    assert row['provider'] == 'fake-llm' and row['provider_opt_in'] == 1 and '"provider_model":"fake-model"' in row['parameters_json']
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE hypothesis_runs SET provider_opt_in=0")
    db.rollback()


def test_thesis_typology_and_computed_unsupported_flag(env):
    db, hid = env['db'], env['hid']
    quote_item = dossier.add_item(db, hid, 'anchor', env['anchor'], classification='supporting')
    artifact_item = dossier.add_item(db, hid, 'artifact', env['artifact'])
    a = theses.add_thesis(db, hid, 'The recording says "wizyta u lekarza".', 'quote', [quote_item['item_id']])
    b = theses.add_thesis(db, hid, 'The letter caused the visit.', 'inference_chain', [])
    c = theses.add_thesis(db, hid, 'Quoted from a file', 'quote', [artifact_item['item_id']])
    assert a['flags'] == [] and a['status_rank'] == 1 and a['status_label_pl'] == 'cytat'
    assert b['unsupported'] and b['flags'] == ['unsupported'] and b['flags_computed']
    assert set(c['flags']) == {'evidence_not_accepted', 'quote_without_exact_source'}
    revised = theses.add_thesis(db, hid, 'The visit followed the letter (timing only).', 'speculation',
                                [quote_item['item_id']], previous_thesis_id=b['thesis_id'])
    listing = theses.list_theses(db, hid)
    by_id = {t['thesis_id']: t for t in listing['theses']}
    assert by_id[b['thesis_id']]['superseded_by'] == revised['thesis_id'] and 'superseded' in by_id[b['thesis_id']]['flags']
    assert listing['unsupported_current'] == 0 and listing['disclaimer'] == DISCLAIMER
    assert [t['id'] for t in listing['typology']] == list(theses.STATUS_TYPES)
    with pytest.raises(HypothesisError, match='already has a revision'):
        theses.add_thesis(db, hid, 'fork', 'analogy', [], previous_thesis_id=b['thesis_id'])
    other = pr.register_hypothesis(db, corpus.definition_for(env['scenario']) | {'title': 'other'})
    with pytest.raises(HypothesisError, match='does not belong'):
        theses.add_thesis(db, other['hypothesis_id'], 'x', 'analogy', [quote_item['item_id']])
    with pytest.raises(sqlite3.IntegrityError):  # DB-level guard as well
        db.execute('INSERT INTO hypothesis_thesis_evidence(thesis_id,item_id) VALUES (?,?)',
                   (theses.add_thesis(db, other['hypothesis_id'], 'y', 'analogy', [])['thesis_id'], quote_item['item_id']))
    db.rollback()
    with pytest.raises(HypothesisError):
        theses.add_thesis(db, hid, 'x', 'certainty', [])
    db.execute("UPDATE annotations SET body='changed' WHERE id=?", (env['annotation'],))
    db.commit()
    note_item = dossier.add_item(db, hid, 'annotation', env['annotation'], classification='context')
    d = theses.add_thesis(db, hid, 'note', 'paraphrase', [note_item['item_id']])
    assert d['flags'] == []
    db.execute("UPDATE annotations SET body='changed again' WHERE id=?", (env['annotation'],))
    db.commit()
    assert 'source_changed' in next(t for t in theses.list_theses(db, hid)['theses'] if t['thesis_id'] == d['thesis_id'])['flags']


def test_epistemic_disclosure_combines_human_text_and_computed_facts(env):
    db, hid = env['db'], env['hid']
    empty = theses.disclosure_view(db, hid)
    assert [q['quadrant'] for q in empty['quadrants']] == list(theses.QUADRANT_IDS)
    assert {'fact': 'event_study', 'value': 'not run yet'} in empty['quadrants'][1]['computed_facts']
    eventstudy.run_event_study(db, env['version']['version_id'])
    theses.add_disclosure(db, hid, 'known_unknowns', 'Whether the phone clock was set to local time.', actor='analyst')
    view = theses.add_disclosure(db, hid, 'known_unknowns', 'Phone clock zone is unknown; see exclusions.')
    q = {x['quadrant']: x for x in view['quadrants']}
    assert q['known_unknowns']['human_text'] == 'Phone clock zone is unknown; see exclusions.'
    assert len(q['known_unknowns']['human_history']) == 2
    facts = {f['fact']: f for f in q['known_unknowns']['computed_facts']}
    assert facts['untimed_events_excluded']['value'] == 7
    assert facts['untimed_events_excluded']['by_reason'] == {'no_time_candidate': 1, 'unknown_zone': 6}
    assert 'unknown_zone_excluded' in facts['flagged_warnings']['value']
    unknown_knowns = {f['fact']: f for f in q['unknown_knowns']['computed_facts']}
    total = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    assert unknown_knowns['events_imported_before_first_registration']['of_total_events'] == total
    assert 0 <= unknown_knowns['events_imported_before_first_registration']['value'] <= total
    gaps = next(f for f in q['unknown_unknowns']['computed_facts'] if f['fact'] == 'documentation_gaps')
    assert gaps['period_s'] == 86400 and gaps['empty_periods'] is not None
    assert view['disclaimer'] == DISCLAIMER
    with pytest.raises(HypothesisError):
        theses.add_disclosure(db, hid, 'unknown_others', 'x')
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        db.execute('UPDATE hypothesis_disclosures SET text=?', ('rewritten',))
    db.rollback()
