// Advocate conversation page. Every value from the server is inserted with
// textContent; source text is data, never markup. Playback is approximate
// browser seeking within a stored recording, not verification of alignment.
import {createRangePlayer} from '/static/range-player.mjs';

const LABELS = {
  quoted_material: 'cytat z zapisanego tekstu',
  proven: 'wynika z cytowanych fragmentów',
  claim: 'twierdzenie zawarte w materiale',
  hypothesis: 'wniosek (hipoteza)',
  unknown: 'brak podstawy w materiale',
  claim_without_basis: 'bez cytatu – niezweryfikowane',
};
const KINDS = {
  transcript_segment: 'transkrypcja nagrania', text_window: 'fragment tekstu',
  event: 'wiadomość', assignment: 'interpretacja w sprawie',
};
const $ = (id) => document.getElementById(id);
const player = createRangePlayer($('player'));
const basisCache = new Map();
let current = null;
let providerModel = null;
let voiceEnabled = false;
const conversationSettings = new Map();

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {credentials: 'same-origin', ...options,
    headers: {'Content-Type': 'application/json', ...(options.headers || {})}});
  let body = null;
  try { body = await response.json(); } catch { body = null; }
  return {status: response.status, body, headers: response.headers};
}

function clock(ms) {
  const seconds = Math.floor(ms / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function show(id) {
  for (const section of ['login', 'denied', 'chat']) $(section).hidden = section !== id;
}

async function renderBasisEntry(entry) {
  const box = el('div', null, 'basis');
  if (entry.unavailable) {
    box.append(el('p', `${entry.ref}: materiał nie jest już dostępny dla tego konta.`, 'warn'));
    return box;
  }
  const head = [entry.ref, KINDS[entry.kind] || entry.kind];
  if (entry.artifact) head.push(entry.artifact.name);
  if (entry.start_ms !== undefined) head.push(`${clock(entry.start_ms)}–${clock(entry.end_ms)}`);
  if (entry.ts_start) head.push(entry.ts_start);
  box.append(el('div', head.join(' · '), 'meta'), el('blockquote', entry.text));
  const meta = [];
  if (entry.model) meta.push(`model tekstu: ${entry.model}`);
  if (entry.artifact && entry.artifact.sha256) meta.push(`SHA-256 materiału: ${entry.artifact.sha256.slice(0, 16)}…`);
  if (entry.derived_text_id) meta.push(`wersja tekstu #${entry.derived_text_id}`);
  if (entry.note) meta.push('to interpretacja zapisana w sprawie, nie tekst źródła');
  if (meta.length) box.append(el('div', meta.join(' · '), 'meta'));
  if (entry.kind === 'transcript_segment' && entry.artifact) {
    const url = `/api/artifacts/${entry.artifact.id}/content`;
    const probe = await fetch(url, {method: 'HEAD', credentials: 'same-origin'});
    if (probe.ok) {
      const button = el('button', `Odsłuchaj ${clock(entry.start_ms)}–${clock(entry.end_ms)}`, 'play');
      button.type = 'button';
      button.addEventListener('click', async () => {
        const audio = $('player');
        if (audio.getAttribute('src') !== url) audio.src = url;
        try { await player.play(entry.start_ms / 1000, entry.end_ms / 1000); }
        catch (error) { $('status').textContent = error.message; }
      });
      box.append(button);
    } else {
      box.append(el('div', 'Nagranie nie jest udostępnione temu kontu; dostępny jest tekst.', 'meta'));
    }
  }
  return box;
}

async function basisFor(answer) {
  if (answer.basis) return answer.basis;
  if (!basisCache.has(answer.answer_uid)) {
    const result = await api(`/api/advocate/answers/${encodeURIComponent(answer.answer_uid)}/basis`);
    basisCache.set(answer.answer_uid, result.status === 200 ? result.body.entries : []);
  }
  return basisCache.get(answer.answer_uid);
}

function renderAnswer(answer) {
  const box = el('div', null, 'answer');
  if (answer.unavailable) {
    box.append(el('p', 'Ta odpowiedź opierała się na materiale, który nie jest już dla Ciebie dostępny.', 'warn'));
    return box;
  }
  const mode = answer.mode === 'retrieval_only'
    ? 'Wyszukane fragmenty materiału (bez modelu językowego).'
    : `Odpowiedź modelu ${answer.model || providerModel || answer.mode.replace('provider:', '')} (Google Vertex) — sprawdź cytaty przed użyciem.`;
  box.append(el('div', mode, 'meta'));
  const verification = answer.verification || {};
  if (verification.provider_fallback) {
    box.append(el('div', `Model nie został użyty: ${verification.provider_fallback}`, 'meta warn'));
  }
  if (verification.removed_citations && verification.removed_citations.length) {
    box.append(el('div', `Usunięto ${verification.removed_citations.length} odwołań spoza udostępnionego materiału.`, 'meta warn'));
  }
  if (voiceEnabled && answer.answer_uid && answer.claims.length) {
    const listen = el('button', 'Odsłuchaj odpowiedź', 'play');
    listen.type = 'button';
    listen.addEventListener('click', async () => {
      listen.disabled = true;
      $('status').textContent = 'Przygotowuję nagranie odpowiedzi…';
      const response = await fetch(`/api/advocate/answers/${encodeURIComponent(answer.answer_uid)}/speech`, {credentials: 'same-origin'});
      listen.disabled = false;
      if (!response.ok) {
        let detail = ''; try { detail = (await response.json()).detail || ''; } catch {}
        $('status').textContent = `Nie udało się odczytać odpowiedzi${detail ? `: ${detail}` : '.'}`;
        return;
      }
      const audio = $('player');
      player.stop();
      audio.src = URL.createObjectURL(await response.blob());
      $('status').textContent = '';
      try { await audio.play(); } catch (error) { $('status').textContent = error.message; }
    });
    box.append(listen);
  }
  if (!answer.claims.length) {
    box.append(el('p', 'Nie znaleziono pasujących fragmentów w udostępnionym materiale. Spróbuj innych słów.'));
  }
  for (const claim of answer.claims) {
    const line = el('div', null, 'claim');
    const label = el('span', LABELS[claim.status] || claim.status, `label ${claim.status}`);
    line.append(label, el('span', claim.text));
    const details = el('div');
    for (const ref of claim.citations) {
      const button = el('button', ref, 'cite');
      button.type = 'button';
      button.setAttribute('aria-expanded', 'false');
      button.addEventListener('click', async () => {
        if (button.getAttribute('aria-expanded') === 'true') {
          details.replaceChildren();
          button.setAttribute('aria-expanded', 'false');
          return;
        }
        const entry = (await basisFor(answer)).find((item) => item.ref === ref);
        details.replaceChildren(entry ? await renderBasisEntry(entry) : el('p', 'Cytat niedostępny.', 'warn'));
        button.setAttribute('aria-expanded', 'true');
      });
      line.append(button);
    }
    box.append(line, details);
  }
  return box;
}

function renderTurn(question, answers) {
  const turn = el('div', null, 'turn');
  turn.append(el('div', question, 'question'));
  for (const answer of answers) turn.append(renderAnswer(answer));
  $('turns').append(turn);
  turn.scrollIntoView({block: 'end'});
}

async function openConversation(uid) {
  current = uid;
  for (const button of document.querySelectorAll('#conversations button')) {
    button.setAttribute('aria-current', String(button.dataset.uid === uid));
  }
  $('turns').replaceChildren();
  const settings = conversationSettings.get(uid) || {};
  $('share-row').hidden = settings.owner_visibility !== 'opt_in_by_recipient';
  $('share').checked = settings.shared_with_owner === true;
  const result = await api(`/api/advocate/conversations/${encodeURIComponent(uid)}`);
  if (result.status !== 200) { $('status').textContent = 'Nie udało się otworzyć rozmowy.'; return; }
  for (const turn of result.body.turns) renderTurn(turn.question, turn.answers);
}

async function loadConversations() {
  const result = await api('/api/advocate/conversations');
  const list = $('conversations');
  list.replaceChildren();
  for (const conversation of result.body || []) {
    const item = el('li');
    const button = el('button', conversation.title || `Rozmowa z ${conversation.created_at.slice(0, 16).replace('T', ' ')}`);
    button.type = 'button';
    button.dataset.uid = conversation.uid;
    conversationSettings.set(conversation.uid, JSON.parse(conversation.settings_json || '{}'));
    button.addEventListener('click', () => openConversation(conversation.uid));
    item.append(button);
    list.append(item);
  }
  return result.body || [];
}

async function newConversation() {
  const result = await api('/api/advocate/conversations', {method: 'POST', body: JSON.stringify({})});
  if (result.status !== 200) { $('status').textContent = 'Nie udało się rozpocząć rozmowy.'; return; }
  await loadConversations();
  await openConversation(result.body.uid);
}

async function ask(event) {
  event.preventDefault();
  const question = $('question').value.trim();
  if (!question) return;
  if (!current) await newConversation();
  $('send').disabled = true;
  $('status').textContent = 'Szukam w udostępnionym materiale…';
  const result = await api(`/api/advocate/conversations/${encodeURIComponent(current)}/turns`,
    {method: 'POST', body: JSON.stringify({question})});
  $('send').disabled = false;
  if (result.status !== 200) {
    $('status').textContent = result.status === 404 ? 'Brak dostępu do tej rozmowy.' : 'Nie udało się uzyskać odpowiedzi.';
    return;
  }
  $('status').textContent = '';
  $('question').value = '';
  renderTurn(question, [result.body]);
}

async function loadSummaries() {
  const result = await api('/api/advocate/summaries');
  const list = $('summary-list');
  list.replaceChildren();
  for (const summary of result.body || []) {
    const box = el('div', null, 'turn');
    box.append(el('div', summary.topic, 'question'),
      renderAnswer({mode: 'provider:vertex', model: summary.model, claims: summary.claims, basis: summary.basis, verification: {}}));
    list.append(box);
  }
}

async function summarize(event) {
  event.preventDefault();
  const topic = $('topic').value.trim();
  if (!topic) return;
  $('summary-send').disabled = true;
  $('status').textContent = 'Tworzę streszczenie z udostępnionego materiału…';
  const result = await api('/api/advocate/summaries', {method: 'POST', body: JSON.stringify({topic})});
  $('summary-send').disabled = false;
  $('status').textContent = result.status === 200 ? '' : `Nie utworzono streszczenia${result.body && result.body.detail ? `: ${result.body.detail}` : '.'}`;
  if (result.status === 200) { $('topic').value = ''; await loadSummaries(); }
}

function setupVoice() {
  $('voice').hidden = false;
  const record = $('record');
  $('voice-consent').addEventListener('change', () => { record.disabled = !$('voice-consent').checked; });
  let recorder = null, chunks = [];
  record.addEventListener('click', async () => {
    if (recorder && recorder.state === 'recording') { recorder.stop(); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({audio: true}); }
    catch { $('status').textContent = 'Brak dostępu do mikrofonu.'; return; }
    recorder = new MediaRecorder(stream);
    chunks = [];
    recorder.addEventListener('dataavailable', (event) => chunks.push(event.data));
    recorder.addEventListener('stop', async () => {
      for (const track of stream.getTracks()) track.stop();
      record.textContent = 'Nagraj pytanie';
      $('status').textContent = 'Rozpoznaję nagranie…';
      const form = new FormData();
      const type = recorder.mimeType || 'audio/webm';
      form.append('file', new Blob(chunks, {type}), 'pytanie.webm');
      form.append('speaker_consent', $('voice-consent').checked ? 'yes' : '');
      const response = await fetch('/api/advocate/voice/transcribe', {method: 'POST', body: form, credentials: 'same-origin'});
      let body = null; try { body = await response.json(); } catch {}
      if (!response.ok) { $('status').textContent = `Nie rozpoznano nagrania${body && body.detail ? `: ${body.detail}` : '.'}`; return; }
      $('question').value = body.text;
      $('status').textContent = 'Sprawdź rozpoznany tekst i kliknij „Zapytaj”.';
    });
    recorder.start();
    record.textContent = 'Zakończ nagrywanie';
    $('status').textContent = 'Nagrywam… (maks. 60 s)';
    setTimeout(() => { if (recorder && recorder.state === 'recording') recorder.stop(); }, 60000);
  });
}

async function start() {
  const status = await api('/api/advocate/status');
  if (status.status === 401) { show('login'); return; }
  if (status.status !== 200 || !status.body.can_chat) {
    show('denied');
    if (status.status === 409) $('denied').querySelector('p').textContent = 'Instalacja nie ma jeszcze właściciela; rzecznik jest niedostępny.';
    return;
  }
  show('chat');
  $('logout').hidden = status.body.auth_mode !== 'tokens';
  $('who').textContent = `Zalogowano: ${status.body.principal}. Odpowiedzi wyłącznie z udostępnionego materiału, z cytatami do sprawdzenia.`;
  const notices = $('notices');
  notices.replaceChildren(
    el('p', status.body.provider
      ? `Odpowiedzi formułuje model ${status.body.provider.model} (Google Vertex) na podstawie wyszukanych fragmentów; każde twierdzenie ma etykietę i cytaty.`
      : 'Model językowy nie jest włączony: rzecznik pokazuje wyszukane fragmenty materiału z cytatami.'),
    el('p', {none: 'Właściciel materiału nie widzi treści tej rozmowy.',
             full: 'Właściciel materiału może czytać tę rozmowę.',
             metadata_only: 'Właściciel materiału widzi, że rozmowa istnieje i kiedy była prowadzona, ale nie jej treść.',
             opt_in_by_recipient: 'Właściciel materiału nie widzi rozmowy, dopóki jej nie udostępnisz.'}[status.body.owner_visibility]
      || `Widoczność rozmowy dla właściciela: ${status.body.owner_visibility}.`),
    el('p', 'Etykiety opisują rodzaj twierdzenia, nie prawdopodobieństwo. Transkrypcje są wynikiem rozpoznawania mowy i mogą zawierać błędy.', 'meta'));
  if (status.headers.get('X-AGEDS-Auth-Mode') === 'legacy_open') {
    notices.append(el('p', 'Tryb bez kont: każdy, kto ma dostęp do tego adresu, działa jako właściciel.', 'warn'));
  }
  if (status.body.voice) {
    voiceEnabled = true;
    setupVoice();
  }
  if (status.body.provider) {
    providerModel = status.body.provider.model;
    $('summaries').hidden = false;
    $('summarize').addEventListener('submit', summarize);
    await loadSummaries();
  }
  $('share').addEventListener('change', async () => {
    const result = await api(`/api/advocate/conversations/${encodeURIComponent(current)}/share`,
      {method: 'POST', body: JSON.stringify({shared: $('share').checked})});
    if (result.status === 200) {
      conversationSettings.set(current, {...(conversationSettings.get(current) || {}), shared_with_owner: result.body.shared_with_owner});
      $('status').textContent = result.body.shared_with_owner ? 'Właściciel może teraz czytać tę rozmowę.' : 'Rozmowa znów jest prywatna.';
    } else {
      $('share').checked = !$('share').checked;
      $('status').textContent = 'Nie udało się zmienić udostępnienia.';
    }
  });
  $('new').addEventListener('click', newConversation);
  $('ask').addEventListener('submit', ask);
  const conversations = await loadConversations();
  if (conversations.length) await openConversation(conversations[0].uid);
}

start();
