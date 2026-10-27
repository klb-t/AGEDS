"""Which optional local media engines are usable on this host (no model loading)."""
from __future__ import annotations

import importlib.metadata
import importlib.util

from .diarization import PyannoteDiarizer
from .ocr import PopplerRasterizer, TesseractCLI
from .recipe import effective_recipe


def _module(name: str, distribution: str) -> dict:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return {'status': 'unavailable', 'reason': f'{name} is not importable (optional dependency not installed)'}
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        version = 'unknown'
    return {'status': 'importable', 'version': version,
            'note': 'importable does not mean model weights are present; inference is not attempted here'}


def media_capabilities() -> dict:
    ocr = effective_recipe(kind='ocr')
    diarization = effective_recipe(kind='diarization')
    return {
        'asr_faster_whisper': _module('faster_whisper', 'faster-whisper'),
        'ocr_tesseract': TesseractCLI().availability(ocr['effective']['languages']),
        'pdf_rasterizer': PopplerRasterizer().availability(),
        'diarization_pyannote': PyannoteDiarizer(diarization).availability(),
        'note': 'Availability is probed locally; no network, model download or inference is performed.',
    }
