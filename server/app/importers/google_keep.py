"""Google Keep (Google Takeout ``Keep/*.json``) → ``note`` events — traces area.

One JSON file per note: ``title``, ``textContent`` or ``listContent``
(``text``/``isChecked``), ``labels``, ``color``, ``isPinned``/``isArchived``/
``isTrashed``, ``annotations`` (web links), ``attachments`` (``filePath`` next to
the JSON), ``sharees`` and the two times ``createdTimestampUsec`` (``created``)
and ``userEditedTimestampUsec`` (``updated``) in **epoch microseconds**. A value
that lands before Keep existed (2013) in that unit — e.g. milliseconds or
seconds written into a ``…Usec`` field — is kept and flagged
``time_before_service_start``; the unit is never guessed.

Trashed and archived notes are imported with their flags (nothing is hidden or
dropped). Sharee e-mail addresses are not copied (count and roles only); the
note body is the user's own text. The per-note HTML files are not parsed (the
JSON carries the same fields): they are counted as ``html_variant_not_parsed``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ..db import session
from ..traces import messaging
from ..traces.common import bounded, bounded_body, clean_text, epoch_time, read_json, signal, write_event
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, add_media_reference, legacy_instant,
                          record_import_run, stored_path, walk_files)

PARSER_NAME = 'google_keep'
PARSER_VERSION = '1'
NOTE_KEYS = ('textContent', 'listContent', 'userEditedTimestampUsec', 'createdTimestampUsec')
SERVICE_START_YEAR = 2013


def is_note(data) -> bool:
    return isinstance(data, dict) and any(key in data for key in NOTE_KEYS)


def observe_note(artifact_id: int, *, resolver=None, limits: CommLimits = DEFAULT_LIMITS,
                 issues: Issues | None = None) -> dict:
    issues = issues or Issues(limits.max_issues)
    with session() as db:
        row = db.execute('SELECT id, source_id, original_name FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    if row is None:
        raise ValueError(f'artifact {artifact_id} does not exist')
    data = read_json(stored_path(artifact_id), limits, issues)
    if not is_note(data):
        raise ValueError('JSON object without Keep note keys (textContent/listContent/…TimestampUsec)')
    problems: list[str] = []
    candidates = []
    for field, kind in (('createdTimestampUsec', 'created'), ('userEditedTimestampUsec', 'updated')):
        if field not in data:
            continue
        candidate, problem = epoch_time(kind, data[field], f'{PARSER_NAME}@{PARSER_VERSION}:{field}', unit='us',
                                        metadata={'field': field})
        if problem:
            problems.append(f'{problem}:{field}')
            continue
        if int(candidate.utc[:4]) < SERVICE_START_YEAR:
            problems.append(f'time_before_service_start:{field}')
        candidates.append(candidate)
    items = data.get('listContent') if isinstance(data.get('listContent'), list) else None
    if items is not None:
        lines = []
        for item in items[:limits.max_raw_items]:
            if isinstance(item, dict):
                lines.append(('[x] ' if item.get('isChecked') else '[ ] ') + str(clean_text(item.get('text') or '')))
        text = '\n'.join(lines)
    else:
        text = data.get('textContent') if isinstance(data.get('textContent'), str) else None
    body, body_info = bounded_body(text, limits)
    labels = [clean_text(l.get('name')) for l in data.get('labels') or [] if isinstance(l, dict)][:limits.max_raw_items]
    sharees = [s for s in data.get('sharees') or [] if isinstance(s, dict)]
    annotations, _ = bounded([{k: a.get(k) for k in ('source', 'title', 'url')} for a in data.get('annotations') or []
                              if isinstance(a, dict)], limits)
    raw = {k: v for k, v in data.items() if k not in ('sharees', 'textContent', 'textContentHtml', 'listContent')}
    raw_copy, truncated = bounded(raw, limits)
    metadata = {'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                'locator': {'artifact_id': artifact_id},
                'note': {'labels': labels, 'color': data.get('color'), 'pinned': data.get('isPinned'),
                         'archived': data.get('isArchived'), 'trashed': data.get('isTrashed'),
                         'list_items': len(items) if items is not None else None,
                         'checked_items': sum(1 for i in items or [] if isinstance(i, dict) and i.get('isChecked')),
                         'annotations': annotations,
                         'sharees': {'count': len(sharees), 'owner_listed': any(s.get('isOwner') for s in sharees),
                                     'types': sorted({str(s.get('type')) for s in sharees})},
                         'html_content_present': 'textContentHtml' in data},
                'body': body_info, 'raw': raw_copy, 'raw_truncated_values': truncated,
                'signal': signal('declared', 'raw_sample', 'google_keep', 'account'),
                'semantics': 'note as exported; edit time is the last user edit recorded by Keep',
                'issues': problems}
    created = next((c for c in candidates if c.kind == 'created'), None)
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        event_id = write_event(db, candidates, source_id=row['source_id'], artifact_id=artifact_id, event_type='note',
                               ts_start=legacy_instant(created), contact_label='google_keep',
                               subject=clean_text(data.get('title')) or None, body=body,
                               external_id=f'{PARSER_NAME}@{PARSER_VERSION}', confidence=None, metadata=metadata)
    references = 0
    for position, attachment in enumerate((data.get('attachments') or [])[:limits.max_raw_items]):
        if not isinstance(attachment, dict):
            continue
        ref = messaging.MediaRef('attachment', attachment.get('filePath'), name=attachment.get('filePath'),
                                 metadata={'mimetype': attachment.get('mimetype')})
        ctx = SimpleNamespace(namespace='google_keep', parser=PARSER_NAME, parser_version=PARSER_VERSION, limits=limits,
                              artifact_id=artifact_id)
        status, child, extra = (resolver or messaging.no_files_resolver())(ref, ctx)
        add_media_reference(event_id=event_id, artifact_id=child, reference_kind='attachment',
                            reference_key=str(clean_text(ref.uri or f'attachment/{position}')), reference_name=ref.name,
                            status=status, parser=PARSER_NAME, parser_version=PARSER_VERSION,
                            locator={'artifact_id': artifact_id, 'json_pointer': f'/attachments/{position}'},
                            metadata={**ref.metadata, **extra})
        references += 1
    for problem in problems:
        issues.add(problem, artifact_id=artifact_id)
    return {'artifact_id': artifact_id, 'event_id': event_id, 'status': 'note_observed', 'time_candidates': len(candidates),
            'attachments': references, 'trashed': bool(data.get('isTrashed')), 'problems': problems}


def import_keep(path: Path, *, source_locator: str | None = None, original_name: str | None = None,
                collection_id: int | None = None, label: str | None = None, limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """One Keep note JSON or the Takeout ``Keep`` folder."""
    path = Path(path)
    issues = Issues(limits.max_issues)
    label = label or 'Google Takeout Keep'
    counts = {'notes': 0, 'json_not_note': 0, 'html_variant_not_parsed': 0, 'trashed': 0, 'attachments': 0}
    results = []

    def locator_of(candidate: Path) -> str:
        if not path.is_dir():
            return source_locator if source_locator is not None else str(candidate)
        relative = candidate.relative_to(path).as_posix()
        return f'{source_locator}/{relative}' if source_locator else str(candidate)
    files = walk_files(path, limits, issues) if path.is_dir() else [path]
    complete = 'walk_entry_limit' not in issues.counts
    resolver = messaging.directory_resolver([path], collection_id=collection_id, label=label, locator_of=locator_of) \
        if path.is_dir() else None
    for file_path in files:
        suffix = file_path.suffix.lower()
        if suffix == '.html':
            counts['html_variant_not_parsed'] += 1
            continue
        if suffix != '.json':
            continue
        if counts['notes'] >= limits.max_records:
            issues.add('record_limit', limit=limits.max_records)
            complete = False
            break
        try:
            probe = read_json(file_path, limits, Issues(1))
        except ValueError:
            if not path.is_dir():
                raise
            issues.add('json_unreadable', name=file_path.name)
            continue
        if not is_note(probe):
            if not path.is_dir():
                raise ValueError('JSON object without Keep note keys (textContent/listContent/…TimestampUsec)')
            counts['json_not_note'] += 1
            continue
        _, artifact_id = acquire(file_path, source_kind='google_takeout', source_label=label, locator=locator_of(file_path),
                                 importer=PARSER_NAME, parser_version=PARSER_VERSION,
                                 original_name=original_name if not path.is_dir() else None,
                                 collection_id=collection_id, mime_type='application/json')
        result = observe_note(artifact_id, resolver=resolver, limits=limits, issues=issues)
        results.append(result)
        counts['notes'] += 1
        counts['trashed'] += int(result['trashed'])
        counts['attachments'] += result['attachments']
    coverage = {'complete': complete}
    record_import_run(artifact_id=results[0]['artifact_id'] if len(results) == 1 else None, importer=PARSER_NAME,
                      parser_version=PARSER_VERSION, options={}, counts=counts, coverage=coverage, issues=issues)
    return {'parser_version': PARSER_VERSION, **counts, 'results': results[:200], 'coverage': coverage,
            'issues': issues.as_dict()}
