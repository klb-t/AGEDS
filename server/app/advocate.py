"""Evidence advocate core (package P6): authorization before any model.

Content reaches an answer only through `AdvocateGate`, which admits an object
when the principal may both read its text and talk to the advocate about it.
This module never queries evidence tables itself (a static test checks it).

An answer is a list of claims. Each claim has a status label — a label in
data, not a numeric certainty — and citations to entries of the answer's
basis. The basis pins material versions (artifact sha256, text version,
segment) and its inputs form the answer's derived scope: reading the answer
later requires every input to be readable again. Quoted material is stored
text (for audio: ASR output); it is not verified truth, authorship or
acoustic alignment.

Without a configured provider the advocate runs `retrieval_only`: it returns
the gated passages themselves and generates no text.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata

from . import authz
from .assignments import new_uid
from .search import SearchQueryError, scoped_search_page

# Presets; unknown labels are kept and reported, never upgraded.
CLAIM_STATUSES = ('quoted_material', 'proven', 'claim', 'hypothesis', 'unknown', 'claim_without_basis')
MAX_QUESTION_CHARS = 4000
MAX_PASSAGES = 8
MAX_TERMS = 12
PASSAGE_WINDOW = 360
CITATION = re.compile(r'\[cit:([A-Za-z0-9_-]{1,32})\]')


class AdvocateError(ValueError):
    """Input the advocate refuses; never carries hidden content."""


class AdvocateGate(authz.ContentGate):
    """Admit only what is readable AND open to advocate conversations."""

    def allows(self, kind: str, object_id, capability: str) -> bool:
        return (super().allows(kind, object_id, capability)
                and super().allows(kind, object_id, 'chat_with_advocate'))

    def can_chat(self) -> bool:
        cover = self.coverage('chat_with_advocate')
        return bool(cover.installation or cover.cases or cover.objects or cover.frozen or cover.legacy or cover.scopes)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _fold(text: str) -> str:
    """Case and diacritic folding comparable to the FTS tokenizer's remove_diacritics."""
    decomposed = unicodedata.normalize('NFKD', text.casefold())
    return ''.join(c for c in decomposed if not unicodedata.combining(c)).replace('ł', 'l')


def query_terms(question: str) -> list[str]:
    seen, terms = set(), []
    for word in re.findall(r'\w{3,}', question, flags=re.UNICODE):
        folded = _fold(word)
        if folded not in seen:
            seen.add(folded)
            terms.append(word)
        if len(terms) >= MAX_TERMS:
            break
    return terms


def fts_query(terms: list[str]) -> str:
    """OR of quoted prefixes; a short prefix tolerates Polish inflection."""
    parts = []
    for term in terms:
        stem = term if len(term) <= 5 else term[:max(5, len(term) - 3)]
        parts.append('"' + stem.replace('"', '""') + '"*')
    return ' OR '.join(parts)


def _matches(text: str, terms: list[str]) -> bool:
    folded = _fold(text)
    return any(_fold(term if len(term) <= 5 else term[:max(5, len(term) - 3)]) in folded for term in terms)


def _window(text: str, terms: list[str]) -> tuple[int, int]:
    folded = _fold(text)
    positions = [folded.find(_fold(t if len(t) <= 5 else t[:max(5, len(t) - 3)])) for t in terms]
    positions = [p for p in positions if p >= 0]
    start = max(0, (min(positions) if positions else 0) - PASSAGE_WINDOW // 3)
    return start, min(len(text), start + PASSAGE_WINDOW)


def _artifact_ref(row: dict) -> dict:
    return {'id': row['id'], 'uid': row['uid'], 'sha256': row['sha256'], 'name': row['original_name']}


def retrieve(gate: AdvocateGate, question: str) -> list[dict]:
    """Gated passages for a question; every entry pins the version it quotes."""
    terms = query_terms(question)
    if not terms:
        return []
    try:
        page = scoped_search_page(fts_query(terms), 50, gate)
    except SearchQueryError:
        return []
    basis: list[dict] = []
    artifacts_seen: dict[int, dict] = {}
    for hit in page['results']:
        if len(basis) >= MAX_PASSAGES:
            break
        kind, object_id = hit['object_kind'], int(hit['object_id'])
        if kind == 'derived_text':
            text_row = gate.get('derived_text', object_id, 'read_text')
            if not gate.allows('artifact', text_row['artifact_id'], 'read_text'):
                continue
            artifact = artifacts_seen.setdefault(text_row['artifact_id'],
                                                 gate.get('artifact', text_row['artifact_id'], 'read_text'))
            segments = json.loads(text_row['segments_json'] or '[]')
            chosen = [(i, s) for i, s in enumerate(segments)
                      if isinstance(s, dict) and isinstance(s.get('text'), str) and _matches(s['text'], terms)]
            if chosen:
                for index, segment in chosen[:3]:
                    quote = segment['text'].strip()
                    basis.append({
                        'kind': 'transcript_segment', 'object': ['derived_text', object_id],
                        'artifact': _artifact_ref(artifact), 'derived_text_id': object_id,
                        'text_kind': text_row['kind'], 'model': text_row['model'],
                        'segment_index': index,
                        'start_ms': round(float(segment.get('start', 0)) * 1000),
                        'end_ms': round(float(segment.get('end', 0)) * 1000),
                        'text': quote, 'text_sha256': _sha(quote)})
            else:
                start, end = _window(text_row['text'], terms)
                quote = text_row['text'][start:end].strip()
                basis.append({
                    'kind': 'text_window', 'object': ['derived_text', object_id],
                    'artifact': _artifact_ref(artifact), 'derived_text_id': object_id,
                    'text_kind': text_row['kind'], 'model': text_row['model'],
                    'char_start': start, 'char_end': end, 'text': quote, 'text_sha256': _sha(quote)})
        elif kind == 'event':
            event = gate.get('event', object_id, 'read_text')
            quote = (event['body'] or event['subject'] or '').strip()
            if quote:
                basis.append({'kind': 'event', 'object': ['event', object_id],
                              'event_type': event['event_type'], 'ts_start': event['ts_start'],
                              'direction': event['direction'], 'text': quote[:PASSAGE_WINDOW * 2],
                              'text_sha256': _sha(quote[:PASSAGE_WINDOW * 2])})
    # Fragments shared on their own (fragment.open_policy) are reachable even when their text is not.
    for fragment in gate.list('fragment', 'read_text'):
        if len(basis) >= MAX_PASSAGES:
            break
        if gate.allows('derived_text', fragment['derived_text_id'], 'read_text') if fragment['derived_text_id'] else False:
            continue  # already searchable as text
        context = authz.fragment_context(gate.db, gate.principal_id, fragment['id'], gate=gate)
        if not context or not context['quote']:
            continue
        around = ' '.join(item['text'].strip() for item in context['context'])
        if not _matches(context['quote'] + ' ' + around, terms):
            continue
        entry = {'kind': 'fragment_quote', 'object': ['fragment', fragment['id']], 'text': context['quote'].strip(),
                 'text_sha256': _sha(context['quote'].strip()), 'derived_text_id': context['derived_text_id'],
                 'note': 'fragment shared on its own; the rest of the document is not shared'}
        if 'start_ms' in context:
            entry.update(start_ms=context['start_ms'], end_ms=context['end_ms'])
        if context['context']:
            entry['context'] = around
        basis.append(entry)
    # Case-scoped meanings of the cited material, labelled as interpretation.
    for artifact_id in artifacts_seen:
        for assignment in gate.list('assignment', 'read_text',
                                    where='WHERE target_kind=? AND target_id=? AND withdrawn_at IS NULL',
                                    params=('artifact', artifact_id)):
            if assignment['meaning_text']:
                basis.append({'kind': 'assignment', 'object': ['assignment', assignment['id']],
                              'artifact_id': artifact_id, 'author_kind': assignment['author_kind'],
                              'status': assignment['status'], 'text': assignment['meaning_text'],
                              'text_sha256': _sha(assignment['meaning_text']),
                              'note': 'interpretation recorded in a case, not source text'})
    if authz.get_setting(gate.db, 'shared_source.cross_case_hint', 'never') == 'own_cases_only':
        # Name only cases this principal can open; others are never hinted at.
        for entry in basis:
            if 'artifact' not in entry:
                continue
            cases = {a['case_id'] for a in gate.list('assignment', 'read_metadata',
                     where='WHERE target_kind=? AND target_id=? AND withdrawn_at IS NULL',
                     params=('artifact', entry['artifact']['id']))}
            names = sorted(gate.get('case', c, 'read_metadata')['name'] for c in cases
                           if authz.ContentGate.allows(gate, 'case', c, 'read_metadata'))
            if len(names) > 1:
                entry['also_in_cases'] = names
    for number, entry in enumerate(basis, 1):
        entry['ref'] = f'p{number}'
    return basis


def derived_scope(basis: list[dict]) -> list:
    inputs = set()
    for entry in basis:
        inputs.add(tuple(entry['object']))
        if 'artifact' in entry:
            inputs.add(('artifact', entry['artifact']['id']))
    return sorted([list(item) for item in inputs])


def verify(claims, basis: list[dict], gate: AdvocateGate) -> tuple[list[dict], dict]:
    """Keep only citations into the basis that are still readable; never upgrade a status."""
    by_ref = {entry['ref']: entry for entry in basis}
    checked, removed, warnings = [], [], []
    for position, claim in enumerate(claims if isinstance(claims, list) else []):
        if not isinstance(claim, dict) or not isinstance(claim.get('text'), str):
            warnings.append({'code': 'malformed_claim', 'position': position})
            continue
        cited = list(claim.get('citations') or []) + CITATION.findall(claim['text'])
        valid = []
        for ref in dict.fromkeys(str(r) for r in cited):
            entry = by_ref.get(ref)
            if entry is None or not gate.allows(entry['object'][0], entry['object'][1], 'read_text'):
                removed.append({'position': position, 'ref': ref, 'code': 'citation_outside_basis'})
            else:
                valid.append(ref)
        status = claim.get('status') if isinstance(claim.get('status'), str) else 'unknown'
        if status not in CLAIM_STATUSES:
            warnings.append({'code': 'status_not_in_presets', 'position': position, 'status': status})
        if status in ('proven', 'quoted_material') and not valid:
            status = 'claim_without_basis'
        checked.append({'text': CITATION.sub('', claim['text']).strip(), 'status': status, 'citations': valid})
    return checked, {'removed_citations': removed, 'warnings': warnings,
                     'statuses_are_labels_not_probabilities': True}


# ---------------------------------------------------------------- conversations

def _conversation(db: sqlite3.Connection, gate: AdvocateGate, conversation_uid: str) -> sqlite3.Row:
    row = db.execute('SELECT * FROM conversations WHERE uid=?', (conversation_uid,)).fetchone()
    if row is None or not authz.ContentGate.allows(gate, 'conversation', row['id'], 'read_metadata'):
        raise authz.Denied('conversation', conversation_uid, 'read_metadata', exists=row is not None)
    return row


def start_conversation(db: sqlite3.Connection, principal_id: int, *, title: str | None = None) -> dict:
    gate = AdvocateGate(db, principal_id)
    if not gate.can_chat():
        raise authz.Denied('advocate', None, 'chat_with_advocate', exists=True)
    owner_visibility = authz.get_setting(db, 'conversation.owner_visibility', 'none')
    uid = new_uid()
    cur = db.execute('INSERT INTO conversations(uid,principal_id,title,settings_json) VALUES(?,?,?,?)',
                     (uid, principal_id, title, _json({'owner_visibility': owner_visibility})))
    return {'id': int(cur.lastrowid), 'uid': uid, 'owner_visibility': owner_visibility}


def ask(db: sqlite3.Connection, principal_id: int, conversation_uid: str, question: str, *, provider=None) -> dict:
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise AdvocateError(f'question must be 1..{MAX_QUESTION_CHARS} characters')
    gate = AdvocateGate(db, principal_id)
    if not gate.can_chat():
        raise authz.Denied('advocate', None, 'chat_with_advocate', exists=True)
    conversation = _conversation(db, gate, conversation_uid)
    if conversation['principal_id'] != principal_id:
        # Seeing that a conversation exists (owner visibility) never allows writing into it.
        raise authz.Denied('conversation', conversation_uid, 'write', exists=True)
    basis = retrieve(gate, question)
    turn_uid = new_uid()
    turn = db.execute('''INSERT INTO advocate_turns(uid,conversation_id,question,retrieval_json,asked_by_principal_id)
                         VALUES(?,?,?,?,?)''',
                      (turn_uid, conversation['id'], question,
                       _json({'terms': query_terms(question), 'passages': len(basis)}), principal_id)).lastrowid
    authz._audit(db, 'advocate_turn', 'advocate_turn', turn, {'uid': turn_uid, 'conversation_uid': conversation_uid},
                 db.execute('SELECT uid FROM principals WHERE id=?', (principal_id,)).fetchone()['uid'])
    provider_call_id, mode, fallback, claims = None, None, None, []
    if provider is not None:
        from .providers import ProviderUnavailable
        history = conversation_history(db, principal_id, conversation_uid, for_prompt=True)
        try:
            claims, provider_call_id = provider.answer(db, question=question, basis=basis, history=history,
                                                       conversation_id=conversation['id'])
            mode = f'provider:{provider.name}'
        except ProviderUnavailable as exc:
            fallback = str(exc)[:300]
    if mode is None:
        mode = 'retrieval_only'
        claims = [{'text': entry['text'], 'status': 'quoted_material' if entry['kind'] != 'assignment' else 'claim',
                   'citations': [entry['ref']]} for entry in basis]
    claims, verification = verify(claims, basis, gate)
    if fallback:
        verification['provider_fallback'] = fallback
    scope = derived_scope(basis)
    answer_uid = new_uid()
    db.execute('''INSERT INTO advocate_answers(uid,turn_id,mode,claims_json,basis_json,derived_scope_json,
                  basis_sha256,verification_json,provider_call_id) VALUES(?,?,?,?,?,?,?,?,?)''',
               (answer_uid, turn, mode, _json(claims), _json(basis), _json(scope), _sha(_json(basis)),
                _json(verification), provider_call_id))
    return {'turn_uid': turn_uid, 'answer_uid': answer_uid, 'mode': mode, 'claims': claims,
            'basis': basis, 'verification': verification}


def conversation_history(db: sqlite3.Connection, principal_id: int, conversation_uid: str, *,
                         for_prompt: bool = False) -> list[dict]:
    """Turns re-authorized now; an answer whose inputs are no longer readable is withheld."""
    gate = AdvocateGate(db, principal_id)
    conversation = _conversation(db, gate, conversation_uid)
    turns = []
    for turn in db.execute('SELECT * FROM advocate_turns WHERE conversation_id=? ORDER BY id', (conversation['id'],)):
        if not authz.ContentGate.allows(gate, 'advocate_turn', turn['id'], 'read_text'):
            # metadata_only: the turn exists and when, never what was asked or answered.
            count = db.execute('SELECT count(*) FROM advocate_answers WHERE turn_id=?', (turn['id'],)).fetchone()[0]
            turns.append({'turn_uid': turn['uid'], 'created_at': turn['created_at'], 'question': None,
                          'content_withheld': True, 'answer_count': count, 'answers': []})
            continue
        item = {'turn_uid': turn['uid'], 'question': turn['question'], 'created_at': turn['created_at'], 'answers': []}
        for answer in db.execute('SELECT * FROM advocate_answers WHERE turn_id=? ORDER BY id', (turn['id'],)):
            readable = authz.visible(db, principal_id, 'advocate_answer', answer['id'], 'read_text') and all(
                gate.allows(kind, object_id, 'read_text') for kind, object_id in json.loads(answer['derived_scope_json']))
            if readable:
                item['answers'].append({'answer_uid': answer['uid'], 'mode': answer['mode'],
                                        'claims': json.loads(answer['claims_json']),
                                        'verification': json.loads(answer['verification_json']),
                                        'basis_sha256': answer['basis_sha256'], 'created_at': answer['created_at']})
                if for_prompt:
                    # The provider re-checks consent for exactly these inputs before resending them.
                    item['answers'][-1]['derived_scope'] = json.loads(answer['derived_scope_json'])
            else:
                item['answers'].append({'answer_uid': answer['uid'], 'unavailable': True,
                                        'reason': 'material_no_longer_available'})
        turns.append(item)
    return turns


def answer_basis(db: sqlite3.Connection, principal_id: int, answer_uid: str) -> dict:
    """The pinned basis, each entry re-authorized now through the same gate."""
    gate = AdvocateGate(db, principal_id)
    row = db.execute('SELECT * FROM advocate_answers WHERE uid=?', (answer_uid,)).fetchone()
    # The answer must belong to a conversation this principal may open; each
    # basis entry is then checked on its own, so a revoked input shows as unavailable.
    if row is None or not authz.ContentGate.allows(gate, 'advocate_turn', row['turn_id'], 'read_metadata'):
        raise authz.Denied('advocate_answer', answer_uid, 'read_metadata', exists=row is not None)
    entries = []
    for entry in json.loads(row['basis_json']):
        kind, object_id = entry['object']
        if gate.allows(kind, object_id, 'read_text') and (
                'artifact' not in entry or gate.allows('artifact', entry['artifact']['id'], 'read_text')):
            entries.append(entry)
        else:
            entries.append({'ref': entry['ref'], 'unavailable': True})
    return {'answer_uid': answer_uid, 'basis_sha256': row['basis_sha256'], 'entries': entries,
            'note': 'quoted text is stored derived text (ASR, OCR, import); not verified truth or authorship'}


def record_derived_state(db: sqlite3.Connection, principal_id: int, kind: str, content, input_refs: list,
                         *, transformation: dict | None = None, provider_call_id: int | None = None) -> dict:
    """Store a summary or analysis; it is visible only to principals who can read every input."""
    gate = AdvocateGate(db, principal_id)
    refs = sorted({(str(k), int(i)) for k, i in input_refs})
    if not refs:
        raise AdvocateError('a derived state needs at least one input')
    for input_kind, input_id in refs:
        if not gate.allows(input_kind, input_id, 'read_text'):
            raise authz.Denied(input_kind, input_id, 'read_text', exists=True)
    uid = new_uid()
    cur = db.execute('''INSERT INTO derived_states(uid,kind,created_by_principal_id,content_json,input_refs_json,
                        transformation_json,provider_call_id) VALUES(?,?,?,?,?,?,?)''',
                     (uid, kind, principal_id, _json(content), _json([list(r) for r in refs]),
                      _json(transformation or {}), provider_call_id))
    return {'id': int(cur.lastrowid), 'uid': uid}


def summarize(db: sqlite3.Connection, principal_id: int, topic: str, *, provider) -> dict:
    """A model summary of the gated passages about a topic, stored as a derived state.

    It needs a provider: without a model there is no summary, only passages
    (ask). The stored summary is visible to a principal only while every input
    is readable for them (derived_scope), whoever created it.
    """
    if not isinstance(topic, str) or not topic.strip() or len(topic) > MAX_QUESTION_CHARS:
        raise AdvocateError(f'topic must be 1..{MAX_QUESTION_CHARS} characters')
    if provider is None:
        raise AdvocateError('summaries need a configured model provider; ask returns the passages')
    from .providers import ProviderUnavailable
    gate = AdvocateGate(db, principal_id)
    if not gate.can_chat():
        raise authz.Denied('advocate', None, 'chat_with_advocate', exists=True)
    basis = retrieve(gate, topic)
    try:
        claims, call_id = provider.answer(db, question=f'Streść, co udostępniony materiał mówi o: {topic}',
                                          basis=basis, history=[], conversation_id=None)
    except ProviderUnavailable as exc:
        raise AdvocateError(f'summary not created: {str(exc)[:300]}') from exc
    claims, verification = verify(claims, basis, gate)
    scope = derived_scope(basis)
    stored = record_derived_state(db, principal_id, 'advocate_summary',
                                  {'topic': topic, 'claims': claims, 'basis': basis, 'verification': verification},
                                  [tuple(item) for item in scope],
                                  transformation={'provider': provider.name, 'model': provider.config['model'],
                                                  'sharing': authz.get_setting(db, 'summary.sharing', 'creator_only'),
                                                  'owner_visibility': authz.get_setting(db, 'conversation.owner_visibility', 'none')},
                                  provider_call_id=call_id)
    return {'uid': stored['uid'], 'topic': topic, 'claims': claims, 'basis': basis, 'verification': verification}


def visible_summaries(db: sqlite3.Connection, principal_id: int) -> list[dict]:
    gate = AdvocateGate(db, principal_id)
    result = []
    for row in gate.list('derived_state', 'read_text', where="WHERE kind='advocate_summary'"):
        content = json.loads(row['content_json'])
        # Cross-case hints were computed from the creator's view; never pass them on.
        basis = [{k: v for k, v in entry.items() if k != 'also_in_cases'} for entry in content['basis']]
        result.append({'uid': row['uid'], 'topic': content['topic'], 'claims': content['claims'],
                       'basis': basis, 'created_at': row['created_at'], 'own': row['created_by_principal_id'] == principal_id,
                       'model': json.loads(row['transformation_json']).get('model')})
    return result


def share_with_owner(db: sqlite3.Connection, principal_id: int, conversation_uid: str, shared: bool) -> dict:
    """opt_in_by_recipient: only the conversation's own principal may open or close it to the owner."""
    row = db.execute('SELECT * FROM conversations WHERE uid=?', (conversation_uid,)).fetchone()
    if row is None or row['principal_id'] != principal_id:
        raise authz.Denied('conversation', conversation_uid, 'share', exists=row is not None)
    settings = json.loads(row['settings_json'] or '{}')
    if settings.get('owner_visibility') != 'opt_in_by_recipient':
        raise AdvocateError('this conversation was not started with opt-in visibility')
    settings['shared_with_owner'] = bool(shared)
    db.execute('UPDATE conversations SET settings_json=? WHERE id=?', (_json(settings), row['id']))
    authz._audit(db, 'conversation_shared' if shared else 'conversation_unshared', 'conversation', row['id'],
                 {'uid': conversation_uid}, None)
    return {'conversation_uid': conversation_uid, 'shared_with_owner': bool(shared)}
