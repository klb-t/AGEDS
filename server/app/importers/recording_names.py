"""Recording metadata from file names, without reading or ingesting audio.

Patterns live in ``data/recording_name_patterns.v1.json`` (versioned, named
groups ``number``, ``name``, ``direction``, ``date``, ``time``). The shipped
patterns are labelled examples; they are not claimed to match any app version.

Each observed name yields a ``recording_name_observations`` row. When a date
and time are recognised, a ``recording`` event carries one ``filename_derived``
time candidate with an **unknown** zone. Number, name and direction are stored
as correlation hypotheses on the observation (and echoed in event metadata),
never in the event's factual columns.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..db import session
from ..evidence import IntegrityError, ensure_source
from ..scanner import _AUDIO
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, add_event_with_times, load_data, local_candidate,
                          record_import_run, walk_files)
from .vcf import normalize_phone

PARSER_NAME = 'recording_name'
PARSER_VERSION = '1'


def load_patterns(path: Path | None = None) -> dict:
    if path is None:
        data = load_data('recording_name_patterns.v1.json')
    else:
        with Path(path).open('r', encoding='utf-8') as handle:
            data = json.load(handle)
    compiled = []
    for pattern in data['patterns']:
        regex = re.compile(pattern['regex'], re.IGNORECASE)
        missing = {'date', 'time'} - set(regex.groupindex)
        if missing:
            raise ValueError(f"pattern {pattern['id']} lacks named groups: {sorted(missing)}")
        compiled.append({**pattern, 'compiled': regex})
    return {'schema': data.get('schema', 'private'), 'version': str(data.get('version', 'unknown')),
            'patterns': compiled}


def _apply(pattern: dict, file_name: str) -> dict | None:
    regex = pattern['compiled']
    match = regex.search(file_name) if pattern.get('search') else regex.match(file_name)
    if not match:
        return None
    groups = {k: v for k, v in match.groupdict().items() if v is not None}
    result = {'pattern_id': pattern['id'], 'groups': groups, 'span': [match.start(), match.end()],
              'specific': not pattern.get('search')}
    try:
        naive = datetime.strptime(groups['date'] + ' ' + groups['time'],
                                  pattern['date_format'] + ' ' + pattern['time_format'])
        result['local'] = naive.isoformat()
        result['precision'] = 'second' if any(t in pattern['time_format'] for t in ('%S',)) else 'minute'
    except ValueError:
        result['time_error'] = 'date_or_time_not_a_valid_calendar_value'
    hypotheses = {}
    if groups.get('number'):
        hypotheses['number'] = {'raw': groups['number'], 'normalized_candidate': normalize_phone(groups['number']),
                                'status': 'correlation_hypothesis_from_file_name'}
    if groups.get('name'):
        hypotheses['name'] = {'raw': groups['name'], 'status': 'correlation_hypothesis_from_file_name'}
    if groups.get('direction'):
        mapped = (pattern.get('direction_map') or {}).get(groups['direction'].lower())
        hypotheses['direction'] = {'raw': groups['direction'], 'label': mapped,
                                   'status': 'correlation_hypothesis_from_file_name'}
    result['hypotheses'] = hypotheses
    return result


def parse_name(file_name: str, patterns: dict) -> dict:
    """Apply every pattern; several differing specific matches are ``ambiguous``."""
    results = [r for r in (_apply(p, file_name) for p in patterns['patterns']) if r is not None]
    specific = [r for r in results if r['specific']]
    chosen = specific or results
    if not chosen:
        return {'status': 'unmatched', 'primary': None, 'alternatives': []}
    primary = chosen[0]
    distinct = {json.dumps([r['groups'], r.get('local')], sort_keys=True) for r in chosen}
    status = 'ambiguous' if len(distinct) > 1 else 'matched'
    return {'status': status, 'primary': primary, 'alternatives': [r for r in results if r is not primary]}


def observe(entries: list[dict], *, source_id: int, patterns: dict, issues: Issues) -> dict:
    """Record observations for ``{'locator', 'file_name', 'artifact_id'?}`` entries."""
    pattern_set = patterns['schema']
    counts = {'observed': 0, 'matched': 0, 'ambiguous': 0, 'unmatched': 0, 'events': 0}
    for entry in entries:
        parsed = parse_name(entry['file_name'], patterns)
        primary = parsed['primary']
        event_id = None
        problems = []
        if primary is not None and primary.get('time_error'):
            problems.append(primary['time_error'])
        if primary is not None and primary.get('local'):
            candidate = local_candidate(
                'filename_derived', primary['groups']['date'] + ' ' + primary['groups']['time'],
                f"{PARSER_NAME}:{pattern_set}/v{patterns['version']}:{primary['pattern_id']}",
                datetime.fromisoformat(primary['local']), primary['precision'],
                {'zone_status': 'file_name_has_no_zone', 'pattern_examples_only': True})
            metadata = {
                'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                'locator': {'kind': 'file_name', 'locator': entry['locator'], 'file_name': entry['file_name'],
                            'span': primary['span']},
                'pattern': {'set': pattern_set, 'version': patterns['version'], 'id': primary['pattern_id'],
                            'status': parsed['status']},
                'hypotheses': primary['hypotheses'],
                'semantics': 'a file whose name suggests a recording time; number/name/direction are hypotheses',
                'audio_read': False,
            }
            event_id = add_event_with_times(
                [candidate], source_id=source_id, artifact_id=entry.get('artifact_id'), event_type='recording',
                subject=entry['file_name'],
                external_id=f"{PARSER_NAME}:{pattern_set}/v{patterns['version']}:{entry['locator']}",
                confidence=None, metadata=metadata)
            counts['events'] += 1
        row = (source_id, entry.get('artifact_id'), event_id, entry['locator'], entry['file_name'], pattern_set,
               patterns['version'], parsed['status'], primary['pattern_id'] if primary else None,
               json.dumps(primary['groups'] if primary else {}, ensure_ascii=False, sort_keys=True),
               json.dumps(primary['hypotheses'] if primary else {}, ensure_ascii=False, sort_keys=True),
               json.dumps([{k: v for k, v in alt.items() if k != 'specific'} for alt in parsed['alternatives']],
                          ensure_ascii=False, sort_keys=True),
               json.dumps({'parser': PARSER_NAME, 'parser_version': PARSER_VERSION, 'issues': problems,
                           'local_time': primary.get('local') if primary else None,
                           'zone': 'unknown', 'patterns_are_examples': True}, ensure_ascii=False, sort_keys=True))
        _store(row)
        counts['observed'] += 1
        counts[parsed['status']] += 1
        for problem in problems:
            issues.add(problem, locator=entry['locator'])
        if parsed['status'] == 'ambiguous':
            issues.add('several_patterns_disagree', locator=entry['locator'])
    return counts


_COLUMNS = ('source_id', 'artifact_id', 'event_id', 'locator', 'file_name', 'pattern_set', 'pattern_set_version',
            'status', 'pattern_id', 'groups_json', 'hypotheses_json', 'alternatives_json', 'metadata_json')


def _store(row: tuple) -> int:
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM recording_name_observations WHERE source_id=? AND locator=? AND '
                              'pattern_set=? AND pattern_set_version=?', (row[0], row[3], row[5], row[6])).fetchone()
        if existing is not None:
            if tuple(existing[c] for c in _COLUMNS) != row:
                raise IntegrityError('recording name observation conflicts with an earlier parse under the same pattern version')
            return int(existing['id'])
        cur = db.execute(f'INSERT INTO recording_name_observations({",".join(_COLUMNS)}) '
                         f'VALUES ({",".join("?" * len(_COLUMNS))})', row)
        return int(cur.lastrowid)


def observe_paths(paths: list[Path], *, label: str | None = None, patterns_path: Path | None = None,
                  extensions: tuple[str, ...] | None = None, limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """Observe names of files (or audio files under directories); no bytes are read."""
    patterns = load_patterns(patterns_path)
    issues = Issues(limits.max_issues)
    allowed = tuple(e.lower() for e in (extensions or tuple(sorted(_AUDIO))))
    entries = []
    for path in paths:
        if path.is_symlink():
            issues.add('symlink_skipped', path=str(path))
        elif path.is_dir():
            entries.extend({'locator': str(p), 'file_name': p.name} for p in walk_files(path, limits, issues)
                           if p.suffix.lower() in allowed)
        elif path.is_file():
            entries.append({'locator': str(path), 'file_name': path.name})
        else:
            issues.add('path_not_found', path=str(path))
    complete = len(entries) <= limits.max_records and not issues.counts.get('walk_entry_limit')
    entries = entries[:limits.max_records]
    source_id = ensure_source('recording_names', label or 'Recording file names', 'recording-names:paths')
    counts = observe(entries, source_id=source_id, patterns=patterns, issues=issues)
    options = {'pattern_set': f"{patterns['schema']}/v{patterns['version']}", 'extensions': list(allowed),
               'audio_read': False}
    record_import_run(artifact_id=None, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts=counts, coverage={'complete': complete}, issues=issues)
    return {'source_id': source_id, 'parser_version': PARSER_VERSION, **counts, 'options': options,
            'coverage': {'complete': complete}, 'issues': issues.as_dict()}


def observe_artifacts(artifact_ids: list[int], *, patterns_path: Path | None = None,
                      limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """Observe names of already acquired artifacts (catalog entries) by their recorded name."""
    patterns = load_patterns(patterns_path)
    issues = Issues(limits.max_issues)
    counts_total: dict[str, int] = {}
    with session() as db:
        rows = [dict(r) for r in db.execute(
            f'SELECT id,source_id,original_name,source_locator FROM artifacts WHERE id IN ({",".join("?" * len(artifact_ids))})',
            tuple(artifact_ids))] if artifact_ids else []
    found = {row['id'] for row in rows}
    for missing in sorted(set(artifact_ids) - found):
        issues.add('artifact_not_found', artifact_id=missing)
    for row in sorted(rows, key=lambda r: r['id']):
        entry = {'locator': row['source_locator'] or f"artifact:{row['id']}", 'file_name': row['original_name'],
                 'artifact_id': row['id']}
        counts = observe([entry], source_id=row['source_id'], patterns=patterns, issues=issues)
        for key, value in counts.items():
            counts_total[key] = counts_total.get(key, 0) + value
    options = {'pattern_set': f"{patterns['schema']}/v{patterns['version']}", 'audio_read': False}
    record_import_run(artifact_id=None, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts=counts_total, coverage={'complete': not issues.counts.get('artifact_not_found')}, issues=issues)
    return {'parser_version': PARSER_VERSION, **counts_total, 'options': options, 'issues': issues.as_dict()}


def observe_names(names: list[str], *, label: str | None = None, locator_prefix: str = 'names',
                  patterns_path: Path | None = None, limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """Observe bare file names (e.g. a catalog listing sent over HTTP); nothing is read."""
    patterns = load_patterns(patterns_path)
    issues = Issues(limits.max_issues)
    entries = [{'locator': f'{locator_prefix}:{name}', 'file_name': name} for name in names[:limits.max_records]]
    source_id = ensure_source('recording_names', label or 'Recording file names', f'recording-names:{locator_prefix}')
    counts = observe(entries, source_id=source_id, patterns=patterns, issues=issues)
    options = {'pattern_set': f"{patterns['schema']}/v{patterns['version']}", 'audio_read': False}
    coverage = {'complete': len(names) <= limits.max_records}
    record_import_run(artifact_id=None, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts=counts, coverage=coverage, issues=issues)
    return {'source_id': source_id, 'parser_version': PARSER_VERSION, **counts, 'options': options,
            'coverage': coverage, 'issues': issues.as_dict()}
