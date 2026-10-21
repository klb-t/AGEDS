"""Fixed, hashed labels printed in every interpretive report.

The analysis area (E2) owns the canonical disclaimer; when its module is
importable its label is used after checking that the declared SHA-256 matches
the text. Otherwise the same wording is used from here and marked
``reports_fallback``. A label is never rewritten or paraphrased by a model.
"""
from __future__ import annotations

import importlib

from .common import sha256_text

INTERPRETIVE_ID = 'ageds.analysis-disclaimer/v1'
INTERPRETIVE = {
    'pl': 'Wynik analizatora jest materiałem pomocniczym dla człowieka/biegłego, nie wnioskiem przyczynowym.',
    'en': "The analyzer's result is auxiliary material for a human/expert, not a causal conclusion.",
}
UNOBSERVED_ID = 'ageds.reports.unobserved-label/v1'
UNOBSERVED = {
    'pl': 'Kategoria „nieobserwowane” oznacza okres bez danych; nie oznacza bezczynności ani braku zdarzeń.',
    'en': 'The category "unobserved" means a period without data; it does not mean inactivity or absence of events.',
}
CORRELATION_ID = 'ageds.reports.correlation-label/v1'
CORRELATION = {
    'pl': 'Współwystępowanie w czasie nie jest przyczynowością; gęstość dokumentacji może wytwarzać pozorne zależności.',
    'en': 'Co-occurrence in time is not causation; documentation density can produce apparent dependencies.',
}

CORRELATION_HYPOTHESIS_ID = 'ageds.reports.correlation-hypothesis-label/v1'
CORRELATION_HYPOTHESIS = {
    'pl': 'Hipotezy korelacji to propozycje reguł z dowodami dla każdego kryterium i decyzją człowieka; '
          'nie są faktami i nie mają prawdopodobieństwa.',
    'en': 'Correlation hypotheses are rule proposals with per-criterion evidence and a human decision; '
          'they are not facts and carry no probability.',
}


def interpretive(lang: str) -> dict:
    try:
        module = importlib.import_module('server.app.analysis.hypotheses')
    except ModuleNotFoundError:
        module = None
    upstream = getattr(module, 'disclaimer', None) if module else None
    if callable(upstream):
        value = upstream()
        text = value.get(lang) or value.get('pl')
        declared = value.get(f'sha256_{lang}') or value.get('sha256_pl')
        if not isinstance(text, str) or declared != sha256_text(text):
            raise RuntimeError('upstream disclaimer label does not match its declared SHA-256')
        return {'id': value.get('id', INTERPRETIVE_ID), 'text': text, 'sha256': declared,
                'source': 'server.app.analysis.hypotheses'}
    text = INTERPRETIVE.get(lang, INTERPRETIVE['pl'])
    return {'id': INTERPRETIVE_ID, 'text': text, 'sha256': sha256_text(text), 'source': 'reports_fallback'}


def fixed(identifier: str, texts: dict, lang: str) -> dict:
    text = texts.get(lang, texts['pl'])
    return {'id': identifier, 'text': text, 'sha256': sha256_text(text), 'source': 'reports'}
