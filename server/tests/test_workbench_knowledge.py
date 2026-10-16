"""Computed knowledge summaries, quick jump and copy-citation text."""
from __future__ import annotations

from server.app import db
from server.app.workbench import knowledge
from server.app.workbench.common import WorkbenchInputError, WorkbenchNotFound
from server.tests.test_workbench_support import WorkbenchCase


class KnowledgeTests(WorkbenchCase):
    def test_summary_states_recorded_unknown_and_not_claimed(self):
        before = self.evidence_digest()
        summary = knowledge.artifact_knowledge(self.artifact)
        recorded = {item['key']: item for item in summary['recorded']}
        self.assertEqual(recorded['content_identity']['value']['sha256'], summary['artifact']['sha256'])
        self.assertEqual(recorded['acquisitions']['value']['observations'], 1)
        self.assertEqual([a['id'] for a in recorded['same_content_elsewhere']['value']['artifacts']], [self.artifact_twin])
        transcripts = next(d for d in recorded['derived_text']['value'] if d['kind'] == 'transcript')
        self.assertEqual((transcripts['versions'], transcripts['without_recorded_run'], transcripts['epistemic_layer']), (2, 1, 'model_output'))
        unknown = {(u['key'], u['reason']) for u in summary['not_recorded_or_unknown']}
        self.assertIn(('derived_text.transcript.run', 'legacy_unknown'), unknown)
        self.assertIn(('processing_runs.model_version', 'recorded_as_unknown'), unknown)
        self.assertIn(('source_created_at', 'no_acquisition_recorded_it'), unknown)
        self.assertNotIn(('source_modified_at', 'no_acquisition_recorded_it'), unknown, 'observations recorded mtime')
        self.assertIn('hash_not_authorship_truth_or_time', summary['not_claimed'])
        self.assertIsNone(summary['scores'])
        self.assertEqual(summary['counts']['citations'], 1)
        self.assertEqual(self.evidence_digest(), before)

    def test_unclassified_kinds_untimed_events_and_zone_unknown_candidates(self):
        from server.app import timeinfo
        with db.session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            timeinfo.record_event_times(conn, self.events[1], [timeinfo.from_iso('sent', '2026-03-01 10:15', 'whatsapp:line')])
        summary = knowledge.artifact_knowledge(self.artifact_sms)
        unknown = {(u['key'], u['reason']): u for u in summary['not_recorded_or_unknown']}
        self.assertIn(('derived_text.speaker_notes.layer', 'unclassified_kind'), unknown)
        self.assertEqual(unknown[('events.ts_start', 'not_recorded')]['count'], 1)
        self.assertEqual(unknown[('event_times.zone', 'unknown_zone')]['count'], 1)
        layers = {d['kind']: d['epistemic_layer'] for d in next(r for r in summary['recorded'] if r['key'] == 'derived_text')['value']}
        self.assertEqual(layers, {'ocr': 'model_output', 'speaker_notes': 'unclassified'})

    def test_http_and_page(self):
        self.assertEqual(self.client.get(f'/api/workbench/artifacts/{self.artifact}/knowledge').status_code, 200)
        self.assertEqual(self.client.get('/api/workbench/artifacts/424242/knowledge').status_code, 404)
        page = self.client.get(f'/workbench/artifacts/{self.artifact}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('nagranie &lt;b&gt;Łódź&lt;/b&gt;.wav', page.text)
        self.assertIn(f'data-wb-copy-citation="{self.anchor["id"]}"', page.text)


class JumpTests(WorkbenchCase):
    def test_kind_prefixed_bare_and_hash_prefix(self):
        self.assertEqual([c['kind'] for c in knowledge.quick_jump(f'a:{self.artifact}')['candidates']], ['artifact'])
        self.assertEqual(knowledge.quick_jump(f'e#{self.events[0]}')['candidates'][0]['url'], f'/artifact/{self.artifact_sms}')
        bare = {c['kind'] for c in knowledge.quick_jump('1')['candidates']}
        self.assertTrue({'artifact', 'event', 'text_version', 'citation', 'annotation'} <= bare)
        with db.session() as conn:
            sha = conn.execute('SELECT sha256 FROM artifacts WHERE id=?', (self.artifact,)).fetchone()[0]
        by_hash = knowledge.quick_jump(sha[:8].upper())['candidates']
        self.assertEqual(sorted(c['id'] for c in by_hash if c['kind'] == 'artifact'), sorted([self.artifact, self.artifact_twin]))
        quote = knowledge.quick_jump(self.anchor['quote_sha256'][:10])['candidates']
        self.assertEqual([(c['kind'], c['match']) for c in quote], [('citation', 'quote_sha256_prefix')])
        for q in ('', 'zz:1', 'a:0', 'a:-1', "1' OR '1'='1", 'abc12', '%', '_'):
            with self.subTest(q=q):
                self.assertEqual(knowledge.quick_jump(q)['candidates'], [])
        with self.assertRaises(WorkbenchInputError):
            knowledge.quick_jump('x' * 129)
        self.assertEqual(self.client.get('/api/workbench/jump', params={'q': 'x' * 200}).status_code, 422)


class CitationTextTests(WorkbenchCase):
    def test_plain_text_keeps_quote_verbatim_with_locator_and_caveat(self):
        pl = knowledge.citation_text(self.anchor['id'], 'pl')
        self.assertIn(self.anchor['quote_text'], pl['text'])
        self.assertIn(f'ageds:citation/{self.anchor["id"]}@derived_text/{self.transcript_legacy}', pl['text'])
        self.assertIn(self.anchor['quote_sha256'], pl['text'])
        self.assertIn('0:00:01.500–0:00:03.000', pl['text'])
        self.assertIn('nie została zweryfikowana', pl['text'])
        self.assertEqual(pl['epistemic_layer'], 'model_output')
        en = self.client.get(f'/api/workbench/citations/{self.anchor["id"]}/text', params={'lang': 'en'}).json()
        self.assertIn('has not been verified', en['text'])
        self.assertIn(' spotkanie w Łodzi o dziewiątej', en['text'], 'the quote is never translated')
        with self.assertRaises(WorkbenchNotFound):
            knowledge.citation_text(424242)
