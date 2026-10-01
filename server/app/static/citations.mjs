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

const root = typeof document === 'undefined' ? null : document.querySelector('[data-citation-workspace]');
if (root) {
  const $ = name => root.querySelector(`[data-${name}]`);
  const version = $('version'), kind = $('kind'), first = $('first'), last = $('last');
  const preview = $('preview'), status = $('status'), save = $('save'), play = $('play');
  const audio = document.querySelector('[data-evidence-audio]');
  const player = audio ? createRangePlayer(audio) : null;
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
    transcript = null; populate();
    if (!version.value) return;
    show('Wczytuję wskazaną wersję…');
    try {
      const identity = workspaceIdentity();
      const response = await fetch(`/api/artifacts/${identity.artifactId}/transcript?derived_text_id=${identity.transcriptId}`, {signal: controller.signal});
      if (!response.ok) throw new Error(`Odczyt wersji: HTTP ${response.status}`);
      const body = await response.json();
      if (request !== generation) return;
      transcript = validateTranscriptIdentity(body, identity.artifactId, identity.transcriptId); populate();
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
      const article = document.createElement('article');
      const label = document.createElement('small');
      label.textContent = `Cytat #${result.id} · transkrypt #${pinnedId} · ${result.start_ms}–${result.end_ms} ms`;
      const quote = document.createElement('p'); quote.className = 'transcript'; quote.textContent = result.quote_text;
      const replay = document.createElement('button'); replay.type = 'button';
      replay.dataset.citationPlay = ''; replay.dataset.citationId = String(result.id);
      replay.dataset.derivedTextId = String(pinnedId);
      replay.dataset.startMs = String(result.start_ms); replay.dataset.endMs = String(result.end_ms);
      replay.textContent = 'Odtwórz zapisany cytat'; replay.disabled = !player || !savedBounds(replay);
      const packet = document.createElement('a');
      packet.href = `/api/artifacts/${identity.artifactId}/citations/${result.id}/packet`;
      packet.textContent = 'Pobierz metadane cytatu';
      packet.title = 'Pakiet zawiera pełną przypiętą wersję transkryptu i zapisane pochodzenie, bez nagrania.';
      packet.dataset.citationPacket = '';
      article.append(label, quote, replay, packet); $('saved').prepend(article);
    } catch (error) { if (request === generation) show(error.message); }
    finally { saving = false; refresh(); }
  });
  window.addEventListener('pagehide', () => { controller?.abort(); player?.destroy(); });
  void load();
}
