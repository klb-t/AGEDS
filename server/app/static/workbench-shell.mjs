// Shared page shell loaded by _nav.html on every page: theme toggle, language switch,
// keyboard shortcuts with a help overlay (?), a command palette (Ctrl/Cmd+K) and a
// per-browser "recently viewed" list. Everything is optional: failures leave the page usable.
import {loadTranslator, readShellConfig} from './workbench-i18n.mjs';
import {applyTheme, effectiveTheme, nextPreference, prefersLight, readPreference, supportsLight, writeCookie, COOKIE as THEME_COOKIE} from './workbench-theme.mjs';
import {KeyRegistry, renderHelp} from './workbench-keys.mjs';

export const RECENT_KEY = 'ageds.recent.v1';
export const RECENT_MAX = 20;

export function readRecent(storage) {
  try {
    const value = JSON.parse(storage?.getItem(RECENT_KEY) || '[]');
    return Array.isArray(value) ? value.filter(e => e && typeof e.path === 'string' && e.path.startsWith('/') && !e.path.startsWith('//')) : [];
  } catch {return [];}
}

export function recordRecent(storage, entry) {
  try {
    const list = [entry, ...readRecent(storage).filter(e => e.path !== entry.path)].slice(0, RECENT_MAX);
    storage.setItem(RECENT_KEY, JSON.stringify(list));
    return list;
  } catch {return null;}
}

function localStorageOrNull(win) {
  try {return win.localStorage;} catch {return null;}
}

function overlay(doc, name, labelKey, t) {
  const backdrop = doc.createElement('div');
  backdrop.className = 'wb-overlay'; backdrop.hidden = true; backdrop.setAttribute('data-wb-overlay', name);
  const dialog = doc.createElement('div');
  dialog.className = 'wb-dialog'; dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-label', t(labelKey)); dialog.tabIndex = -1;
  backdrop.append(dialog); doc.body.append(backdrop);
  let returnFocus = null;
  const api = {
    element: backdrop, dialog,
    open() {returnFocus = doc.activeElement; backdrop.hidden = false; dialog.focus(); doc.documentElement.dataset.wbOpenOverlay = name;},
    close() {backdrop.hidden = true; delete doc.documentElement.dataset.wbOpenOverlay; try {returnFocus?.focus?.();} catch {}},
    get isOpen() {return !backdrop.hidden;},
  };
  backdrop.addEventListener('click', event => {if (event.target === backdrop) api.close();});
  return api;
}

function setupTheme(doc, win, t) {
  const root = doc.documentElement;
  const button = doc.querySelector('[data-wb-theme-toggle]');
  const supported = supportsLight(root);
  let preference = readPreference(doc);
  if (supported) applyTheme(root, preference);
  const label = () => {
    if (!button) return;
    const shown = effectiveTheme(preference, prefersLight(win), supported);
    button.textContent = t(`shell.theme.${preference}`);
    button.title = supported ? t('shell.theme.title_state', {shown: t(`shell.theme.shown.${shown}`)}) : t('shell.theme.unsupported');
    button.setAttribute('data-wb-theme-preference', preference);
    root.dataset.wbThemeShown = shown;
  };
  const toggle = () => {
    if (!supported) return;
    preference = nextPreference(preference);
    applyTheme(root, preference);
    const persisted = writeCookie(doc, THEME_COOKIE, preference);
    root.dataset.wbThemePersisted = String(persisted);
    label();
  };
  if (button) {button.disabled = !supported; button.addEventListener('click', toggle);}
  try {win.matchMedia?.('(prefers-color-scheme: light)').addEventListener?.('change', label);} catch {}
  label();
  return toggle;
}

function setupLanguage(doc, win, config) {
  const select = doc.querySelector('[data-wb-lang]');
  if (!select) return;
  select.addEventListener('change', () => {
    const lang = select.value;
    const url = new URL(win.location.href);
    url.searchParams.delete('lang');
    const persisted = writeCookie(doc, config.langCookie || 'ageds_lang', lang);
    if (!persisted) url.searchParams.set('lang', lang);
    win.location.assign(url.toString());
  });
}

function navLinks(doc) {
  return [...doc.querySelectorAll('.ageds-nav a[href]')].map(a => ({label: a.textContent.trim(), href: a.getAttribute('href')}));
}

function setupPalette(doc, win, t, actions, storage) {
  const box = overlay(doc, 'palette', 'palette.title', t);
  const input = doc.createElement('input');
  input.type = 'search'; input.setAttribute('data-wb-palette-input', ''); input.setAttribute('aria-label', t('palette.placeholder'));
  input.placeholder = t('palette.placeholder');
  const list = doc.createElement('ul'); list.className = 'wb-palette-list'; list.setAttribute('role', 'listbox');
  const hint = doc.createElement('p'); hint.className = 'wb-meta'; hint.textContent = t('palette.hint');
  box.dialog.append(input, list, hint);
  let entries = [], selected = 0, jumpSeq = 0;
  const base = () => [
    ...navLinks(doc).map(l => ({label: l.label, detail: l.href, run: () => win.location.assign(l.href)})),
    ...actions.map(a => ({label: a.label, detail: t('palette.action'), run: a.run})),
    ...readRecent(storage).map(r => ({label: r.title || r.path, detail: t('palette.recent'), run: () => win.location.assign(r.path)})),
  ];
  const render = () => {
    list.replaceChildren();
    entries.forEach((entry, index) => {
      const item = doc.createElement('li'); item.setAttribute('role', 'option'); item.setAttribute('aria-selected', String(index === selected));
      item.setAttribute('data-wb-palette-entry', entry.kind || 'entry');
      const text = doc.createElement('span'); text.textContent = entry.label;
      const detail = doc.createElement('small'); detail.textContent = entry.detail || '';
      item.append(text, detail); item.addEventListener('click', () => {box.close(); entry.run();});
      list.append(item);
    });
  };
  const refresh = async () => {
    const query = input.value.trim();
    const needle = query.toLowerCase();
    entries = base().filter(e => !needle || `${e.label} ${e.detail}`.toLowerCase().includes(needle));
    if (query) entries.push({kind: 'search', label: t('palette.search_layers', {q: query}), detail: '/workbench',
      run: () => win.location.assign(`/workbench?q=${encodeURIComponent(query)}`)});
    selected = 0; render();
    const ticket = ++jumpSeq;
    if (!query || query.length > 128) return;
    try {
      const response = await win.fetch(`/api/workbench/jump?q=${encodeURIComponent(query)}`);
      if (!response.ok || ticket !== jumpSeq) return;
      const data = await response.json();
      const jumps = (data.candidates || []).filter(c => typeof c.url === 'string' && c.url.startsWith('/') && !c.url.startsWith('//'))
        .map(c => ({kind: 'jump', label: `${t('kind.' + c.kind)} #${c.id} · ${c.label || ''}`, detail: c.detail || '', run: () => win.location.assign(c.url)}));
      if (ticket === jumpSeq && jumps.length) {entries = [...jumps, ...entries]; selected = 0; render();}
    } catch {}
  };
  input.addEventListener('input', refresh);
  input.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      selected = (selected + (event.key === 'ArrowDown' ? 1 : -1) + entries.length) % Math.max(entries.length, 1); render();
    } else if (event.key === 'Enter') {
      event.preventDefault(); const entry = entries[selected]; if (entry) {box.close(); entry.run();}
    }
  });
  return {open() {input.value = ''; refresh(); box.open(); input.focus();}, box};
}

async function start(doc = document, win = window) {
  const root = doc.documentElement;
  const config = readShellConfig(doc);
  const lang = config.lang || root.lang || 'pl';
  if (supportsLight(root)) applyTheme(root, readPreference(doc));
  const t = await loadTranslator(lang, {fetchImpl: (...args) => win.fetch(...args)});
  const toggleTheme = setupTheme(doc, win, t);
  setupLanguage(doc, win, config);
  const storage = localStorageOrNull(win);
  const help = overlay(doc, 'help', 'keys.overlay_title', t);
  let keyFile = {bindings: [], reserved: [], sequence_timeout_ms: 1200};
  try {const response = await win.fetch(config.keysUrl || '/static/workbench-keys.json'); if (response.ok) keyFile = await response.json();} catch {}
  const registry = new KeyRegistry({timeoutMs: keyFile.sequence_timeout_ms || 1200, page: win.location.pathname});
  for (const reserved of keyFile.reserved || []) {try {registry.reserve(reserved);} catch {}}
  const hrefs = new Set(navLinks(doc).map(l => l.href));
  const openHelp = () => {renderHelp(doc, help.dialog, registry, t); palette.box.close(); help.open();};
  const actions = [
    {label: t('keys.toggle_theme'), run: () => toggleTheme()},
    {label: t('keys.help'), run: () => openHelp()},
    ...(config.languages || []).map(code => ({label: t('palette.language', {name: t(`lang.${code}`)}), run: () => {
      const select = doc.querySelector('[data-wb-lang]'); if (select) {select.value = code; select.dispatchEvent(new Event('change'));}
    }})),
  ];
  const palette = setupPalette(doc, win, t, actions, storage);
  const handlers = {
    help: openHelp,
    palette: () => {help.close(); palette.open();},
    'focus-search': () => {const field = doc.querySelector('[data-wb-search], input[name="q"]'); field?.focus(); field?.select?.();},
    'toggle-theme': () => toggleTheme(),
    close: () => {help.close(); palette.box.close();},
  };
  for (const binding of keyFile.bindings || []) {
    if (binding.requires_nav && !hrefs.has(binding.href)) continue;
    const handler = handlers[binding.id] || (binding.href ? () => win.location.assign(binding.href) : null);
    try {registry.register({...binding, handler});} catch (error) {registry.conflicts.push({id: binding.id, clashes: [{with: String(error.message)}]});}
  }
  doc.addEventListener('keydown', event => {
    const overlayOpen = help.isOpen || palette.box.isOpen;
    if (overlayOpen && event.target?.closest?.('[data-wb-palette-input]') && event.key !== 'Escape') return;
    registry.handle(event, {overlayOpen});
  });
  doc.querySelector('[data-wb-help]')?.addEventListener('click', openHelp);
  doc.querySelector('[data-wb-palette]')?.addEventListener('click', () => palette.open());
  if (storage) recordRecent(storage, {path: win.location.pathname + win.location.search, title: doc.title, at: new Date().toISOString()});
  root.dataset.wbKeyConflicts = String(registry.conflicts.length);
  root.dataset.wbShell = 'ready';
  return {registry, t};
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  start().catch(error => {try {console.warn('workbench shell disabled:', error); document.documentElement.dataset.wbShell = 'failed';} catch {}});
}
