from __future__ import annotations
import csv, io, json, shutil, sqlite3, tempfile
from pathlib import Path
from typing import Annotated
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from contextlib import closing
from .media.version_status import NOT_SUPERSEDED_SQL, superseded as superseded_versions
from .db import connect, init_db, session
from .config import settings
from .evidence import IntegrityError, ensure_source, ingest_file, temporary_copy_acquisitions
from .citations import anchor_payload, create_citation, loads_transcript_segments, milliseconds
from .importers.sms_backup import import_sms_backup
from .importers.whatsapp import import_whatsapp_txt
from .search import SearchQueryError, SearchWorkLimitError, scoped_search_page, search_page
from .read_pages import PageInputError, PageNotFound, PageStoredError, PageLimitError, read_page
from .verified_media import MediaUnavailable, MediaIntegrityError, MediaLimitError, verified_media_response
from .transcription import queue_all_audio, queue_transcription, word_timing_capabilities
from .http_authz import Access, TOKEN_COOKIE, artifact as artifact_guard, auth_headers, authenticated, host_and_origin, installation, public, scoped

app=FastAPI(title='AGEDS Evidence Workbench',version='0.2.0')
app.middleware('http')(auth_headers)
app.middleware('http')(host_and_origin)
app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')
from .web import templates
from .api import include_routers
include_routers(app)
from .web import NAV as _NAV
_NAV.append(('/advocate', 'Rzecznik'))  # the advocate page lives in main.py
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

CSV_FORMULA_GUARD = 'apostrophe_prefix_v1'
_FORMULA_LEADS = ('=', '+', '-', '@', '\t', '\r')


def _csv_guard(value):
    """Spreadsheet apps execute text starting with = + - @ TAB CR; such text cells
    get a leading apostrophe. The JSON APIs keep stored values unchanged."""
    if isinstance(value, str) and value.startswith(_FORMULA_LEADS):
        return "'" + value
    return value


def _display_projection(db):
    from .timeline.projection import ensure_projection
    from .timeline.ruleset import load_rule
    return ensure_projection(db, load_rule(kind='display_rule'), {})


def _recent_events(db, limit: int) -> list[dict]:
    """Dashboard list: newest projected time first, undated last; never import time."""
    from .timeline.query import legacy_events
    rows = legacy_events(db, _display_projection(db), limit=limit, phone=None, event_type=None)
    keep = ('id', 'event_type', 'ts_start', 'direction', 'contact_label', 'phone_or_address', 'subject', 'body')
    return [{k: (row[k][:240] if k == 'body' and isinstance(row[k], str) else row[k]) for k in keep} for row in rows]


def dashboard_counts():
    with session() as db:
        return {k:db.execute(sql).fetchone()[0] for k,sql in {
            'artifacts':'SELECT count(*) FROM artifacts','events':'SELECT count(*) FROM events','transcripts':"SELECT count(*) FROM derived_text WHERE kind='transcript'",'annotations':'SELECT count(*) FROM annotations','queued':"SELECT count(*) FROM jobs WHERE status='queued'",'failed':"SELECT count(*) FROM jobs WHERE status='failed'"}.items()}

@app.get('/',response_class=HTMLResponse,dependencies=[installation('read_metadata')])
def home(request:Request,q:str='',kind:str=''):
    with session() as db:
        recent=[dict(r) for r in db.execute("SELECT id,original_name,mime_type,size_bytes,sha256,created_at FROM artifacts ORDER BY id DESC LIMIT 30")]
        events=_recent_events(db, 30)
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

@app.post('/upload',dependencies=[installation('ingest')])
async def upload(file:UploadFile=File(...),source_label:str=Form('Manual upload')):
    suffix=Path(file.filename or '').suffix
    with tempfile.NamedTemporaryFile(delete=False,suffix=suffix) as tmp:
        shutil.copyfileobj(file.file,tmp); p=Path(tmp.name)
    try:
        src=ensure_source('manual_upload',source_label)
        aid=ingest_file(p,source_id=src,source_locator=f'upload:{file.filename}',original_name=file.filename,mime_type=file.content_type,
                        source_stat='temporary_copy')
        return {'ok':True,'artifact_id':aid}
    finally:p.unlink(missing_ok=True)

@app.post('/import/sms-backup',dependencies=[installation('ingest')])
async def import_sms(file:UploadFile=File(...)):
    with tempfile.NamedTemporaryFile(delete=False,suffix='.xml') as tmp:
        shutil.copyfileobj(file.file,tmp); p=Path(tmp.name)
    try:
        with temporary_copy_acquisitions():
            return import_sms_backup(p,source_locator=f'upload:sms-backup:{file.filename}',original_name=file.filename)
    finally:p.unlink(missing_ok=True)

@app.post('/import/whatsapp',dependencies=[installation('ingest')])
async def import_wa(file:UploadFile=File(...),chat_label:str=Form('')):
    with tempfile.NamedTemporaryFile(delete=False,suffix='.txt') as tmp:
        shutil.copyfileobj(file.file,tmp); p=Path(tmp.name)
    try:
        with temporary_copy_acquisitions():
            return import_whatsapp_txt(p,chat_label or None,source_locator=f'upload:whatsapp:{file.filename}',original_name=file.filename)
    finally:p.unlink(missing_ok=True)

@app.get('/artifact/{artifact_id}',response_class=HTMLResponse,dependencies=[installation('read_metadata')])
def artifact(request:Request,artifact_id:SqlPathId):
    with session() as db:
        art=db.execute("SELECT a.*,s.kind source_kind,s.label source_label FROM artifacts a LEFT JOIN sources s ON s.id=a.source_id WHERE a.id=?",(artifact_id,)).fetchone()
        if not art:raise HTTPException(404)
        previews=[dict(r) for r in db.execute("SELECT id,kind,model,language,substr(text,1,4096) AS text,length(text)>4096 AS text_truncated FROM derived_text WHERE artifact_id=? AND kind!='transcript' ORDER BY id DESC LIMIT 51",(artifact_id,))]
        events=[dict(r) for r in db.execute("SELECT id,ts_start,event_type,direction,contact_label,phone_or_address,substr(body,1,4096) AS body,substr(subject,1,4096) AS subject FROM events WHERE artifact_id=? ORDER BY ts_start LIMIT 101",(artifact_id,))]
    with session() as db:
        superseded_map = superseded_versions(db, artifact_id)
    version_page = _read_page_http(artifact_id, 'transcripts')
    citation_page = _read_page_http(artifact_id, 'citations')
    annotation_page = _read_page_http(artifact_id, 'annotations')
    return templates.TemplateResponse(request,'artifact.html',{
        'a':dict(art),'texts':previews[:50],'text_previews_more':len(previews)>50,
        'annotations':annotation_page['items'],'events':events[:100],'events_more':len(events)>100,
        'citations':citation_page['items'],'version_page':version_page,'superseded':superseded_map,
        'citation_page':citation_page,'annotation_page':annotation_page})


@app.post('/artifact/{artifact_id}/annotate',dependencies=[installation('annotate')])
def annotate(artifact_id:SqlPathId,body:str=Form(...),label:str=Form(''),kind:str=Form('note'),start_ms:int|None=Form(None,ge=0,le=MAX_SQL_INTEGER),end_ms:int|None=Form(None,ge=0,le=MAX_SQL_INTEGER),derived_text_id:str|None=Form(None)):
    try:
        version = int(derived_text_id) if derived_text_id and derived_text_id.strip() else None
        annotation = AnnotationIn(body=body,label=label or None,kind=kind,startMs=start_ms,endMs=end_ms,derivedTextId=version)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = _save_annotation(artifact_id, annotation)
    return {'ok':True,'annotation_id':result['id']}

@app.post('/artifact/{artifact_id}/transcribe',dependencies=[installation('process')])
def transcribe(artifact_id:SqlPathId): return api_queue_transcription(artifact_id, None)

@app.post('/transcribe/all',dependencies=[installation('process')])
def transcribe_all(): return {'queued_artifacts':queue_all_audio()}

@app.get('/api/search')
def api_search(q:str,access:Access=scoped('read_text')):
    try:
        if access.open:
            page = search_page(q, 200)
        else:
            with closing(connect(read_only=True)) as db:
                gate = access.gate(db)
                page = search_page(q, 200) if gate.installation('read_text') else scoped_search_page(q, 200, gate)
    except SearchQueryError as exc:
        raise HTTPException(422, str(exc)) from exc
    except SearchWorkLimitError as exc:
        raise HTTPException(503, 'Search work limit exceeded; narrow the query.') from exc
    return JSONResponse(page['results'], headers={
        'X-AGEDS-Search-Limit': str(page['limit']),
        'X-AGEDS-Search-Has-More': str(page['has_more']).lower(),
        'X-AGEDS-Search-Complete': str(page['complete']).lower(),
    })

# GET /api/timeline moved to server/app/api/timeline.py (timeline area): same path and
# response shape; ordering now follows the versioned display-time projection.

@app.get('/export/events.csv',dependencies=[installation('export')])
def export_events_csv():
    with session() as db:
        projection = _display_projection(db)
        order = [r[0] for r in db.execute(
            "SELECT ref_id FROM timeline_items WHERE projection_id=? AND item_kind='event' "
            "ORDER BY sort_us IS NULL, sort_us, ref_id", (projection['id'],))]
        by_id = {r['id']: dict(r) for r in db.execute("SELECT * FROM events ORDER BY id")}
    seen = set(order)
    # Every event is exported: rows the projection does not list follow by id.
    rows = [by_id[i] for i in order if i in by_id] + [row for i, row in by_id.items() if i not in seen]
    buf=io.StringIO();
    if rows:
        w=csv.DictWriter(buf,fieldnames=rows[0].keys());w.writeheader()
        w.writerows({k: _csv_guard(v) for k, v in row.items()} for row in rows)
    return StreamingResponse(iter([buf.getvalue()]),media_type='text/csv',headers={
        'Content-Disposition':'attachment; filename=events.csv',
        'X-AGEDS-CSV-Formula-Guard': CSV_FORMULA_GUARD,
        'X-AGEDS-Events-Order': f"projected-time-asc; undated-last-by-event-id; rule {projection['rule']['rule_id']}@{projection['rule']['version']}"})

@app.get('/export/manifest.json')
def export_manifest(access:Access=installation('export')):
    from .packages import export_metadata_package
    return JSONResponse(export_metadata_package(viewer_id=access.principal_id))

@app.get('/api/export/scoped')
def api_scoped_export(access:Access=scoped('export')):
    """Metadata this principal may export; references outside the scope are nulled and listed."""
    from .sharing import export_scope_package
    if access.open:
        raise HTTPException(409, 'scoped export needs auth.mode=tokens; use /export/manifest.json for the whole installation')
    with closing(connect(read_only=True)) as db:
        return JSONResponse(export_scope_package(db, access.principal_id))

@app.post('/api/packages/validate',dependencies=[authenticated()])
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
def api_artifacts(limit:int=Query(500,ge=1,le=10000),access:Access=scoped('read_metadata')):
    with session() as db:
        gate = access.gate(db)
        # The unfiltered listing includes text previews, so it needs read_text as well (review 3, R3-1).
        everything = gate is None or (gate.installation('read_metadata') and gate.installation('read_text'))
        rows=db.execute("""
            SELECT a.id,a.original_name,a.mime_type,a.size_bytes,a.sha256,a.created_at,a.source_locator,
              CASE
                WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' AND j.status='running') THEN 'running'
                WHEN EXISTS(SELECT 1 FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' AND j.status='queued') THEN 'queued'
                WHEN (SELECT status FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' ORDER BY j.id DESC LIMIT 1)='failed' THEN 'failed'
                WHEN EXISTS(SELECT 1 FROM derived_text d WHERE d.artifact_id=a.id AND d.kind='transcript') THEN 'done'
                ELSE 'none'
              END transcript_status,
              (SELECT language FROM derived_text d WHERE d.artifact_id=a.id AND d.kind='transcript' AND {current} ORDER BY d.id DESC LIMIT 1) transcript_language,
              (SELECT substr(text,1,400) FROM derived_text d WHERE d.artifact_id=a.id AND d.kind='transcript' AND {current} ORDER BY d.id DESC LIMIT 1) transcript_preview,
              (SELECT priority FROM jobs j WHERE j.artifact_id=a.id AND j.kind='transcribe' ORDER BY j.id DESC LIMIT 1) queued_priority
            FROM artifacts a
            WHERE a.mime_type LIKE 'audio/%' OR a.mime_type LIKE 'video/%'
            ORDER BY COALESCE(queued_priority,-1) DESC,a.id DESC LIMIT ?
        """.replace('{current}', NOT_SUPERSEDED_SQL),(limit if everything else -1,)).fetchall()
        if not everything:
            # Filter before the limit so hidden rows cannot shorten or reorder a page.
            rows=[r for r in rows if gate.allows('artifact',r['id'],'read_metadata')][:limit]
        out=[]
        for r in rows:
            d=dict(r)
            if not everything:
                # Status, language and preview come only from text versions this reader may see.
                visible_texts = [t for t in db.execute(f"SELECT id,language,substr(text,1,400) AS preview FROM derived_text d WHERE artifact_id=? AND kind='transcript' AND {NOT_SUPERSEDED_SQL} ORDER BY id DESC",(d['id'],))
                                 if gate.allows('derived_text', t['id'], 'read_metadata')]
                latest = visible_texts[0] if visible_texts else None
                d['transcript_language'] = latest['language'] if latest else None
                d['transcript_preview'] = latest['preview'] if latest and gate.allows('derived_text', latest['id'], 'read_text') else None
                # Processing state only from jobs this reader may see (a frozen snapshot hides later runs).
                jobs = [j for j in db.execute("SELECT id,status,priority FROM jobs WHERE artifact_id=? AND kind='transcribe' ORDER BY id DESC",(d['id'],))
                        if gate.allows('job', j['id'], 'read_metadata')]
                statuses = {j['status'] for j in jobs}
                d['transcript_status'] = ('running' if 'running' in statuses else 'queued' if 'queued' in statuses
                                          else 'failed' if jobs and jobs[0]['status'] == 'failed'
                                          else 'done' if visible_texts else 'none')
                d['queued_priority'] = jobs[0]['priority'] if jobs else None
            out.append({
                'id':d['id'],'originalName':d['original_name'],'mimeType':d['mime_type'],'sizeBytes':d['size_bytes'],
                'sha256':d['sha256'],'createdAt':d['created_at'],'sourceLocator':d['source_locator'],
                'transcriptStatus':d['transcript_status'],'transcriptLanguage':d['transcript_language'],
                'transcriptPreview':d['transcript_preview'],'queuedPriority':d['queued_priority'],'tags':[]
            })
        return out

@app.post('/api/artifacts/upload',dependencies=[installation('ingest')])
async def api_upload_artifact(file:UploadFile=File(...),source_label:str=Form('AGEDS client'),source_locator:str|None=Form(None),metadata_json:str=Form('{}')):
    client_metadata = _json_object(metadata_json)
    suffix=Path(file.filename or '').suffix
    with tempfile.NamedTemporaryFile(delete=False,suffix=suffix) as tmp:
        shutil.copyfileobj(file.file,tmp); p=Path(tmp.name)
    try:
        src=ensure_source('client_upload',source_label)
        aid=ingest_file(p,source_id=src,source_locator=source_locator or f'client-upload:{file.filename}',original_name=file.filename,mime_type=file.content_type,
                        metadata={'client_metadata':client_metadata,'client_metadata_status':'unverified',
                                  'acquisition_kind':'uploaded_stream','filesystem_stat_scope':'server_temporary_upload_not_original_source'},
                        source_stat='temporary_copy')
        with session() as db:
            sha=db.execute('SELECT sha256 FROM artifacts WHERE id=?',(aid,)).fetchone()['sha256']
        return {'ok':True,'artifact_id':aid,'sha256':sha}
    finally:p.unlink(missing_ok=True)

@app.post('/api/artifacts/{artifact_id}/transcribe',dependencies=[installation('process')])
def api_queue_transcription(artifact_id:SqlPathId,priority:int|None=Query(None,ge=-MAX_SQL_INTEGER,le=MAX_SQL_INTEGER)):
    with session() as db:
        if not db.execute('SELECT 1 FROM artifacts WHERE id=?',(artifact_id,)).fetchone():
            raise HTTPException(404, 'artifact not found')
    try:
        return {'job_id':queue_transcription(artifact_id,priority)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

def _row_filter(db, access: Access, kind: str, capability: str):
    """None when everything is readable (legacy_open or installation scope), else a per-row check."""
    gate = access.gate(db)
    if gate is None or gate.installation(capability):
        return None
    return lambda object_id: gate.allows(kind, object_id, capability)

@app.get('/api/artifacts/{artifact_id}/transcript')
def api_transcript(artifact_id:SqlPathId,derived_text_id:int|None=Query(None,ge=1,le=MAX_SQL_INTEGER),
                   access:Access=artifact_guard('read_text')):
    with session() as db:
        allow = _row_filter(db, access, 'derived_text', 'read_text')
        if derived_text_id is None:
            # The latest version this reader may see, not the latest version overall.
            # Superseded versions stay addressable by id but are not the default.
            r = next((row for row in db.execute(f"SELECT * FROM derived_text d WHERE artifact_id=? AND kind='transcript' AND {NOT_SUPERSEDED_SQL} ORDER BY id DESC",(artifact_id,))
                      if allow is None or allow(row['id'])), None)
        else:
            r=db.execute("SELECT * FROM derived_text WHERE artifact_id=? AND id=? AND kind='transcript'",(artifact_id,derived_text_id)).fetchone()
            if r is not None and allow is not None and not allow(r['id']):
                r = None
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


PAGE_KINDS = {'transcripts': ('derived_text', 'read_text'), 'citations': ('evidence_anchor', 'read_text'),
              'annotations': ('annotation', 'read_metadata')}

def _read_page_http(artifact_id, kind, *, limit=50, before_id=None, snapshot_max_id=None, access=None):
    try:
        if access is None or access.open:
            return read_page(artifact_id, kind, limit=limit, before_id=before_id, snapshot_max_id=snapshot_max_id)
        object_kind, capability = PAGE_KINDS[kind]
        with closing(connect(read_only=True)) as db:
            return read_page(artifact_id, kind, limit=limit, before_id=before_id, snapshot_max_id=snapshot_max_id,
                             allow=_row_filter(db, access, object_kind, capability))
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
                        snapshot_max_id:int|None=Query(None,ge=0,le=MAX_SQL_INTEGER),
                        access:Access=artifact_guard('read_text')):
    return _read_page_http(artifact_id,'transcripts',limit=limit,before_id=before_id,snapshot_max_id=snapshot_max_id,access=access)

@app.get('/api/artifacts/{artifact_id}/citations/page')
def api_citation_page(artifact_id:SqlPathId, limit:int=Query(50,ge=1,le=100),
                      before_id:int|None=Query(None,ge=1,le=MAX_SQL_INTEGER),
                      snapshot_max_id:int|None=Query(None,ge=0,le=MAX_SQL_INTEGER),
                      access:Access=artifact_guard('read_text')):
    return _read_page_http(artifact_id,'citations',limit=limit,before_id=before_id,snapshot_max_id=snapshot_max_id,access=access)

@app.get('/api/artifacts/{artifact_id}/annotations/page',dependencies=[installation('read_metadata')])
def api_annotation_page(artifact_id:SqlPathId, limit:int=Query(50,ge=1,le=100),
                        before_id:int|None=Query(None,ge=1,le=MAX_SQL_INTEGER),
                        snapshot_max_id:int|None=Query(None,ge=0,le=MAX_SQL_INTEGER)):
    return _read_page_http(artifact_id,'annotations',limit=limit,before_id=before_id,snapshot_max_id=snapshot_max_id)

@app.get('/api/artifacts/{artifact_id}/transcripts')
def api_transcript_versions(artifact_id:SqlPathId,access:Access=artifact_guard('read_metadata')):
    with session() as db:
        allow = _row_filter(db, access, 'derived_text', 'read_metadata')
        return [dict(r) for r in db.execute("SELECT id,artifact_id,run_id,model,language,created_at FROM derived_text WHERE artifact_id=? AND kind='transcript' ORDER BY id DESC",(artifact_id,))
                if allow is None or allow(r['id'])]

@app.get('/api/artifacts/{artifact_id}/source-observations')
def api_source_observations(artifact_id:SqlPathId,access:Access=artifact_guard('read_metadata')):
    with session() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM source_observations WHERE artifact_id=? ORDER BY id',(artifact_id,))]
        gate = access.gate(db)
        # An acquisition belongs to its source's case, not to every case the material is assigned to.
        return rows if gate is None else [r for r in rows if gate.allows('source_observation', r['id'], 'read_metadata')]

@app.api_route('/api/artifacts/{artifact_id}/content', methods=['GET', 'HEAD'], dependencies=[artifact_guard('read_bytes')])
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
def api_citation_create(artifact_id:SqlPathId,a:CitationIn,access:Access=artifact_guard('cite')):
    if not access.open:
        with session() as db:
            gate = access.gate(db)
            if not gate.allows('artifact', artifact_id, 'read_text'):
                raise HTTPException(403, 'citing requires read_text')
            # The quote comes from this text version: it must be readable itself.
            if not gate.allows('derived_text', a.derivedTextId, 'read_text'):
                raise HTTPException(404, 'not available')
    try:
        return create_citation(artifact_id,a.derivedTextId,a.segmentIndices,
                              word_refs=[ref.model_dump() for ref in a.wordRefs] if a.wordRefs is not None else None,
                              quote_text=a.quoteText)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc

@app.get('/api/artifacts/{artifact_id}/citations')
def api_citations(artifact_id:SqlPathId,access:Access=artifact_guard('read_text')):
    with session() as db:
        allow = _row_filter(db, access, 'evidence_anchor', 'read_text')
        return [anchor_payload(r) for r in db.execute('SELECT * FROM evidence_anchors WHERE artifact_id=? ORDER BY id',(artifact_id,))
                if allow is None or allow(r['id'])]

@app.get('/api/artifacts/{artifact_id}/citations/{anchor_id}/packet',dependencies=[installation('export')])
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

@app.get('/api/source-roots',dependencies=[installation('ingest')])
def api_source_roots():
    return [{'id':i,'label':root.name} for i,root in enumerate(settings.scan_roots)]

@app.post('/api/source-scans',dependencies=[installation('ingest')])
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

@app.get('/api/jobs/{job_id}',dependencies=[installation('process')])
def api_job(job_id:SqlPathId):
    with session() as db:
        job = db.execute('SELECT id,kind,artifact_id,status,priority,attempts,error,created_at,started_at,finished_at FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job:
            raise HTTPException(404,'job not found')
        result = dict(job)
        result['runs'] = [dict(r) for r in db.execute('SELECT id,status,started_at,finished_at,model,tool,tool_version,error FROM processing_runs WHERE job_id=? ORDER BY id',(job_id,))]
        return result

@app.get('/api/artifacts/{artifact_id}/annotations',dependencies=[installation('read_metadata')])
def api_annotations(artifact_id:SqlPathId):
    with session() as db:
        rows=db.execute("SELECT * FROM annotations WHERE artifact_id=? ORDER BY id DESC",(artifact_id,)).fetchall()
        return [_annotation_payload(r) for r in rows]

@app.post('/api/artifacts/{artifact_id}/annotations',dependencies=[installation('annotate')])
def api_annotation_create(artifact_id:SqlPathId,a:AnnotationIn):
    return _save_annotation(artifact_id,a)


# --- Evidence advocate (P6+) -------------------------------------------------
class ConversationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str | None = Field(default=None, max_length=200)

class QuestionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=4000)

def _advocate_principal(db, access: Access) -> int:
    if not access.open:
        return access.principal_id
    from .authz import get_setting
    owner = get_setting(db, 'installation.owner_principal_uid')
    row = db.execute('SELECT id FROM principals WHERE uid=?', (owner,)).fetchone() if owner else None
    if row is None:
        raise HTTPException(409, 'the advocate needs an installation owner: run authz-owner-init')
    return row['id']

def _advocate_call(fn):
    from .advocate import AdvocateError
    from .authz import Denied
    try:
        return fn()
    except Denied as exc:
        raise HTTPException(404, 'not available') from exc
    except AdvocateError as exc:
        raise HTTPException(422, str(exc)) from exc

@app.post('/api/advocate/conversations')
def api_advocate_start(body: ConversationIn, access: Access = scoped('chat_with_advocate')):
    from .advocate import start_conversation
    with session() as db:
        principal = _advocate_principal(db, access)
        return _advocate_call(lambda: start_conversation(db, principal, title=body.title))

@app.get('/api/advocate/conversations')
def api_advocate_conversations(others: bool = False, access: Access = scoped('chat_with_advocate')):
    """Own conversations; with others=true, those of other principals whose own mode lets this one see them."""
    from .authz import ContentGate
    with session() as db:
        principal = _advocate_principal(db, access)
        if not others:
            return [dict(r) for r in db.execute(
                'SELECT uid,title,settings_json,created_at FROM conversations WHERE principal_id=? ORDER BY id DESC', (principal,))]
        gate = ContentGate(db, principal)
        rows = gate.list('conversation', 'read_metadata', where='WHERE principal_id<>?', params=(principal,))
        # A title is the recipient's own words: shown only where the content itself may be read.
        return [{'uid': r['uid'], 'created_at': r['created_at'], 'settings_json': r['settings_json'],
                 'title': r['title'] if authz_visible(db, principal, r) else None} for r in rows]

def authz_visible(db, principal, conversation_row) -> bool:
    from .authz import _private
    from .authz import coverage as _coverage
    return _coverage(db, principal, 'read_text').covers(_private(conversation_row, 'content'))

class ShareIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    shared: bool

@app.post('/api/advocate/conversations/{conversation_uid}/share')
def api_advocate_share(conversation_uid: str, body: ShareIn, access: Access = scoped('chat_with_advocate')):
    from .advocate import share_with_owner
    with session() as db:
        principal = _advocate_principal(db, access)
        return _advocate_call(lambda: share_with_owner(db, principal, conversation_uid, body.shared))

class DelegationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: str = Field(min_length=1, max_length=200)
    capabilities: list[str] = Field(min_length=1, max_length=20)

@app.post('/api/delegations')
def api_delegate(body: DelegationIn, access: Access = scoped('invite')):
    """Pass on part of one's own grants (capability invite) to a new principal; the token is shown once."""
    from . import authz
    from .assignments import create_principal
    from .auth import issue_token
    if access.open:
        raise HTTPException(409, 'delegation needs auth.mode=tokens')
    with session() as db:
        scopes = sorted({r['scope_id'] for r in db.execute(
            'SELECT scope_id FROM grants WHERE principal_id=? AND revoked_at IS NULL', (access.principal_id,))})
        grantee = create_principal(db, body.display_name, kind='human', actor=f'principal:{access.principal_id}')
        delegated = []
        for scope_id in scopes:
            try:
                delegated.append(authz.delegate(db, access.principal_id, scope_id, grantee, body.capabilities)['uid'])
            except authz.Denied:
                continue
        if not delegated:
            raise HTTPException(403, 'no grant of yours allows passing on these capabilities')
        return {'grant_uids': delegated, 'token': issue_token(db, grantee, label='delegated'), 'token_shown_once': True}

@app.post('/api/advocate/conversations/{conversation_uid}/turns')
def api_advocate_ask(conversation_uid: str, body: QuestionIn, access: Access = scoped('chat_with_advocate')):
    from .advocate import ask
    from .providers import configured_provider
    with session() as db:
        principal = _advocate_principal(db, access)
        return _advocate_call(lambda: ask(db, principal, conversation_uid, body.question,
                                          provider=configured_provider(db, principal)))

@app.get('/api/advocate/conversations/{conversation_uid}')
def api_advocate_history(conversation_uid: str, access: Access = scoped('chat_with_advocate')):
    from .advocate import conversation_history
    with session() as db:
        principal = _advocate_principal(db, access)
        return _advocate_call(lambda: {'conversation_uid': conversation_uid,
                                       'turns': conversation_history(db, principal, conversation_uid)})

@app.get('/api/advocate/answers/{answer_uid}/basis')
def api_advocate_basis(answer_uid: str, access: Access = scoped('chat_with_advocate')):
    from .advocate import answer_basis
    with session() as db:
        principal = _advocate_principal(db, access)
        return _advocate_call(lambda: answer_basis(db, principal, answer_uid))


@app.get('/api/advocate/status')
def api_advocate_status(access: Access = scoped('chat_with_advocate')):
    """What the recipient needs to know before asking: provider, owner visibility, own name."""
    from .authz import get_setting
    from .providers import configured_provider
    from .advocate import AdvocateGate
    with session() as db:
        principal = _advocate_principal(db, access)
        provider = configured_provider(db, principal)
        name = db.execute('SELECT display_name FROM principals WHERE id=?', (principal,)).fetchone()['display_name']
        gate = AdvocateGate(db, principal)
        shared = {'cases': [c['name'] for c in gate.list('case', 'read_metadata')],
                  'materials': len(gate.list('artifact', 'read_text'))}
        return {'principal': name, 'auth_mode': access.mode, 'shared': shared,
                'can_chat': AdvocateGate(db, principal).can_chat(),
                'provider': None if provider is None else {'name': provider.name, 'model': provider.config['model']},
                'voice': _voice_status(db, principal),
                'owner_visibility': get_setting(db, 'conversation.owner_visibility', 'none')}

@app.get('/advocate', response_class=HTMLResponse, dependencies=[public()])
def advocate_page(request: Request, error: str = ''):
    return templates.TemplateResponse(request, 'advocate.html', {'error': error[:200]})

@app.post('/advocate/login', dependencies=[public()])
def advocate_login(request: Request, token: str = Form(..., max_length=512)):
    from fastapi.responses import RedirectResponse
    from .auth import start_session
    with session() as db:
        # Only an API token starts a session; the cookie holds a separate secret bound to it.
        started = start_session(db, token.strip())
        if started is None:
            return RedirectResponse('/advocate?error=token', status_code=303)
        session_token, max_age = started
    response = RedirectResponse('/advocate', status_code=303)
    secure = request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https'
    response.set_cookie(TOKEN_COOKIE, session_token, httponly=True, samesite='strict', secure=secure, max_age=max_age)
    return response

@app.post('/advocate/logout', dependencies=[public()])
def advocate_logout(request: Request):
    from fastapi.responses import RedirectResponse
    from .auth import end_session
    with session() as db:
        end_session(db, request.cookies.get(TOKEN_COOKIE))
    response = RedirectResponse('/advocate', status_code=303)
    response.delete_cookie(TOKEN_COOKIE)
    return response


class SummaryIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    topic: str = Field(min_length=1, max_length=4000)

@app.post('/api/advocate/summaries')
def api_advocate_summarize(body: SummaryIn, access: Access = scoped('chat_with_advocate')):
    from .advocate import summarize
    from .providers import configured_provider
    with session() as db:
        principal = _advocate_principal(db, access)
        return _advocate_call(lambda: summarize(db, principal, body.topic, provider=configured_provider(db, principal)))

@app.get('/api/advocate/summaries')
def api_advocate_summaries(access: Access = scoped('chat_with_advocate')):
    from .advocate import visible_summaries
    with session() as db:
        return visible_summaries(db, _advocate_principal(db, access))


def _voice_status(db, principal):
    from .providers import configured_voice
    voice = configured_voice(db, principal)
    return None if voice is None else {'stt_model': voice.config['stt_model'], 'tts_voice': voice.config['tts_voice']}

@app.post('/api/advocate/voice/transcribe')
async def api_advocate_transcribe(file: UploadFile = File(...), speaker_consent: str = Form(''),
                                  access: Access = scoped('chat_with_advocate')):
    """The speaker's recording to text, sent only with the speaker's consent; the text is returned for review."""
    from .providers import ProviderUnavailable, configured_voice
    from .advocate import AdvocateGate
    audio = await file.read(10 * 1024 * 1024 + 1)
    with session() as db:
        principal = _advocate_principal(db, access)
        if not AdvocateGate(db, principal).can_chat():
            raise HTTPException(404, 'not available')
        voice = configured_voice(db, principal)
        if voice is None:
            raise HTTPException(409, 'voice is not enabled')
        try:
            return voice.transcribe(db, audio, file.content_type or '', speaker_consent=speaker_consent == 'yes')
        except ProviderUnavailable as exc:
            raise HTTPException(422, str(exc)[:300]) from exc

@app.get('/api/advocate/answers/{answer_uid}/speech')
def api_advocate_speech(answer_uid: str, access: Access = scoped('chat_with_advocate')):
    from .authz import Denied
    from .providers import ProviderUnavailable, configured_voice
    with session() as db:
        principal = _advocate_principal(db, access)
        voice = configured_voice(db, principal)
        if voice is None:
            raise HTTPException(409, 'voice is not enabled')
        try:
            audio = voice.synthesize(db, answer_uid)
        except Denied as exc:
            raise HTTPException(404, 'not available') from exc
        except ProviderUnavailable as exc:
            raise HTTPException(409, str(exc)[:300]) from exc
    return Response(content=audio, media_type='audio/mpeg', headers={'Cache-Control': 'no-store'})
