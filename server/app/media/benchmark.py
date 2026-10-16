"""Manual ASR benchmark on a user-supplied local corpus. No quality claims.

The user writes a manifest of audio files and reference texts; recipes run
through the same adapter interface as the worker (``adapter_for_recipe``), or
precomputed hypotheses are scored without inference. WER and CER are computed
by code (``textmetrics``) with a declared, versioned normalization, per item,
per language and per reference segment when references are segmented.
Results go only to the given output directory, which must be outside the
repository, the content store and every directory holding manifest audio; source
audio is opened read-only.

Manifest (``ageds.asr_benchmark_manifest`` v1)::

    {"schema": "ageds.asr_benchmark_manifest", "schema_version": 1,
     "recipes": ["legacy", "per-segment-pl-nl-en"],
     "items": [{"id": "call-01", "audio": "audio/call-01.wav", "language": "pl",
                "reference": "...",
                "reference_segments": [{"start": 0.0, "end": 4.2, "text": "...", "language": "nl"}]}]}

Hypotheses (``ageds.asr_benchmark_hypotheses`` v1, optional, no inference)::

    {"schema": "ageds.asr_benchmark_hypotheses", "schema_version": 1,
     "runs": {"label": {"call-01": {"text": "...", "segments": [...]}}}}
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .recipe import effective_recipe, sha256_json
from .textmetrics import (DEFAULT_MAX_CELLS, NORMALIZATION_ID, NORMALIZATION_RULES, error_rates)

MANIFEST_SCHEMA = 'ageds.asr_benchmark_manifest'
HYPOTHESES_SCHEMA = 'ageds.asr_benchmark_hypotheses'
RESULT_SCHEMA = 'ageds.asr_benchmark_result'
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 10_000
REPO_ROOT = Path(__file__).resolve().parents[3]
SCOPE_STATEMENT = ('Error rates measured on the supplied local corpus against the supplied references with the '
                   'declared normalization. They are not a general quality claim about any language, model, '
                   'speaker or recording condition; synthetic or small corpora say little about real speech.')


def _load_json(path: Path, schema: str, limit: int = MAX_MANIFEST_BYTES) -> dict:
    data = path.read_bytes()
    if len(data) > limit:
        raise ValueError(f'{path.name} exceeds {limit} bytes')

    def no_constants(constant):
        raise ValueError(f'nonfinite JSON constant: {constant}')
    value = json.loads(data.decode('utf-8'), parse_constant=no_constants)
    if not isinstance(value, dict) or value.get('schema') != schema or value.get('schema_version') != 1:
        raise ValueError(f'{path.name} must declare schema {schema} version 1')
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def check_output_dir(output: Path, *, store_dir: Path | None = None, source_dirs=()) -> Path:
    """Refuse output inside the repository, the content store or a directory of the source
    audio (review U2: originals are read-only); create it otherwise."""
    resolved = output.expanduser().resolve()
    if _inside(resolved, REPO_ROOT):
        raise ValueError(f'refusing to write benchmark results inside the repository ({REPO_ROOT}); choose a private directory')
    if store_dir is not None and _inside(resolved, Path(store_dir).resolve()):
        raise ValueError('refusing to write benchmark results inside the evidence content store')
    for directory in source_dirs:
        if _inside(resolved, Path(directory).resolve()):
            raise ValueError(f'refusing to write benchmark results inside a source audio directory ({directory})')
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _validate_items(manifest: dict, base: Path) -> list[dict]:
    items = manifest.get('items')
    if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
        raise ValueError(f'manifest items must be a nonempty list of at most {MAX_ITEMS}')
    seen, result = set(), []
    for position, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'].strip():
            raise ValueError(f'item {position} needs a nonblank id')
        if item['id'] in seen:
            raise ValueError(f'duplicate item id {item["id"]}')
        seen.add(item['id'])
        segments = item.get('reference_segments')
        if segments is not None:
            if not isinstance(segments, list):
                raise ValueError(f'{item["id"]}: reference_segments must be a list')
            for segment in segments:
                if (not isinstance(segment, dict) or not isinstance(segment.get('text'), str)
                        or any(type(segment.get(k)) not in (int, float) or not math.isfinite(segment[k]) for k in ('start', 'end'))
                        or segment['end'] < segment['start']):
                    raise ValueError(f'{item["id"]}: each reference segment needs finite start <= end and text')
        reference = item.get('reference')
        if reference is None and segments is not None:
            reference = ' '.join(segment['text'] for segment in segments)
        if not isinstance(reference, str):
            raise ValueError(f'{item["id"]}: reference text (or reference_segments) is required')
        language = item.get('language')
        if language is not None and not isinstance(language, str):
            raise ValueError(f'{item["id"]}: language must be a string or null')
        audio = item.get('audio')
        audio_path = None
        if audio is not None:
            if not isinstance(audio, str) or not audio:
                raise ValueError(f'{item["id"]}: audio must be a path string')
            audio_path = (base / audio).resolve() if not Path(audio).is_absolute() else Path(audio)
        result.append({'id': item['id'], 'audio': audio_path, 'language': language, 'reference': reference,
                       'reference_segments': segments})
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _hypothesis_words(segments) -> list[tuple[float, str]]:
    """(midpoint seconds, word) from stored word timings, else segment midpoints."""
    words = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        timed = segment.get('words')
        if isinstance(timed, list) and timed:
            for word in timed:
                if isinstance(word, dict) and isinstance(word.get('word'), str) and all(
                        type(word.get(k)) in (int, float) for k in ('start', 'end')):
                    words.append(((word['start'] + word['end']) / 2, word['word']))
            continue
        if isinstance(segment.get('text'), str) and all(type(segment.get(k)) in (int, float) for k in ('start', 'end')):
            words.append(((segment['start'] + segment['end']) / 2, segment['text']))
    return words


def _segment_language(segments, start: float, end: float) -> str | None:
    overlap: dict[str, float] = {}
    for segment in segments or []:
        if not isinstance(segment, dict) or not isinstance(segment.get('language'), str):
            continue
        s, e = segment.get('start'), segment.get('end')
        if type(s) in (int, float) and type(e) in (int, float):
            value = min(e, end) - max(s, start)
            if value > 0:
                overlap[segment['language']] = overlap.get(segment['language'], 0.0) + value
    return max(sorted(overlap), key=lambda code: overlap[code]) if overlap else None


def score_item(item: dict, hypothesis_text: str, hypothesis_segments, *, max_cells: int) -> dict:
    result = {'id': item['id'], 'language': item['language'],
              **error_rates(item['reference'], hypothesis_text, max_cells=max_cells)}
    if item['reference_segments'] is not None:
        words = _hypothesis_words(hypothesis_segments)
        scored = []
        for index, segment in enumerate(item['reference_segments']):
            inside = ' '.join(text for midpoint, text in words if segment['start'] <= midpoint < segment['end'])
            entry = {'index': index, 'start': segment['start'], 'end': segment['end'],
                     'language': segment.get('language'),
                     'hypothesis_language_by_overlap': _segment_language(hypothesis_segments, segment['start'], segment['end']),
                     **error_rates(segment['text'], inside, max_cells=max_cells)}
            entry.pop('reference_normalized', None)
            scored.append(entry)
        result['segments'] = scored
        result['segment_assignment'] = 'hypothesis words by midpoint time inside [start, end)'
    return result


def _aggregate(scored: list[dict]) -> dict:
    def accumulate(bucket, entry):
        for name, unit in (('word', 'wer'), ('char', 'cer')):
            counts = entry.get(name, {})
            if counts.get('status') in ('computed', 'undefined_empty_reference'):
                bucket[name]['edits'] += counts['edits']
                bucket[name]['reference_length'] += counts['reference_length']
            else:
                bucket[name]['not_computed'] += 1
        bucket['items'] += 1

    def empty():
        return {'items': 0, 'word': {'edits': 0, 'reference_length': 0, 'not_computed': 0},
                'char': {'edits': 0, 'reference_length': 0, 'not_computed': 0}}

    def finish(bucket):
        for name, unit in (('word', 'wer'), ('char', 'cer')):
            total = bucket[name]['reference_length']
            bucket[name][unit] = bucket[name]['edits'] / total if total else None
        return bucket
    overall, by_language, by_segment_language = empty(), {}, {}
    agreement = {'compared': 0, 'agree': 0}
    for entry in scored:
        if 'error' in entry:
            continue
        accumulate(overall, entry)
        accumulate(by_language.setdefault(entry.get('language') or 'unspecified', empty()), entry)
        for segment in entry.get('segments', []):
            accumulate(by_segment_language.setdefault(segment.get('language') or 'unspecified', empty()), segment)
            if segment.get('language') and segment.get('hypothesis_language_by_overlap'):
                agreement['compared'] += 1
                agreement['agree'] += segment['language'] == segment['hypothesis_language_by_overlap']
    return {'overall_micro': finish(overall), 'by_item_language': {k: finish(v) for k, v in sorted(by_language.items())},
            'by_reference_segment_language': {k: finish(v) for k, v in sorted(by_segment_language.items())},
            'segment_language_label_agreement': dict(agreement, meaning='agreement of language labels only; not quality')}


def run_benchmark(manifest_path: Path, output_dir: Path, *, recipes: list[str] | None = None,
                  hypotheses_path: Path | None = None, adapter_factory=None, max_cells: int = DEFAULT_MAX_CELLS,
                  store_dir: Path | None = None) -> dict:
    manifest_path = Path(manifest_path)
    manifest = _load_json(manifest_path, MANIFEST_SCHEMA)
    items = _validate_items(manifest, manifest_path.resolve().parent)
    audio_dirs = sorted({item['audio'].resolve().parent for item in items if item['audio'] is not None})
    output = check_output_dir(Path(output_dir), store_dir=store_dir, source_dirs=audio_dirs)
    runs = []
    if hypotheses_path is not None:
        hypotheses = _load_json(Path(hypotheses_path), HYPOTHESES_SCHEMA)
        if not isinstance(hypotheses.get('runs'), dict) or not hypotheses['runs']:
            raise ValueError('hypotheses file needs a nonempty runs object')
        for label in sorted(hypotheses['runs']):
            produced = hypotheses['runs'][label]
            scored = []
            for item in items:
                entry = produced.get(item['id']) if isinstance(produced, dict) else None
                if not isinstance(entry, dict) or not isinstance(entry.get('text'), str):
                    scored.append({'id': item['id'], 'error': 'no hypothesis supplied for this item'})
                    continue
                scored.append(score_item(item, entry['text'], entry.get('segments'), max_cells=max_cells))
            runs.append({'label': label, 'mode': 'precomputed_hypotheses', 'recipe': 'unknown',
                         'items': scored, 'aggregates': _aggregate(scored)})
    else:
        recipe_ids = recipes or manifest.get('recipes') or ['legacy']
        if not isinstance(recipe_ids, list) or not all(isinstance(r, str) for r in recipe_ids):
            raise ValueError('recipes must be a list of recipe ids')
        if adapter_factory is None:
            from ..worker import adapter_for_recipe
            adapter_factory = adapter_for_recipe
        for recipe_id in recipe_ids:
            record = effective_recipe(recipe_id, kind='asr')
            adapter = adapter_factory(record)
            scored = []
            for item in items:
                if item['audio'] is None:
                    scored.append({'id': item['id'], 'error': 'item has no audio path'})
                    continue
                try:
                    started = time.monotonic()
                    with item['audio'].open('rb') as stream:
                        result = adapter.transcribe(stream)
                    elapsed = time.monotonic() - started
                except Exception as error:  # recorded per item; the benchmark continues
                    scored.append({'id': item['id'], 'error': f'{type(error).__name__}: {error}'})
                    continue
                entry = score_item(item, result.text, result.segments, max_cells=max_cells)
                work = getattr(result, 'work', None) or {}
                amount = work.get('amount') if isinstance(work, dict) else None
                entry.update({'audio_sha256': _sha256_file(item['audio']), 'hypothesis_text': result.text,
                              'hypothesis_language': result.language, 'processing_seconds': elapsed,
                              'audio_seconds': amount if type(amount) in (int, float) else 'unknown',
                              'real_time_factor': elapsed / amount if type(amount) in (int, float) and amount > 0 else 'unknown'})
                scored.append(entry)
            runs.append({'label': recipe_id, 'mode': 'inference', 'recipe': {'id': record['id'], 'sha256': record['sha256'],
                                                                             'effective': record['effective']},
                         'items': scored, 'aggregates': _aggregate(scored)})
    now = datetime.now(timezone.utc)
    result = {
        'schema': RESULT_SCHEMA, 'schema_version': 1, 'created_at': now.isoformat(),
        'manifest': {'path_name': manifest_path.name, 'sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                     'items': len(items)},
        'normalization': {'id': NORMALIZATION_ID, 'rules': list(NORMALIZATION_RULES)},
        'metrics': {'wer': 'word edits (S+D+I) / reference words after normalization',
                    'cer': 'character edits / reference characters (including single spaces) after normalization',
                    'empty_reference': 'rate undefined (null); insertions reported', 'alignment_max_cells': max_cells},
        'environment': {'python': platform.python_version()},
        'scope_statement': SCOPE_STATEMENT,
        'runs': runs,
    }
    result['content_sha256'] = sha256_json({k: v for k, v in result.items() if k != 'created_at'})
    target = output / f'asr-benchmark-{now.strftime("%Y%m%dT%H%M%S%fZ")}-{result["manifest"]["sha256"][:8]}.json'
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8') + b'\n'
    fd, temporary = tempfile.mkstemp(prefix='.asr-benchmark-', suffix='.tmp', dir=output)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)       # exclusive publication; never overwrites
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'output': str(target), 'content_sha256': result['content_sha256'], 'runs': len(runs),
            'items': len(items), 'normalization': NORMALIZATION_ID, 'result': result}
