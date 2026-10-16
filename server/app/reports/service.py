"""Create, verify and list reports (shared by CLI and HTTP)."""
from __future__ import annotations

import os
import uuid
from contextlib import closing
from pathlib import Path

from . import definitions, outputs
from .bundle import export_bundle
from .common import BUNDLE_DIR, ReportInputError, ReportNotFound, canonical_json, sha256_bytes, sha256_json, utc_now
from .document import build_document
from .manifest import MANIFEST_NAME, build_manifest, verify_report
from .paths import Publication, check_output_dir
from .providers import SectionContext, run_section
from .render import render_html, template_files
from .store import get_report, insert_report, list_reports


def _check_references(db, params: dict) -> None:
    if params['collection_id'] is not None:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collections'").fetchone()
        if not exists or not db.execute('SELECT 1 FROM collections WHERE id=?', (params['collection_id'],)).fetchone():
            raise ReportInputError(f'collection {params["collection_id"]} does not exist')


def compute_sections(report_type: str, params: dict) -> list[dict]:
    """Run every planned section in ONE read-only transaction (no writes)."""
    from ..db import connect
    with closing(connect(read_only=True)) as db:
        db.execute('BEGIN')
        try:
            _check_references(db, params)
            scope: dict = {'_sections': []}
            results = []
            for name, options in definitions.section_plan(report_type, params):
                ctx = SectionContext(db=db, params=params, lang=params['language'], options=options, scope=scope,
                                     bundle_enabled=params['include_citation_bundle'])
                result = run_section(name, ctx)
                results.append(result)
                scope['_sections'].append(result)
            return results
        finally:
            db.rollback()


def _citation_pairs(sections: list[dict]) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    for section in sections:
        for artifact_id, anchor_id in section['citations']:
            if (artifact_id, anchor_id) not in pairs:
                pairs.append((artifact_id, anchor_id))
    return pairs


def _apply_bundle_result(sections: list[dict], bundle: dict | None) -> None:
    """Keep packet links only for packets that were actually written."""
    written = {(p['artifact_id'], p['anchor_id']) for p in bundle['packets']} if bundle else set()
    for section in sections:
        for block in section['blocks']:
            if block['kind'] != 'table' or not any(c['key'] == 'packet' for c in block['columns']):
                continue
            for row in block['rows']:
                if (row.get('artifact_id'), row.get('anchor_id')) not in written:
                    row['packet'] = None
                    row['packet_href'] = None
        if bundle:
            for item in bundle['excluded']:
                if [item['artifact_id'], item['anchor_id']] in section['citations']:
                    section['exclusions'][f'packet_not_exported:{item["anchor_id"]}'] = item['reason']


def create_report(report_type: str, params: dict | None, output_dir, *, actor: str = 'user') -> dict:
    params = definitions.normalize_params(report_type, params)
    definition = definitions.TYPES[report_type]
    check_output_dir(output_dir)  # refuse early, before any work
    sections = compute_sections(report_type, params)
    report_uid = uuid.uuid4().hex
    generated_at = utc_now()
    stamp = generated_at.replace('-', '').replace(':', '')
    publication = Publication(Path(output_dir), f'ageds-report-{report_type}-{stamp}-{report_uid[:12]}')
    try:
        staging = publication.staging
        pairs = _citation_pairs(sections)
        bundle_manifest = None
        if params['include_citation_bundle'] and pairs:
            bundle_manifest = export_bundle(pairs, staging / BUNDLE_DIR, report_uid=report_uid)
        _apply_bundle_result(sections, bundle_manifest)
        document = build_document(report_uid=report_uid, report_type=report_type, definition=definition,
                                  params=params, sections=sections, generated_at=generated_at,
                                  template_files=template_files(definition['template']))
        outputs.write_new(staging / 'report.json', (_pretty(document)))
        outputs.write_new(staging / 'report.html', render_html(document, definition['template']))
        formats: dict = {'html': {'status': 'written', 'files': [{'path': 'report.html'}]},
                         'json': {'status': 'written', 'files': [{'path': 'report.json'}]}}
        for name in ('csv', 'xlsx', 'pdf'):
            if name not in params['formats']:
                formats[name] = {'status': 'not_requested'}
        if 'csv' in params['formats']:
            formats['csv'] = outputs.write_csv_tables(document, staging)
        if 'xlsx' in params['formats']:
            formats['xlsx'] = outputs.write_xlsx(document, staging)
        if 'pdf' in params['formats']:
            formats['pdf'] = outputs.write_pdf(staging)
        bundle_ref = None
        if bundle_manifest is not None:
            bundle_ref = {'path': BUNDLE_DIR, 'schema': bundle_manifest['schema'],
                          'packets': len(bundle_manifest['packets']), 'excluded': len(bundle_manifest['excluded']),
                          'payload_sha256': bundle_manifest['integrity']['payload_sha256']}
        manifest = build_manifest(document, staging, formats, bundle_ref)
        manifest_raw = canonical_json(manifest)
        outputs.write_new(staging / MANIFEST_NAME, manifest_raw)
        final = publication.publish()
    except BaseException:
        publication.abort()
        raise
    manifest_sha = sha256_bytes(manifest_raw)
    from ..db import session
    row = {'report_uid': report_uid, 'report_type': report_type, 'template_id': document['method']['template']['id'],
           'template_version': definition['version'], 'params_json': canonical_json(params).decode('utf-8'),
           'params_sha256': sha256_json(params), 'content_sha256': document['integrity']['content_sha256'],
           'manifest_sha256': manifest_sha, 'output_dir': str(final), 'manifest_name': MANIFEST_NAME,
           'formats_json': canonical_json({k: v['status'] for k, v in formats.items()}).decode('utf-8'),
           'sections_json': canonical_json(document['method']['sections']).decode('utf-8'),
           'citations_json': canonical_json([list(pair) for pair in pairs]).decode('utf-8'),
           'ageds_version': document['method']['ageds_version'], 'git_commit': document['method']['git_commit'],
           'created_at': generated_at}
    with session() as db:
        report_id = insert_report(db, row)
    return {'report_id': report_id, 'report_uid': report_uid, 'report_type': report_type, 'output_dir': str(final),
            'manifest': str(final / MANIFEST_NAME), 'manifest_sha256': manifest_sha,
            'content_sha256': document['integrity']['content_sha256'],
            'formats': {k: v['status'] for k, v in formats.items()},
            'sections': {s['section']: s['status'] for s in document['method']['sections']},
            'files': len(manifest['files']), 'signed': False}


def _pretty(document: dict) -> bytes:
    import json
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode('utf-8') + b'\n'


def verify_registered(report_id: int) -> dict:
    """Verify a registered report's files against its manifest and stored digest."""
    from ..db import connect
    with closing(connect(read_only=True)) as db:
        row = get_report(db, report_id)
    manifest = Path(row['output_dir']) / row['manifest_name']
    result = verify_report(manifest, expected_manifest_sha256=row['manifest_sha256'])
    result['report_id'] = report_id
    if result['report_uid'] not in (None, row['report_uid']):
        result['errors'].append({'code': 'register_uid_mismatch', 'path': '$.report_uid',
                                 'message': 'Manifest belongs to another report than the register row.'})
        result['valid'] = False
    return result


def registered(report_id: int) -> dict:
    from ..db import connect
    with closing(connect(read_only=True)) as db:
        return get_report(db, report_id)


def listing(*, report_type: str | None = None, limit: int = 100, include_output_dir: bool = True) -> list[dict]:
    from ..db import connect
    with closing(connect(read_only=True)) as db:
        from .store import table_exists
        if not table_exists(db):
            return []
        return list_reports(db, report_type=report_type, limit=limit, include_output_dir=include_output_dir)


def report_file(report_id: int, relative: str) -> tuple[Path, dict]:
    """A file of a registered report, only if listed in its verified manifest."""
    row = registered(report_id)
    root = Path(row['output_dir'])
    from .bundle_consumer import read_regular, strict_json
    raw = read_regular(root / row['manifest_name'], 64 * 1024 * 1024)
    if sha256_bytes(raw) != row['manifest_sha256']:
        raise ReportNotFound('manifest changed since registration; file not served')
    manifest = strict_json(raw)
    entry = next((item for item in manifest.get('files', []) if item.get('path') == relative), None)
    if entry is None:
        raise ReportNotFound('file is not listed in the report manifest')
    path = root / relative
    if os.path.islink(path) or not os.path.realpath(path).startswith(os.path.realpath(root) + os.sep):
        raise ReportNotFound('file path is unsafe')
    return path, entry
