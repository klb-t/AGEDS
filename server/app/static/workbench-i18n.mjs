// Browser side of the workbench i18n (same catalogs and rules as server/app/workbench/i18n.py).
// Pure functions are exported for Node tests; nothing here touches the DOM except readShellConfig().
export const LANGUAGES = Object.freeze(['pl', 'nl', 'en']);
export const DEFAULT_LANGUAGE = 'pl';
const PLACEHOLDER = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;
const TAG = /^[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*$|^\*$/;

/** CLDR cardinal category for integers (pl: one/few/many, nl/en: one/other); non-integers -> other. */
export function pluralCategory(lang, n) {
  if (!Number.isInteger(n)) return 'other';
  const v = Math.abs(n);
  if (lang === 'pl') {
    if (v === 1) return 'one';
    if ([2, 3, 4].includes(v % 10) && ![12, 13, 14].includes(v % 100)) return 'few';
    return 'many';
  }
  return v === 1 ? 'one' : 'other';
}

export function formatTemplate(template, params = {}) {
  return template.replace(PLACEHOLDER, (whole, name) => (Object.hasOwn(params, name) ? String(params[name]) : whole));
}

function resolve(value, lang, params) {
  if (value && typeof value === 'object') {
    const category = pluralCategory(lang, params.n);
    return formatTemplate(value[category] || value.other, params);
  }
  return formatTemplate(value, params);
}

/**
 * t(key, params): chosen catalog -> Polish fallback (reported once) -> visible "⟦key⟧" (reported once).
 * Never returns an empty string.
 */
export function createTranslator({lang = DEFAULT_LANGUAGE, catalog = {}, fallback = {}, report = () => {}} = {}) {
  const reported = new Set();
  const once = (level, key) => {const marker = `${level}:${key}`; if (!reported.has(marker)) {reported.add(marker); report(level, key, lang);}};
  const usable = value => typeof value === 'string' ? value.length > 0 : Boolean(value && typeof value === 'object' && value.other);
  return function t(key, params = {}) {
    if (key !== '_meta' && Object.hasOwn(catalog, key) && usable(catalog[key])) return resolve(catalog[key], lang, params);
    if (key !== '_meta' && Object.hasOwn(fallback, key) && usable(fallback[key])) {
      once('warn', key);
      return resolve(fallback[key], DEFAULT_LANGUAGE, params);
    }
    once('error', key);
    return `⟦${key}⟧`;
  };
}

/** Bounded Accept-Language parsing: [[primary subtag, q]] by q, header order for ties. */
export function parseAcceptLanguage(header) {
  if (typeof header !== 'string' || !header || header.length > 1024) return [];
  const entries = [];
  header.split(',').slice(0, 32).forEach((part, index) => {
    const [tag, ...params] = part.split(';').map(s => s.trim());
    if (!tag || !TAG.test(tag)) return;
    let q = 1;
    for (const param of params) if (param.slice(0, 2).toLowerCase() === 'q=') {q = Number(param.slice(2)); if (Number.isNaN(q)) q = -1;}
    if (!(q > 0 && q <= 1)) return;
    entries.push([tag.split('-')[0].toLowerCase(), q, index]);
  });
  entries.sort((a, b) => b[1] - a[1] || a[2] - b[2]);
  return entries.map(([tag, q]) => [tag, q]);
}

/** Same precedence as the server: query, cookie, Accept-Language/navigator languages, default. */
export function negotiate({query = null, cookie = null, acceptLanguage = null, navigatorLanguages = []} = {}) {
  if (LANGUAGES.includes(query)) return {lang: query, source: 'query'};
  if (LANGUAGES.includes(cookie)) return {lang: cookie, source: 'cookie'};
  for (const [tag] of parseAcceptLanguage(acceptLanguage)) if (LANGUAGES.includes(tag)) return {lang: tag, source: 'accept-language'};
  for (const item of navigatorLanguages || []) {
    const tag = String(item).split('-')[0].toLowerCase();
    if (LANGUAGES.includes(tag)) return {lang: tag, source: 'navigator'};
  }
  return {lang: DEFAULT_LANGUAGE, source: 'default'};
}

/** Fetch the chosen catalog and the Polish fallback; failures degrade to visible keys, never throw. */
export async function loadTranslator(lang, {fetchImpl = globalThis.fetch, pattern = '/static/i18n/{lang}.json', report} = {}) {
  const load = async code => {
    try {const response = await fetchImpl(pattern.replace('{lang}', code)); return response.ok ? await response.json() : {};}
    catch {return {};}
  };
  const safeLang = LANGUAGES.includes(lang) ? lang : DEFAULT_LANGUAGE;
  const [catalog, fallback] = await Promise.all([load(safeLang), safeLang === DEFAULT_LANGUAGE ? Promise.resolve(null) : load(DEFAULT_LANGUAGE)]);
  const logger = report || ((level, key, l) => {try {console[level === 'error' ? 'error' : 'warn'](`i18n: missing ${key} (${l})`);} catch {}});
  return createTranslator({lang: safeLang, catalog, fallback: fallback || catalog, report: logger});
}

export function readShellConfig(doc) {
  try {return JSON.parse(doc.getElementById('ageds-shell-config')?.textContent || '{}');} catch {return {};}
}
