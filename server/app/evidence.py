from __future__ import annotations
import hashlib, json, mimetypes, os, sqlite3, tempfile
from contextlib import contextmanager
from contextvars import ContextVar
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

# What the filesystem stat of ``path`` describes (observation metadata
# ``filesystem_stat_scope``). Only ``source_path`` stats the original location.
STAT_SCOPES = {
    'path': 'source_path',
    'temporary_copy': 'temporary_copy_not_original_source',
    'declared': 'declared_by_caller',
}
_DEFAULT_STAT_SCOPE: ContextVar[str | None] = ContextVar('ageds_ingest_stat_scope', default=None)


@contextmanager
def temporary_copy_acquisitions():
    """Inside this block ``ingest_file`` treats every path as a server-side temporary copy.

    Upload routes wrap importers with it: the importers acquire the private
    temporary file, whose mtime is the upload instant, not a property of the
    source. An explicit ``source_stat`` argument still wins.
    """
    token = _DEFAULT_STAT_SCOPE.set('temporary_copy')
    try:
        yield
    finally:
        _DEFAULT_STAT_SCOPE.reset(token)


def _declared_instant(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise ValueError('declared_source_modified_at must be ISO 8601 with an offset (an instant), or None')
    return moment.astimezone(timezone.utc).isoformat()


def ingest_file(path: Path, *, source_id: int, source_locator: str | None = None, original_name: str | None = None, mime_type: str | None = None, metadata: dict | None = None, is_raw: bool = True, collection_id: int | None = None, parent_artifact_id: int | None = None,
                source_stat: str | None = None, declared_source_modified_at: str | None = None, declared_time_basis: str | None = None) -> int:
    """Acquire bytes; ``parent_artifact_id`` marks a contained/extracted child.

    The parent must already exist in the same case. ``None`` means "not
    stated": a repeated acquisition of the same content/locator without a
    parent reuses the artifact. Naming a different parent is a visible
    conflict, never a silent re-parenting.

    ``source_stat`` says what ``path`` is (review finding 1): ``'path'`` (the
    default outside :func:`temporary_copy_acquisitions`) is the original
    location and its mtime becomes ``source_modified_at``; ``'temporary_copy'``
    is a server-side copy (upload, extraction), so no source time is recorded;
    ``'declared'`` records ``declared_source_modified_at`` (an instant or None)
    with the caller's ``declared_time_basis`` (e.g. a catalogued mtime).
    """
    scope = source_stat or _DEFAULT_STAT_SCOPE.get() or 'path'
    if scope not in STAT_SCOPES:
        raise ValueError(f'source_stat must be one of {", ".join(STAT_SCOPES)}')
    if scope == 'declared':
        if not isinstance(declared_time_basis, str) or not declared_time_basis.strip():
            raise ValueError('a declared source time needs declared_time_basis')
        declared = _declared_instant(declared_source_modified_at)
    elif declared_source_modified_at is not None or declared_time_basis is not None:
        raise ValueError("declared_source_modified_at/declared_time_basis require source_stat='declared'")
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    observed_at = _utc_now()
    source_stat_before = path.stat() if scope == 'path' else None
    digest, stored, size = store_file(path)
    source_stat_after = path.stat() if scope == 'path' else None
    name = original_name or path.name
    mime = mime_type or mimetypes.guess_type(name)[0] or 'application/octet-stream'
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        source = db.execute('SELECT case_id FROM sources WHERE id=?', (source_id,)).fetchone()
        if source is None:
            raise ValueError('source does not exist')
        if collection_id is not None:
            collection = db.execute('SELECT case_id FROM collections WHERE id=?', (collection_id,)).fetchone()
            if collection is None or collection['case_id'] != source['case_id']:
                raise IntegrityError('collection does not exist in the source case')
        if parent_artifact_id is not None:
            parent_cases = {r['case_id'] for r in db.execute('''
                SELECT s.case_id FROM artifacts a JOIN sources s ON s.id=a.source_id WHERE a.id=?
                UNION SELECT s.case_id FROM source_observations o JOIN sources s ON s.id=o.source_id WHERE o.artifact_id=?
                ''', (parent_artifact_id, parent_artifact_id))}
            if parent_cases != {source['case_id']}:
                raise IntegrityError('parent artifact does not exist in the source case')
        row = db.execute("SELECT id FROM artifacts WHERE sha256=? AND COALESCE(source_locator,'')=COALESCE(?, '') ORDER BY id LIMIT 1", (digest, source_locator)).fetchone()
        if row:
            aid = int(row['id'])
            provenance_cases = {r['case_id'] for r in db.execute('''
                SELECT s.case_id FROM artifacts a JOIN sources s ON s.id=a.source_id WHERE a.id=?
                UNION SELECT s.case_id FROM source_observations o JOIN sources s ON s.id=o.source_id WHERE o.artifact_id=?
                ''', (aid, aid))}
            if not provenance_cases or provenance_cases != {source['case_id']}:
                raise IntegrityError('same content/locator belongs to another or unknown case; explicit migration is required')
            stored_artifact = db.execute('SELECT stored_path,size_bytes,is_raw,parent_artifact_id FROM artifacts WHERE id=?', (aid,)).fetchone()
            if bool(stored_artifact['is_raw']) != bool(is_raw):
                raise IntegrityError('raw and derived acquisitions cannot silently share an artifact identity')
            if parent_artifact_id is not None and stored_artifact['parent_artifact_id'] != parent_artifact_id:
                raise IntegrityError('same content/locator was acquired under a different parent artifact')
            existing_path = stored_artifact['stored_path']
            if not existing_path or Path(existing_path).is_symlink() or not Path(existing_path).is_file() or sha256_file(Path(existing_path)) != digest:
                raise IntegrityError('existing artifact content is unavailable or failed SHA-256 verification')
            if stored_artifact['size_bytes'] is not None and stored_artifact['size_bytes'] != size:
                raise IntegrityError('existing artifact size disagrees with captured bytes')
        else:
            cur = db.execute("""INSERT INTO artifacts(source_id,sha256,original_name,mime_type,size_bytes,source_locator,stored_path,is_raw,metadata_json,parent_artifact_id)
                                VALUES(?,?,?,?,?,?,?,?,?,?)""", (source_id,digest,name,mime,size,source_locator,str(stored),1 if is_raw else 0,metadata_json,parent_artifact_id))
            aid = int(cur.lastrowid)
            db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES ('artifact',?,?,?)", (aid,name,metadata_json))
        observation_metadata = {
            'source_metadata': metadata or {},
            'filesystem_stat_scope': STAT_SCOPES[scope],
            'content_claim': 'captured_bytes_only_not_truth_or_authorship',
        }
        if scope == 'path':
            before, after = source_stat_before, source_stat_after
            source_modified_at = datetime.fromtimestamp(before.st_mtime, timezone.utc).isoformat()
            observation_metadata.update({
                'filesystem_stat': {'mtime_ns': before.st_mtime_ns, 'ctime_ns': before.st_ctime_ns, 'size_bytes': before.st_size},
                'filesystem_stat_after_capture': {'mtime_ns': after.st_mtime_ns, 'ctime_ns': after.st_ctime_ns, 'size_bytes': after.st_size},
                'filesystem_changed_during_capture': (before.st_mtime_ns,before.st_ctime_ns,before.st_size) != (after.st_mtime_ns,after.st_ctime_ns,after.st_size),
                'source_snapshot_consistency': 'not_guaranteed_without_source_lock_or_snapshot',
                'filesystem_time_semantics': 'mtime=filesystem_modification; ctime=filesystem_status_change_not_creation',
            })
        elif scope == 'temporary_copy':
            source_modified_at = None
            observation_metadata.update({
                'filesystem_stat': None,
                'filesystem_time_semantics': 'not recorded: the path was a server-side temporary copy whose times describe the copy, not the source',
            })
        else:
            source_modified_at = declared
            observation_metadata.update({
                'filesystem_stat': None,
                'source_modified_at_basis': declared_time_basis.strip(),
                'filesystem_time_semantics': 'source_modified_at declared by the acquiring code; see source_modified_at_basis',
            })
        observation = db.execute('''INSERT INTO source_observations(
            artifact_id,source_id,source_locator,observed_at,acquisition_kind,
            original_name,mime_type,size_bytes,sha256,source_modified_at,metadata_json,collection_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (aid, source_id, source_locator, observed_at, 'ingest', name, mime, size, digest,
             source_modified_at, json.dumps(observation_metadata, ensure_ascii=False), collection_id))
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('ingest','artifact',?,?)", (aid,json.dumps({'sha256':digest,'source_locator':source_locator,'source_id':source_id,'observation_id':observation.lastrowid,'collection_id':collection_id,'deduplicated':row is not None,**({'parent_artifact_id':parent_artifact_id} if parent_artifact_id is not None else {})},ensure_ascii=False)))
        return aid

def add_event(*, source_id: int, artifact_id: int | None, event_type: str, ts_start: str | None = None, ts_end: str | None = None, direction: str | None = None, sender: str | None = None, recipients: str | None = None, contact_label: str | None = None, phone_or_address: str | None = None, subject: str | None = None, body: str | None = None, external_id: str | None = None, thread_id: str | None = None, confidence: float | None = None, metadata: dict | None = None, db: sqlite3.Connection | None = None) -> int:
    """Insert or re-observe one event; an optional caller transaction batches writes.

    The caller owns BEGIN/commit/rollback when it supplies ``db``.
    """
    values = (artifact_id,source_id,event_type,ts_start,ts_end,direction,sender,recipients,contact_label,phone_or_address,subject,body,external_id,thread_id,confidence,json.dumps(metadata or {},ensure_ascii=False,sort_keys=True))
    columns = ('artifact_id','source_id','event_type','ts_start','ts_end','direction','sender','recipients','contact_label','phone_or_address','subject','body','external_id','thread_id','confidence','metadata_json')
    def insert(connection: sqlite3.Connection) -> int:
        source = connection.execute('SELECT case_id FROM sources WHERE id=?', (source_id,)).fetchone()
        if source is None:
            raise ValueError('source does not exist')
        if artifact_id is not None:
            provenance_cases = {r['case_id'] for r in connection.execute('''
                SELECT s.case_id FROM artifacts a JOIN sources s ON s.id=a.source_id WHERE a.id=?
                UNION SELECT s.case_id FROM source_observations o JOIN sources s ON s.id=o.source_id WHERE o.artifact_id=?
                ''', (artifact_id, artifact_id))}
            if provenance_cases != {source['case_id']}:
                raise IntegrityError('event source and artifact must belong to the same known case')
        if external_id is not None:
            existing = connection.execute('SELECT * FROM events WHERE artifact_id IS ? AND source_id=? AND event_type=? AND external_id=? ORDER BY id LIMIT 1', (artifact_id,source_id,event_type,external_id)).fetchone()
            if existing:
                # Equality of parsed values avoids silently accepting parser-ID
                # collisions or a later reinterpretation under the same version.
                if any(existing[column] != value for column, value in zip(columns, values)):
                    raise IntegrityError('event external_id conflicts with existing payload; retain a separate parser version')
                eid = int(existing['id'])
                connection.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('event_reobserved','event',?,?)", (eid,json.dumps({'artifact_id':artifact_id,'source_id':source_id,'external_id':external_id},ensure_ascii=False)))
                return eid
        cur = connection.execute("""INSERT INTO events(artifact_id,source_id,event_type,ts_start,ts_end,direction,sender,recipients,contact_label,phone_or_address,subject,body,external_id,thread_id,confidence,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
        eid = int(cur.lastrowid)
        title = ' | '.join(x for x in [event_type, contact_label, phone_or_address, subject] if x)
        connection.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES ('event',?,?,?)", (eid,title,body or ''))
        connection.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('event_imported','event',?,?)", (eid,json.dumps({'artifact_id':artifact_id,'source_id':source_id,'external_id':external_id},ensure_ascii=False)))
        return eid
    if db is not None:
        return insert(db)
    with session() as connection:
        connection.execute('BEGIN IMMEDIATE')
        return insert(connection)

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
