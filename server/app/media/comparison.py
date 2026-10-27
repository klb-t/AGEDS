"""Deterministic comparison of two text versions of one artifact (e.g. two ASR runs).

Neither version is treated as reference truth. Output:

* word-level diff (``difflib.SequenceMatcher`` with ``autojunk=False`` over
  tokens; tokens are stored ASR words when every segment has valid word
  timings, otherwise whitespace tokens of the stored text). Diff counts are not
  a minimal edit distance and not WER.
* per-segment language differences aligned by time overlap (segment language
  when stored, else the version-level language with scope ``version``);
* segment boundary and matched-word start-time differences.

The same inputs always give the same JSON (``content_sha256`` covers it).
"""
from __future__ import annotations

import difflib
import json
import math
import statistics

from ..citations import loads_transcript_segments, validated_segment_words
from ..db import session
from .recipe import sha256_json
from .textmetrics import NORMALIZATION_ID, normalize_text

SCHEMA = 'ageds.text_version_comparison'
DEFAULT_MAX_TOKENS = 20_000
MAX_OPCODES = 5_000
MAX_TOKENS_PER_OPCODE = 200
MAX_SEGMENT_PAIRS = 20_000


def _version(db, derived_text_id: int) -> dict:
    row = db.execute('SELECT * FROM derived_text WHERE id=?', (derived_text_id,)).fetchone()
    if row is None:
        raise LookupError(f'derived text {derived_text_id} not found')
    result = dict(row)
    run = db.execute('SELECT id,status,model,tool,provenance_json FROM processing_runs WHERE id=?', (row['run_id'],)).fetchone() \
        if row['run_id'] else None
    recipe = {}
    if run is not None:
        try:
            recipe = (json.loads(run['provenance_json']) or {}).get('recipe') or {}
        except (TypeError, ValueError):
            recipe = {}
    result['_run'] = dict(run) if run else None
    result['_recipe'] = recipe if isinstance(recipe, dict) else {}
    return result


def _segments(version: dict) -> tuple[list, str | None]:
    try:
        return loads_transcript_segments(version['segments_json'] or '[]'), None
    except ValueError as error:
        return [], str(error)


def _tokens(version: dict, segments: list, normalize: bool) -> tuple[list[str], list | None, str]:
    words, source = [], 'words'
    try:
        if not segments:
            raise ValueError('no segments')
        for segment in segments:
            words.extend(validated_segment_words(segment))
    except (ValueError, TypeError, AttributeError):
        words, source = [], 'text_whitespace'
    if source == 'words':
        tokens = [word['word'].strip() for word in words]
        times = [(word['start'], word['end']) for word in words]
    else:
        tokens, times = version['text'].split(), None
    if normalize:
        kept_tokens, kept_times = [], [] if times is not None else None
        for index, token in enumerate(tokens):
            value = normalize_text(token).replace(' ', '')
            if value:
                kept_tokens.append(value)
                if times is not None:
                    kept_times.append(times[index])
        tokens, times = kept_tokens, kept_times
    return tokens, times, source


def _timed_segments(version: dict, segments: list) -> list[dict]:
    result = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        start, end = segment.get('start'), segment.get('end')
        if type(start) not in (int, float) or type(end) not in (int, float) or not (math.isfinite(start) and math.isfinite(end)) or end < start:
            continue
        language = segment.get('language')
        result.append({'index': index, 'start': float(start), 'end': float(end),
                       'language': language if isinstance(language, str) else version['language'],
                       'language_scope': 'segment' if isinstance(language, str) else 'version'})
    result.sort(key=lambda item: (item['start'], item['end'], item['index']))
    return result


def _stats(values: list[float]) -> dict:
    if not values:
        return {'count': 0}
    absolute = [abs(v) for v in values]
    return {'count': len(values), 'mean_abs': statistics.fmean(absolute), 'median_abs': statistics.median(absolute),
            'max_abs': max(absolute), 'mean_signed': statistics.fmean(values)}


def _describe(version: dict, tokens: list, source: str, segment_error: str | None) -> dict:
    recipe = version['_recipe']
    return {'derived_text_id': version['id'], 'kind': version['kind'], 'run_id': version['run_id'],
            'model': version['model'], 'language': version['language'], 'created_at': version['created_at'],
            'recipe_id': recipe.get('id', 'unknown'), 'recipe_sha256': recipe.get('sha256', 'unknown'),
            'strategy': (recipe.get('effective') or {}).get('strategy', 'unknown') if isinstance(recipe.get('effective'), dict) else 'unknown',
            'token_source': source, 'token_count': len(tokens),
            **({'segments_error': segment_error} if segment_error else {})}


def compare_versions(a_id: int, b_id: int, *, normalize: bool = False, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    if a_id == b_id:
        raise ValueError('choose two different versions')
    if not 1 <= max_tokens <= 200_000:
        raise ValueError('max_tokens must be 1..200000')
    with session() as db:
        a, b = _version(db, a_id), _version(db, b_id)
    if a['artifact_id'] != b['artifact_id']:
        raise ValueError('versions belong to different artifacts')
    if a['kind'] != b['kind']:
        raise ValueError(f'versions have different kinds ({a["kind"]} vs {b["kind"]})')
    a_segments, a_error = _segments(a)
    b_segments, b_error = _segments(b)
    a_tokens, a_times, a_source = _tokens(a, a_segments, normalize)
    b_tokens, b_times, b_source = _tokens(b, b_segments, normalize)
    coverage = {'status': 'complete', 'max_tokens': max_tokens}
    if len(a_tokens) > max_tokens or len(b_tokens) > max_tokens:
        coverage = {'status': 'partial', 'max_tokens': max_tokens, 'reason': 'token limit; only leading tokens compared',
                    'a_tokens_total': len(a_tokens), 'b_tokens_total': len(b_tokens)}
    a_cmp, b_cmp = a_tokens[:max_tokens], b_tokens[:max_tokens]
    matcher = difflib.SequenceMatcher(None, a_cmp, b_cmp, autojunk=False)
    counts = {'equal': 0, 'a_only_deleted': 0, 'b_only_inserted': 0, 'replaced_a': 0, 'replaced_b': 0}
    opcodes, word_deltas = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            counts['equal'] += i2 - i1
            if a_times is not None and b_times is not None:
                word_deltas.extend(b_times[j][0] - a_times[i][0] for i, j in zip(range(i1, i2), range(j1, j2)))
        elif tag == 'delete':
            counts['a_only_deleted'] += i2 - i1
        elif tag == 'insert':
            counts['b_only_inserted'] += j2 - j1
        else:
            counts['replaced_a'] += i2 - i1
            counts['replaced_b'] += j2 - j1
        if len(opcodes) < MAX_OPCODES:
            opcodes.append({'op': tag, 'a': [i1, i2], 'b': [j1, j2],
                            'a_tokens': a_cmp[i1:min(i2, i1 + MAX_TOKENS_PER_OPCODE)],
                            'b_tokens': b_cmp[j1:min(j2, j1 + MAX_TOKENS_PER_OPCODE)],
                            'truncated': (i2 - i1) > MAX_TOKENS_PER_OPCODE or (j2 - j1) > MAX_TOKENS_PER_OPCODE})
    total_opcodes = len(matcher.get_opcodes())
    a_timed, b_timed = _timed_segments(a, a_segments), _timed_segments(b, b_segments)
    pairs, best, overlapped_b = [], {}, set()
    language_seconds = {'same': 0.0, 'different': 0.0, 'unknown': 0.0}
    transitions: dict[str, float] = {}
    start_index = 0
    pairs_truncated = False
    for left in a_timed:
        while start_index < len(b_timed) and b_timed[start_index]['end'] <= left['start']:
            start_index += 1
        for right in b_timed[start_index:]:
            if right['start'] >= left['end']:
                break
            overlap = min(left['end'], right['end']) - max(left['start'], right['start'])
            if overlap <= 0:
                continue
            overlapped_b.add(right['index'])
            if left['language'] is None or right['language'] is None:
                relation = 'unknown'
            else:
                relation = 'same' if left['language'] == right['language'] else 'different'
            language_seconds[relation] += overlap
            key = f'{left["language"]}->{right["language"]}'
            transitions[key] = transitions.get(key, 0.0) + overlap
            current = best.get(left['index'])
            if current is None or overlap > current[0]:
                best[left['index']] = (overlap, left, right)
            if len(pairs) < MAX_SEGMENT_PAIRS:
                pairs.append({'a_segment': left['index'], 'b_segment': right['index'], 'overlap_seconds': overlap,
                              'a_language': left['language'], 'b_language': right['language'],
                              'a_language_scope': left['language_scope'], 'b_language_scope': right['language_scope'],
                              'language_relation': relation, 'a_start': left['start'], 'a_end': left['end'],
                              'b_start': right['start'], 'b_end': right['end']})
            else:
                pairs_truncated = True
    start_deltas = [right['start'] - left['start'] for _, left, right in (best[k] for k in sorted(best))]
    end_deltas = [right['end'] - left['end'] for _, left, right in (best[k] for k in sorted(best))]
    body = {
        'schema': SCHEMA, 'schema_version': 1, 'artifact_id': a['artifact_id'],
        'a': _describe(a, a_tokens, a_source, a_error), 'b': _describe(b, b_tokens, b_source, b_error),
        'normalization': NORMALIZATION_ID if normalize else 'none (stored tokens compared exactly)',
        'text_diff': {'algorithm': 'difflib.SequenceMatcher(autojunk=False) over tokens', 'coverage': coverage,
                      'counts': counts, 'opcodes_total': total_opcodes, 'opcodes_listed': len(opcodes),
                      'opcodes': opcodes,
                      'note': 'diff counts describe this alignment; they are not WER and neither side is reference truth'},
        'language': {'pairs': pairs, 'pairs_truncated': pairs_truncated,
                     'overlap_seconds': language_seconds,
                     'transitions_seconds': dict(sorted(transitions.items())),
                     'unaligned_a_segments': [s['index'] for s in a_timed if s['index'] not in best],
                     'unaligned_b_segments': [s['index'] for s in b_timed if s['index'] not in overlapped_b],
                     'note': 'segment language is language identification output, not transcript correctness'},
        'timing': {'segment_start_delta_seconds': _stats(start_deltas), 'segment_end_delta_seconds': _stats(end_deltas),
                   'matched_word_start_delta_seconds': _stats(word_deltas) if a_times is not None and b_times is not None
                   else {'count': 0, 'reason': 'word timings unavailable on at least one side'},
                   'pairing': 'each A segment with the B segment of largest overlap; deltas are B minus A'},
    }
    body['content_sha256'] = sha256_json(body)
    return body


def artifact_versions(artifact_id: int, *, limit: int = 200) -> list[dict]:
    """Transcript/OCR versions of an artifact with the strategy and recipe of their run."""
    from ..transcription import word_timing_capabilities
    with session() as db:
        rows = db.execute("""SELECT d.id,d.kind,d.model,d.language,d.created_at,d.run_id,d.segments_json,
                                    length(d.text) AS text_length,r.status AS run_status,r.provenance_json
                             FROM derived_text d LEFT JOIN processing_runs r ON r.id=d.run_id
                             WHERE d.artifact_id=? AND d.kind IN ('transcript','ocr') ORDER BY d.id DESC LIMIT ?""",
                          (artifact_id, limit)).fetchall()
        from .version_status import superseded
        marks = superseded(db, artifact_id)
    result = []
    for row in rows:
        try:
            recipe = (json.loads(row['provenance_json'] or '{}') or {}).get('recipe') or {}
        except (TypeError, ValueError):
            recipe = {}
        effective = recipe.get('effective') if isinstance(recipe.get('effective'), dict) else {}
        try:
            segments = loads_transcript_segments(row['segments_json'] or '[]')
        except ValueError:
            segments = None
        languages = sorted({s.get('language') for s in segments or [] if isinstance(s, dict) and isinstance(s.get('language'), str)})
        result.append({
            'id': row['id'], 'kind': row['kind'], 'model': row['model'], 'language': row['language'],
            'segment_languages': languages, 'created_at': row['created_at'], 'run_id': row['run_id'],
            'run_status': row['run_status'], 'text_length': row['text_length'],
            'segments': len(segments) if segments is not None else 'invalid',
            'recipe_id': recipe.get('id', 'unknown' if row['run_id'] else 'legacy_unknown'),
            'recipe_sha256': recipe.get('sha256', 'unknown'), 'pin_status': recipe.get('pin_status', 'unknown'),
            'strategy': effective.get('strategy', effective.get('layout', 'unknown')),
            'word_timing': word_timing_capabilities(segments)['status'] if row['kind'] == 'transcript' and segments is not None else 'n/a',
            'superseded': marks.get(row['id']),
        })
    return result
