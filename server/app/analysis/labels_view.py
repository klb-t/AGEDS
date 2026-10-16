"""One event's label: current path and provenance, corrections and stored label runs."""
from __future__ import annotations

import sqlite3

from . import categories as cat
from .source import event_item

SUMMARY_KEYS = ('event_id', 'event_type', 'artifact_id', 'source_id', 'thread_id', 'counterparty', 'chatbot',
                'direction', 't_start', 't_end', 'time_basis', 'untimed_reason', 'text_chars', 'tags', 'chat',
                'declared_duration_s', 'duration_basis')


def event_summary(item: dict) -> dict:
    out = {k: item.get(k) for k in SUMMARY_KEYS}
    f = item.get('fields') or {}
    out['subject'] = f.get('subject')
    body = f.get('body')
    out['body_preview'] = body[:280] if isinstance(body, str) else None
    out['links'] = {'artifact': f'/artifact/{item["artifact_id"]}' if item.get('artifact_id') else None,
                    'event': f'/api/analysis/events/{item["event_id"]}'}
    return out


def event_labels(db: sqlite3.Connection, event_id: int, config: cat.CategoryConfig | None,
                 *, stored_limit: int = 50) -> dict:
    item = event_item(db, event_id)
    annotations = cat.load_effective_annotations(db, [event_id])
    current = cat.label_items([item], config, annotations=annotations)[event_id] if config else None
    stored = [dict(r) for r in db.execute(
        '''SELECT l.label_run_id, l.category, l.weight, l.path, l.rules, l.annotation_id, l.model_run_id,
                  r.config_sha256, r.config_version, r.created_at
           FROM analysis_event_labels l JOIN analysis_label_runs r ON r.id=l.label_run_id
           WHERE l.event_id=? ORDER BY l.id DESC LIMIT ?''', (event_id, stored_limit))]
    return {'event': event_summary(item), 'current_label': current,
            'config': {'sha256': config.sha256, 'version': config.version} if config else None,
            'annotations': cat.annotation_history(db, event_id), 'stored_labels': stored}
