from __future__ import annotations
import csv, io, json, shutil, tempfile
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .db import init_db, session
from .evidence import ensure_source, ingest_file
from .importers.sms_backup import import_sms_backup
from .importers.whatsapp import import_whatsapp_txt
from .search import search as fts_search
from .transcription import queue_all_audio, queue_transcription

app=FastAPI(title='Evidence Workbench',version='0.1.0')
app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')
templates=Jinja2Templates(directory=Path(__file__).parent/'templates')

@app.on_event('startup')
def startup(): init_db()

def dashboard_counts():
    with session() as db:
        return {k:db.execute(sql).fetchone()[0] for k,sql in {
            'artifacts':'SELECT count(*) FROM artifacts','events':'SELECT count(*) FROM events','transcripts':"SELECT count(*) FROM derived_text WHERE kind='transcript'",'annotations':'SELECT count(*) FROM annotations','queued':"SELECT count(*) FROM jobs WHERE status='queued'",'failed':"SELECT count(*) FROM jobs WHERE status='failed'"}.items()}

@app.get('/',response_class=HTMLResponse)
def home(request:Request,q:str='',kind:str=''):
    with session() as db:
        recent=[dict(r) for r in db.execute("SELECT id,original_name,mime_type,size_bytes,sha256,created_at FROM artifacts ORDER BY id DESC LIMIT 30")]
        events=[dict(r) for r in db.execute("SELECT id,event_type,ts_start,direction,contact_label,phone_or_address,subject,substr(body,1,240) body FROM events ORDER BY COALESCE(ts_start,created_at) DESC LIMIT 30")]
    results=fts_search(q,100) if q else []
    return templates.TemplateResponse(request,'index.html',{'counts':dashboard_counts(),'recent':recent,'events':events,'results':results,'q':q})

@app.post('/upload')
async def upload(file:UploadFile=File(...),source_label:str=Form('Manual upload')):
    suffix=Path(file.filename or '').suffix
    with tempfile.NamedTemporaryFile(delete=False,suffix=suffix) as tmp:
        shutil.copyfileobj(file.file,tmp); p=Path(tmp.name)
    try:
        src=ensure_source('manual_upload',source_label)
        aid=ingest_file(p,source_id=src,source_locator=f'upload:{file.filename}',original_name=file.filename,mime_type=file.content_type)
        return {'ok':True,'artifact_id':aid}
    finally:p.unlink(missing_ok=True)

@app.post('/import/sms-backup')
async def import_sms(file:UploadFile=File(...)):
    with tempfile.NamedTemporaryFile(delete=False,suffix='.xml') as tmp:
        shutil.copyfileobj(file.file,tmp); p=Path(tmp.name)
    try:return import_sms_backup(p)
    finally:p.unlink(missing_ok=True)

@app.post('/import/whatsapp')
async def import_wa(file:UploadFile=File(...),chat_label:str=Form('')):
    with tempfile.NamedTemporaryFile(delete=False,suffix='.txt') as tmp:
        shutil.copyfileobj(file.file,tmp); p=Path(tmp.name)
    try:return import_whatsapp_txt(p,chat_label or None)
    finally:p.unlink(missing_ok=True)

@app.get('/artifact/{artifact_id}',response_class=HTMLResponse)
def artifact(request:Request,artifact_id:int):
    with session() as db:
        art=db.execute("SELECT a.*,s.kind source_kind,s.label source_label FROM artifacts a LEFT JOIN sources s ON s.id=a.source_id WHERE a.id=?",(artifact_id,)).fetchone()
        if not art:raise HTTPException(404)
        texts=[dict(r) for r in db.execute("SELECT * FROM derived_text WHERE artifact_id=? ORDER BY id DESC",(artifact_id,))]
        anns=[dict(r) for r in db.execute("SELECT * FROM annotations WHERE artifact_id=? ORDER BY id DESC",(artifact_id,))]
        events=[dict(r) for r in db.execute("SELECT * FROM events WHERE artifact_id=? ORDER BY ts_start",(artifact_id,))]
    return templates.TemplateResponse(request,'artifact.html',{'a':dict(art),'texts':texts,'annotations':anns,'events':events})

@app.post('/artifact/{artifact_id}/annotate')
def annotate(artifact_id:int,body:str=Form(...),label:str=Form(''),kind:str=Form('note'),start_ms:int|None=Form(None),end_ms:int|None=Form(None)):
    with session() as db:
        if not db.execute("SELECT 1 FROM artifacts WHERE id=?",(artifact_id,)).fetchone():raise HTTPException(404)
        cur=db.execute("INSERT INTO annotations(artifact_id,kind,label,body,start_ms,end_ms) VALUES (?,?,?,?,?,?)",(artifact_id,kind,label or None,body,start_ms,end_ms))
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('annotate','annotation',?,?)",(cur.lastrowid,json.dumps({'artifact_id':artifact_id})))
    return {'ok':True,'annotation_id':cur.lastrowid}

@app.post('/artifact/{artifact_id}/transcribe')
def transcribe(artifact_id:int): return {'job_id':queue_transcription(artifact_id)}

@app.post('/transcribe/all')
def transcribe_all(): return {'queued_artifacts':queue_all_audio()}

@app.get('/api/search')
def api_search(q:str): return fts_search(q,200)

@app.get('/api/timeline')
def timeline(limit:int=500,phone:str|None=None,event_type:str|None=None):
    clauses=[];args=[]
    if phone:clauses.append('phone_or_address=?');args.append(phone)
    if event_type:clauses.append('event_type=?');args.append(event_type)
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    with session() as db:
        return [dict(r) for r in db.execute(f"SELECT * FROM events{where} ORDER BY COALESCE(ts_start,created_at) DESC LIMIT ?",(*args,limit))]

@app.get('/export/events.csv')
def export_events_csv():
    with session() as db: rows=[dict(r) for r in db.execute("SELECT * FROM events ORDER BY ts_start")]
    buf=io.StringIO();
    if rows:
        w=csv.DictWriter(buf,fieldnames=rows[0].keys());w.writeheader();w.writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=events.csv'})

@app.get('/export/manifest.json')
def export_manifest():
    with session() as db:
        arts=[dict(r) for r in db.execute("SELECT id,sha256,original_name,mime_type,size_bytes,source_locator,source_created_at,source_modified_at,captured_at,is_raw,metadata_json,created_at FROM artifacts ORDER BY id")]
        audit=[dict(r) for r in db.execute("SELECT * FROM audit_log ORDER BY id")]
    return JSONResponse({'artifacts':arts,'audit_log':audit})

# --- Mobile / cross-platform JSON API ---------------------------------------
from pydantic import BaseModel

class AnnotationIn(BaseModel):
    body: str
    label: str | None = None
    kind: str = 'note'
    startMs: int | None = None
    endMs: int | None = None

@app.get('/api/artifacts')
def api_artifacts(limit:int=500):
    with session() as db:
        rows=db.execute("""
            SELECT a.id,a.original_name,a.mime_type,a.size_bytes,a.sha256,a.created_at,a.source_locator,
              CASE
                WHEN EXISTS(SELECT 1 FROM derived_text d WHERE d.artifact_id=a.id AND d.kind='transcript') THEN 'done'
                WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' AND j.status='running') THEN 'running'
                WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' AND j.status='queued') THEN 'queued'
                WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' AND j.status='failed') THEN 'failed'
                ELSE 'none'
              END transcript_status,
              (SELECT language FROM derived_text d WHERE d.artifact_id=a.id AND d.kind='transcript' ORDER BY d.id DESC LIMIT 1) transcript_language,
              (SELECT substr(text,1,400) FROM derived_text d WHERE d.artifact_id=a.id AND d.kind='transcript' ORDER BY d.id DESC LIMIT 1) transcript_preview,
              (SELECT priority FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' ORDER BY j.id DESC LIMIT 1) queued_priority
            FROM artifacts a
            WHERE a.mime_type LIKE 'audio/%' OR a.mime_type LIKE 'video/%'
            ORDER BY COALESCE(queued_priority,-1) DESC,a.id DESC LIMIT ?
        """,(limit,)).fetchall()
        out=[]
        for r in rows:
            d=dict(r)
            out.append({
                'id':d['id'],'originalName':d['original_name'],'mimeType':d['mime_type'],'sizeBytes':d['size_bytes'],
                'sha256':d['sha256'],'createdAt':d['created_at'],'sourceLocator':d['source_locator'],
                'transcriptStatus':d['transcript_status'],'transcriptLanguage':d['transcript_language'],
                'transcriptPreview':d['transcript_preview'],'queuedPriority':d['queued_priority'],'tags':[]
            })
        return out

@app.post('/api/artifacts/upload')
async def api_upload_artifact(file:UploadFile=File(...),source_label:str=Form('AGEDS client')):
    suffix=Path(file.filename or '').suffix
    with tempfile.NamedTemporaryFile(delete=False,suffix=suffix) as tmp:
        shutil.copyfileobj(file.file,tmp); p=Path(tmp.name)
    try:
        src=ensure_source('client_upload',source_label)
        aid=ingest_file(p,source_id=src,source_locator=f'client-upload:{file.filename}',original_name=file.filename,mime_type=file.content_type)
        with session() as db:
            sha=db.execute('SELECT sha256 FROM artifacts WHERE id=?',(aid,)).fetchone()['sha256']
        return {'ok':True,'artifact_id':aid,'sha256':sha}
    finally:p.unlink(missing_ok=True)

@app.post('/api/artifacts/{artifact_id}/transcribe')
def api_queue_transcription(artifact_id:int,priority:int=0):
    return {'job_id':queue_transcription(artifact_id,priority)}

@app.get('/api/artifacts/{artifact_id}/transcript')
def api_transcript(artifact_id:int):
    with session() as db:
        r=db.execute("SELECT * FROM derived_text WHERE artifact_id=? AND kind='transcript' ORDER BY id DESC LIMIT 1",(artifact_id,)).fetchone()
        if not r: raise HTTPException(404,'transcript not ready')
        return {
            'id':r['id'],'artifactId':r['artifact_id'],'model':r['model'],'language':r['language'],'text':r['text'],
            'segments':json.loads(r['segments_json'] or '[]'),'confidence':r['confidence'],'createdAt':r['created_at']
        }

@app.get('/api/artifacts/{artifact_id}/annotations')
def api_annotations(artifact_id:int):
    with session() as db:
        rows=db.execute("SELECT * FROM annotations WHERE artifact_id=? ORDER BY id DESC",(artifact_id,)).fetchall()
        return [{
            'id':r['id'],'artifactId':r['artifact_id'],'kind':r['kind'],'label':r['label'],'body':r['body'],
            'startMs':r['start_ms'],'endMs':r['end_ms'],'createdAt':r['created_at']
        } for r in rows]

@app.post('/api/artifacts/{artifact_id}/annotations')
def api_annotation_create(artifact_id:int,a:AnnotationIn):
    with session() as db:
        if not db.execute('SELECT 1 FROM artifacts WHERE id=?',(artifact_id,)).fetchone(): raise HTTPException(404)
        cur=db.execute("INSERT INTO annotations(artifact_id,kind,label,body,start_ms,end_ms) VALUES (?,?,?,?,?,?)",
                       (artifact_id,a.kind,a.label,a.body,a.startMs,a.endMs))
        aid=int(cur.lastrowid)
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES ('annotate','annotation',?,?)",
                   (aid,json.dumps({'artifact_id':artifact_id,'client':'json-api'})))
        return {'id':aid,'artifactId':artifact_id,'kind':a.kind,'label':a.label,'body':a.body,'startMs':a.startMs,'endMs':a.endMs,'createdAt':None}
