"""Media processing area: ASR recipes and strategies, OCR, diarization, queue estimates.

Every model attempt remains one leased ``processing_runs`` row (see ``jobs``).
Model outputs, layout hypotheses and human speaker assignments stay separate
layers; nothing in this package edits original bytes or earlier versions.
"""
