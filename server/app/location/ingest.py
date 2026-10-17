"""Shared import mechanics for every location format (Google, GPX, KML/KMZ, GeoJSON, FIT).

Contract (same as the communication importers, see ``docs/IMPORTERS_COMM.md``):
the opt-in ``enable_location`` is required per run and recorded; the file is
acquired through ``evidence.ingest_file`` (collection, source_stat semantics);
parsing reads the **stored copy**, never the original; re-importing the same
bytes with the same parameters is idempotent (same import row, samples, events
and observations; a new ``location_import_runs`` row and audit entry record the
run); every limit that is reached is reported as partial coverage.

A format module supplies ``parse(path, ctx)`` yielding records:

* ``('fix', locator, fix)`` — a positioned raw/provider sample (``store.fix_row``);
* ``('signal', locator, signal)`` — a non-positional raw signal;
* ``('event', event)`` — an aggregated event (visit, segment, track, ...) whose
  ``path`` lists ``(locator, fix)`` pairs stored as fixes;
* ``('issue', code, details)``.

Raw samples never become one event each: after the samples are stored, contiguous
sampling sessions of this import become ``location_samples`` events (one per
session, ``signal.class=passive``) so the timeline shows when a device recorded
positions without one item per fix.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import evidence
from ..db import session
from ..importers.comm_common import CommLimits, Issues, acquire, legacy_instant, record_import_run, stored_path
from ..timeinfo import TimeCandidate, from_iso, record_event_times
from . import store

SEMANTICS = {
    'raw_sample': 'device/provider position sample: a device or account location, not proof of a person\'s presence',
    'provider_inference': ('provider inference (third-party model output) from device signals; the provider\'s '
                           'confidence is a label, not our probability; not an observation of a person'),
    'provider_user_edit': ('provider inference that the account holder confirmed or edited in the provider\'s app '
                           '(flagged); still not an observation of a person'),
    'declared': 'declared by a person or an app (planned route, marked waypoint); not an observation of movement',
    'derived': 'derived by AGEDS from stored samples with a versioned rule; an interpretation, not an observation',
}
PROVIDER_TEXT = ('keep', 'omit')


class LocationOptInRequired(PermissionError):
    pass


class FormatUnavailable(RuntimeError):
    """An optional adapter (e.g. FIT without ``fitparse``) is not installed; nothing was acquired."""


@dataclass(frozen=True)
class LocationLimits:
    max_fixes: int = 50_000_000
    max_signals: int = 10_000_000
    max_events: int = 1_000_000
    max_path_points: int = 100_000
    max_element_bytes: int = 64 * 1024 * 1024
    max_sessions: int = 200_000
    max_issues: int = 500
    max_raw_chars: int = 4096
    max_raw_items: int = 200
    max_archive_member_bytes: int = 1024 * 1024 * 1024
    max_archive_members: int = 10_000

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f'limit {name} must be a positive integer')

    def replace(self, **changes) -> 'LocationLimits':
        return LocationLimits(**{**asdict(self), **{k: v for k, v in changes.items() if v is not None}})

    def raw(self) -> CommLimits:
        return CommLimits(max_raw_chars=self.max_raw_chars, max_raw_items=self.max_raw_items, max_issues=self.max_issues)


DEFAULT_LIMITS = LocationLimits()


@dataclass(frozen=True)
class ImportParams:
    """Per-run parameters; all of them are recorded and part of the import identity."""
    store_decimals: int | None = None
    stream: str | None = None
    provider_place_text: str = 'keep'
    sample_session_gap_s: int = 1800
    format_options: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.store_decimals is not None and (isinstance(self.store_decimals, bool) or
                                                not isinstance(self.store_decimals, int) or
                                                not 0 <= self.store_decimals <= 9):
            raise ValueError('store_decimals must be null (source precision) or an integer 0-9')
        if self.stream is not None and (not isinstance(self.stream, str) or not self.stream.strip()
                                        or len(self.stream) > 200 or any(ord(c) < 32 for c in self.stream)):
            raise ValueError('stream must be a non-blank printable string up to 200 characters')
        if self.provider_place_text not in PROVIDER_TEXT:
            raise ValueError(f'provider_place_text must be one of {PROVIDER_TEXT}')
        if isinstance(self.sample_session_gap_s, bool) or not isinstance(self.sample_session_gap_s, int) or \
                not 1 <= self.sample_session_gap_s <= 30 * 86400:
            raise ValueError('sample_session_gap_s must be an integer from 1 to 2592000')
        if not isinstance(self.format_options, dict):
            raise ValueError('format_options must be an object')

    def as_dict(self) -> dict:
        return {'store_decimals': self.store_decimals, 'stream': self.stream,
                'provider_place_text': self.provider_place_text, 'sample_session_gap_s': self.sample_session_gap_s,
                'format_options': dict(sorted(self.format_options.items()))}


@dataclass
class ParseContext:
    artifact_id: int
    path: Path
    params: ImportParams
    limits: LocationLimits
    issues: Issues
    counts: dict
    coverage: dict
    original_name: str | None = None

    def issue(self, code: str, **details) -> None:
        self.issues.add(code, **details)


def require_opt_in(enable_location: bool) -> dict:
    if not enable_location:
        raise LocationOptInRequired('location import is disabled; pass enable_location=True '
                                    '(CLI --enable-location, HTTP enable_location=true) to opt in')
    return {'enable_location': True, 'processing': 'local_only', 'reverse_geocoding': False, 'ip_geolocation': False,
            'map_tiles': False}


def _event_metadata(event: dict, *, parser: str, version: str, artifact_id: int, stream: str, provider: str,
                    opt_in: dict, params: ImportParams, signal_class: str) -> dict:
    basis = event.get('basis', 'provider_inference')
    meta = dict(event.get('metadata') or {})
    return {'parser': {'name': parser, 'version': version},
            'locator': {'artifact_id': artifact_id, **event['locator']},
            'signal': {'class': event.get('signal_class', signal_class), 'basis': basis, 'provider': provider,
                       'device_or_account': stream},
            **meta, 'opt_in': opt_in, 'store': {'store_decimals': params.store_decimals},
            'semantics': SEMANTICS.get(basis, SEMANTICS['provider_inference']),
            'issues': sorted(set(event.get('issues') or []))}


def run_import(path: Path, *, importer: str, parser_version: str, parse, enable_location: bool,
               params: ImportParams | None = None, source_kind: str, source_label: str, provider: str,
               mime_type: str | None = None, detect=None, source_locator: str | None = None,
               original_name: str | None = None, collection_id: int | None = None, label: str | None = None,
               limits: LocationLimits = DEFAULT_LIMITS, before_acquire=None) -> dict:
    """Acquire ``path``, parse its stored copy with ``parse`` and store samples/events (see module docstring).

    ``detect(stored_path) -> format label`` names the format recorded on the import;
    ``before_acquire()`` may raise ``FormatUnavailable`` so nothing is acquired.
    """
    opt_in = require_opt_in(enable_location)
    params = params or ImportParams()
    if before_acquire is not None:
        before_acquire()
    options = {**opt_in, 'parameters': params.as_dict()}
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(
        path, source_kind=source_kind, source_label=label or source_label, locator=locator, importer=importer,
        parser_version=parser_version, original_name=original_name, collection_id=collection_id, options=options,
        mime_type=mime_type)
    snapshot = stored_path(artifact_id)
    fmt = detect(snapshot) if detect else importer
    base_stream = store.stream_key(explicit=params.stream, collection_id=collection_id, source_id=source_id)
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        import_id, reused = store.ensure_import(db, artifact_id=artifact_id, importer=importer,
                                                parser_version=parser_version, fmt=fmt, stream=base_stream,
                                                params=params.as_dict())
    issues = Issues(limits.max_issues)
    counts = {'events': 0, 'events_by_type': {}, 'path_fixes': 0}
    coverage = {'complete': True, 'limits_reached': []}
    ctx = ParseContext(artifact_id, snapshot, params, limits, issues, counts, coverage, original_name)
    writer = store.SampleWriter(import_id, store_decimals=params.store_decimals)
    pending_events: list[dict] = []
    params_tag = store.sha256_text(store.canonical(params.as_dict()))[:12]

    def limit(name: str) -> None:
        if name not in coverage['limits_reached']:
            coverage['limits_reached'].append(name)
            issues.add('limit_reached', limit=name, value=getattr(limits, name))
        coverage['complete'] = False

    def stream_for(device_tag) -> str:
        return store.stream_key(explicit=params.stream, collection_id=collection_id, source_id=source_id,
                                device_tag=device_tag) if device_tag else base_stream

    def flush_events() -> None:
        if not pending_events:
            return
        writer.flush()
        with session() as db:
            db.execute('BEGIN IMMEDIATE')
            for item in pending_events:
                event_id = evidence.add_event(db=db, **item['event'])
                if item['times']:
                    record_event_times(db, event_id, item['times'])
        pending_events.clear()

    stopped = False
    for record in parse(snapshot, ctx):
        kind = record[0]
        if kind == 'issue':
            issues.add(record[1], **(record[2] if len(record) > 2 else {}))
            continue
        if kind == 'fix':
            if writer.counts['fixes_observed'] >= limits.max_fixes:
                limit('max_fixes')
                stopped = True
                break
            fix = record[2]
            writer.add_fix(fix, stream=stream_for(fix.get('device_tag')), locator=record[1])
            continue
        if kind == 'signal':
            if writer.counts['signals_observed'] >= limits.max_signals:
                limit('max_signals')
                continue
            writer.add_signal(record[2], stream=base_stream, locator=record[1])
            continue
        if kind == 'coverage':
            coverage.update(record[1])
            continue
        event = record[1]
        if counts['events'] >= limits.max_events:
            limit('max_events')
            stopped = True
            break
        device = (event.get('metadata') or {}).get('device_tag')
        for path_locator, fix in event.get('path') or ():
            if writer.counts['fixes_observed'] >= limits.max_fixes:
                limit('max_fixes')
                break
            writer.add_fix(fix, stream=stream_for(device), locator=path_locator)
            counts['path_fixes'] += 1
        start: TimeCandidate | None = event.get('start')
        end: TimeCandidate | None = event.get('end')
        times = [c for c in (start, end) if c is not None] + list(event.get('other_times') or ())
        if start is not None and end is not None and start.raw == end.raw and start.source == end.source:
            times = [start, *event.get('other_times', ())]
        metadata = _event_metadata(event, parser=importer, version=parser_version, artifact_id=artifact_id,
                                   stream=stream_for(device), provider=provider, opt_in=opt_in, params=params,
                                   signal_class=event.get('signal_class', 'passive'))
        loc = event['locator']
        key = ':'.join(str(loc.get(k)) for k in ('format', 'section', 'index') if loc.get(k) is not None)
        pending_events.append({'times': times, 'event': {
            'source_id': source_id, 'artifact_id': artifact_id, 'event_type': event['event_type'],
            'ts_start': legacy_instant(start), 'ts_end': legacy_instant(end), 'subject': event.get('subject'),
            'body': event.get('body'), 'external_id': f'{importer}:{parser_version}:{params_tag}:{key}',
            'confidence': None, 'metadata': metadata}})
        counts['events'] += 1
        counts['events_by_type'][event['event_type']] = counts['events_by_type'].get(event['event_type'], 0) + 1
        if len(pending_events) >= 500:
            flush_events()
    flush_events()
    writer.flush()
    if stopped:
        coverage['complete'] = False
    # Sampling sessions of this import's raw samples -> one passive event per session.
    with session() as db:
        sessions, truncated = store.sample_sessions(store.iter_import_fixes(db, import_id),
                                                    gap_s=params.sample_session_gap_s,
                                                    max_sessions=limits.max_sessions)
    if truncated:
        limit('max_sessions')
    for chunk_start in range(0, len(sessions), 500):
        with session() as db:
            db.execute('BEGIN IMMEDIATE')
            for s in sessions[chunk_start:chunk_start + 500]:
                start = from_iso('start', store.us_to_iso(s['first_us']), f'{importer}:{parser_version}:sampling_session')
                end = from_iso('end', store.us_to_iso(s['last_us']), f'{importer}:{parser_version}:sampling_session')
                metadata = {'parser': {'name': importer, 'version': parser_version},
                            'locator': {'artifact_id': artifact_id, 'location_import_id': import_id,
                                        'first_fix_id': s['first_fix_id'], 'last_fix_id': s['last_fix_id']},
                            'signal': {'class': 'passive', 'basis': 'raw_sample', 'provider': provider,
                                       'device_or_account': s['stream']},
                            'sampling': {'fixes': s['fixes'], 'gap_rule_s': params.sample_session_gap_s,
                                         'max_gap_s': s['max_gap_us'] / 1e6, 'fix_kinds': s['kinds'],
                                         'provider_sources': s['sources'], 'accuracy_m': s['accuracy_m'],
                                         'bbox_stored_precision': s['bbox'], 'decimals': s['decimals']},
                            'opt_in': opt_in, 'store': {'store_decimals': params.store_decimals},
                            'semantics': ('sampling session: the device recorded positions in this span; the '
                                          'times are those of the first and last sample, gaps up to the rule are '
                                          'not observations of continuous presence'),
                            'issues': []}
                event_id = evidence.add_event(
                    db=db, source_id=source_id, artifact_id=artifact_id, event_type='location_samples',
                    ts_start=start.utc, ts_end=end.utc, subject=f'{s["fixes"]} próbek',
                    external_id=(f'{importer}:{parser_version}:{params_tag}:samples:{s["stream"]}:{s["first_us"]}:'
                                 f'{s["last_us"]}:{s["fixes"]}:{s["first_fix_id"]}'),
                    confidence=None, metadata=metadata)
                record_event_times(db, event_id, [start, end] if s['last_us'] != s['first_us'] else [start])
    counts['sampling_sessions'] = len(sessions)
    counts.update(writer.counts)
    counts['events_by_type'] = dict(sorted(counts['events_by_type'].items()))
    issue_report = issues.as_dict()
    store.record_run(import_id, counts=counts, coverage=coverage, issues={'by_code': issue_report['by_code'],
                                                                         'total': issue_report['total']})
    record_import_run(artifact_id=artifact_id, importer=importer, parser_version=parser_version, options=options,
                      counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'location_import_id': import_id,
            'import_reused': reused, 'importer': importer, 'parser_version': parser_version, 'format': fmt,
            'stream': base_stream, 'options': options, **{k: v for k, v in counts.items()},
            'coverage': coverage, 'issues': issue_report, 'limits': asdict(limits)}


def read_prefix(path: Path, size: int = 65536) -> bytes:
    with path.open('rb') as handle:
        return handle.read(size)


def env_flag(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes')


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
