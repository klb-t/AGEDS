// Progressive enhancements for workbench pages. Forms work without this module.
import {loadTranslator, readShellConfig} from './workbench-i18n.mjs';

export async function copyText(text, {clipboard = globalThis.navigator?.clipboard} = {}) {
  try {await clipboard.writeText(text); return true;} catch {return false;}
}

async function start(doc = document, win = window) {
  const config = readShellConfig(doc);
  const t = await loadTranslator(config.lang || doc.documentElement.lang || 'pl', {fetchImpl: (...a) => win.fetch(...a)});
  for (const all of doc.querySelectorAll('[data-wb-select-all]')) {
    all.addEventListener('change', () => {
      for (const box of all.form?.querySelectorAll('[data-wb-target]') || []) box.checked = all.checked;
    });
  }
  for (const form of doc.querySelectorAll('form[data-wb-confirm]')) {
    form.addEventListener('submit', event => {if (!win.confirm(form.getAttribute('data-wb-confirm'))) event.preventDefault();});
  }
  for (const button of doc.querySelectorAll('[data-wb-copy-citation]')) {
    button.addEventListener('click', async () => {
      const status = button.closest('section, .panel')?.querySelector('[data-wb-copy-status]') || doc.querySelector('[data-wb-copy-status]');
      const id = button.getAttribute('data-wb-copy-citation');
      if (!/^[1-9][0-9]{0,15}$/.test(id || '')) return;
      try {
        const response = await win.fetch(`/api/workbench/citations/${id}/text?lang=${encodeURIComponent(config.lang || 'pl')}`);
        if (!response.ok) throw new Error(String(response.status));
        const data = await response.json();
        const copied = await copyText(data.text, {clipboard: win.navigator?.clipboard});
        if (status) status.textContent = copied ? t('wb.copy.done', {id}) : t('wb.copy.manual', {id});
        if (!copied) {
          const area = doc.createElement('textarea'); area.readOnly = true; area.className = 'wb-copy-fallback';
          area.value = data.text; area.setAttribute('data-wb-copy-fallback', id);
          button.after(area); area.select();
        }
        button.setAttribute('data-wb-copied', copied ? 'clipboard' : 'manual');
      } catch {
        if (status) status.textContent = t('wb.copy.failed', {id});
      }
    });
  }
  doc.documentElement.dataset.wbPage = 'ready';
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  start().catch(error => {try {console.warn('workbench page enhancements disabled:', error);} catch {}});
}
