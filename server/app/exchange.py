"""Bounded, read-only citation evidence; no locator access or restoration."""
from __future__ import annotations

import hashlib
import json
import re

from .citations import MAX_MILLISECONDS, loads_transcript_segments, projection_from_selector

SCHEMA_ID = 'ageds.citation-evidence/v1'
DOMAIN = SCHEMA_ID + '\n'
CANONICALIZATION = 'python-json-sorted-keys-compact-utf8-no-nan/v1'
MAX_PACKET_BYTES = 2 * 1024 * 1024
MAX_SEGMENTS = 10_000
MAX_OBSERVATIONS = 1_000
MAX_DEPTH = 32
MAX_NODES = 100_000


class PacketNotFound(ValueError):
    """Requested artifact/anchor pair does not exist."""


class PacketLimitError(ValueError):
    """Evidence exceeds the bounded exchange profile."""


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def canonical_packet_bytes(packet) -> bytes:
    """Serialize the wire profile and enforce its outer byte/structure bounds."""
    try:
        _budget(packet, [MAX_NODES])
        data = canonical_json(packet)
        if len(data) > MAX_PACKET_BYTES:
            raise PacketLimitError('citation packet exceeds byte limit')
        return data
    except (TypeError, UnicodeError, RecursionError, OverflowError) as error:
        raise ValueError('citation packet cannot be serialized: ' + str(error)) from error


def _budget(value, remaining, depth=0):
    remaining[0] -= 1
    container = isinstance(value, (dict, list))
    if container:
        depth += 1
    if depth > MAX_DEPTH or remaining[0] < 0:
        raise PacketLimitError('citation packet exceeds structural limit')
    if isinstance(value, dict):
        for key, child in value.items():
            _budget(key, remaining, depth)
            _budget(child, remaining, depth)
    elif isinstance(value, list):
        for child in value:
            _budget(child, remaining, depth)


def _strict_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('selector contains duplicate JSON keys')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite selector')))


def _identity(value):
    if type(value) is not int or not 0 < value <= MAX_MILLISECONDS:
        raise ValueError('record identity must be a positive SQLite integer')


def _content(row):
    digest, size = row.get('sha256'), row.get('size_bytes')
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
        raise ValueError('invalid recorded source SHA256')
    if size is not None and (type(size) is not int or size < 0):
        raise ValueError('invalid recorded source byte count')


def _rows(db, table, where, params, *, excluded=(), limit=1):
    """Bound row payload in SQLite before returning text into Python.

    Table/column names below are internal schema identifiers, never user input.
    No migration, temporary table, audit write or source-path read occurs.
    """
    columns = [row[1] for row in db.execute(f'PRAGMA table_info({table})') if row[1] not in excluded]
    if not columns:
        return []
    names = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
    lengths = '+'.join(f'coalesce(length(cast("{name}" as blob)),0)' for name in columns)
    # Count/size first within the same read transaction; no oversized row fetch.
    summary = db.execute(f'SELECT count(*),coalesce(sum({lengths}),0) FROM {table} WHERE {where}', params).fetchone()
    if summary[0] > limit or summary[1] > MAX_PACKET_BYTES:
        raise PacketLimitError('citation packet exceeds row or byte limit')
    return [dict(row) for row in db.execute(f'SELECT {names} FROM {table} WHERE {where} ORDER BY id', params)]


def export_citation_packet(artifact_id: int, anchor_id: int, *, include_stored_path: bool = False) -> dict:
    """Export one pinned quote in one SQLite read-only snapshot, or ValueError.

    All source paths remain literal metadata. The default removes the private
    server storage path; it does not anonymize names, locators or raw metadata.
    Auxiliary raw JSON is opaque and retained even when malformed.
    """
    if any(type(value) is not int or not 0 < value <= MAX_MILLISECONDS for value in (artifact_id, anchor_id)):
        raise ValueError('artifact and anchor IDs must be positive SQLite integers')
    if type(include_stored_path) is not bool:
        raise ValueError('include_stored_path must be a boolean')
    from .db import connect
    db = connect(read_only=True)
    try:
        db.execute('BEGIN')
        artifacts = _rows(db, 'artifacts', 'id=?', (artifact_id,),
                          excluded=() if include_stored_path else ('stored_path',))
        anchors = _rows(db, 'evidence_anchors', 'id=? AND artifact_id=?', (anchor_id, artifact_id))
        if not artifacts or not anchors:
            raise PacketNotFound('citation does not belong to the requested artifact')
        artifact, anchor = artifacts[0], anchors[0]
        versions = _rows(db, 'derived_text', "id=? AND artifact_id=? AND kind='transcript'",
                         (anchor['derived_text_id'], artifact_id))
        if not versions:
            raise ValueError('citation has no matching pinned transcript version')
        version = versions[0]
        _identity(version['id'])
        if not isinstance(version['text'], str):
            raise ValueError('stored transcript text must be literal text')
        segments = loads_transcript_segments(version['segments_json'])
        if len(segments) > MAX_SEGMENTS:
            raise PacketLimitError('citation packet exceeds segment limit')
        selector = _strict_object(anchor['selector_json'])
        remaining = [MAX_NODES]
        _budget(segments, remaining)
        _budget(selector, remaining)
        projection = projection_from_selector(segments, selector)
        for field in ('quote_text', 'quote_sha256', 'start_ms', 'end_ms'):
            if canonical_json(anchor[field]) != canonical_json(projection[field]):
                raise ValueError('stored citation differs from pinned transcript: ' + field)
        sources = _rows(db, 'sources', 'id=?', (artifact.get('source_id'),))
        observations = _rows(db, 'source_observations', 'artifact_id=?', (artifact_id,), limit=MAX_OBSERVATIONS)
        runs = _rows(db, 'processing_runs', 'id=?', (version.get('run_id'),), excluded=('lease_token',))
        source, run = (sources[0] if sources else None), (runs[0] if runs else None)
        if artifact.get('source_id') is not None and source is None:
            raise ValueError('artifact references missing source')
        if version.get('run_id') is not None and (run is None or run['artifact_id'] != artifact_id):
            raise ValueError('transcript references missing or foreign processing run')
        if source is not None:
            _identity(source['id'])
            _identity(artifact['source_id'])
        if run is not None:
            _identity(run['id'])
            _identity(version['run_id'])
        _content(artifact)
        for observation in observations:
            _identity(observation['id'])
            if observation.get('source_id') is not None:
                _identity(observation['source_id'])
            _content(observation)
            for field in ('sha256', 'size_bytes'):
                if observation[field] is not None and artifact[field] is not None and observation[field] != artifact[field]:
                    raise ValueError('acquisition content differs from artifact: ' + field)
        unknowns = []
        for condition, name in ((artifact.get('sha256') is None, 'source_content_digest_unknown'),
                                (artifact.get('size_bytes') is None, 'source_size_unknown'),
                                (source is None, 'source_record_unknown'),
                                (not observations, 'acquisition_history_unknown'),
                                (any(row['acquisition_kind'] == 'legacy_snapshot' for row in observations), 'acquisition_history_partial'),
                                (run is None, 'processing_run_unknown')):
            if condition:
                unknowns.append(name)
        if run:
            if run.get('status') != 'done':
                unknowns.append('processing_run_success_unconfirmed')
            for field in ('tool', 'tool_version', 'provider', 'model', 'model_version'):
                if run.get(field) in (None, '', 'unknown'):
                    unknowns.append('processing_run.' + field + '_unknown')
        payload = dict(artifact=artifact, source=source, source_observations=observations,
                       processing_run=run, derived_text=version, anchor=anchor,
                       projection=projection, unknowns=unknowns, scope={
                           'source_bytes_included': False, 'media_bytes_included': False,
                           'live_restore_supported': False, 'tasks_imported': False,
                           'locators': 'literal_inert_metadata', 'stored_path_included': include_stored_path})
        packet = {'schema': SCHEMA_ID, 'payload': payload, 'integrity': {
            'algorithm': 'sha256', 'canonicalization': CANONICALIZATION, 'domain': DOMAIN,
            'payload_sha256': hashlib.sha256(DOMAIN.encode('utf-8') + canonical_json(payload)).hexdigest()}}
        _budget(packet, remaining)
        if len(canonical_json(packet)) > MAX_PACKET_BYTES:
            raise PacketLimitError('citation packet exceeds byte limit')
        return packet
    except (TypeError, UnicodeError, RecursionError, OverflowError) as error:
        raise ValueError('citation packet cannot represent stored data: ' + str(error)) from error
    finally:
        db.rollback()
        db.close()
