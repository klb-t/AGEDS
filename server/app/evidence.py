from __future__ import annotations
import hashlib, json, mimetypes, os, sqlite3, tempfile
from datetime import datetime, timezone
from pathlib import Path
from .config import settings
from .db import session

CHUNK = 1024 * 1024


class IntegrityError(ValueError):
    """A byte identity or provenance boundary failed validation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()

def store_file(path: Path) -> tuple[str, Path, int]:
    """Hash the captured stream; publish a complete copy without replacing bytes.

    The address detects content changes when compared. It does not establish
    authorship or truth, and local permissions are not a WORM guarantee.
    """
    settings.store_dir.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=settings.store_dir, prefix='.capture-', delete=False) as out:
            temporary = Path(out.name)
            digest_builder = hashlib.sha256()
            size = 0
            with path.open('rb') as source:
                while chunk := source.read(CHUNK):
                    out.write(chunk)
                    digest_builder.update(chunk)
                    size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        digest = digest_builder.hexdigest()
        suffix = ''.join(path.suffixes)[-20:]
        dst_dir = settings.store_dir / digest[:2] / digest[2:4]
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f'{digest}{suffix}'
        os.chmod(temporary, 0o444)
        try:
            # Exclusive creation also handles two processes capturing the same
            # stream: neither can publish a partially written or replaced copy.
            os.link(temporary, dst)
        except FileExistsError:
            if dst.is_symlink() or not dst.is_file() or dst.stat().st_size != size or sha256_file(dst) != digest:
                raise IntegrityError(f'stored content failed SHA-256 verification: {dst}')
        return digest, dst, size
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def ensure_source(kind: str, label: str, locator: str | None = None, account: str | None = None, metadata: dict | None = None, *, case_id: int | None = None) -> int:
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        if case_id is None:
            case = db.execute('SELECT id FROM cases ORDER BY id LIMIT 1').fetchone()
            if case is None:
                raise ValueError('no case exists; initialize the database first')
            case_id = int(case['id'])
        if not db.execute('SELECT 1 FROM cases WHERE id=?', (case_id,)).fetchone():
            raise ValueError('case does not exist')
        row = db.execute("SELECT id FROM sources WHERE case_id=? AND kind=? AND label=? AND COALESCE(locator,'')=COALESCE(?, '') AND account IS ? ORDER BY id LIMIT 1", (case_id, kind, label, locator, account)).fetchone()
        if row:
            return int(row['id'])
        cur = db.execute("INSERT INTO sources(case_id,kind,label,locator,account,metadata_json) VALUES (?,?,?,?,?,?)", (case_id,kind,label,locator,account,json.dumps(metadata or {},ensure_ascii=False)))
        return int(cur.lastrowid)

def ingest_file(path: Path, *, source_id: int, source_locator: str | None = None, original_name: str | None = None, mime_type: str | None = None, metadata: dict | None = None, is_raw: bool = True) -> int:
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    observed_at = _utc_now()
    source_stat = path.stat()
    digest, stored, size = store_file(path)
    source_stat_after = path.stat()
    name = original_name or path.name
    mime = mime_type or mimetypes.guess_type(name)[0] or 'application/octet-stream'
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        source = db.execute('SELECT case_id FROM sources WHERE id=?', (source_id,)).fetchone()
        if source is None:
            raise ValueError('source does not exist')
        row = db.execute("SELECT id FROM artifacts WHERE sha256=? AND COALESCE(source_locator,'')=COALESCE(?, '') ORDER BY id LIMIT 1", (digest, source_locator)).fetchone()
        if row:
            aid = int(row['id'])
            provenance_cases = {r['case_id'] for r in db.execute('''
                SELECT s.case_id FROM artifacts a JOIN sources s ON s.id=a.source_id WHERE a.id=?
                UNION SELECT s.case_id FROM source_observations o JOIN sources s ON s.id=o.source_id WHERE o.artifact_id=?
                ''', (aid, aid))}
            if not provenance_cases or provenance_cases != {source['case_id']}:
                raise IntegrityError('same content/locator belongs to another or unknown case; explicit migration is required')
            stored_artifact = db.execute('SELECT stored_path,size_bytes,is_raw FROM artifacts WHERE id=?', (aid,)).fetchone()
            if bool(stored_artifact['is_raw']) != bool(is_raw):
                raise IntegrityError('raw and derived acquisitions cannot silently share an artifact identity')
            existing_path = stored_artifact['stored_path']
            if not existing_path or Path(existing_path).is_symlink() or not Path(existing_path).is_file() or sha256_file(Path(existing_path)) != digest:
                raise IntegrityError('existing artifact content is unavailable or failed SHA-256 verification')
            if stored_artifact['size_bytes'] is not None and stored_artifact['size_bytes'] != size:
                raise IntegrityError('existing artifact size disagrees with captured bytes')
        else:
            cur = db.execute("""INSERT INTO artifacts(source_id,sha256,original_name,mime_type,size_bytes,source_locator,stored_path,is_raw,metadata_json)
                                VALUES(?,?,?,?,?,?,?,?,?)""", (source_id,digest,name,mime,size,source_locator,str(stored),1 if is_raw else 0,metadata_json))
            aid = int(cur.lastrowid)
            db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES ('artifact',?,?,?)", (aid,name,metadata_json))
        observation_metadata = {
            'source_metadata': metadata or {},
            'filesystem_stat': {'mtime_ns': source_stat.st_mtime_ns, 'ctime_ns': source_stat.st_ctime_ns, 'size_bytes': source_stat.st_size},
            'filesystem_stat_after_capture': {'mtime_ns': source_stat_after.st_mtime_ns, 'ctime_ns': source_stat_after.st_ctime_ns, 'size_bytes': source_stat_after.st_size},
            'filesystem_changed_during_capture': (source_stat.st_mtime_ns,source_stat.st_ctime_ns,source_stat.st_size) != (source_stat_after.st_mtime_ns,source_stat_after.st_ctime_ns,source_stat_after.st_size),
            'source_snapshot_consistency': 'not_guaranteed_without_source_lock_or_snapshot',
            'filesystem_time_semantics': 'mtime=filesystem_modification; ctime=filesystem_status_change_not_creation',
            'content_claim': 'captured_bytes_only_not_truth_or_authorship',
        }
        observation = db.execute('''INSERT INTO source_observations(
            artifact_id,source_id,source_locator,observed_at,acquisition_kind,
            original_name,mime_type,size_bytes,sha256,source_modified_at,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (aid, source_id, source_locator, observed_at, 'ingest', name, mime, size, digest,
             datetime.fromtimestamp(source_stat.st_mtime, timezone.utc).isoformat(),
             json.dumps(observation_metadata, ensure_ascii=False)))
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('ingest','artifact',?,?)", (aid,json.dumps({'sha256':digest,'source_locator':source_locator,'source_id':source_id,'observation_id':observation.lastrowid,'deduplicated':row is not None},ensure_ascii=False)))
        return aid

def add_event(*, source_id: int, artifact_id: int | None, event_type: str, ts_start: str | None = None, ts_end: str | None = None, direction: str | None = None, sender: str | None = None, recipients: str | None = None, contact_label: str | None = None, phone_or_address: str | None = None, subject: str | None = None, body: str | None = None, external_id: str | None = None, thread_id: str | None = None, confidence: float | None = None, metadata: dict | None = None) -> int:
    values = (artifact_id,source_id,event_type,ts_start,ts_end,direction,sender,recipients,contact_label,phone_or_address,subject,body,external_id,thread_id,confidence,json.dumps(metadata or {},ensure_ascii=False,sort_keys=True))
    columns = ('artifact_id','source_id','event_type','ts_start','ts_end','direction','sender','recipients','contact_label','phone_or_address','subject','body','external_id','thread_id','confidence','metadata_json')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        source = db.execute('SELECT case_id FROM sources WHERE id=?', (source_id,)).fetchone()
        if source is None:
            raise ValueError('source does not exist')
        if artifact_id is not None:
            provenance_cases = {r['case_id'] for r in db.execute('''
                SELECT s.case_id FROM artifacts a JOIN sources s ON s.id=a.source_id WHERE a.id=?
                UNION SELECT s.case_id FROM source_observations o JOIN sources s ON s.id=o.source_id WHERE o.artifact_id=?
                ''', (artifact_id, artifact_id))}
            if provenance_cases != {source['case_id']}:
                raise IntegrityError('event source and artifact must belong to the same known case')
        if external_id is not None:
            existing = db.execute('SELECT * FROM events WHERE artifact_id IS ? AND source_id=? AND event_type=? AND external_id=? ORDER BY id LIMIT 1', (artifact_id,source_id,event_type,external_id)).fetchone()
            if existing:
                # Equality of parsed values avoids silently accepting parser-ID
                # collisions or a later reinterpretation under the same version.
                if any(existing[column] != value for column, value in zip(columns, values)):
                    raise IntegrityError('event external_id conflicts with existing payload; retain a separate parser version')
                eid = int(existing['id'])
                db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('event_reobserved','event',?,?)", (eid,json.dumps({'artifact_id':artifact_id,'source_id':source_id,'external_id':external_id},ensure_ascii=False)))
                return eid
        cur = db.execute("""INSERT INTO events(artifact_id,source_id,event_type,ts_start,ts_end,direction,sender,recipients,contact_label,phone_or_address,subject,body,external_id,thread_id,confidence,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
        eid = int(cur.lastrowid)
        title = ' | '.join(x for x in [event_type, contact_label, phone_or_address, subject] if x)
        db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES ('event',?,?,?)", (eid,title,body or ''))
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('event_imported','event',?,?)", (eid,json.dumps({'artifact_id':artifact_id,'source_id':source_id,'external_id':external_id},ensure_ascii=False)))
        return eid

def add_derived_text(artifact_id: int, kind: str, text: str, *, model: str | None = None, language: str | None = None, segments: list | None = None, confidence: float | None = None, run_id: int | None = None, metadata: dict | None = None, db: sqlite3.Connection | None = None) -> int:
    """Append a text version; an optional caller transaction supports lease fencing.

    Legacy calls leave run_id unset: provenance is unknown, not reconstructed.
    The caller owns commit/rollback when it supplies db.
    """
    def append(connection: sqlite3.Connection) -> int:
        artifact = connection.execute('SELECT original_name FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
        if artifact is None:
            raise ValueError('artifact does not exist')
        if run_id is not None:
            run = connection.execute('SELECT artifact_id FROM processing_runs WHERE id=?', (run_id,)).fetchone()
            if run is None or run['artifact_id'] != artifact_id:
                raise IntegrityError('processing run does not belong to this artifact')
        cur = connection.execute("INSERT INTO derived_text(artifact_id,kind,model,language,text,segments_json,confidence,run_id,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)", (artifact_id,kind,model,language,text,json.dumps(segments or [],ensure_ascii=False),confidence,run_id,json.dumps(metadata or {},ensure_ascii=False)))
        did = int(cur.lastrowid)
        connection.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES ('derived_text',?,?,?)", (did,f"{kind}: {artifact['original_name']}",text))
        connection.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('derive_text','derived_text',?,?)", (did,json.dumps({'artifact_id':artifact_id,'kind':kind,'model':model,'run_id':run_id},ensure_ascii=False)))
        return did
    if db is not None:
        return append(db)
    with session() as connection:
        return append(connection)
