import {createRangePlayer, validRange} from './range-player.mjs';

const ID_ERROR = 'Identyfikator jest nieobsługiwany przez ten widok: wymagane dokładne dodatnie ID do 9007199254740991. Operację wstrzymano; ID nie zostanie zaokrąglone.';

/** DOM IDs are decimal text; validate the exact integer before conversion. */
export function parseDomId(value) {
  if (typeof value !== 'string' || !/^[1-9][0-9]*$/.test(value) || value.length > 16 || BigInt(value) > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error(ID_ERROR);
  return Number(value);
}

/** Legacy API IDs are JSON numbers. Strings/booleans/coercions are not the contract. */
export function requireApiId(value) {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) throw new Error(ID_ERROR);
  return value;
}

export function validateTranscriptIdentity(body, artifactId, transcriptId) {
  if (requireApiId(body?.artifactId) !== requireApiId(artifactId) || requireApiId(body?.id) !== requireApiId(transcriptId)) {
    throw new Error('Odpowiedź wskazuje inny artefakt lub wersję. Operację wstrzymano.');
  }
  return body;
}

export function validateCitationIdentity(body, artifactId, transcriptId, expectedQuote) {
  requireApiId(body?.id);
  if (requireApiId(body?.artifact_id) !== requireApiId(artifactId) || requireApiId(body?.derived_text_id) !== requireApiId(transcriptId)) {
    throw new Error('Zapisany cytat wskazuje inny artefakt lub wersję. Nie udostępniono odsłuchu ani eksportu.');
  }
  if (expectedQuote !== undefined && (typeof expectedQuote !== 'string' || body.quote_text !== expectedQuote)) {
    throw new Error('Odpowiedź zapisu nie zachowuje dokładnego wybranego tekstu. Nie udostępniono odsłuchu ani eksportu.');
  }
  return body;
}

export function validatePacketPath(path, artifactId, citationId) {
  const match = typeof path === 'string' && /^\/api\/artifacts\/([1-9][0-9]*)\/citations\/([1-9][0-9]*)\/packet$/.exec(path);
  if (!match || parseDomId(match[1]) !== requireApiId(artifactId) || parseDomId(match[2]) !== requireApiId(citationId)) {
    throw new Error('Eksport wskazuje inny lub nieobsługiwany identyfikator. Operację wstrzymano.');
  }
  return path;
}

export const HISTORY_CAP = 1000;
const PAGE_KEYS = ['artifactId', 'items', 'nextBeforeId', 'snapshotMaxId', 'hasMore', 'limit'];
const pageError = () => new Error('Nieprawidłowa strona historii: identyfikatory, kolejność lub kursor nie są zgodne. Nic nie dodano.');

export function validateHistoryItem(item, kind, artifactId) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) throw pageError();
  requireApiId(item.id);
  const owner = kind === 'annotations' ? item.artifactId : item.artifact_id;
  if (requireApiId(owner) !== requireApiId(artifactId)) throw pageError();
  if (kind === 'versions') {
    if (item.run_id !== null) requireApiId(item.run_id);
    if (typeof item.created_at !== 'string' || ![item.model, item.language].every(x => x === null || typeof x === 'string')) throw pageError();
    if ('text' in item || 'segments' in item || 'segments_json' in item) throw pageError();
  } else if (kind === 'citations') {
    validateCitationIdentity(item, artifactId, item.derived_text_id);
    if (typeof item.quote_text !== 'string' || !Number.isSafeInteger(item.start_ms) || !Number.isSafeInteger(item.end_ms) || item.start_ms < 0 || item.end_ms < item.start_ms) throw pageError();
  } else if (kind === 'annotations') {
    if (item.derivedTextId !== null) requireApiId(item.derivedTextId);
    if (![item.kind, item.body, item.createdAt].every(x => typeof x === 'string') || !(item.label === null || typeof item.label === 'string')) throw pageError();
    if (item.startMs !== null || item.endMs !== null) {
      if (!Number.isSafeInteger(item.startMs) || !Number.isSafeInteger(item.endMs) || item.startMs < 0 || item.endMs < item.startMs) throw pageError();
    }
  } else throw pageError();
  return item;
}

export function validateHistoryPage(body, {kind, artifactId, beforeId = null, snapshotMaxId, limit, seen = new Set()}) {
  if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).length !== PAGE_KEYS.length || PAGE_KEYS.some(key => !Object.hasOwn(body, key))) throw pageError();
  if (requireApiId(body.artifactId) !== requireApiId(artifactId) || typeof body.hasMore !== 'boolean' || !Number.isSafeInteger(body.limit) || body.limit < 1 || body.limit > 100 || (limit !== undefined && body.limit !== limit)) throw pageError();
  if (!Array.isArray(body.items) || body.items.length > body.limit) throw pageError();
  if (body.snapshotMaxId !== 0) requireApiId(body.snapshotMaxId);
  if (snapshotMaxId !== undefined && body.snapshotMaxId !== snapshotMaxId) throw pageError();
  if (beforeId !== null) requireApiId(beforeId);
  let previous = beforeId;
  for (const item of body.items) {
    validateHistoryItem(item, kind, artifactId);
    if (body.snapshotMaxId === 0 || item.id > body.snapshotMaxId || (previous !== null && item.id >= previous) || seen.has(item.id)) throw pageError();
    previous = item.id;
  }
  if (body.hasMore) {
    if (!body.items.length || requireApiId(body.nextBeforeId) !== body.items.at(-1).id) throw pageError();
  } else if (body.nextBeforeId !== null) throw pageError();
  if (body.snapshotMaxId === 0 && (body.items.length || body.hasMore)) throw pageError();
  return body;
}

/** Stateful but DOM-independent cursor ownership; all pages validate before mutation. */
export function createHistoryPager(kind, artifactId, initial) {
  requireApiId(artifactId);
  validateHistoryPage(initial, {kind, artifactId});
  const seen = new Set(initial.items.map(item => item.id));
  let cursor = initial.nextBeforeId, more = initial.hasMore, token = 0, active = null, closed = false;
  const snapshot = initial.snapshotMaxId;
  return {
    get count() { return seen.size; },
    get capped() { return more && seen.size >= HISTORY_CAP; },
    get hasMore() { return more; },
    get busy() { return active !== null; },
    begin() {
      if (closed || active !== null || !more || seen.size >= HISTORY_CAP) return null;
      active = Object.freeze({token: ++token, beforeId: cursor, snapshotMaxId: snapshot, limit: Math.min(initial.limit, HISTORY_CAP - seen.size)});
      return active;
    },
    accept(request, page) {
      if (closed || active !== request) return null;
      validateHistoryPage(page, {kind, artifactId, beforeId: request.beforeId, snapshotMaxId: snapshot, limit: request.limit, seen});
      if (seen.size + page.items.length > HISTORY_CAP) throw pageError();
      for (const item of page.items) seen.add(item.id);
      cursor = page.nextBeforeId; more = page.hasMore; active = null;
      return page.items;
    },
    fail(request) { if (active === request) active = null; },
    addNew(item) {
      validateHistoryItem(item, kind, artifactId);
      if (seen.has(item.id) || seen.size >= HISTORY_CAP) return false;
      seen.add(item.id); return true;
    },
    close() { closed = true; active = null; token += 1; },
  };
}

const root = typeof document === 'undefined' ? null : document.querySelector('[data-citation-workspace]');
if (root) {
  const $ = name => root.querySelector(`[data-${name}]`);
  const version = $('version'), kind = $('kind'), first = $('first'), last = $('last');
  const preview = $('preview'), status = $('status'), save = $('save'), play = $('play');
  const audio = document.querySelector('[data-evidence-audio]');
  const player = audio ? createRangePlayer(audio) : null;
  const historyPagers = new Map(), historyControllers = new Set();
  let historyClosed = false;
  let transcript = null, units = [], generation = 0, playGeneration = 0, controller = null, saving = false;
  function stopPlayback() { playGeneration += 1; player?.stop(); }
  function show(message) { status.textContent = message; }
  async function playBounds(start, end) {
    if (!player) return;
    const request = generation, playRequest = ++playGeneration;
    try { await player.play(start, end); }
    catch (error) { if (request === generation && playRequest === playGeneration) show(error.message); }
  }
  function savedBounds(button) {
    const startMs = Number(button.dataset.startMs), endMs = Number(button.dataset.endMs);
    if (!Number.isSafeInteger(startMs) || !Number.isSafeInteger(endMs)) return null;
    const start = startMs / 1000, end = endMs / 1000;
    return validRange(start, end) ? {start, end} : null;
  }
  function workspaceIdentity() {
    return {artifactId: parseDomId(root.dataset.artifactId), transcriptId: parseDomId(version.value)};
  }
  function savedIdentity(button) {
    return {artifactId: parseDomId(root.dataset.artifactId), citationId: parseDomId(button?.dataset.citationId), transcriptId: parseDomId(button?.dataset.derivedTextId)};
  }
  function packetIdentity(packet) {
    const button = packet.closest('article')?.querySelector('[data-citation-play]');
    const identity = savedIdentity(button);
    validatePacketPath(packet.getAttribute('href'), identity.artifactId, identity.citationId);
  }
  for (const button of root.querySelectorAll('[data-citation-play]')) {
    try { savedIdentity(button); button.disabled = !player || !savedBounds(button); }
    catch (error) { button.disabled = true; button.title = error.message; button.after(document.createTextNode(error.message)); }
  }
  for (const packet of root.querySelectorAll('[data-citation-packet]')) {
    try { packetIdentity(packet); }
    catch (error) { packet.removeAttribute('href'); packet.setAttribute('aria-disabled', 'true'); packet.title = error.message; packet.textContent = `Eksport niedostępny: ${error.message}`; }
  }
  $('saved').addEventListener('click', event => {
    const packet = event.target.closest('[data-citation-packet]');
    if (packet && $('saved').contains(packet)) {
      try { packetIdentity(packet); }
      catch (error) { event.preventDefault(); stopPlayback(); show(error.message); }
      return;
    }
    const button = event.target.closest('[data-citation-play]');
    if (!button || !$('saved').contains(button) || button.disabled) return;
    try { savedIdentity(button); }
    catch (error) { stopPlayback(); show(error.message); return; }
    const bounds = savedBounds(button);
    if (!bounds) return;
    show(`Odtwarzanie cytatu #${button.dataset.citationId}, wersja #${button.dataset.derivedTextId}. Zakres pochodzi z zapisanego cytatu; odsłuch nie weryfikuje automatycznie tekstu ASR.`);
    void playBounds(bounds.start, bounds.end);
  });
  function selection() {
    const start = Number(first.value), end = Number(last.value);
    if (!units.length || start < 0 || !Number.isInteger(start) || !Number.isInteger(end) || end < start || end >= units.length) return [];
    if (kind.value === 'words' && units[start].block !== units[end].block) return [];
    return units.slice(start, end + 1);
  }
  function refresh() {
    const picked = selection();
    let identityValid = false;
    try {
      const identity = workspaceIdentity();
      validateTranscriptIdentity(transcript, identity.artifactId, identity.transcriptId);
      identityValid = true;
    } catch { /* load/click handlers expose the reason; keep actions unavailable. */ }
    preview.textContent = picked.map(x => x.text).join('');
    save.disabled = !identityValid || !picked.length || saving;
    play.disabled = !identityValid || !player || !picked.length || !validRange(picked[0]?.start, picked.at(-1)?.end);
    if (units.length && !picked.length) show('Wybierz rosnący zakres bez pominiętych segmentów. Dla brakujących znaczników słów użyj segmentów.');
  }
  function populate() {
    stopPlayback();
    units = [];
    let block = 0;
    const availability = new Map((transcript?.wordTiming?.segments || []).map(x => [x.segment_index, x.word_selection_available]));
    for (const [segmentIndex, segment] of (transcript?.segments || []).entries()) {
      if (kind.value === 'segments') {
        units.push({text: segment.text, start: segment.start, end: segment.end, segmentIndex});
      } else if (availability.get(segmentIndex)) {
        for (const [wordIndex, word] of segment.words.entries()) {
          units.push({text: word.word, start: word.start, end: word.end, segmentIndex, wordIndex, block});
          if (units.length >= 10000) break;
        }
      } else { block += 1; }
      if (units.length >= 10000) break;
    }
    first.replaceChildren(); last.replaceChildren();
    units.forEach((unit, index) => {
      for (const select of [first, last]) {
        const option = document.createElement('option');
        option.value = String(index);
        option.textContent = `${index + 1}. ${unit.start ?? '?'}–${unit.end ?? '?'} s · ${String(unit.text).slice(0, 100)}`;
        select.append(option);
      }
    });
    refresh();
    if (!units.length) show(kind.value === 'words' ? 'Ta wersja nie ma zgodnych znaczników słów. Dostępny pozostaje wybór segmentów.' : 'Ta wersja nie zawiera segmentów.');
    else if (units.length >= 10000) show('Wyświetlono pierwsze 10 000 pozycji. Zakres listy jest ograniczony.');
    else show(`Wersja #${transcript.id}. Wybierz początek i koniec ciągłego cytatu. Czasy pochodzą z ASR.`);
  }
  async function load() {
    const request = ++generation;
    controller?.abort(); controller = new AbortController();
    transcript = null; document.querySelector('[data-transcript-text]').textContent = ''; populate();
    if (!version.value) return;
    show('Wczytuję wskazaną wersję…');
    try {
      const identity = workspaceIdentity();
      const response = await fetch(`/api/artifacts/${identity.artifactId}/transcript?derived_text_id=${identity.transcriptId}`, {signal: controller.signal});
      if (!response.ok) throw new Error(`Odczyt wersji: HTTP ${response.status}`);
      const body = await response.json();
      if (request !== generation) return;
      transcript = validateTranscriptIdentity(body, identity.artifactId, identity.transcriptId);
      document.querySelector('[data-transcript-text]').textContent = typeof transcript.text === 'string' ? transcript.text : ''; populate();
    } catch (error) { if (request === generation && error.name !== 'AbortError') show(error.message); }
  }
  version.addEventListener('change', load);
  kind.addEventListener('change', populate);
  for (const select of [first, last]) select.addEventListener('change', () => { stopPlayback(); refresh(); });
  $('stop').addEventListener('click', stopPlayback);
  play.addEventListener('click', async () => {
    const picked = selection();
    if (!player || !picked.length) return;
    try {
      const identity = workspaceIdentity();
      validateTranscriptIdentity(transcript, identity.artifactId, identity.transcriptId);
    } catch (error) { stopPlayback(); show(error.message); return; }
    await playBounds(picked[0].start, picked.at(-1).end);
  });
  save.addEventListener('click', async () => {
    const picked = selection();
    if (!transcript || !picked.length || saving) return;
    let identity;
    try {
      identity = workspaceIdentity();
      validateTranscriptIdentity(transcript, identity.artifactId, identity.transcriptId);
    } catch (error) { stopPlayback(); show(error.message); return; }
    const request = generation, pinnedId = identity.transcriptId;
    const body = {derivedTextId: pinnedId, quoteText: picked.map(x => x.text).join('')};
    if (kind.value === 'words') body.wordRefs = picked.map(x => ({segment_index: x.segmentIndex, word_index: x.wordIndex}));
    else body.segmentIndices = picked.map(x => x.segmentIndex);
    saving = true; refresh();
    try {
      const response = await fetch(`/api/artifacts/${identity.artifactId}/citations`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `Nie można zapisać cytatu: HTTP ${response.status}`);
      validateCitationIdentity(result, identity.artifactId, pinnedId, body.quoteText);
      if (parseDomId(root.dataset.artifactId) !== identity.artifactId) throw new Error('Artefakt widoku zmienił się podczas zapisu. Operację wstrzymano.');
      if (request === generation) show(`Zapisano cytat #${result.id}, wersja #${pinnedId}, ${result.start_ms}–${result.end_ms} ms. Zgodność z transkryptem sprawdzona; odsłuch nie został potwierdzony.`);
      const pager = historyPagers.get('citations');
      if (!pager || !pager.addNew(result)) {
        show(`Zapisano cytat #${result.id}. Lista osiągnęła limit ${HISTORY_CAP} lub cytat już jest widoczny; odśwież widok, aby wczytać najnowsze cytaty.`);
      } else {
        $('saved').prepend(citationArticle(result));
      }
      refreshHistory('citations');
    } catch (error) { if (request === generation) show(error.message); }
    finally { saving = false; refresh(); }
  });
  function citationArticle(result) {
    const identity = {artifactId: parseDomId(root.dataset.artifactId), citationId: requireApiId(result.id)};
    validateHistoryItem(result, 'citations', identity.artifactId);
    const article = document.createElement('article');
    const label = document.createElement('small');
    label.textContent = `Cytat #${result.id} · transkrypt #${result.derived_text_id} · ${result.start_ms}–${result.end_ms} ms`;
    const quote = document.createElement('p'); quote.className = 'transcript'; quote.textContent = result.quote_text;
    const replay = document.createElement('button'); replay.type = 'button';
    replay.dataset.citationPlay = ''; replay.dataset.citationId = String(result.id);
    replay.dataset.derivedTextId = String(result.derived_text_id);
    replay.dataset.startMs = String(result.start_ms); replay.dataset.endMs = String(result.end_ms);
    replay.textContent = 'Odtwórz zapisany cytat'; replay.disabled = !player || !savedBounds(replay);
    const packet = document.createElement('a');
    packet.href = validatePacketPath(`/api/artifacts/${identity.artifactId}/citations/${result.id}/packet`, identity.artifactId, result.id);
    packet.textContent = 'Pobierz metadane cytatu';
    packet.title = 'Pakiet zawiera pełną przypiętą wersję transkryptu i zapisane pochodzenie, bez nagrania.';
    packet.dataset.citationPacket = '';
    article.append(label, quote, replay, packet); return article;
  }
  function appendHistory(name, items) {
    // Pages have been validated completely; source strings only enter textContent.
    if (name === 'versions') {
      const selected = version.value;
      const annotationVersion = document.querySelector('[data-annotation-version]');
      const annotationSelected = annotationVersion.value;
      if (items.length) for (const option of version.querySelectorAll('option[value=""]')) option.remove();
      for (const item of items) {
        for (const select of [version, annotationVersion]) {
          const option = document.createElement('option'); option.value = String(item.id);
          option.textContent = `#${item.id} · ${item.model || 'model nieznany'} · ${item.created_at}`;
          select.append(option);
        }
        const article = document.createElement('article');
        article.textContent = `#${item.id} · ${item.model || 'model nieznany'} · ${item.language || ''} · ${item.created_at}`;
        document.querySelector('[data-version-summaries]').append(article);
      }
      if (selected) version.value = selected;
      annotationVersion.value = annotationSelected;
    } else if (name === 'citations') {
      for (const item of items) $('saved').append(citationArticle(item));
    } else {
      for (const item of items) {
        const article = document.createElement('article'), label = document.createElement('small');
        label.textContent = `${item.kind} · ${item.createdAt}${item.derivedTextId === null ? '' : ` · transkrypt #${item.derivedTextId}`}`;
        const heading = document.createElement('h3'); heading.textContent = item.label || '';
        const body = document.createElement('p'); body.textContent = item.body;
        article.append(label, heading, body); document.querySelector('[data-annotation-history]').append(article);
      }
    }
  }
  function refreshHistory(name, error = null) {
    const pager = historyPagers.get(name), button = document.querySelector(`[data-history-more="${name}"]`);
    const message = document.querySelector(`[data-history-status="${name}"]`);
    if (!pager) { button.disabled = true; if (error) message.textContent = error; return; }
    button.disabled = historyClosed || pager.busy || pager.capped || !pager.hasMore;
    message.textContent = error || (pager.capped ? `Wyświetlono limit ${HISTORY_CAP} pozycji. Dalsze wczytywanie w tym widoku zatrzymano; odśwież stronę, aby zacząć od najnowszych.` :
      pager.busy ? 'Wczytuję starsze pozycje…' : `${pager.count} pozycji. ${pager.hasMore ? 'Starsze pozycje są dostępne.' : 'Koniec historii w tej migawce.'}`);
  }
  try {
    const artifactId = parseDomId(root.dataset.artifactId);
    const bootstrap = JSON.parse(document.querySelector('[data-history-pages]').textContent);
    for (const name of ['versions', 'citations', 'annotations']) {
      try {
        const pager = createHistoryPager(name, artifactId, bootstrap[name]);
        historyPagers.set(name, pager);
        const button = document.querySelector(`[data-history-more="${name}"]`);
        button.addEventListener('click', async () => {
          const request = pager.begin();
          if (!request) return;
          const abort = new AbortController(); historyControllers.add(abort); refreshHistory(name);
          try {
            if (parseDomId(root.dataset.artifactId) !== artifactId) throw pageError();
            const route = name === 'versions' ? 'transcripts' : name;
            const params = new URLSearchParams({limit: String(request.limit), before_id: String(request.beforeId), snapshot_max_id: String(request.snapshotMaxId)});
            const response = await fetch(`/api/artifacts/${artifactId}/${route}/page?${params}`, {signal: abort.signal});
            if (!response.ok) throw new Error(`Odczyt historii: HTTP ${response.status}. Nic nie dodano.`);
            const page = await response.json();
            if (historyClosed) return;
            if (parseDomId(root.dataset.artifactId) !== artifactId) throw pageError();
            const items = pager.accept(request, page);
            if (items !== null) appendHistory(name, items);
            refreshHistory(name);
          } catch (error) {
            pager.fail(request);
            if (!historyClosed && error.name !== 'AbortError') refreshHistory(name, error.message);
          } finally { historyControllers.delete(abort); }
        });
        refreshHistory(name);
      } catch (error) { refreshHistory(name, error.message); }
    }
  } catch (error) { for (const name of ['versions', 'citations', 'annotations']) refreshHistory(name, error.message); }
  window.addEventListener('pagehide', () => {
    historyClosed = true;
    for (const pager of historyPagers.values()) pager.close();
    for (const abort of historyControllers) abort.abort();
    controller?.abort(); player?.destroy();
  });
  void load();
}
