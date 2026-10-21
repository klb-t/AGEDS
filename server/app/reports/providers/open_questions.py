"""Open questions: what a person typed (human prose, shown as such) plus gaps
the code can count (unknown fields and exclusions from earlier sections).

The code-detected list contains only counts and names produced by providers;
no sentence is generated from model output.
"""
from __future__ import annotations

from ..blocks import notice, prose, table
from ..labels import label
from . import Section

VERSION = 'open_questions.v1'


def provide(ctx) -> dict:
    section = Section(ctx, 'open_questions', __name__, VERSION, 'section_open_questions')
    questions = ctx.params['open_questions']
    for index, text in enumerate(questions):
        section.block(prose(f'open_questions.human.{index}', 'human_input', text, {}, author='human'))
    if questions:
        section.block(notice('open_questions.human_note', 'info', label(ctx.lang, 'human_input_note')))
    detected = []
    for earlier in ctx.scope.get('_sections', []):
        for name in earlier['unknowns']:
            detected.append({'section': earlier['section'], 'kind': 'unknown', 'code': name, 'value': None})
        for name, value in earlier['exclusions'].items():
            if value not in (0, False, None, ''):
                detected.append({'section': earlier['section'], 'kind': 'exclusion', 'code': name, 'value': value})
        if earlier['status'] in ('unavailable', 'error', 'partial'):
            detected.append({'section': earlier['section'], 'kind': 'status', 'code': earlier['status'],
                             'value': earlier['reason']})
    max_rows = ctx.params['max_rows']
    section.block(table('open_questions.detected', [('section', 'text'), ('kind', 'text'), ('code', 'text'),
                                                    ('value', 'text')], detected[:max_rows], ctx.lang,
                        title_key='detected_gaps', total_rows=len(detected), truncated=len(detected) > max_rows))
    if not questions and not detected:
        section.status('empty', 'no questions entered and no gaps detected')
    return section.result()
