"""Google Photos Takeout sidecars and photo geotags (EXIF GPS) — traces area.

Inputs:

* sidecar JSON files next to media (``IMG_1.jpg.json``,
  ``IMG_1.jpg.supplemental-metadata.json``, cut names at the observed length
  limit, ``IMG_1.jpg(1).json`` for the duplicate ``IMG_1(1).jpg``);
* media files: EXIF of JPEG/PNG/TIFF through ``exif.py`` (``exif_minimal`` v1
  events, unchanged) plus the GPS IFD (parser ``exif_gps`` v1). HEIC/video
  containers are listed as ``exif_container_not_supported``.

What is stored (see ``schema/traces.py``):

* a ``photo_metadata`` event per sidecar (title as subject, every stated time as a
  candidate: ``photoTakenTime`` → ``created``, ``creationTime`` (item creation /
  upload in Google Photos) → ``other``, ``photoLastModifiedTime`` → ``updated``;
  the localized ``formatted`` strings are kept raw in candidate metadata, not
  parsed); the sidecar row keeps description, url and the ``people`` names raw.
  Third-party names are never put into event columns, bodies or the search index;
* ``geoData`` and ``geoDataExif`` are both kept as separate geotags, and the
  sidecar row records whether they agree (tolerance from the rules file); ``0,0``
  is Google's "no location" placeholder, not a position;
* sidecar → media matching is a hypothesis per rule id over a hashed scope
  (the files of the same directory): one described file → ``single_candidate``,
  several → ``ambiguous`` (nothing chosen), none → ``no_candidate``; edited copies
  (``-edited`` and localized forms) are listed with ``relation='edited_variant'``;
* EXIF GPS: position → ``exif_gps`` geotag; ``GPSDateStamp``+``GPSTimeStamp`` →
  a separate UTC ``exif`` candidate appended to the ``exif_minimal`` event of the
  same artifact (or to an own ``photo_metadata`` event when EXIF has no time).

Coordinates are stored only with ``enable_location`` (recorded per run); without
it geotags record presence only. The preserved files themselves always contain
what the export contains.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from ..db import session
from ..timeinfo import TimeCandidate
from ..traces import geotags
from ..traces.common import (append_row, bounded, canonical_sha256, clean_text, dumps, epoch_time, read_json,
                             signal, write_event)
from . import exif
from .comm_common import (DEFAULT_LIMITS, CommLimits, Issues, acquire, legacy_instant, load_data,
                          record_import_run, stored_path, walk_files)

PARSER_NAME = 'google_photos_sidecar'
PARSER_VERSION = '1'
MEDIA_IMPORTER = 'google_photos_media'
MAX_SIDECAR_BYTES = 1024 * 1024
_COUNTER = re.compile(r'^(.*)\((\d{1,4})\)$')
# Rules that name the media directly; the truncation rules only apply when none of these fits.
STRONG_RULES = frozenset({'sidecar_name_exact_v1', 'duplicate_counter_v1', 'title_exact_v1', 'base_without_extension_v1'})
_TIME_FIELDS = (('photoTakenTime', 'created', 'photo taken time as recorded by Google Photos (from the file, '
                 'the upload or a user edit; provider value)'),
                ('creationTime', 'other', 'item creation (upload) time in Google Photos'),
                ('photoLastModifiedTime', 'updated', 'last modification of the item in Google Photos'))


def rules() -> dict:
    return load_data('photos_takeout.v1.json')


def _nfc(text: str) -> str:
    return unicodedata.normalize('NFC', text)


def _split(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition('.')
    return (stem, '.' + ext) if dot and stem else (name, '')


def _media_ext(name: str, config: dict) -> bool:
    return _split(name)[1].lower() in config['media_extensions']


def analyse_name(name: str, config: dict | None = None) -> dict:
    """What a sidecar file name says about the media it describes (``base`` may be cut)."""
    config = config or rules()
    text = _nfc(name)
    out = {'name': text, 'base': None, 'variant': 'not_sidecar_name', 'counter': None, 'cut': False}
    if not text.lower().endswith(config['sidecar_suffix']):
        return out
    stem = text[:-len(config['sidecar_suffix'])]
    match = _COUNTER.match(stem)
    if match:
        stem, out['counter'] = match.group(1), int(match.group(2))
    marker = config['supplemental_marker']
    at_limit = len(text) >= config['observed_max_sidecar_name_chars'] - 1
    if stem.endswith(marker):
        out.update(base=stem[:-len(marker)], variant='supplemental_metadata')
    else:
        dot = stem.rfind('.')
        tail = stem[dot:] if dot > 0 else ''
        head = stem[:dot] if dot > 0 else stem
        if len(tail) >= 3 and marker.startswith(tail) and (_media_ext(head, config) or at_limit):
            out.update(base=head, variant='supplemental_metadata_cut')
        else:
            out.update(base=stem, variant='json')
    if not _media_ext(out['base'], config):
        if at_limit:
            out['cut'] = True
        elif out['variant'] == 'json':
            out['variant'] = 'json_without_media_extension'
    elif at_limit and out['variant'] == 'json':
        out['cut'] = True  # the base may itself be cut although it ends like a media name
    return out


def match_candidates(sidecar: dict, title: str | None, media_names: list[str], config: dict | None = None) -> list[dict]:
    """Every media file the sidecar may describe, with the rule ids that propose it."""
    config = config or rules()
    norm: dict[str, list[str]] = defaultdict(list)
    for name in media_names:
        norm[_nfc(name)].append(name)
    found: dict[str, set] = defaultdict(set)

    def add(normalized: str, rule: str):
        for original in norm.get(normalized, ()):
            found[original].add(rule)
    base, counter, cut = sidecar.get('base'), sidecar.get('counter'), sidecar.get('cut')
    if base:
        if counter is None and not cut:
            add(base, 'sidecar_name_exact_v1')
        if counter is not None:
            stem, ext = _split(base)
            add(f'{stem}({counter}){ext}', 'duplicate_counter_v1')
        if sidecar.get('variant') == 'json_without_media_extension':
            for name in norm:
                if _split(name)[0] == base and _media_ext(name, config):
                    add(name, 'base_without_extension_v1')
        if cut and len(base) >= config['min_truncated_prefix_chars']:
            for name in norm:
                if name != base and name.startswith(base):
                    add(name, 'truncated_sidecar_prefix_v1')
            if counter is None:
                add(base, 'sidecar_name_exact_v1')
    title_norm = _nfc(title) if isinstance(title, str) and title.strip() else None
    if title_norm and counter is None:
        add(title_norm, 'title_exact_v1')
        t_stem, t_ext = _split(title_norm)
        for name in norm:
            stem, ext = _split(name)
            if (len(name) >= config['min_truncated_media_chars'] and name != title_norm
                    and ext.lower() == t_ext.lower() and len(stem) < len(t_stem) and t_stem.startswith(stem)):
                add(name, 'truncated_media_name_v1')
    described = set(found)
    roots = {_nfc(n) for n in described} | {n for n in (base, title_norm) if n and counter is None}
    for root in sorted(roots):
        stem, ext = _split(root)
        for suffix in config['edited_suffixes']:
            for original in norm.get(f'{stem}{suffix}{ext}', ()):
                if original not in described:
                    found[original].add('edited_variant_v1')
    out = []
    for name, rules_ in sorted(found.items()):
        relation = 'described_media' if name in described else 'edited_variant'
        strength = 'strong' if rules_ & STRONG_RULES else 'weak' if relation == 'described_media' else 'variant'
        out.append({'name': name, 'rules': sorted(rules_), 'relation': relation, 'strength': strength})
    return out


def _scope_sha(scope_kind: str, files: list[dict]) -> str:
    """Identity of the candidate scope: names, content and what is already acquired/catalogued.

    A later run over the same directory after more files were acquired is a new
    scope, so it appends a new match row instead of conflicting with the earlier one.
    """
    return canonical_sha256([scope_kind, sorted([f['name'], f.get('sha256'), f.get('artifact_id'),
                                                 f.get('catalog_entry_id')] for f in files)])


def record_match(db, sidecar_id: int, title: str | None, name_info: dict, scope_kind: str, files: list[dict],
                 config: dict) -> dict:
    """Append the match hypothesis for one sidecar over one candidate scope."""
    by_name = {f['name']: f for f in files}
    candidates = []
    for item in match_candidates(name_info, title, list(by_name), config):
        file = by_name[item['name']]
        candidates.append({**item, 'artifact_id': file.get('artifact_id'), 'catalog_entry_id': file.get('catalog_entry_id'),
                           'sha256': file.get('sha256')})
    strong = [c for c in candidates if c['strength'] == 'strong']
    weak = [c for c in candidates if c['strength'] == 'weak']
    deciding, decided_by = (strong, 'strong_rules') if strong else (weak, 'weak_rules') if weak else ([], None)
    status = 'no_candidate' if not deciding else 'single_candidate' if len(deciding) == 1 else 'ambiguous'
    for candidate in candidates:
        candidate['described'] = (status == 'single_candidate' and candidate in deciding)
        candidate['decided_by'] = decided_by
    match_id = append_row(db, 'trace_photo_matches',
                          {'sidecar_id': sidecar_id, 'scope_sha256': _scope_sha(scope_kind, files),
                           'rules_version': f'photos_takeout.v{config["version"]}'},
                          {'status': status, 'scope': scope_kind, 'candidates_json': dumps(candidates)})
    return {'match_id': match_id, 'status': status, 'candidates': candidates}


def _sidecar_times(data: dict, source: str, problems: list[str]) -> tuple[list[TimeCandidate], dict]:
    candidates, raw = [], {}
    for field, kind, semantics in _TIME_FIELDS:
        value = data.get(field)
        if value is None:
            continue
        if not isinstance(value, dict):
            problems.append(f'time_field_not_an_object:{field}')
            raw[field] = {'status': 'not_an_object'}
            continue
        formatted = value.get('formatted') if isinstance(value.get('formatted'), str) else None
        candidate, problem = epoch_time(kind, value.get('timestamp'), f'{source}:{field}', unit='s',
                                        metadata={'field': field, 'semantics': semantics,
                                                  'formatted_raw': clean_text(formatted),
                                                  'formatted_parsed': False})
        raw[field] = {'timestamp_raw': clean_text(str(value.get('timestamp'))) if value.get('timestamp') is not None else None,
                      'formatted_raw': clean_text(formatted), 'status': problem or 'parsed'}
        if problem:
            problems.append(f'{problem}:{field}')
        else:
            candidates.append(candidate)
    return candidates, raw


def is_sidecar(data, config: dict) -> bool:
    return isinstance(data, dict) and any(key in data for key in config['sidecar_keys_any'])


def observe_sidecar(artifact_id: int, *, enable_location: bool = False, coordinate_decimals: int | None = None,
                    scope_kind: str | None = None, scope_files: list[dict] | None = None,
                    limits: CommLimits = DEFAULT_LIMITS, issues: Issues | None = None, config: dict | None = None) -> dict:
    """Parse an acquired sidecar: event, sidecar row, both geotags and (with a scope) the match."""
    config = config or rules()
    issues = issues or Issues(limits.max_issues)
    with session() as db:
        row = db.execute('SELECT id, source_id, original_name, sha256 FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    if row is None:
        raise ValueError(f'artifact {artifact_id} does not exist')
    path = stored_path(artifact_id)
    if path.stat().st_size > MAX_SIDECAR_BYTES:
        issues.add('json_over_sidecar_size_limit', artifact_id=artifact_id, limit=MAX_SIDECAR_BYTES)
        return {'artifact_id': artifact_id, 'status': 'not_a_sidecar_size_limit'}
    data = read_json(path, limits, issues)
    if not is_sidecar(data, config):
        issues.add('json_not_a_photos_sidecar', artifact_id=artifact_id)
        return {'artifact_id': artifact_id, 'status': 'not_a_sidecar'}
    problems: list[str] = []
    name_info = analyse_name(row['original_name'] or '', config)
    title = data.get('title') if isinstance(data.get('title'), str) else None
    if 'title' in data and title is None:
        problems.append('title_not_a_string')
    candidates, times_raw = _sidecar_times(data, f'{PARSER_NAME}@{PARSER_VERSION}', problems)
    geo = {}
    positions = {}
    for field, kind in (('geoData', 'takeout_geoData'), ('geoDataExif', 'takeout_geoDataExif')):
        if field not in data:
            geo[field] = {'status': 'absent'}
            continue
        status, lat, lon, alt = geotags.sidecar_position(data[field])
        geo[field] = {'status': status}
        positions[field] = (status, lat, lon, alt)
        if status == 'invalid':
            problems.append(f'geo_invalid:{field}')
    both = [positions.get(f) for f in ('geoData', 'geoDataExif')]
    if all(p and p[0] == 'coordinates' for p in both):
        tolerance = config['geo_agreement_tolerance_deg']
        agree = abs(both[0][1] - both[1][1]) <= tolerance and abs(both[0][2] - both[1][2]) <= tolerance
        geo['comparison'] = 'agree' if agree else 'differ'
        if not agree:
            problems.append('geo_data_differs_from_geo_data_exif')
    elif any(p and p[0] == 'coordinates' for p in both):
        geo['comparison'] = 'one_side_only'
    else:
        geo['comparison'] = 'not_comparable'
    geo['tolerance_deg'] = config['geo_agreement_tolerance_deg']
    people = data.get('people') if isinstance(data.get('people'), list) else []
    people_raw, _ = bounded(people, limits)
    raw_copy = {k: v for k, v in data.items() if k not in ('geoData', 'geoDataExif', 'people', 'description')}
    raw_bounded, truncated = bounded(raw_copy, limits)
    description = data.get('description') if isinstance(data.get('description'), str) else None
    url = data.get('url') if isinstance(data.get('url'), str) else None
    metadata = {
        'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION, 'rules_version': config['version']},
        'locator': {'artifact_id': artifact_id, 'sidecar_name': name_info['name']},
        'photo': {'source': 'google_photos_sidecar', 'title': clean_text(title), 'name_variant': name_info['variant'],
                  'name_cut_at_limit': name_info['cut'], 'counter': name_info['counter'],
                  'description_present': bool(description), 'people_count': len(people),
                  'geo_status': {k: v['status'] for k, v in geo.items() if isinstance(v, dict)},
                  'geo_comparison': geo['comparison']},
        'signal': signal('active', 'raw_sample', 'google_photos', 'account',
                         note='capture or upload recorded by the account; not proof of who took the photo or where'),
        'semantics': 'Google Photos item metadata as exported; times and places are claims of the service',
        'privacy': {'people_names': 'kept raw in trace_photo_sidecars only; not indexed for search',
                    'coordinates': 'only in trace_geotags under enable_location'},
        'issues': problems,
    }
    taken = next((c for c in candidates if c.metadata.get('field') == 'photoTakenTime'), None)
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        event_id = write_event(db, candidates, source_id=row['source_id'], artifact_id=artifact_id,
                               event_type='photo_metadata', ts_start=legacy_instant(taken),
                               subject=clean_text(title) or row['original_name'],
                               external_id=f'{PARSER_NAME}@{PARSER_VERSION}', confidence=None, metadata=metadata)
        sidecar_id = append_row(db, 'trace_photo_sidecars',
                                {'artifact_id': artifact_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION},
                                {'event_id': event_id, 'sidecar_name': name_info['name'], 'name_variant': name_info['variant'],
                                 'title': clean_text(title), 'description': clean_text(description),
                                 'people_json': dumps(people_raw), 'url': clean_text(url), 'times_json': dumps(times_raw),
                                 'geo_json': dumps(geo), 'raw_json': dumps({'fields': raw_bounded, 'truncated_values': truncated}),
                                 'issues_json': dumps(problems)})
        stored_geo = []
        for field, kind in (('geoData', 'takeout_geoData'), ('geoDataExif', 'takeout_geoDataExif')):
            if field not in positions:
                continue
            status, lat, lon, alt = positions[field]
            extra = {}
            if geo['comparison'] in ('agree', 'differ') and field == 'geoData':
                extra['distance_to_geoDataExif_m'] = round(geotags.haversine_m(both[0][1:3], both[1][1:3]), 2)
            raw_geo, _ = bounded(data[field], limits)
            stored_geo.append(geotags.store(
                db, artifact_id=artifact_id, source_kind=kind, status=status, lat=lat, lon=lon, alt=alt,
                parser=PARSER_NAME, parser_version=PARSER_VERSION, enable_location=enable_location,
                coordinate_decimals=coordinate_decimals, sidecar_id=sidecar_id, event_id=event_id,
                time_utc=taken.utc if taken else None, time_raw=taken.raw if taken else None,
                time_basis='google_photos_photoTakenTime' if taken else None,
                raw={'value': raw_geo, **extra}, issues=[p for p in problems if field in p]))
        match = None
        if scope_kind is not None:
            match = record_match(db, sidecar_id, title, name_info, scope_kind, scope_files or [], config)
    for problem in problems:
        issues.add(problem, artifact_id=artifact_id)
    return {'artifact_id': artifact_id, 'status': 'sidecar_observed', 'event_id': event_id, 'sidecar_id': sidecar_id,
            'geotag_ids': stored_geo, 'time_candidates': len(candidates), 'name': name_info,
            'match': {k: match[k] for k in ('status', 'candidates')} if match else None, 'problems': problems}


def observe_media(artifact_id: int, *, enable_location: bool = False, coordinate_decimals: int | None = None,
                  limits: CommLimits = DEFAULT_LIMITS, issues: Issues | None = None, config: dict | None = None) -> dict:
    """EXIF time claims (``exif_minimal`` v1) plus the GPS IFD (``exif_gps`` v1) of one acquired image."""
    config = config or rules()
    issues = issues or Issues(limits.max_issues)
    with session() as db:
        row = db.execute('SELECT id, source_id, original_name, sha256 FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
    if row is None:
        raise ValueError(f'artifact {artifact_id} does not exist')
    suffix = _split(row['original_name'] or '')[1].lower()
    if suffix not in config['exif_readable_extensions']:
        issues.add('exif_container_not_supported', artifact_id=artifact_id, extension=suffix)
        return {'artifact_id': artifact_id, 'status': 'exif_container_not_supported'}
    result = exif.observe_artifact(artifact_id, limits=limits, issues=issues)
    values, problems, container = exif.read_exif(stored_path(artifact_id), limits, gps=True)
    gps = (values or {}).get('gps')
    if not gps:
        return {**result, 'gps': 'absent' if values else 'no_exif'}
    status, lat, lon, alt, accuracy, position_issues = geotags.exif_position(gps)
    source = f'{exif.GPS_PARSER_NAME}@{exif.GPS_PARSER_VERSION}:GPSDateStamp+GPSTimeStamp'
    candidate, time_issues = geotags.exif_gps_time(gps, source)
    gps_problems = [p for p in problems if p.startswith('gps_')] + position_issues + time_issues
    raw, _ = bounded(gps, limits)
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        event_id = result.get('event_id')
        if candidate is not None:
            if event_id is None:
                event_id = write_event(
                    db, [candidate], source_id=row['source_id'], artifact_id=artifact_id, event_type='photo_metadata',
                    ts_start=legacy_instant(candidate), subject=row['original_name'],
                    external_id=f'{exif.GPS_PARSER_NAME}@{exif.GPS_PARSER_VERSION}', confidence=None,
                    metadata={'parser': {'name': exif.GPS_PARSER_NAME, 'version': exif.GPS_PARSER_VERSION},
                              'locator': {'artifact_id': artifact_id, 'container': container},
                              'photo': {'source': 'exif_gps', 'geotag_status': status},
                              'signal': signal('passive', 'raw_sample', 'camera_gps_receiver', 'device'),
                              'semantics': 'GPS receiver time embedded in the file; editable metadata, not capture truth',
                              'issues': gps_problems})
            else:
                from ..timeinfo import record_event_times
                record_event_times(db, event_id, [candidate])
        geotag_id = geotags.store(
            db, artifact_id=artifact_id, source_kind='exif_gps', status=status, lat=lat, lon=lon, alt=alt,
            parser=exif.GPS_PARSER_NAME, parser_version=exif.GPS_PARSER_VERSION, enable_location=enable_location,
            coordinate_decimals=coordinate_decimals, media_sha256=row['sha256'], event_id=event_id,
            time_utc=candidate.utc if candidate else None, time_raw=candidate.raw if candidate else None,
            time_basis='exif_gps_receiver_utc' if candidate else None, raw=raw, accuracy_m=accuracy,
            issues=gps_problems)
    for problem in gps_problems:
        issues.add(problem, artifact_id=artifact_id)
    return {**result, 'gps': status, 'geotag_id': geotag_id, 'gps_time': candidate.utc if candidate else None,
            'gps_event_id': event_id}


def _opt_in(enable_location: bool, coordinate_decimals: int | None) -> dict:
    if coordinate_decimals is not None and not 0 <= int(coordinate_decimals) <= 8:
        raise ValueError('coordinate_decimals must be between 0 and 8 (or omitted for source precision)')
    return {'enable_location': bool(enable_location), 'coordinate_decimals': coordinate_decimals,
            'processing': 'local_only', 'reverse_geocoding': False}


def import_sidecar(path: Path, *, enable_location: bool = False, coordinate_decimals: int | None = None,
                   source_locator: str | None = None, original_name: str | None = None, collection_id: int | None = None,
                   label: str | None = None, scope_kind: str | None = None, scope_files: list[dict] | None = None,
                   limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """One sidecar file (upload, import plan); matching only over an explicit scope."""
    opt_in = _opt_in(enable_location, coordinate_decimals)
    config = rules()
    with path.open('rb') as handle:
        head = handle.read(MAX_SIDECAR_BYTES + 1)
    if len(head) > MAX_SIDECAR_BYTES:
        raise ValueError(f'a Google Photos sidecar is at most {MAX_SIDECAR_BYTES} bytes here')
    try:
        probe = json.loads(head.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'sidecar JSON is not valid: {exc}') from exc
    if not is_sidecar(probe, config):
        raise ValueError('JSON object without photoTakenTime/creationTime/geoData keys is not a Photos sidecar')
    issues = Issues(limits.max_issues)
    _, artifact_id = acquire(path, source_kind='google_photos_takeout', source_label=label or 'Google Photos Takeout',
                             locator=source_locator if source_locator is not None else str(path), importer=PARSER_NAME,
                             parser_version=PARSER_VERSION, original_name=original_name, options=opt_in,
                             collection_id=collection_id, mime_type='application/json')
    result = observe_sidecar(artifact_id, enable_location=enable_location, coordinate_decimals=coordinate_decimals,
                             scope_kind=scope_kind, scope_files=scope_files, limits=limits, issues=issues, config=config)
    counts = {'sidecars': 1 if result['status'] == 'sidecar_observed' else 0,
              'geotags_stored': _stored(result.get('geotag_ids') or [])}
    coverage = {'complete': True}
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=opt_in,
                      counts=counts, coverage=coverage, issues=issues)
    return {**result, 'parser_version': PARSER_VERSION, **counts, 'options': opt_in, 'coverage': coverage,
            'issues': issues.as_dict()}


def _stored(geotag_ids: list[int]) -> int:
    if not geotag_ids:
        return 0
    with session() as db:
        marks = ','.join('?' * len(geotag_ids))
        return db.execute(f'SELECT COUNT(*) FROM trace_geotags WHERE id IN ({marks}) AND coordinates_stored=1',
                          geotag_ids).fetchone()[0]


def import_photo(path: Path, *, enable_location: bool = False, coordinate_decimals: int | None = None,
                 source_locator: str | None = None, original_name: str | None = None, collection_id: int | None = None,
                 label: str | None = None, limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """One image file: EXIF times and the GPS geotag."""
    opt_in = _opt_in(enable_location, coordinate_decimals)
    issues = Issues(limits.max_issues)
    _, artifact_id = acquire(path, source_kind='image_files', source_label=label or 'Image files',
                             locator=source_locator if source_locator is not None else str(path), importer=MEDIA_IMPORTER,
                             parser_version=exif.GPS_PARSER_VERSION, original_name=original_name, options=opt_in,
                             collection_id=collection_id)
    result = observe_media(artifact_id, enable_location=enable_location, coordinate_decimals=coordinate_decimals,
                           limits=limits, issues=issues)
    counts = {'images': 1, 'geotags_stored': _stored([result['geotag_id']] if result.get('geotag_id') else [])}
    coverage = {'complete': True}
    record_import_run(artifact_id=artifact_id, importer=MEDIA_IMPORTER, parser_version=exif.GPS_PARSER_VERSION,
                      options=opt_in, counts=counts, coverage=coverage, issues=issues)
    return {**result, **counts, 'options': opt_in, 'coverage': coverage, 'issues': issues.as_dict()}


def import_photos_takeout(root: Path, *, enable_location: bool = False, coordinate_decimals: int | None = None,
                          label: str | None = None, collection_id: int | None = None,
                          source_locator: str | None = None, limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """A Google Photos Takeout folder (or any photo folder): sidecars, media, EXIF GPS, matches."""
    opt_in = _opt_in(enable_location, coordinate_decimals)
    config = rules()
    root = Path(root)
    if root.is_file():
        is_json = root.suffix.lower() == '.json'
        call = import_sidecar if is_json else import_photo
        return call(root, enable_location=enable_location, coordinate_decimals=coordinate_decimals,
                    source_locator=source_locator, collection_id=collection_id, label=label, limits=limits)
    issues = Issues(limits.max_issues)
    files = walk_files(root, limits, issues)
    complete = 'walk_entry_limit' not in issues.counts
    by_dir: dict[Path, list[Path]] = defaultdict(list)
    for path in files:
        by_dir[path.parent].append(path)
    counts = Counter()
    label = label or 'Google Photos Takeout'

    def locator(path: Path) -> str:
        if source_locator is None:
            return str(path)
        return f'{source_locator}/{path.relative_to(root).as_posix()}'
    match_status = Counter()
    touched: list[int] = []
    for directory in sorted(by_dir):
        items = sorted(by_dir[directory])
        media = [p for p in items if _media_ext(p.name, config)]
        sidecars = [p for p in items if p.name.lower().endswith(config['sidecar_suffix'])]
        scope = []
        for path in media:
            if counts['records'] >= limits.max_records:
                issues.add('record_limit', limit=limits.max_records)
                complete = False
                break
            counts['records'] += 1
            _, artifact_id = acquire(path, source_kind='google_photos_takeout', source_label=label,
                                     locator=locator(path), importer=MEDIA_IMPORTER,
                                     parser_version=exif.GPS_PARSER_VERSION, options=opt_in, collection_id=collection_id)
            with session() as db:
                sha = db.execute('SELECT sha256 FROM artifacts WHERE id=?', (artifact_id,)).fetchone()['sha256']
            outcome = observe_media(artifact_id, enable_location=enable_location,
                                    coordinate_decimals=coordinate_decimals, limits=limits, issues=issues, config=config)
            counts['media'] += 1
            counts[f'media_{outcome.get("status")}'] += 1
            if outcome.get('gps') not in (None, 'absent', 'no_exif'):
                counts[f'exif_gps_{outcome["gps"]}'] += 1
            scope.append({'name': path.name, 'artifact_id': artifact_id, 'sha256': sha})
            touched.append(artifact_id)
        described: set[str] = set()
        for path in sidecars:
            if counts['records'] >= limits.max_records:
                issues.add('record_limit', limit=limits.max_records)
                complete = False
                break
            if path.stat().st_size > MAX_SIDECAR_BYTES:
                issues.add('json_over_sidecar_size_limit', path=path.name, limit=MAX_SIDECAR_BYTES)
                counts['json_skipped'] += 1
                continue
            try:
                probe = json.loads(path.read_bytes().decode('utf-8-sig'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                issues.add('json_unreadable', path=path.name)
                counts['json_skipped'] += 1
                continue
            if not is_sidecar(probe, config):
                counts['json_not_sidecar'] += 1  # album metadata.json, print orders, ...
                continue
            counts['records'] += 1
            _, artifact_id = acquire(path, source_kind='google_photos_takeout', source_label=label,
                                     locator=locator(path), importer=PARSER_NAME, parser_version=PARSER_VERSION,
                                     options=opt_in, collection_id=collection_id, mime_type='application/json')
            outcome = observe_sidecar(artifact_id, enable_location=enable_location,
                                      coordinate_decimals=coordinate_decimals, scope_kind='directory',
                                      scope_files=scope, limits=limits, issues=issues, config=config)
            counts['sidecars'] += 1
            touched.append(artifact_id)
            if outcome.get('match'):
                match_status[outcome['match']['status']] += 1
                described.update(c['name'] for c in outcome['match']['candidates'])
        for item in scope:
            if item['name'] not in described:
                counts['media_without_sidecar'] += 1
                issues.add('media_without_sidecar', name=item['name'])
    counts['geotags_stored'] = _stored_for(touched)
    counts.update({f'match_{k}': v for k, v in match_status.items()})
    counts.pop('records', None)
    coverage = {'complete': complete}
    record_import_run(artifact_id=None, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=opt_in,
                      counts=dict(counts), coverage=coverage, issues=issues)
    return {'parser_version': PARSER_VERSION, **dict(sorted(counts.items())), 'options': opt_in,
            'coverage': coverage, 'issues': issues.as_dict()}


def _stored_for(artifact_ids: list[int]) -> int:
    total = 0
    with session() as db:
        for start in range(0, len(artifact_ids), 500):
            chunk = artifact_ids[start:start + 500]
            marks = ','.join('?' * len(chunk))
            total += db.execute(f'SELECT COUNT(*) FROM trace_geotags WHERE coordinates_stored=1 AND artifact_id IN ({marks})',
                                chunk).fetchone()[0]
    return total


def catalog_scope(entry_id: int) -> list[dict]:
    """Sibling media of a catalogued sidecar (same scan, container and directory) for plan matching."""
    from ..importplan.detect import split_member
    config = rules()
    with session() as db:
        entry = db.execute('SELECT scan_id, parent_entry_id, locator FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
        if entry is None:
            return []
        container, path = split_member(entry['locator'])
        directory = path.rsplit('/', 1)[0] if '/' in path else ''
        siblings = db.execute('''SELECT id, name, locator, sha256, status FROM catalog_entries
                                 WHERE scan_id=? AND parent_entry_id IS ? ORDER BY id''',
                              (entry['scan_id'], entry['parent_entry_id'])).fetchall()
        out = []
        for sibling in siblings:
            s_container, s_path = split_member(sibling['locator'])
            s_dir = s_path.rsplit('/', 1)[0] if '/' in s_path else ''
            if s_container != container or s_dir != directory or not _media_ext(sibling['name'] or '', config):
                continue
            artifact = db.execute('SELECT artifact_id FROM catalog_entry_artifacts WHERE entry_id=? ORDER BY id LIMIT 1',
                                  (sibling['id'],)).fetchone()
            out.append({'name': sibling['name'], 'catalog_entry_id': sibling['id'], 'sha256': sibling['sha256'],
                        'artifact_id': artifact['artifact_id'] if artifact else None})
    return out
