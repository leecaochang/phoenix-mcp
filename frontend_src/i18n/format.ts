// Locale-aware date, time and number formatting.
//
// Every toLocale* call in the panel and the two injected bundles goes through
// here. A bare toLocaleString() follows the BROWSER's language, which is not
// the language the panel is showing: the panel resolves its own (the phx-lang
// override, else Home Assistant's profile language), so a panel switched to
// Chinese still printed its timestamps in the browser's convention.
//
// Grounded in HA's own frontend rather than invented. The FrontendLocaleData
// shape mirrors src/data/translation.ts, the NumberFormat table mirrors
// numberFormatToLocale in src/common/number/format_number.ts (including
// quote_decimal, the Swiss 1'234.56 style, and none, which is en-US with
// grouping switched off), and the 12-hour probe mirrors useAmPm in
// src/common/datetime/use_am_pm.ts, 22:00 rendering as "10" being what tells a
// 12-hour locale from a 24-hour one.
//
// DELIBERATELY not honored, because each would mean hand-building Intl option
// sets rather than reading a preference, and no panel surface needs one:
// date_format's DMY/MDY/YMD overrides (dates here sit in table cells where the
// language default reads fine), time_zone's "server" option (every timestamp
// the panel shows is absolute and rendered in local time, HA's own default),
// and first_weekday (there is no date picker).
//
// Must never import from api.ts, nor from ./index.ts: index.ts calls
// setFormatLocale from loadTranslations so no caller can forget it, which only
// stays acyclic while this module imports nothing of its own.

/** The subset of HA's FrontendLocaleData this module reads. All optional: an
 *  older HA, or a test double, may carry no locale object at all. */
export interface HassLocale {
  language?: string;
  number_format?: string;
  time_format?: string;
}

interface HassLike {
  language?: string;
  locale?: HassLocale;
}

// Undefined until setFormatLocale runs, which means the browser default: the
// pre-load state is exactly today's behaviour rather than a guess.
let dateLocale: string | undefined;
let numberLocale: string | string[] | undefined;
let numberGrouping = true;
let hour12: boolean | undefined;

// Built lazily and dropped whenever the locale moves. Constructing an Intl
// object is the expensive part, and the collator is used inside sort
// comparators where it would otherwise be rebuilt once per comparison.
let compactFormatter: Intl.NumberFormat | null = null;
let collator: Intl.Collator | null = null;

/** HA's numberFormatToLocale. The default branch is the display language. */
function resolveNumberLocale(format: string | undefined, language: string | undefined): string | string[] | undefined {
  switch (format) {
    case "comma_decimal": return ["en-US", "en"];
    case "decimal_comma": return ["de", "es", "it"];
    case "space_comma": return ["fr", "sv", "cs"];
    case "quote_decimal": return ["de-CH"];
    case "none": return "en-US";
    case "system": return undefined;
    default: return language;
  }
}

/**
 * HA's useAmPm. An absent time_format returns undefined rather than guessing,
 * so Intl picks the convention from the locale on its own.
 */
function resolveHour12(format: string | undefined, language: string | undefined): boolean | undefined {
  if (format === "12") return true;
  if (format === "24") return false;
  if (format !== "language" && format !== "system") return undefined;
  const probeLanguage = format === "language" ? language : undefined;
  try {
    return new Date("January 1, 2023 22:00:00").toLocaleString(probeLanguage).includes("10");
  } catch {
    return undefined;
  }
}

/**
 * Adopt a language and Home Assistant's formatting preferences.
 *
 * `language` is the panel's RESOLVED display language, which outranks
 * hass.locale.language: an admin who overrides the panel to Chinese while
 * leaving their HA profile in English is asking this panel to be Chinese, and a
 * date in the other language's convention would read as a bug.
 */
export function setFormatLocale(hass: unknown, language?: string): boolean {
  const h = (hass ?? {}) as HassLike;
  const locale = h.locale ?? {};
  const before = signature();
  dateLocale = language || locale.language || h.language || undefined;
  numberLocale = resolveNumberLocale(locale.number_format, dateLocale);
  numberGrouping = locale.number_format !== "none";
  hour12 = resolveHour12(locale.time_format, dateLocale);
  compactFormatter = null;
  collator = null;
  return signature() !== before;
}

// Returned so syncTranslations can repaint when only a FORMAT preference moved.
// An admin who switches HA to 24-hour time never changes the language, so
// keying the repaint on the language alone would leave every timestamp on the
// page in the old convention until something else happened to re-render.
function signature(): string {
  return JSON.stringify([dateLocale, numberLocale, numberGrouping, hour12]);
}

/** The resolved date locale, or undefined for the browser default. */
export function getFormatLocale(): string | undefined {
  return dateLocale;
}

// Spread rather than passed as `hour12: undefined`, which Intl reads as an
// explicit request rather than as "no preference".
function withHour12(options: Intl.DateTimeFormatOptions): Intl.DateTimeFormatOptions {
  return hour12 === undefined ? options : { ...options, hour12 };
}

/** Date only. */
export function localeDate(iso: string | number | Date): string {
  return new Date(iso).toLocaleDateString(dateLocale);
}

/** Date and time, the full default form. */
export function localeDateTime(iso: string | number | Date): string {
  const options = withHour12({});
  return new Date(iso).toLocaleString(dateLocale, Object.keys(options).length ? options : undefined);
}

/** Month, day and time: the compact form the narrow audit table uses. */
export function localeDateTimeShort(iso: string | number | Date): string {
  return new Date(iso).toLocaleString(dateLocale, withHour12({
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  }));
}

/** Wall clock, hours and minutes only. */
export function localeClock(ts: string | number | Date): string {
  return new Date(ts).toLocaleTimeString(dateLocale, withHour12({
    hour: "2-digit", minute: "2-digit",
  }));
}

/** A plain number, grouped per the operator's HA number-format preference. */
export function localeNumber(value: number): string {
  return value.toLocaleString(numberLocale, numberGrouping ? undefined : { useGrouping: false });
}

/**
 * A large count in the locale's own short form: 48.2K, 4.8万, 1,2 Mio.
 *
 * Hand-rolled "48.2k" was wrong outside English in a way that is easy to miss,
 * because it is not a translation problem: Chinese groups by 10^4 (万), so no
 * amount of translating the suffix produces the right NUMBER. CLDR knows the
 * grouping as well as the suffix, which is why this delegates rather than
 * appending a localized unit to an English-scaled figure. Note some locales
 * legitimately decline to abbreviate thousands at all (German renders 48.200),
 * so this can be wider than the English form.
 *
 * Zero and non-finite collapse to "0" so a missing usage counter reads as
 * nothing rather than as NaN.
 */
export function localeCompactNumber(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (compactFormatter === null) {
    compactFormatter = new Intl.NumberFormat(numberLocale, {
      notation: "compact",
      maximumFractionDigits: 1,
      useGrouping: numberGrouping,
    });
  }
  return compactFormatter.format(value);
}

/**
 * Compare two display strings for sorting, in the panel's language.
 *
 * A bare a.localeCompare(b) sorts by the BROWSER's language, so a panel in one
 * language ordered its lists by another's rules; the difference is invisible in
 * English and immediate in any language whose alphabet or accent handling
 * differs. Collation options are deliberately left at their defaults: switching
 * on numeric collation would reorder existing lists ("Light 10" before
 * "Light 2"), which is a separate decision from localizing them.
 */
export function compareStrings(a: string, b: string): number {
  if (collator === null) collator = new Intl.Collator(dateLocale);
  return collator.compare(a, b);
}
