// Theme preference: 'auto' (follow prefers-color-scheme), 'dark' or 'light'.
// Stored per browser in the ageds_theme cookie so the server can render data-theme without a flash.
// Every storage access is wrapped: without cookies the page still renders (auto/dark) correctly.
export const THEMES = Object.freeze(['auto', 'dark', 'light']);
export const COOKIE = 'ageds_theme';
const MAX_AGE = 60 * 60 * 24 * 365;

export function readCookie(doc, name) {
  try {
    for (const part of String(doc.cookie || '').split(';')) {
      const [key, ...rest] = part.trim().split('=');
      if (key === name) return decodeURIComponent(rest.join('='));
    }
  } catch {}
  return null;
}

export function writeCookie(doc, name, value) {
  try {
    doc.cookie = `${name}=${encodeURIComponent(value)}; Path=/; Max-Age=${MAX_AGE}; SameSite=Lax`;
    return readCookie(doc, name) === value;
  } catch {return false;}
}

export function readPreference(doc) {
  const value = readCookie(doc, COOKIE);
  return THEMES.includes(value) ? value : 'auto';
}

export function supportsLight(root) {
  return String(root?.getAttribute?.('data-theme-support') || '').split(/\s+/).includes('light');
}

/** The theme actually shown: explicit choice, else the system preference, else dark. */
export function effectiveTheme(preference, prefersLight, lightSupported = true) {
  if (!lightSupported) return 'dark';
  if (preference === 'light' || preference === 'dark') return preference;
  return prefersLight ? 'light' : 'dark';
}

export function nextPreference(preference) {
  return {auto: 'light', light: 'dark', dark: 'auto'}[preference] || 'auto';
}

/** Reflect the preference on <html>; 'auto' removes data-theme so the CSS media query decides. */
export function applyTheme(root, preference) {
  if (!root?.setAttribute) return;
  if (preference === 'light' || preference === 'dark') root.setAttribute('data-theme', preference);
  else root.removeAttribute('data-theme');
}

export function prefersLight(win) {
  try {return Boolean(win?.matchMedia?.('(prefers-color-scheme: light)').matches);} catch {return false;}
}
