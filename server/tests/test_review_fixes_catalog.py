"""Review findings #4 (catalog scan write transactions, worker loop), U1 (single-file scan temp/DB location),
U4 (test-double describe provider in the UI), U5 (7z member read bounds)."""
from __future__ import annotations

import io
import sqlite3
import threading
import zipfile

import pytest

from server.app import db as dbmod
from server.app import worker
from server.app.catalog import scan as scanmod
from server.app.catalog import store
from server.tests.test_review_fixes_support import env  # noqa: F401 (fixture)


# --------------------------------------------------------------------- finding 4

def _corpus(root):
    root.mkdir()
    (root / 'a.txt').write_bytes(b'a' * 100)
    (root / 'b.bin').write_bytes(b'b' * 100)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('inner.txt', b'inner bytes')
    (root / 'd.zip').write_bytes(buffer.getvalue())
    return root


@pytest.mark.parametrize('blocked_name', ['b.bin', 'inner.txt'])
def test_finding4_no_write_transaction_is_open_while_content_is_read(env, monkeypatch, blocked_name):
    source = _corpus(env.root / 'corpus')
    collection = store.create_collection('scan concurrency (synthetic)')
    reading, writer_done = threading.Event(), threading.Event()
    original = scanmod._Engine._read

    def slow_read(self, stream, size, node, account):
        if node.name == blocked_name and not reading.is_set():
            reading.set()  # stands in for hashing a large file: another writer arrives now
            assert writer_done.wait(30)
        return original(self, stream, size, node, account)

    monkeypatch.setattr(scanmod._Engine, '_read', slow_read)
    outcome: dict = {}
    thread = threading.Thread(target=lambda: outcome.update(
        result=scanmod.run_catalog_scan(source, collection['id'], temp_dir=env.root / 'spool')))
    thread.start()
    try:
        assert reading.wait(30)
        other = sqlite3.connect(str(env.settings.db_path), timeout=1.0)
        try:
            other.execute('BEGIN IMMEDIATE')  # e.g. a worker's claim_job or a publication
            other.rollback()
            outcome['writer'] = 'acquired'
        except sqlite3.OperationalError as exc:
            outcome['writer'] = str(exc)
        finally:
            other.close()
    finally:
        writer_done.set()
        thread.join(60)
    assert outcome['writer'] == 'acquired'
    assert outcome['result']['status'] == 'completed'
    with dbmod.session() as db:
        names = sorted(r[0] for r in db.execute('SELECT name FROM catalog_entries WHERE scan_id=?',
                                                (outcome['result']['scan_id'],)))
        assert names == ['a.txt', 'b.bin', 'd.zip', 'inner.txt']
        with pytest.raises(sqlite3.DatabaseError, match='immutable'):
            db.execute("UPDATE catalog_entries SET status='ok' WHERE scan_id=?", (outcome['result']['scan_id'],))


def test_finding4_worker_loop_survives_locked_claims(env, monkeypatch):
    attempts, sleeps = [], []

    def flaky_claim(**_kwargs):
        attempts.append(1)
        if len(attempts) <= 3:
            raise sqlite3.OperationalError('database is locked')
        return None

    monkeypatch.setattr(worker, 'claim_job', flaky_claim)
    worker.main(iterations=5, sleep=sleeps.append)
    assert len(attempts) == 5
    assert sleeps[:3] == [0.5, 1.0, 2.0] and sleeps[3:] == [2, 2]
    monkeypatch.setattr(worker, 'claim_job', lambda **_kw: (_ for _ in ()).throw(sqlite3.OperationalError('disk I/O error')))
    worker.main(iterations=12, sleep=sleeps.append)
    assert max(sleeps) == worker.CLAIM_RETRY_MAX_SECONDS


# --------------------------------------------------------------------- U1

def _nested_zip(path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, 'w') as archive:
        archive.writestr('deep.txt', b'deep bytes')
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, 'w') as archive:
        archive.writestr('inner.zip', inner.getvalue())
    path.write_bytes(outer.getvalue())
    return path


def test_u1_single_file_scan_keeps_temp_db_and_store_out_of_its_directory(env):
    evidence_dir = env.root / 'evidence-folder'
    evidence_dir.mkdir()
    archive = _nested_zip(evidence_dir / 'export.zip')
    collection = store.create_collection('single file (synthetic)')
    with pytest.raises(ValueError, match='temporary directory'):
        scanmod.run_catalog_scan(archive, collection['id'], temp_dir=evidence_dir)
    with pytest.raises(ValueError, match='temporary directory'):
        scanmod.run_catalog_scan(archive, collection['id'], temp_dir=evidence_dir / 'spool')
    beside_db = _nested_zip(env.settings.db_path.parent / 'export.zip')
    with pytest.raises(ValueError, match='database'):
        scanmod.run_catalog_scan(beside_db, collection['id'], temp_dir=env.root / 'spool')
    assert sorted(p.name for p in evidence_dir.iterdir()) == ['export.zip']  # nothing written beside the source
    assert env.one('SELECT COUNT(*) FROM catalog_scans') == 0
    done = scanmod.run_catalog_scan(archive, collection['id'], temp_dir=env.root / 'spool')
    assert done['status'] == 'completed' and done['coverage']['entries'] == 3


# --------------------------------------------------------------------- U4

def test_u4_test_double_provider_is_not_offered_in_the_ui_by_default(env, monkeypatch):
    collection = store.create_collection('provider listing (synthetic)')
    with env.client() as http:
        page = http.get('/catalog', params={'collection': collection['id']}).text
    assert 'value="fake"' not in page and 'value="none"' in page
    monkeypatch.setenv('EW_CATALOG_TEST_PROVIDERS', '1')
    with env.client() as http:
        page = http.get('/catalog', params={'collection': collection['id']}).text
    assert 'value="fake"' in page


# --------------------------------------------------------------------- U5

class _Info:
    def __init__(self, filename, uncompressed):
        self.filename, self.uncompressed, self.is_directory, self.compressed = filename, uncompressed, False, 10


class _Fake7z:
    def __init__(self, infos):
        self.infos = infos

    def SevenZipFile(self, fileobj, mode='r'):  # noqa: N802 (py7zr API name)
        infos = self.infos

        class Archive:
            def needs_password(self):
                return False

            def list(self):
                return infos

            def close(self):
                pass
        return Archive()


def test_u5_7z_duplicate_names_count_every_member_read_into_memory():
    from server.app.catalog import archives
    from server.app.catalog.limits import CatalogLimits
    limits = CatalogLimits()
    big = limits.spool_memory_bytes + 1
    container = archives.SevenZipContainer(io.BytesIO(b''), 0, limits,
                                           _Fake7z([_Info('a.txt', 10), _Info('a.txt', big), _Info('b.txt', 10)]))
    members = list(container.members())
    assert container.precheck(members[1]) == 'adapter_reads_member_into_memory'
    # py7zr's read(targets=[name]) extracts every member with that name into memory.
    assert container.precheck(members[0]) == 'adapter_reads_member_into_memory'
    assert container.precheck(members[2]) is None
