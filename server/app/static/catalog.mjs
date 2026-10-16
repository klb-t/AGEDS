// Catalog page actions: small JSON calls to /api/catalog, then a reload.
// Server-rendered HTML stays the source of truth; nothing here renders source text.

const NUMERIC = new Set(['priority', 'parent_collection_id']);

export function formFields(form, { dropEmpty = false } = {}) {
  const fields = {};
  for (const [key, raw] of new FormData(form)) {
    const value = typeof raw === 'string' ? raw.trim() : raw;
    if (value === '') {
      if (!dropEmpty) fields[key] = null;
      continue;
    }
    fields[key] = NUMERIC.has(key) ? Number.parseInt(value, 10) : value;
  }
  return fields;
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

const ACTIONS = {
  'collection-create': (form) => send('/api/catalog/collections', 'POST', formFields(form, { dropEmpty: true })),
  'collection-update': (form) => send(`/api/catalog/collections/${form.dataset.collectionId}`, 'PATCH', formFields(form)),
  mark: (form) => {
    const { priority = null, note = null } = formFields(form);
    return send('/api/catalog/marks', 'POST', {
      collectionId: Number(form.dataset.collectionId), locator: form.dataset.locator, priority, note,
    });
  },
  explain: (form) => send(`/api/catalog/collections/${form.dataset.collectionId}/explain`, 'POST', { provider: form.provider.value }),
  decision: (form, submitter) => {
    const { actor = null, reason = null } = formFields(form);
    return send(`/api/catalog/annotations/${form.dataset.annotationId}/decision`, 'POST', {
      decision: submitter?.value ?? 'promoted', actor, reason,
    });
  },
};

if (typeof document !== 'undefined') {
  const status = document.getElementById('catalog-status');
  document.addEventListener('submit', async (event) => {
    const form = event.target.closest('form[data-catalog-action]');
    if (!form) return;
    event.preventDefault();
    const action = ACTIONS[form.dataset.catalogAction];
    const buttons = form.querySelectorAll('button');
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const result = await action(form, event.submitter);
      if (result && result.status === 'not_run') {
        status.textContent = `Profil zapisany (sha256 ${result.profile_sha256.slice(0, 12)}); provider "none" niczego nie wygenerował.`;
        buttons.forEach((b) => { b.disabled = false; });
        return;
      }
      window.location.reload();
    } catch (error) {
      status.textContent = `Błąd: ${error.message}`;
      buttons.forEach((b) => { b.disabled = false; });
    }
  });
}
