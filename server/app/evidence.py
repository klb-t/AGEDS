from __future__ import annotations
import hashlib, json, mimetypes, os, shutil
from pathlib import Path
from typing import BinaryIO
from .config import settings
from .db import session

CHUNK = 1024 * 1024

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()

def store_file(path: Path) -> tuple[str, Path, int]:
    digest = sha256_file(path)
    suffix = ''.join(path.suffixes)[-20:]
    dst_dir = settings.store_dir / digest[:2] / digest[2:4]
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{digest}{suffix}"
    if not dst.exists():
        shutil.copy2(path, dst)
    return digest, dst, path.stat().st_size

def ensure_source(kind: str, label: str, locator: str | None = None, account: str | None = None, metadata: dict | None = None) -> int:
    with session() as db:
        row = db.execute("SELECT id FROM sources WHERE kind=? AND label=? AND COALESCE(locator,'')=COALESCE(?, '') ORDER BY id LIMIT 1", (kind, label, locator)).fetchone()
        if row:
            return int(row['id'])
        case_id = int(db.execute("SELECT id FROM cases ORDER BY id LIMIT 1").fetchone()['id'])
        cur = db.execute("INSERT INTO sources(case_id,kind,label,locator,account,metadata_json) VALUES (?,?,?,?,?,?)", (case_id,kind,label,locator,account,json.dumps(metadata or {},ensure_ascii=False)))
        return int(cur.lastrowid)

def ingest_file(path: Path, *, source_id: int, source_locator: str | None = None, original_name: str | None = None, mime_type: str | None = None, metadata: dict | None = None, is_raw: bool = True) -> int:
    digest, stored, size = store_file(path)
    name = original_name or path.name
    mime = mime_type or mimetypes.guess_type(name)[0] or 'application/octet-stream'
    with session() as db:
        row = db.execute("SELECT id FROM artifacts WHERE sha256=? AND COALESCE(source_locator,'')=COALESCE(?, '')", (digest, source_locator)).fetchone()
        if row:
            return int(row['id'])
        cur = db.execute("""INSERT INTO artifacts(source_id,sha256,original_name,mime_type,size_bytes,source_locator,stored_path,is_raw,metadata_json)
                            VALUES(?,?,?,?,?,?,?,?,?)""", (source_id,digest,name,mime,size,source_locator,str(stored),1 if is_raw else 0,json.dumps(metadata or {},ensure_ascii=False)))
        aid = int(cur.lastrowid)
        db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES ('artifact',?,?,?)", (aid,name,json.dumps(metadata or {},ensure_ascii=False)))
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('ingest','artifact',?,?)", (aid,json.dumps({'sha256':digest,'source_locator':source_locator},ensure_ascii=False)))
        return aid

def add_event(*, source_id: int, artifact_id: int | None, event_type: str, ts_start: str | None = None, ts_end: str | None = None, direction: str | None = None, sender: str | None = None, recipients: str | None = None, contact_label: str | None = None, phone_or_address: str | None = None, subject: str | None = None, body: str | None = None, external_id: str | None = None, thread_id: str | None = None, confidence: float | None = None, metadata: dict | None = None) -> int:
    with session() as db:
        cur = db.execute("""INSERT INTO events(artifact_id,source_id,event_type,ts_start,ts_end,direction,sender,recipients,contact_label,phone_or_address,subject,body,external_id,thread_id,confidence,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (artifact_id,source_id,event_type,ts_start,ts_end,direction,sender,recipients,contact_label,phone_or_address,subject,body,external_id,thread_id,confidence,json.dumps(metadata or {},ensure_ascii=False)))
        eid = int(cur.lastrowid)
        title = ' | '.join(x for x in [event_type, contact_label, phone_or_address, subject] if x)
        db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES ('event',?,?,?)", (eid,title,body or ''))
        return eid

def add_derived_text(artifact_id: int, kind: str, text: str, *, model: str | None = None, language: str | None = None, segments: list | None = None, confidence: float | None = None) -> int:
    with session() as db:
        cur = db.execute("INSERT INTO derived_text(artifact_id,kind,model,language,text,segments_json,confidence) VALUES (?,?,?,?,?,?,?)", (artifact_id,kind,model,language,text,json.dumps(segments or [],ensure_ascii=False),confidence))
        did = int(cur.lastrowid)
        name = db.execute("SELECT original_name FROM artifacts WHERE id=?",(artifact_id,)).fetchone()['original_name']
        db.execute("INSERT INTO search_fts(object_kind,object_id,title,content) VALUES ('derived_text',?,?,?)", (did,f"{kind}: {name}",text))
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('derive_text','derived_text',?,?)", (did,json.dumps({'artifact_id':artifact_id,'kind':kind,'model':model},ensure_ascii=False)))
        return did
