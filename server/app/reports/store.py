"""Append-only report register (table ``reports``, schema version 60)."""
from __future__ import annotations

import json
import sqlite3

from .common import ReportNotFound

PUBLIC_COLUMNS = ('id', 'report_uid', 'report_type', 'template_id', 'template_version', 'params_json',
                  'params_sha256', 'content_sha256', 'manifest_sha256', 'manifest_name', 'formats_json',
                  'sections_json', 'citations_json', 'ageds_version', 'git_commit', 'created_at')


def insert_report(db: sqlite3.Connection, row: dict) -> int:
    columns = ('report_uid', 'report_type', 'template_id', 'template_version', 'params_json', 'params_sha256',
               'content_sha256', 'manifest_sha256', 'output_dir', 'manifest_name', 'formats_json', 'sections_json',
               'citations_json', 'ageds_version', 'git_commit', 'created_at')
    cursor = db.execute(f'INSERT INTO reports({",".join(columns)}) VALUES({",".join("?" * len(columns))})',
                        tuple(row[column] for column in columns))
    db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES('reports','report_created','report',?,?)",
               (cursor.lastrowid, json.dumps({'report_uid': row['report_uid'], 'report_type': row['report_type'],
                                              'manifest_sha256': row['manifest_sha256'],
                                              'content_sha256': row['content_sha256']}, sort_keys=True)))
    return int(cursor.lastrowid)


def _decode(row: sqlite3.Row, *, include_output_dir: bool) -> dict:
    item = {key: row[key] for key in PUBLIC_COLUMNS}
    for key in ('params_json', 'formats_json', 'sections_json', 'citations_json'):
        item[key[:-5]] = json.loads(item.pop(key))
    if include_output_dir:
        item['output_dir'] = row['output_dir']
    return item


def table_exists(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reports'").fetchone() is not None


def list_reports(db: sqlite3.Connection, *, report_type: str | None = None, limit: int = 100,
                 include_output_dir: bool = True) -> list[dict]:
    sql = 'SELECT * FROM reports'
    args: list = []
    if report_type:
        sql += ' WHERE report_type=?'
        args.append(report_type)
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(limit)
    return [_decode(row, include_output_dir=include_output_dir) for row in db.execute(sql, args)]


def get_report(db: sqlite3.Connection, report_id: int, *, include_output_dir: bool = True) -> dict:
    row = db.execute('SELECT * FROM reports WHERE id=?', (report_id,)).fetchone()
    if row is None:
        raise ReportNotFound(f'report {report_id} does not exist')
    return _decode(row, include_output_dir=include_output_dir)
