"""Validated local ASR data, consumed by the existing adapter and run store.

This is not a queue resolver: legacy jobs still select the worker's current
recipe. Model revision and unspecified library options are not invented.
"""
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path

DEFAULT_RECIPE_PATH = Path(__file__).resolve().parents[1] / 'profiles/asr/default.json'


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate ASR recipe key')
        value[key] = item
    return value


@dataclass(frozen=True)
class AsrRecipe:
    id: str
    revision: int
    model: str
    device: str
    compute_type: str
    word_timestamps: bool
    vad_filter: bool

    def options(self):
        return {'word_timestamps': self.word_timestamps, 'vad_filter': self.vad_filter}

    def snapshot(self):
        return {'schema': 'ageds.asr_recipe/1', 'id': self.id, 'revision': self.revision,
                'model': self.model, 'device': self.device, 'compute_type': self.compute_type,
                'options': self.options()}

    @property
    def content_hash(self):
        return hashlib.sha256(json.dumps(self.snapshot(), ensure_ascii=False,
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()

    def with_runtime_settings(self, settings):
        # Existing EW_WHISPER_* / Settings values remain the model overlay.
        for value in (settings.whisper_model, settings.whisper_device, settings.whisper_compute_type):
            if not isinstance(value, str) or not value.strip():
                raise ValueError('ASR runtime setting must be a nonempty string')
        return replace(self, model=settings.whisper_model, device=settings.whisper_device,
                       compute_type=settings.whisper_compute_type)


def load_recipe(path=DEFAULT_RECIPE_PATH):
    value = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
    keys = {'schema', 'id', 'revision', 'model', 'device', 'compute_type', 'options'}
    if not isinstance(value, dict) or set(value) != keys or value['schema'] != 'ageds.asr_recipe/1':
        raise ValueError('Unsupported ASR recipe schema or fields')
    if type(value['revision']) is not int or value['revision'] < 1:
        raise ValueError('ASR recipe revision must be a positive integer')
    for key in ('id', 'model', 'device', 'compute_type'):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError('ASR recipe identity and model fields must be nonempty strings')
    options = value['options']
    if not isinstance(options, dict) or set(options) != {'word_timestamps', 'vad_filter'}:
        raise ValueError('Unsupported ASR recipe options')
    if any(type(option) is not bool for option in options.values()):
        raise ValueError('ASR recipe options must be booleans')
    return AsrRecipe(value['id'], value['revision'], value['model'], value['device'],
                     value['compute_type'], **options)
