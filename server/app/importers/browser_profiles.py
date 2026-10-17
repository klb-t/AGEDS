"""Local browser profile history (Chromium/Edge ``History``, Firefox ``places.sqlite``) — opt-in.

Refuses to run (and acquires nothing) without ``enable_browser_history``; the
opt-in is recorded on every visit row, session event and run.

Reading never touches the live profile beyond copying its bytes: the database
(and a ``-wal`` file next to it, acquired as a child artifact; a ``-journal`` is
reported) is acquired into the store; parsing runs on a **private temporary
copy** of the stored bytes. Without a WAL the copy is opened
``mode=ro&immutable=1`` (``reading_key='immutable'``); with a WAL the copy pair is
opened ``mode=ro`` so committed WAL frames are visible
(``reading_key='wal:<sha256 prefix>'``) — the store copy itself is never opened by
SQLite. A profile copied while the browser runs may still be inconsistent (no
snapshot or lock of the original); close the browser first for a clean copy.

* every visit → one ``trace_browser_visits`` row (URL/title bounded, raw time,
  UTC, transition code, source visit id, referrer visit);
* times: Chromium WebKit epoch (µs since 1601-01-01 UTC), Firefox PRTime (µs
  since 1970); ``0`` is a placeholder, values outside 1800–2200 are
  ``time_out_of_range`` — the unit is never guessed;
* the timeline gets aggregated ``browser_session`` events (rule
  ``browser_session_gap_v1``: consecutive navigation visits with gaps ≤
  ``session_gap_minutes``, default 30; subframe/embed visits are counted but do
  not form sessions), with start/end candidates and the top domains.

A visit record is a signal that a page was loaded in a profile on a device, not
proof of reading, attention or of who used the device.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ..db import session
from ..timeinfo import TimeCandidate, format_utc
from ..traces.common import OptInRequired, append_row, clean_text, dumps, mapping, signal, write_event
from .comm_common import DEFAULT_LIMITS, CommLimits, Issues, acquire, record_import_run, stored_path

PARSER_NAME = 'browser_profile'
PARSER_VERSION = '1'
MAPPING = 'browser_profiles.v1.json'
_WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PROFILE_FILES = ('History', 'places.sqlite')


class BrowserHistoryOptInRequired(OptInRequired):
    pass


def visit_instant(raw, epoch: str) -> tuple[str | None, str]:
    if raw is None:
        return None, 'time_missing'
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None, 'time_not_an_integer'
    if raw == 0:
        return None, 'time_zero_placeholder'
    base = _WEBKIT_EPOCH if epoch == 'webkit_1601_us' else _UNIX_EPOCH
    try:
        moment = base + timedelta(microseconds=raw)
    except OverflowError:
        return None, 'time_out_of_range'
    if not 1800 <= moment.year <= 2200:
        return None, 'time_out_of_range'
    return format_utc(moment), 'ok'


def _tables(connection) -> set[str]:
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(connection, table: str) -> set[str]:
    return {r[1] for r in connection.execute(f'PRAGMA table_info({table})')}


def _open_copy(db_path: Path, wal_path: Path | None, directory: Path) -> tuple[sqlite3.Connection, str]:
    copy = directory / 'profile.sqlite'
    shutil.copyfile(db_path, copy)
    if wal_path is not None:
        shutil.copyfile(wal_path, directory / 'profile.sqlite-wal')
        connection = sqlite3.connect(f'file:{copy}?mode=ro', uri=True)
        reading = 'wal'
    else:
        connection = sqlite3.connect(f'file:{copy}?mode=ro&immutable=1', uri=True)
        reading = 'immutable'
    connection.row_factory = sqlite3.Row
    return connection, reading


def _rows(connection, browser: str, config: dict, limit: int):
    if browser == 'chromium':
        columns = _columns(connection, 'visits')
        duration = 'v.visit_duration' if 'visit_duration' in columns else 'NULL'
        sql = (f'SELECT v.id AS visit_id, v.visit_time AS time_raw, v.from_visit AS from_visit, v.transition AS transition, '
               f'{duration} AS duration_us, u.url AS url, u.title AS title FROM visits v LEFT JOIN urls u ON u.id=v.url '
               f'ORDER BY v.id LIMIT ?')
    else:
        sql = ('SELECT h.id AS visit_id, h.visit_date AS time_raw, h.from_visit AS from_visit, h.visit_type AS transition, '
               'NULL AS duration_us, p.url AS url, p.title AS title FROM moz_historyvisits h '
               'LEFT JOIN moz_places p ON p.id=h.place_id ORDER BY h.id LIMIT ?')
    return connection.execute(sql, (limit + 1,)).fetchall()


def detect(path: Path) -> str | None:
    with path.open('rb') as handle:
        if handle.read(16) != b'SQLite format 3\x00':
            return None
    config, _ = mapping(MAPPING)
    with tempfile.TemporaryDirectory(prefix='ageds-browser-probe-') as directory:
        connection, _ = _open_copy(path, None, Path(directory))
        try:
            tables = _tables(connection)
        except sqlite3.DatabaseError:
            return None
        finally:
            connection.close()
    for browser in ('chromium', 'firefox'):
        if set(config[browser]['required_tables']) <= tables:
            return browser
    return None


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def import_profile(path: Path, *, enable_browser_history: bool = False, session_gap_minutes: int | None = None,
                   source_locator: str | None = None, original_name: str | None = None,
                   collection_id: int | None = None, label: str | None = None,
                   limits: CommLimits = DEFAULT_LIMITS) -> dict:
    """A ``History``/``places.sqlite`` file, or a profile folder holding one."""
    if not enable_browser_history:
        raise BrowserHistoryOptInRequired('local browser history import is disabled; pass enable_browser_history=True '
                                          '(CLI --enable-browser-history) to opt in')
    config, mapping_sha = mapping(MAPPING)
    gap_minutes = session_gap_minutes or config['session_rule']['default_gap_minutes']
    if not 1 <= int(gap_minutes) <= 1440:
        raise ValueError('session_gap_minutes must be between 1 and 1440')
    path = Path(path)
    if path.is_dir():
        found = [path / name for name in PROFILE_FILES if (path / name).is_file() and not (path / name).is_symlink()]
        if len(found) != 1:
            raise ValueError(f'expected exactly one of {PROFILE_FILES} in the profile folder, found {len(found)}')
        relative = found[0].name
        source_locator = f'{source_locator}/{relative}' if source_locator else None
        path = found[0]
    browser = detect(path)
    if browser is None:
        raise ValueError('not a Chromium History or Firefox places.sqlite database (SQLite header and tables)')
    options = {'enable_browser_history': True, 'processing': 'local_only', 'session_gap_minutes': int(gap_minutes),
               'reading': 'private temporary copy of the stored bytes'}
    issues = Issues(limits.max_issues)
    locator = source_locator if source_locator is not None else str(path)
    source_id, artifact_id = acquire(path, source_kind='browser_profile', source_label=label or 'Browser profile',
                                     locator=locator, importer=PARSER_NAME, parser_version=PARSER_VERSION,
                                     original_name=original_name, options=options, collection_id=collection_id,
                                     mime_type='application/vnd.sqlite3')
    wal_artifact = None
    wal = path.with_name(path.name + '-wal')
    if wal.is_file() and not wal.is_symlink() and wal.stat().st_size > 0:
        _, wal_artifact = acquire(wal, source_kind='browser_profile', source_label=label or 'Browser profile',
                                  locator=locator + '-wal', importer=PARSER_NAME, parser_version=PARSER_VERSION,
                                  options=options, collection_id=collection_id, parent_artifact_id=artifact_id,
                                  mime_type='application/octet-stream')
    if path.with_name(path.name + '-journal').exists():
        issues.add('rollback_journal_present_not_applied')
    epoch = 'webkit_1601_us' if browser == 'chromium' else 'prtime_1970_us'
    with tempfile.TemporaryDirectory(prefix='ageds-browser-') as directory:
        connection, reading = _open_copy(stored_path(artifact_id), stored_path(wal_artifact) if wal_artifact else None,
                                         Path(directory))
        try:
            rows = _rows(connection, browser, config, limits.max_records)
        except sqlite3.DatabaseError as exc:
            connection.close()
            raise ValueError(f'browser history tables not readable: {exc}') from exc
        connection.close()
    if reading == 'wal':
        with session() as db:
            sha = db.execute('SELECT sha256 FROM artifacts WHERE id=?', (wal_artifact,)).fetchone()['sha256']
        reading_key = f'wal:{sha[:16]}'
    else:
        reading_key = 'immutable'
    complete = len(rows) <= limits.max_records
    if not complete:
        issues.add('record_limit', limit=limits.max_records)
        rows = rows[:limits.max_records]
    names = config[browser]['transition_names']
    if browser == 'chromium':
        non_navigation = set(config['chromium']['non_navigation_core_types'])
        core = lambda t: (t & config['chromium']['core_transition_mask']) if isinstance(t, int) else None  # noqa: E731
    else:
        non_navigation = set(config['firefox']['non_navigation_types'])
        core = lambda t: t if isinstance(t, int) else None  # noqa: E731
    counts = Counter()
    navigation = []
    opt_in_json = dumps({'enable_browser_history': True, 'processing': 'local_only'})
    with session() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in rows:
            utc, status = visit_instant(row['time_raw'], epoch)
            if status != 'ok':
                counts[status] += 1
                issues.add(status, visit_id=row['visit_id'])
            url = row['url'] if isinstance(row['url'], str) else None
            title = row['title'] if isinstance(row['title'], str) else None
            truncated = bool(url and len(url) > config['max_url_chars'])
            append_row(db, 'trace_browser_visits',
                       {'artifact_id': artifact_id, 'parser': PARSER_NAME, 'parser_version': PARSER_VERSION,
                        'reading_key': reading_key, 'visit_id': row['visit_id']},
                       {'browser': browser, 'url': clean_text(url[:config['max_url_chars']]) if url else None,
                        'url_truncated': int(truncated),
                        'title': clean_text(title[:config['max_title_chars']]) if title else None,
                        'visit_time_raw': row['time_raw'] if isinstance(row['time_raw'], int) else None,
                        'visit_utc': utc, 'epoch': epoch, 'time_status': status,
                        'transition_raw': row['transition'] if isinstance(row['transition'], int) else None,
                        'from_visit': row['from_visit'] if isinstance(row['from_visit'], int) else None,
                        'duration_us': row['duration_us'] if isinstance(row['duration_us'], int) else None,
                        'opt_in_json': opt_in_json})
            counts['visits'] += 1
            kind = core(row['transition'])
            counts[f'transition_{names.get(str(kind), "unknown")}'] += 1
            if utc and int(utc[:4]) < config[browser]['service_start_year']:
                counts['time_before_service_start'] += 1
                issues.add('time_before_service_start', visit_id=row['visit_id'])
            if utc and kind not in non_navigation:
                navigation.append((utc, row['visit_id'], _domain(url), row['time_raw']))
        sessions = _sessions(navigation, int(gap_minutes) * 60)
        rule = {'id': config['session_rule']['id'], 'gap_seconds': int(gap_minutes) * 60,
                'excluded_transition_types': sorted(non_navigation), 'mapping_sha256': mapping_sha}
        for index, item in enumerate(sessions):
            start = TimeCandidate('start', str(item['first_raw']), f'{PARSER_NAME}@{PARSER_VERSION}:session_first_visit',
                                  utc=item['start'], tz_status='utc_epoch', tz='UTC', precision='microsecond',
                                  metadata={'projection': rule['id'], 'visit_id': item['first_id']})
            end = TimeCandidate('end', str(item['last_raw']), f'{PARSER_NAME}@{PARSER_VERSION}:session_last_visit',
                                utc=item['end'], tz_status='utc_epoch', tz='UTC', precision='microsecond',
                                metadata={'projection': rule['id'], 'visit_id': item['last_id']})
            domains = Counter(d for d in item['domains'] if d)
            write_event(db, [start, end], source_id=source_id, artifact_id=artifact_id, event_type='browser_session',
                        ts_start=item['start'], ts_end=item['end'], contact_label=browser,
                        subject=f'{browser} session ({item["visits"]} visits)',
                        external_id=f'{PARSER_NAME}@{PARSER_VERSION}:{reading_key}:gap{rule["gap_seconds"]}:{item["first_id"]}',
                        confidence=None,
                        metadata={'parser': {'name': PARSER_NAME, 'version': PARSER_VERSION},
                                  'locator': {'artifact_id': artifact_id, 'reading_key': reading_key,
                                              'first_visit_id': item['first_id'], 'last_visit_id': item['last_id']},
                                  'session': {'visits': item['visits'], 'rule': rule,
                                              'top_domains': domains.most_common(config['session_rule']['top_domains'])},
                                  'signal': signal('active', 'derived', browser, 'device'),
                                  'opt_in': {'enable_browser_history': True},
                                  'semantics': 'aggregated visit records of a local profile; not proof of attention'})
            counts['sessions'] += 1
    coverage = {'complete': complete}
    result_counts = dict(sorted(counts.items()))
    record_import_run(artifact_id=artifact_id, importer=PARSER_NAME, parser_version=PARSER_VERSION, options=options,
                      counts=result_counts, coverage=coverage, issues=issues)
    return {'artifact_id': artifact_id, 'wal_artifact_id': wal_artifact, 'browser': browser, 'reading_key': reading_key,
            'parser_version': PARSER_VERSION, **result_counts, 'options': options, 'coverage': coverage,
            'issues': issues.as_dict()}


def _sessions(visits: list[tuple[str, int, str | None]], gap_seconds: int) -> list[dict]:
    out = []
    for utc, visit_id, domain, raw in sorted(visits):
        moment = datetime.fromisoformat(utc.replace('Z', '+00:00'))
        if out and (moment - out[-1]['_last']).total_seconds() <= gap_seconds:
            current = out[-1]
            current.update(end=utc, last_id=visit_id, last_raw=raw, _last=moment)
            current['visits'] += 1
            current['domains'].append(domain)
        else:
            out.append({'start': utc, 'end': utc, 'first_id': visit_id, 'last_id': visit_id, 'first_raw': raw,
                        'last_raw': raw, 'visits': 1, 'domains': [domain], '_last': moment})
    return out
