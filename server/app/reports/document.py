"""The computed report document and its method record (standard layer 1).

The method record names: AGEDS version, git commit (or ``unknown``) and
whether the tree had uncommitted changes, generation time (UTC), parameters
with their SHA-256, template id/version and template file hashes, the model
runs referenced, unknown fields, requested and observed data ranges per
section, exclusions and limits, the hashes of every cited artifact, text
version and anchor, and the rows read (ids + row SHA-256). The content digest
(the "report hash") covers the whole document except its ``integrity`` field.
"""
from __future__ import annotations

import platform
import subprocess

from . import disclaimers
from .blocks import kv, table
from .common import CANONICALIZATION, sha256_json
from .labels import LABELS, label
from .manifest import REPORT_DOMAIN, REPORT_SCHEMA, content_digest
from .paths import REPOSITORY_ROOT

NUMBERS_POLICY = 'computed_by_code; prose uses code templates with computed slots; templates receive computed values only'
LIMITATIONS = [
    'A digest detects change against a known value; this report is not signed.',
    'Equal hashes prove identical bytes, not authorship, truth or time of creation.',
    'Times are placed on the UTC axis only from known offsets or an explicitly assumed zone; others are excluded and counted.',
    'Absence of data in a period means no observation, not inactivity.',
    'Quotes are source text shown as data; they are bounded and their full-text SHA-256 is given.',
    'Model output (ASR, OCR, LLM) is listed with its run; it is not verified against the recording or the truth.',
]


def ageds_version() -> str:
    try:
        from ..main import app
        return str(app.version)
    except Exception:  # version is informational; never fabricate one
        return 'unknown'


def git_state() -> tuple[str, object]:
    try:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=REPOSITORY_ROOT, capture_output=True, text=True,
                              timeout=10)
        if head.returncode != 0:
            return 'unknown', 'unknown'
        status = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=REPOSITORY_ROOT,
                                capture_output=True, text=True, timeout=30)
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else 'unknown'
        return head.stdout.strip() or 'unknown', dirty
    except (OSError, subprocess.SubprocessError):
        return 'unknown', 'unknown'


def _renderer() -> dict:
    import jinja2
    try:
        import openpyxl
        xlsx = openpyxl.__version__
    except ImportError:
        xlsx = 'unavailable'
    return {'python': platform.python_version(), 'jinja2': jinja2.__version__, 'openpyxl': xlsx}


def _merge_sections(sections: list[dict]) -> dict:
    runs, unknowns, exclusions, ranges = [], [], [], []
    cited = {'artifacts': {}, 'derived_text': {}, 'anchors': {}}
    rows: dict[str, dict] = {}
    seen_runs = set()
    for section in sections:
        name = section['section']
        for run in section['model_runs']:
            key = (run.get('table'), run.get('run_id'))
            if key not in seen_runs:
                seen_runs.add(key)
                runs.append(run)
        unknowns.extend({'section': name, 'field': field} for field in section['unknowns'])
        exclusions.extend({'section': name, 'name': key, 'value': value}
                          for key, value in sorted(section['exclusions'].items()))
        span = section['data_range']
        ranges.append({'section': name, 'from': span['from'], 'to': span['to'], 'basis': span['basis']})
        for kind, entries in section['cited'].items():
            for entry in entries:
                cited[kind].setdefault(entry['id'], entry)
        for table_name, entries in section['inputs'].items():
            bucket = rows.setdefault(table_name, {})
            for entry in entries:
                bucket.setdefault(entry['id'], entry)
    runs.sort(key=lambda run: (str(run.get('table')), run.get('run_id') or 0))
    inputs_rows = {name: [bucket[key] for key in sorted(bucket)] for name, bucket in sorted(rows.items())}
    return {'model_runs': runs, 'unknowns': unknowns, 'exclusions': exclusions, 'ranges': ranges,
            'cited': {kind: [values[key] for key in sorted(values)] for kind, values in cited.items()},
            'inputs': {'tables': [{'table': name, 'rows': len(entries), 'rows_sha256': sha256_json(entries)}
                                  for name, entries in inputs_rows.items()],
                       'rows': inputs_rows}}


def _method_view(method: dict, lang: str) -> list[dict]:
    merged = method
    view = [
        table('method.parameters', [('parameter', 'text'), ('value', 'json')],
              [{'parameter': key, 'value': value} for key, value in sorted(method['parameters'].items())],
              lang, title_key='parameters'),
        table('method.template_files', [('template_file', 'text'), ('template_sha256', 'hash')],
              [{'template_file': item['name'], 'template_sha256': item['sha256']} for item in method['template']['files']],
              lang, title_key='template_id'),
        kv('method.renderer', sorted(method['renderer'].items()), lang, title_key='renderer'),
        table('method.sections', [('section', 'text'), ('provider', 'text'), ('provider_version', 'text'),
                                  ('status', 'text'), ('reason', 'text'), ('snapshot', 'text'), ('rule', 'text')],
              method['sections'], lang, title_key='sections_status'),
        table('method.ranges', [('section', 'text'), ('first_utc', 'time'), ('last_utc', 'time'), ('kind', 'text')],
              [{'section': r['section'], 'first_utc': r['from'], 'last_utc': r['to'], 'kind': r['basis']}
               for r in method['data_range']['sections']], lang, title_key='observed_from'),
        table('method.model_runs', [('run_id', 'id'), ('status', 'text'), ('tool', 'text'), ('tool_version', 'text'),
                                    ('provider', 'text'), ('model', 'text'), ('model_version', 'text'),
                                    ('recipe_id', 'text'), ('recipe_sha256', 'hash'), ('parameters_hash', 'hash')],
              [{**run, 'parameters_hash': run.get('parameters_sha256')} for run in merged['model_runs']],
              lang, title_key='model_runs'),
        table('method.unknowns', [('section', 'text'), ('unknown_field', 'text')],
              [{'section': item['section'], 'unknown_field': item['field']} for item in method['unknowns']],
              lang, title_key='unknowns'),
        table('method.exclusions', [('section', 'text'), ('exclusion', 'text'), ('value', 'json')],
              [{'section': item['section'], 'exclusion': item['name'], 'value': item['value']}
               for item in method['exclusions']], lang, title_key='exclusions'),
        table('method.cited_artifacts', [('artifact_id', 'id'), ('sha256', 'hash'), ('size_bytes', 'number')],
              [{'artifact_id': item['id'], 'sha256': item.get('sha256'), 'size_bytes': item.get('size_bytes')}
               for item in method['cited']['artifacts']], lang, title_key='cited_hashes'),
        table('method.cited_versions', [('derived_text_id', 'id'), ('artifact_id', 'id'), ('text_sha256', 'hash'),
                                        ('run_id', 'id')],
              [{'derived_text_id': item['id'], 'artifact_id': item['artifact_id'], 'text_sha256': item['text_sha256'],
                'run_id': item['run_id']} for item in method['cited']['derived_text']], lang),
        table('method.cited_anchors', [('anchor_id', 'id'), ('artifact_id', 'id'), ('derived_text_id', 'id'),
                                       ('quote_sha256', 'hash'), ('start_ms', 'number'), ('end_ms', 'number')],
              [{'anchor_id': item['id'], **{k: item[k] for k in ('artifact_id', 'derived_text_id', 'quote_sha256',
                                                                  'start_ms', 'end_ms')}}
               for item in method['cited']['anchors']], lang),
        table('method.inputs', [('input_table', 'text'), ('rows', 'number'), ('rows_sha256', 'hash')],
              [{'input_table': item['table'], 'rows': item['rows'], 'rows_sha256': item['rows_sha256']}
               for item in method['inputs']['tables']], lang, title_key='inputs'),
        table('method.derived_caches', [('cache', 'text'), ('rule_id', 'text'), ('rule_version', 'text'),
                                        ('rule_sha256', 'hash'), ('params', 'json'), ('projection_id', 'id'),
                                        ('build_id', 'id'), ('build_mode', 'text'), ('build_finished_at', 'time'),
                                        ('refreshed_by_report', 'bool'), ('write_scope', 'text')],
              method['derived_caches'], lang, title_key='derived_caches'),
        table('method.limitations', [('limitation', 'text')], [{'limitation': text} for text in method['limitations']],
              lang, title_key='limitations'),
    ]
    return view


def build_document(*, report_uid: str, report_type: str, definition: dict, params: dict, sections: list[dict],
                   generated_at: str, template_files: list[dict], derived_caches: list[dict] | None = None) -> dict:
    lang = params['language']
    commit, dirty = git_state()
    merged = _merge_sections(sections)
    for section in sections:
        section['title'] = label(lang, section['title_key'])
        section['status_label'] = label(lang, f'status_{section["status"]}')
    method = {
        'standard_layer': 'minimum_metadata',
        'ageds_version': ageds_version(), 'git_commit': commit, 'git_dirty': dirty,
        'generated_at': generated_at, 'report_type': report_type,
        'template': {'id': f'reports/{definition["template"]}', 'version': definition['version'],
                     'files': template_files},
        'parameters': params, 'parameters_sha256': sha256_json(params),
        'renderer': _renderer(), 'numbers_policy': NUMBERS_POLICY, 'model_prose': 'none', 'signed': False,
        'data_range': {'requested_from': params['from'], 'requested_to': params['to'], 'sections': merged['ranges']},
        'sections': [{key: section[key] for key in ('section', 'provider', 'provider_version', 'status', 'reason',
                                                    'snapshot', 'rule')} for section in sections],
        'model_runs': merged['model_runs'], 'unknowns': merged['unknowns'], 'exclusions': merged['exclusions'],
        'cited': merged['cited'], 'inputs': merged['inputs'], 'limitations': list(LIMITATIONS),
        # Derived caches read by the snapshot (and refreshed just before it, in their own write
        # transaction, when stale): the only writes a report causes besides its register row.
        'derived_caches': list(derived_caches or []),
    }
    used_labels = {key for key in LABELS['pl']} | {f'intro_{name}' for name in ('catalog', 'timeline', 'time_budget',
                                                                                  'hypothesis_dossier', 'lawyer_package')}
    document = {
        'schema': REPORT_SCHEMA, 'report_uid': report_uid, 'report_type': report_type,
        'title': params['title'] or label(lang, definition['title_key']),
        'title_source': 'human' if params['title'] else 'code',
        'type_label': label(lang, definition['title_key']), 'language': lang,
        'labels': {key: label(lang, key) for key in sorted(used_labels)},
        'method': method, 'method_view': _method_view(method, lang), 'sections': sections,
        'disclaimers': [],
    }
    for section in sections:
        for block in section['blocks']:
            if block['kind'] == 'notice' and block['level'] == 'disclaimer':
                document['disclaimers'].append({'section': section['section'], 'text': block['text'],
                                                'sha256': block['sha256']})
    document['integrity'] = {'algorithm': 'sha256', 'canonicalization': CANONICALIZATION, 'domain': REPORT_DOMAIN,
                             'content_sha256': content_digest(document)}
    return document


__all__ = ['build_document', 'disclaimers']
