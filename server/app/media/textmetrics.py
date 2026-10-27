"""Declared text normalization and Levenshtein alignment counts (WER/CER by code).

Normalization ``ageds-asr-eval-norm/1`` (applied identically to references and
hypotheses; versioned so a later rule change is a new id):

1. Unicode NFC; then ``str.casefold()``; then NFC again.
2. Characters of Unicode category P* (punctuation) become a space, except an
   apostrophe (U+0027 or U+2019) between two letters, kept as U+0027
   (``zo'n``, ``z'n``, ``it's``), and a comma, period or colon between two
   digits, kept as written (``3,5``, ``1.000``, ``10:42``).
3. Separators (Z*) and ASCII whitespace become a space; format characters (Cf,
   e.g. soft hyphen, zero-width joiner) and other control characters are removed.
4. Whitespace runs collapse to one space; leading/trailing space is removed.

Diacritics are kept (``zażółć`` != ``zazolc``; ``één`` != ``een``). Digits and
symbols are kept; numbers are not spelled out and spelling variants are not
merged. WER uses whitespace tokens of the normalized text; CER uses its
characters including the single spaces between words.

Alignment: dynamic-programming Levenshtein with unit costs. Among equal-cost
paths the tie-break order is match/substitution, then deletion, then
insertion, so S/D/I counts are deterministic; the total is the minimal edit
distance either way.
"""
from __future__ import annotations

import unicodedata

NORMALIZATION_ID = 'ageds-asr-eval-norm/1'
NORMALIZATION_RULES = (
    'Unicode NFC, casefold, NFC',
    'punctuation (P*) -> space, except apostrophe U+0027/U+2019 between letters kept as U+0027 '
    'and , . : between digits kept as written',
    'separators (Z*) and ASCII whitespace -> space; Cf/Cc characters removed',
    'collapse whitespace, strip',
    'diacritics, digits and symbols kept; no number or spelling normalization',
)
APOSTROPHES = {"'", '’'}
NUMBER_SEPARATORS = {',', '.', ':'}
DEFAULT_MAX_CELLS = 4_000_000


class AlignmentLimit(ValueError):
    """The alignment matrix would exceed the declared cell budget."""


def normalize_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError('text must be a string')
    value = unicodedata.normalize('NFC', unicodedata.normalize('NFC', text).casefold())
    out = []
    for index, char in enumerate(value):
        category = unicodedata.category(char)
        if category.startswith('P'):
            before = value[index - 1] if index else ''
            after = value[index + 1] if index + 1 < len(value) else ''
            if char in APOSTROPHES and before.isalpha() and after.isalpha():
                out.append("'")
            elif char in NUMBER_SEPARATORS and before.isdigit() and after.isdigit():
                out.append(char)
            else:
                out.append(' ')
        elif category.startswith('Z') or char in '\t\n\r\f\v':
            out.append(' ')
        elif category in ('Cf', 'Cc'):
            continue
        else:
            out.append(char)
    return ' '.join(''.join(out).split())


def align_counts(reference, hypothesis, *, max_cells: int = DEFAULT_MAX_CELLS) -> dict:
    """Minimal edit distance with deterministic substitution/deletion/insertion counts."""
    n, m = len(reference), len(hypothesis)
    if n * m > max_cells:
        raise AlignmentLimit(f'alignment needs {n * m} cells; limit is {max_cells}')
    # Each cell: (cost, substitutions, deletions, insertions).
    previous = [(j, 0, 0, j) for j in range(m + 1)]
    for i in range(1, n + 1):
        current = [(i, 0, i, 0)]
        ref_item = reference[i - 1]
        for j in range(1, m + 1):
            diagonal = previous[j - 1]
            if ref_item == hypothesis[j - 1]:
                best = diagonal
            else:
                best = (diagonal[0] + 1, diagonal[1] + 1, diagonal[2], diagonal[3])
            up = previous[j]
            if up[0] + 1 < best[0]:
                best = (up[0] + 1, up[1], up[2] + 1, up[3])
            left = current[j - 1]
            if left[0] + 1 < best[0]:
                best = (left[0] + 1, left[1], left[2], left[3] + 1)
            current.append(best)
        previous = current
    cost, substitutions, deletions, insertions = previous[m]
    return {'edits': cost, 'substitutions': substitutions, 'deletions': deletions, 'insertions': insertions,
            'hits': n - substitutions - deletions, 'reference_length': n, 'hypothesis_length': m}


def error_rates(reference: str, hypothesis: str, *, max_cells: int = DEFAULT_MAX_CELLS) -> dict:
    """WER and CER after normalization; an empty reference has no defined rate."""
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    result = {'normalization': NORMALIZATION_ID, 'reference_normalized': ref, 'hypothesis_normalized': hyp}
    for name, unit, ref_items, hyp_items in (('word', 'wer', ref.split(), hyp.split()), ('char', 'cer', list(ref), list(hyp))):
        try:
            counts = align_counts(ref_items, hyp_items, max_cells=max_cells)
        except AlignmentLimit as error:
            result[name] = {'status': 'not_computed', 'reason': str(error),
                            'reference_length': len(ref_items), 'hypothesis_length': len(hyp_items)}
            continue
        if counts['reference_length']:
            counts[unit] = counts['edits'] / counts['reference_length']
            counts['status'] = 'computed'
        else:
            counts[unit] = None
            counts['status'] = 'undefined_empty_reference'
            counts['reason'] = f'empty reference; {counts["insertions"]} inserted {name}s counted'
        result[name] = counts
    return result
