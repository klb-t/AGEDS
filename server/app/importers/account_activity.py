"""Account security and login activity (Facebook, Instagram, Google Takeout) — traces area.

Each record → one ``account_activity`` event (``contact_label`` = provider,
``subject`` = action) with:

* every stated time as a candidate: activity ``created``; session/device
  ``start`` (created) and ``updated`` (last activity). Epoch seconds and ISO text
  with an offset fix the instant; ``2023-01-01 10:00:00 UTC`` is read as UTC;
  anything else stays raw and unparsed;
* ``metadata.account_activity``: action, **IP address raw** (with a syntactic
  ``ipaddress`` check, version and scope flags), user agent, device, app/site,
  session type, language;
* cookie values (``datr``, cookie names) are **not stored**: only presence and a
  16-hex SHA-256 prefix to compare records of the same export;
* provider location fields (city/region/country, "Estimated location") are the
  provider's IP geolocation — **third-party inference**. The event only says they
  are present; their values go to ``trace_provider_locations`` and only under the
  ``enable_location`` opt-in, so events stay identical across opt-in runs. AGEDS
  performs no IP geolocation (a possible future opt-in that would send IPs to a
  third party).

The IP is not put into the event body or the search index. A login record is an
observation of an account session from some device, not of a person.
Facebook/Instagram text uses the ``fb_latin1_utf8_v1`` decoded layer.
"""
from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import re
from pathlib import Path

from ..db import session
from ..timeinfo import from_iso
from ..traces.common import (TextLayers, append_row, bounded, dumps, epoch_time, mapping, read_json, signal,
                             write_event)
from .comm_common import DEFAULT_LIMITS, CommLimits, Issues, acquire, legacy_instant, record_import_run, stored_path

PARSER_NAME = 'account_activity'
PARSER_VERSION = '1'
MAPPING = 'account_activity.v1.json'
_UTC_SUFFIX = re.compile(r'\s+(?:UTC|GMT)$')


def time_value(kind: str, value, source: str, field_name: str):
    """(candidate, problem) for an epoch-seconds number/string or ISO-like text."""
    if value is None or value == '':
        return None, None
    if isinstance(value, (int, float)) and not isinstance(value, bool) or (isinstance(value, str) and re.fullmatch(r'-?\d{1,12}', value.strip())):
        return epoch_time(kind, value, source, unit='s', metadata={'field': field_name})
    if isinstance(value, str):
        text = _UTC_SUFFIX.sub('Z', value.strip())
        try:
            return from_iso(kind, text, source, metadata={'field': field_name, 'raw_text': value}), None
        except ValueError:
            return None, 'time_unparsed'
    return None, 'time_not_text_or_number'


def ip_info(raw) -> dict | None:
    if raw in (None, ''):
        return None
    text = str(raw).strip()
    try:
        address = ipaddress.ip_address(text.split('%', 1)[0])
    except ValueError:
        return {'raw': text[:200], 'status': 'not_an_ip_address'}
    return {'raw': text[:200], 'status': 'parsed', 'version': address.version, 'is_private': address.is_private,
            'is_global': address.is_global, 'geolocation': 'not_performed'}


def _flatten(record: dict) -> dict:
    """Instagram ``string_map_data`` / Facebook ``label_values`` → label → value (raw kept elsewhere)."""
    flat = {}
    for key, value in record.items():
        if key == 'string_map_data' and isinstance(value, dict):
            for label, entry in value.items():
                if isinstance(entry, dict):
                    flat[label] = entry.get('value') if entry.get('value') not in (None, '') else entry.get('timestamp')
        elif key == 'label_values' and isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict) and isinstance(entry.get('label'), str):
                    flat[entry['label']] = entry.get('value', entry.get('timestamp_value'))
        elif not isinstance(value, (dict, list)):
            flat[key] = value
    if 'title' in record and isinstance(record['title'], str) and 'Time' not in flat and 'timestamp' not in flat:
        flat['Time'] = record['title']
    return flat


def _pick(flat: dict, names: list[str]):
    for name in names:
        if name in flat and flat[name] not in (None, ''):
            return name, flat[name]
    return None, None


def records_of(data, name: str, config: dict) -> tuple[str, str, list] | None:
    """(provider, kind, records) for a JSON document, or None when the layout is unknown."""
    if isinstance(data, dict):
        for root, spec in config['json_roots'].items():
            if isinstance(data.get(root), list):
                return spec['provider'], spec['kind'], data[root]
    if isinstance(data, list) and data and isinstance(data[0], dict) and 'label_values' in data[0]:
        kind = config['label_values_files'].get(name.lower())
        return 'facebook', kind or 'account_activity', data
    return None


def _write(records: list[tuple[int, dict, dict]], *, provider: str, kind: str, variant: str, artifact_id: int,
           source_id: int, enable_location: bool, config: dict, limits: CommLimits, issues: Issues) -> dict:
    fields = config['fields']
    rule = config['provider_text_decoding'].get(provider)
    counts = {'records': 0, 'events': 0, 'provider_location_present': 0, 'provider_location_stored': 0}
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        for index, raw, flat in records:
            counts['records'] += 1
            layers = TextLayers(rule, limits)
            problems = []
            candidates = []
            for slot, time_kind in (('time', 'created'), ('start', 'start'), ('updated', 'updated')):
                field_name, value = _pick(flat, fields[slot])
                if field_name is None:
                    continue
                candidate, problem = time_value(time_kind, value, f'{PARSER_NAME}@{PARSER_VERSION}:{field_name}', field_name)
                if problem:
                    problems.append(f'{problem}:{field_name}')
                elif candidate is not None:
                    candidates.append(candidate)
            if not candidates:
                problems.append('time_missing')
            action = layers.text('action', _pick(flat, fields['action'])[1]) if isinstance(_pick(flat, fields['action'])[1], str) else None
            activity = {'record_kind': kind, 'action': action or kind,
                        'ip': ip_info(_pick(flat, fields['ip'])[1]),
                        'user_agent': layers.text('user_agent', _pick(flat, fields['user_agent'])[1]),
                        'device': layers.text('device', _pick(flat, fields['device'])[1]),
                        'app_or_site': layers.text('app', _pick(flat, fields['app'])[1]),
                        'session_type': layers.text('session_type', _pick(flat, fields['session_type'])[1]),
                        'language': _pick(flat, fields['language'])[1]}
            cookie_field, cookie = _pick(flat, fields['cookie'])
            activity['cookie'] = None if cookie is None else {
                'field': cookie_field, 'value': 'not_stored',
                'sha256_prefix': hashlib.sha256(str(cookie).encode('utf-8', 'surrogatepass')).hexdigest()[:16]}
            location = {name: flat[name] for name in fields['provider_location'] if flat.get(name) not in (None, '')}
            if location:
                counts['provider_location_present'] += 1
                activity['provider_location'] = {'present': True, 'fields': sorted(location),
                                                 'values': 'trace_provider_locations (enable_location only)'}
            if activity['ip'] and activity['ip']['status'] != 'parsed':
                problems.append('ip_address_not_parsed')
            secret_keys = set(fields['cookie']) | set(fields['provider_location'])
            raw_copy, truncated = bounded(_redact(raw, secret_keys), limits)
            metadata = {'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION, 'mapping_version': config['version'],
                                   'variant': variant},
                        'locator': {'artifact_id': artifact_id, 'record_index': index},
                        'account_activity': activity, 'text': layers.as_dict(),
                        'signal': signal('passive', 'raw_sample', provider, 'account',
                                         note='security log kept by the provider; an account session from some device'),
                        'raw': raw_copy, 'raw_truncated_values': truncated,
                        'semantics': 'account activity record as exported; not proof of who used the account or where',
                        'issues': problems}
            created = next((c for c in candidates if c.kind in ('created', 'start') and c.utc), None)
            event_id = write_event(db, candidates, source_id=source_id, artifact_id=artifact_id,
                                   event_type='account_activity', ts_start=legacy_instant(created),
                                   contact_label=provider, subject=activity['action'],
                                   external_id=f'{PARSER_NAME}@{PARSER_VERSION}:{kind}:{index}', confidence=None,
                                   metadata=metadata)
            counts['events'] += 1
            if location and enable_location:
                values = {k: layers.text(f'location/{k}', v) if isinstance(v, str) else v for k, v in location.items()}
                append_row(db, 'trace_provider_locations',
                           {'event_id': event_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION},
                           {'artifact_id': artifact_id, 'provider': provider, 'values_json': dumps(values),
                            'basis': 'provider_inference',
                            'opt_in_json': dumps({'enable_location': True, 'processing': 'local_only'})})
                counts['provider_location_stored'] += 1
            for problem in problems:
                issues.add(problem, record_index=index)
    return counts


def _redact(value, keys: set):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in keys:
                out[key] = 'not_stored'
            elif key == 'string_map_data' and isinstance(item, dict):
                out[key] = {label: ('not_stored' if label in keys else entry) for label, entry in item.items()}
            elif key == 'label_values' and isinstance(item, list):
                out[key] = [{**entry, 'value': 'not_stored'} if isinstance(entry, dict) and entry.get('label') in keys
                            else entry for entry in item]
            else:
                out[key] = _redact(item, keys)
        return out
    if isinstance(value, list):
        return [_redact(v, keys) for v in value]
    return value


def _csv_records(path: Path, name: str, config: dict, limits: CommLimits, issues: Issues):
    data = path.read_bytes()
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('utf-8', errors='replace')
        issues.add('invalid_utf8_replaced')
    reader = csv.reader(io.StringIO(text, newline=''))
    try:
        rows = list(row for _, row in zip(range(limits.max_records + 2), reader))
    except csv.Error as exc:
        raise ValueError(f'CSV not readable: {exc}') from exc
    if not rows:
        raise ValueError('empty CSV')
    header = [cell.strip() for cell in rows[0]]
    for profile in config['csv_profiles']:
        if re.match(profile['name_regex'], name) and all(col in header for col in profile['required']):
            if len(rows) > limits.max_records + 1:
                issues.add('record_limit', limit=limits.max_records)
            out = []
            for index, row in enumerate(rows[1:limits.max_records + 1]):
                record = dict(zip(header, row))
                if len(row) != len(header):
                    issues.add('row_width_differs_from_header', row_index=index)
                out.append((index, record, record))
            return profile['provider'], profile['kind'], profile['id'], out
    raise ValueError('CSV header does not match a known account activity profile '
                     f'({", ".join(p["id"] for p in config["csv_profiles"])})')


def import_account_activity(path: Path, *, enable_location: bool = False, provider_hint: str | None = None,
                            source_locator: str | None = None, original_name: str | None = None,
                            collection_id: int | None = None, label: str | None = None,
                            limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """One login/security activity file (JSON or Google CSV)."""
    config, _ = mapping(MAPPING)
    path = Path(path)
    name = original_name or path.name
    options = {'enable_location': bool(enable_location), 'processing': 'local_only', 'ip_geolocation': False}
    issues = Issues(limits.max_issues)
    is_csv = name.lower().endswith('.csv')
    if not is_csv:
        probe = read_json(path, limits, issues)
        if records_of(probe, name, config) is None:
            raise ValueError('JSON layout is not a known account activity file')
    _, artifact_id = acquire(path, source_kind='account_activity_export', source_label=label or 'Account activity export',
                             locator=source_locator if source_locator is not None else str(path), importer=PARSER_NAME,
                             parser_version=PARSER_VERSION, original_name=original_name, options=options,
                             collection_id=collection_id, mime_type='text/csv' if is_csv else 'application/json')
    with session() as db:
        source_id = db.execute('SELECT source_id FROM artifacts WHERE id=?', (artifact_id,)).fetchone()['source_id']
    snapshot = stored_path(artifact_id)
    if is_csv:
        provider, kind, variant, records = _csv_records(snapshot, name, config, limits, issues)
    else:
        provider, kind, records_raw = records_of(read_json(snapshot, limits, issues), name, config)
        variant = 'json'
        if provider_hint in ('facebook', 'instagram') and provider != provider_hint:
            issues.add('provider_hint_differs_from_layout', layout=provider, hint=provider_hint)
        records = []
        for index, record in enumerate(records_raw):
            if index >= limits.max_records:
                issues.add('record_limit', limit=limits.max_records)
                break
            if not isinstance(record, dict):
                issues.add('record_not_an_object', record_index=index)
                continue
            records.append((index, record, _flatten(record)))
    counts = _write(records, provider=provider, kind=kind, variant=variant, artifact_id=artifact_id, source_id=source_id,
                    enable_location=enable_location, config=config, limits=limits, issues=issues)
    coverage = {'complete': not ({'record_limit', 'record_not_an_object'} & set(issues.counts))}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts=counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'source_id': source_id, 'provider': provider, 'record_kind': kind,
            'variant': variant, 'parser_version': PARSER_VERSION, **counts, 'options': options, 'coverage': coverage,
            'issues': issues.as_dict()}
