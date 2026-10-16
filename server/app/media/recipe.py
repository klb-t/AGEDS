"""Processing recipes as versioned data with a canonical hash.

A recipe preset is a JSON file (``media/recipes/<id>.json`` or a private
``EW_MEDIA_RECIPE_DIR``). ``effective_recipe`` resolves ``{"$setting": ...}``
placeholders from the current settings, applies explicit overrides, validates
every key and hashes the canonical JSON of the execution-relevant fields. The
enqueue path pins that complete effective recipe in ``jobs.payload_json``; the
worker executes the pinned copy, so a later change of presets, settings or the
default never changes a job that was already queued.

Jobs created before recipes existed have an empty payload. They are executed
with the ``legacy`` preset resolved at run time and recorded as
``pin_status='unpinned_pre_recipe_job'`` with ``historical_recipe='unknown'``;
no recipe is attributed to earlier runs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

RECIPE_SCHEMA = 'ageds.media_recipe'
RECIPE_SCHEMA_VERSION = 1
PAYLOAD_SCHEMA = 'ageds.job_payload'
PAYLOAD_SCHEMA_VERSION = 1
BUILTIN_DIR = Path(__file__).parent / 'recipes'
MAX_RECIPE_BYTES = 64 * 1024
MAX_PRIVATE_RECIPES = 500
ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
ASR_LANGUAGE = re.compile(r'^[a-z]{2,3}(-[a-z0-9]{2,8})?$')
TESSERACT_LANGUAGE = re.compile(r'^[A-Za-z0-9_]{2,32}$')

RECIPE_KINDS = ('asr', 'ocr', 'diarization')
JOB_KIND_FOR_RECIPE = {'asr': 'transcribe', 'ocr': 'ocr', 'diarization': 'diarize'}
RECIPE_KIND_FOR_JOB = {job: recipe for recipe, job in JOB_KIND_FOR_RECIPE.items()}
DEFAULT_RECIPE_ENV = {'asr': ('EW_ASR_RECIPE', 'legacy'),
                      'ocr': ('EW_OCR_RECIPE', 'ocr-tesseract-pol-nld-eng'),
                      'diarization': ('EW_DIARIZATION_RECIPE', 'diarization-pyannote')}
DEDUP_POLICIES = ('reuse', 'reuse_completed', 'duplicate')
DEDUP_ENV = 'EW_MEDIA_DEDUP_POLICY'
SETTING_FIELDS = {'whisper_model': 'EW_WHISPER_MODEL', 'whisper_device': 'EW_WHISPER_DEVICE',
                  'whisper_compute_type': 'EW_WHISPER_COMPUTE_TYPE'}
LANGUAGE_PROBABILITY_MEANING = 'language identification probability; not transcript correctness'

ASR_STRATEGIES = ('auto_per_file', 'per_segment_language')
VAD_PARAMETER_KEYS = {'threshold', 'neg_threshold', 'min_speech_duration_ms', 'max_speech_duration_s',
                      'min_silence_duration_ms', 'speech_pad_ms'}
# faster-whisper 1.2.0 WhisperModel.transcribe keyword arguments a recipe may
# pass verbatim. Language, VAD and timestamps have dedicated recipe fields.
TRANSCRIBE_OPTION_KEYS = {'beam_size', 'best_of', 'patience', 'length_penalty', 'repetition_penalty',
                          'no_repeat_ngram_size', 'temperature', 'compression_ratio_threshold',
                          'log_prob_threshold', 'no_speech_threshold', 'condition_on_previous_text',
                          'prompt_reset_on_temperature', 'initial_prompt', 'prefix', 'suppress_blank',
                          'suppress_tokens', 'without_timestamps', 'max_initial_timestamp',
                          'prepend_punctuations', 'append_punctuations', 'max_new_tokens', 'chunk_length',
                          'hallucination_silence_threshold', 'hotwords', 'task'}
MODEL_OPTION_KEYS = {'device_index', 'cpu_threads', 'num_workers', 'download_root', 'local_files_only', 'revision'}

COMMON_KEYS = ('schema', 'schema_version', 'id', 'kind', 'revision')
EXECUTION_KEYS = {
    'asr': COMMON_KEYS + ('strategy', 'engine', 'model', 'device', 'compute_type', 'model_options', 'vad',
                          'word_timestamps', 'language', 'allowed_languages', 'windows', 'detection',
                          'transcribe_options', 'limits'),
    'ocr': COMMON_KEYS + ('engine', 'languages', 'psm', 'oem', 'layout', 'pdf', 'limits'),
    'diarization': COMMON_KEYS + ('engine', 'pipeline', 'num_speakers', 'min_speakers', 'max_speakers', 'limits'),
}
DESCRIPTIVE_KEYS = ('description',)
IMMUTABLE_OVERRIDE_KEYS = set(COMMON_KEYS)
# Overrides an HTTP client may send: decoding and strategy parameters only
# (``None`` = the whole value; a set = only those nested keys). Engine, model,
# device, compute type, every model option, the diarization pipeline and the
# resource limits choose code, files or network behaviour on the server and are
# operator decisions: a recipe preset (built-in or EW_MEDIA_RECIPE_DIR) or the CLI.
CLIENT_OVERRIDE_KEYS = {
    'asr': {'strategy': None, 'vad': None, 'word_timestamps': None, 'language': None, 'allowed_languages': None,
            'windows': None, 'detection': None, 'transcribe_options': None},
    'ocr': {'languages': None, 'psm': None, 'oem': None, 'layout': None, 'pdf': {'dpi', 'max_pages'}},
    'diarization': {'num_speakers': None, 'min_speakers': None, 'max_speakers': None},
}


class RecipeError(ValueError):
    """A recipe, override or pinned payload failed validation."""


def canonical_json(value) -> bytes:
    """Canonical form hashed for recipes: sorted keys, no whitespace, UTF-8, finite numbers."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def sha256_json(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _private_dir() -> Path | None:
    raw = os.getenv('EW_MEDIA_RECIPE_DIR', '')
    return Path(raw) if raw else None


def _read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RecipeError(f'recipe file unavailable: {path.name}')
    raw = path.read_bytes()
    if len(raw) > MAX_RECIPE_BYTES:
        raise RecipeError(f'recipe file exceeds {MAX_RECIPE_BYTES} bytes: {path.name}')

    def no_constants(constant):
        raise RecipeError(f'nonfinite JSON constant in recipe: {constant}')

    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RecipeError(f'duplicate key in recipe: {key}')
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode('utf-8'), parse_constant=no_constants, object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecipeError(f'recipe is not valid UTF-8 JSON: {path.name}: {error}') from error
    if not isinstance(value, dict):
        raise RecipeError('recipe must be a JSON object')
    return value


def _preset_paths() -> dict[str, tuple[Path, str]]:
    found: dict[str, tuple[Path, str]] = {}
    for path in sorted(BUILTIN_DIR.glob('*.json')):
        found[path.stem] = (path, 'builtin')
    private = _private_dir()
    if private is not None and private.is_dir():
        for count, path in enumerate(sorted(private.glob('*.json'))):
            if count >= MAX_PRIVATE_RECIPES:
                break
            if path.stem in found:
                raise RecipeError(f'private recipe {path.stem} collides with a built-in preset id')
            found[path.stem] = (path, 'private')
    return found


def preset_ids(kind: str | None = None) -> list[str]:
    result = []
    for recipe_id, (path, _) in _preset_paths().items():
        if kind is None or _read_json(path).get('kind') == kind:
            result.append(recipe_id)
    return result


def load_preset(recipe_id: str) -> tuple[dict, str]:
    if not isinstance(recipe_id, str) or not ID_PATTERN.match(recipe_id):
        raise RecipeError('recipe id must match ' + ID_PATTERN.pattern)
    paths = _preset_paths()
    if recipe_id not in paths:
        raise RecipeError(f'unknown recipe {recipe_id}')
    path, origin = paths[recipe_id]
    preset = _read_json(path)
    if preset.get('id') != recipe_id:
        raise RecipeError(f'recipe file {path.name} declares id {preset.get("id")!r}')
    return preset, origin


def default_recipe_id(kind: str) -> str:
    if kind not in DEFAULT_RECIPE_ENV:
        raise RecipeError(f'unknown recipe kind {kind}')
    env, fallback = DEFAULT_RECIPE_ENV[kind]
    return os.getenv(env, '') or fallback


def default_dedup_policy() -> str:
    policy = os.getenv(DEDUP_ENV, '') or 'reuse'
    if policy not in DEDUP_POLICIES:
        raise RecipeError(f'{DEDUP_ENV} must be one of {", ".join(DEDUP_POLICIES)}')
    return policy


def _merge(base: dict, overrides: dict, path: str = '') -> dict:
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if not isinstance(key, str):
            raise RecipeError('override keys must be strings')
        if not path and key in IMMUTABLE_OVERRIDE_KEYS:
            raise RecipeError(f'override cannot change {key}')
        if isinstance(value, dict) and isinstance(result.get(key), dict) and '$setting' not in value:
            result[key] = _merge(result[key], value, f'{path}{key}.')
        else:
            result[key] = copy.deepcopy(value)
    return result


def check_client_overrides(kind: str, overrides) -> None:
    """Refuse override keys an HTTP client may not choose (see ``CLIENT_OVERRIDE_KEYS``)."""
    if overrides is None:
        return
    if not isinstance(overrides, dict):
        raise RecipeError('overrides must be an object')
    allowed = CLIENT_OVERRIDE_KEYS.get(kind)
    if allowed is None:
        raise RecipeError(f'unknown recipe kind {kind}')
    refused = []
    for key, value in overrides.items():
        if key not in allowed:
            refused.append(str(key))
        elif allowed[key] is not None:
            if not isinstance(value, dict):
                raise RecipeError(f'override {key} must be an object')
            refused.extend(f'{key}.{inner}' for inner in value if inner not in allowed[key])
    if refused:
        permitted = sorted(k if v is None else f'{k}.{{{",".join(sorted(v))}}}' for k, v in allowed.items())
        raise RecipeError(f'override {", ".join(sorted(refused))} can be set only in a local recipe preset or through '
                          f'the CLI (operator decision); HTTP clients may override only: {", ".join(permitted)}')


def _finite(value, name: str, *, low=None, high=None, integer=False, allow_none=False):
    if value is None and allow_none:
        return
    if type(value) is bool or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
        raise RecipeError(f'{name} must be {"an integer" if integer else "a number"}')
    if not math.isfinite(value):
        raise RecipeError(f'{name} must be finite')
    if low is not None and value < low:
        raise RecipeError(f'{name} must be >= {low}')
    if high is not None and value > high:
        raise RecipeError(f'{name} must be <= {high}')


def _json_scalarish(value, name: str, depth: int = 0):
    if depth > 4:
        raise RecipeError(f'{name} is nested too deeply')
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str) and len(value) > 4096:
            raise RecipeError(f'{name} string exceeds 4096 characters')
        return
    if isinstance(value, (int, float)):
        _finite(value, name)
        return
    if isinstance(value, list) and len(value) <= 64:
        for item in value:
            _json_scalarish(item, name, depth + 1)
        return
    raise RecipeError(f'{name} must be a JSON scalar or a short list')


def _resolve_settings(recipe: dict, settings) -> dict:
    resolved = {}
    for key, value in list(recipe.items()):
        if isinstance(value, dict) and '$setting' in value:
            if set(value) != {'$setting'} or value['$setting'] not in SETTING_FIELDS:
                raise RecipeError(f'{key} has an invalid $setting placeholder')
            setting = value['$setting']
            actual = getattr(settings, setting)
            recipe[key] = actual
            resolved[key] = {'setting': setting, 'env': SETTING_FIELDS[setting], 'value': actual}
    return resolved


def _validate_asr(r: dict) -> None:
    if r['strategy'] not in ASR_STRATEGIES:
        raise RecipeError(f'asr strategy must be one of {", ".join(ASR_STRATEGIES)}')
    if r['engine'] != 'faster-whisper':
        raise RecipeError('asr engine must be faster-whisper')
    for key in ('model', 'device', 'compute_type'):
        if not isinstance(r[key], str) or not r[key].strip() or len(r[key]) > 512:
            raise RecipeError(f'{key} must be a nonblank string')
    if not isinstance(r['model_options'], dict) or set(r['model_options']) - MODEL_OPTION_KEYS:
        raise RecipeError('model_options accepts only ' + ', '.join(sorted(MODEL_OPTION_KEYS)))
    for key, value in r['model_options'].items():
        _json_scalarish(value, f'model_options.{key}')
    vad = r['vad']
    if not isinstance(vad, dict) or set(vad) != {'enabled', 'parameters'} or type(vad['enabled']) is not bool:
        raise RecipeError('vad must be {"enabled": bool, "parameters": null|object}')
    if vad['parameters'] is not None:
        if not isinstance(vad['parameters'], dict) or set(vad['parameters']) - VAD_PARAMETER_KEYS:
            raise RecipeError('vad.parameters accepts only ' + ', '.join(sorted(VAD_PARAMETER_KEYS)))
        for key, value in vad['parameters'].items():
            _finite(value, f'vad.parameters.{key}', low=0, allow_none=key == 'neg_threshold')
    if type(r['word_timestamps']) is not bool:
        raise RecipeError('word_timestamps must be boolean')
    if r['language'] is not None and (not isinstance(r['language'], str) or not ASR_LANGUAGE.match(r['language'])):
        raise RecipeError('language must be null or a lowercase language code')
    allowed = r['allowed_languages']
    if allowed is not None:
        if (not isinstance(allowed, list) or not allowed or len(allowed) > 32 or len(set(allowed)) != len(allowed)
                or any(not isinstance(code, str) or not ASR_LANGUAGE.match(code) for code in allowed)):
            raise RecipeError('allowed_languages must be a nonempty list of unique lowercase language codes')
    options = r['transcribe_options']
    if not isinstance(options, dict) or set(options) - TRANSCRIBE_OPTION_KEYS:
        raise RecipeError('transcribe_options accepts only faster-whisper decoding options: '
                          + ', '.join(sorted(TRANSCRIBE_OPTION_KEYS)))
    for key, value in options.items():
        _json_scalarish(value, f'transcribe_options.{key}')
    if options.get('task', 'transcribe') != 'transcribe':
        raise RecipeError('only task=transcribe is supported; translation would not be a transcript')
    if r['strategy'] == 'auto_per_file':
        for key in ('allowed_languages', 'windows', 'detection', 'limits'):
            if r[key] is not None:
                raise RecipeError(f'{key} applies only to the per_segment_language strategy')
        return
    if allowed is None:
        raise RecipeError('per_segment_language requires allowed_languages')
    if r['language'] is not None:
        raise RecipeError('per_segment_language identifies language per window; language must be null')
    if not vad['enabled']:
        raise RecipeError('per_segment_language requires VAD speech regions')
    windows = r['windows']
    if not isinstance(windows, dict) or set(windows) != {'max_window_s', 'min_window_s'}:
        raise RecipeError('windows must be {"max_window_s", "min_window_s"}')
    _finite(windows['max_window_s'], 'windows.max_window_s', low=0.5, high=30.0)
    _finite(windows['min_window_s'], 'windows.min_window_s', low=0.0, high=windows['max_window_s'])
    detection = r['detection']
    if not isinstance(detection, dict) or set(detection) != {'language_detection_segments',
                                                             'language_detection_threshold', 'top_k_reported'}:
        raise RecipeError('detection must define language_detection_segments, language_detection_threshold, top_k_reported')
    _finite(detection['language_detection_segments'], 'detection.language_detection_segments', low=1, high=1, integer=True)
    _finite(detection['language_detection_threshold'], 'detection.language_detection_threshold', low=0.0, high=1.0)
    _finite(detection['top_k_reported'], 'detection.top_k_reported', low=1, high=200, integer=True)
    limits = r['limits']
    if not isinstance(limits, dict) or set(limits) != {'max_windows', 'max_regions_recorded'}:
        raise RecipeError('limits must define max_windows and max_regions_recorded')
    _finite(limits['max_windows'], 'limits.max_windows', low=1, high=100_000, integer=True)
    _finite(limits['max_regions_recorded'], 'limits.max_regions_recorded', low=0, high=100_000, integer=True)


def _validate_ocr(r: dict) -> None:
    if r['engine'] != 'tesseract-cli':
        raise RecipeError('ocr engine must be tesseract-cli')
    languages = r['languages']
    if (not isinstance(languages, list) or not languages or len(languages) > 16
            or any(not isinstance(code, str) or not TESSERACT_LANGUAGE.match(code) for code in languages)
            or len(set(languages)) != len(languages)):
        raise RecipeError('languages must be a nonempty list of unique Tesseract language names')
    _finite(r['psm'], 'psm', low=0, high=13, integer=True)
    _finite(r['oem'], 'oem', low=0, high=3, integer=True, allow_none=True)
    if r['layout'] not in ('document', 'chat_screenshot'):
        raise RecipeError('layout must be document or chat_screenshot')
    pdf = r['pdf']
    if not isinstance(pdf, dict) or set(pdf) != {'rasterizer', 'dpi', 'max_pages'} or pdf['rasterizer'] != 'pdftoppm':
        raise RecipeError('pdf must be {"rasterizer": "pdftoppm", "dpi", "max_pages"}')
    _finite(pdf['dpi'], 'pdf.dpi', low=36, high=1200, integer=True)
    _finite(pdf['max_pages'], 'pdf.max_pages', low=1, high=10_000, integer=True)
    limits = r['limits']
    if not isinstance(limits, dict) or set(limits) != {'max_input_bytes', 'max_page_image_bytes', 'timeout_s', 'max_words'}:
        raise RecipeError('limits must define max_input_bytes, max_page_image_bytes, timeout_s, max_words')
    _finite(limits['max_input_bytes'], 'limits.max_input_bytes', low=1, high=2**40, integer=True)
    _finite(limits['max_page_image_bytes'], 'limits.max_page_image_bytes', low=1, high=2**40, integer=True)
    _finite(limits['timeout_s'], 'limits.timeout_s', low=1, high=86400)
    _finite(limits['max_words'], 'limits.max_words', low=1, high=10_000_000, integer=True)


def _validate_diarization(r: dict) -> None:
    if r['engine'] != 'pyannote.audio':
        raise RecipeError('diarization engine must be pyannote.audio')
    if not isinstance(r['pipeline'], str) or not r['pipeline'].strip() or len(r['pipeline']) > 1024:
        raise RecipeError('pipeline must be a nonblank model id or local path')
    for key in ('num_speakers', 'min_speakers', 'max_speakers'):
        _finite(r[key], key, low=1, high=1000, integer=True, allow_none=True)
    if r['min_speakers'] is not None and r['max_speakers'] is not None and r['min_speakers'] > r['max_speakers']:
        raise RecipeError('min_speakers exceeds max_speakers')
    limits = r['limits']
    if not isinstance(limits, dict) or set(limits) != {'max_turns'}:
        raise RecipeError('limits must define max_turns')
    _finite(limits['max_turns'], 'limits.max_turns', low=1, high=10_000_000, integer=True)


VALIDATORS = {'asr': _validate_asr, 'ocr': _validate_ocr, 'diarization': _validate_diarization}


def validate_effective(effective: dict) -> None:
    """Strict: unknown or missing keys are errors, never silently ignored parameters."""
    if not isinstance(effective, dict):
        raise RecipeError('effective recipe must be an object')
    kind = effective.get('kind')
    if kind not in RECIPE_KINDS:
        raise RecipeError(f'recipe kind must be one of {", ".join(RECIPE_KINDS)}')
    expected = set(EXECUTION_KEYS[kind])
    if set(effective) != expected:
        missing, extra = sorted(expected - set(effective)), sorted(set(effective) - expected)
        raise RecipeError(f'recipe keys differ from schema: missing={missing} unknown={extra}')
    if effective['schema'] != RECIPE_SCHEMA or effective['schema_version'] != RECIPE_SCHEMA_VERSION:
        raise RecipeError('unsupported recipe schema')
    if not isinstance(effective['id'], str) or not ID_PATTERN.match(effective['id']):
        raise RecipeError('recipe id must match ' + ID_PATTERN.pattern)
    _finite(effective['revision'], 'revision', low=1, integer=True)
    try:
        canonical_json(effective)
    except (TypeError, ValueError) as error:
        raise RecipeError(f'recipe is not canonical JSON: {error}') from error
    VALIDATORS[kind](effective)


def effective_recipe(recipe_id: str | None = None, *, kind: str = 'asr', overrides: dict | None = None,
                     settings=None) -> dict:
    """Resolve, validate and hash; the single entry point for enqueue, worker and export."""
    if settings is None:
        from ..config import settings as current
        settings = current
    recipe_id = recipe_id or default_recipe_id(kind)
    preset, origin = load_preset(recipe_id)
    if preset.get('kind') != kind:
        raise RecipeError(f'recipe {recipe_id} is kind {preset.get("kind")!r}, not {kind!r}')
    if overrides is not None and not isinstance(overrides, dict):
        raise RecipeError('overrides must be an object')
    preset_sha = sha256_json(preset)
    unknown_top = set(preset) - set(EXECUTION_KEYS.get(kind, ())) - set(DESCRIPTIVE_KEYS)
    if unknown_top:
        raise RecipeError(f'recipe {recipe_id} has unknown keys {sorted(unknown_top)}')
    merged = _merge(preset, overrides or {})
    unknown_override = set(merged) - set(EXECUTION_KEYS[kind]) - set(DESCRIPTIVE_KEYS)
    if unknown_override:
        raise RecipeError(f'override has unknown keys {sorted(unknown_override)}')
    description = merged.pop('description', None)
    resolved = _resolve_settings(merged, settings)
    validate_effective(merged)
    return {
        'id': merged['id'], 'kind': kind, 'sha256': sha256_json(merged), 'effective': merged,
        'provenance': {'preset_origin': origin, 'preset_sha256': preset_sha,
                       'overrides': copy.deepcopy(overrides or {}), 'resolved_settings': resolved,
                       'description': description, 'hash_rule': 'sha256 of canonical JSON of effective'},
    }


def job_payload(record: dict, dedup_policy: str, *, pinned_at: str | None = None) -> dict:
    return {'schema': PAYLOAD_SCHEMA, 'schema_version': PAYLOAD_SCHEMA_VERSION,
            'recipe': {'id': record['id'], 'kind': record['kind'], 'sha256': record['sha256'],
                       'effective': record['effective'], 'provenance': record['provenance']},
            'dedup_policy': dedup_policy, 'pinned_at': pinned_at or _now()}


def parse_payload(raw) -> dict | None:
    """Return the pinned recipe payload, None for an empty legacy payload, or raise."""
    if raw is None or raw == '' or raw == '{}':
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise RecipeError(f'job payload is not JSON: {error}') from error
    if payload == {}:
        return None
    if not isinstance(payload, dict) or payload.get('schema') != PAYLOAD_SCHEMA:
        raise RecipeError('job payload has an unknown schema')
    return payload


def recipe_from_job(job: dict, *, settings=None) -> dict:
    """The recipe a claimed job must execute, with explicit pin status.

    A pinned recipe is revalidated and its hash recomputed; a mismatch fails
    the attempt instead of executing parameters other than those recorded.
    """
    kind = RECIPE_KIND_FOR_JOB.get(job.get('kind'))
    if kind is None:
        raise RecipeError(f'job kind {job.get("kind")!r} has no recipe kind')
    payload = parse_payload(job.get('payload_json'))
    if payload is None:
        if kind != 'asr':
            raise RecipeError(f'{job.get("kind")} jobs require a pinned recipe payload')
        record = effective_recipe('legacy', kind='asr', settings=settings)
        record['pin_status'] = 'unpinned_pre_recipe_job'
        record['historical_recipe'] = 'unknown'
        record['provenance'] = dict(record['provenance'], resolution='resolved_at_run_time_from_current_settings')
        return record
    pinned = payload.get('recipe')
    if not isinstance(pinned, dict) or not isinstance(pinned.get('effective'), dict):
        raise RecipeError('job payload has no pinned effective recipe')
    effective = pinned['effective']
    validate_effective(effective)
    if effective['kind'] != kind:
        raise RecipeError(f'pinned recipe kind {effective["kind"]} does not match job kind {job.get("kind")}')
    digest = sha256_json(effective)
    if digest != pinned.get('sha256'):
        raise RecipeError('pinned recipe hash mismatch; the job payload was altered or corrupted')
    return {'id': effective['id'], 'kind': kind, 'sha256': digest, 'effective': copy.deepcopy(effective),
            'provenance': pinned.get('provenance') or 'unknown', 'pin_status': 'pinned_at_enqueue',
            'pinned_at': payload.get('pinned_at', 'unknown'), 'dedup_policy': payload.get('dedup_policy', 'unknown')}


def run_recipe_record(record: dict, application: str) -> dict:
    """The block stored in processing_runs.provenance_json['recipe']."""
    result = {'id': record['id'], 'kind': record['kind'], 'sha256': record['sha256'],
              'effective': record['effective'], 'pin_status': record.get('pin_status', 'unknown'),
              'application': application, 'provenance': record.get('provenance', 'unknown')}
    for key in ('pinned_at', 'historical_recipe', 'dedup_policy'):
        if key in record:
            result[key] = record[key]
    return result


def verify_run_recipe(provenance: dict) -> dict:
    """Recompute the hash of a run's recorded recipe for export or display."""
    recipe = provenance.get('recipe') if isinstance(provenance, dict) else None
    if not isinstance(recipe, dict):
        return {'status': 'not_recorded', 'recipe': 'unknown'}
    try:
        validate_effective(recipe.get('effective'))
        digest = sha256_json(recipe['effective'])
    except (RecipeError, TypeError, KeyError) as error:
        return {'status': 'invalid', 'reason': str(error), 'recorded_sha256': recipe.get('sha256')}
    return {'status': 'hash_matches' if digest == recipe.get('sha256') else 'hash_mismatch',
            'recorded_sha256': recipe.get('sha256'), 'computed_sha256': digest,
            'id': recipe.get('id'), 'pin_status': recipe.get('pin_status', 'unknown'),
            'application': recipe.get('application', 'unknown')}


def auto_per_file_transcribe_kwargs(effective: dict) -> dict:
    """Keyword arguments for one whole-file faster-whisper call.

    For the ``legacy`` preset this is exactly ``{'word_timestamps': True,
    'vad_filter': True}``, the call made before recipes existed.
    """
    kwargs = {'word_timestamps': effective['word_timestamps'], 'vad_filter': effective['vad']['enabled']}
    if effective['vad']['parameters'] is not None:
        kwargs['vad_parameters'] = dict(effective['vad']['parameters'])
    if effective['language'] is not None:
        kwargs['language'] = effective['language']
    kwargs.update(copy.deepcopy(effective['transcribe_options']))
    return kwargs
