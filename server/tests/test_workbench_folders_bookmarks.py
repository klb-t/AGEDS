"""Working folders (references with pinned identity, never evidence changes) and timeline bookmarks."""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from server.app import db
from server.app.workbench import bookmarks, folders
from server.app.workbench.common import WorkbenchConflict, WorkbenchInputError, WorkbenchNotFound
from server.tests.test_workbench_support import WorkbenchCase

REPO_ROOT = Path(__file__).resolve().parents[2]


class FolderTests(WorkbenchCase):
    def make(self):
        root = folders.create_folder('Sprawa 1')
        child = folders.create_folder('Dowody', root['id'], 'notatka teczki')
        return root, child

    def test_nested_folders_names_cycles_and_depth(self):
        root, child = self.make()
        self.assertEqual([p['name'] for p in child['path']], ['Sprawa 1', 'Dowody'])
        with self.assertRaises(WorkbenchConflict):
            folders.create_folder('Dowody', root['id'])
        folders.create_folder('Dowody')  # same name on another level is fine
        with self.assertRaises(WorkbenchConflict):
            folders.update_folder(root['id'], parent_id=child['id'])
        with self.assertRaises(WorkbenchConflict):
            folders.update_folder(root['id'], parent_id=root['id'])
        with self.assertRaises(WorkbenchNotFound):
            folders.create_folder('x', 424242)
        parent = child['id']
        for level in range(folders.MAX_FOLDER_DEPTH - 2):
            parent = folders.create_folder(f'L{level}', parent)['id']
        with self.assertRaises(WorkbenchInputError):
            folders.create_folder('too deep', parent)
        tree = folders.list_folders()
        self.assertEqual(tree[0]['name'], 'Sprawa 1')
        self.assertEqual(max(f['depth'] for f in tree), folders.MAX_FOLDER_DEPTH - 1)

    def test_items_pin_identity_and_report_status_without_changing_evidence(self):
        before = self.evidence_digest()
        _, folder = self.make()
        fid = folder['id']
        added = {kind: folders.add_item(fid, kind, ident, note=f'n-{kind}') for kind, ident in [
            ('artifact', self.artifact), ('event', self.events[0]), ('citation', self.anchor['id']),
            ('text_version', self.transcript_legacy), ('annotation', self.annotation)]}
        self.assertEqual(added['citation']['pinned_version_id'], self.transcript_legacy)
        self.assertEqual(added['text_version']['pinned_version_id'], self.transcript_legacy)
        self.assertEqual(added['annotation']['pinned_version_id'], self.transcript_legacy)
        self.assertRegex(added['artifact']['pinned']['sha256'], '^[0-9a-f]{64}$')
        self.assertEqual(added['citation']['locator'], f'ageds:citation/{self.anchor["id"]}@derived_text/{self.transcript_legacy}')
        self.assertEqual(self.evidence_digest(), before, 'adding references never changes evidence')
        with self.assertRaises(WorkbenchConflict):
            folders.add_item(fid, 'artifact', self.artifact)
        with self.assertRaises(WorkbenchNotFound):
            folders.add_item(fid, 'event', 424242)
        with self.assertRaises(WorkbenchInputError):
            folders.add_item(fid, 'source', 1)
        # A human edit of the annotation is surfaced as 'changed'; a deletion as 'missing'.
        with db.session() as conn:
            conn.execute("UPDATE annotations SET body='poprawiona', updated_at='2026-10-16T00:00:00' WHERE id=?", (self.annotation,))
        state = {i['item_kind']: i for i in folders.get_folder(fid)['items']}
        self.assertEqual(state['annotation']['status'], 'changed')
        self.assertIn('body_sha256', state['annotation']['changed_keys'])
        self.assertEqual(state['artifact']['status'], 'unchanged')
        with db.session() as conn:
            conn.execute('DELETE FROM annotations WHERE id=?', (self.annotation,))
        folder_state = folders.get_folder(fid)
        self.assertEqual(folder_state['status_counts'], {'unchanged': 4, 'changed': 0, 'missing': 1})

    def test_order_notes_remove_and_recursive_delete_are_audited(self):
        root, child = self.make()
        fid = child['id']
        ids = [folders.add_item(fid, 'event', e)['id'] for e in self.events]
        inserted = folders.add_item(fid, 'artifact', self.artifact, position=0)
        self.assertEqual([i['id'] for i in folders.get_folder(fid)['items']], [inserted['id'], *ids])
        reordered = folders.reorder_items(fid, list(reversed([inserted['id'], *ids])))
        self.assertEqual([i['position'] for i in reordered['items']], [0, 1, 2, 3, 4])
        with self.assertRaises(WorkbenchInputError):
            folders.reorder_items(fid, ids)
        moved = folders.move_item(fid, ids[0], -1)
        self.assertEqual(moved['items'][2]['id'], ids[0])
        folders.update_item(fid, ids[1], note='ważny SMS')
        self.assertEqual([i['note'] for i in folders.get_folder(fid)['items'] if i['id'] == ids[1]], ['ważny SMS'])
        removed = folders.remove_item(fid, ids[2])
        self.assertFalse(removed['evidence_changed'])
        with self.assertRaises(WorkbenchConflict):
            folders.delete_folder(root['id'])
        result = folders.delete_folder(root['id'], recursive=True)
        self.assertEqual(sorted(result['deleted_folders']), sorted([root['id'], child['id']]))
        self.assertEqual(result['references_removed'], 4)
        audit = self.audit('workbench_folder_delete')[-1]['details']
        self.assertEqual(len(audit['references_removed']), 4)
        self.assertFalse(audit['evidence_changed'])
        self.assertEqual(self.count('events'), 4)

    def test_export_json_and_csv_references_only(self):
        _, folder = self.make()
        fid = folder['id']
        folders.add_item(fid, 'citation', self.anchor['id'], note='=HYPERLINK("http://example.org")')
        folders.add_item(fid, 'event', self.events[1], note='-2+3')
        sub = folders.create_folder('Pod', fid)
        folders.add_item(sub['id'], 'artifact', self.artifact)
        payload, media = folders.export_folder(fid, 'json')
        document = json.loads(payload)
        self.assertEqual(media, 'application/json')
        self.assertEqual(document['schema'], 'ageds.workbench-folder-references/v1')
        self.assertEqual(document['scope'], {'references_only': True, 'source_bytes_included': False, 'text_included': False,
                                             'membership_is_evidence': False, 'locators': 'inert', 'status_computed_at_export': True})
        self.assertEqual(document['counts']['items'], 2)
        self.assertEqual(document['items'][0]['note'], '=HYPERLINK("http://example.org")', 'JSON keeps notes exactly')
        self.assertNotIn('biuro', payload.decode('utf-8'), 'no source text is exported')
        recursive = json.loads(folders.export_folder(fid, 'json', recursive=True)[0])
        self.assertEqual(recursive['counts']['items'], 3)
        self.assertEqual(recursive['items'][-1]['folder_path'], 'Sprawa 1 / Dowody / Pod')
        text, media = folders.export_folder(fid, 'csv')
        rows = list(csv.DictReader(io.StringIO(text.decode('utf-8'))))
        self.assertEqual(media, 'text/csv; charset=utf-8')
        self.assertEqual(rows[0]['note'], '\'=HYPERLINK("http://example.org")')
        self.assertEqual(rows[1]['note'], "'-2+3")
        response = self.client.get(f'/api/workbench/folders/{fid}/export', params={'format': 'csv'})
        self.assertEqual(response.headers['content-disposition'], f'attachment; filename="ageds-folder-{fid}.csv"')
        self.assertEqual(self.client.get(f'/api/workbench/folders/{fid}/export', params={'format': 'xml'}).status_code, 422)

    def test_http_and_form_flows(self):
        created = self.client.post('/api/workbench/folders', json={'name': 'Teczka API'})
        self.assertEqual(created.status_code, 201)
        fid = created.json()['id']
        self.assertEqual(self.client.post(f'/api/workbench/folders/{fid}/items', json={'kind': 'event', 'id': self.events[0]}).status_code, 201)
        self.assertEqual(self.client.post(f'/api/workbench/folders/{fid}/items', json={'kind': 'event', 'id': 'x'}).status_code, 422)
        entry = self.client.get(f'/api/workbench/folders/{fid}').json()['items'][0]['id']
        self.assertEqual(self.client.patch(f'/api/workbench/folders/{fid}/items/{entry}', json={'item_id': 5}).status_code, 422)
        self.assertEqual(self.client.patch(f'/api/workbench/folders/{fid}/items/{entry}', json={'note': 'ok'}).json()['note'], 'ok')
        form = self.client.post('/workbench/folders', data={'name': 'Teczka formularz'}, follow_redirects=False)
        self.assertEqual(form.status_code, 303)
        form_id = int(form.headers['location'].rsplit('/', 1)[1])
        add = self.client.post(f'/workbench/folders/{form_id}/items', data={'kind': 'artifact', 'item_id': f'#{self.artifact}'}, follow_redirects=False)
        self.assertEqual(add.status_code, 303)
        page = self.client.get(f'/workbench/folders/{form_id}')
        self.assertIn(f'ageds:artifact/{self.artifact}', page.text)
        anywhere = self.client.post('/workbench/folder-items', data={'folder_id': str(form_id), 'kind': 'event', 'item_id': str(self.events[1]),
                                                                       'return_to': f'/artifact/{self.artifact}'}, follow_redirects=False)
        self.assertTrue(anywhere.headers['location'].startswith(f'/artifact/{self.artifact}'))
        item = folders.get_folder(form_id)['items'][0]['id']
        removed = self.client.post(f'/workbench/folders/{form_id}/items/{item}/remove', follow_redirects=False)
        self.assertIn('done=', removed.headers['location'])
        self.assertEqual(len(folders.get_folder(form_id)['items']), 1)
        self.assertEqual(self.client.get('/workbench/folders/424242').status_code, 404)


class FolderCliTests(WorkbenchCase):
    def command(self, *args):
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONDONTWRITEBYTECODE='1', EW_DATA_DIR=str(self.root),
                   EW_STORE_DIR=str(self.root / 'store'), EW_DB_PATH=str(self.settings.db_path), EW_REQUIRE_TOKEN='',
                   EW_SCAN_ROOTS=str(self.root / 'sources'))
        return subprocess.run([sys.executable, '-m', 'server.app.cli', *map(str, args)], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_export_writes_new_file_outside_sources_and_store(self):
        folder = folders.create_folder('CLI')
        folders.add_item(folder['id'], 'citation', self.anchor['id'])
        (self.root / 'sources').mkdir()
        output = self.root / 'out' / 'teczka.csv'
        process = self.command('workbench-folder-export', folder['id'], output)
        self.assertEqual(process.returncode, 0, process.stderr)
        status = json.loads(process.stdout)
        self.assertEqual((status['format'], status['references_only'], status['source_bytes_written']), ('csv', True, False))
        self.assertTrue(output.read_text(encoding='utf-8').startswith('folder_id,folder_path'))
        self.assertEqual(self.command('workbench-folder-export', folder['id'], output).returncode, 2, 'never overwrite')
        inside = self.command('workbench-folder-export', folder['id'], self.root / 'sources' / 'x.json')
        self.assertEqual(inside.returncode, 2)
        self.assertIn('source root', inside.stderr)
        self.assertFalse((self.root / 'sources' / 'x.json').exists())
        store = self.command('workbench-folder-export', folder['id'], self.root / 'store' / 'x.json')
        self.assertEqual(store.returncode, 2)
        missing = self.command('workbench-folder-export', 424242, self.root / 'out' / 'missing.json')
        self.assertEqual(missing.returncode, 2)

    def test_search_tag_index_and_reports(self):
        search = json.loads(self.command('workbench-search', 'lodzi', '--layer', 'ocr').stdout)
        self.assertEqual([h['layer'] for h in search['results']], ['ocr'])
        tagged = json.loads(self.command('workbench-tag', '--tag', 'cli', f'artifact:{self.artifact}', f'e:{self.events[0]}').stdout)
        self.assertEqual(tagged['changed_count'], 2)
        self.assertEqual(self.command('workbench-tag', '--tag', 'cli', 'artifact:424242').returncode, 2)
        check = self.command('workbench-index-check')
        self.assertEqual(check.returncode, 0, check.stdout)
        self.assertTrue(json.loads(check.stdout)['consistent'])
        theme = json.loads(self.command('workbench-theme-report').stdout)
        self.assertTrue(all(t['all_aa'] for t in theme['themes'].values()))
        self.assertEqual(self.command('workbench-i18n-check').returncode, 0)
        code = 'import server.app.cli,sys,json; print(json.dumps({n:n in sys.modules for n in ["server.app.config","server.app.db","server.app.workbench.folders"]}))'
        isolated = subprocess.run([sys.executable, '-c', code], cwd=self.root, env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
                                  capture_output=True, text=True, timeout=30)
        self.assertFalse(json.loads(isolated.stdout)['server.app.workbench.folders'])


class BookmarkTests(WorkbenchCase):
    def test_time_semantics_window_and_unzoned(self):
        zoned = bookmarks.create_bookmark('2026-03-01T08:00:00+01:00', '2026-03-01T09:30:00+01:00', label='Spotkanie',
                                          ref={'kind': 'event', 'id': self.events[0]}, view={'zoom': 'hour'})
        self.assertEqual((zoned['start']['utc'], zoned['tz_status']), ('2026-03-01T07:00:00Z', 'explicit_offset'))
        point = bookmarks.create_bookmark('2026-03-02T00:00:00.500Z', note='punkt')
        local = bookmarks.create_bookmark('2026-03-01 10:15', label='WhatsApp lokalnie')
        self.assertEqual((local['tz_status'], local['start']['utc']), ('unknown', None))
        window = bookmarks.list_bookmarks(from_='2026-03-01T07:30:00Z', to='2026-03-01T23:00:00Z')
        self.assertEqual([b['id'] for b in window['items']], [zoned['id']], 'overlap counts, not only start')
        self.assertEqual(window['unzoned_excluded'], 1)
        everything = bookmarks.list_bookmarks()
        self.assertEqual([b['id'] for b in everything['items']], [zoned['id'], point['id'], local['id']])
        with self.assertRaises(WorkbenchInputError):
            bookmarks.list_bookmarks(from_='2026-03-01', include_unzoned=True)
        with self.assertRaises(WorkbenchInputError):
            bookmarks.list_bookmarks(from_='2026-03-01 00:00')
        self.assertEqual(bookmarks.list_bookmarks(ref_kind='event', ref_id=self.events[0])['items'][0]['id'], zoned['id'])
        self.assertTrue(bookmarks.list_bookmarks(limit=1)['has_more'])

    def test_validation_and_references(self):
        for start, end in (('not a time', None), ('2026-03-01T10:00:00Z', '2026-03-01T09:00:00Z'),
                           ('2026-03-01T10:00:00Z', '2026-03-01 11:00'), ('2026-13-01T00:00:00Z', None), ('x' * 65, None)):
            with self.subTest(start=start[:20], end=end):
                with self.assertRaises(WorkbenchInputError):
                    bookmarks.create_bookmark(start, end)
        segment = bookmarks.create_bookmark('2026-03-01T08:00:00Z', ref={'kind': 'transcript_segment', 'id': self.artifact,
                                                                         'version_id': self.transcript_legacy, 'segment_index': 2})
        self.assertTrue(segment['ref']['exists'])
        with self.assertRaises(WorkbenchInputError):
            bookmarks.create_bookmark('2026-03-01T08:00:00Z', ref={'kind': 'transcript_segment', 'id': self.artifact,
                                                                   'version_id': self.transcript_legacy, 'segment_index': 3})
        with self.assertRaises(WorkbenchNotFound):
            bookmarks.create_bookmark('2026-03-01T08:00:00Z', ref={'kind': 'transcript_segment', 'id': self.artifact_twin,
                                                                   'version_id': self.transcript_legacy, 'segment_index': 0})
        with self.assertRaises(WorkbenchNotFound):
            bookmarks.create_bookmark('2026-03-01T08:00:00Z', ref={'kind': 'citation', 'id': 424242})
        with self.assertRaises(WorkbenchInputError):
            bookmarks.create_bookmark('2026-03-01T08:00:00Z', ref={'kind': 'event', 'id': self.events[0], 'segment_index': 1})
        with self.assertRaises(WorkbenchInputError):
            bookmarks.create_bookmark('2026-03-01T08:00:00Z', view={'nan': float('nan')})
        with self.assertRaises(WorkbenchInputError):
            bookmarks.create_bookmark('2026-03-01T08:00:00Z', view={'big': 'x' * 20_000})

    def test_http_contract_for_the_timeline(self):
        created = self.client.post('/api/workbench/bookmarks', json={'start': '2026-03-01T08:00:00+00:00', 'label': 'A',
                                                                    'ref': {'kind': 'event', 'id': self.events[0]}})
        self.assertEqual(created.status_code, 201, created.text)
        bid = created.json()['id']
        self.assertEqual(created.json()['epistemic_layer'], 'human_annotation')
        self.assertEqual(self.client.post('/api/workbench/bookmarks', json={'start': 'x', 'bogus': 1}).status_code, 422)
        listing = self.client.get('/api/workbench/bookmarks', params={'from': '2026-03-01T00:00:00Z', 'to': '2026-03-02T00:00:00Z'}).json()
        self.assertEqual([b['id'] for b in listing['items']], [bid])
        patched = self.client.patch(f'/api/workbench/bookmarks/{bid}', json={'note': 'po rozmowie', 'end': '2026-03-01T09:00:00+00:00'})
        self.assertEqual(patched.json()['end']['utc'], '2026-03-01T09:00:00Z')
        self.assertEqual(self.client.delete(f'/api/workbench/bookmarks/{bid}').status_code, 200)
        self.assertEqual(self.client.get(f'/api/workbench/bookmarks/{bid}').status_code, 404)
        audit = self.audit('workbench_bookmark_delete')[-1]['details']['deleted']
        self.assertEqual(audit['note'], 'po rozmowie')
        form = self.client.post('/workbench/bookmarks', data={'start': '2026-03-01T08:00:00Z', 'label': 'formularz', 'ref_kind': 'transcript_segment',
                                                               'ref_id': str(self.artifact), 'ref_version_id': str(self.transcript_legacy),
                                                               'ref_segment_index': '1'}, follow_redirects=False)
        self.assertEqual(form.status_code, 303)
        self.assertIn('formularz', self.client.get('/workbench/bookmarks').text)
