from __future__ import annotations
import csv, io, json, shutil, sqlite3, tempfile
from pathlib import Path
from typing import Annotated
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from .db import init_db, session
from .config import settings
from .evidence import IntegrityError, ensure_source, ingest_file
from .citations import anchor_payload, create_citation, loads_transcript_segments, milliseconds
from .importers.sms_backup import import_sms_backup
from .importers.whatsapp import import_whatsapp_txt
from .search import SearchQueryError, SearchWorkLimitError, search_page
from .read_pages import PageInputError, PageNotFound, PageStoredError, PageLimitError, read_page
from .verified_media import MediaUnavailable, MediaIntegrityError, MediaLimitError, verified_media_response
from .transcription import queue_all_audio, queue_transcription, word_timing_capabilities

app=FastAPI(title='AGEDS Evidence Workbench',version='0.2.0')
app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')
templates=Jinja2Templates(directory=Path(__file__).parent/'templates')
MAX_SQL_INTEGER = 2**63 - 1
SqlPathId = Annotated[int, PathParameter(ge=1, le=MAX_SQL_INTEGER)]
StrictSqlId = Annotated[StrictInt, Field(ge=1, le=MAX_SQL_INTEGER)]
StrictMilliseconds = Annotated[StrictInt, Field(ge=0, le=MAX_SQL_INTEGER)]

@app.on_event('startup')
def startup(): init_db()

@app.exception_handler(IntegrityError)
async def integrity_error(request: Request, exc: IntegrityError):
    return JSONResponse(status_code=409, content={'detail': str(exc)})

def _json_object(value: str) -> dict:
    def invalid_constant(constant):
        raise ValueError(f'nonfinite JSON constant: {constant}')
    try:
        result = json.loads(value, parse_constant=invalid_constant)
        if not isinstance(result, dict):
            raise ValueError('metadata must be a JSON object')
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return result
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HTTPException(422, str(exc)) from exc

def _annotation_payload(r) -> dict:
    return {'id':r['id'],'artifactId':r['artifact_id'],'kind':r['kind'],'label':r['label'],'body':r['body'],
            'startMs':r['start_ms'],'endMs':r['end_ms'],'createdAt':r['created_at'],
            'derivedTextId':r['derived_text_id']}

def _save_annotation(artifact_id: int, a) -> dict:
    if not a.body.strip():
        raise HTTPException(422, 'annotation body cannot be blank')
    if (a.startMs is None) != (a.endMs is None):
        raise HTTPException(422, 'both startMs and endMs are required for a range')
    if a.startMs is not None and (a.startMs < 0 or a.endMs < a.startMs):
        raise HTTPException(422, 'invalid annotation range')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM artifacts WHERE id=?',(artifact_id,)).fetchone():
            raise HTTPException(404, 'artifact not found')
        if a.derivedTextId is not None:
            version = db.execute("SELECT segments_json FROM derived_text WHERE id=? AND artifact_id=? AND kind='transcript'",
                                 (a.derivedTextId, artifact_id)).fetchone()
            if not version:
                raise HTTPException(422, 'transcript version does not belong to artifact')
            if a.startMs is not None:
                try:
                    segments = json.loads(version['segments_json'])
                    bounds = [s['end'] for s in segments]
                    if not bounds:
                        raise ValueError('unknown transcript time bounds')
                    end_bound = max(milliseconds(t) for t in bounds)
                    if a.endMs > end_bound:
                        raise ValueError('annotation range outside transcript')
                except (ValueError, TypeError, KeyError) as exc:
                    raise HTTPException(422, str(exc)) from exc
        elif a.startMs is not None:
            raise HTTPException(422, 'a timed annotation requires a concrete transcript version')
        cur = db.execute('''INSERT INTO annotations(artifact_id,derived_text_id,kind,label,body,start_ms,end_ms)
                            VALUES(?,?,?,?,?,?,?)''',
                         (artifact_id,a.derivedTextId,a.kind,a.label,a.body,a.startMs,a.endMs))
        annotation_id = int(cur.lastrowid)
        db.execute("INSERT INTO audit_log(action,object_kind,object_id,details_json) VALUES('annotate','annotation',?,?)",
                   (annotation_id,json.dumps({'artifact_id':artifact_id,'derived_text_id':a.derivedTextId})))
        return _annotation_payload(db.execute('SELECT * FROM annotations WHERE id=?',(annotation_id,)).fetchone())

def dashboard_counts():
    with session() as db:
        return {k:db.execute(sql).fetchone()[0] for k,sql in {
            'artifacts':'SELECT count(*) FROM artifacts','events':'SELECT count(*) FROM events','transcripts':"SELECT count(*) FROM derived_text WHERE kind='transcript'",'annotations':'SELECT count(*) FROM annotations','queued':"SELECT count(*) FROM jobs WHERE status='queued'",'failed':"SELECT count(*) FROM jobs WHERE status='failed'"}.items()}

@app.get('/',response_class=HTMLResponse)
def home(request:Request,q:str='',kind:str=''):
    with session() as db:
        recent=[dict(r) for r in db.execute("SELECT id,original_name,mime_type,size_bytes,sha256,created_at FROM artifacts ORDER BY id DESC LIMIT 30")]
        events=[dict(r) for r in db.execute("SELECT id,event_type,ts_start,direction,contact_label,phone_or_address,subject,substr(body,1,240) body FROM events ORDER BY COALESCE(ts_start,created_at) DESC LIMIT 30")]
    search_error = None
    search_status = 200
    page = {'results': [], 'has_more': False, 'complete': True, 'limit': 100}
    try:
        page = search_page(q, 100)
    except SearchQueryError as exc:
        search_error, search_status = str(exc), 422
    except SearchWorkLimitError:
        search_error, search_status = 'Przekroczono limit pracy wyszukiwania. Zawęź zapytanie.', 503
    return templates.TemplateResponse(request,'index.html',{
        'counts':dashboard_counts(),'recent':recent,'events':events,
        'results':page['results'],'q':q,'search_error':search_error,
        'search_has_more':page['has_more'],'search_limit':page['limit']}, status_code=search_status)

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
    try:return import_sms_backup(p,source_locator=f'upload:sms-backup:{file.filename}',original_name=file.filename)
    finally:p.unlink(missing_ok=True)

@app.post('/import/whatsapp')
async def import_wa(file:UploadFile=File(...),chat_label:str=Form('')):
    with tempfile.NamedTemporaryFile(delete=False,suffix='.txt') as tmp:
        shutil.copyfileobj(file.file,tmp); p=Path(tmp.name)
    try:return import_whatsapp_txt(p,chat_label or None,source_locator=f'upload:whatsapp:{file.filename}',original_name=file.filename)
    finally:p.unlink(missing_ok=True)

@app.get('/artifact/{artifact_id}',response_class=HTMLResponse)
def artifact(request:Request,artifact_id:SqlPathId):
    with session() as db:
        art=db.execute("SELECT a.*,s.kind source_kind,s.label source_label FROM artifacts a LEFT JOIN sources s ON s.id=a.source_id WHERE a.id=?",(artifact_id,)).fetchone()
        if not art:raise HTTPException(404)
        previews=[dict(r) for r in db.execute("SELECT id,kind,model,language,substr(text,1,4096) AS text,length(text)>4096 AS text_truncated FROM derived_text WHERE artifact_id=? AND kind!='transcript' ORDER BY id DESC LIMIT 51",(artifact_id,))]
        events=[dict(r) for r in db.execute("SELECT id,ts_start,event_type,direction,contact_label,phone_or_address,substr(body,1,4096) AS body,substr(subject,1,4096) AS subject FROM events WHERE artifact_id=? ORDER BY ts_start LIMIT 101",(artifact_id,))]
    version_page = _read_page_http(artifact_id, 'transcripts')
    citation_page = _read_page_http(artifact_id, 'citations')
    annotation_page = _read_page_http(artifact_id, 'annotations')
    return templates.TemplateResponse(request,'artifact.html',{
        'a':dict(art),'texts':previews[:50],'text_previews_more':len(previews)>50,
        'annotations':annotation_page['items'],'events':events[:100],'events_more':len(events)>100,
        'citations':citation_page['items'],'version_page':version_page,
        'citation_page':citation_page,'annotation_page':annotation_page})


@app.post('/artifact/{artifact_id}/annotate')
def annotate(artifact_id:SqlPathId,body:str=Form(...),label:str=Form(''),kind:str=Form('note'),start_ms:int|None=Form(None,ge=0,le=MAX_SQL_INTEGER),end_ms:int|None=Form(None,ge=0,le=MAX_SQL_INTEGER),derived_text_id:str|None=Form(None)):
    try:
        version = int(derived_text_id) if derived_text_id and derived_text_id.strip() else None
        annotation = AnnotationIn(body=body,label=label or None,kind=kind,startMs=start_ms,endMs=end_ms,derivedTextId=version)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = _save_annotation(artifact_id, annotation)
    return {'ok':True,'annotation_id':result['id']}

@app.post('/artifact/{artifact_id}/transcribe')
def transcribe(artifact_id:SqlPathId): return api_queue_transcription(artifact_id, None)

@app.post('/transcribe/all')
def transcribe_all(): return {'queued_artifacts':queue_all_audio()}

@app.get('/api/search')
def api_search(q:str):
    try:
        page = search_page(q, 200)
    except SearchQueryError as exc:
        raise HTTPException(422, str(exc)) from exc
    except SearchWorkLimitError as exc:
        raise HTTPException(503, 'Search work limit exceeded; narrow the query.') from exc
    return JSONResponse(page['results'], headers={
        'X-AGEDS-Search-Limit': str(page['limit']),
        'X-AGEDS-Search-Has-More': str(page['has_more']).lower(),
        'X-AGEDS-Search-Complete': str(page['complete']).lower(),
    })

@app.get('/api/timeline')
def timeline(limit:int=Query(500,ge=1,le=10000),phone:str|None=None,event_type:str|None=None):
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
    from .packages import export_metadata_package
    return JSONResponse(export_metadata_package())

@app.post('/api/packages/validate')
def api_validate_package(package: dict):
    from .packages import validate_metadata_package
    return validate_metadata_package(package)

# --- Mobile / cross-platform JSON API ---------------------------------------
class AnnotationIn(BaseModel):
    body: str
    label: str | None = None
    kind: str = 'note'
    startMs: StrictMilliseconds | None = None
    endMs: StrictMilliseconds | None = None
    derivedTextId: StrictSqlId | None = None

class WordReferenceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    segment_index: Annotated[StrictInt, Field(ge=0)]
    word_index: Annotated[StrictInt, Field(ge=0)]

class CitationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    derivedTextId: StrictSqlId
    segmentIndices: list[StrictInt] | None = Field(default=None, max_length=10000)
    wordRefs: list[WordReferenceIn] | None = Field(default=None, max_length=10000)
    quoteText: str | None = Field(default=None, max_length=1000000)

class ScanIn(BaseModel):
    rootId: StrictInt
    relativePath: str = ''

@app.get('/api/artifacts')
def api_artifacts(limit:int=Query(500,ge=1,le=10000)):
    with session() as db:
        rows=db.execute("""
            SELECT a.id,a.original_name,a.mime_type,a.size_bytes,a.sha256,a.created_at,a.source_locator,
              CASE
                WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' AND j.status='running') THEN 'running'
                WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' AND j.status='queued') THEN 'queued'
                WHEN (SELECT status FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' ORDER BY j.id DESC LIMIT 1)='failed' THEN 'failed'
                WHEN EXISTS(SELECT 1 FROM derived_text d WHERE d.artifact_id=a.id AND d.kind='transcript') THEN 'done'
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
async def api_upload_artifact(file:UploadFile=File(...),source_label:str=Form('AGEDS client'),source_locator:str|None=Form(None),metadata_json:str=Form('{}')):
    client_metadata = _json_object(metadata_json)
    suffix=Path(file.filename or '').suffix
    with tempfile.NamedTemporaryFile(delete=False,suffix=suffix) as tmp:
        shutil.copyfileobj(file.file,tmp); p=Path(tmp.name)
    try:
        src=ensure_source('client_upload',source_label)
        aid=ingest_file(p,source_id=src,source_locator=source_locator or f'client-upload:{file.filename}',original_name=file.filename,mime_type=file.content_type,
                        metadata={'client_metadata':client_metadata,'client_metadata_status':'unverified',
                                  'acquisition_kind':'uploaded_stream','filesystem_stat_scope':'server_temporary_upload_not_original_source'})
        with session() as db:
            sha=db.execute('SELECT sha256 FROM artifacts WHERE id=?',(aid,)).fetchone()['sha256']
        return {'ok':True,'artifact_id':aid,'sha256':sha}
    finally:p.unlink(missing_ok=True)

@app.post('/api/artifacts/{artifact_id}/transcribe')
def api_queue_transcription(artifact_id:SqlPathId,priority:int|None=Query(None,ge=-MAX_SQL_INTEGER,le=MAX_SQL_INTEGER)):
    with session() as db:
        if not db.execute('SELECT 1 FROM artifacts WHERE id=?',(artifact_id,)).fetchone():
            raise HTTPException(404, 'artifact not found')
    try:
        return {'job_id':queue_transcription(artifact_id,priority)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@app.get('/api/artifacts/{artifact_id}/transcript')
def api_transcript(artifact_id:SqlPathId,derived_text_id:int|None=Query(None,ge=1,le=MAX_SQL_INTEGER)):
    with session() as db:
        if derived_text_id is None:
            r=db.execute("SELECT * FROM derived_text WHERE artifact_id=? AND kind='transcript' ORDER BY id DESC LIMIT 1",(artifact_id,)).fetchone()
        else:
            r=db.execute("SELECT * FROM derived_text WHERE artifact_id=? AND id=? AND kind='transcript'",(artifact_id,derived_text_id)).fetchone()
        if not r: raise HTTPException(404,'transcript not ready')
        run = db.execute('SELECT * FROM processing_runs WHERE id=?',(r['run_id'],)).fetchone() if r['run_id'] else None
        run_payload = dict(run) if run else None
        if run_payload:
            run_payload.pop('lease_token', None)
        try:
            segments = loads_transcript_segments(r['segments_json'] or '[]')
            metadata = json.loads(r['metadata_json'])
            if not isinstance(segments, list) or not isinstance(metadata, dict):
                raise ValueError('invalid stored projection types')
            payload = {
                'id':r['id'],'artifactId':r['artifact_id'],'model':r['model'],'language':r['language'],'text':r['text'],
                'segments':segments,'confidence':r['confidence'],'createdAt':r['created_at'],
                'runId':r['run_id'],'metadata':metadata,'run':run_payload,
                'provenanceStatus':'recorded_processing_run' if run else 'legacy_unknown',
                'wordTiming':word_timing_capabilities(segments)
            }
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            return payload
        except (ValueError, TypeError, UnicodeError) as exc:
            raise HTTPException(409, 'Stored transcript has malformed or nonfinite JSON; original values remain preserved in metadata export.') from exc


def _read_page_http(artifact_id, kind, *, limit=50, before_id=None, snapshot_max_id=None):
    try:
        return read_page(artifact_id, kind, limit=limit, before_id=before_id, snapshot_max_id=snapshot_max_id)
    except PageInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    except PageNotFound as exc:
        raise HTTPException(404, 'artifact not found') from exc
    except PageStoredError as exc:
        raise HTTPException(409, 'Stored list metadata is invalid; original records remain unchanged.') from exc
    except PageLimitError as exc:
        raise HTTPException(413, 'List page exceeds the read budget; original records remain unchanged.') from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, 'Evidence database is unavailable for read-only listing.') from exc

@app.get('/api/artifacts/{artifact_id}/transcripts/page')
def api_transcript_page(artifact_id:SqlPathId, limit:int=Query(50,ge=1,le=100),
                        before_id:int|None=Query(None,ge=1,le=MAX_SQL_INTEGER),
                        snapshot_max_id:int|None=Query(None,ge=0,le=MAX_SQL_INTEGER)):
    return _read_page_http(artifact_id,'transcripts',limit=limit,before_id=before_id,snapshot_max_id=snapshot_max_id)

@app.get('/api/artifacts/{artifact_id}/citations/page')
def api_citation_page(artifact_id:SqlPathId, limit:int=Query(50,ge=1,le=100),
                      before_id:int|None=Query(None,ge=1,le=MAX_SQL_INTEGER),
                      snapshot_max_id:int|None=Query(None,ge=0,le=MAX_SQL_INTEGER)):
    return _read_page_http(artifact_id,'citations',limit=limit,before_id=before_id,snapshot_max_id=snapshot_max_id)

@app.get('/api/artifacts/{artifact_id}/annotations/page')
def api_annotation_page(artifact_id:SqlPathId, limit:int=Query(50,ge=1,le=100),
                        before_id:int|None=Query(None,ge=1,le=MAX_SQL_INTEGER),
                        snapshot_max_id:int|None=Query(None,ge=0,le=MAX_SQL_INTEGER)):
    return _read_page_http(artifact_id,'annotations',limit=limit,before_id=before_id,snapshot_max_id=snapshot_max_id)

@app.get('/api/artifacts/{artifact_id}/transcripts')
def api_transcript_versions(artifact_id:SqlPathId):
    with session() as db:
        return [dict(r) for r in db.execute("SELECT id,artifact_id,run_id,model,language,created_at FROM derived_text WHERE artifact_id=? AND kind='transcript' ORDER BY id DESC",(artifact_id,))]

@app.get('/api/artifacts/{artifact_id}/source-observations')
def api_source_observations(artifact_id:SqlPathId):
    with session() as db:
        return [dict(r) for r in db.execute('SELECT * FROM source_observations WHERE artifact_id=? ORDER BY id',(artifact_id,))]

@app.api_route('/api/artifacts/{artifact_id}/content', methods=['GET', 'HEAD'])
def api_artifact_content(request:Request, artifact_id:SqlPathId):
    with session() as db:
        row = db.execute('SELECT * FROM artifacts WHERE id=?',(artifact_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'artifact not found')
    if not row['stored_path']:
        raise HTTPException(409, 'stored content is unavailable')
    try:
        # This sync route runs in a worker thread. The response retains its
        # verified descriptor and rechecks each buffered chunk before sending.
        return verified_media_response(
            row['stored_path'], row['sha256'], row['size_bytes'],
            media_type=row['mime_type'], filename=row['original_name'],
            range_header=request.headers.get('range'),
            if_range=request.headers.get('if-range'), head=request.method == 'HEAD',
            store_root=settings.store_dir,
        )
    except MediaLimitError as exc:
        raise HTTPException(413, 'Stored content exceeds the verified serving limit.') from exc
    except (MediaUnavailable, MediaIntegrityError) as exc:
        raise HTTPException(409, 'Stored content is unavailable or failed byte identity verification.') from exc

@app.post('/api/artifacts/{artifact_id}/citations')
def api_citation_create(artifact_id:SqlPathId,a:CitationIn):
    try:
        return create_citation(artifact_id,a.derivedTextId,a.segmentIndices,
                              word_refs=[ref.model_dump() for ref in a.wordRefs] if a.wordRefs is not None else None,
                              quote_text=a.quoteText)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc

@app.get('/api/artifacts/{artifact_id}/citations')
def api_citations(artifact_id:SqlPathId):
    with session() as db:
        return [anchor_payload(r) for r in db.execute('SELECT * FROM evidence_anchors WHERE artifact_id=? ORDER BY id',(artifact_id,))]

@app.get('/api/artifacts/{artifact_id}/citations/{anchor_id}/packet')
def api_citation_packet(artifact_id: SqlPathId, anchor_id: SqlPathId):
    from .exchange import (PacketLimitError, PacketNotFound, canonical_packet_bytes,
                           export_citation_packet)
    try:
        packet = export_citation_packet(artifact_id, anchor_id)
        payload = canonical_packet_bytes(packet)
    except PacketNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except PacketLimitError as exc:
        raise HTTPException(413, str(exc)) from exc
    except (ValueError, TypeError, UnicodeError) as exc:
        raise HTTPException(409, 'Stored evidence cannot form a valid citation packet; original records remain unchanged.') from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, 'Evidence database is unavailable for read-only export.') from exc
    return Response(content=payload, media_type='application/json', headers={
        'Content-Disposition': f'attachment; filename="ageds-citation-{artifact_id}-{anchor_id}.json"',
        'Cache-Control': 'no-store',
    })

@app.get('/api/source-roots')
def api_source_roots():
    return [{'id':i,'label':root.name} for i,root in enumerate(settings.scan_roots)]

@app.post('/api/source-scans')
def api_source_scan(scan: ScanIn):
    from .scanner import scan_sources
    if not settings.scan_roots:
        raise HTTPException(403,'scanning disabled: operator must configure EW_SCAN_ROOTS')
    if not 0 <= scan.rootId < len(settings.scan_roots):
        raise HTTPException(404,'source root not found')
    relative = Path(scan.relativePath)
    if relative.is_absolute() or '..' in relative.parts:
        raise HTTPException(422,'relativePath must stay within the configured source root')
    root = settings.scan_roots[scan.rootId].resolve()
    target = root / relative
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise HTTPException(422,'source path escapes configured root')
    if not target.is_dir():
        raise HTTPException(404,'source directory not found')
    try:
        return scan_sources(target)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc

@app.get('/api/jobs/{job_id}')
def api_job(job_id:SqlPathId):
    with session() as db:
        job = db.execute('SELECT id,kind,artifact_id,status,priority,attempts,error,created_at,started_at,finished_at FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job:
            raise HTTPException(404,'job not found')
        result = dict(job)
        result['runs'] = [dict(r) for r in db.execute('SELECT id,status,started_at,finished_at,model,tool,tool_version,error FROM processing_runs WHERE job_id=? ORDER BY id',(job_id,))]
        return result

@app.get('/api/artifacts/{artifact_id}/annotations')
def api_annotations(artifact_id:SqlPathId):
    with session() as db:
        rows=db.execute("SELECT * FROM annotations WHERE artifact_id=? ORDER BY id DESC",(artifact_id,)).fetchall()
        return [_annotation_payload(r) for r in rows]

@app.post('/api/artifacts/{artifact_id}/annotations')
def api_annotation_create(artifact_id:SqlPathId,a:AnnotationIn):
    return _save_annotation(artifact_id,a)
