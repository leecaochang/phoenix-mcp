// Panel string catalog.
//
// Strings live in custom_components/phoenix_mcp/catalogs/{lang}.json under the
// "panel" key and arrive from Phoenix's own admin API. They used to come over
// HA's frontend/get_translations websocket command, which reads translations/
// and nothing else; they had to leave that directory because hassfest rejects
// any top-level key outside HA's own closed set of categories, which fails the
// HACS submission.
//
// helpers.panel_catalog reproduces both behaviours that move cost us: English is
// the base with the requested language overlaid, so a key a translator has not
// reached resolves to English server-side and this module needs no fallback
// chain of its own; and a translated string whose {placeholder} set differs from
// English is dropped in favour of English, which is why the interpolation syntax
// here is Python format style rather than something else.
//
// This module must never import from api.ts. 17 test files mock "../api" with
// exhaustive factory objects, so an import edge would break all of them at once.

import { setFormatLocale } from "./format";

// Date, time and number formatting lives in ./format and is re-exported here so
// every consumer has one import for "anything locale-dependent". The dependency
// runs one way only (format imports nothing), which is what lets loadTranslations
// adopt the locale below without a cycle.
export {
  compareStrings,
  getFormatLocale,
  localeCompactNumber,
  localeClock,
  localeDate,
  localeDateTime,
  localeDateTimeShort,
  localeNumber,
  setFormatLocale,
} from "./format";
export type { HassLocale } from "./format";

// Phoenix serves its own catalog, already flattened to dotted keys and already
// English-backed, so there is no prefix to strip and no fallback chain here.
const CATALOG_URL = "/api/phoenix-mcp/admin/catalog/";

export type Params = Record<string, string | number>;

/**
 * The locales this build ships, each labelled with its own endonym.
 *
 * The endonym is deliberately NOT a catalog entry. A language picker shows every
 * option in its own language so a reader can find theirs even when the UI is
 * currently in one they cannot read, which means the label is the same string in
 * every catalog and translating it would be wrong rather than merely redundant.
 *
 * Must stay in step with custom_components/phoenix_mcp/catalogs/*.json;
 * i18n_keys.test.ts fails if it drifts.
 */
export const LANGUAGES: ReadonlyArray<{ code: string; endonym: string }> = [
  { code: "en", endonym: "English" },
  { code: "es", endonym: "Español" },
  { code: "fr", endonym: "Français" },
  { code: "ja", endonym: "日本語" },
  { code: "ko", endonym: "한국어" },
  { code: "zh-Hans", endonym: "中文（简体）" },
  { code: "zh-Hant", endonym: "繁體中文" },
];

/** Follow the Home Assistant user profile rather than overriding it. */
export const LANGUAGE_AUTO = "auto";

// Per browser, same shape as the "phx-theme" preference, and for the same
// reason: the two injected bundles load on arbitrary HA pages outside the panel
// and read it from the same origin without needing an API call.
const LANGUAGE_KEY = "phx-lang";

/** The stored override, or LANGUAGE_AUTO when unset or no longer shipped. */
export function getLanguagePreference(): string {
  let saved: string | null = null;
  try { saved = localStorage.getItem(LANGUAGE_KEY); } catch { /* storage blocked: use auto */ }
  return LANGUAGES.some((l) => l.code === saved) ? (saved as string) : LANGUAGE_AUTO;
}

/**
 * Persist the override and tell the other bundles. The panel repaints itself;
 * the global Agent Chat window and the injected buttons live outside this
 * element and cannot see the change any other way.
 */
export function setLanguagePreference(pref: string): void {
  try { localStorage.setItem(LANGUAGE_KEY, pref); } catch { /* storage blocked: skip persistence */ }
  window.dispatchEvent(new CustomEvent("phx-language-changed"));
}

/** The language to actually fetch: the override, else HA's profile language. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function resolveLanguage(hass: any): string {
  const pref = getLanguagePreference();
  if (pref !== LANGUAGE_AUTO) return pref;
  return hass?.language || "en";
}

let strings: Record<string, string> = {};
let ready = false;
let loaded: string | null = null;

// The injected bundles can outlive one <home-assistant> root while HA replaces
// it during navigation or a frontend reload. Prefer the current root whenever
// it exposes HA's authenticated fetch, then fall back to the captured object.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function catalogHass(fallback: any): any {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const live = (document.querySelector("home-assistant") as any)?.hass;
    if (typeof live?.fetchWithAuth === "function") return live;
  } catch { /* use the caller's object */ }
  return fallback;
}

export function isI18nReady(): boolean {
  return ready;
}

/** Collapse a nested catalog object into dotted keys. */
export function flattenCatalog(obj: unknown, prefix = ""): Record<string, string> {
  const out: Record<string, string> = {};
  if (!obj || typeof obj !== "object") return out;
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") {
      out[path] = value;
    } else if (value && typeof value === "object") {
      Object.assign(out, flattenCatalog(value, path));
    }
  }
  return out;
}

/** Install a nested catalog synchronously. Used by tests and any sync path. */
export function primeTranslations(catalog: unknown): void {
  strings = flattenCatalog(catalog);
  ready = true;
}

/**
 * Fetch this panel's strings for one language.
 *
 * Marks the catalog ready even on failure: a panel rendering raw keys is a
 * visibly broken deploy, which beats a spinner that never resolves.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export async function loadTranslations(hass: any, language: string): Promise<void> {
  // Before the fetch, and outside the try: the formatting locale is read off
  // hass and does not depend on the catalog arriving, so a failed fetch must
  // still leave dates in the language the panel is about to render in.
  setFormatLocale(hass, language);
  try {
    // This module must not import api.ts (see the file header), but it can use
    // HA's own authenticated fetch directly. HA alone owns token refresh.
    const url = `${CATALOG_URL}${encodeURIComponent(language || "en")}`;
    const requestHass = catalogHass(hass);
    // Never probe an admin endpoint anonymously while HA is replacing its root.
    if (typeof requestHass?.fetchWithAuth !== "function") return;
    const res = await requestHass.fetchWithAuth(url);
    const body = res.ok ? await res.json() : null;
    const next: Record<string, string> = {};
    for (const [key, value] of Object.entries(body?.resources ?? {})) {
      if (typeof value === "string") next[key] = value;
    }
    // A fetch that yields nothing keeps whatever is already loaded. Replacing a
    // working catalog with an empty one turns every label into a raw key, which
    // is worse than showing the previous language after a failed refetch.
    if (Object.keys(next).length > 0) {
      strings = next;
      loaded = language || "en";
    }
  } catch (e) {
    console.error("Phoenix MCP: could not load panel translations", e);
  } finally {
    ready = true;
  }
}

/**
 * Refetch only when the language that should be showing is not the one loaded.
 *
 * The stored preference is not the only way the answer changes: under "auto" it
 * follows the Home Assistant profile language, which an admin can change in
 * their HA profile without ever touching this panel and without a full page
 * load. Callers can run this on every hass update, since it is a comparison
 * until something actually differs.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export async function syncTranslations(hass: any): Promise<boolean> {
  const want = resolveLanguage(hass);
  if (want === loaded) {
    // Same language, but HA's own 24-hour or number-format preference may have
    // moved; adopting it is a repaint even though no catalog is refetched.
    return setFormatLocale(hass, want);
  }
  await loadTranslations(hass, want);
  return true;
}

/** True when the catalog has this key. */
export function hasMessage(key: string): boolean {
  return key in strings;
}

/** The stored template for a key, or the key itself when it is missing. */
export function rawMessage(key: string): string {
  return strings[key] ?? key;
}

export function interpolate(template: string, params?: Params): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in params ? String(params[name]) : match,
  );
}

export function t(key: string, params?: Params): string {
  return interpolate(rawMessage(key), params);
}

/**
 * Plural form. The catalog holds "<base>.one" and "<base>.other"; count is
 * always available to the template as {count}.
 */
export function tn(base: string, count: number, params?: Params): string {
  return t(`${base}.${count === 1 ? "one" : "other"}`, { count, ...params });
}
