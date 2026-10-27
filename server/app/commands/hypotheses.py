"""CLI for the hypothesis analyzer: preregistration, dossier, theses, disclosure, event study.

Each command prints one JSON object on stdout; errors go to stderr. Definitions
are read from a JSON file (bounded size, duplicate keys and NaN refused). An
event study only runs on a registered version id; parameter overrides are
recorded in the result as deviations from the registration.
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path

COMMANDS = ('hypothesis-register', 'hypothesis-version', 'hypothesis-preview', 'hypotheses-list', 'hypothesis-show',
            'dossier-add', 'dossier-classify', 'dossier-candidates', 'dossier-list', 'thesis-add', 'disclosure-add',
            'eventstudy-run', 'eventstudy-show')
MAX_DEFINITION_BYTES = 1024 * 1024
CLASSES = ('candidate', 'supporting', 'contradicting', 'context', 'rejected')
KINDS = ('event', 'artifact', 'derived_text', 'anchor', 'annotation')


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError('must be a nonnegative integer')
    return parsed


def _ids(value: str) -> list[int]:
    try:
        ids = [int(part) for part in value.replace(';', ',').split(',') if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError('expected comma-separated integer ids') from exc
    if any(i < 1 for i in ids):
        raise argparse.ArgumentTypeError('ids must be positive')
    return ids


def read_definition(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) > MAX_DEFINITION_BYTES:
        raise ValueError(f'definition file exceeds {MAX_DEFINITION_BYTES} bytes')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'definition repeats JSON key {key!r}')
            result[key] = value
        return result
    value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda c: (_ for _ in ()).throw(ValueError(f'nonfinite number {c} in definition')))
    if not isinstance(value, dict):
        raise ValueError('definition file must contain one JSON object')
    return value


def register(subparsers) -> None:
    p = subparsers.add_parser('hypothesis-register', help='preregister a hypothesis definition (JSON file) before analysis')
    p.add_argument('file', type=Path)
    p.add_argument('--actor', default=None, help='self-declared author label (no authentication)')
    p.add_argument('--change-note', default=None)
    p = subparsers.add_parser('hypothesis-version', help='register a changed definition as a new linked version')
    p.add_argument('hypothesis_id', type=_positive)
    p.add_argument('file', type=Path)
    p.add_argument('--actor', default=None)
    p.add_argument('--change-note', default=None)
    p = subparsers.add_parser('hypothesis-preview', help='validate, normalize and hash a definition without storing it')
    p.add_argument('file', type=Path)
    p = subparsers.add_parser('hypotheses-list', help='preregistered analyzer hypotheses (registered/first-analysed times); hypothesis-list belongs to the Loom quarantine')
    p.add_argument('--limit', type=_positive, default=200)
    p.add_argument('--offset', type=_nonnegative, default=0)
    p = subparsers.add_parser('hypothesis-show', help='versions, runs, dossier, theses and disclosure of one hypothesis')
    p.add_argument('hypothesis_id', type=_positive)
    p = subparsers.add_parser('dossier-add', help='add a pinned pointer to a source record to the dossier')
    p.add_argument('hypothesis_id', type=_positive)
    p.add_argument('kind', choices=KINDS)
    p.add_argument('target_id', type=_positive)
    p.add_argument('--observation', type=_positive, default=None, help='source observation id (artifact items)')
    p.add_argument('--classification', choices=CLASSES, default=None)
    p.add_argument('--rationale', default=None)
    p.add_argument('--version', type=_positive, default=None, help='hypothesis version the decision refers to (default latest)')
    p.add_argument('--note', default=None)
    p.add_argument('--actor', default=None)
    p = subparsers.add_parser('dossier-classify', help='append a human classification decision for a dossier item')
    p.add_argument('item_id', type=_positive)
    p.add_argument('classification', choices=CLASSES)
    p.add_argument('--rationale', default=None)
    p.add_argument('--version', type=_positive, default=None)
    p.add_argument('--actor', default=None)
    p = subparsers.add_parser('dossier-candidates', help='propose candidate items (FTS + rules + optional provider)')
    p.add_argument('hypothesis_id', type=_positive)
    p.add_argument('--query', action='append', default=None, help='FTS5 query; repeatable; default from the definition')
    p.add_argument('--no-rules', action='store_true')
    p.add_argument('--provider', default='none', help='candidate provider (default none: no model, no call)')
    p.add_argument('--opt-in', action='store_true', help='explicit per-run consent to send excerpts to the provider')
    p.add_argument('--limit', type=_positive, default=50)
    p.add_argument('--version', type=_positive, default=None)
    p.add_argument('--categories', type=Path, default=None, help='private category configuration for category selectors')
    p.add_argument('--direction', choices=('definition', 'disconfirming', 'both'), default=None,
                   help='supporting queries, preregistered disconfirming queries, or both (default: both when registered)')
    p.add_argument('--actor', default=None)
    p = subparsers.add_parser('dossier-list', help='dossier items with current classification and decision history')
    p.add_argument('hypothesis_id', type=_positive)
    p = subparsers.add_parser('thesis-add', help='add a thesis with its typology status and evidence item ids')
    p.add_argument('hypothesis_id', type=_positive)
    p.add_argument('--type', required=True, dest='status_type',
                   choices=('quote', 'paraphrase', 'inference_chain', 'analogy', 'speculation', 'counterfactual', 'normative'))
    p.add_argument('--text', required=True)
    p.add_argument('--evidence', type=_ids, default=[], help='comma-separated dossier item ids')
    p.add_argument('--previous', type=_positive, default=None, help='thesis id this revises')
    p.add_argument('--actor', default=None)
    p = subparsers.add_parser('disclosure-add', help='append human text to one epistemic-disclosure quadrant')
    p.add_argument('hypothesis_id', type=_positive)
    p.add_argument('quadrant', choices=('known_knowns', 'known_unknowns', 'unknown_knowns', 'unknown_unknowns'))
    p.add_argument('text')
    p.add_argument('--actor', default=None)
    p = subparsers.add_parser('eventstudy-run', help='run the permutation event study on a REGISTERED version id')
    p.add_argument('version_id', type=_positive)
    p.add_argument('--permutations', type=_positive, default=None, help='override (recorded as a deviation)')
    p.add_argument('--seed', type=_nonnegative, default=None, help='override (recorded as a deviation)')
    p.add_argument('--null-model', default=None,
                   choices=('circular_shift', 'permutation', 'stratified_weekday_hour', 'block_permutation'))
    p.add_argument('--block-s', type=_positive, default=None)
    p.add_argument('--max-work', type=_positive, default=None, help='explicit work budget (bisect operations)')
    p.add_argument('--categories', type=Path, default=None,
                   help='private category configuration (else AGEDS_ACTIVITY_CATEGORIES); its hash must match the registration')
    p.add_argument('--actor', default=None)
    p.add_argument('--summary', action='store_true', help='print a summary instead of the full result')
    p = subparsers.add_parser('eventstudy-show', help='print one stored run (result hash re-verified)')
    p.add_argument('run_id', type=_positive)
    p.add_argument('--output', type=Path, default=None, help='write to a NEW file instead of stdout')


def _summary(run: dict) -> dict:
    result = run['result']
    return {k: run[k] for k in ('run_id', 'hypothesis_version_id', 'definition_sha256', 'seed', 'code_version',
                                'input_sha256', 'result_sha256', 'integrity', 'identical_result_runs', 'created_at')} | {
        'status': result['status'], 'disclaimer': result['disclaimer'],
        'input': {k: result['input'][k] for k in ('stimulus_count', 'outcome_count', 'density_count', 'outside_span')}
        | {'untimed_excluded': result['input']['untimed_excluded']['count']},
        'windows': [{'label': w['label'], 'observed': w['naive']['observed'], 'null_mean': w['naive']['null_mean'],
                     'p_naive': w['naive']['p_value'], 'p_normalized': w['normalized'].get('p_value')}
                    for w in result['windows']],
        'flagged_warnings': [w['id'] for w in result['warnings'] if w['flagged']]}


def _dispatch(args, db) -> dict:
    from ..analysis import dossier, eventstudy, preregistration, theses
    from ..analysis.hypotheses import disclaimer
    c = args.command
    if c == 'hypothesis-register':
        return preregistration.register_hypothesis(db, read_definition(args.file), actor=args.actor, change_note=args.change_note)
    if c == 'hypothesis-version':
        return preregistration.register_version(db, args.hypothesis_id, read_definition(args.file), actor=args.actor,
                                                change_note=args.change_note)
    if c == 'hypothesis-preview':
        return preregistration.preview(read_definition(args.file))
    if c == 'hypotheses-list':
        return {'disclaimer': disclaimer(), **preregistration.list_hypotheses(db, limit=args.limit, offset=args.offset)}
    if c == 'hypothesis-show':
        return {'disclaimer': disclaimer(), 'versions': preregistration.list_versions(db, args.hypothesis_id),
                'runs': eventstudy.list_runs(db, args.hypothesis_id), 'dossier': dossier.list_dossier(db, args.hypothesis_id),
                'theses': theses.list_theses(db, args.hypothesis_id), 'disclosure': theses.disclosure_view(db, args.hypothesis_id)}
    if c == 'dossier-add':
        return dossier.add_item(db, args.hypothesis_id, args.kind, args.target_id, observation_id=args.observation,
                                classification=args.classification, rationale=args.rationale, version_id=args.version,
                                note=args.note, actor=args.actor)
    if c == 'dossier-classify':
        return dossier.classify(db, args.item_id, args.classification, rationale=args.rationale, version_id=args.version,
                                actor=args.actor)
    if c == 'dossier-candidates':
        return dossier.candidate_search(db, args.hypothesis_id, version_id=args.version, queries=args.query,
                                        use_rules=not args.no_rules, provider=args.provider, opt_in=args.opt_in,
                                        limit=args.limit, actor=args.actor, category_config=args.categories,
                                        direction=args.direction)
    if c == 'dossier-list':
        return dossier.list_dossier(db, args.hypothesis_id)
    if c == 'thesis-add':
        return theses.add_thesis(db, args.hypothesis_id, args.text, args.status_type, args.evidence,
                                 previous_thesis_id=args.previous, actor=args.actor)
    if c == 'disclosure-add':
        return theses.add_disclosure(db, args.hypothesis_id, args.quadrant, args.text, actor=args.actor)
    if c == 'eventstudy-run':
        overrides = {key: value for key, value in (('permutations', args.permutations), ('seed', args.seed),
                                                   ('null_model', args.null_model), ('block_s', args.block_s))
                     if value is not None}
        limits = eventstudy.EventStudyLimits(**({'max_work': args.max_work} if args.max_work else {}))
        run = eventstudy.run_event_study(db, args.version_id, overrides=overrides or None, actor=args.actor, limits=limits,
                                         category_config=args.categories)
        return _summary(run) if args.summary else run
    return eventstudy.get_run(db, args.run_id)


def handle(args) -> int | None:
    if getattr(args, 'command', None) not in COMMANDS:
        return None
    from ..analysis.hypotheses import HypothesisNotFound
    from ..db import connect, init_db
    init_db()
    with closing(connect()) as db:
        try:
            result = _dispatch(args, db)
        except HypothesisNotFound as exc:
            print(json.dumps({'command': args.command, 'error': 'NotFound', 'message': str(exc)}, ensure_ascii=False),
                  file=sys.stderr)
            return 2
    if args.command == 'eventstudy-show' and args.output is not None:
        from ..cli import _write_new_json
        _write_new_json(result, args.output, max_bytes=256 * 1024 * 1024)
        print(json.dumps({'command': args.command, 'output': str(args.output.resolve()), 'run_id': result['run_id'],
                          'result_sha256': result['result_sha256'], 'integrity': result['integrity']}, ensure_ascii=False))
        return 0
    print(json.dumps({'command': args.command, **result}, ensure_ascii=False, allow_nan=False))
    return 0
