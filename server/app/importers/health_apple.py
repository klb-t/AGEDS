"""Apple Health ``export.xml`` (or the export zip) → samples and ``health_session`` events. Opt-in.

Streaming with bounded memory: each top-level ``Record``, ``Workout``,
``ActivitySummary`` and ``Correlation`` is read as a small subtree and
discarded; the internal DTD Apple ships is accepted (ELEMENT/ATTLIST only),
entity declarations are refused and ATTLIST defaults never appear as data.
Inside a zip, ``apple_health_export/export.xml`` is streamed from the stored
zip without extracting it.

* ``Record`` → one ``health_samples`` row (type, value, unit, start/end with the
  stated offset such as ``+0100``, ``sourceName``/``sourceVersion``/``device``
  raw). Basis: ``provider_user_edit`` when ``HKWasUserEntered`` is set,
  ``provider_inference`` for computed types (data file), else ``raw_sample``.
* Sleep-analysis records are samples too and are grouped into sleep sessions by
  rule ``ledger.sleep_session.v1`` per source and device (``sleep_session_gap_s``,
  recorded): two devices' overlapping nights stay two sessions.
* ``Workout`` → one session (declared duration and the start–end span both kept)
  plus its ``WorkoutStatistics``/``WorkoutEvent`` rows as linked samples.
* ``ActivitySummary`` → daily summary samples (day precision, zone unknown).
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from ..ledger import health, store, xmlstream
from ..timeinfo import TimeCandidate
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, bound_json, load_data, local_candidate,
                          record_import_run, stored_path)
from ..ledger import timeparse

PARSER_NAME = 'apple_health_export'
PARSER_VERSION = '1'
PROVIDER = 'apple_health'
TOP = frozenset({'Record', 'Workout', 'ActivitySummary', 'Correlation', 'ExportDate', 'Me'})
ZIP_MEMBER_SUFFIX = 'export.xml'


def _metadata_entries(node: xmlstream.Node) -> dict:
    return {m.attrib.get('key'): m.attrib.get('value') for m in node.findall('MetadataEntry') if m.attrib.get('key')}


def import_apple_health(path: Path, *, enable_health: bool = False, sleep_session_gap_s: int = 3600,
                        source_locator: str | None = None, original_name: str | None = None,
                        collection_id: int | None = None, label: str | None = None,
                        limits: CommLimits = DEFAULT_LIMITS) -> dict:
    health.require_opt_in(enable_health)
    if isinstance(sleep_session_gap_s, bool) or not isinstance(sleep_session_gap_s, int) or \
            not 0 <= sleep_session_gap_s <= 86400:
        raise ValueError('sleep_session_gap_s must be an integer number of seconds in [0, 86400]')
    mapping = load_data('health_apple.v1.json')
    locator = source_locator if source_locator is not None else str(path)
    options = {'enable_health': True, 'sleep_session_gap_s': sleep_session_gap_s}
    source_id, artifact_id = acquire(
        path, source_kind='health_export', source_label=label or 'Apple Health export', locator=locator,
        importer=PARSER_NAME, parser_version=PARSER_VERSION, original_name=original_name, collection_id=collection_id,
        options=options)
    snapshot = stored_path(artifact_id)
    issues = Issues(limits.max_issues)
    with snapshot.open('rb') as probe:
        is_zip = probe.read(4) == b'PK\x03\x04'
    member = None
    if is_zip:
        archive = zipfile.ZipFile(snapshot)
        names = [i.filename for i in archive.infolist() if i.filename.endswith(ZIP_MEMBER_SUFFIX)
                 and not i.filename.endswith('export_cda.xml')]
        if len(names) != 1:
            archive.close()
            raise ValueError(f'expected one export.xml in the zip, found {len(names)}')
        member = names[0]
        stream_source = archive.open(member)
    else:
        archive, stream_source = None, snapshot
    state = _State(artifact_id, source_id, mapping, issues, limits, sleep_session_gap_s, member)
    try:
        for item in xmlstream.stream(stream_source, lambda tag, parent: parent == 'HealthData' and tag in TOP,
                                     allow_internal_dtd=True):
            if item[0] == 'root':
                if item[1] != 'HealthData':
                    raise ValueError(f'not an Apple Health export (root element {item[1]!r})')
                continue
            if item[0] != 'node':
                continue
            if not state.take(item[1]):
                break
    finally:
        if archive is not None:
            stream_source.close()
            archive.close()
    state.samples.flush()
    sessions = state.finish_sleep()
    coverage = {'complete': not state.limited}
    counts = {**state.counts, 'samples_inserted': state.samples.inserted, 'samples_existing': state.samples.existing,
              'sleep_sessions': sessions}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION,
            'zip_member': member, 'export_date': state.export_date, **counts, 'coverage': coverage,
            'issues': issues.as_dict(), 'opt_in': health.opt_in_record()}


class _State:
    def __init__(self, artifact_id, source_id, mapping, issues, limits, gap, member):
        self.artifact_id, self.source_id, self.mapping = artifact_id, source_id, mapping
        self.issues, self.limits, self.gap, self.member = issues, limits, gap, member
        self.ordinals: dict[str, int] = {}
        self.samples = health.SampleWriter()
        self.sleep: list[dict] = []
        self.counts = {'records': 0, 'workouts': 0, 'activity_summaries': 0, 'correlations': 0, 'sleep_samples': 0}
        self.seen = 0
        self.limited = False
        self.export_date = None
        self.inferred = set(mapping['provider_inference_types'])
        self.formats = mapping['time_formats']

    def position(self, tag: str) -> str:
        index = self.ordinals.get(tag, 0)
        self.ordinals[tag] = index + 1
        return f'{tag}[{index}]'

    def time(self, kind, raw, source, problems):
        candidate, problem = health.time_candidate(kind, raw, source, self.formats)
        if problem:
            problems.append(problem)
        return candidate

    def take(self, node: xmlstream.Node) -> bool:
        if node.tag == 'ExportDate':
            self.export_date = node.attrib.get('value')
            return True
        if node.tag == 'Me':
            return True  # personal characteristics (birth date, sex, blood type) are not imported
        self.seen += 1
        if self.seen > self.limits.max_records:
            self.limited = True
            self.issues.add('record_limit', limit=self.limits.max_records)
            return False
        position = self.position(node.tag)
        if node.tag == 'Record':
            self.counts['records'] += 1
            self.record(node, position, None)
        elif node.tag == 'Correlation':
            self.counts['correlations'] += 1
            for index, child in enumerate(node.findall('Record')):
                self.counts['records'] += 1
                self.record(child, f'{position}/Record[{index}]', {'correlation_type': node.attrib.get('type'),
                                                                   'correlation_position': position})
        elif node.tag == 'Workout':
            self.counts['workouts'] += 1
            self.workout(node, position)
        elif node.tag == 'ActivitySummary':
            self.counts['activity_summaries'] += 1
            self.summary(node, position)
        if node.truncated:
            self.issues.add('subtree_truncated', position=position)
        return True

    def record(self, node: xmlstream.Node, position: str, correlation: dict | None) -> None:
        a = node.attrib
        problems: list[str] = []
        kind = a.get('type') or 'unknown'
        source = f'{PROVIDER}:{position}'
        start = self.time('start', a.get('startDate'), f'{source}:start', problems)
        end = self.time('end', a.get('endDate'), f'{source}:end', problems)
        meta = _metadata_entries(node)
        user = any(meta.get(k) in ('1', 'true', 'YES') for k in self.mapping['user_entered_metadata_keys'])
        basis = 'provider_user_edit' if user else ('provider_inference' if kind in self.inferred else 'raw_sample')
        span = health.duration(start, end)
        if span['status'] == 'negative':
            problems.append('end_before_start')
        extra = {'creation_date': a.get('creationDate'), 'metadata_entries': bound_json(meta, self.limits)[0],
                 'span': span, 'issues': problems, 'zip_member': self.member, 'xml_line': node.line,
                 **({'correlation': correlation} if correlation else {})}
        if node.findall('HeartRateVariabilityMetadataList'):
            extra['hrv_instantaneous_points'] = sum(len(c.children) for c in node.findall('HeartRateVariabilityMetadataList'))
        for problem in problems:
            self.issues.add(problem, position=position)
        row = health.sample_row(artifact_id=self.artifact_id, parser=PARSER_NAME, parser_version=PARSER_VERSION,
                                position=position, sample_type=kind,
                                sample_class='category' if kind.startswith('HKCategory') else 'quantity',
                                value_raw=a.get('value'), unit=a.get('unit'), start=start, end=end,
                                source_name=a.get('sourceName'), source_version=a.get('sourceVersion'),
                                device_raw=(a.get('device') or None) and a.get('device')[:self.limits.max_raw_chars],
                                basis=basis, metadata=extra)
        if kind == self.mapping['sleep_type'] and start is not None:
            self.counts['sleep_samples'] += 1
            self.sleep.append({'row': row, 'start': start, 'end': end, 'source': a.get('sourceName'),
                               'device': a.get('device'), 'position': position, 'value': a.get('value')})
            return
        self.samples.add(row)

    def workout(self, node: xmlstream.Node, position: str) -> None:
        a = node.attrib
        problems: list[str] = []
        source = f'{PROVIDER}:{position}'
        start = self.time('start', a.get('startDate'), f'{source}:start', problems)
        end = self.time('end', a.get('endDate'), f'{source}:end', problems)
        created = self.time('created', a.get('creationDate'), f'{source}:created', problems)
        meta = _metadata_entries(node)
        user = any(meta.get(k) in ('1', 'true', 'YES') for k in self.mapping['user_entered_metadata_keys'])
        span = health.duration(start, end)
        if span['status'] == 'negative':
            problems.append('end_before_start')
        declared = {'value': a.get('duration'), 'unit': a.get('durationUnit'), 'exact': health.exact_value(a.get('duration'))}
        children = []
        for index, stat in enumerate(node.findall('WorkoutStatistics')):
            s = stat.attrib
            s_start = self.time('start', s.get('startDate'), f'{source}/WorkoutStatistics[{index}]:start', problems)
            s_end = self.time('end', s.get('endDate'), f'{source}/WorkoutStatistics[{index}]:end', problems)
            value = s.get('sum') if s.get('sum') is not None else s.get('average')
            children.append(health.sample_row(
                artifact_id=self.artifact_id, parser=PARSER_NAME, parser_version=PARSER_VERSION,
                position=f'{position}/WorkoutStatistics[{index}]', sample_type=s.get('type') or 'unknown',
                sample_class='workout_statistic', value_raw=value, unit=s.get('unit'), start=s_start, end=s_end,
                source_name=a.get('sourceName'), source_version=a.get('sourceVersion'), device_raw=None,
                basis='provider_inference', metadata={'statistics': {k: s.get(k) for k in ('sum', 'average', 'minimum', 'maximum')
                                                                     if s.get(k) is not None}}))
        events = [{'type': e.attrib.get('type'), 'date': e.attrib.get('date'), 'duration': e.attrib.get('duration'),
                   'durationUnit': e.attrib.get('durationUnit')} for e in node.findall('WorkoutEvent')]
        routes = [r.text_of('FileReference') or (r.find('FileReference').attrib.get('path') if r.find('FileReference') else None)
                  for r in node.findall('WorkoutRoute')]
        activity = a.get('workoutActivityType')
        children.insert(0, health.sample_row(
            artifact_id=self.artifact_id, parser=PARSER_NAME, parser_version=PARSER_VERSION, position=position,
            sample_type=activity or 'HKWorkout', sample_class='workout', value_raw=a.get('duration'),
            unit=a.get('durationUnit'), start=start, end=end, source_name=a.get('sourceName'),
            source_version=a.get('sourceVersion'), device_raw=(a.get('device') or None),
            basis='provider_user_edit' if user else 'provider_inference',
            metadata={'totals': {k: a.get(k) for k in ('totalDistance', 'totalDistanceUnit', 'totalEnergyBurned',
                                                       'totalEnergyBurnedUnit') if a.get(k) is not None}}))
        for problem in problems:
            self.issues.add(problem, position=position)
        signal = health.session_signal(PROVIDER, a.get('sourceName'), cls='declared' if user else 'active',
                                       basis='provider_user_edit' if user else 'provider_inference',
                                       note='workout record kept by the app; type, energy and distance are its computations')
        event = health.session_event(
            source_id=self.source_id, artifact_id=self.artifact_id, external_id=f'{PARSER_NAME}:{position}',
            kind='workout', activity_raw=activity, start=start, end=end, provider=PROVIDER,
            source_name=a.get('sourceName'), signal=signal,
            metadata={'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                      'locator': {'artifact_id': self.artifact_id, 'element': position, 'xml_line': node.line,
                                  'zip_member': self.member},
                      'session': {'declared_duration': declared, 'workout_events': events[:200],
                                  'routes': [r for r in routes if r], 'metadata_entries': bound_json(meta, self.limits)[0],
                                  'device_raw': (a.get('device') or '')[:self.limits.max_raw_chars] or None},
                      'issues': problems})
        health.write_session(event, [c for c in (start, end, created) if c is not None], children)

    def summary(self, node: xmlstream.Node, position: str) -> None:
        a = node.attrib
        problems: list[str] = []
        day = a.get('dateComponents')
        start = None
        if day:
            parsed = timeparse.parse(day, ['YYYY-MM-DD'])
            if parsed['status'] == 'ok':
                start = local_candidate('start', day, f'{PROVIDER}:{position}:day', parsed['naive'], 'day',
                                        {'zone_status': 'export_zone_not_stated', 'date_kind': 'summary_day'})
            else:
                problems.append('summary_day_unparsed')
        for metric in self.mapping['activity_summary_metrics']:
            if a.get(metric) is None:
                continue
            unit = a.get(self.mapping['activity_summary_units'].get(metric, ''), None) if metric in \
                self.mapping['activity_summary_units'] else None
            self.samples.add(health.sample_row(
                artifact_id=self.artifact_id, parser=PARSER_NAME, parser_version=PARSER_VERSION,
                position=f'{position}/{metric}', sample_type=f'ActivitySummary.{metric}', sample_class='daily_summary',
                value_raw=a.get(metric), unit=unit, start=start, end=None, source_name=None, source_version=None,
                device_raw=None, basis='provider_inference', metadata={'day': day, 'issues': problems}))

    def finish_sleep(self) -> int:
        groups = health.group_sleep(self.sleep, self.gap)
        asleep = set(self.mapping['sleep_asleep_values'])
        for group in groups:
            first, last = group[0], max(group, key=lambda s: health.instant(s['end'] or s['start'])[1])
            stage_seconds: dict[str, float] = {}
            for sample in group:
                span = health.duration(sample['start'], sample['end'])
                if span['status'] == 'ok':
                    stage_seconds[sample['value'] or 'unknown'] = stage_seconds.get(sample['value'] or 'unknown', 0) + span['seconds']
            start = _copy(first['start'], 'start', f'{PROVIDER}:sleep_session:{first["position"]}:start')
            end = _copy(last['end'] or last['start'], 'end', f'{PROVIDER}:sleep_session:{first["position"]}:end')
            source_key = store.sha256_of([first['source'], first['device']])[:12]
            event = health.session_event(
                source_id=self.source_id, artifact_id=self.artifact_id,
                external_id=f'{PARSER_NAME}:sleep:{source_key}:{first["position"]}:{health.SLEEP_SESSION_RULE}:gap{self.gap}',
                kind='sleep', activity_raw=None, start=start, end=end, provider=PROVIDER, source_name=first['source'],
                signal=health.session_signal(PROVIDER, first['source'],
                                             note='sleep stages are the device/app inference, grouped by a versioned rule'),
                metadata={'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                          'locator': {'artifact_id': self.artifact_id, 'first_sample': first['position'],
                                      'last_sample': group[-1]['position'], 'zip_member': self.member},
                          'session': {'grouping': {'rule': health.SLEEP_SESSION_RULE, 'gap_s': self.gap,
                                                   'key': 'source_name+device'},
                                      'samples': len(group), 'stage_seconds': stage_seconds,
                                      'asleep_seconds_sum': sum(v for k, v in stage_seconds.items() if k in asleep),
                                      'device_raw': (first['device'] or '')[:self.limits.max_raw_chars] or None},
                          'issues': []})
            health.write_session(event, [start, end], [s['row'] for s in group])
        return len(groups)


def _copy(candidate: TimeCandidate, kind: str, source: str) -> TimeCandidate:
    return TimeCandidate(kind, candidate.raw, source, utc=candidate.utc, local=candidate.local,
                         tz_status=candidate.tz_status, tz=candidate.tz, precision=candidate.precision,
                         metadata={**candidate.metadata, 'derived_from_sample': candidate.source})
