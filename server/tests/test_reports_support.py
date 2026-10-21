"""Shared synthetic environment for the reports tests (no private data, no model).

``ReportEnv`` builds a temporary database and content store, imports the
synthetic corpus parts ``chat_exports`` (three ChatGPT exports of one
conversation with an edit and a branch, plus Claude) and ``communications``
(MBOX, calls.xml), catalogues ``catalog_files`` (nested archives, shared PDF,
type conflicts) into two collections, and plants hard cases directly: an SMS with an explicit
offset whose text contains HTML, a formula-like text and digits; a WhatsApp-like
wall-clock time with unknown zone; an untimed call; a control character; a
recording with two transcript versions, segment and word citations, a legacy
NaN word timing; a human annotation; an integration proposal; a token (whose
secret must never leave the database). ``crosscheck`` proves that every number
in a report's HTML is a value of its ``report.json``.
"""
from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile

from server.app import citations, db, evidence, timeinfo
from server.app.reports.common import resolve_pointer
from server.app.reports.render import fmt
from server.tests.corpus import catalog_files, chat_exports, communications

SEGMENTS = [
    {'start': 0.0, 'end': 1.25, 'text': ' Zażółć gęślą jaźń',
     'words': [{'start': 0.0, 'end': 0.5, 'word': ' Zażółć'}, {'start': 0.5, 'end': 0.9, 'word': ' gęślą'},
               {'start': 0.9, 'end': 1.25, 'word': ' jaźń'}]},
    {'start': 1.25, 'end': 2.5, 'text': ' umowa <b>nr 17</b> podpisana 2026-03-01'},
    {'start': 2.5, 'end': 3.0, 'text': ' koniec', 'words': [{'start': float('nan'), 'end': 3.0, 'word': ' koniec'}]},
]
HTML_BODY = 'spotkanie o 9:30 <script>window.__x=1</script> & <b>pogrubienie</b>'
FORMULA_BODY = '=HYPERLINK("https://example.org","klik")'
CONTROL_BODY = 'linia\x01z kontrolnym znakiem'


class ReportEnv:
    def __init__(self, *, chat: bool = True, comm: bool = True, catalog: bool = True, analysis: bool = True,
                 root: Path | None = None):
        self.tmp = tempfile.TemporaryDirectory(prefix='ageds-reports-') if root is None else None
        self.root = Path(self.tmp.name) if root is None else Path(root)
        (self.root / 'data' / 'store').mkdir(parents=True, exist_ok=True)
        self.settings = replace(db.settings, data_dir=self.root / 'data', db_path=self.root / 'data' / 'evidence.db',
                                store_dir=self.root / 'data' / 'store', scan_roots=(self.root / 'scan',))
        (self.root / 'scan').mkdir(exist_ok=True)
        self.output = self.root / 'private-reports'
        self.chat, self.comm, self.catalog, self.analysis = chat, comm, catalog, analysis
        self.budget_run = self.hypothesis = None
        self.correlations: list[int] = []

    def start(self, monkeypatch) -> 'ReportEnv':
        from server.app import main
        for module in (db, evidence, main):
            monkeypatch.setattr(module, 'settings', self.settings)
        monkeypatch.delenv('EW_PDF_CHROMIUM', raising=False)
        monkeypatch.delenv('EW_REPORTS_DIR', raising=False)
        monkeypatch.setenv('EW_REQUIRE_TOKEN', '')
        db.init_db()
        self.plant()
        return self

    def close(self):
        if self.tmp is not None:
            self.tmp.cleanup()

    def plant(self):
        corpus_root = self.root / 'corpus'
        self.corpus = {}
        if self.chat:
            from server.app.importers import chatgpt, claude_ai
            self.corpus['chat'] = chat_exports.build(corpus_root / 'chat')
            for label in ('e1', 'e2', 'e3'):
                chatgpt.import_chatgpt_export([self.corpus['chat']['chatgpt_exports'][label]], export_label=label)
            claude_ai.import_claude_export([self.corpus['chat']['claude']['path']], export_label='k1')
        if self.comm:
            from server.app.importers import call_log, email_mbox
            self.corpus['comm'] = communications.build(corpus_root / 'comm')
            email_mbox.import_mbox(Path(self.corpus['comm']['mbox']['path']))
            call_log.import_calls_xml(Path(self.corpus['comm']['calls']['path']))
        if self.catalog:
            from server.app.catalog import store as catalog_store
            from server.app.catalog.scan import run_catalog_scan
            self.corpus['catalog'] = catalog_files.build(corpus_root / 'catalog')
            temp = self.root / 'catalog-temp'
            temp.mkdir()
            self.collections = []
            for key, label in (('phone_a', 'Phone A (synthetic)'), ('drive_b', 'Drive B (synthetic)')):
                collection = catalog_store.create_collection(label, device_or_account=key)
                run_catalog_scan(self.corpus['catalog']['roots'][key], collection['id'], temp_dir=temp)
                self.collections.append(collection['id'])
        self.source = evidence.ensure_source('synthetic', 'Phone A export')
        xml = self.root / 'messages.xml'
        xml.write_bytes(b'<smses count="3"/>')
        self.artifact_sms = evidence.ingest_file(xml, source_id=self.source, source_locator='content://a/messages.xml')
        rows = [
            ('sms', '2026-03-01T08:00:00+00:00', 'in', '+48 000 000 001', 'Urząd Przykładowy', HTML_BODY, 'sms-1'),
            ('sms', '2026-03-01T09:30:00+01:00', 'out', '+48 000 000 001', 'Urząd Przykładowy', FORMULA_BODY, 'sms-2'),
            ('whatsapp', None, 'in', '+31 6 0000 0002', 'Osoba B', 'biuro zamknięte do 14', 'wa-1'),
            ('call', None, 'in', '+48 000 000 003', None, None, 'call-untimed'),
            ('sms', '2026-03-05T07:00:00+00:00', 'in', 'kontakt@example.org', 'Kancelaria', CONTROL_BODY, 'sms-3'),
        ]
        self.events = [evidence.add_event(source_id=self.source, artifact_id=self.artifact_sms, event_type=t, ts_start=ts,
                                          direction=d, phone_or_address=p, contact_label=l, body=b, external_id=x)
                       for t, ts, d, p, l, b, x in rows]
        with db.session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            timeinfo.record_event_times(conn, self.events[2], [timeinfo.from_iso('sent', '2026-03-01 10:15', 'whatsapp:line')])
        audio = self.root / 'recording.wav'
        audio.write_bytes(b'RIFF synthetic wav bytes for reports')
        self.artifact_audio = evidence.ingest_file(audio, source_id=self.source, source_locator='content://a/recording.wav')
        self.v1 = evidence.add_derived_text(self.artifact_audio, 'transcript', ''.join(s['text'] for s in SEGMENTS),
                                            model='synthetic', language='pl', segments=SEGMENTS)
        self.v2 = evidence.add_derived_text(self.artifact_audio, 'transcript', ' nowa wersja', model='synthetic-2',
                                            language='pl', segments=[{'start': 0.0, 'end': 0.5, 'text': ' nowa wersja'}])
        self.anchor_segment = citations.create_citation(self.artifact_audio, self.v1, [1])
        self.anchor_words = citations.create_citation(self.artifact_audio, self.v1,
                                                      word_refs=[{'segment_index': 0, 'word_index': 1},
                                                                 {'segment_index': 0, 'word_index': 2}])
        with db.session() as conn:
            conn.execute('INSERT INTO annotations(artifact_id,event_id,kind,label,body) VALUES (?,?,?,?,?)',
                         (self.artifact_sms, self.events[0], 'note', 'ważne', 'Do sprawdzenia: pismo z 3 marca'))
            conn.execute('''INSERT INTO annotation_proposals(client_id,artifact_id,body,mode,provider,model,status,created_at)
                            VALUES ('chatadhd',?,?,'anchored','unknown','unknown','proposed','2026-03-06T00:00:00Z')''',
                         (self.artifact_audio, 'propozycja <i>adnotacji</i>'))
        from server.app.integrations import tokens
        self.token = tokens.create_token('reports-test', ('read',), client_id='reports-test')
        if self.analysis:
            self.plant_analysis()

    def plant_analysis(self):
        """A stored time-budget run (E1) and a hypothesis with versions, an event study, dossier items,
        theses and a disclosure (E2) over the planted events, so the analysis report sections render
        real stored results (every number then traceable to report.json and the stored run)."""
        from server.app.analysis import budget, categories, dossier, eventstudy, preregistration, theses
        config = categories.parse_config({
            'schema': categories.CONFIG_SCHEMA, 'version': 'reports-synthetic-1',
            'categories': [{'id': 'sprawy_urzedowe', 'label': 'Sprawy urzędowe'}, {'id': 'kancelaria', 'label': 'Kancelaria'}],
            'default_category': None, 'combine': 'max', 'official_categories': ['sprawy_urzedowe'],
            'rules': [{'id': 'urzad', 'version': 1, 'kind': 'counterparty', 'values': ['+48 000 000 001'],
                       'weights': {'sprawy_urzedowe': 1}},
                      {'id': 'kancelaria', 'version': 1, 'kind': 'counterparty', 'values': ['kontakt@example.org'],
                       'weights': {'kancelaria': 1}}]})
        conn = db.connect()
        try:
            stored = budget.run_budget(conn, config, window_from='2026-03-01', window_to='2026-03-08', zone='Europe/Warsaw',
                                       params={'grid_mode': 'one_at_a_time'})
            conn.commit()
            self.budget_run = stored['run']['run_id']
            definition = {
                'title': 'Synthetic: incoming SMS → outgoing SMS', 'text': 'After an incoming SMS an outgoing SMS follows.',
                'stimulus': {'event_types': ['sms'], 'directions': ['in']},
                'outcome': {'event_types': ['sms'], 'directions': ['out']},
                'windows': [{'label': 'after_6h', 'start_s': 0, 'end_s': 6 * 3600}],
                'lags': {'min_s': -6 * 3600, 'max_s': 6 * 3600, 'bin_s': 3600},
                'analysis': {'permutations': 19, 'seed': 7},
            }
            version = preregistration.register_hypothesis(conn, definition, actor='reports-test')
            self.hypothesis = version['hypothesis_id']
            self.event_study = eventstudy.run_event_study(conn, version['version_id'], actor='reports-test')
            supporting = dossier.add_item(conn, self.hypothesis, 'event', self.events[1], classification='supporting',
                                          rationale='odpowiedź po piśmie', actor='reports-test')
            anchor = dossier.add_item(conn, self.hypothesis, 'anchor', self.anchor_segment['id'], actor='reports-test')
            self.dossier_items = (supporting['item_id'], anchor['item_id'])
            self.thesis_supported = theses.add_thesis(conn, self.hypothesis, 'Odpowiedź wysłano tego samego dnia.',
                                                      'paraphrase', [supporting['item_id']], actor='reports-test')
            self.thesis_unsupported = theses.add_thesis(conn, self.hypothesis, 'Urząd odpowiada zawsze w 2 godziny.',
                                                        'speculation', [], actor='reports-test')
            theses.add_disclosure(conn, self.hypothesis, 'known_unknowns', 'Brak billingu za marzec.', actor='reports-test')
            if self.chat:  # one conversation in several exports: correlation hypotheses, one of them reviewed
                from server.app.timeline import correlation
                run = correlation.run_rule(conn, 'same_conversation', actor='reports-test')
                listed = correlation.list_hypotheses(conn, run_id=run['id'])['hypotheses']
                self.correlations = [h['id'] for h in listed]
                if listed:
                    correlation.review(conn, listed[0]['id'], 'accept', reason='sprawdzone ręcznie', actor='reports-test')
        finally:
            conn.close()

    def report(self, report_type: str, params: dict | None = None, output: Path | None = None) -> dict:
        from server.app.reports.service import create_report
        return create_report(report_type, params or {}, output or self.output)


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.spans: list[tuple[str, str]] = []
        self.outside: list[str] = []
        self.title = ''
        self.hrefs: list[str] = []
        self.tags: list[str] = []
        self._pointer = None
        self._buffer = ''
        self._skip = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        if tag in ('style', 'title'):
            self._skip = tag
        if tag == 'a' and attrs.get('href'):
            self.hrefs.append(attrs['href'])
        if tag == 'span' and 'data-k' in attrs:
            if self._pointer is not None:
                raise AssertionError('nested data spans')
            self._pointer, self._buffer = attrs['data-k'], ''

    def handle_endtag(self, tag):
        if tag == self._skip:
            self._skip = None
        if tag == 'span' and self._pointer is not None:
            self.spans.append((self._pointer, self._buffer))
            self._pointer = None

    def handle_data(self, data):
        if self._skip == 'title':
            self.title += data
        elif self._skip == 'style':
            return
        elif self._pointer is not None:
            self._buffer += data
        else:
            self.outside.append(data)


def crosscheck(html: bytes, document: dict) -> dict:
    """Every ``data-k`` span equals its JSON value; no numeral outside spans."""
    parser = _Collector()
    parser.feed(html.decode('utf-8'))
    lang = document['language']
    mismatches = []
    for pointer, text in parser.spans:
        expected = fmt(resolve_pointer(document, pointer), lang)
        if text != expected:
            mismatches.append((pointer, text, expected))
    stray = [chunk for chunk in parser.outside if any(char.isnumeric() for char in chunk)]
    return {'spans': len(parser.spans), 'mismatches': mismatches, 'stray_numerals': stray,
            'title_ok': parser.title == document['title'], 'hrefs': parser.hrefs, 'tags': set(parser.tags)}


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding='utf-8'))
