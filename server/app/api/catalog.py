"""HTTP routes owned by the catalog area (AGEDS-TASK-20261016-A).

JSON API under ``/api/catalog`` and a server-rendered page at ``/catalog``.
Every list is a bounded page. HTTP scans are limited to operator-configured
``EW_SCAN_ROOTS`` exactly like ``/api/source-scans``. API authentication is not
implemented (see HANDOFF): run the service on a trusted/private network.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .. import db as dbmod
from ..catalog import coverage, describe, export, ingest, store
from ..catalog.limits import LIMIT_NAMES, CatalogLimits
from ..catalog.scan import run_catalog_scan
from ..web import templates

router = APIRouter()
NAV: list[tuple[str, str]] = [('/catalog', 'Katalog')]
Id = Annotated[int, Query(ge=1, le=2**63 - 1)]
BodyId = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]


class CollectionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str = Field(min_length=1, max_length=500)
    device_or_account: str | None = Field(default=None, max_length=500)
    export_date_raw: str | None = Field(default=None, max_length=200)
    acquisition_method: str | None = Field(default=None, max_length=500)
    operator: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=20000)
    priority: StrictInt | None = None
    parent_collection_id: BodyId | None = None


class CollectionPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str | None = Field(default=None, min_length=1, max_length=500)
    device_or_account: str | None = Field(default=None, max_length=500)
    export_date_raw: str | None = Field(default=None, max_length=200)
    acquisition_method: str | None = Field(default=None, max_length=500)
    operator: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=20000)
    priority: StrictInt | None = None
    parent_collection_id: BodyId | None = None
    actor: str = Field(default='web', min_length=1, max_length=200)


class ScanIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    collectionId: BodyId
    rootId: StrictInt = Field(ge=0)
    relativePath: str = ''
    limits: dict[str, StrictInt] = Field(default_factory=dict)
    expandDocumentContainers: StrictBool = False


class MarkIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    collectionId: BodyId
    locator: str = Field(min_length=1, max_length=8192)
    priority: StrictInt | None = None
    note: str | None = Field(default=None, max_length=20000)
    actor: str = Field(default='web', min_length=1, max_length=200)


class ExplainIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str = Field(default='none', min_length=1, max_length=100)
    allowExternal: StrictBool = False
    params: dict[str, str] = Field(default_factory=dict)
    minGapDays: StrictInt = Field(default=30, ge=1, le=36500)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['promoted', 'rejected']
    actor: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=5000)


class IngestIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entryIds: list[BodyId] = Field(min_length=1, max_length=100)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except store.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _annotation_dir() -> Path:
    return Path(dbmod.settings.data_dir) / 'catalog-annotations'


@router.get('/api/catalog/collections')
def api_collections():
    return {'collections': _call(store.list_collections)}


@router.post('/api/catalog/collections')
def api_collection_create(body: CollectionIn):
    fields = body.model_dump(exclude_none=True)
    label = fields.pop('label')
    return _call(store.create_collection, label, actor='web', **fields)


@router.get('/api/catalog/collections/{collection_id}')
def api_collection(collection_id: int):
    return _call(store.get_collection, collection_id)


@router.patch('/api/catalog/collections/{collection_id}')
def api_collection_update(collection_id: int, body: CollectionPatch):
    changes = {k: getattr(body, k) for k in body.model_fields_set if k != 'actor'}
    return _call(store.update_collection, collection_id, changes, actor=body.actor)


@router.post('/api/catalog/scans')
def api_scan(body: ScanIn):
    roots = dbmod.settings.scan_roots
    if not roots:
        raise HTTPException(403, 'scanning disabled: operator must configure EW_SCAN_ROOTS')
    if body.rootId >= len(roots):
        raise HTTPException(404, 'source root not found')
    relative = Path(body.relativePath)
    if relative.is_absolute() or '..' in relative.parts:
        raise HTTPException(422, 'relativePath must stay within the configured source root')
    root = Path(roots[body.rootId]).resolve()
    target = root / relative
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise HTTPException(422, 'source path escapes configured root')
    if not target.exists():
        raise HTTPException(404, 'source path not found')
    unknown = set(body.limits) - set(LIMIT_NAMES)
    if unknown:
        raise HTTPException(422, f'unknown limits: {sorted(unknown)}')
    limits = _call(CatalogLimits, **body.limits, expand_document_containers=body.expandDocumentContainers)
    return _call(run_catalog_scan, target, body.collectionId, limits=limits)


@router.get('/api/catalog/scans')
def api_scans(collectionId: Id | None = None, limit: int = Query(50, ge=1, le=store.MAX_PAGE)):
    return {'scans': store.list_scans(collectionId, limit)}


def _filters(collectionId, scanId, allScans, type, family, status, conflict, groupId, duplicatesOnly, parentId, containersOnly, q):
    return {'collection_id': collectionId, 'scan_id': scanId, 'all_scans': allScans, 'type': type, 'family': family,
            'status': status, 'conflict': conflict, 'group_id': groupId, 'duplicates_only': duplicatesOnly,
            'parent_id': parentId, 'containers_only': containersOnly, 'q': q}


@router.get('/api/catalog/entries')
def api_entries(collectionId: Id | None = None, scanId: Id | None = None, allScans: bool = False,
                type: str | None = Query(None, max_length=50), family: str | None = Query(None, max_length=50),
                status: str | None = Query(None, max_length=30), conflict: bool = False, groupId: Id | None = None,
                duplicatesOnly: bool = False, parentId: Id | None = None, containersOnly: bool = False,
                q: str | None = Query(None, max_length=500), afterId: Id | None = None,
                limit: int = Query(100, ge=1, le=store.MAX_PAGE)):
    filters = _filters(collectionId, scanId, allScans, type, family, status, conflict, groupId, duplicatesOnly, parentId,
                       containersOnly, q)
    return _call(store.list_entries, filters, after_id=afterId, limit=limit)


@router.get('/api/catalog/entries/{entry_id}')
def api_entry(entry_id: int):
    return _call(store.get_entry, entry_id)


@router.post('/api/catalog/marks')
def api_mark(body: MarkIn):
    return _call(store.add_mark, body.collectionId, body.locator, priority=body.priority, note=body.note, actor=body.actor)


@router.get('/api/catalog/groups')
def api_groups(collectionId: Id | None = None, includeEmpty: bool = False, limit: int = Query(100, ge=1, le=store.MAX_PAGE),
               offset: int = Query(0, ge=0, le=10_000_000)):
    return store.list_groups(collection_id=collectionId, include_empty=includeEmpty, limit=limit, offset=offset)


@router.get('/api/catalog/groups/{group_id}')
def api_group(group_id: int):
    return _call(store.get_group, group_id)


@router.get('/api/catalog/collisions')
def api_collisions(collectionId: Id | None = None, limit: int = Query(100, ge=1, le=store.MAX_PAGE),
                   offset: int = Query(0, ge=0, le=10_000_000)):
    return store.list_collisions(collection_id=collectionId, limit=limit, offset=offset)


@router.get('/api/catalog/coverage')
def api_coverage(collectionId: Id | None = None, minGapDays: int = Query(30, ge=1, le=36500)):
    return _call(coverage.coverage_report, collectionId, min_gap_days=minGapDays)


_MEDIA = {'csv': 'text/csv; charset=utf-8', 'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          'html': 'text/html; charset=utf-8'}


@router.get('/api/catalog/export')
def api_export(format: Literal['csv', 'xlsx', 'html'] = 'csv', collectionId: Id | None = None,
               maxRows: int = Query(100_000, ge=1, le=1_000_000), minGapDays: int = Query(30, ge=1, le=36500)):
    payload, result = _call(export.export_bytes, format, collection_id=collectionId, max_rows=maxRows, min_gap_days=minGapDays)
    suffix = f'-collection-{collectionId}' if collectionId else ''
    return Response(content=payload, media_type=_MEDIA[format], headers={
        'Content-Disposition': f'attachment; filename="ageds-catalog{suffix}.{format}"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'X-AGEDS-Rows': str(result['rows']), 'X-AGEDS-Truncated': str(result['truncated_by_max_rows']).lower(),
    })


@router.post('/api/catalog/collections/{collection_id}/explain')
def api_explain(collection_id: int, body: ExplainIn):
    return _call(describe.explain, collection_id, provider=body.provider, output_dir=_annotation_dir(),
                 allow_external=body.allowExternal, params=body.params, min_gap_days=body.minGapDays, actor='web')


@router.get('/api/catalog/annotations')
def api_annotations(collectionId: Id | None = None, limit: int = Query(100, ge=1, le=500)):
    return {'annotations': describe.list_annotations(collectionId, limit)}


@router.post('/api/catalog/annotations/{annotation_id}/decision')
def api_decision(annotation_id: int, body: DecisionIn):
    return _call(describe.decide, annotation_id, body.decision, actor=body.actor, reason=body.reason)


@router.post('/api/catalog/ingest')
def api_ingest(body: IngestIn):
    return _call(ingest.ingest_entries, list(body.entryIds), actor='web')


STATUSES = store.ENTRY_STATUSES


@router.get('/catalog', response_class=HTMLResponse)
def catalog_page(request: Request, collection: Id | None = None, type: str | None = Query(None, max_length=50),
                 status: str | None = Query(None, max_length=30), conflict: bool = False, dup: bool = False,
                 group: Id | None = None, q: str | None = Query(None, max_length=500), after: Id | None = None,
                 minGap: int = Query(30, ge=1, le=36500), limit: int = Query(100, ge=1, le=store.MAX_PAGE)):
    if status is not None and status not in STATUSES:
        status = None
    collections = store.list_collections()
    selected = next((c for c in collections if c['id'] == collection), None)
    filters = _filters(collection, None, False, type or None, None, status, conflict, group, dup, None, False, q or None)
    page = _call(store.list_entries, filters, after_id=after, limit=limit)
    report = coverage.coverage_report(collection, min_gap_days=minGap) if selected else None
    context = {
        'collections': collections, 'selected': store.get_collection(collection) if selected else None,
        'coverage': report['collections'][0] if report else None, 'page': page,
        'filters': {'collection': collection, 'type': type or '', 'status': status or '', 'conflict': conflict, 'dup': dup,
                    'group': group, 'q': q or '', 'minGap': minGap, 'limit': limit},
        'types': store.type_options(), 'statuses': STATUSES,
        'groups': store.list_groups(collection_id=collection, limit=20)['groups'],
        'collisions': store.list_collisions(collection_id=collection, limit=20)['collisions'],
        'annotations': describe.list_annotations(collection, 20) if selected else [],
        'scans': store.list_scans(collection, 10) if selected else [],
        'providers': ['none', *sorted(describe.PROVIDERS)],
    }
    base = {'collection': collection, 'type': type, 'status': status, 'conflict': 'true' if conflict else None,
            'dup': 'true' if dup else None, 'group': group, 'q': q, 'minGap': minGap if minGap != 30 else None,
            'limit': limit if limit != 100 else None}

    def query(**overrides) -> str:
        values = {**base, **overrides}
        return '/catalog?' + urlencode({k: v for k, v in values.items() if v not in (None, '', False)})

    def export_url(fmt: str) -> str:
        values = {'format': fmt, 'collectionId': collection}
        return '/api/catalog/export?' + urlencode({k: v for k, v in values.items() if v is not None})

    context.update(query=query, export_url=export_url,
                   next_url=query(after=page['next_after_id']) if page['has_more'] else None)
    return templates.TemplateResponse(request, 'catalog.html', context)
