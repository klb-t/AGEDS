"""Profile -> explain -> promote: deterministic numbers, hypotheses, provenance, opt-in."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from server.app import db as dbmod
from server.app.catalog import describe
from server.app.catalog.scan import run_catalog_scan
from server.tests.test_catalog_scan import make_world, tree_state


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def test_profile_is_deterministic_private_label_free_and_reused(world):
    first = describe.build_profile(world.a['id'])
    second = describe.build_profile(world.a['id'])
    assert first == second
    text = json.dumps(first, ensure_ascii=False)
    assert 'Phone A' not in text and 'phone-a' not in text
    first_id, first_sha = describe.store_profile(first)
    assert describe.store_profile(second) == (first_id, first_sha)
    assert first['types']['pdf']['entries'] == 3 and first['content_groups_shared_with_other_collections'] >= 1
    assert first['max_archive_depth_seen'] == 4 and first['scan_complete'] is False


def test_provider_none_generates_nothing(world, tmp_path):
    result = describe.explain(world.a['id'], output_dir=tmp_path / 'side')
    assert result['status'] == 'not_run' and not (tmp_path / 'side').exists()
    with dbmod.session() as db:
        assert db.execute('SELECT COUNT(*) FROM catalog_description_runs').fetchone()[0] == 0


def test_fake_hypothesis_numbers_come_from_the_profile(world, tmp_path):
    result = describe.explain(world.a['id'], provider='fake', output_dir=tmp_path / 'side', params={'seed': '7'})
    annotation = result['annotation']
    profile = describe.build_profile(world.a['id'])
    assert annotation['placeholders']['totals.entries'] == profile['totals']['entries']
    assert f"zawiera {profile['totals']['entries']} wpisów" in annotation['resolved_text']
    assert '{{' in annotation['model_text'] and annotation['warnings'] == []
    sidecar = Path(result['sidecar'])
    assert sidecar.name == f'collection-{world.a["id"]}.annotations.ndjson'
    line = json.loads(sidecar.read_text().splitlines()[0])
    for key in ('run_id', 'prompt_sha256', 'input_profile_sha256', 'model', 'model_version', 'params', 'provider', 'created_at'):
        assert line[key], key
    assert (line['status'], line['format_compat'], line['format']) == ('hypothesis', 'unknown', describe.SIDECAR_FORMAT)
    assert line['input_profile_sha256'] == result['profile_sha256'] and line['params'] == {'seed': '7'}
    again = describe.explain(world.a['id'], provider='fake', output_dir=tmp_path / 'side')
    assert again['run_id'] != result['run_id'] and len(sidecar.read_text().splitlines()) == 2


def test_model_digits_and_unresolved_placeholders_are_flagged(world, tmp_path):
    digits = describe.explain(world.a['id'], provider='fake', output_dir=tmp_path, params={'style': 'with_digits'})
    assert [w['code'] for w in digits['annotation']['warnings']] == ['model_wrote_digits']
    unresolved = describe.explain(world.a['id'], provider='fake', output_dir=tmp_path, params={'style': 'unresolved'})
    assert unresolved['annotation']['warnings'][0] == {'code': 'unresolved_placeholder', 'path': 'no.such.path'}
    assert '{{no.such.path}}' in unresolved['annotation']['resolved_text']


def test_decisions_are_explicit_append_only_and_in_the_sidecar(world, tmp_path):
    result = describe.explain(world.a['id'], provider='fake', output_dir=tmp_path)
    annotation_id = result['annotation']['annotation_id']
    with pytest.raises(ValueError):
        describe.decide(annotation_id, 'promoted', actor=' ')
    with pytest.raises(ValueError):
        describe.decide(annotation_id, 'accepted', actor='reviewer')
    describe.decide(annotation_id, 'promoted', actor='reviewer', reason='matches the profile')
    describe.decide(annotation_id, 'rejected', actor='second reviewer', reason='changed mind')
    listed = describe.list_annotations(world.a['id'])
    assert listed[0]['current_status'] == 'rejected'
    lines = [json.loads(l) for l in Path(result['sidecar']).read_text().splitlines()]
    assert [l['record'] for l in lines] == ['hypothesis', 'decision', 'decision']
    assert [l.get('decision') for l in lines[1:]] == ['promoted', 'rejected']
    with dbmod.session() as db:
        actions = [r[0] for r in db.execute("SELECT action FROM audit_log WHERE object_kind='catalog_annotation' ORDER BY id")]
        assert actions == ['catalog_annotation_promoted', 'catalog_annotation_rejected']
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE catalog_annotations SET resolved_text='edited' WHERE id=?", (annotation_id,))
        with pytest.raises(sqlite3.DatabaseError):
            db.execute('DELETE FROM catalog_annotation_decisions')


def test_external_providers_need_per_run_opt_in_and_failures_are_runs(world, tmp_path, monkeypatch):
    class External(describe.FakeProvider):
        name, external = 'external-test', True

    class Broken(describe.FakeProvider):
        name = 'broken-test'

        def describe(self, profile, prompt, params):
            raise TimeoutError('provider timed out')
    monkeypatch.setitem(describe.PROVIDERS, 'external-test', External())
    monkeypatch.setitem(describe.PROVIDERS, 'broken-test', Broken())
    with pytest.raises(PermissionError):
        describe.explain(world.a['id'], provider='external-test', output_dir=tmp_path)
    allowed = describe.explain(world.a['id'], provider='external-test', output_dir=tmp_path, allow_external=True)
    assert allowed['annotation']['opt_in']['allow_external'] is True
    failed = describe.explain(world.a['id'], provider='broken-test', output_dir=tmp_path)
    assert failed['status'] == 'failed' and 'timed out' in failed['error'] and failed['annotation'] is None
    with dbmod.session() as db:
        rows = db.execute('SELECT provider, external, status, model FROM catalog_description_runs ORDER BY id').fetchall()
    assert [tuple(r) for r in rows] == [('external-test', 1, 'completed', 'fake-describer'), ('broken-test', 0, 'failed', 'unknown')]
    with pytest.raises(ValueError):
        describe.explain(world.a['id'], provider='missing', output_dir=tmp_path)
    with pytest.raises(ValueError):
        describe.register_provider(type('X', (), {'name': 'none', 'describe': None})())


def test_sidecar_never_goes_into_a_source_root(world):
    inside = Path(world.expected['roots']['phone_a']) / 'annotations'
    with pytest.raises(ValueError, match='outside'):
        describe.explain(world.a['id'], provider='fake', output_dir=inside)
    assert tree_state(world.corpus) == world.before


def test_profile_changes_with_new_scan_parameters(world, tmp_path):
    first = describe.build_profile(world.a['id'], min_gap_days=30)
    other = describe.build_profile(world.a['id'], min_gap_days=400)
    assert first != other and other['totals']['gaps'] == 0
    run_catalog_scan(world.expected['roots']['phone_a'], world.a['id'], temp_dir=world.temp)
    rescanned = describe.build_profile(world.a['id'])
    assert rescanned['scan_ids'] != first['scan_ids'] and rescanned['totals'] == first['totals']
