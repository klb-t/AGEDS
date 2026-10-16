"""Review 2 (AGEDS-TASK-20261016-R2) P3: a current thesis whose only pointers are candidates or rejected items has
no accepted evidence; the disclosure counts it (additively, next to theses without any pointer)."""
from __future__ import annotations

from server.app.analysis import dossier, theses
from server.tests.test_hypotheses_dossier import env  # noqa: F401 (fixture)


def test_review2_p3_theses_without_accepted_evidence_are_disclosed(env):  # noqa: F811
    db, hid = env['db'], env['hid']
    rejected = dossier.add_item(db, hid, 'anchor', env['anchor'], classification='rejected')
    candidate = dossier.add_item(db, hid, 'artifact', env['artifact'])
    accepted = dossier.add_item(db, hid, 'annotation', env['annotation'], classification='supporting')
    only_rejected = theses.add_thesis(db, hid, 'Pointer to a rejected item only.', 'speculation', [rejected['item_id']])
    only_candidate = theses.add_thesis(db, hid, 'Pointer to an unreviewed candidate.', 'speculation', [candidate['item_id']])
    none = theses.add_thesis(db, hid, 'No pointer at all.', 'speculation', [])
    supported = theses.add_thesis(db, hid, 'Accepted evidence.', 'speculation', [accepted['item_id']])
    assert only_rejected['flags'] == ['evidence_not_accepted'] and not only_rejected['unsupported']
    assert only_candidate['flags'] == ['evidence_not_accepted'] and none['unsupported'] and supported['flags'] == []
    listing = theses.list_theses(db, hid)
    assert listing['unsupported_current'] == 1
    assert listing['without_accepted_evidence_current'] == 3  # pre-fix: only the pointer-less thesis was counted
    quadrant = next(q for q in theses.disclosure_view(db, hid)['quadrants'] if q['quadrant'] == 'known_unknowns')
    facts = {f['fact']: f['value'] for f in quadrant['computed_facts']}
    assert facts['current_theses_without_evidence'] == 1
    assert facts['current_theses_without_accepted_evidence'] == 3
