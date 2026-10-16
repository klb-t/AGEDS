"""Review findings #1 (stat scope of acquisitions; file_mtime only from the original path) and
#5 (child locators carry the container identity; additional parents are observed, not refused)."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.app import db as dbmod
from server.app import evidence, main
from server.app.api import imports_comm
from server.app.catalog import ingest as catalog_ingest
from server.app.catalog import scan as catalog_scan
from server.app.catalog import store as catalog_store
from server.app.timeline import ruleset
from server.app.timeline.projection import recording_candidates
from server.tests.corpus import chat_exports, communications
from server.tests.test_review_fixes_support import env, mbox_message, wav_bytes  # noqa: F401 (fixture)

OLD = datetime(2021, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
OLD_ISO = OLD.isoformat()


def _observations(env, since: int = 0) -> list[dict]:
    out = env.rows('SELECT * FROM source_observations WHERE id>? ORDER BY id', (since,))
    for row in out:
        row['meta'] = json.loads(row['metadata_json'])
    return out


def _file_mtime_candidates(artifact_id: int) -> list:
    rule = ruleset.load_rule(kind='display_rule')
    with dbmod.session() as db:
        return [c for c in recording_candidates(db, artifact_id, rule, {}) if c.kind == 'file_mtime']


def _segments(http) -> list[dict]:
    response = http.get('/api/timeline/window', params={'kind': 'transcript_segment'})
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------- finding 1

def test_finding1_upload_then_transcript_is_not_placed_at_upload_time(env):
    with env.client() as http:
        artifact = http.post('/upload', files={'file': ('call_with_bob.wav', wav_bytes(), 'audio/wav')}).json()['artifact_id']
        evidence.add_derived_text(artifact, 'transcript', 'hello there', model='fake', language='en',
                                  segments=[{'start': 0.0, 'end': 1.0, 'text': 'hello there'}])
        page = _segments(http)
    assert page['items'] == [] and page['undated_count'] == 1
    observation = _observations(env)[-1]
    assert observation['source_modified_at'] is None
    assert observation['meta']['filesystem_stat_scope'] == 'temporary_copy_not_original_source'
    assert observation['meta']['filesystem_stat'] is None
    assert _file_mtime_candidates(artifact) == []


def test_finding1_every_server_temporary_copy_records_no_source_mtime(env, monkeypatch):
    corpus = env.root / 'corpus'
    m = communications.build(corpus)
    chats = chat_exports.build(env.root / 'chats')
    sms_xml = b'<smses count="1"><sms date="1000" type="1" address="+31600000099" body="x"/></smses>'
    whatsapp_txt = '01/02/2024, 10:00 - Ann: hello\n'.encode()
    with env.client() as http:
        uploads = [
            http.post('/upload', files={'file': ('a.wav', wav_bytes(), 'audio/wav')}),
            http.post('/api/artifacts/upload', files={'file': ('b.wav', wav_bytes(2), 'audio/wav')}),
            http.post('/import/sms-backup', files={'file': ('sms.xml', sms_xml, 'text/xml')}),
            http.post('/import/whatsapp', files={'file': ('chat.txt', whatsapp_txt, 'text/plain')}),
            http.post('/api/imports/comm/email', files={'file': ('takeout.mbox', Path(m['mbox']['path']).read_bytes())}),
            http.post('/api/imports/comm/whatsapp-export', files={'file': ('export.zip', Path(m['whatsapp']['zip']).read_bytes())}),
            http.post('/api/imports/chatgpt', files=[('files', ('conversations.json',
                                                                Path(chats['chatgpt_exports']['e1'], 'conversations.json').read_bytes()))]),
        ]
    for response in uploads:
        assert response.status_code == 200, response.text[:300]
    observations = _observations(env)
    assert len(observations) > 10  # containers, extracted children, chat text, attachments
    for row in observations:
        assert row['source_modified_at'] is None, (row['source_locator'], row['meta'])
        assert row['meta']['filesystem_stat_scope'] == 'temporary_copy_not_original_source', row['source_locator']


def test_finding1_path_ingest_keeps_source_mtime_and_extracted_children_do_not(env, monkeypatch):
    corpus = env.root / 'corpus'
    m = communications.build(corpus)
    os.utime(m['mbox']['path'], (OLD.timestamp(), OLD.timestamp()))
    monkeypatch.setattr(imports_comm, 'settings', replace(env.settings, scan_roots=(corpus,)))
    with env.client() as http:
        response = http.post('/api/imports/comm/email/path',
                             json={'rootId': 0, 'relativePath': str(Path(m['mbox']['path']).relative_to(corpus))})
    assert response.status_code == 200, response.text
    container = response.json()['artifact_id']
    rows = _observations(env)
    by_artifact = {r['artifact_id']: r for r in rows}
    assert by_artifact[container]['source_modified_at'] == OLD_ISO
    assert by_artifact[container]['meta']['filesystem_stat_scope'] == 'source_path'
    children = [r for r in rows if r['artifact_id'] != container]
    assert children and all(r['source_modified_at'] is None for r in children)
    # A recording ingested from its original path keeps a usable file_mtime candidate.
    audio = env.root / 'rec.wav'
    audio.write_bytes(wav_bytes())
    os.utime(audio, (OLD.timestamp(), OLD.timestamp()))
    artifact = evidence.ingest_file(audio, source_id=evidence.ensure_source('synthetic', 'disk'), source_locator=str(audio))
    candidates = _file_mtime_candidates(artifact)
    assert [(c.usable, c.utc) for c in candidates] == [(True, '2021-03-04T05:06:07Z')]
    assert candidates[0].metadata['filesystem_stat_scope'] == 'source_path'


def test_finding1_catalog_ingest_declares_the_catalogued_mtime(env):
    source = env.root / 'catalog-src'
    source.mkdir()
    audio = source / 'rec.wav'
    audio.write_bytes(wav_bytes())
    os.utime(audio, (OLD.timestamp(), OLD.timestamp()))
    collection = catalog_store.create_collection('catalog mtime (synthetic)')
    scan = catalog_scan.run_catalog_scan(source, collection['id'], temp_dir=env.root / 'spool')
    entry = env.one('SELECT id FROM catalog_entries WHERE scan_id=? AND name=?', (scan['scan_id'], 'rec.wav'))
    result = catalog_ingest.ingest_entries([entry], temp_dir=env.root / 'spool')['results'][0]
    observation = _observations(env)[-1]
    assert observation['source_modified_at'] == OLD_ISO
    assert observation['meta']['filesystem_stat_scope'] == 'declared_by_caller'
    time_id = env.one("SELECT id FROM catalog_entry_times WHERE entry_id=? AND source='filesystem_stat'", (entry,))
    assert observation['meta']['source_modified_at_basis'] == f'catalog_entry_times:{time_id} (filesystem_stat, utc_epoch)'
    candidates = _file_mtime_candidates(result['artifact_id'])
    assert [(c.usable, c.utc) for c in candidates] == [(True, '2021-03-04T05:06:07Z')]
    assert candidates[0].metadata['source_modified_at_basis'].startswith('catalog_entry_times:')


def test_finding1_declared_scope_validation(env):
    path = env.root / 'x.bin'
    path.write_bytes(b'x')
    source = evidence.ensure_source('synthetic', 'declared')
    with pytest.raises(ValueError):
        evidence.ingest_file(path, source_id=source, source_stat='declared', declared_source_modified_at=OLD_ISO)
    with pytest.raises(ValueError):
        evidence.ingest_file(path, source_id=source, source_stat='declared', declared_time_basis='x',
                             declared_source_modified_at='2021-03-04T05:06:07')  # no offset: not an instant
    with pytest.raises(ValueError):
        evidence.ingest_file(path, source_id=source, source_stat='guess')
    assert env.one('SELECT COUNT(*) FROM source_observations') == 0


def _legacy_observation(env, artifact_id: int, locator: str, source_metadata: dict) -> None:
    """A row as written before stat scopes existed (no top-level filesystem_stat_scope)."""
    metadata = {'source_metadata': source_metadata,
                'filesystem_stat': {'mtime_ns': 1, 'ctime_ns': 1, 'size_bytes': 1}}
    with dbmod.session() as db:
        db.execute('''INSERT INTO source_observations(artifact_id,source_id,source_locator,observed_at,acquisition_kind,
                      original_name,size_bytes,sha256,source_modified_at,metadata_json)
                      SELECT id,source_id,?,?, 'ingest',original_name,size_bytes,sha256,?,? FROM artifacts WHERE id=?''',
                   (locator, OLD_ISO, '2026-10-01T12:00:00+00:00', json.dumps(metadata), artifact_id))


@pytest.mark.parametrize('locator,source_metadata,reason', [
    ('upload:call.wav', {}, 'file_mtime_of_temporary_upload'),
    ('client-upload:call.wav', {}, 'file_mtime_of_temporary_upload'),
    ('content://x', {'acquisition_kind': 'uploaded_stream'}, 'file_mtime_of_temporary_upload'),
    ('/data/takeout.mbox!/message/0/mime-part/2', {'importer': 'email_rfc5322'}, 'file_mtime_of_temporary_copy'),
    ('catalog:collection/1/rec.wav', {'catalog_entry_id': 3}, 'file_mtime_of_temporary_copy'),
    ('/data/export/IMG-1.jpg', {'extracted_from': {'message_index': 0}}, 'file_mtime_of_temporary_copy'),
    ('/data/recordings/call.wav', {}, None),
])
def test_finding1_legacy_rows_with_temporary_markers_are_ignored(env, locator, source_metadata, reason):
    path = env.root / 'rec.wav'
    path.write_bytes(wav_bytes())
    artifact = evidence.ingest_file(path, source_id=evidence.ensure_source('synthetic', 'legacy'),
                                    source_locator='synthetic:rec', source_stat='temporary_copy')
    _legacy_observation(env, artifact, locator, source_metadata)
    candidates = _file_mtime_candidates(artifact)
    assert len(candidates) == 1
    if reason is None:
        assert candidates[0].usable
        assert candidates[0].metadata['filesystem_stat_scope'] == 'legacy_unmarked_assumed_source_path'
    else:
        assert not candidates[0].usable and candidates[0].unusable_reason == reason


# --------------------------------------------------------------------- finding 5

ATTACHMENT = b'%PDF-1.4 same attachment bytes'
TAKEOUT_NAME = 'All mail Including Spam and Trash.mbox'


def _email_counts(env) -> dict:
    return {'events': env.one("SELECT COUNT(*) FROM events WHERE event_type='email'"),
            'artifacts': env.one('SELECT COUNT(*) FROM artifacts'),
            'references': env.one('SELECT COUNT(*) FROM event_media_references')}


def test_finding5_two_takeouts_with_the_same_mbox_name_both_import(env):
    export1 = mbox_message(1, 'hello', ATTACHMENT)
    export2 = mbox_message(1, 'hello', ATTACHMENT) + mbox_message(2, 'a newer message', ATTACHMENT)
    results = []
    with env.client() as http:
        for data in (export1, export2, export2):
            response = http.post('/api/imports/comm/email', files={'file': (TAKEOUT_NAME, data, 'application/mbox')})
            assert response.status_code == 200, response.text
            results.append(response.json())
            if len(results) == 2:
                before_reimport = _email_counts(env)
    first, second, again = results
    assert second['artifact_id'] != first['artifact_id'] and again['artifact_id'] == second['artifact_id']
    assert _email_counts(env) == before_reimport  # the same container again: idempotent
    assert [(r['artifact_id'], r['external_id']) for r in env.rows(
        "SELECT artifact_id, external_id FROM events WHERE event_type='email' ORDER BY id")] == [
        (first['artifact_id'], '<1@example.org>'), (second['artifact_id'], '<1@example.org>'),
        (second['artifact_id'], '<2@example.org>')]
    children = env.rows('SELECT a.id, a.parent_artifact_id, a.source_locator, a.sha256, c.sha256 AS container_sha '
                        'FROM artifacts a JOIN artifacts c ON c.id=a.parent_artifact_id ORDER BY a.id')
    assert len(children) == 3 and len({c['sha256'] for c in children}) == 1  # one stored copy, three provenances
    for child in children:
        assert child['source_locator'].startswith(f"upload:email:{TAKEOUT_NAME}@sha256:{child['container_sha']}!/message/")
    assert {r['parser_version'] for r in env.rows('SELECT parser_version FROM event_media_references')} == {'2'}


def test_finding5_reexported_whatsapp_zip_with_unchanged_media(env, tmp_path):
    import io
    import zipfile

    def export(lines: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('WhatsApp Chat with Example.txt', lines)
            archive.writestr('IMG-20240105-WA0001.jpg', b'unchanged synthetic photo')
        return buffer.getvalue()

    first_chat = '05/01/2024, 10:15 - Example A: IMG-20240105-WA0001.jpg (file attached)\n'
    with env.client() as http:
        for data in (export(first_chat), export(first_chat + '06/01/2024, 09:00 - Example B: later\n'),
                     export(first_chat) + b''):
            response = http.post('/api/imports/comm/whatsapp-export',
                                 files={'file': ('WhatsApp Chat with Example.zip', data, 'application/zip')})
            assert response.status_code == 200, response.text
    media = env.rows("SELECT parent_artifact_id, source_locator FROM artifacts WHERE original_name='IMG-20240105-WA0001.jpg' ORDER BY id")
    assert len(media) == 2 and media[0]['parent_artifact_id'] != media[1]['parent_artifact_id']
    assert all('@sha256:' in m['source_locator'] for m in media)


def test_finding5_same_child_under_another_parent_is_an_additional_observation(env):
    source = evidence.ensure_source('synthetic', 'parents')
    parent_a, parent_b, child = (env.root / name for name in ('a.zip', 'b.zip', 'child.bin'))
    parent_a.write_bytes(b'container a')
    parent_b.write_bytes(b'container b')
    child.write_bytes(b'child bytes')
    a = evidence.ingest_file(parent_a, source_id=source, source_locator='a')
    b = evidence.ingest_file(parent_b, source_id=source, source_locator='b')
    first = evidence.ingest_file(child, source_id=source, source_locator='legacy!/child.bin', parent_artifact_id=a)
    second = evidence.ingest_file(child, source_id=source, source_locator='legacy!/child.bin', parent_artifact_id=b)
    assert first == second
    assert env.one('SELECT parent_artifact_id FROM artifacts WHERE id=?', (first,)) == a  # never re-parented
    rows = _observations(env)
    stated = [(r['meta'].get('parent_artifact_id'), r['meta'].get('parent_relation')) for r in rows if r['artifact_id'] == first]
    assert stated == [(a, None), (b, 'additional_parent_observed')]
    audit = json.loads(env.rows("SELECT details_json FROM audit_log WHERE action='ingest' ORDER BY id DESC LIMIT 1")[0]['details_json'])
    assert audit['parent_relation'] == 'additional_parent_observed' and audit['artifact_parent_artifact_id'] == a


def test_finding5_container_imported_by_version_1_keeps_version_1(env, monkeypatch):
    from server.app.importers import email_mbox
    export = mbox_message(1, 'hello', ATTACHMENT)
    path = env.root / TAKEOUT_NAME
    path.write_bytes(export)
    # Simulate the database left by parser version 1 (name-based child locators).
    monkeypatch.setattr(email_mbox, 'PARSER_VERSION', '1')
    legacy = email_mbox.import_mbox(path)
    assert legacy['parser_version'] == '1'
    child = env.rows('SELECT source_locator FROM artifacts WHERE parent_artifact_id=?', (legacy['artifact_id'],))
    assert child == [{'source_locator': f'{path}!/message/0/mime-part/2'}]
    counts = _email_counts(env)
    monkeypatch.setattr(email_mbox, 'PARSER_VERSION', '2')
    again = email_mbox.import_mbox(path)
    assert again['parser_version'] == '1' and _email_counts(env) == counts
