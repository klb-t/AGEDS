"""Media processing: runs (with recipe pins), derived text versions and jobs.

Every run is listed with tool/provider/model/version or ``unknown``. A
transcript is model output for one run; ``language`` is the model's label, not
a correctness score. Text versions are listed with the SHA-256 of their text.
"""
from __future__ import annotations

import json

from ..blocks import kv, table
from ..common import sha256_text
from . import Section
from ._runs import run_record

VERSION = 'media.v1'
RUN_COLUMNS = [('run_id', 'id'), ('artifact_id', 'id'), ('status', 'text'), ('tool', 'text'), ('tool_version', 'text'),
               ('provider', 'text'), ('model', 'text'), ('model_version', 'text'), ('recipe_id', 'text'),
               ('recipe_sha256', 'hash'), ('parameters_hash', 'hash'), ('started_at', 'time'), ('finished_at', 'time')]
TEXT_COLUMNS = [('derived_text_id', 'id'), ('artifact_id', 'id'), ('kind', 'text'), ('model', 'text'),
                ('language', 'text'), ('run_id', 'id'), ('chars', 'number'), ('segments', 'number'),
                ('words_stored', 'number'), ('text_sha256', 'hash'), ('created_at', 'time')]


def _segments(raw) -> tuple[int | None, int | None]:
    try:
        value = json.loads(raw or '[]')
    except (TypeError, ValueError):
        return None, None
    if not isinstance(value, list):
        return None, None
    words = sum(1 for segment in value if isinstance(segment, dict) and isinstance(segment.get('words'), list) and segment['words'])
    return len(value), words


def _pin(raw) -> str:
    try:
        payload = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return 'unparseable_payload'
    recipe = payload.get('recipe') if isinstance(payload, dict) else None
    return 'pinned' if isinstance(recipe, dict) and recipe.get('sha256') else 'unpinned'


def provide(ctx) -> dict:
    section = Section(ctx, 'media', __name__, VERSION, 'section_media')
    db, max_rows = ctx.db, ctx.params['max_rows']
    total_runs = db.execute('SELECT COUNT(*) FROM processing_runs').fetchone()[0]
    total_texts = db.execute('SELECT COUNT(*) FROM derived_text').fetchone()[0]
    runs = db.execute('SELECT * FROM processing_runs ORDER BY id LIMIT ?', (max_rows,)).fetchall()
    texts = db.execute('SELECT * FROM derived_text ORDER BY id LIMIT ?', (max_rows,)).fetchall()
    if not total_runs and not total_texts:
        section.status('empty', 'no processing runs or derived text')
    if total_runs > max_rows or total_texts > max_rows:
        section.status('partial', 'run or text listing limited by max_rows')
        section.exclude('runs_not_listed_over_max_rows', max(0, total_runs - max_rows))
        section.exclude('texts_not_listed_over_max_rows', max(0, total_texts - max_rows))
    status_rows = [dict(r) for r in db.execute('SELECT status, COUNT(*) AS count FROM processing_runs GROUP BY 1 ORDER BY 1')]
    section.block(kv('media.summary', [('model_runs', total_runs), ('derived_text_versions', total_texts)]
                     + [(f'runs_{row["status"]}', row['count']) for row in status_rows], ctx.lang))
    run_rows = []
    for run in runs:
        record, unknowns = run_record(run)
        section.input_row('processing_runs', run)
        section.model_run(record)
        for name in unknowns:
            section.unknown(name)
        run_rows.append({'run_id': run['id'], 'artifact_id': run['artifact_id'], 'status': record['status'],
                         'tool': record['tool'], 'tool_version': record['tool_version'], 'provider': record['provider'],
                         'model': record['model'], 'model_version': record['model_version'],
                         'recipe_id': record['recipe_id'], 'recipe_sha256': record['recipe_sha256'],
                         'parameters_hash': record['parameters_sha256'], 'started_at': run['started_at'],
                         'finished_at': run['finished_at']})
    section.block(table('media.runs', RUN_COLUMNS, run_rows, ctx.lang, title_key='model_runs',
                        total_rows=total_runs, truncated=total_runs > max_rows))
    text_rows = []
    for text in texts:
        section.input_row('derived_text', text)
        segments, words = _segments(text['segments_json'])
        if text['run_id'] is None:
            section.unknown(f'derived_text.{text["id"]}.processing_run')
        text_rows.append({'derived_text_id': text['id'], 'artifact_id': text['artifact_id'], 'kind': text['kind'],
                          'model': text['model'] or 'unknown', 'language': text['language'] or 'unknown',
                          'run_id': text['run_id'], 'chars': len(text['text']) if isinstance(text['text'], str) else None,
                          'segments': segments, 'words_stored': words,
                          'text_sha256': sha256_text(text['text']) if isinstance(text['text'], str) else None,
                          'created_at': text['created_at']})
    section.block(table('media.texts', TEXT_COLUMNS, text_rows, ctx.lang, total_rows=total_texts,
                        truncated=total_texts > max_rows))
    jobs: dict[tuple, int] = {}
    for job in db.execute('SELECT kind, status, payload_json FROM jobs'):
        key = (job['kind'], job['status'], _pin(job['payload_json']))
        jobs[key] = jobs.get(key, 0) + 1
    section.block(table('media.jobs', [('job_kind', 'text'), ('job_status', 'text'), ('pin_status', 'text'),
                                       ('count', 'number')],
                        [{'job_kind': k, 'job_status': s, 'pin_status': p, 'count': n}
                         for (k, s, p), n in sorted(jobs.items())], ctx.lang, title_key='jobs'))
    return section.result()
