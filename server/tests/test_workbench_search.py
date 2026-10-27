"""Layered search: separate index, epistemic labels, facets, filters, limits and adversarial queries."""
from __future__ import annotations

from html.parser import HTMLParser
from unittest.mock import patch

from server.app import db, evidence
from server.app.schema import extension_migrations
from server.app.schema import workbench as workbench_schema
from server.app.search import SearchQueryError, SearchWorkLimitError
from server.app.workbench import index as wb_index
from server.app.workbench import layered_search
from server.app.workbench.layered_search import layered_search_page
from server.tests.test_workbench_support import SOURCE_MARKUP, WorkbenchCase


def hits(page, kind=None):
    return [(h['object_kind'], h['object_id']) for h in page['results'] if kind is None or h['object_kind'] == kind]


class LayeredSearchTests(WorkbenchCase):
    def test_every_layer_is_indexed_and_labelled(self):
        page = layered_search_page('umowa OR umowy OR faktura OR spotkanie OR nagranie')
        layers = {h['layer']: h['epistemic_layer'] for h in page['results']}
        self.assertEqual(layers['event'], 'parser_output')
        self.assertEqual(layers['transcript'], 'model_output')
        self.assertEqual(layers['ocr'], 'model_output')
        self.assertEqual(layers['derived_other'], 'unclassified')
        self.assertEqual(layers['annotation'], 'human_annotation')
        self.assertEqual(layers['artifact_metadata'], 'raw_source_field')
        self.assertTrue(page['complete'])
        self.assertEqual(sum(page['facets'].values()), len(page['results']))
        legend = {row['layer']: row['epistemic_layer'] for row in page['epistemic_legend']}
        self.assertEqual(legend['annotation'], 'human_annotation')

    def test_run_provenance_is_reported_not_invented(self):
        page = layered_search_page('wersja OR Zażółć', ['transcript'])
        by_id = {h['object_id']: h['provenance'] for h in page['results']}
        self.assertEqual(by_id[self.transcript_legacy]['provenance_status'], 'legacy_unknown')
        self.assertIsNone(by_id[self.transcript_legacy]['run_id'])
        self.assertEqual(by_id[self.transcript_run]['provenance_status'], 'recorded_processing_run')
        self.assertEqual(by_id[self.transcript_run]['run_id'], self.run_id)
        self.assertEqual(by_id[self.transcript_run]['run_tool'], 'synthetic-asr')

    def test_layer_filter_limits_results_but_facets_cover_all_layers(self):
        page = layered_search_page('umowa', ['annotation'])
        self.assertEqual(page['results'], [])
        self.assertTrue(page['layer_filter'])
        self.assertGreaterEqual(page['facets']['event'], 1)
        only_events = layered_search_page('umowa', ['event'])
        self.assertEqual({h['layer'] for h in only_events['results']}, {'event'})
        multi = layered_search_page('umowa OR umowy', ['event', 'derived_other'])
        self.assertEqual({h['layer'] for h in multi['results']}, {'event', 'derived_other'})
        self.assertEqual(layered_search_page('umowa', ['event', 'event'])['layers'], ['event'])

    def test_polish_diacritics_including_l_stroke(self):
        for query in ('zazolc', 'ZAŻÓŁĆ', 'gesla', 'lodzi', 'Łodzi', 'lukasz', 'łukasz'):
            with self.subTest(query=query):
                self.assertTrue(layered_search_page(query)['results'], query)
        annotation = layered_search_page('lukasz', ['annotation'])
        self.assertEqual(hits(annotation), [('annotation', self.annotation)])
        name = layered_search_page('lodz', ['artifact_metadata'])
        self.assertIn(('artifact', self.artifact), hits(name))
        # The legacy index does not fold ł; the workbench index does, in the index only.
        with db.session() as conn:
            stored = conn.execute('SELECT label, body FROM annotations WHERE id=?', (self.annotation,)).fetchone()
        self.assertEqual(tuple(stored), ('Ważne', 'Łukasz potwierdził spotkanie'))

    def test_adversarial_queries_are_typed_errors_or_literal_matches(self):
        invalid = ['"unterminated', 'folded:lodz', 'layer:event', 'unknowncolumn:x', "'; DROP TABLE artifacts; --",
                   'x OR', 'NEAR(', '(((((((((((((((((a)))))))))))))))))', 'a' * 5000, 'x\x00y', '\ud800']
        for query in invalid:
            with self.subTest(query=query[:30]):
                with self.assertRaises(SearchQueryError):
                    layered_search_page(query)
                if query != '\ud800':  # an HTTP client cannot encode a lone surrogate
                    response = self.client.get('/api/workbench/search', params={'q': query})
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()['detail']['code'], 'invalid_input')
        for query in ('title:nagranie', 'content:umowa', '{title content}:umowa', 'umowa*', '"umowa podpisana"', 'NEAR(umowa podpisana, 2)',
                      'umowa NOT faktura', '^umowa', 'object_kind:event'):
            with self.subTest(query=query):
                layered_search_page(query)
        self.assertEqual(self.count('artifacts'), 3)
        with self.assertRaises(SearchQueryError):
            layered_search_page('umowa', ['nonexistent'])
        with self.assertRaises(SearchQueryError):
            layered_search_page('umowa', 'event')
        for limit in (0, 201, True, 1.5):
            with self.assertRaises(SearchQueryError):
                layered_search_page('umowa', None, limit)

    def test_limits_has_more_and_complete(self):
        source = evidence.ensure_source('synthetic', 'bulk')
        for index in range(30):
            evidence.add_event(source_id=source, artifact_id=None, event_type='sms', body=f'needle {index}', external_id=f'n-{index}')
        page = layered_search_page('needle', None, 30)
        self.assertEqual((len(page['results']), page['has_more'], page['complete']), (30, False, True))
        page = layered_search_page('needle', None, 29)
        self.assertEqual((len(page['results']), page['has_more'], page['complete']), (29, True, False))
        self.assertEqual(page['facets']['event'], 30)
        with patch.object(layered_search, 'FACET_SCAN_LIMIT', 10):
            partial = layered_search_page('needle', None, 5)
        self.assertFalse(partial['facets_complete'])
        self.assertEqual(partial['facets']['event'], 10)
        blank = layered_search_page('   ')
        self.assertEqual((blank['results'], blank['complete']), ([], True))

    def test_work_budget_exhaustion_returns_no_partial_result(self):
        with patch.object(layered_search, 'MAX_VM_STEPS', 1):
            with self.assertRaises(SearchWorkLimitError):
                layered_search_page('umowa OR spotkanie OR nagranie')
            response = self.client.get('/api/workbench/search', params={'q': 'umowa OR spotkanie'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'work_limit')
        self.assertTrue(layered_search_page('umowa')['results'], 'a fresh connection works after interruption')

    def test_snippets_are_plain_source_text(self):
        page = layered_search_page('Zażółć', ['transcript'])
        self.assertIn(SOURCE_MARKUP, page['results'][0]['snippet'])
        html = self.client.get('/workbench', params={'q': 'Zażółć OR biuro'})
        self.assertEqual(html.status_code, 200)

        class Tags(HTMLParser):
            found = []

            def handle_starttag(self, tag, attrs):
                self.found.append((tag, dict(attrs)))
        parser = Tags()
        parser.feed(html.text)
        self.assertFalse([t for t, a in parser.found if t in ('img', 'svg', 'mark') or any(k.startswith('on') for k in a)])
        scripts = [a for t, a in parser.found if t == 'script']
        self.assertTrue(all(a.get('src', '').startswith('/static/') or a.get('id') == 'ageds-shell-config' for a in scripts))
        self.assertIn('&lt;script&gt;x&lt;/script&gt;', html.text)

    def test_annotations_are_indexed_on_insert_update_delete(self):
        with db.session() as conn:
            conn.execute("UPDATE annotations SET body='zmieniona notatka', updated_at=CURRENT_TIMESTAMP WHERE id=?", (self.annotation,))
        self.assertFalse(layered_search_page('lukasz', ['annotation'])['results'])
        self.assertEqual(hits(layered_search_page('zmieniona', ['annotation'])), [('annotation', self.annotation)])
        with db.session() as conn:
            conn.execute('DELETE FROM annotations WHERE id=?', (self.annotation,))
        self.assertFalse(layered_search_page('zmieniona')['results'])
        self.assertTrue(wb_index.index_status()['consistent'])

    def test_legacy_api_search_contract_is_unchanged(self):
        response = self.client.get('/api/search', params={'q': 'lukasz OR Łukasz'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [], 'annotations are not added to the legacy index')
        self.assertEqual(response.headers['x-ageds-search-complete'], 'true')
        legacy = self.client.get('/api/search', params={'q': 'umowa'}).json()
        self.assertTrue(all(set(item) == {'object_kind', 'object_id', 'title', 'snippet', 'rank'} for item in legacy))
        self.assertEqual(self.count('search_fts', "object_kind='annotation'"), 0)

    def test_http_endpoint_shape(self):
        response = self.client.get('/api/workbench/search', params=[('q', 'umowa'), ('layer', 'event'), ('layer', 'transcript'), ('limit', '5')])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in ('query', 'layers', 'results', 'limit', 'has_more', 'complete', 'facets', 'facets_complete', 'epistemic_legend'):
            self.assertIn(key, body)
        self.assertEqual(body['layers'], ['event', 'transcript'])
        self.assertEqual(self.client.get('/api/workbench/search', params={'q': 'umowa', 'layer': 'bogus'}).status_code, 422)
        self.assertEqual(self.client.get('/api/workbench/search', params={'q': 'umowa', 'limit': 201}).status_code, 422)
        self.assertEqual(self.client.get('/api/workbench/layers').json()['derived_kind_layers']['ocr'], 'ocr')


class IndexMigrationTests(WorkbenchCase):
    def test_migrations_are_registered_idempotent_and_in_range(self):
        versions = [v for v, _, _ in extension_migrations() if 80 <= v <= 89]
        self.assertEqual(versions, [80, 81, 82, 83])
        db.init_db()
        db.init_db()
        with db.session() as conn:
            applied = [r[0] for r in conn.execute('SELECT version FROM schema_migrations WHERE version BETWEEN 80 AND 89 ORDER BY version')]
        self.assertEqual(applied, [80, 81, 82, 83])
        self.assertTrue(wb_index.index_status()['consistent'])

    def test_backfill_indexes_rows_that_existed_before_the_migration(self):
        with db.session() as conn:
            conn.execute('DROP TRIGGER workbench_index_annotation_ai')
            conn.execute("INSERT INTO annotations(artifact_id, kind, body) VALUES (?, 'note', 'przedmigracyjna notatka')", (self.artifact,))
            conn.execute('DELETE FROM workbench_search_docs')
            conn.execute("INSERT INTO workbench_fts(workbench_fts) VALUES ('rebuild')")
            conn.execute('DELETE FROM schema_migrations WHERE version=80')
            for name in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'workbench_index_%'")]:
                conn.execute(f'DROP TRIGGER {name}')
        db.init_db()
        page = layered_search_page('przedmigracyjna', ['annotation'])
        self.assertEqual(len(page['results']), 1)
        self.assertTrue(wb_index.index_status()['consistent'])

    def test_index_check_detects_drift_and_rebuild_repairs_only_the_index(self):
        before = self.evidence_digest()
        with db.session() as conn:
            conn.execute("DELETE FROM workbench_search_docs WHERE object_kind='event'")
            conn.execute("UPDATE workbench_search_docs SET content='tampered' WHERE object_kind='annotation'")
        status = wb_index.index_status()
        self.assertFalse(status['consistent'])
        self.assertEqual(status['by_kind']['event']['missing'], 4)
        self.assertEqual(status['by_kind']['annotation']['stale'], 1)
        result = wb_index.rebuild_index(channel='test')
        self.assertTrue(result['after']['consistent'])
        self.assertEqual(self.evidence_digest(), before)
        self.assertEqual(len(self.audit('workbench_index_rebuild')), 1)

    def test_index_triggers_never_block_evidence_writes(self):
        with db.session() as conn:
            conn.execute("INSERT INTO workbench_search_docs(object_kind, object_id, title, content) VALUES ('event', 999, 'stale', 'stale')")
            conn.execute("INSERT INTO events(id, source_id, event_type, body) VALUES (999, ?, 'sms', 'nowe zdarzenie')", (self.source,))
        self.assertEqual(hits(layered_search_page('nowe', ['event'])), [('event', 999)])
        self.assertTrue(wb_index.index_status()['consistent'])

    def test_released_migration_sql_is_pinned(self):
        # Guard against editing a released migration in place; add a new version instead.
        import hashlib
        digests = {v: hashlib.sha256(sql.encode('utf-8')).hexdigest()[:16] for v, _, sql in workbench_schema.MIGRATIONS}
        self.assertEqual(sorted(digests), [80, 81, 82, 83])
        self.assertEqual(digests, PINNED_MIGRATION_DIGESTS)


PINNED_MIGRATION_DIGESTS = {80: 'af0ecc5f831f071e', 81: '7ed271a5d3b894ed', 82: 'f2ad959da990f97f', 83: '434ff204efc81d21'}
