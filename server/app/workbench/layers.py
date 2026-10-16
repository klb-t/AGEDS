"""Search layers and their epistemic labels.

The classification is deterministic code, not a model judgment. A derived
text kind that is not listed here is reported as ``derived_other`` with the
epistemic label ``unclassified``; it is never guessed into another layer.
"""
from __future__ import annotations

# Epistemic labels: what kind of statement the matched text is.
RAW_SOURCE_FIELD = 'raw_source_field'      # value as received/observed at acquisition (names, locators, client metadata)
PARSER_OUTPUT = 'parser_output'            # projection of a raw record by a deterministic parser
MODEL_OUTPUT = 'model_output'              # ASR/OCR/other model inference; a run may be recorded or legacy-unknown
HUMAN_ANNOTATION = 'human_annotation'      # a person's note, label or interpretation
UNCLASSIFIED = 'unclassified'              # stored kind unknown to this classification

EPISTEMIC_LABELS = (RAW_SOURCE_FIELD, PARSER_OUTPUT, MODEL_OUTPUT, HUMAN_ANNOTATION, UNCLASSIFIED)

# Derived text kinds by layer. Kinds written today: 'transcript' (ASR), 'ocr' (media area).
DERIVED_KIND_LAYERS: dict[str, str] = {
    'transcript': 'transcript',
    'asr': 'transcript',
    'ocr': 'ocr',
    'parsed_text': 'parsed_text',
    'parser_text': 'parsed_text',
    'extracted_text': 'parsed_text',
    'text_extraction': 'parsed_text',
}

LAYERS: dict[str, dict] = {
    'artifact_metadata': {'object_kind': 'artifact', 'epistemic': RAW_SOURCE_FIELD,
                          'fields': 'original_name, source_locator, mime_type, metadata_json'},
    'event': {'object_kind': 'event', 'epistemic': PARSER_OUTPUT,
              'fields': 'event_type, contact_label, phone_or_address, sender, recipients, subject, body'},
    'transcript': {'object_kind': 'derived_text', 'epistemic': MODEL_OUTPUT, 'fields': 'derived_text.text'},
    'ocr': {'object_kind': 'derived_text', 'epistemic': MODEL_OUTPUT, 'fields': 'derived_text.text'},
    'parsed_text': {'object_kind': 'derived_text', 'epistemic': PARSER_OUTPUT, 'fields': 'derived_text.text'},
    'derived_other': {'object_kind': 'derived_text', 'epistemic': UNCLASSIFIED, 'fields': 'derived_text.text'},
    'annotation': {'object_kind': 'annotation', 'epistemic': HUMAN_ANNOTATION, 'fields': 'label, body'},
}
LAYER_NAMES = tuple(LAYERS)


def layer_of(object_kind: str, subkind: str | None) -> str:
    if object_kind == 'artifact':
        return 'artifact_metadata'
    if object_kind == 'event':
        return 'event'
    if object_kind == 'annotation':
        return 'annotation'
    if object_kind == 'derived_text':
        return DERIVED_KIND_LAYERS.get(subkind or '', 'derived_other')
    raise ValueError(f'unknown indexed object kind: {object_kind}')


def epistemic_of(layer: str) -> str:
    return LAYERS[layer]['epistemic']


def layer_predicate(layer: str) -> tuple[str, list]:
    """SQL predicate over alias ``d`` (workbench_search_docs) and its parameters."""
    spec = LAYERS[layer]
    kind = spec['object_kind']
    if kind != 'derived_text':
        return 'd.object_kind=?', [kind]
    if layer == 'derived_other':
        known = sorted(DERIVED_KIND_LAYERS)
        marks = ','.join('?' * len(known))
        return f"(d.object_kind='derived_text' AND (d.subkind IS NULL OR d.subkind NOT IN ({marks})))", known
    kinds = sorted(k for k, v in DERIVED_KIND_LAYERS.items() if v == layer)
    marks = ','.join('?' * len(kinds))
    return f"(d.object_kind='derived_text' AND d.subkind IN ({marks}))", kinds


def legend() -> list[dict]:
    return [{'layer': name, 'epistemic_layer': spec['epistemic'], 'object_kind': spec['object_kind'],
             'indexed_fields': spec['fields']} for name, spec in LAYERS.items()]
