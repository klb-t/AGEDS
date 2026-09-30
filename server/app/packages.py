"""Read-only, self-contained metadata snapshots and structural verification.

This package carries literal references and recorded bytes hashes, never source
bytes. Its digest detects changes against a known digest; it is not a signature,
an authorship proof, a truth check, or a promise of live-state restoration.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

SCHEMA_ID = 'ageds.metadata-package/v1'
CANONICALIZATION = 'sorted-keys-compact-utf8-no-nan/v1'
TABLES = (
    'cases', 'sources', 'artifacts', 'source_observations', 'events',
    'processing_runs', 'derived_text', 'annotations', 'evidence_anchors',
    'tags', 'artifact_tags', 'event_tags', 'links', 'jobs', 'audit_log',
    'schema_migrations',
)
ORDER_COLUMNS = {
    'artifact_tags': 'artifact_id,tag_id', 'event_tags': 'event_id,tag_id',
    'schema_migrations': 'version',
}
OBJECT_TABLES = {
    'case': 'cases', 'source': 'sources', 'artifact': 'artifacts',
    'source_observation': 'source_observations', 'event': 'events',
    'processing_run': 'processing_runs', 'derived_text': 'derived_text',
    'annotation': 'annotations', 'evidence_anchor': 'evidence_anchors',
    'tag': 'tags', 'job': 'jobs', 'link': 'links',
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _integrity(payload: dict) -> dict:
    return {
        'algorithm': 'sha256', 'canonicalization': CANONICALIZATION,
        'payload_sha256': _sha(payload),
        'table_sha256': {name: _sha(payload['tables'][name]) for name in TABLES},
    }


def export_metadata_package() -> dict:
    """Snapshot all core metadata in one read transaction, without migration.

    Older databases can be inspected without rewriting them: missing additive
    tables are explicit, and historical rows are not fabricated. A consumer must
    not treat empty missing tables as evidence that no such history existed.
    """
    # Verification can be used in an isolated CLI without importing config,
    # whose initialization creates live storage directories.
    from .db import connect
    db = connect(read_only=True)
    try:
        db.execute('BEGIN')
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = {}
        for name in TABLES:
            tables[name] = ([dict(row) for row in db.execute(
                f'SELECT * FROM {name} ORDER BY {ORDER_COLUMNS.get(name, "id")}')]
                if name in existing else [])
        payload = {
            'schema': SCHEMA_ID, 'metadata_only': True,
            'source_bytes_included': False, 'replay_supported': False,
            'signed': False,
            'exported_at': datetime.now(timezone.utc).isoformat(),
            'missing_tables': [name for name in TABLES if name not in existing],
            'tables': tables,
        }
        # Reject nonfinite numeric database values instead of emitting JSON NaN.
        payload['integrity'] = _integrity(payload)
        db.rollback()
        return payload
    finally:
        db.close()


def validate_metadata_package(package: Any) -> dict:
    """Verify hashes and the metadata graph without reading referenced paths.

    Return structured errors rather than mutating a database. Active jobs and
    leases are historical metadata here and never resumed by this function.
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    counts: dict[str, int] = {}
    digest_valid = True
    relationships_valid = True
    anchors_valid = True

    def error(code: str, path: str, message: str, group: str = 'relationships'):
        nonlocal digest_valid, relationships_valid, anchors_valid
        errors.append({'code': code, 'path': path, 'message': message})
        if group == 'digest':
            digest_valid = False
        elif group == 'anchors':
            anchors_valid = False
        else:
            relationships_valid = False

    def warning(code: str, path: str, message: str):
        warnings.append({'code': code, 'path': path, 'message': message})

    def result() -> dict:
        return {'valid': not errors, 'digest_valid': digest_valid,
                'relationships_valid': relationships_valid, 'anchors_valid': anchors_valid,
                'errors': errors, 'warnings': warnings, 'counts': counts}

    if not isinstance(package, dict):
        error('invalid_package', '$', 'Package must be a JSON object.', 'digest')
        return result()
    if package.get('schema') != SCHEMA_ID:
        error('unsupported_schema', '$.schema', f'Expected {SCHEMA_ID}.', 'digest')
    for field, expected in (('metadata_only', True), ('source_bytes_included', False),
                            ('replay_supported', False), ('signed', False)):
        if package.get(field) is not expected:
            error('invalid_capability_flag', f'$.{field}', f'{field} must be {expected}.', 'digest')
    tables = package.get('tables')
    if not isinstance(tables, dict):
        error('invalid_tables', '$.tables', 'Tables must be a JSON object.')
        return result()
    integrity = package.get('integrity')
    if not isinstance(integrity, dict):
        error('missing_integrity', '$.integrity', 'Integrity metadata is required.', 'digest')
        integrity = {}
    if integrity.get('algorithm') != 'sha256' or integrity.get('canonicalization') != CANONICALIZATION:
        error('unsupported_integrity', '$.integrity', 'Unknown hash or canonicalization contract.', 'digest')
    payload = {key: value for key, value in package.items() if key != 'integrity'}
    try:
        if integrity.get('payload_sha256') != _sha(payload):
            error('payload_digest_mismatch', '$.integrity.payload_sha256', 'Metadata payload digest does not match.', 'digest')
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        error('noncanonical_json', '$', str(exc), 'digest')
    table_digests = integrity.get('table_sha256')
    if not isinstance(table_digests, dict):
        error('invalid_table_digests', '$.integrity.table_sha256', 'Each core table must have a digest.', 'digest')
        table_digests = {}

    rows: dict[str, list[dict]] = {}
    indexes: dict[str, dict[int, dict]] = {}
    for name in TABLES:
        value = tables.get(name)
        if not isinstance(value, list):
            error('missing_or_invalid_table', f'$.tables.{name}', 'Core table must be an array.')
            value = []
        counts[name] = len(value)
        try:
            if table_digests.get(name) != _sha(value):
                error('table_digest_mismatch', f'$.tables.{name}', 'Table digest does not match.', 'digest')
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
            error('noncanonical_table_json', f'$.tables.{name}', str(exc), 'digest')
        rows[name] = []
        indexes[name] = {}
        seen_joins: set[tuple] = set()
        for position, row in enumerate(value):
            path = f'$.tables.{name}[{position}]'
            if not isinstance(row, dict):
                error('invalid_row', path, 'Table row must be an object.')
                continue
            rows[name].append(row)
            if name in ('artifact_tags', 'event_tags'):
                keys = ('artifact_id', 'tag_id') if name == 'artifact_tags' else ('event_id', 'tag_id')
                identity = tuple(row.get(key) for key in keys)
                if any(type(part) is not int or part <= 0 for part in identity):
                    error('invalid_join_identity', path, 'Join IDs must be positive integers.')
                elif identity in seen_joins:
                    error('duplicate_join_identity', path, 'Repeated join identity.')
                else:
                    seen_joins.add(identity)
                continue
            key = 'version' if name == 'schema_migrations' else 'id'
            identity = row.get(key)
            if type(identity) is not int or identity <= 0:
                error('invalid_identity', f'{path}.{key}', 'ID must be a positive integer.')
            elif identity in indexes[name]:
                error('duplicate_identity', f'{path}.{key}', 'Repeated table identity.')
            else:
                indexes[name][identity] = row

    def reference(row: dict, field: str, target: str, path: str, required=False) -> dict | None:
        identity = row.get(field)
        if identity is None:
            if required:
                error('missing_reference', f'{path}.{field}', f'Required reference to {target}.')
            return None
        if type(identity) is not int or identity not in indexes[target]:
            error('broken_reference', f'{path}.{field}', f'Referenced {target} ID does not exist.')
            return None
        return indexes[target][identity]

    def lookup(table: str, identity: Any) -> dict | None:
        return indexes[table].get(identity) if type(identity) is int else None

    relations = {
        'sources': (('case_id', 'cases', True),),
        'artifacts': (('source_id', 'sources', False), ('parent_artifact_id', 'artifacts', False)),
        'source_observations': (('artifact_id', 'artifacts', True), ('source_id', 'sources', False)),
        'events': (('artifact_id', 'artifacts', False), ('source_id', 'sources', False)),
        'processing_runs': (('job_id', 'jobs', False), ('artifact_id', 'artifacts', False)),
        'derived_text': (('artifact_id', 'artifacts', True), ('run_id', 'processing_runs', False)),
        'annotations': (('artifact_id', 'artifacts', False), ('event_id', 'events', False), ('derived_text_id', 'derived_text', False)),
        'evidence_anchors': (('artifact_id', 'artifacts', True), ('derived_text_id', 'derived_text', True)),
        'artifact_tags': (('artifact_id', 'artifacts', True), ('tag_id', 'tags', True)),
        'event_tags': (('event_id', 'events', True), ('tag_id', 'tags', True)),
        'jobs': (('artifact_id', 'artifacts', False),),
    }
    for table, specifications in relations.items():
        for position, row in enumerate(rows[table]):
            path = f'$.tables.{table}[{position}]'
            for field, target, required in specifications:
                reference(row, field, target, path, required)

    artifact_cases: dict[int, set] = {}
    for identity, artifact in indexes['artifacts'].items():
        source = lookup('sources', artifact.get('source_id'))
        case = source.get('case_id') if source else None
        artifact_cases[identity] = {case} if type(case) is int else set()
    for observation in rows['source_observations']:
        source = lookup('sources', observation.get('source_id'))
        case = source.get('case_id') if source else None
        artifact_id = observation.get('artifact_id')
        if type(artifact_id) is int and artifact_id in artifact_cases and type(case) is int:
            artifact_cases[artifact_id].add(case)
        artifact = lookup('artifacts', artifact_id)
        if artifact:
            for field in ('sha256', 'size_bytes'):
                if artifact.get(field) is not None and observation.get(field) is not None and artifact[field] != observation[field]:
                    error('observation_content_mismatch', f'$.tables.source_observations[id={observation.get("id")}].{field}', 'Observation content identity differs from its artifact.')
    for identity, cases in artifact_cases.items():
        if len(cases) > 1:
            error('cross_case_artifact', f'$.tables.artifacts[id={identity}]', 'Artifact provenance crosses case boundaries.')
        if not cases:
            warning('unknown_artifact_case', f'$.tables.artifacts[id={identity}]', 'Artifact case provenance is unknown.')
        parent_id = indexes['artifacts'][identity].get('parent_artifact_id')
        parent_cases = artifact_cases.get(parent_id, set()) if type(parent_id) is int else set()
        if cases and parent_cases and cases != parent_cases:
            error('cross_case_parent_artifact', f'$.tables.artifacts[id={identity}].parent_artifact_id', 'Artifact and its parent cross case boundaries.')
    for row in rows['events']:
        artifact_id = row.get('artifact_id')
        cases = artifact_cases.get(artifact_id, set()) if type(artifact_id) is int else set()
        source = lookup('sources', row.get('source_id'))
        source_case = source.get('case_id') if source else None
        if cases and type(source_case) is int and cases != {source_case}:
            error('cross_case_event', f'$.tables.events[id={row.get("id")}]', 'Event source and artifact belong to different cases.')

    for run in rows['processing_runs']:
        job = lookup('jobs', run.get('job_id'))
        if job and job.get('artifact_id') is not None and run.get('artifact_id') != job.get('artifact_id'):
            error('run_job_artifact_mismatch', f'$.tables.processing_runs[id={run.get("id")}]', 'Run and job artifacts disagree.')
    for text in rows['derived_text']:
        run = lookup('processing_runs', text.get('run_id'))
        if run and run.get('artifact_id') != text.get('artifact_id'):
            error('text_run_artifact_mismatch', f'$.tables.derived_text[id={text.get("id")}]', 'Text version and processing run artifacts disagree.')
    for annotation in rows['annotations']:
        text = lookup('derived_text', annotation.get('derived_text_id'))
        event = lookup('events', annotation.get('event_id'))
        implied = {item.get('artifact_id') for item in (text, event) if item and type(item.get('artifact_id')) is int}
        if type(annotation.get('artifact_id')) is int:
            implied.add(annotation['artifact_id'])
        if len(implied) > 1:
            error('annotation_artifact_mismatch', f'$.tables.annotations[id={annotation.get("id")}]', 'Annotation target artifacts disagree.')
        source = lookup('sources', event.get('source_id')) if event else None
        event_case = source.get('case_id') if source else None
        if type(event_case) is int and any(artifact_cases.get(identity, set()) and artifact_cases[identity] != {event_case} for identity in implied):
            error('cross_case_annotation_event', f'$.tables.annotations[id={annotation.get("id")}]', 'Annotation event source crosses its artifact case boundary.')

    def polymorphic(kind, identity, path, strict):
        if kind is None and identity is None:
            return
        target = OBJECT_TABLES.get(kind) if isinstance(kind, str) else None
        if target is None:
            if strict:
                error('unknown_link_target_kind', path, 'Unknown target kind cannot be verified.')
            else:
                warning('unknown_audit_target_kind', path, 'Domain-specific audit target cannot be resolved by v1.')
        elif type(identity) is not int or identity not in indexes[target]:
            error('broken_object_reference', path, f'Referenced {target} ID does not exist.')

    for link in rows['links']:
        path = f'$.tables.links[id={link.get("id")}]'
        for side in ('left', 'right'):
            polymorphic(link.get(f'{side}_kind'), link.get(f'{side}_id'), f'{path}.{side}', True)
    for audit in rows['audit_log']:
        polymorphic(audit.get('object_kind'), audit.get('object_id'), f'$.tables.audit_log[id={audit.get("id")}]', False)

    # Selector validation uses only text data pinned by derived_text_id. A later
    # transcript for the same artifact has no effect on the selected quote.
    for anchor in rows['evidence_anchors']:
        path = f'$.tables.evidence_anchors[id={anchor.get("id")}]'
        text = lookup('derived_text', anchor.get('derived_text_id'))
        if not text or text.get('kind') != 'transcript' or text.get('artifact_id') != anchor.get('artifact_id'):
            error('anchor_version_mismatch', path, 'Anchor must pin a text version of its artifact.', 'anchors')
            continue
        quote = anchor.get('quote_text')
        try:
            quote_digest = hashlib.sha256(quote.encode('utf-8')).hexdigest() if isinstance(quote, str) else None
        except UnicodeError:
            quote_digest = None
        if quote_digest is None or quote_digest != anchor.get('quote_sha256'):
            error('anchor_quote_digest_mismatch', path, 'Quote UTF-8 SHA-256 does not match.', 'anchors')
        start, end = anchor.get('start_ms'), anchor.get('end_ms')
        if type(start) is not int or type(end) is not int or start < 0 or end < start:
            error('invalid_anchor_interval', path, 'Anchor interval must be nonnegative integer milliseconds.', 'anchors')
        try:
            selector = json.loads(anchor.get('selector_json', ''))
            segments = json.loads(text.get('segments_json', '[]'))
            projection = _select_quote(selector, segments)
            if (quote, anchor.get('quote_sha256'), start, end) != (projection['quote_text'],projection['quote_sha256'],projection['start_ms'],projection['end_ms']):
                error('anchor_selector_mismatch', path, 'Quote or interval differs from the pinned selector.', 'anchors')
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, UnicodeError, RecursionError) as exc:
            error('invalid_anchor_selector', path, str(exc), 'anchors')

    missing = package.get('missing_tables')
    if not isinstance(missing, list) or any(name not in TABLES for name in missing):
        error('invalid_missing_tables', '$.missing_tables', 'Missing tables must name core tables.')
    elif missing:
        warning('legacy_incomplete_schema', '$.missing_tables', 'Missing historical tables are unknown history, not reconstructed empty history.')
        for name in missing:
            if rows[name]:
                error('contradictory_missing_table', f'$.tables.{name}', 'A missing table cannot also contain rows.')
    warning('unsigned_metadata_only', '$', 'Digest is not authentication; source bytes and replay are not included or verified.')
    return result()


def _select_quote(selector: Any, segments: Any) -> dict:
    """The v1 selector contract; rejects implicit guesses or lossy fallbacks."""
    if not isinstance(selector, dict) or not isinstance(segments, list):
        raise ValueError('Selector must be an object and segments an array.')
    if selector.get('kind') != 'segments':
        raise ValueError('Unsupported selector kind.')
    from .citations import projection_from_segments
    projection = projection_from_segments(segments, selector.get('indices'))
    if canonical_json(selector) != canonical_json(projection['selector']):
        raise ValueError('Selector contract differs from the canonical exact-concatenation selector.')
    return projection
