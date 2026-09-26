from __future__ import annotations
import json
from .db import session

def queue_transcription(artifact_id: int, priority: int = 0) -> int:
    with session() as db:
        existing=db.execute("SELECT id FROM jobs WHERE kind='transcribe' AND artifact_id=? AND status IN ('queued','running')",(artifact_id,)).fetchone()
        if existing:return int(existing['id'])
        cur=db.execute("INSERT INTO jobs(kind,artifact_id,priority) VALUES ('transcribe',?,?)",(artifact_id,priority))
        return int(cur.lastrowid)

def queue_all_audio() -> int:
    with session() as db:
        ids=[r['id'] for r in db.execute("SELECT id FROM artifacts WHERE mime_type LIKE 'audio/%' OR mime_type LIKE 'video/%'")]
    for aid in ids: queue_transcription(int(aid))
    return len(ids)
