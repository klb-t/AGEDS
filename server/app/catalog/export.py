"""Catalog export to CSV, XLSX or self-contained HTML.

Exports never overwrite: the file is written next to the target and published
with an exclusive hard link. The target must not be inside any scanned source
root of the case (or a configured scan root). Source-derived text is data: XLSX
cells are forced to the string type (no formulas), HTML is fully escaped and has
no script. C0/C1 control characters in text are shown as ``\\xNN``; locators and
``name_raw_hex`` keep the exact bytes.
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .. import db as dbmod
from . import signatures
from .coverage import coverage_report
from .scan import CATALOG_SCHEMA, CATALOG_VERSION
from .store import TYPE_SQL, current_scan_ids, list_collisions, list_groups

FORMATS = ('csv', 'xlsx', 'html')
COLUMNS = (
    'entry_id', 'scan_id', 'collection_id', 'collection_label', 'locator', 'depth', 'origin', 'parent_entry_id',
    'name', 'name_encoding', 'name_raw_hex', 'name_flags', 'extension', 'extension_type', 'sniffed_type',
    'effective_type', 'sniff_basis', 'type_relation', 'type_conflict', 'family', 'size_bytes', 'compressed_size',
    'sha256', 'hash_status', 'content_group_id', 'status', 'issue_code', 'issues', 'filesystem_mtime_utc',
    'archive_time_source', 'archive_time_raw', 'archive_time_utc', 'archive_time_local', 'archive_time_tz_status',
    'times_json', 'mark_priority', 'mark_note',
)
_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f]')


def _clean(value):
    if isinstance(value, str):
        return _CONTROL.sub(lambda m: f'\\x{ord(m.group()):02x}', value)
    return value


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _outside_sources(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve(strict=False)
    roots = [Path(p).resolve(strict=False) for p in dbmod.settings.scan_roots]
    with closing(dbmod.connect()) as db:
        roots += [Path(r[0]) for r in db.execute('SELECT DISTINCT root_path FROM catalog_scans')]
    for root in roots:
        if _inside(resolved, root):
            raise ValueError('output must be outside every scanned source root')
    return resolved


def _check_output(output: Path) -> Path:
    output = Path(output).expanduser()
    if output.is_symlink() or output.exists():
        raise FileExistsError(f'Output already exists: {output}; choose a new filename')
    return _outside_sources(output)


def _check_output_dir(directory: Path) -> Path:
    directory = Path(directory).expanduser()
    if directory.is_symlink():
        raise ValueError('output directory must not be a symlink')
    resolved = _outside_sources(directory)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _publish(write, output: Path, suffix: str) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.ageds-catalog-', suffix=suffix, dir=output.parent)
    os.close(fd)
    try:
        write(Path(temporary))
        with open(temporary, 'rb') as handle:
            os.fsync(handle.fileno())
        os.link(temporary, output)  # exclusive: fails if the target appeared meanwhile
        return output.stat().st_size
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _scan_ids(db, collection_id: int | None, scan_ids: list[int] | None) -> list[int]:
    if scan_ids:
        return [int(s) for s in scan_ids]
    return current_scan_ids(db, collection_id)


def iter_rows(*, collection_id: int | None = None, scan_ids: list[int] | None = None, max_rows: int = 1_000_000,
              batch: int = 2000):
    """Yield export rows (dicts with COLUMNS) in entry-id order; bounded by max_rows."""
    with closing(dbmod.connect()) as db:
        scans = _scan_ids(db, collection_id, scan_ids)
        if not scans:
            return
        marks = ','.join('?' * len(scans))
        after, produced = 0, 0
        while produced < max_rows:
            take = min(batch, max_rows - produced)
            rows = db.execute(f'''SELECT e.*, {TYPE_SQL} AS effective_type, c.label AS collection_label
                    FROM catalog_entries e JOIN collections c ON c.id=e.collection_id
                    WHERE e.scan_id IN ({marks}) AND e.id > ? {"AND e.collection_id=?" if collection_id is not None else ""}
                    ORDER BY e.id LIMIT ?''', (*scans, after, *([collection_id] if collection_id is not None else []), take)).fetchall()
            if not rows:
                return
            ids = [r['id'] for r in rows]
            times: dict[int, list] = {}
            for t in db.execute(f'SELECT * FROM catalog_entry_times WHERE entry_id IN ({",".join("?" * len(ids))}) ORDER BY id', ids):
                times.setdefault(t['entry_id'], []).append(t)
            for row in rows:
                entry_times = times.get(row['id'], [])
                fs = next((t for t in entry_times if t['source'] == 'filesystem_stat'), None)
                archive = next((t for t in entry_times if t['source'] != 'filesystem_stat'), None)
                mark = db.execute('SELECT priority, note FROM catalog_marks WHERE collection_id=? AND locator=? ORDER BY id DESC LIMIT 1',
                                  (row['collection_id'], row['locator'])).fetchone()
                yield {
                    'entry_id': row['id'], 'scan_id': row['scan_id'], 'collection_id': row['collection_id'],
                    'collection_label': row['collection_label'], 'locator': row['locator'], 'depth': row['depth'],
                    'origin': row['origin'], 'parent_entry_id': row['parent_entry_id'], 'name': row['name'],
                    'name_encoding': row['name_encoding'], 'name_raw_hex': row['name_raw_hex'],
                    'name_flags': ' '.join(json.loads(row['name_flags_json'])), 'extension': row['extension'],
                    'extension_type': row['extension_type'], 'sniffed_type': row['sniffed_type'],
                    'effective_type': row['effective_type'], 'sniff_basis': row['sniff_basis'],
                    'type_relation': row['type_relation'], 'type_conflict': row['type_conflict'], 'family': row['family'],
                    'size_bytes': row['size_bytes'], 'compressed_size': row['compressed_size'], 'sha256': row['sha256'],
                    'hash_status': row['hash_status'], 'content_group_id': row['content_group_id'], 'status': row['status'],
                    'issue_code': row['issue_code'],
                    'issues': ' '.join(i.get('code', '') for i in json.loads(row['issues_json'])),
                    'filesystem_mtime_utc': fs['utc'] if fs else None,
                    'archive_time_source': archive['source'] if archive else None,
                    'archive_time_raw': archive['raw'] if archive else None,
                    'archive_time_utc': archive['utc'] if archive else None,
                    'archive_time_local': archive['local'] if archive else None,
                    'archive_time_tz_status': archive['tz_status'] if archive else None,
                    'times_json': json.dumps([{k: t[k] for k in ('kind', 'source', 'raw', 'utc', 'local', 'tz_status')}
                                              for t in entry_times], ensure_ascii=False, sort_keys=True),
                    'mark_priority': mark['priority'] if mark else None, 'mark_note': mark['note'] if mark else None,
                }
                produced += 1
            after = ids[-1]
            if len(rows) < take:
                return


def _about(collection_id, scan_ids, rows_written, truncated, max_rows, min_gap_days) -> dict:
    return {
        'schema': 'ageds.catalog-export/v1', 'generated_at': datetime.now(timezone.utc).isoformat(),
        'catalog': f'{CATALOG_SCHEMA} {CATALOG_VERSION}', 'signature_table_version': signatures.version(),
        'collection_id': collection_id, 'scan_ids': scan_ids, 'rows_written': rows_written,
        'truncated_by_max_rows': truncated, 'max_rows': max_rows, 'min_gap_days': min_gap_days,
        'notes': ['Locators are inert identifiers, not paths. Hash equality is byte identity only.',
                  'Control characters in text cells are shown as \\xNN; locator and name_raw_hex keep exact bytes.',
                  'Scan observations are not acquisitions; ingest creates acquisition records separately.',
                  'CSV only: text cells starting with = + - @ TAB CR are prefixed with an apostrophe '
                  '(formula guard ' + CSV_FORMULA_GUARD + '); XLSX/HTML keep the text unchanged.'],
    }


CSV_FORMULA_GUARD = 'apostrophe_prefix_v1'
_FORMULA_LEADS = ('=', '+', '-', '@', '\t', '\r')


def _csv_guard(value):
    """Spreadsheet apps execute text starting with =,+,-,@ as formulas. Text cells
    that would be interpreted get a leading apostrophe; exact names stay in
    name_raw_hex and in the XLSX/HTML exports."""
    if isinstance(value, str) and value.startswith(_FORMULA_LEADS):
        return "'" + value
    return value


def _write_csv(path: Path, rows) -> int:
    count = 0
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_guard(_clean(v)) for k, v in row.items()})
            count += 1
    return count


def _write_xlsx(path: Path, rows, report: dict, groups: dict, collisions: dict, about_fn) -> int:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

    workbook = Workbook(write_only=True)

    def cell(sheet, value):
        value = _clean(value)
        item = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            item.data_type = 's'  # never a formula, whatever the source text starts with
        return item

    def append(sheet, values):
        sheet.append([cell(sheet, v) for v in values])

    entries = workbook.create_sheet('entries')
    append(entries, COLUMNS)
    count = 0
    for row in rows:
        append(entries, [row[c] for c in COLUMNS])
        count += 1
    sheet = workbook.create_sheet('coverage')
    append(sheet, ('collection_id', 'collection_label', 'type', 'family', 'entries', 'bytes', 'type_conflicts', 'containers',
                   'ok', 'unsupported', 'damaged', 'limit_exceeded', 'encrypted', 'skipped', 'unreadable', 'unstable',
                   'time_source', 'tz_status', 'value_field', 'first', 'last', 'observed_days', 'placeholders_excluded', 'gaps'))
    for collection in report['collections']:
        for item in collection['types']:
            bases = item['time_bases'] or [None]
            for basis in bases:
                status = item['by_status']
                append(sheet, (collection['collection_id'], collection['label'], item['type'], item['family'], item['entries'],
                               item['bytes'], item['type_conflicts'], item['containers'], status['ok'], status['unsupported'],
                               status['damaged'], status['limit_exceeded'], status['encrypted'], status['skipped'],
                               status['unreadable'], status['unstable'],
                               basis and basis['source'], basis and basis['tz_status'], basis and basis['value_field'],
                               basis and basis['first'], basis and basis['last'], basis and basis['observed_days'],
                               basis and basis['placeholder_values_excluded'],
                               basis and '; '.join(f"{g['last_observed_day']}..{g['next_observed_day']} ({g['empty_days']} d)" for g in basis['gaps'])))
    sheet = workbook.create_sheet('duplicates')
    append(sheet, ('group_id', 'sha256', 'size_bytes', 'catalog_entries', 'collections', 'max_depth', 'artifacts'))
    for group in groups['groups']:
        append(sheet, (group['id'], group['sha256'], group['size_bytes'], group['entries'], group['collections'],
                       group['max_depth'], group['artifacts']))
    sheet = workbook.create_sheet('collisions')
    append(sheet, ('name', 'variants', 'entries', 'collections', 'locators'))
    for item in collisions['collisions']:
        append(sheet, (item['name'], item['variants'], item['entries'], item['collections'],
                       ' | '.join(l['locator'] for l in item['locations'])))
    sheet = workbook.create_sheet('about')
    about = about_fn(count)
    for key, value in about.items():
        append(sheet, (key, json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value))
    workbook.save(path)
    return count


_STYLE = ('body{font-family:system-ui,sans-serif;margin:1.5rem;color:#1b1f24}table{border-collapse:collapse;font-size:.85rem}'
          'th,td{border:1px solid #d0d7de;padding:.25rem .45rem;text-align:left;vertical-align:top}th{background:#f6f8fa}'
          'code{font-size:.8rem}.conflict{background:#fff8c5}.bad{background:#ffebe9}section{margin:1.5rem 0}')


def _write_html(path: Path, rows, report: dict, groups: dict, collisions: dict, about_fn) -> int:
    e = lambda value: html.escape(str(_clean(value)) if value is not None else '', quote=True)
    count = 0
    with open(path, 'w', encoding='utf-8') as out:
        out.write('<!doctype html><html lang="pl"><head><meta charset="utf-8">'
                  '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
                  f'<title>Katalog źródeł — eksport</title><style>{_STYLE}</style></head><body>')
        out.write('<h1>Katalog źródeł</h1><p>Obserwacje skanu (nie pozyskania). Lokatory są identyfikatorami, nie ścieżkami. '
                  'Równość hashy oznacza identyczne bajty, nie autorstwo ani czas powstania.</p>')
        out.write('<section><h2>Pokrycie</h2>')
        for collection in report['collections']:
            totals = collection['totals']
            out.write(f'<h3>#{e(collection["collection_id"])} {e(collection["label"])}</h3>')
            out.write('<p>' + ', '.join(f'{e(k)}: {e(v)}' for k, v in totals.items()) + '</p>')
            out.write('<table><tr><th>typ</th><th>wpisy</th><th>bajty</th><th>konflikty</th><th>statusy</th><th>czas (źródło, strefa)</th><th>luki</th></tr>')
            for item in collection['types']:
                statuses = ', '.join(f'{k}={v}' for k, v in item['by_status'].items() if v)
                bases = '<br>'.join(f'{e(b["source"])} ({e(b["tz_status"])}): {e(b["first"])} – {e(b["last"])}' for b in item['time_bases'])
                gaps = '<br>'.join(f'{e(g["last_observed_day"])} → {e(g["next_observed_day"])} ({e(g["empty_days"])} d, {e(b["source"])})'
                                   for b in item['time_bases'] for g in b['gaps'])
                out.write(f'<tr><td>{e(item["type"])}</td><td>{e(item["entries"])}</td><td>{e(item["bytes"])}</td>'
                          f'<td>{e(item["type_conflicts"])}</td><td>{e(statuses)}</td><td>{bases}</td><td>{gaps}</td></tr>')
            out.write('</table>')
        out.write('</section><section><h2>Te same bajty w wielu miejscach</h2><table><tr><th>grupa</th><th>sha256</th><th>rozmiar</th><th>wpisy</th><th>kolekcje</th><th>artefakty</th></tr>')
        for group in groups['groups']:
            out.write(f'<tr><td>{e(group["id"])}</td><td><code>{e(group["sha256"])}</code></td><td>{e(group["size_bytes"])}</td>'
                      f'<td>{e(group["entries"])}</td><td>{e(group["collections"])}</td><td>{e(group["artifacts"])}</td></tr>')
        out.write('</table></section><section><h2>Kolizje nazw (różne bajty)</h2><table><tr><th>nazwa</th><th>warianty</th><th>lokatory</th></tr>')
        for item in collisions['collisions']:
            out.write(f'<tr><td>{e(item["name"])}</td><td>{e(item["variants"])}</td><td>'
                      + '<br>'.join(f'<code>{e(l["locator"])}</code>' for l in item['locations']) + '</td></tr>')
        out.write('</table></section><section><h2>Wpisy</h2><table><tr>' + ''.join(f'<th>{e(c)}</th>' for c in COLUMNS if c != 'times_json') + '</tr>')
        for row in rows:
            css = ' class="bad"' if row['status'] != 'ok' else ' class="conflict"' if row['type_conflict'] else ''
            out.write(f'<tr{css}>' + ''.join(f'<td>{e(row[c])}</td>' for c in COLUMNS if c != 'times_json') + '</tr>')
            count += 1
        about = about_fn(count)
        out.write('</table></section><section><h2>Parametry</h2><pre>' + html.escape(json.dumps(about, ensure_ascii=True, indent=2)) + '</pre></section>')
        out.write('</body></html>\n')
    return count


def export_catalog(output: str | Path, fmt: str, *, collection_id: int | None = None, scan_ids: list[int] | None = None,
                   max_rows: int = 1_000_000, min_gap_days: int = 30) -> dict:
    if fmt not in FORMATS:
        raise ValueError(f'format must be one of {FORMATS}')
    if not isinstance(max_rows, int) or max_rows < 1:
        raise ValueError('max_rows must be a positive integer')
    target = _check_output(Path(output))
    with closing(dbmod.connect()) as db:
        selected = _scan_ids(db, collection_id, scan_ids)
        total = 0
        if selected:
            params = [*selected, *([collection_id] if collection_id is not None else [])]
            total = db.execute(f'SELECT COUNT(*) FROM catalog_entries WHERE scan_id IN ({",".join("?" * len(selected))})'
                               f'{" AND collection_id=?" if collection_id is not None else ""}', params).fetchone()[0]
    truncated = total > max_rows
    rows = iter_rows(collection_id=collection_id, scan_ids=selected, max_rows=max_rows)
    about_fn = lambda count: _about(collection_id, selected, count, truncated, max_rows, min_gap_days)
    written = {'count': 0}

    def write(path: Path):
        if fmt == 'csv':
            written['count'] = _write_csv(path, rows)
            return
        report = coverage_report(collection_id, scan_ids=scan_ids, min_gap_days=min_gap_days)
        groups = list_groups(collection_id=collection_id, limit=500)
        collisions = list_collisions(collection_id=collection_id, limit=500)
        writer = _write_xlsx if fmt == 'xlsx' else _write_html
        written['count'] = writer(path, rows, report, groups, collisions, about_fn)

    size = _publish(write, target, '.' + fmt)
    return {'output': str(target), 'format': fmt, 'rows': written['count'], 'entries_selected': total,
            'truncated_by_max_rows': truncated, 'bytes': size, 'scan_ids': selected, 'source_bytes_written': False}


def export_bytes(fmt: str, **kwargs) -> tuple[bytes, dict]:
    """Export into a private temporary directory and return the bytes (for HTTP downloads)."""
    with tempfile.TemporaryDirectory(prefix='ageds-catalog-export-') as directory:
        result = export_catalog(Path(directory) / f'catalog.{fmt}', fmt, **kwargs)
        return Path(result['output']).read_bytes(), result
