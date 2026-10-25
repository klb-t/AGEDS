"""Model providers for the advocate (package P8). None is enabled by default.

A provider is a setting (`advocate.provider`). It receives a prompt only when
(1) it is configured, (2) a recorded, unrevoked provider consent covers every
basis input of the turn, and (3) the day's recorded spend plus this call's
worst case stays within `advocate.daily_budget_eur`. Otherwise the turn falls
back to retrieval_only and says why. What is sent is exactly the gated basis
and the re-authorized conversation history; each call is recorded with the
prompt hash and input references (content only if `advocate.log_prompt_content`).

Prices are data. The preset for gemini-3.5-flash comes from third-party price
aggregators and is marked unverified; the EUR/USD preset 1.0 overstates cost.
A call whose usage is not reported is booked at its worst case, never zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request

from . import authz
from .assignments import new_uid

VERTEX_PRESET = {
    'name': 'vertex', 'type': 'vertex_gemini', 'location': 'global', 'model': 'gemini-3.5-flash',
    'temperature': 0.2, 'max_output_tokens': 2048, 'timeout_seconds': 120,
    'price_usd_per_million': {'input': 1.50, 'output': 9.00,
                              'source': 'third-party aggregators (getmaxim.ai, typingmind), unverified against Google, 2026-10-25'},
    'eur_per_usd': 1.0, 'chars_per_token': 3.0,
}
SYSTEM_PROMPT = (
    'Jesteś rzecznikiem materiału dowodowego. Odpowiadasz osobie uprawnionej (np. adwokatowi) wyłącznie na '
    'podstawie przekazanych fragmentów. Każdy fragment ma identyfikator (p1, p2, ...). Fragmenty to zapisany '
    'tekst (np. transkrypcja ASR, import wiadomości) albo interpretacja przypisana w sprawie; nie są '
    'zweryfikowaną prawdą. Nie dodawaj faktów spoza fragmentów. Gdy fragmenty nie wystarczają, powiedz to. '
    'Odpowiadaj w języku pytania. Zwróć JSON {"claims": [{"text": str, "status": str, "citations": [str]}]}; '
    'status: "proven" tylko gdy twierdzenie wprost wynika z cytowanych fragmentów, "claim" gdy fragment '
    'zawiera czyjeś twierdzenie, "hypothesis" dla wniosku, "unknown" gdy brak podstawy. Statusy są etykietami, '
    'nie prawdopodobieństwami. Nie podawaj procentów pewności.'
)
RESPONSE_SCHEMA = {
    'type': 'OBJECT', 'required': ['claims'],
    'properties': {'claims': {'type': 'ARRAY', 'items': {
        'type': 'OBJECT', 'required': ['text', 'status', 'citations'],
        'properties': {'text': {'type': 'STRING'},
                       'status': {'type': 'STRING', 'enum': ['proven', 'claim', 'hypothesis', 'unknown']},
                       'citations': {'type': 'ARRAY', 'items': {'type': 'STRING'}}}}}},
}


NOT_SENT = ('no Google credentials', 'Vertex project')


class ProviderUnavailable(RuntimeError):
    """The turn must fall back to retrieval_only; the message names why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_consent(db: sqlite3.Connection, provider_name: str, scope_id: int, *, granted_by: str,
                   basis_text: str, data_policy: dict | None = None) -> str:
    uid = new_uid()
    db.execute('''INSERT INTO provider_consents(uid,provider_name,scope_id,granted_by,basis_text,data_policy_json,granted_at)
                  VALUES(?,?,?,?,?,?,?)''', (uid, provider_name, scope_id, granted_by, basis_text,
                                            json.dumps(data_policy or {}, ensure_ascii=False), _now()))
    authz._audit(db, 'provider_consent', 'provider_consent', None,
                 {'uid': uid, 'provider': provider_name, 'scope_id': scope_id}, granted_by)
    return uid


def revoke_consent(db: sqlite3.Connection, uid: str, *, actor: str) -> None:
    db.execute('UPDATE provider_consents SET revoked_at=? WHERE uid=? AND revoked_at IS NULL', (_now(), uid))
    authz._audit(db, 'provider_consent_revoked', 'provider_consent', None, {'uid': uid}, actor)


def consent_coverage(db: sqlite3.Connection, provider_name: str) -> authz.Coverage:
    scopes = [r['scope_id'] for r in db.execute(
        'SELECT scope_id FROM provider_consents WHERE provider_name=? AND revoked_at IS NULL', (provider_name,))]
    return authz.scope_coverage(db, scopes)


def spent_today_eur(db: sqlite3.Connection) -> float:
    day = datetime.now(timezone.utc).date().isoformat()
    row = db.execute('SELECT COALESCE(SUM(cost_eur),0) FROM provider_calls WHERE substr(started_at,1,10)=?', (day,)).fetchone()
    return float(row[0])


def _cost(config: dict, input_tokens: float, output_tokens: float) -> float:
    price = config['price_usd_per_million']
    return (input_tokens * price['input'] + output_tokens * price['output']) / 1_000_000 * config['eur_per_usd']


# ---------------------------------------------------------------- transports

def _access_token() -> str:
    request = urllib.request.Request(
        'http://metadata.google.internal/computeMetadata/v1/instance/service-account/default/token',
        headers={'Metadata-Flavor': 'Google'})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)['access_token']
    except (OSError, urllib.error.URLError, KeyError, ValueError):
        pass
    gcloud = shutil.which('gcloud')
    if gcloud:
        result = subprocess.run([gcloud, 'auth', 'print-access-token'], capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise ProviderUnavailable('no Google credentials (metadata server or gcloud)')


def _project(config: dict) -> str:
    if config.get('project'):
        return config['project']
    request = urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/project/project-id',
                                     headers={'Metadata-Flavor': 'Google'})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode().strip()
    except (OSError, urllib.error.URLError) as exc:
        raise ProviderUnavailable('Vertex project is not configured') from exc


def vertex_transport(config: dict, body: dict) -> dict:
    """POST generateContent; returns the decoded response or raises ProviderUnavailable."""
    location = config['location']
    host = 'aiplatform.googleapis.com' if location == 'global' else f'{location}-aiplatform.googleapis.com'
    url = (f"https://{host}/v1/projects/{_project(config)}/locations/{location}"
           f"/publishers/google/models/{config['model']}:generateContent")
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers={
        'Authorization': f'Bearer {_access_token()}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=config.get('timeout_seconds', 120)) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode('utf-8', 'replace')
        raise ProviderUnavailable(f'Vertex HTTP {exc.code}: {detail}') from exc
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise ProviderUnavailable(f'Vertex call failed: {type(exc).__name__}') from exc


# ---------------------------------------------------------------- provider

@dataclass
class VertexProvider:
    config: dict
    principal_id: int
    transport: object = vertex_transport

    @property
    def name(self) -> str:
        return self.config['name']

    def build_request(self, question: str, basis: list[dict], history: list[dict]) -> dict:
        passages = []
        for entry in basis:
            label = {'transcript_segment': 'transkrypcja', 'text_window': 'tekst', 'event': 'wiadomość',
                     'assignment': 'interpretacja w sprawie'}.get(entry['kind'], entry['kind'])
            where = ''
            if 'start_ms' in entry:
                where = f" [{entry['start_ms'] // 1000 // 60:02d}:{entry['start_ms'] // 1000 % 60:02d}]"
            source = entry.get('artifact', {}).get('name', '') if 'artifact' in entry else entry.get('ts_start') or ''
            passages.append(f"{entry['ref']} ({label}{where}, {source}): {entry['text']}")
        contents = []
        for turn in history[-6:]:
            readable = [a for a in turn['answers'] if not a.get('unavailable')]
            if not readable:
                continue  # answers whose material is no longer available never re-enter a prompt
            contents.append({'role': 'user', 'parts': [{'text': turn['question']}]})
            contents.append({'role': 'model', 'parts': [{'text': ' '.join(c['text'] for c in readable[-1]['claims'])}]})
        contents.append({'role': 'user', 'parts': [{'text': 'Fragmenty:\n' + ('\n'.join(passages) or '(brak)') +
                                                    f'\n\nPytanie: {question}'}]})
        return {
            'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
            'contents': contents,
            'generationConfig': {'temperature': self.config['temperature'],
                                 'maxOutputTokens': self.config['max_output_tokens'],
                                 'responseMimeType': 'application/json', 'responseSchema': RESPONSE_SCHEMA},
        }

    def answer(self, db: sqlite3.Connection, *, question: str, basis: list[dict], history: list[dict],
               conversation_id: int):
        consent = consent_coverage(db, self.name)
        for entry in basis:
            objects = [tuple(entry['object'])] + ([('artifact', entry['artifact']['id'])] if 'artifact' in entry else [])
            if not all(authz.covered_by(db, consent, kind, object_id) for kind, object_id in objects):
                raise ProviderUnavailable('no provider consent covers this material')
        if not basis:
            raise ProviderUnavailable('no readable passages match the question')
        body = self.build_request(question, basis, history)
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True)
        worst = _cost(self.config, len(raw) / self.config['chars_per_token'], self.config['max_output_tokens'])
        budget = authz.get_setting(db, 'advocate.daily_budget_eur', 1.0)
        if spent_today_eur(db) + worst > budget:
            raise ProviderUnavailable(f'daily budget {budget} EUR would be exceeded')
        started = _now()
        refs = sorted({tuple(e['object']) for e in basis})
        log_content = authz.get_setting(db, 'advocate.log_prompt_content', False) is True
        status, error, usage, claims = 'ok', None, {}, None
        try:
            response = self.transport(self.config, body)
            usage = response.get('usageMetadata', {})
            text = ''.join(part.get('text', '') for part in response['candidates'][0]['content']['parts'])
            try:
                claims = json.loads(text)['claims']
            except (ValueError, KeyError, TypeError):
                status, claims = 'unstructured_output', [{'text': text.strip(), 'status': 'unknown', 'citations': []}]
        except ProviderUnavailable as exc:
            status, error = 'error', str(exc)
        except (KeyError, IndexError, TypeError) as exc:
            status, error = 'error', f'unexpected response shape: {type(exc).__name__}'
        known = 'promptTokenCount' in usage and 'candidatesTokenCount' in usage
        never_sent = status == 'error' and error.startswith(NOT_SENT)
        cost = (_cost(self.config, usage['promptTokenCount'], usage['candidatesTokenCount']) if known
                else 0.0 if never_sent else worst)
        cur = db.execute('''INSERT INTO provider_calls(uid,provider_name,model,principal_id,purpose,prompt_sha256,
            input_refs_json,prompt_content,status,error,input_tokens,output_tokens,cached_tokens,cost_eur,
            cost_basis_json,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (new_uid(), self.name, self.config['model'], self.principal_id, 'advocate_answer',
             hashlib.sha256(raw.encode()).hexdigest(), json.dumps([list(r) for r in refs]),
             raw if log_content else None, status, error, usage.get('promptTokenCount'),
             usage.get('candidatesTokenCount'), usage.get('cachedContentTokenCount'), cost,
             json.dumps({'usage_reported': known, 'price': self.config['price_usd_per_million'],
                         'eur_per_usd': self.config['eur_per_usd'], 'worst_case_eur': worst}), started, _now()))
        if status == 'error':
            raise ProviderUnavailable(error)
        return claims, int(cur.lastrowid)


def configured_provider(db: sqlite3.Connection, principal_id: int):
    """The configured provider, or None (retrieval_only). Nothing is enabled by default."""
    config = authz.get_setting(db, 'advocate.provider')
    if not isinstance(config, dict) or config.get('type') != 'vertex_gemini':
        return None
    merged = {**VERTEX_PRESET, **config}
    return VertexProvider(merged, principal_id)


# ---------------------------------------------------------------- voice (package P12)

VOICE_PRESET = {
    'stt_model': 'gemini-3.5-flash', 'location': 'global', 'tts_voice': 'pl-PL-Wavenet-B', 'tts_language': 'pl-PL',
    'tts_price_usd_per_million_chars': 16.0, 'max_audio_bytes': 10 * 1024 * 1024, 'max_tts_chars': 4000,
    'stt_worst_case_tokens': 8000,
    'tts_price_source': 'Google Cloud TTS WaveNet list price as remembered; unverified, 2026-10-25',
}
STT_PROMPT = ('Przepisz dosłownie to nagranie pytania, w języku nagrania. Zwróć wyłącznie tekst pytania, '
              'bez komentarzy. Niezrozumiałe fragmenty oznacz [niezrozumiałe].')
AUDIO_TYPES = ('audio/webm', 'audio/ogg', 'audio/wav', 'audio/mpeg', 'audio/mp4')


def tts_transport(config: dict, body: dict) -> dict:
    request = urllib.request.Request('https://texttospeech.googleapis.com/v1/text:synthesize',
                                     data=json.dumps(body).encode(), method='POST', headers={
        'Authorization': f'Bearer {_access_token()}', 'Content-Type': 'application/json',
        'x-goog-user-project': _project(config)})
    try:
        with urllib.request.urlopen(request, timeout=config.get('timeout_seconds', 120)) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ProviderUnavailable(f'TTS HTTP {exc.code}: {exc.read()[:300].decode("utf-8", "replace")}') from exc
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise ProviderUnavailable(f'TTS call failed: {type(exc).__name__}') from exc


def _book(db, provider: str, model: str, principal_id: int, purpose: str, raw: str, refs, status: str,
          error, usage: dict, cost: float, basis: dict, started: str) -> int:
    cur = db.execute('''INSERT INTO provider_calls(uid,provider_name,model,principal_id,purpose,prompt_sha256,
        input_refs_json,prompt_content,status,error,input_tokens,output_tokens,cached_tokens,cost_eur,
        cost_basis_json,started_at,finished_at) VALUES(?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?)''',
        (new_uid(), provider, model, principal_id, purpose, hashlib.sha256(raw.encode()).hexdigest(),
         json.dumps([list(r) for r in refs]), status, error, usage.get('promptTokenCount'),
         usage.get('candidatesTokenCount'), usage.get('cachedContentTokenCount'), cost, json.dumps(basis), started, _now()))
    return int(cur.lastrowid)


@dataclass
class VoiceProvider:
    """Speech in and out for the advocate; the same gate, consent and budget as text."""
    config: dict
    principal_id: int
    stt: object = vertex_transport
    tts: object = tts_transport

    def _budget(self, db, worst: float) -> None:
        budget = authz.get_setting(db, 'advocate.daily_budget_eur', 1.0)
        if spent_today_eur(db) + worst > budget:
            raise ProviderUnavailable(f'daily budget {budget} EUR would be exceeded')

    def transcribe(self, db: sqlite3.Connection, audio: bytes, mime: str, *, speaker_consent: bool) -> dict:
        """The speaker's own recording; sent only with the speaker's explicit consent for this request."""
        if speaker_consent is not True:
            raise ProviderUnavailable('the speaker did not agree to send the recording for recognition')
        base_mime = mime.split(';')[0].strip().lower()
        if base_mime not in AUDIO_TYPES or not audio or len(audio) > self.config['max_audio_bytes']:
            raise ProviderUnavailable('unsupported or oversized recording')
        stt_config = {**self.config, 'model': self.config['stt_model']}
        worst = _cost(stt_config, self.config['stt_worst_case_tokens'], 1000)
        self._budget(db, worst)
        import base64
        body = {'contents': [{'role': 'user', 'parts': [
            {'inlineData': {'mimeType': base_mime, 'data': base64.b64encode(audio).decode()}}, {'text': STT_PROMPT}]}],
            'generationConfig': {'temperature': 0.0, 'maxOutputTokens': 1000}}
        started, status, error, usage, text = _now(), 'ok', None, {}, ''
        try:
            response = self.stt(stt_config, body)
            usage = response.get('usageMetadata', {})
            text = ''.join(p.get('text', '') for p in response['candidates'][0]['content']['parts']).strip()
        except ProviderUnavailable as exc:
            status, error = 'error', str(exc)
        except (KeyError, IndexError, TypeError) as exc:
            status, error = 'error', f'unexpected response shape: {type(exc).__name__}'
        known = 'promptTokenCount' in usage and 'candidatesTokenCount' in usage
        cost = _cost(stt_config, usage['promptTokenCount'], usage['candidatesTokenCount']) if known else (
            0.0 if status == 'error' and error.startswith(NOT_SENT) else worst)
        call = _book(db, self.config['name'], stt_config['model'], self.principal_id, 'speech_to_text',
                     hashlib.sha256(audio).hexdigest(), [], status, error, usage, cost,
                     {'usage_reported': known, 'speaker_consent': True, 'audio_bytes': len(audio)}, started)
        if status == 'error':
            raise ProviderUnavailable(error)
        return {'text': text, 'provider_call': call, 'note': 'machine transcription of your question; check it before asking'}

    def synthesize(self, db: sqlite3.Connection, answer_uid: str) -> bytes:
        """Read an answer aloud; only an answer this principal may read, with consent for every input."""
        row = db.execute('SELECT * FROM advocate_answers WHERE uid=?', (answer_uid,)).fetchone()
        if row is None or not authz.visible(db, self.principal_id, 'advocate_answer', row['id'], 'read_text'):
            raise authz.Denied('advocate_answer', answer_uid, 'read_text', exists=row is not None)
        from .advocate import AdvocateGate
        gate = AdvocateGate(db, self.principal_id)
        scope = json.loads(row['derived_scope_json'])
        if not all(gate.allows(kind, object_id, 'read_text') for kind, object_id in scope):
            raise authz.Denied('advocate_answer', answer_uid, 'read_text', exists=True)
        consent = consent_coverage(db, self.config['name'])
        if not all(authz.covered_by(db, consent, kind, object_id) for kind, object_id in scope):
            raise ProviderUnavailable('no provider consent covers this material')
        text = ' '.join(claim['text'] for claim in json.loads(row['claims_json']))[:self.config['max_tts_chars']]
        if not text.strip():
            raise ProviderUnavailable('the answer has no text to read')
        price = self.config['tts_price_usd_per_million_chars']
        cost = len(text) * price / 1_000_000 * self.config['eur_per_usd']
        self._budget(db, cost)
        body = {'input': {'text': text}, 'voice': {'languageCode': self.config['tts_language'],
                                                   'name': self.config['tts_voice']},
                'audioConfig': {'audioEncoding': 'MP3'}}
        started, status, error, audio = _now(), 'ok', None, b''
        try:
            import base64
            audio = base64.b64decode(self.tts(self.config, body)['audioContent'])
        except ProviderUnavailable as exc:
            status, error = 'error', str(exc)
        except (KeyError, TypeError, ValueError) as exc:
            status, error = 'error', f'unexpected response shape: {type(exc).__name__}'
        _book(db, 'google_tts', self.config['tts_voice'], self.principal_id, 'text_to_speech', text,
              sorted(tuple(item) for item in scope), status, error, {},
              0.0 if status == 'error' and error.startswith(NOT_SENT) else cost,
              {'characters': len(text), 'price_usd_per_million_chars': price, 'source': self.config['tts_price_source']},
              started)
        if status == 'error':
            raise ProviderUnavailable(error)
        return audio


def configured_voice(db: sqlite3.Connection, principal_id: int):
    """Voice needs a configured text provider and its own setting; nothing is enabled by default."""
    voice = authz.get_setting(db, 'advocate.voice')
    text = configured_provider(db, principal_id)
    if not isinstance(voice, dict) or voice.get('enabled') is not True or text is None:
        return None
    return VoiceProvider({**text.config, **VOICE_PRESET, **voice}, principal_id)
