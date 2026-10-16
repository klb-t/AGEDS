"""Annotation proposals from external clients, quarantined until a human decides.

A client with the ``propose`` scope submits a proposal; it is stored with
``status='proposed'`` and never applied automatically. A human accepts it via
the AGEDS UI or CLI, which creates the real ``annotations`` row with the same
validation rules as the native annotation API, and records provenance
(proposal id, client id, token id, mode, provider, model) in the proposal row
and in ``audit_log``. Rejection keeps the row with its decision.
"""
from __future__ import annotations

import json
import re

from ..citations import MAX_MILLISECONDS, loads_transcript_segments, milliseconds
from ..db import session
from .tokens import utc_now

MAX_BODY_CHARS = 65_536
MAX_LABEL_CHARS = 200
MAX_BASIS = 200
MAX_NOTE_CHARS = 2000
_ITEM_ID = re.compile(r'^(segment|event|artifact|citation|derived_text|quote):[0-9:\-]{1,64}$')
_IDENT = re.compile(r'^[A-Za-z0-9_.:-]{1,200}$')


class ProposalError(ValueError):
    """Invalid proposal input or decision (HTTP 422)."""


class ProposalNotFound(ValueError):
    """Proposal or referenced object does not exist (HTTP 404)."""


def _proposal_payload(row) -> dict:
    result = dict(row)
    result['basis'] = json.loads(result.pop('basis_json'))
    result['metadata'] = json.loads(result.pop('metadata_json'))
    return result


def submit_proposal(payload: dict, *, client_id: str, token_id: int | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ProposalError('proposal must be a JSON object')
    if not isinstance(client_id, str) or not _IDENT.match(client_id):
        raise ProposalError('client_id must match [A-Za-z0-9_.:-]{1,200}')
    allowed = {'artifact_id', 'derived_text_id', 'kind', 'label', 'body', 'start_ms', 'end_ms', 'basis', 'mode', 'provider', 'model', 'metadata'}
    extra = set(payload) - allowed
    if extra:
        raise ProposalError('unknown proposal fields: ' + ', '.join(sorted(extra)))
    artifact_id = payload.get('artifact_id')
    if type(artifact_id) is not int or not 1 <= artifact_id <= MAX_MILLISECONDS:
        raise ProposalError('artifact_id must be a positive SQLite integer')
    derived_text_id = payload.get('derived_text_id')
    if derived_text_id is not None and (type(derived_text_id) is not int or not 1 <= derived_text_id <= MAX_MILLISECONDS):
        raise ProposalError('derived_text_id must be a positive SQLite integer')
    body = payload.get('body')
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY_CHARS:
        raise ProposalError(f'body must be nonblank text up to {MAX_BODY_CHARS} characters')
    kind = payload.get('kind', 'note')
    if not isinstance(kind, str) or not _IDENT.match(kind):
        raise ProposalError('kind must match [A-Za-z0-9_.:-]{1,200}')
    label = payload.get('label')
    if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL_CHARS):
        raise ProposalError('label must be text up to 200 characters')
    start_ms, end_ms = payload.get('start_ms'), payload.get('end_ms')
    if (start_ms is None) != (end_ms is None):
        raise ProposalError('both start_ms and end_ms are required for a range')
    if start_ms is not None:
        if any(type(v) is not int or not 0 <= v <= MAX_MILLISECONDS for v in (start_ms, end_ms)) or end_ms < start_ms:
            raise ProposalError('invalid proposal range')
        if derived_text_id is None:
            raise ProposalError('a timed proposal requires a concrete transcript version')
    basis = payload.get('basis', [])
    if not isinstance(basis, list) or len(basis) > MAX_BASIS or any(not isinstance(b, str) or not _ITEM_ID.match(b) for b in basis):
        raise ProposalError('basis must be an array of anchored item ids (up to 200)')
    mode = payload.get('mode', 'anchored')
    if mode not in ('anchored', 'interpretive'):
        raise ProposalError("mode must be 'anchored' or 'interpretive'")
    if mode == 'interpretive' and not basis:
        raise ProposalError('an interpretive proposal must cite at least one anchored item id')
    provider = payload.get('provider') or 'unknown'
    model = payload.get('model') or 'unknown'
    if not all(isinstance(v, str) and len(v) <= MAX_LABEL_CHARS for v in (provider, model)):
        raise ProposalError('provider and model must be text up to 200 characters')
    metadata = payload.get('metadata', {})
    if not isinstance(metadata, dict):
        raise ProposalError('metadata must be a JSON object')
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(metadata_json) > MAX_BODY_CHARS:
        raise ProposalError('metadata exceeds the size limit')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM artifacts WHERE id=?', (artifact_id,)).fetchone():
            raise ProposalNotFound('artifact not found')
        if derived_text_id is not None and not db.execute("SELECT 1 FROM derived_text WHERE id=? AND artifact_id=? AND kind='transcript'",
                                                          (derived_text_id, artifact_id)).fetchone():
            raise ProposalError('transcript version does not belong to artifact')
        cur = db.execute('''INSERT INTO annotation_proposals(token_id,client_id,artifact_id,derived_text_id,kind,label,body,start_ms,end_ms,
                            basis_json,mode,provider,model,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (token_id, client_id, artifact_id, derived_text_id, kind, label, body, start_ms, end_ms,
                          json.dumps(basis), mode, provider, model, utc_now(), metadata_json))
        proposal_id = int(cur.lastrowid)
        db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,'annotation_proposal',?,?)",
                   (f'client:{client_id}', 'proposal_submit', proposal_id,
                    json.dumps({'artifact_id': artifact_id, 'derived_text_id': derived_text_id, 'token_id': token_id, 'mode': mode,
                                'basis_count': len(basis)}, ensure_ascii=False)))
        return _proposal_payload(db.execute('SELECT * FROM annotation_proposals WHERE id=?', (proposal_id,)).fetchone())


def list_proposals(*, status: str | None = None, client_id: str | None = None, limit: int = 200) -> list[dict]:
    if status is not None and status not in ('proposed', 'accepted', 'rejected'):
        raise ProposalError('status must be proposed, accepted or rejected')
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ProposalError('limit must be an integer from 1 to 10000')
    clauses, params = [], []
    if status:
        clauses.append('status=?')
        params.append(status)
    if client_id:
        clauses.append('client_id=?')
        params.append(client_id)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    with session() as db:
        return [_proposal_payload(row) for row in db.execute(f'SELECT * FROM annotation_proposals{where} ORDER BY id LIMIT ?', (*params, limit))]


def _validate_range_against_version(db, proposal) -> None:
    if proposal['start_ms'] is None:
        return
    version = db.execute('SELECT segments_json FROM derived_text WHERE id=?', (proposal['derived_text_id'],)).fetchone()
    if version is None:
        raise ProposalError('transcript version no longer exists')
    segments = loads_transcript_segments(version['segments_json'])
    bounds = [segment['end'] for segment in segments if isinstance(segment, dict) and 'end' in segment]
    if not bounds:
        raise ProposalError('unknown transcript time bounds')
    if proposal['end_ms'] > max(milliseconds(end) for end in bounds):
        raise ProposalError('proposal range outside transcript')


def decide_proposal(proposal_id: int, decision: str, *, decided_by: str, note: str | None = None) -> dict:
    """Accept creates the annotation with provenance; reject keeps the row."""
    if type(proposal_id) is not int or proposal_id < 1:
        raise ProposalError('proposal id must be a positive integer')
    if decision not in ('accepted', 'rejected'):
        raise ProposalError("decision must be 'accepted' or 'rejected'")
    if not isinstance(decided_by, str) or not decided_by.strip() or len(decided_by) > MAX_LABEL_CHARS:
        raise ProposalError('decided_by must be nonblank text up to 200 characters')
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE_CHARS):
        raise ProposalError('note must be text up to 2000 characters')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM annotation_proposals WHERE id=?', (proposal_id,)).fetchone()
        if row is None:
            raise ProposalNotFound('proposal not found')
        if row['status'] != 'proposed':
            raise ProposalError(f'proposal already {row["status"]}')
        now = utc_now()
        annotation_id = None
        if decision == 'accepted':
            if not db.execute('SELECT 1 FROM artifacts WHERE id=?', (row['artifact_id'],)).fetchone():
                raise ProposalError('artifact no longer exists')
            try:
                _validate_range_against_version(db, row)
            except (ValueError, TypeError, KeyError) as error:
                raise ProposalError(str(error)) from error
            cur = db.execute('''INSERT INTO annotations(artifact_id,derived_text_id,kind,label,body,start_ms,end_ms)
                                VALUES(?,?,?,?,?,?,?)''',
                             (row['artifact_id'], row['derived_text_id'], row['kind'], row['label'], row['body'],
                              row['start_ms'], row['end_ms']))
            annotation_id = int(cur.lastrowid)
            provenance = {'proposal_id': proposal_id, 'client_id': row['client_id'], 'token_id': row['token_id'],
                          'mode': row['mode'], 'provider': row['provider'], 'model': row['model'],
                          'basis': json.loads(row['basis_json']), 'artifact_id': row['artifact_id'],
                          'derived_text_id': row['derived_text_id'], 'decided_by': decided_by}
            db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,'annotation',?,?)",
                       (f'human:{decided_by}', 'annotation_from_proposal', annotation_id, json.dumps(provenance, ensure_ascii=False)))
        db.execute('UPDATE annotation_proposals SET status=?, decided_at=?, decided_by=?, decision_note=?, annotation_id=? WHERE id=?',
                   (decision, now, decided_by, note, annotation_id, proposal_id))
        db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,'annotation_proposal',?,?)",
                   (f'human:{decided_by}', f'proposal_{decision}', proposal_id,
                    json.dumps({'annotation_id': annotation_id, 'note': note, 'client_id': row['client_id']}, ensure_ascii=False)))
        return _proposal_payload(db.execute('SELECT * FROM annotation_proposals WHERE id=?', (proposal_id,)).fetchone())
