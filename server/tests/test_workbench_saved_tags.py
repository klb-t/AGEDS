"""Saved searches (counts only, never results) and bulk tagging with audit, rename and merge."""
from __future__ import annotations

import json

from server.app import db
from server.app.workbench import saved, tags
from server.app.workbench.common import WorkbenchConflict, WorkbenchInputError, WorkbenchNotFound
from server.app.search import SearchQueryError
from server.tests.test_workbench_support import WorkbenchCase


class SavedSearchTests(WorkbenchCase):
    def test_create_run_update_delete_keeps_counts_not_results(self):
        item = saved.create_saved_search('Umowa — zdarzenia', 'umowa OR umowy', ['event', 'transcript'], 50, 'notatka')
        self.assertEqual((item['layers'], item['limit'], item['last_run'], item['results_stored']), (['event', 'transcript'], 50, None, False))
        run = saved.run_saved_search(item['id'])
        self.assertEqual(run['counts']['returned'], len(run['page']['results']))
        self.assertEqual(run['counts']['facets'], run['page']['facets'])
        self.assertFalse(run['counts']['results_stored'])
        stored = saved.get_saved_search(item['id'])
        self.assertEqual(stored['last_run']['returned'], run['counts']['returned'])
        self.assertEqual(len(stored['runs']), 1)
        with db.session() as conn:
            raw = conn.execute('SELECT counts_json FROM workbench_saved_search_runs').fetchone()[0]
            with self.assertRaises(Exception):
                conn.execute('UPDATE workbench_saved_search_runs SET counts_json=?', ('{}',))
        self.assertNotIn('snippet', raw)
        self.assertNotIn('umowa podpisana', raw, 'result text is never stored')
        updated = saved.update_saved_search(item['id'], layers=[], query='umowa')
        self.assertEqual((updated['layers'], updated['query']), ([], 'umowa'))
        saved.run_saved_search(item['id'])
        self.assertEqual(len(saved.get_saved_search(item['id'])['runs']), 2)
        deleted = saved.delete_saved_search(item['id'])
        self.assertTrue(deleted['run_log_kept'])
        self.assertEqual(self.count('workbench_saved_search_runs'), 2)
        actions = [a['action'] for a in self.audit() if a['action'].startswith('workbench_saved_search')]
        self.assertEqual(actions, ['workbench_saved_search_create', 'workbench_saved_search_update', 'workbench_saved_search_delete'])

    def test_validation(self):
        saved.create_saved_search('A', 'umowa')
        with self.assertRaises(WorkbenchConflict):
            saved.create_saved_search('A', 'inne')
        with self.assertRaises(SearchQueryError):
            saved.create_saved_search('B', '"unterminated')
        with self.assertRaises(WorkbenchInputError):
            saved.create_saved_search('C', '   ')
        with self.assertRaises(WorkbenchInputError):
            saved.create_saved_search(' padded ', 'umowa')
        with self.assertRaises(SearchQueryError):
            saved.create_saved_search('D', 'umowa', ['bogus'])
        with self.assertRaises(WorkbenchNotFound):
            saved.run_saved_search(999)

    def test_http_contract(self):
        created = self.client.post('/api/workbench/saved-searches', json={'name': 'Łódź', 'query': 'lodzi', 'layers': ['ocr']})
        self.assertEqual(created.status_code, 201, created.text)
        sid = created.json()['id']
        self.assertEqual(self.client.post('/api/workbench/saved-searches', json={'name': 'Łódź', 'query': 'x'}).status_code, 409)
        self.assertEqual(self.client.patch(f'/api/workbench/saved-searches/{sid}', json={'bogus': 1}).status_code, 422)
        run = self.client.post(f'/api/workbench/saved-searches/{sid}/run').json()
        self.assertEqual(run['counts']['facets']['ocr'], 1)
        self.assertEqual(self.client.get('/api/workbench/saved-searches').json()['items'][0]['last_run']['returned'], 1)
        self.assertEqual(self.client.delete(f'/api/workbench/saved-searches/{sid}').status_code, 200)
        self.assertEqual(self.client.get(f'/api/workbench/saved-searches/{sid}').status_code, 404)


class TagTests(WorkbenchCase):
    def test_bulk_add_and_remove_are_audited_with_ids(self):
        before = self.evidence_digest()
        result = tags.apply_tag('ważne', [f'artifact:{self.artifact}', f'a:{self.artifact_twin}', f'event:{self.events[0]}',
                                          {'kind': 'event', 'id': self.events[1]}, f'artifact:{self.artifact}'])
        self.assertTrue(result['tag_created'])
        self.assertEqual(result['targets']['artifact']['changed'], [self.artifact, self.artifact_twin])
        self.assertEqual(result['targets']['event']['changed'], [self.events[0], self.events[1]])
        again = tags.apply_tag('ważne', [f'artifact:{self.artifact}', f'event:{self.events[2]}'])
        self.assertEqual(again['targets']['artifact'], {'changed': [], 'unchanged': [self.artifact]})
        self.assertEqual(again['targets']['event']['changed'], [self.events[2]])
        removed = tags.apply_tag('ważne', [f'event:{self.events[0]}', f'event:{self.events[3]}'], remove=True)
        self.assertEqual(removed['targets']['event'], {'changed': [self.events[0]], 'unchanged': [self.events[3]]})
        rows = self.audit('tag_bulk_add') + self.audit('tag_bulk_remove')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['details']['targets']['artifact']['changed'], [self.artifact, self.artifact_twin])
        self.assertEqual(self.count('artifact_tags'), 2)
        self.assertEqual(self.count('event_tags'), 2)
        self.assertEqual(self.evidence_digest(), before, 'tags never change evidence rows')

    def test_all_or_nothing_and_input_bounds(self):
        with self.assertRaises(WorkbenchNotFound):
            tags.apply_tag('x', [f'artifact:{self.artifact}', 'artifact:424242'])
        self.assertEqual(self.count('tags'), 0)
        self.assertEqual(self.count('artifact_tags'), 0)
        for bad in (['artifact:0'], ['document:1'], ['artifact:-1'], [], 'artifact:1', [{'kind': 'event', 'id': True}],
                    [f'artifact:{i}' for i in range(1, tags.MAX_BULK_TARGETS + 2)]):
            with self.subTest(bad=str(bad)[:40]):
                with self.assertRaises(WorkbenchInputError):
                    tags.apply_tag('x', bad)
        for name in ('', ' lead', 'trail ', 'a\nb', 'a\x07b', 'x' * 201):
            with self.subTest(name=name[:10]):
                with self.assertRaises(WorkbenchInputError):
                    tags.apply_tag(name, [f'artifact:{self.artifact}'])
        with self.assertRaises(WorkbenchNotFound):
            tags.apply_tag('never-created', [f'artifact:{self.artifact}'], remove=True)

    def test_rename_and_merge_are_explicit(self):
        a = tags.apply_tag('Ważne', [f'artifact:{self.artifact}', f'event:{self.events[0]}'])['tag_id']
        b = tags.apply_tag('wazne', [f'artifact:{self.artifact}', f'event:{self.events[1]}'])['tag_id']
        c = tags.apply_tag('WAŻNE', [f'event:{self.events[2]}'])['tag_id']
        listing = tags.list_tags()
        self.assertIn(sorted([a, b, c]), [sorted(g) for g in listing['possible_duplicates']])
        self.assertEqual(self.count('tags'), 3, 'similar names are suggestions, never merged automatically')
        with self.assertRaises(WorkbenchConflict):
            tags.rename_tag(b, 'Ważne')
        renamed = tags.rename_tag(c, 'pilne')
        self.assertEqual((renamed['old_name'], renamed['new_name']), ('WAŻNE', 'pilne'))
        merged = tags.merge_tags([b], a)
        self.assertEqual(merged['added_to_target'], {'artifact': [], 'event': [self.events[1]]})
        self.assertEqual(merged['sources_deleted'][0]['artifacts'], [self.artifact])
        with db.session() as conn:
            names = [r[0] for r in conn.execute('SELECT name FROM tags ORDER BY id')]
            members = conn.execute('SELECT count(*) FROM event_tags WHERE tag_id=?', (a,)).fetchone()[0]
        self.assertEqual(names, ['Ważne', 'pilne'])
        self.assertEqual(members, 2)
        with self.assertRaises(WorkbenchInputError):
            tags.merge_tags([a], a)
        with self.assertRaises(WorkbenchNotFound):
            tags.merge_tags([b], a)
        self.assertEqual(len(self.audit('tag_merge')), 1)

    def test_http_bulk_and_form_flow(self):
        response = self.client.post('/api/workbench/tags/apply', json={'tag': 'sprawa', 'targets': [f'event:{self.events[0]}'], 'action': 'add'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.post('/api/workbench/tags/apply', json={'tag': 'sprawa', 'targets': ['event:999999']}).status_code, 404)
        self.assertEqual(self.client.post('/api/workbench/tags/apply', json={'tag': 'sprawa', 'targets': [], 'action': 'x'}).status_code, 422)
        form = self.client.post('/workbench/tags/apply', data={'tag': 'formularz', 'target': [f'artifact:{self.artifact}', f'event:{self.events[1]}'],
                                                                'extra_targets': f'e:{self.events[2]}, artifact:{self.artifact_twin}',
                                                                'action': 'add', 'return_to': '/workbench?q=umowa'}, follow_redirects=False)
        self.assertEqual(form.status_code, 303)
        self.assertTrue(form.headers['location'].startswith('/workbench?q=umowa&done='))
        page = self.client.get(form.headers['location'])
        self.assertIn('formularz', page.text)
        details = self.audit('tag_bulk_add')[-1]['details']
        self.assertEqual(details['channel'], 'html_form')
        self.assertEqual(sorted(details['targets']['event']['changed']), sorted([self.events[1], self.events[2]]))
        for evil in ('//example.org/x', 'https://example.org/', '\\\\example.org', '/ok\r\nSet-Cookie: x=1'):
            with self.subTest(evil=evil):
                bad = self.client.post('/workbench/tags/apply', data={'tag': 'formularz', 'target': [f'event:{self.events[0]}'], 'return_to': evil},
                                       follow_redirects=False)
                self.assertTrue(bad.headers['location'].startswith('/workbench?'), bad.headers['location'])
        failed = self.client.post('/workbench/tags/apply', data={'tag': 'x', 'target': ['event:999999']}, follow_redirects=False)
        self.assertIn('wb_error=not_found', failed.headers['location'])
        self.assertIn('Nie znaleziono', self.client.get(failed.headers['location']).text)
        listing = self.client.get('/api/workbench/tags').json()
        self.assertEqual({t['name'] for t in listing['tags']}, {'sprawa', 'formularz'})
        self.assertTrue(all(t['epistemic_layer'] == 'human_annotation' for t in listing['tags']))

    def test_rename_merge_http(self):
        a = tags.apply_tag('a', [f'event:{self.events[0]}'])['tag_id']
        b = tags.apply_tag('b', [f'event:{self.events[1]}'])['tag_id']
        self.assertEqual(self.client.post(f'/api/workbench/tags/{a}/rename', json={'name': 'b'}).status_code, 409)
        self.assertEqual(self.client.post(f'/api/workbench/tags/{a}/rename', json={'name': 'a2'}).status_code, 200)
        merged = self.client.post('/api/workbench/tags/merge', json={'sources': [b], 'target': a})
        self.assertEqual(merged.status_code, 200, merged.text)
        self.assertEqual(json.loads(json.dumps(merged.json()))['target']['name'], 'a2')
        form = self.client.post('/workbench/tags/merge', data={'source': [str(a)], 'target': str(a)}, follow_redirects=False)
        self.assertIn('wb_error=invalid_input', form.headers['location'])


class CrossSiteWriteTests(WorkbenchCase):
    def test_cross_site_browser_writes_are_refused_reads_are_not(self):
        hostile = [{'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'https://attacker.example.com'}, {'Origin': 'null'}]
        for headers in hostile:
            with self.subTest(headers=headers):
                form = self.client.post('/workbench/tags/apply', data={'tag': 'x', 'target': [f'event:{self.events[0]}']},
                                        headers=headers, follow_redirects=False)
                self.assertEqual(form.status_code, 403)
                api = self.client.post('/api/workbench/folders', json={'name': 'x'}, headers=headers)
                self.assertEqual(api.status_code, 403)
                self.assertEqual(api.json()['detail']['code'], 'cross_site_write')
        self.assertEqual(self.count('tags'), 0)
        self.assertEqual(self.count('workbench_folders'), 0)
        same = self.client.post('/api/workbench/folders', json={'name': 'ok'},
                                headers={'Origin': 'http://testserver', 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(same.status_code, 201)
        self.assertEqual(self.client.get('/api/workbench/tags', headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 200)
