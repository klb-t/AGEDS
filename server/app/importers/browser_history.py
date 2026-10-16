"""Browser/YouTube history as an activity signal — disabled unless opted in.

``enable_browser_history`` must be set explicitly (CLI
``--enable-browser-history``); the opt-in is stored on the run and on every
event. Chrome's Takeout ``BrowserHistory.json`` (``time_usec``, epoch
microseconds) becomes ``browser_visit`` events. YouTube/Chrome histories in
My Activity format are delegated to the My Activity importer with the same
opt-in. A visit record is a signal, not proof of reading or attention.
"""
from __future__ import annotations

import json
from pathlib import Path

from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, add_event_with_times, bound_json,
                          epoch_candidate, legacy_instant, record_import_run, stored_path)
from .takeout_activity import import_my_activity

PARSER_NAME = 'chrome_browser_history'
PARSER_VERSION = '1'


class BrowserHistoryOptInRequired(PermissionError):
    pass


def import_browser_history(path: Path, *, enable_browser_history: bool = False, source_locator: str | None = None,
                           original_name: str | None = None, collection_id: int | None = None,
                           label: str | None = None, limits: CommLimits = DEFAULT_LIMITS) -> dict:
    if not enable_browser_history:
        raise BrowserHistoryOptInRequired('browser history import is disabled; pass enable_browser_history=True '
                                          '(CLI --enable-browser-history) to opt in')
    with path.open('rb') as handle:
        head = handle.read(4096).lstrip()
    if head[:1] == b'[' or head[:1] == b'<':
        # My Activity layout (e.g. YouTube watch-history.json / .html).
        result = import_my_activity(path, enable_browser_history=True, source_locator=source_locator,
                                    original_name=original_name, collection_id=collection_id, label=label,
                                    limits=limits)
        return {**result, 'delegated_to': 'takeout_my_activity'}
    opt_in = {'enable_browser_history': True, 'processing': 'local_only'}
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(
        path, source_kind='google_takeout', source_label=label or 'Takeout browser history', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name,
        collection_id=collection_id, options=opt_in, mime_type='application/json')
    issues = Issues(limits.max_issues)
    snapshot = stored_path(artifact_id)
    counts = {'visits': 0}
    if snapshot.stat().st_size > limits.max_json_bytes:
        issues.add('document_size_limit', limit=limits.max_json_bytes)
        coverage = {'complete': False}
        record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                          options=opt_in, counts=counts, coverage=coverage, issues=issues)
        return {'artifact_id': artifact_id, **counts, 'coverage': coverage, 'issues': issues.as_dict()}
    data = json.loads(snapshot.read_bytes().decode('utf-8-sig'))
    visits = data.get('Browser History') if isinstance(data, dict) else None
    if not isinstance(visits, list):
        raise ValueError('expected a Chrome Takeout object with a "Browser History" list')
    complete = True
    for index, visit in enumerate(visits):
        if index >= limits.max_records:
            issues.add('record_limit', limit=limits.max_records)
            complete = False
            break
        if not isinstance(visit, dict):
            issues.add('record_not_an_object', index=index)
            continue
        problems = []
        candidate = None
        raw_time = visit.get('time_usec')
        if raw_time is None:
            problems.append('time_missing')
        else:
            try:
                candidate = epoch_candidate('created', str(raw_time), f'{PARSER_NAME}:time_usec', unit='us',
                                            metadata={'semantics': 'visit_record_time'})
            except (ValueError, OverflowError):
                problems.append('time_unparsed')
        raw, truncated = bound_json(visit, limits)
        add_event_with_times(
            [candidate] if candidate else [], source_id=source_id, artifact_id=artifact_id, event_type='browser_visit',
            ts_start=legacy_instant(candidate), subject=visit.get('title'), body=visit.get('url'),
            external_id=f'{PARSER_NAME}:{index}', confidence=None,
            metadata={'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                      'locator': {'artifact_id': artifact_id, 'index': index}, 'raw_record': raw,
                      'raw_truncated_values': truncated, 'opt_in': opt_in,
                      'semantics': 'browser visit record; not proof of reading or attention', 'issues': problems})
        for problem in problems:
            issues.add(problem, index=index)
        counts['visits'] += 1
    coverage = {'complete': complete}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=opt_in,
                      counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser_version': PARSER_VERSION, **counts,
            'options': opt_in, 'coverage': coverage, 'issues': issues.as_dict()}
