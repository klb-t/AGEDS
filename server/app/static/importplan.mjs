// Import plan page: small JSON calls to /api/import-plans, then a reload.
// Server-rendered HTML stays the source of truth; nothing here renders source text
// (status messages use textContent only).

export function decisionFromFields({ itemId, entries, optInBoxes = [], status = null }) {
  const decision = { itemId: Number(itemId) };
  if (status) decision.status = status;
  const optIns = {};
  for (const [name, checked] of optInBoxes) optIns[name.slice('optin:'.length)] = Boolean(checked);
  if (Object.keys(optIns).length) decision.optIns = optIns;
  const parameters = {};
  for (const [key, raw] of entries) {
    const value = typeof raw === 'string' ? raw.trim() : raw;
    if (key === 'importer') {
      if (value !== '') decision.importer = value;
      continue;
    }
    if (!key.startsWith('param:')) continue;
    const name = key.slice('param:'.length);
    if (value === '') parameters[name] = null;
    else if (name === 'coarse_decimals' && /^-?\d+$/.test(value)) parameters[name] = Number.parseInt(value, 10);
    else parameters[name] = value;
  }
  if (Object.keys(parameters).length) decision.parameters = parameters;
  return decision;
}

export function decisionFromForm(form, submitter) {
  return decisionFromFields({
    itemId: form.dataset.itemId,
    entries: [...new FormData(form)],
    optInBoxes: [...form.querySelectorAll('input[type=checkbox][name^="optin:"]')].map((box) => [box.name, box.checked]),
    status: submitter?.value ?? null,
  });
}

export function bulkPayload(action, selectedIds) {
  if (action === 'approve-ready') return { action: 'approve' };
  const ids = selectedIds.map(Number).filter((n) => Number.isSafeInteger(n) && n > 0);
  if (!ids.length) throw new Error('Nie zaznaczono żadnej pozycji.');
  return { action: action === 'skip-selected' ? 'skip' : 'approve', itemIds: ids };
}

async function send(url, method, body) {
  const response = await fetch(url, {
    method, headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail ?? response.statusText));
  }
  return payload;
}

if (typeof document !== 'undefined') {
  const status = document.getElementById('importplan-status');
  const main = document.querySelector('main.importplan');
  const actor = () => (document.getElementById('ip-actor')?.value || 'web').trim() || 'web';
  const planId = () => main?.dataset.planId;
  const baseVersion = () => Number(main?.dataset.baseVersion);
  const selected = () => [...document.querySelectorAll('input.ip-select:checked')].map((box) => box.value);

  const ACTIONS = {
    create: async (form) => {
      const collectionIds = [...form.querySelectorAll('input[name=collection]:checked')].map((box) => Number(box.value));
      if (!collectionIds.length) throw new Error('Wybierz co najmniej jedną kolekcję.');
      const billing = form.querySelector('select[name=billingProfileId]')?.value || null;
      const result = await send('/api/import-plans', 'POST', {
        collectionIds, actor: actor(), options: billing ? { billingProfileId: billing } : {},
      });
      window.location.assign(`/import?plan=${result.plan.id}`);
      return 'navigating';
    },
    bulk: (form, submitter) => send(`/api/import-plans/${planId()}/decisions`, 'POST', {
      baseVersion: baseVersion(), actor: actor(), bulk: bulkPayload(submitter?.value ?? 'approve-ready', selected()),
    }),
    decide: (form, submitter) => send(`/api/import-plans/${planId()}/decisions`, 'POST', {
      baseVersion: baseVersion(), actor: actor(), decisions: [decisionFromForm(form, submitter)],
    }),
    run: (form) => send(`/api/import-plans/${planId()}/runs`, 'POST', {
      actor: actor(), mode: form.querySelector('select[name=mode]').value,
      rerunDone: form.querySelector('input[name=rerunDone]').checked,
      recoverRunning: form.querySelector('input[name=recoverRunning]').checked,
    }),
  };

  document.addEventListener('submit', async (event) => {
    const form = event.target.closest('form[data-ip-action]');
    if (!form) return;
    event.preventDefault();
    const action = ACTIONS[form.dataset.ipAction];
    const buttons = form.querySelectorAll('button');
    buttons.forEach((b) => { b.disabled = true; });
    status.textContent = form.dataset.ipAction === 'run' ? 'Import w toku…' : 'Zapisywanie…';
    try {
      const result = await action(form, event.submitter);
      if (result === 'navigating') return;
      if (result && Array.isArray(result.not_applied) && result.not_applied.length) {
        sessionStorageSafe('ip-notice', `Pominięto ${result.not_applied.length} zablokowanych pozycji (niejednoznaczne, opt-in lub brak parametru).`);
      }
      window.location.reload();
    } catch (error) {
      status.textContent = `Błąd: ${error.message}`;
      buttons.forEach((b) => { b.disabled = false; });
    }
  });

  function sessionStorageSafe(key, value) {
    try { window.sessionStorage.setItem(key, value); } catch { /* storage may be unavailable */ }
  }
  try {
    const notice = window.sessionStorage.getItem('ip-notice');
    if (notice) {
      status.textContent = notice;
      window.sessionStorage.removeItem('ip-notice');
    }
  } catch { /* storage may be unavailable */ }
}
