"""Typed graph export for Loom and quarantine of relations Loom returns.

Export (``ageds.graph-export/v1``) projects database rows into nodes and edges
with provenance on every record: source table, row id, locator, run id and an
``epistemic_status`` from ``EPISTEMIC``. Counterparties are observed labels or
addresses (hypotheses, never identities). Claims do not exist in this revision
and are exported as an explicit empty collection with a reason.

The export is a mapping, not a merge (A08): ``loom_mapping`` says how each
AGEDS status would be spelled in Loom's ``loom.graph_packet/1`` vocabulary
(evidence class and origin), but this module does not emit a validated Loom
packet and does not share Loom's semantics for claims or confidence.

Import (``ageds.relation-hypotheses/v1``) stores relations in
``relation_hypotheses`` with ``status='quarantined'``. Nothing reaches ``links``
until a human promotes a row; promotion records the resulting ``link_id`` and
an audit entry. Unresolved references are kept as rejected rows, not dropped.
"""
from __future__ import annotations

import hashlib
import json
import re

from ..db import session
from . import read_api
from .tokens import utc_now

EXPORT_SCHEMA = 'ageds.graph-export/v1'
HYPOTHESES_SCHEMA = 'ageds.relation-hypotheses/v1'
EPISTEMIC = ('observed', 'parser_output', 'model_output', 'human_annotation', 'hypothesis')
MAX_NODES = 50_000
MAX_EDGES = 100_000
MAX_TEXT_CHARS = read_api.EVENT_TEXT_MAX_CHARS
MAX_IMPORT_ITEMS = 100_000
MAX_IMPORT_BYTES = 32 * 1024 * 1024
MAX_RATIONALE_CHARS = 4096
NODE_PREFIX = {'event': 'event', 'artifact': 'artifact', 'derived_text': 'transcript', 'annotation': 'annotation',
               'evidence_anchor': 'citation'}
KIND_BY_PREFIX = {prefix: kind for kind, prefix in NODE_PREFIX.items()}
TABLE_BY_KIND = {'event': 'events', 'artifact': 'artifacts', 'derived_text': 'derived_text', 'annotation': 'annotations',
                 'evidence_anchor': 'evidence_anchors'}
LOOM_MAPPING = {
    'observed': {'evidence_class': 'observed', 'origin': 'archive', 'note': 'captured bytes with acquisition observations'},
    'parser_output': {'evidence_class': 'derived', 'origin': 'system', 'note': 'deterministic parser projection of an artifact'},
    'model_output': {'evidence_class': 'derived', 'origin': 'archive', 'derivation': 'processing_run (ASR)',
                     'note': 'model result pinned to a run; not model_knowledge, the input is archive content'},
    'human_annotation': {'evidence_class': 'user', 'origin': 'user', 'note': 'human note or selection'},
    'hypothesis': {'evidence_class': 'inferred', 'origin': 'system', 'note': 'correlation or label hypothesis; needs a check before any promotion'},
}
_IDENT = re.compile(r'^[A-Za-z0-9_.:-]{1,200}$')
_ADDRESS = re.compile(r'^(event|artifact|transcript|annotation|citation|counterparty):([0-9a-f]{1,64})$')


class GraphInputError(ValueError):
    """Invalid export window, import document or decision (HTTP 422)."""


class HypothesisNotFound(ValueError):
    """Hypothesis row does not exist (HTTP 404)."""


def _text(value):
    if not isinstance(value, str):
        return None, False, None
    digest = hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()
    if len(value) > MAX_TEXT_CHARS:
        return value[:MAX_TEXT_CHARS], True, digest
    return value, False, digest


def _provenance(table, row_id, *, locator=None, run_id=None, epistemic):
    assert epistemic in EPISTEMIC
    return {'source_table': table, 'row_id': row_id, 'locator': locator, 'run_id': run_id, 'epistemic_status': epistemic,
            'loom': LOOM_MAPPING[epistemic]}


def counterparty_address(value: str) -> str:
    normalized = ' '.join(value.split()).casefold()
    return 'counterparty:' + hashlib.sha256(normalized.encode('utf-8', 'surrogatepass')).hexdigest()[:16]


def export_graph(*, from_: str | None = None, to: str | None = None, include_text: bool = True,
                 max_nodes: int = MAX_NODES, max_edges: int = MAX_EDGES) -> dict:
    """One read transaction; events select the window, related rows follow."""
    for name, value in (('from', from_), ('to', to)):
        if value is not None and (not isinstance(value, str) or '\x00' in value or len(value) > read_api.TIMELINE_MAX_TIME_CHARS):
            raise GraphInputError(f'{name} must be text up to {read_api.TIMELINE_MAX_TIME_CHARS} characters')
    if from_ is not None and to is not None and from_ > to:
        raise GraphInputError('from must not exceed to')
    if any(type(v) is not int or not 1 <= v <= 10_000_000 for v in (max_nodes, max_edges)):
        raise GraphInputError('max_nodes and max_edges must be integers from 1 to 10000000')
    windowed = from_ is not None or to is not None
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    truncation = {'nodes': False, 'edges': False, 'events': False, 'artifacts': False}

    def add_node(node_id, node):
        if node_id in nodes:
            return True
        if len(nodes) >= max_nodes:
            truncation['nodes'] = True
            return False
        nodes[node_id] = {'id': node_id, **node}
        return True

    def add_edge(edge):
        if len(edges) >= max_edges:
            truncation['edges'] = True
            return
        edges.append(edge)

    def reader(db):
        clauses, params = [], []
        if from_ is not None:
            clauses.append('ts_start >= ?')
            params.append(from_)
        if to is not None:
            clauses.append('ts_start <= ?')
            params.append(to)
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = db.execute(f'SELECT * FROM events{where} ORDER BY ts_start, id LIMIT ?', (*params, max_nodes + 1)).fetchall()
        truncation['events'] = len(rows) > max_nodes
        artifact_ids = set()
        counterparties: dict[str, dict] = {}
        for row in rows[:max_nodes]:
            attrs = {key: row[key] for key in ('event_type', 'ts_start', 'ts_end', 'direction', 'sender', 'recipients',
                                                'contact_label', 'phone_or_address', 'external_id', 'thread_id', 'confidence')}
            attrs['time_status'] = 'stored_ts_start_text' if row['ts_start'] else 'untimed'
            for field in ('subject', 'body'):
                text, truncated, digest = _text(row[field])
                attrs[field + '_sha256'] = digest
                if include_text:
                    attrs[field] = text
                    attrs[field + '_truncated'] = truncated
            node_id = f'event:{row["id"]}'
            if not add_node(node_id, {'kind': 'event', 'label': row['event_type'], 'attrs': attrs,
                                      'provenance': _provenance('events', row['id'], locator={'artifact_id': row['artifact_id'], 'source_id': row['source_id'], 'external_id': row['external_id']}, epistemic='parser_output')}):
                break
            if row['artifact_id'] is not None:
                artifact_ids.add(row['artifact_id'])
                add_edge({'src': node_id, 'dst': f'artifact:{row["artifact_id"]}', 'relation': 'derived_from',
                          'provenance': _provenance('events', row['id'], epistemic='parser_output')})
            for field in ('phone_or_address', 'contact_label'):
                value = row[field]
                if not isinstance(value, str) or not value.strip():
                    continue
                address = counterparty_address(value)
                party = counterparties.setdefault(address, {'kind': 'counterparty', 'label': value, 'attrs': {'observed_field': field, 'observed_in_events': 0, 'identity_claim': False, 'labels': []},
                                                            'provenance': _provenance('events', None, locator={'field': field}, epistemic='hypothesis')})
                party['attrs']['observed_in_events'] += 1
                if value not in party['attrs']['labels'] and len(party['attrs']['labels']) < 20:
                    party['attrs']['labels'].append(value)
                add_edge({'src': node_id, 'dst': address, 'relation': 'counterparty_of', 'attrs': {'direction': row['direction'], 'field': field},
                          'provenance': _provenance('events', row['id'], epistemic='parser_output')})
        for address, party in counterparties.items():
            add_node(address, party)
        if not windowed:
            extra = db.execute('SELECT id FROM artifacts ORDER BY id LIMIT ?', (max_nodes + 1,)).fetchall()
            truncation['artifacts'] = len(extra) > max_nodes
            artifact_ids.update(r['id'] for r in extra[:max_nodes])
        for artifact_id in sorted(artifact_ids):
            art = db.execute('SELECT id,source_id,sha256,original_name,mime_type,size_bytes,source_locator,created_at,is_raw FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
            if art is None:
                continue
            if not add_node(f'artifact:{artifact_id}', {'kind': 'artifact', 'label': art['original_name'],
                                                         'attrs': {k: art[k] for k in ('sha256', 'mime_type', 'size_bytes', 'source_locator', 'created_at', 'is_raw', 'source_id')},
                                                         'provenance': _provenance('artifacts', artifact_id, locator=art['source_locator'], epistemic='observed')}):
                break
            for version in db.execute("SELECT d.id,d.run_id,d.model,d.language,d.created_at,length(d.text) AS text_chars,r.tool,r.tool_version,r.provider,r.model AS run_model,r.model_version,r.status AS run_status FROM derived_text d LEFT JOIN processing_runs r ON r.id=d.run_id WHERE d.artifact_id=? AND d.kind='transcript' ORDER BY d.id", (artifact_id,)):
                node_id = f'transcript:{version["id"]}'
                run = {k: (version[k] if version[k] not in (None, '') else 'unknown') for k in ('tool', 'tool_version', 'provider', 'run_model', 'model_version', 'run_status')} if version['run_id'] else None
                add_node(node_id, {'kind': 'transcript', 'label': f'transcript #{version["id"]}',
                                   'attrs': {'model': version['model'], 'language': version['language'], 'created_at': version['created_at'],
                                             'text_chars': version['text_chars'], 'run': run, 'provenance_status': 'recorded_processing_run' if version['run_id'] else 'legacy_unknown'},
                                   'provenance': _provenance('derived_text', version['id'], run_id=version['run_id'], epistemic='model_output')})
                add_edge({'src': node_id, 'dst': f'artifact:{artifact_id}', 'relation': 'derived_from',
                          'provenance': _provenance('derived_text', version['id'], run_id=version['run_id'], epistemic='model_output')})
            for note in db.execute('SELECT * FROM annotations WHERE artifact_id=? ORDER BY id', (artifact_id,)):
                node_id = f'annotation:{note["id"]}'
                body, truncated, digest = _text(note['body'])
                attrs = {'kind': note['kind'], 'label': note['label'], 'start_ms': note['start_ms'], 'end_ms': note['end_ms'],
                         'created_at': note['created_at'], 'body_sha256': digest}
                if include_text:
                    attrs.update(body=body, body_truncated=truncated)
                add_node(node_id, {'kind': 'annotation', 'label': note['label'] or note['kind'], 'attrs': attrs,
                                   'provenance': _provenance('annotations', note['id'], epistemic='human_annotation')})
                add_edge({'src': node_id, 'dst': f'artifact:{artifact_id}', 'relation': 'annotates',
                          'provenance': _provenance('annotations', note['id'], epistemic='human_annotation')})
                if note['derived_text_id'] is not None:
                    add_edge({'src': node_id, 'dst': f'transcript:{note["derived_text_id"]}', 'relation': 'annotates',
                              'provenance': _provenance('annotations', note['id'], epistemic='human_annotation')})
            for anchor in db.execute('SELECT id,derived_text_id,start_ms,end_ms,quote_sha256,selector_json,created_at FROM evidence_anchors WHERE artifact_id=? ORDER BY id', (artifact_id,)):
                node_id = f'citation:{anchor["id"]}'
                add_node(node_id, {'kind': 'citation', 'label': f'citation #{anchor["id"]}',
                                   'attrs': {'derived_text_id': anchor['derived_text_id'], 'start_ms': anchor['start_ms'], 'end_ms': anchor['end_ms'],
                                             'quote_sha256': anchor['quote_sha256'], 'selector': json.loads(anchor['selector_json']),
                                             'created_at': anchor['created_at'], 'quote_epistemic_status': 'model_output', 'audio_verification': 'not_performed'},
                                   'provenance': _provenance('evidence_anchors', anchor['id'], epistemic='human_annotation')})
                add_edge({'src': node_id, 'dst': f'transcript:{anchor["derived_text_id"]}', 'relation': 'quotes',
                          'provenance': _provenance('evidence_anchors', anchor['id'], epistemic='human_annotation')})
        promoted = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='relation_hypotheses'").fetchone():
            for row in db.execute("SELECT id,origin,link_id,decided_by,decided_at FROM relation_hypotheses WHERE status='promoted' AND link_id IS NOT NULL"):
                promoted[row['link_id']] = {'hypothesis_id': row['id'], 'origin': row['origin'], 'decided_by': row['decided_by'], 'decided_at': row['decided_at']}
        for link in db.execute('SELECT * FROM links ORDER BY id'):
            src = f'{NODE_PREFIX.get(link["left_kind"], link["left_kind"])}:{link["left_id"]}'
            dst = f'{NODE_PREFIX.get(link["right_kind"], link["right_kind"])}:{link["right_id"]}'
            if src not in nodes or dst not in nodes:
                continue
            add_edge({'src': src, 'dst': dst, 'relation': link['relation'],
                      'attrs': {'confidence': link['confidence'], 'rationale': link['rationale'], 'created_at': link['created_at'],
                                'promoted_from': promoted.get(link['id'])},
                      'provenance': _provenance('links', link['id'], epistemic='hypothesis')})
        return None
    read_api.run_read(reader)
    edges_kept = [edge for edge in edges if edge['src'] in nodes and edge['dst'] in nodes]
    dangling = len(edges) - len(edges_kept)
    counts = {'nodes': len(nodes), 'edges': len(edges_kept), 'dangling_edges_dropped': dangling}
    for status in EPISTEMIC:
        counts[f'nodes_{status}'] = sum(1 for n in nodes.values() if n['provenance']['epistemic_status'] == status)
    return {
        'schema': EXPORT_SCHEMA, 'generated_at': utc_now(),
        'scope': {'from': from_, 'to': to, 'window_semantics': 'lexicographic over stored ts_start text' if windowed else 'all events',
                  'text_included': include_text, 'source_bytes_included': False, 'media_bytes_included': False,
                  'stored_path_included': False, 'private_seed_included': False},
        'epistemic_statuses': list(EPISTEMIC), 'loom_mapping': LOOM_MAPPING,
        'loom_packet_compat': 'unknown; this is an AGEDS projection with a vocabulary hint, not a validated loom.graph_packet/1',
        'nodes': list(nodes.values()), 'edges': edges_kept,
        'claims': {'available': False, 'items': [], 'reason': 'AGEDS stores no claims in this revision; interpretations stay outside the evidence graph'},
        'counts': counts, 'truncation': truncation,
        'limits': {'max_nodes': max_nodes, 'max_edges': max_edges, 'text_chars': MAX_TEXT_CHARS},
    }


def _reference(value, position: int):
    """Accept ``{'kind','id'}`` or an address like ``event:12``; return (db_kind, id)."""
    if isinstance(value, str):
        match = _ADDRESS.match(value)
        if not match:
            raise GraphInputError(f'item {position}: reference must look like event:<id>, artifact:<id>, transcript:<id>, annotation:<id> or citation:<id>')
        prefix, raw = match.groups()
        if prefix == 'counterparty':
            return 'counterparty', raw
        if not raw.isdigit():
            raise GraphInputError(f'item {position}: row ids are decimal integers')
        return KIND_BY_PREFIX[prefix], int(raw)
    if isinstance(value, dict) and set(value) == {'kind', 'id'}:
        kind, row_id = value['kind'], value['id']
        if kind in KIND_BY_PREFIX:
            kind = KIND_BY_PREFIX[kind]
        if kind not in TABLE_BY_KIND and kind != 'counterparty':
            raise GraphInputError(f'item {position}: unknown reference kind')
        if kind != 'counterparty' and (type(row_id) is not int or row_id < 1):
            raise GraphInputError(f'item {position}: row id must be a positive integer')
        return kind, row_id
    raise GraphInputError(f'item {position}: reference must be an address string or {{kind,id}}')


def import_hypotheses(document, *, origin: str = 'loom', file_name: str | None = None, raw_bytes: bytes | None = None) -> dict:
    if not isinstance(document, dict) or document.get('schema') != HYPOTHESES_SCHEMA:
        raise GraphInputError(f'document schema must be {HYPOTHESES_SCHEMA}; loom.graph_packet_diff/1 is not accepted here (format_compat: unknown)')
    if not isinstance(origin, str) or not _IDENT.match(origin):
        raise GraphInputError('origin must match [A-Za-z0-9_.:-]{1,200}')
    items = document.get('items')
    if not isinstance(items, list) or len(items) > MAX_IMPORT_ITEMS:
        raise GraphInputError(f'items must be an array of up to {MAX_IMPORT_ITEMS} entries')
    run = document.get('run', {})
    if not isinstance(run, dict):
        raise GraphInputError('run must be a JSON object')
    payload_bytes = raw_bytes if raw_bytes is not None else json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(payload_bytes) > MAX_IMPORT_BYTES:
        raise GraphInputError('document exceeds the import byte limit')
    file_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    parsed = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise GraphInputError(f'item {position}: must be a JSON object')
        extra = set(item) - {'external_id', 'left', 'right', 'relation', 'confidence', 'rationale', 'provenance'}
        if extra:
            raise GraphInputError(f'item {position}: unknown fields ' + ', '.join(sorted(extra)))
        relation = item.get('relation')
        if not isinstance(relation, str) or not _IDENT.match(relation):
            raise GraphInputError(f'item {position}: relation must match [A-Za-z0-9_.:-]{{1,200}}')
        confidence = item.get('confidence')
        if confidence is not None and (type(confidence) not in (int, float) or isinstance(confidence, bool) or not 0 <= confidence <= 1):
            raise GraphInputError(f'item {position}: confidence must be a number from 0 to 1 or null')
        rationale = item.get('rationale')
        if rationale is not None and (not isinstance(rationale, str) or len(rationale) > MAX_RATIONALE_CHARS):
            raise GraphInputError(f'item {position}: rationale must be text up to {MAX_RATIONALE_CHARS} characters')
        external_id = item.get('external_id')
        if external_id is not None and (not isinstance(external_id, str) or len(external_id) > 200):
            raise GraphInputError(f'item {position}: external_id must be text up to 200 characters')
        provenance = item.get('provenance', {})
        if not isinstance(provenance, dict):
            raise GraphInputError(f'item {position}: provenance must be a JSON object')
        parsed.append((external_id, _reference(item.get('left'), position), _reference(item.get('right'), position),
                       relation, float(confidence) if confidence is not None else None, rationale, provenance))
    now = utc_now()
    counts = {'quarantined': 0, 'rejected_unresolved': 0, 'rejected_unsupported_kind': 0}
    ids = []
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        cur = db.execute('INSERT INTO hypothesis_imports(origin,schema,file_name,file_sha256,counts_json,created_at) VALUES(?,?,?,?,?,?)',
                         (origin, HYPOTHESES_SCHEMA, file_name, file_sha256, '{}', now))
        import_id = int(cur.lastrowid)
        for external_id, (lk, lid), (rk, rid), relation, confidence, rationale, provenance in parsed:
            status, note = 'quarantined', None
            if lk == 'counterparty' or rk == 'counterparty':
                status, note = 'rejected', 'unsupported_kind_counterparty: counterparties are hypotheses without rows; relate events or add an annotation'
                counts['rejected_unsupported_kind'] += 1
            else:
                for kind, row_id in ((lk, lid), (rk, rid)):
                    if not db.execute(f'SELECT 1 FROM {TABLE_BY_KIND[kind]} WHERE id=?', (row_id,)).fetchone():
                        status, note = 'rejected', f'unresolved_reference: {kind}:{row_id}'
                        break
                if status == 'rejected':
                    counts['rejected_unresolved'] += 1
                else:
                    counts['quarantined'] += 1
            stored_provenance = {'origin': origin, 'import_id': import_id, 'file_sha256': file_sha256, 'run': run,
                                 'client_provenance': provenance, 'refs': {'left': f'{lk}:{lid}', 'right': f'{rk}:{rid}'}}
            cur = db.execute('''INSERT INTO relation_hypotheses(import_id,origin,external_id,left_kind,left_id,right_kind,right_id,relation,confidence,
                                rationale,epistemic_status,provenance_json,status,decided_at,decided_by,decision_note,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,'hypothesis',?,?,?,?,?,?)''',
                             (import_id, origin, external_id, lk, 0 if lk == 'counterparty' else lid, rk, 0 if rk == 'counterparty' else rid,
                              relation, confidence, rationale, json.dumps(stored_provenance, ensure_ascii=False, sort_keys=True),
                              status, now if status == 'rejected' else None, 'import_validation' if status == 'rejected' else None, note, now))
            ids.append(int(cur.lastrowid))
        db.execute('UPDATE hypothesis_imports SET counts_json=? WHERE id=?', (json.dumps(counts), import_id))
        db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,'hypothesis_import',?,?)",
                   (f'client:{origin}', 'hypotheses_import', import_id, json.dumps({'file_sha256': file_sha256, 'counts': counts, 'file_name': file_name}, ensure_ascii=False)))
    return {'schema': HYPOTHESES_SCHEMA, 'import_id': import_id, 'origin': origin, 'file_sha256': file_sha256, 'counts': counts,
            'hypothesis_ids': ids, 'links_written': 0, 'note': 'all items are quarantined or rejected; nothing reached links'}


def _hypothesis_payload(row) -> dict:
    result = dict(row)
    result['provenance'] = json.loads(result.pop('provenance_json'))
    return result


def list_hypotheses(*, status: str | None = None, limit: int = 200) -> list[dict]:
    if status is not None and status not in ('quarantined', 'promoted', 'rejected'):
        raise GraphInputError('status must be quarantined, promoted or rejected')
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise GraphInputError('limit must be an integer from 1 to 10000')
    where, params = ('', ()) if status is None else (' WHERE status=?', (status,))
    with session() as db:
        return [_hypothesis_payload(row) for row in db.execute(f'SELECT * FROM relation_hypotheses{where} ORDER BY id LIMIT ?', (*params, limit))]


def decide_hypothesis(hypothesis_id: int, decision: str, *, decided_by: str, note: str | None = None) -> dict:
    """Promote writes exactly one ``links`` row; reject keeps the quarantine row."""
    if type(hypothesis_id) is not int or hypothesis_id < 1:
        raise GraphInputError('hypothesis id must be a positive integer')
    if decision not in ('promoted', 'rejected'):
        raise GraphInputError("decision must be 'promoted' or 'rejected'")
    if not isinstance(decided_by, str) or not decided_by.strip() or len(decided_by) > 200:
        raise GraphInputError('decided_by must be nonblank text up to 200 characters')
    if note is not None and (not isinstance(note, str) or len(note) > 2000):
        raise GraphInputError('note must be text up to 2000 characters')
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM relation_hypotheses WHERE id=?', (hypothesis_id,)).fetchone()
        if row is None:
            raise HypothesisNotFound('hypothesis not found')
        if row['status'] != 'quarantined':
            raise GraphInputError(f'hypothesis already {row["status"]}')
        link_id = None
        if decision == 'promoted':
            for kind, row_id in ((row['left_kind'], row['left_id']), (row['right_kind'], row['right_id'])):
                if kind not in TABLE_BY_KIND or not db.execute(f'SELECT 1 FROM {TABLE_BY_KIND[kind]} WHERE id=?', (row_id,)).fetchone():
                    raise GraphInputError(f'referenced row no longer exists: {kind}:{row_id}')
            cur = db.execute('INSERT INTO links(left_kind,left_id,right_kind,right_id,relation,confidence,rationale) VALUES(?,?,?,?,?,?,?)',
                             (row['left_kind'], row['left_id'], row['right_kind'], row['right_id'], row['relation'], row['confidence'], row['rationale']))
            link_id = int(cur.lastrowid)
            db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,'link',?,?)",
                       (f'human:{decided_by}', 'link_from_hypothesis', link_id,
                        json.dumps({'hypothesis_id': hypothesis_id, 'origin': row['origin'], 'import_id': row['import_id'], 'note': note}, ensure_ascii=False)))
        db.execute('UPDATE relation_hypotheses SET status=?, decided_at=?, decided_by=?, decision_note=?, link_id=? WHERE id=?',
                   (decision, utc_now(), decided_by, note, link_id, hypothesis_id))
        db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES(?,?,'relation_hypothesis',?,?)",
                   (f'human:{decided_by}', f'hypothesis_{decision}', hypothesis_id, json.dumps({'link_id': link_id, 'note': note, 'origin': row['origin']}, ensure_ascii=False)))
        return _hypothesis_payload(db.execute('SELECT * FROM relation_hypotheses WHERE id=?', (hypothesis_id,)).fetchone())
