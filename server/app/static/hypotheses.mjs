/**
 * /hypotheses page: forms that call the JSON API, browser-side SHA-256 check of
 * the canonical definition, and chart rendering from the stored run result.
 * Every value shown comes from the server or the stored result (textContent only).
 */
import {renderControl, renderHistogram} from './hypotheses-chart.mjs';

export async function sha256Hex(text, subtle = globalThis.crypto?.subtle) {
  if (!subtle) return null;
  const digest = await subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

/** Strict integer field: empty → undefined; anything but digits (optional minus) → error. */
export function optionalInteger(raw, name) {
  const text = String(raw ?? '').trim();
  if (!text) return undefined;
  if (!/^-?\d+$/.test(text)) throw new Error(`${name}: wymagana liczba całkowita`);
  const value = Number(text);
  if (!Number.isSafeInteger(value)) throw new Error(`${name}: liczba poza zakresem`);
  return value;
}

export function integerList(raw, name) {
  return String(raw ?? '').split(/[\s,;]+/).filter(Boolean).map(part => optionalInteger(part, name));
}

export function runOverrides(form) {
  const overrides = {};
  for (const key of ['permutations', 'seed', 'block_s']) {
    const value = optionalInteger(form.elements[key]?.value, key);
    if (value !== undefined) overrides[key] = value;
  }
  const model = form.elements.null_model?.value;
  if (model) overrides.null_model = model;
  return Object.keys(overrides).length ? overrides : undefined;
}

async function postJSON(url, body) {
  const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) {
    const detail = payload?.detail;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail ?? response.status));
  }
  return payload;
}

function status(form, text, ok = false) {
  const node = form.querySelector('[data-form-status]');
  if (node) {
    node.textContent = text;
    node.dataset.ok = ok ? '1' : '0';
  }
}

function reloadTo(hypothesisId, runId) {
  const url = new URL(window.location.href);
  url.search = '';
  if (hypothesisId) url.searchParams.set('id', hypothesisId);
  if (runId) url.searchParams.set('run', runId);
  window.location.assign(url.toString());
}

function readDefinition(form) {
  try {
    return JSON.parse(form.elements.definition.value);
  } catch (error) {
    throw new Error(`Definicja nie jest poprawnym JSON: ${error.message}`);
  }
}

function bindDefinitionForm(form) {
  const hypothesisId = form.dataset.hypothesisId;
  form.querySelector('[data-preview]')?.addEventListener('click', async () => {
    try {
      const preview = await postJSON('/api/hypotheses/preview', {definition: readDefinition(form)});
      const browserHash = await sha256Hex(preview.canonical);
      form.querySelector('[data-preview-hash]').textContent = preview.definition_sha256;
      form.querySelector('[data-browser-hash]').textContent = browserHash === null ? 'niedostępny (brak WebCrypto)'
        : browserHash === preview.definition_sha256 ? `zgodny (${browserHash.slice(0, 12)}…)` : `NIEZGODNY: ${browserHash}`;
      form.querySelector('[data-preview-canonical]').textContent = preview.canonical;
      status(form, 'Podgląd: nic nie zapisano. Hash obejmuje znormalizowaną definicję z wartościami domyślnymi.', true);
    } catch (error) { status(form, error.message); }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const body = {definition: readDefinition(form), actor: form.elements.actor.value || null,
        change_note: form.elements.change_note?.value || null};
      const result = await postJSON(hypothesisId ? `/api/hypotheses/${hypothesisId}/versions` : '/api/hypotheses', body);
      status(form, result.created ? `Zarejestrowano wersję ${result.version_number}: ${result.definition_sha256}`
        : `Bez zmian — definicja jest identyczna z wersją ${result.version_number}.`, true);
      if (result.created) reloadTo(result.hypothesis_id);
    } catch (error) { status(form, error.message); }
  });
}

function bind(selector, handler) {
  for (const form of document.querySelectorAll(selector)) {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const submit = form.querySelector('button[type=submit], button:not([type])');
      if (submit) submit.disabled = true;
      try { await handler(form); } catch (error) { status(form, error.message); } finally { if (submit) submit.disabled = false; }
    });
  }
}

export function init(root = document) {
  const page = root.querySelector('[data-hypotheses-page]');
  if (!page) return;
  const hypothesisId = page.dataset.hypothesisId || null;
  for (const form of root.querySelectorAll('[data-definition-form]')) bindDefinitionForm(form);
  bind('[data-run-form]', async form => {
    status(form, 'Liczenie… (permutacje po stronie serwera)', true);
    const run = await postJSON(`/api/hypotheses/versions/${form.elements.version_id.value}/event-study`,
      {overrides: runOverrides(form), actor: form.elements.actor.value || null});
    reloadTo(hypothesisId, run.run_id);
  });
  bind('[data-candidates-form]', async form => {
    const queries = form.elements.queries.value.split('\n').map(q => q.trim()).filter(Boolean);
    const result = await postJSON(`/api/hypotheses/${hypothesisId}/candidates`, {
      queries: queries.length ? queries : null, use_rules: form.elements.use_rules.checked,
      provider: form.elements.provider.value, opt_in: form.elements.opt_in.checked,
      limit: optionalInteger(form.elements.limit.value, 'limit') ?? 50, actor: form.elements.actor.value || null});
    status(form, `Dodano kandydatów: ${result.added_item_ids.length}; już obecne: ${result.already_present_item_ids.length}.`, true);
    reloadTo(hypothesisId);
  });
  bind('[data-add-item-form]', async form => {
    await postJSON(`/api/hypotheses/${hypothesisId}/dossier`, {
      target_kind: form.elements.target_kind.value, target_id: optionalInteger(form.elements.target_id.value, 'ID'),
      observation_id: optionalInteger(form.elements.observation_id.value, 'obserwacja') ?? null,
      classification: form.elements.classification.value || null, rationale: form.elements.rationale.value || null,
      actor: form.elements.actor.value || null});
    reloadTo(hypothesisId);
  });
  bind('[data-classify-form]', async form => {
    await postJSON(`/api/hypotheses/dossier/${form.dataset.itemId}/classify`, {
      classification: form.elements.classification.value, rationale: form.elements.rationale.value || null,
      actor: form.elements.actor?.value || null});
    reloadTo(hypothesisId);
  });
  bind('[data-thesis-form]', async form => {
    await postJSON(`/api/hypotheses/${hypothesisId}/theses`, {
      text: form.elements.text.value, status_type: form.elements.status_type.value,
      evidence_item_ids: integerList(form.elements.evidence.value, 'dowody'),
      previous_thesis_id: optionalInteger(form.elements.previous_thesis_id.value, 'poprzednia teza') ?? null,
      actor: form.elements.actor.value || null});
    reloadTo(hypothesisId);
  });
  bind('[data-disclosure-form]', async form => {
    await postJSON(`/api/hypotheses/${hypothesisId}/disclosure`, {
      quadrant: form.dataset.quadrant, text: form.elements.text.value, actor: form.elements.actor?.value || null});
    reloadTo(hypothesisId);
  });
  const data = root.getElementById?.('hyp-run-data') ?? document.getElementById('hyp-run-data');
  if (data) {
    const result = JSON.parse(data.textContent);
    const histogram = root.querySelector('[data-histogram]');
    if (histogram && result.lag_histogram) renderHistogram(histogram, result.lag_histogram, result.definition.windows);
    const control = root.querySelector('[data-control-series]');
    if (control && result.control_series) renderControl(control, result.control_series);
    page.dataset.chartsRendered = '1';
  }
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => init());
  else init();
}
