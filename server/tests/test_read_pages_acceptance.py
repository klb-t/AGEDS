"""Independent bounded history-page acceptance (N31); synthetic rows only."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.app import db, main

KINDS = ('transcripts', 'citations', 'annotations')
IDS = [7, 2**53+1, 2**63-5]
TEXT = ' Zażółć e\u0301\n音声 <script>raw</script>'
SELECTOR = {'kind':'segments','indices':[0],'text_join':'concatenate_exact',
            'time_unit':'seconds','stored_time_unit':'milliseconds','rounding':'nearest_ms','precision':'segment'}


class ReadPagesAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = replace(db.settings, data_dir=self.root, db_path=self.root/'pages.sqlite', store_dir=self.root/'store')
        for module in (db, main):
            item = patch.object(module, 'settings', self.settings)
            item.start()
            self.addCleanup(item.stop)
        db.init_db()
        with db.session() as conn:
            for aid in (1,2,3):
                conn.execute('INSERT INTO artifacts(id,original_name,stored_path) VALUES (?,?,?)',
                             (aid, 'synthetic', '/must/not/open;$(inert)'))
        for identifier in IDS:
            self.insert_all(identifier, 1)
        self.insert_all(2**63-2, 2)

    def insert_all(self, identifier, artifact):
        with db.session() as conn:
            conn.execute("INSERT INTO derived_text(id,artifact_id,kind,text,segments_json) VALUES (?,?,'transcript',?,?)",
                         (identifier, artifact, TEXT, json.dumps([{'start':0,'end':1,'text':TEXT}],ensure_ascii=False)))
            conn.execute('INSERT INTO evidence_anchors(id,artifact_id,derived_text_id,start_ms,end_ms,quote_text,quote_sha256,selector_json) VALUES (?,?,?,0,1000,?,?,?)',
                         (identifier,artifact,identifier,TEXT,hashlib.sha256(TEXT.encode()).hexdigest(),json.dumps(SELECTOR)))
            conn.execute("INSERT INTO annotations(id,artifact_id,derived_text_id,body) VALUES (?,?,?,?)",
                         (identifier,artifact,identifier,TEXT))

    def read(self, kind, **kwargs):
        from server.app.read_pages import read_page
        return read_page(1, kind, **kwargs)

    @staticmethod
    def url(kind, artifact=1):
        return f'/api/artifacts/{artifact}/{kind}/page'

    def test_sparse_sql63_ids_roundtrip_without_float_or_other_artifact(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                first = self.read(kind, limit=2)
                self.assertEqual(set(first), {'artifactId','items','nextBeforeId','snapshotMaxId','hasMore','limit'})
                self.assertEqual(first['artifactId'],1)
                self.assertEqual(first['snapshotMaxId'],IDS[-1])
                self.assertEqual([item['id'] for item in first['items']],list(reversed(IDS[1:])))
                self.assertTrue(first['hasMore'])
                self.assertEqual(first['nextBeforeId'],IDS[1])
                last = self.read(kind, limit=2,before_id=first['nextBeforeId'],snapshot_max_id=first['snapshotMaxId'])
                self.assertEqual([item['id'] for item in last['items']],[IDS[0]])
                self.assertFalse(last['hasMore'])
                self.assertIsNone(last['nextBeforeId'])
                self.assertEqual(last['snapshotMaxId'],first['snapshotMaxId'])

    def test_concurrent_append_above_snapshot_does_not_join_existing_walk(self):
        first = {kind:self.read(kind,limit=1) for kind in KINDS}
        self.insert_all(2**63-3,1)
        for kind in KINDS:
            with self.subTest(kind=kind):
                page = first[kind]
                continuation = self.read(kind, limit=100,before_id=page['nextBeforeId'],snapshot_max_id=page['snapshotMaxId'])
                self.assertEqual([item['id'] for item in continuation['items']],list(reversed(IDS[:-1])))
                self.assertEqual(self.read(kind)['items'][0]['id'],2**63-3)

    def test_readonly_no_source_access_or_database_mutation(self):
        before = self.settings.db_path.read_bytes()
        with patch('builtins.open',side_effect=AssertionError('no source reads')), \
             patch.object(Path,'open',side_effect=AssertionError('no source reads')):
            for kind in KINDS:
                result = self.read(kind,limit=2)
                self.assertEqual(len(result['items']),2)
        self.assertEqual(self.settings.db_path.read_bytes(),before)
        self.assertFalse(self.settings.store_dir.exists())

    def test_http_page_items_match_legacy_lists_without_schema_change(self):
        with TestClient(main.app) as client:
            for kind in KINDS:
                with self.subTest(kind=kind):
                    legacy = client.get(f'/api/artifacts/1/{kind}')
                    self.assertEqual(legacy.status_code,200)
                    self.assertIsInstance(legacy.json(),list)
                    response = client.get(self.url(kind),params={'limit':100})
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertEqual(response.json()['items'],sorted(legacy.json(),key=lambda item:item['id'],reverse=True))
                    self.assertEqual(response.json()['snapshotMaxId'],IDS[-1])
                    self.assertFalse(response.json()['hasMore'])
                    self.assertIsNone(response.json()['nextBeforeId'])
                    if kind=='citations':
                        self.assertEqual(response.json()['items'][0]['quote_text'],TEXT)
                    if kind=='annotations':
                        self.assertEqual(response.json()['items'][0]['body'],TEXT)

    def test_http_invalid_cursor_bounds_are_explicit_422(self):
        attempts = [{'limit':0},{'limit':101},{'limit':'true'},{'limit':'2.5'},
                    {'before_id':IDS[-1]},{'snapshot_max_id':IDS[-1]},
                    {'before_id':0,'snapshot_max_id':IDS[-1]},
                    {'before_id':2**63,'snapshot_max_id':2**63},
                    {'before_id':IDS[-1]+1,'snapshot_max_id':IDS[-1]},
                    {'before_id':'NaN','snapshot_max_id':IDS[-1]}]
        with TestClient(main.app) as client:
            for kind in KINDS:
                for params in attempts:
                    with self.subTest(kind=kind,params=params):
                        response = client.get(self.url(kind),params=params)
                        self.assertEqual(response.status_code,422,response.text)
                        self.assertNotIn('items',response.json())

    def test_empty_artifact_and_missing_artifact_are_distinct(self):
        with TestClient(main.app) as client:
            for kind in KINDS:
                with self.subTest(kind=kind):
                    empty = client.get(self.url(kind,3))
                    self.assertEqual(empty.status_code,200,empty.text)
                    self.assertEqual(empty.json()['items'],[])
                    self.assertEqual(empty.json()['snapshotMaxId'],0)
                    self.assertFalse(empty.json()['hasMore'])
                    self.assertIsNone(empty.json()['nextBeforeId'])
                    self.assertEqual(client.get(self.url(kind,999)).status_code,404)

    def test_oversized_next_row_is_not_skipped_and_cursor_reports_413(self):
        from server.app import read_pages as pages
        with db.session() as conn:
            conn.execute('UPDATE annotations SET body=? WHERE id=?',('x'*(pages.MAX_ROW_BYTES+1),IDS[0]))
        page = self.read('annotations',limit=100)
        self.assertEqual([item['id'] for item in page['items']],list(reversed(IDS[1:])))
        self.assertTrue(page['hasMore'])
        self.assertEqual(page['nextBeforeId'],IDS[1])
        with self.assertRaises(pages.PageLimitError):
            self.read('annotations',limit=100,before_id=page['nextBeforeId'],snapshot_max_id=page['snapshotMaxId'])
        with TestClient(main.app) as client:
            response=client.get(self.url('annotations'),params={'before_id':page['nextBeforeId'],'snapshot_max_id':page['snapshotMaxId']})
            self.assertEqual(response.status_code,413,response.text)
            self.assertNotIn('items',response.json())

    def test_transcript_summary_does_not_materialize_large_unselected_raw_text(self):
        from server.app import read_pages as pages
        with db.session() as conn:
            conn.execute('UPDATE derived_text SET text=?,segments_json=?,metadata_json=? WHERE id=?',
                         ('x'*(pages.MAX_ROW_BYTES+1),'[raw malformed huge ignored]'*100000,'{invalid}',IDS[-1]))
        page=self.read('transcripts',limit=1)
        self.assertEqual(page['items'][0]['id'],IDS[-1])
        self.assertNotIn('text',page['items'][0])
        self.assertNotIn('segments_json',page['items'][0])
        self.assertNotIn('metadata_json',page['items'][0])

    def test_byte_limited_pages_preserve_every_id_in_order(self):
        from server.app import read_pages as pages
        with db.session() as conn:
            conn.execute('UPDATE annotations SET body=? WHERE artifact_id=1',('音声'*80,))
        with patch.object(pages,'MAX_PAGE_BYTES',1100):
            page=self.read('annotations',limit=100)
            seen=[]
            pages_seen=0
            while True:
                pages_seen+=1
                self.assertLess(pages_seen,5)
                self.assertLessEqual(len(json.dumps(page,ensure_ascii=False,separators=(',',':')).encode()),1100)
                seen.extend(item['id'] for item in page['items'])
                if not page['hasMore']:
                    break
                self.assertTrue(page['items'])
                self.assertEqual(page['nextBeforeId'],page['items'][-1]['id'])
                page=self.read('annotations',limit=100,before_id=page['nextBeforeId'],snapshot_max_id=page['snapshotMaxId'])
            self.assertEqual(seen,list(reversed(IDS)))
            self.assertGreater(pages_seen,1)

    def test_direct_cursor_api_refuses_boolean_and_non_integer_ids(self):
        from server.app import read_pages as pages
        for kwargs in ({'limit':True},{'limit':1.0},{'before_id':True,'snapshot_max_id':IDS[-1]},
                       {'before_id':IDS[-1],'snapshot_max_id':False},
                       {'before_id':str(IDS[-1]),'snapshot_max_id':IDS[-1]}):
            with self.subTest(kwargs=kwargs),self.assertRaises(pages.PageInputError):
                self.read('transcripts',**kwargs)

    def test_forged_other_artifact_cursor_never_exposes_its_rows(self):
        from server.app.read_pages import read_page
        for kind in KINDS:
            with self.subTest(kind=kind):
                page=read_page(1,kind,before_id=2**63-2,snapshot_max_id=2**63-2,limit=100)
                self.assertEqual([item['id'] for item in page['items']],list(reversed(IDS)))
                self.assertEqual(page['artifactId'],1)

    def test_malformed_selected_citation_does_not_become_partial_success(self):
        from server.app import read_pages as pages
        bad_values=['{broken','{"kind":"segments","kind":"words"}', '{"number":NaN}', '['*1000+'0'+']'*1000]
        for number,raw in enumerate(bad_values,start=8):
            with db.session() as conn:
                conn.execute('INSERT INTO evidence_anchors(id,artifact_id,derived_text_id,start_ms,end_ms,quote_text,quote_sha256,selector_json) VALUES (?,1,?,0,1000,?,?,?)',
                             (number,IDS[0],TEXT,hashlib.sha256(TEXT.encode()).hexdigest(),raw))
            with self.subTest(raw=raw[:60]),self.assertRaises((pages.PageStoredError,pages.PageLimitError)):
                self.read('citations',before_id=number+1,snapshot_max_id=IDS[-1])

    def test_oversized_first_row_never_fetches_text_into_python(self):
        from server.app import read_pages as pages
        with db.session() as conn:
            conn.execute('UPDATE annotations SET body=? WHERE id=?',('x'*(pages.MAX_ROW_BYTES+1),IDS[-1]))
        statements=[]
        original_connect=db.connect
        class GuardConnection:
            def __init__(self,connection):
                self.connection=connection
            def __getattr__(self,name):
                return getattr(self.connection,name)
            def execute(self,sql,*args):
                statements.append(sql)
                if sql.startswith('SELECT "id"') and 'FROM annotations' in sql:
                    raise AssertionError('oversized TEXT row must never be fetched')
                return self.connection.execute(sql,*args)
        def connect(*args,**kwargs):
            self.assertIs(kwargs.get('read_only'),True)
            return GuardConnection(original_connect(*args,**kwargs))
        with patch.object(db,'connect',connect),self.assertRaises(pages.PageLimitError):
            self.read('annotations')
        self.assertTrue(any('AS row_bytes' in sql for sql in statements))

    def test_empty_snapshot_remains_empty_after_later_append(self):
        from server.app.read_pages import read_page
        for kind in KINDS:
            self.assertEqual(read_page(3,kind)['snapshotMaxId'],0)
        self.insert_all(2**63-1,3)
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(read_page(3,kind,before_id=1,snapshot_max_id=0)['items'],[])
                self.assertEqual(read_page(3,kind)['items'][0]['id'],2**63-1)

    def test_malformed_later_row_aborts_whole_page_instead_of_omitting_it(self):
        from server.app import read_pages as pages
        with db.session() as conn:
            conn.execute('INSERT INTO evidence_anchors(id,artifact_id,derived_text_id,start_ms,end_ms,quote_text,quote_sha256,selector_json) VALUES (8,1,?,0,1000,?,?,?)',
                         (IDS[0],TEXT,hashlib.sha256(TEXT.encode()).hexdigest(),'{invalid'))
        with self.assertRaises(pages.PageStoredError):
            self.read('citations',limit=100)
        with TestClient(main.app) as client:
            response=client.get(self.url('citations'),params={'limit':100})
            self.assertEqual(response.status_code,409,response.text)
            self.assertNotIn('items',response.json())

    def test_projected_escape_expansion_cannot_bypass_row_limit(self):
        from server.app import read_pages as pages
        # JSON quotes double in the wire encoding although stored UTF-8 is small.
        with db.session() as conn:
            conn.execute('UPDATE annotations SET body=? WHERE id=?',('"'*(pages.MAX_ROW_BYTES//2+10),IDS[-1]))
        with self.assertRaises(pages.PageLimitError):
            self.read('annotations',limit=1)

    def test_database_work_budget_is_explicit_failure_without_partial_page(self):
        from server.app import read_pages as pages
        before=self.settings.db_path.read_bytes()
        with patch.object(pages,'MAX_SQLITE_STEPS',0),patch.object(pages,'SQLITE_PROGRESS_INTERVAL',1):
            with self.assertRaises(pages.PageLimitError):
                self.read('transcripts')
        self.assertEqual(self.settings.db_path.read_bytes(),before)

    def test_http_missing_database_is_503_without_creating_it(self):
        missing=self.root/'absent'/'missing.sqlite'
        settings=replace(self.settings,db_path=missing)
        with TestClient(main.app) as client,patch.object(db,'settings',settings):
            for kind in KINDS:
                with self.subTest(kind=kind):
                    response=client.get(self.url(kind))
                    self.assertEqual(response.status_code,503,response.text)
                    self.assertNotIn(str(missing),response.text)
        self.assertFalse(missing.exists())
