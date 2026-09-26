from __future__ import annotations
import json, time, traceback
from datetime import datetime, timezone
from .config import settings
from .db import init_db, session
from .evidence import add_derived_text

def claim_job():
    with session() as db:
        row=db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY priority DESC,id LIMIT 1").fetchone()
        if not row:return None
        db.execute("UPDATE jobs SET status='running',attempts=attempts+1,started_at=CURRENT_TIMESTAMP WHERE id=?",(row['id'],))
        return dict(row)

def run_transcribe(job):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError('Install requirements-whisper.txt to enable transcription') from e
    with session() as db:
        art=db.execute("SELECT * FROM artifacts WHERE id=?",(job['artifact_id'],)).fetchone()
        if not art: raise RuntimeError('artifact missing')
        path=art['stored_path']
    model=WhisperModel(settings.whisper_model,device=settings.whisper_device,compute_type=settings.whisper_compute_type)
    segments,info=model.transcribe(path,word_timestamps=True,vad_filter=True)
    out=[]; texts=[]
    for s in segments:
        words=[{'start':w.start,'end':w.end,'word':w.word,'probability':w.probability} for w in (s.words or [])]
        out.append({'start':s.start,'end':s.end,'text':s.text,'words':words})
        texts.append(s.text.strip())
    add_derived_text(int(job['artifact_id']),'transcript',' '.join(texts),model=f'faster-whisper:{settings.whisper_model}',language=info.language,segments=out,confidence=getattr(info,'language_probability',None))

def finish(job_id,status,error=None):
    with session() as db:
        db.execute("UPDATE jobs SET status=?,error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",(status,error,job_id))

def main():
    init_db()
    print('Evidence Workbench worker started')
    while True:
        job=claim_job()
        if not job:
            time.sleep(2); continue
        try:
            if job['kind']=='transcribe': run_transcribe(job)
            else: raise RuntimeError(f"unknown job kind {job['kind']}")
            finish(job['id'],'done')
        except Exception as e:
            finish(job['id'],'failed',f'{e}\n{traceback.format_exc()}')

if __name__=='__main__': main()
