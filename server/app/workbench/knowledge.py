"""Computed summaries: what the stored records say about an artifact, and what they leave open.

Everything here is counted or looked up by code from existing tables. The
summary states which facts are recorded (with their basis), which fields are
not recorded or marked ``unknown``, and fixed limits of what the records can
claim. It never scores truth, authorship or reliability.

Also: quick jump by kind/id or hash prefix, and a plain-text citation with its
inert locator for copying.
"""
from __future__ import annotations

import re
import sqlite3

from ..db import session
from .common import WorkbenchInputError, WorkbenchNotFound, positive_id
from .folders import folders_containing
from .i18n import translate
from .layers import DERIVED_KIND_LAYERS, epistemic_of, layer_of

LIST_LIMIT = 50
JUMP_LIMIT = 20
MAX_JUMP_CHARS = 128
_UNKNOWN = 'unknown'


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def artifact_knowledge(artifact_id: int) -> dict:
    positive_id(artifact_id, 'artifact id')
    with session() as db:
        art = db.execute('''SELECT a.*, s.kind AS source_kind, s.label AS source_label FROM artifacts a
                            LEFT JOIN sources s ON s.id=a.source_id WHERE a.id=?''', (artifact_id,)).fetchone()
        if art is None:
            raise WorkbenchNotFound('artifact not found')
        obs = db.execute('''SELECT count(*) AS n, count(DISTINCT source_id) AS sources, count(DISTINCT source_locator) AS locators,
                              min(observed_at) AS first_observed, max(observed_at) AS last_observed,
                              sum(acquisition_kind='legacy_snapshot') AS legacy, sum(source_created_at IS NULL) AS no_created,
                              sum(source_modified_at IS NULL) AS no_modified
                            FROM source_observations WHERE artifact_id=?''', (artifact_id,)).fetchone()
        kinds = {r['acquisition_kind']: r['n'] for r in db.execute(
            'SELECT acquisition_kind, count(*) AS n FROM source_observations WHERE artifact_id=? GROUP BY acquisition_kind', (artifact_id,))}
        group = [dict(r) for r in db.execute(
            'SELECT id, original_name, source_locator FROM artifacts WHERE sha256=? AND id<>? ORDER BY id LIMIT ?',
            (art['sha256'], artifact_id, LIST_LIMIT + 1))] if art['sha256'] else []
        derived = []
        for r in db.execute('''SELECT kind, count(*) AS n, sum(run_id IS NULL) AS without_run,
                                 count(DISTINCT model) AS models, group_concat(DISTINCT language) AS languages
                               FROM derived_text WHERE artifact_id=? GROUP BY kind ORDER BY kind''', (artifact_id,)):
            layer = layer_of('derived_text', r['kind'])
            derived.append({'kind': r['kind'], 'layer': layer, 'epistemic_layer': epistemic_of(layer), 'versions': r['n'],
                            'without_recorded_run': r['without_run'], 'distinct_models': r['models'],
                            'languages': sorted(set((r['languages'] or '').split(','))) if r['languages'] else []})
        runs = {r['status']: r['n'] for r in db.execute(
            'SELECT status, count(*) AS n FROM processing_runs WHERE artifact_id=? GROUP BY status', (artifact_id,))}
        run_unknowns = db.execute('''SELECT sum(model='unknown') AS model, sum(model_version='unknown') AS model_version,
                                       sum(tool_version='unknown') AS tool_version, sum(provider='unknown') AS provider
                                     FROM processing_runs WHERE artifact_id=?''', (artifact_id,)).fetchone()
        jobs = {r['status']: r['n'] for r in db.execute(
            'SELECT status, count(*) AS n FROM jobs WHERE artifact_id=? GROUP BY status', (artifact_id,))}
        counts = {
            'citations': db.execute('SELECT count(*) FROM evidence_anchors WHERE artifact_id=?', (artifact_id,)).fetchone()[0],
            'annotations': db.execute('SELECT count(*) FROM annotations WHERE artifact_id=?', (artifact_id,)).fetchone()[0],
            'events': db.execute('SELECT count(*) FROM events WHERE artifact_id=?', (artifact_id,)).fetchone()[0],
            'events_untimed': db.execute('SELECT count(*) FROM events WHERE artifact_id=? AND ts_start IS NULL', (artifact_id,)).fetchone()[0],
        }
        time_candidates = None
        if _table_exists(db, 'event_times'):
            row = db.execute('''SELECT count(*) AS n, sum(t.tz_status='unknown') AS unknown_zone FROM event_times t
                                JOIN events e ON e.id=t.event_id WHERE e.artifact_id=?''', (artifact_id,)).fetchone()
            time_candidates = {'candidates': row['n'], 'unknown_zone': row['unknown_zone'] or 0}
        citations = [dict(r) for r in db.execute(
            '''SELECT id, derived_text_id, start_ms, end_ms, quote_sha256, substr(quote_text,1,200) AS quote_preview
               FROM evidence_anchors WHERE artifact_id=? ORDER BY id DESC LIMIT ?''', (artifact_id, LIST_LIMIT))]
        tags = [dict(r) for r in db.execute(
            'SELECT t.id, t.name FROM artifact_tags l JOIN tags t ON t.id=l.tag_id WHERE l.artifact_id=? ORDER BY t.name', (artifact_id,))]
        bookmarks = db.execute('''SELECT count(*) FROM workbench_bookmarks
                                  WHERE ref_kind IN ('artifact','transcript_segment') AND ref_id=?''', (artifact_id,)).fetchone()[0]
    recorded, unknown = [], []
    recorded.append({'key': 'content_identity', 'value': {'sha256': art['sha256'], 'size_bytes': art['size_bytes']},
                     'basis': 'store_file SHA-256 of captured bytes'})
    recorded.append({'key': 'acquisitions', 'value': {'observations': obs['n'], 'by_kind': kinds, 'distinct_sources': obs['sources'],
                                                       'distinct_locators': obs['locators'], 'first_observed_at': obs['first_observed'],
                                                       'last_observed_at': obs['last_observed']},
                     'basis': 'source_observations (append-only)'})
    if group:
        recorded.append({'key': 'same_content_elsewhere', 'value': {'artifacts': group[:LIST_LIMIT], 'truncated': len(group) > LIST_LIMIT},
                         'basis': 'artifacts with the same sha256 under another locator'})
    if derived:
        recorded.append({'key': 'derived_text', 'value': derived, 'basis': 'derived_text versions by kind'})
    if runs:
        recorded.append({'key': 'processing_runs', 'value': runs, 'basis': 'processing_runs by status'})
    for field in ('mime_type', 'source_created_at', 'source_modified_at', 'captured_at', 'source_locator'):
        if art[field] is None:
            unknown.append({'key': f'artifact.{field}', 'reason': 'not_recorded'})
    if obs['legacy']:
        unknown.append({'key': 'acquisition_history', 'reason': 'legacy_snapshot', 'count': obs['legacy']})
    if obs['n'] and obs['no_created'] == obs['n']:
        unknown.append({'key': 'source_created_at', 'reason': 'no_acquisition_recorded_it'})
    for item in derived:
        if item['without_recorded_run']:
            unknown.append({'key': f'derived_text.{item["kind"]}.run', 'reason': 'legacy_unknown', 'count': item['without_recorded_run']})
        if item['kind'] not in DERIVED_KIND_LAYERS:
            unknown.append({'key': f'derived_text.{item["kind"]}.layer', 'reason': 'unclassified_kind'})
    for field in ('model', 'model_version', 'tool_version', 'provider'):
        if run_unknowns[field]:
            unknown.append({'key': f'processing_runs.{field}', 'reason': 'recorded_as_unknown', 'count': run_unknowns[field]})
    if (art['mime_type'] or '').startswith(('audio/', 'video/')) and not any(d['kind'] == 'transcript' for d in derived):
        unknown.append({'key': 'transcript', 'reason': 'none_yet'})
    if counts['events_untimed']:
        unknown.append({'key': 'events.ts_start', 'reason': 'not_recorded', 'count': counts['events_untimed']})
    if time_candidates and time_candidates['unknown_zone']:
        unknown.append({'key': 'event_times.zone', 'reason': 'unknown_zone', 'count': time_candidates['unknown_zone']})
    if jobs.get('failed'):
        unknown.append({'key': 'jobs', 'reason': 'failed_attempts', 'count': jobs['failed']})
    return {
        'artifact': {'id': art['id'], 'original_name': art['original_name'], 'mime_type': art['mime_type'],
                     'sha256': art['sha256'], 'size_bytes': art['size_bytes'], 'source_kind': art['source_kind'],
                     'source_label': art['source_label'], 'source_locator': art['source_locator']},
        'recorded': recorded, 'not_recorded_or_unknown': unknown,
        'not_claimed': ['hash_not_authorship_truth_or_time', 'asr_language_probability_not_correctness',
                        'no_signature_or_custody', 'folder_tag_membership_not_evidence', 'word_timing_not_verified_alignment'],
        'counts': counts | {'tags': len(tags), 'bookmarks': bookmarks, 'jobs': jobs},
        'time_candidates': time_candidates, 'citations': citations, 'tags': tags,
        'folders': folders_containing('artifact', artifact_id),
        'computed_by': 'workbench.knowledge.artifact_knowledge', 'scores': None,
    }


_KIND_ALIASES = {'a': 'artifact', 'artifact': 'artifact', 'e': 'event', 'event': 'event', 'v': 'text_version',
                 'version': 'text_version', 'text_version': 'text_version', 'c': 'citation', 'citation': 'citation',
                 'n': 'annotation', 'annotation': 'annotation', 'f': 'folder', 'folder': 'folder',
                 'b': 'bookmark', 'bookmark': 'bookmark'}
_KIND_ID = re.compile(r'^([a-z_]+)\s*[:#/ ]\s*#?([1-9][0-9]{0,18})$')
_BARE_ID = re.compile(r'^#?([1-9][0-9]{0,18})$')
_HEX = re.compile(r'^[0-9a-fA-F]{6,64}$')


def _candidate(db: sqlite3.Connection, kind: str, ident: int) -> dict | None:
    if kind == 'artifact':
        r = db.execute('SELECT id, original_name, sha256 FROM artifacts WHERE id=?', (ident,)).fetchone()
        return r and {'kind': kind, 'id': ident, 'label': r['original_name'], 'detail': (r['sha256'] or '')[:12], 'url': f'/artifact/{ident}'}
    if kind == 'event':
        r = db.execute('SELECT id, artifact_id, event_type, ts_start FROM events WHERE id=?', (ident,)).fetchone()
        return r and {'kind': kind, 'id': ident, 'label': r['event_type'], 'detail': r['ts_start'] or '',
                      'url': f'/artifact/{r["artifact_id"]}' if r['artifact_id'] else None}
    if kind == 'text_version':
        r = db.execute('SELECT id, artifact_id, kind, model FROM derived_text WHERE id=?', (ident,)).fetchone()
        return r and {'kind': kind, 'id': ident, 'label': r['kind'], 'detail': r['model'] or _UNKNOWN, 'url': f'/artifact/{r["artifact_id"]}'}
    if kind == 'citation':
        r = db.execute('SELECT id, artifact_id, derived_text_id, quote_sha256 FROM evidence_anchors WHERE id=?', (ident,)).fetchone()
        return r and {'kind': kind, 'id': ident, 'label': f'derived_text #{r["derived_text_id"]}', 'detail': r['quote_sha256'][:12],
                      'url': f'/artifact/{r["artifact_id"]}'}
    if kind == 'annotation':
        r = db.execute('SELECT id, artifact_id, kind, label FROM annotations WHERE id=?', (ident,)).fetchone()
        return r and {'kind': kind, 'id': ident, 'label': r['label'] or r['kind'], 'detail': r['kind'],
                      'url': f'/artifact/{r["artifact_id"]}' if r['artifact_id'] else None}
    if kind == 'folder':
        r = db.execute('SELECT id, name FROM workbench_folders WHERE id=?', (ident,)).fetchone()
        return r and {'kind': kind, 'id': ident, 'label': r['name'], 'detail': '', 'url': f'/workbench/folders/{ident}'}
    if kind == 'bookmark':
        r = db.execute('SELECT id, label, start_raw FROM workbench_bookmarks WHERE id=?', (ident,)).fetchone()
        return r and {'kind': kind, 'id': ident, 'label': r['label'] or '', 'detail': r['start_raw'], 'url': f'/workbench/bookmarks#bookmark-{ident}'}
    return None


def quick_jump(q) -> dict:
    """Resolve 'a:12', 'e#7', '12' (every kind with that id) or a hex prefix of 6+ chars (artifact/quote SHA-256)."""
    if not isinstance(q, str) or len(q) > MAX_JUMP_CHARS:
        raise WorkbenchInputError(f'query must be text up to {MAX_JUMP_CHARS} characters')
    text = q.strip()
    found: list[dict] = []
    with session() as db:
        match = _KIND_ID.match(text.lower())
        if match and match.group(1) in _KIND_ALIASES:
            hit = _candidate(db, _KIND_ALIASES[match.group(1)], int(match.group(2)))
            found.extend([hit] if hit else [])
        elif _BARE_ID.match(text):
            ident = int(_BARE_ID.match(text).group(1))
            for kind in ('artifact', 'event', 'text_version', 'citation', 'annotation', 'folder', 'bookmark'):
                hit = _candidate(db, kind, ident)
                if hit:
                    found.append(hit)
        if _HEX.match(text):
            prefix = text.lower()
            for r in db.execute('SELECT id FROM artifacts WHERE sha256 >= ? AND sha256 < ? ORDER BY sha256, id LIMIT ?',
                                (prefix, prefix + 'g', JUMP_LIMIT + 1)):
                hit = _candidate(db, 'artifact', r['id'])
                if hit and hit not in found:
                    found.append(hit | {'match': 'sha256_prefix'})
            for r in db.execute('SELECT id FROM evidence_anchors WHERE quote_sha256 >= ? AND quote_sha256 < ? ORDER BY quote_sha256, id LIMIT ?',
                                (prefix, prefix + 'g', JUMP_LIMIT + 1)):
                hit = _candidate(db, 'citation', r['id'])
                if hit:
                    found.append(hit | {'match': 'quote_sha256_prefix'})
    return {'query': q, 'candidates': found[:JUMP_LIMIT], 'has_more': len(found) > JUMP_LIMIT,
            'note': 'numeric ids are per table; the same number can name different objects'}


def _clock(ms: int) -> str:
    seconds, millis = divmod(int(ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:d}:{minutes:02d}:{seconds:02d}.{millis:03d}'


def citation_text(anchor_id: int, lang: str = 'pl') -> dict:
    """A plain-text citation (quote verbatim) with version, range, hashes and an inert locator."""
    positive_id(anchor_id, 'citation id')
    with session() as db:
        row = db.execute('''SELECT c.*, a.original_name, a.sha256 AS artifact_sha256, d.kind AS version_kind, d.model, d.language,
                              d.run_id FROM evidence_anchors c JOIN artifacts a ON a.id=c.artifact_id
                              JOIN derived_text d ON d.id=c.derived_text_id WHERE c.id=?''', (anchor_id,)).fetchone()
    if row is None:
        raise WorkbenchNotFound('citation not found')
    locator = f'ageds:citation/{row["id"]}@derived_text/{row["derived_text_id"]}'
    params = {'quote': row['quote_text'], 'name': row['original_name'], 'version': row['derived_text_id'],
              'kind': row['version_kind'], 'model': row['model'] or _UNKNOWN, 'language': row['language'] or _UNKNOWN,
              'start': _clock(row['start_ms']), 'end': _clock(row['end_ms']), 'start_ms': row['start_ms'], 'end_ms': row['end_ms'],
              'locator': locator, 'quote_sha256': row['quote_sha256'], 'artifact_sha256': row['artifact_sha256'] or _UNKNOWN,
              'artifact': row['artifact_id']}
    lines = [translate(lang, 'citation.copy.quote', **params), translate(lang, 'citation.copy.source', **params),
             translate(lang, 'citation.copy.range', **params), translate(lang, 'citation.copy.locator', **params),
             translate(lang, 'citation.copy.caveat')]
    return {'id': row['id'], 'artifact_id': row['artifact_id'], 'derived_text_id': row['derived_text_id'], 'locator': locator,
            'text': '\n'.join(lines), 'quote_text': row['quote_text'], 'lang': lang,
            'epistemic_layer': epistemic_of(layer_of('derived_text', row['version_kind']))}
