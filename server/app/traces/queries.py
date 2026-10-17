"""Read-only views of the traces area for the API and the "Ślady" page.

Rows are observations of files and accounts. Third-party names (photo
``people``) are left out unless a caller asks for them explicitly.
"""
from __future__ import annotations

import json

from ..db import session

EVENT_TYPES = ('photo_metadata', 'image_capture', 'chat_message', 'social_post', 'account_activity',
               'document_metadata', 'note', 'browser_session')
IMPORTERS = ('google_photos_sidecar', 'google_photos_media', 'facebook_messages', 'instagram_messages',
             'telegram_desktop', 'google_chat', 'twitter_x_archive', 'linkedin_messages', 'account_activity',
             'document_metadata', 'google_keep', 'browser_profile')
TABLES = ('trace_photo_sidecars', 'trace_photo_matches', 'trace_geotags', 'trace_document_metadata',
          'trace_browser_visits')


def _exists(db, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def summary() -> dict:
    with session() as db:
        tables = {t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in TABLES if _exists(db, t)}
        events = {r[0]: r[1] for r in db.execute(
            f'SELECT event_type, COUNT(*) FROM events WHERE event_type IN ({",".join("?" * len(EVENT_TYPES))}) '
            'GROUP BY event_type ORDER BY event_type', EVENT_TYPES)}
        platforms = {r[0] or 'unknown': r[1] for r in db.execute(
            "SELECT json_extract(metadata_json,'$.chat.platform'), COUNT(*) FROM events WHERE event_type='chat_message' "
            "AND json_extract(metadata_json,'$.chat.platform') IS NOT NULL GROUP BY 1 ORDER BY 1")}
        geotags = {f'{r[0]}:{r[1]}': r[2] for r in db.execute(
            'SELECT source_kind, status, COUNT(*) FROM trace_geotags GROUP BY 1, 2 ORDER BY 1, 2')}
        matches = {r[0]: r[1] for r in db.execute('SELECT status, COUNT(*) FROM trace_photo_matches GROUP BY 1 ORDER BY 1')}
        documents = {f'{r[0]}:{r[1]}': r[2] for r in db.execute(
            'SELECT format, status, COUNT(*) FROM trace_document_metadata GROUP BY 1, 2 ORDER BY 1, 2')}
        runs = []
        for row in db.execute("SELECT id, ts, details_json FROM audit_log WHERE action='import_run' "
                              "ORDER BY id DESC LIMIT 400"):
            details = json.loads(row['details_json'])
            if details.get('importer') in IMPORTERS:
                runs.append({'id': row['id'], 'created_at': row['ts'], 'importer': details['importer'],
                             'parser_version': details.get('parser_version'), 'options': details.get('options'),
                             'counts': details.get('counts'), 'coverage': details.get('coverage'),
                             'issue_counts': details.get('issue_counts')})
            if len(runs) >= 50:
                break
    return {'tables': tables, 'events': events, 'chat_platforms': platforms, 'geotags': geotags,
            'photo_matches': matches, 'documents': documents, 'recent_runs': runs,
            'semantics': 'observations of files and accounts (device/account signals), not of a person'}


def _page(sql: str, params: tuple, limit: int, after_id: int) -> tuple[list[dict], bool]:
    with session() as db:
        rows = [dict(r) for r in db.execute(sql + ' AND id>? ORDER BY id LIMIT ?', (*params, after_id, limit + 1))]
    return rows[:limit], len(rows) > limit


def _decode(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if key.endswith('_json'):
            out[key[:-5]] = json.loads(value) if value else None
        else:
            out[key] = value
    return out


def photo_sidecars(*, limit: int = 100, after_id: int = 0, include_people: bool = False) -> dict:
    rows, more = _page('SELECT * FROM trace_photo_sidecars WHERE 1=1', (), limit, after_id)
    items = []
    with session() as db:
        for row in rows:
            item = _decode(row)
            if not include_people:
                item['people'] = {'count': len(item['people'] or []), 'omitted': 'pass include_people=true'}
                item['description'] = None if not row['description'] else {'omitted': 'pass include_people=true'}
            match = db.execute('SELECT * FROM trace_photo_matches WHERE sidecar_id=? ORDER BY id DESC LIMIT 1',
                               (row['id'],)).fetchone()
            item['latest_match'] = _decode(dict(match)) if match else None
            items.append(item)
    return {'items': items, 'has_more': more,
            'semantics': 'Google Photos item metadata; sidecar-to-media links are hypotheses with rule ids'}


def geotag_rows(*, limit: int = 100, after_id: int = 0) -> dict:
    rows, more = _page('SELECT * FROM trace_geotags WHERE 1=1', (), limit, after_id)
    return {'items': [_decode(r) for r in rows], 'has_more': more,
            'semantics': 'coordinates only where the run opted in (enable_location); position claims of files/accounts'}


def documents(*, limit: int = 100, after_id: int = 0) -> dict:
    rows, more = _page('SELECT * FROM trace_document_metadata WHERE 1=1', (), limit, after_id)
    return {'items': [_decode(r) for r in rows], 'has_more': more,
            'semantics': 'authors and tools are claims written into the file, not identities'}


def browser_visits(*, limit: int = 100, after_id: int = 0) -> dict:
    rows, more = _page('SELECT * FROM trace_browser_visits WHERE 1=1', (), limit, after_id)
    return {'items': [_decode(r) for r in rows], 'has_more': more,
            'semantics': 'visit records of a local browser profile (opt-in); not proof of reading or attention'}
