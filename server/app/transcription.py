from __future__ import annotations
import json
from .db import session

def queue_transcription(artifact_id: int, priority: int | None = None) -> int:
    with session() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM artifacts WHERE id=?", (artifact_id,)).fetchone():
            raise ValueError(f"artifact {artifact_id} missing")
        existing=db.execute("SELECT id FROM jobs WHERE kind='transcribe' AND artifact_id=? AND status IN ('queued','running')",(artifact_id,)).fetchone()
        if existing:
            job_id = int(existing['id'])
            previous = db.execute("SELECT priority FROM jobs WHERE id=?", (job_id,)).fetchone()['priority']
            if priority is not None and previous != priority:
                db.execute("UPDATE jobs SET priority=? WHERE id=?", (priority, job_id))
                db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('job_reprioritized','job',?,?)", (job_id,json.dumps({'previous_priority':previous,'priority':priority})))
            return job_id
        effective_priority = 0 if priority is None else priority
        cur=db.execute("INSERT INTO jobs(kind,artifact_id,priority) VALUES ('transcribe',?,?)",(artifact_id,effective_priority))
        job_id=int(cur.lastrowid)
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('job_enqueued','job',?,?)", (job_id,json.dumps({'artifact_id':artifact_id,'kind':'transcribe','priority':effective_priority})))
        return job_id

def queue_all_audio() -> int:
    with session() as db:
        ids=[r['id'] for r in db.execute("SELECT id FROM artifacts WHERE mime_type LIKE 'audio/%' OR mime_type LIKE 'video/%'")]
    for aid in ids: queue_transcription(int(aid))
    return len(ids)
