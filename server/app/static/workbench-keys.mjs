// Keyboard shortcut registry with conflict detection, key sequences and a help overlay.
// Bindings come from /static/workbench-keys.json (no hidden state). Keys are ignored while the
// focus is in an input, textarea, select or editable content, so typing is never intercepted.
const MODIFIERS = ['Ctrl', 'Alt', 'Meta', 'Shift'];
const ALIASES = {control: 'Ctrl', ctrl: 'Ctrl', alt: 'Alt', option: 'Alt', meta: 'Meta', cmd: 'Meta', command: 'Meta', shift: 'Shift'};
const KEY_NAMES = {esc: 'Escape', escape: 'Escape', space: 'Space', ' ': 'Space', enter: 'Enter', tab: 'Tab',
  arrowleft: 'ArrowLeft', arrowright: 'ArrowRight', arrowup: 'ArrowUp', arrowdown: 'ArrowDown',
  pageup: 'PageUp', pagedown: 'PageDown', home: 'Home', end: 'End', backspace: 'Backspace', delete: 'Delete'};
export const EDITABLE_SELECTOR = 'input,textarea,select,[contenteditable=""],[contenteditable="true"],[contenteditable="plaintext-only"]';

function canonical(mods, key) {
  const printable = key.length === 1;
  const hasCommand = mods.has('Ctrl') || mods.has('Alt') || mods.has('Meta');
  // A printable character already reflects Shift (e.g. '?'), so Shift is not part of its identity.
  const parts = MODIFIERS.filter(m => mods.has(m) && !(m === 'Shift' && printable));
  const name = printable && hasCommand ? key.toLowerCase() : key;
  return [...parts, name].join('+');
}

/** "Ctrl+K", "meta+k", "?", "Shift+Tab", "+" -> canonical combo string; throws on unknown modifiers. */
export function normalizeCombo(text) {
  if (typeof text !== 'string' || !text) throw new Error('empty key combo');
  const tokens = text === '+' ? ['+'] : text.endsWith('++') ? [...text.slice(0, -2).split('+'), '+'] : text.split('+');
  const key = tokens.pop();
  const mods = new Set();
  for (const token of tokens) {
    const mod = ALIASES[token.toLowerCase()];
    if (!mod) throw new Error(`unknown modifier: ${token}`);
    mods.add(mod);
  }
  if (!key) throw new Error(`missing key in ${text}`);
  const named = KEY_NAMES[key.toLowerCase()] || (key.length === 1 ? key : key[0].toUpperCase() + key.slice(1));
  return canonical(mods, named);
}

export function parseSequence(text) {
  return String(text).trim().split(/\s+/).map(normalizeCombo);
}

export function comboFromEvent(event) {
  const mods = new Set();
  if (event.ctrlKey) mods.add('Ctrl');
  if (event.altKey) mods.add('Alt');
  if (event.metaKey) mods.add('Meta');
  if (event.shiftKey) mods.add('Shift');
  const raw = event.key === ' ' ? 'Space' : event.key;
  if (!raw || ['Control', 'Alt', 'Meta', 'Shift'].includes(raw)) return null;
  return canonical(mods, KEY_NAMES[raw.toLowerCase()] && raw.length > 1 ? KEY_NAMES[raw.toLowerCase()] : raw);
}

export function isEditable(target) {
  try {return Boolean(target?.closest?.(EDITABLE_SELECTOR) || target?.isContentEditable);} catch {return false;}
}

const sameOrPrefix = (a, b) => {const n = Math.min(a.length, b.length); for (let i = 0; i < n; i++) if (a[i] !== b[i]) return false; return true;};
const scopesOverlap = (a, b, page) => {
  if (a === 'overlay' || b === 'overlay') return a === b;
  const active = s => s === 'global' || s === '*' || s === page;
  return active(a) && active(b);
};

export class KeyRegistry {
  constructor({timeoutMs = 1200, now = () => Date.now(), page = '/'} = {}) {
    this.timeoutMs = timeoutMs; this.now = now; this.page = page;
    this.bindings = []; this.reserved = []; this.conflicts = []; this.buffer = []; this.lastAt = 0;
  }

  /** Reserve keys handled by a page itself (documentation + conflict detection only). */
  reserve({page = '*', owner = 'page', keys = [], label = ''}) {
    const sequences = keys.map(parseSequence);
    this.reserved.push({page, owner, keys, label, sequences});
  }

  /** Register a binding; the first registration wins and every clash is recorded, never silently shadowed. */
  register({id, keys = [], scope = 'global', label = '', handler = null, href = null}) {
    const sequences = keys.map(parseSequence);
    const clashes = [];
    for (const seq of sequences) {
      for (const other of this.bindings) for (const theirs of other.sequences)
        if (scopesOverlap(scope, other.scope, this.page) && sameOrPrefix(seq, theirs)) clashes.push({with: other.id, keys: theirs.join(' ')});
      for (const res of this.reserved) for (const theirs of res.sequences)
        if (scopesOverlap(scope, res.page, this.page) && sameOrPrefix(seq, theirs)) clashes.push({with: `reserved:${res.owner}`, keys: theirs.join(' ')});
    }
    if (clashes.length) {this.conflicts.push({id, clashes}); return {ok: false, clashes};}
    this.bindings.push({id, keys, scope, label, handler, href, sequences});
    return {ok: true, clashes: []};
  }

  list() {return this.bindings.map(({id, keys, scope, label, href}) => ({id, keys, scope, label, href}));}

  /** Feed a keydown; returns the handled binding id or null. Editable targets are never intercepted. */
  handle(event, {overlayOpen = false} = {}) {
    if (!overlayOpen && isEditable(event.target)) {this.buffer = []; return null;}
    const combo = comboFromEvent(event);
    if (!combo) return null;
    const at = this.now();
    if (at - this.lastAt > this.timeoutMs) this.buffer = [];
    this.lastAt = at;
    const active = this.bindings.filter(b => overlayOpen ? b.scope === 'overlay' : ['global', '*', this.page].includes(b.scope));
    const match = attempt => {
      let partial = false;
      for (const binding of active) for (const seq of binding.sequences) {
        if (seq.length === attempt.length && seq.every((c, i) => c === attempt[i])) return {binding};
        if (seq.length > attempt.length && attempt.every((c, i) => c === seq[i])) partial = true;
      }
      return {partial};
    };
    let attempt = [...this.buffer, combo];
    let result = match(attempt);
    if (!result.binding && !result.partial && this.buffer.length) {attempt = [combo]; result = match(attempt);}
    if (result.binding) {
      this.buffer = [];
      event.preventDefault?.();
      result.binding.handler?.(event, result.binding);
      return result.binding.id;
    }
    this.buffer = result.partial ? attempt : [];
    return null;
  }
}

/** Render the help table into an element using textContent only. */
export function renderHelp(doc, container, registry, t) {
  container.replaceChildren();
  const heading = doc.createElement('h2'); heading.textContent = t('keys.overlay_title'); container.append(heading);
  const hint = doc.createElement('p'); hint.textContent = t('keys.overlay_hint'); container.append(hint);
  const table = doc.createElement('table');
  const addRow = (keys, text) => {
    const row = doc.createElement('tr'); const keyCell = doc.createElement('td'); const textCell = doc.createElement('td');
    keys.forEach((k, i) => {const kbd = doc.createElement('kbd'); kbd.textContent = k; keyCell.append(kbd); if (i < keys.length - 1) keyCell.append(' / ');});
    textCell.textContent = text; row.append(keyCell, textCell); table.append(row);
  };
  for (const binding of registry.list()) addRow(binding.keys, t(binding.label));
  for (const res of registry.reserved) addRow(res.keys, t(res.label));
  container.append(table);
  if (registry.conflicts.length) {
    const warn = doc.createElement('p'); warn.className = 'wb-alert'; warn.setAttribute('data-wb-key-conflicts', String(registry.conflicts.length));
    warn.textContent = t('keys.conflicts', {list: registry.conflicts.map(c => `${c.id} ↔ ${c.clashes.map(x => x.with).join(', ')}`).join('; ')});
    container.append(warn);
  }
}
