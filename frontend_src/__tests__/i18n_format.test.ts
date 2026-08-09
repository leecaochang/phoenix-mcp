// Dates, times and numbers must follow the panel's language, not the browser's.
//
// The failure this guards is silent in exactly the way the rest of the i18n
// work is: a bare toLocaleString() renders perfectly, just in the wrong
// convention, and only someone whose browser and panel languages differ ever
// sees it. So the scan below is the real guard, and the behaviour tests pin the
// pieces of HA's locale model that were mirrored rather than invented.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import {
  compareStrings,
  getFormatLocale,
  localeCompactNumber,
  localeClock,
  localeDate,
  localeDateTime,
  localeDateTimeShort,
  localeNumber,
  setFormatLocale,
  loadTranslations,
  syncTranslations,
} from "../i18n";

const SRC = join(process.cwd(), "frontend_src");
// The one module allowed to call Intl directly; it is what everything else routes through.
const FORMAT_MODULE = join("i18n", "format.ts");

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      if (name !== "__tests__") out.push(...sourceFiles(full));
    } else if (/\.tsx?$/.test(name)) {
      out.push(full);
    }
  }
  return out;
}

// Every way of reaching the platform's locale machinery directly. All three
// silently use the BROWSER's language: toLocale* for rendering, localeCompare
// for sort order, and a bare Intl constructor for both.
const RAW_LOCALE_USES = [
  /\.toLocale(?:String|DateString|TimeString)\s*\(/g,
  /\.localeCompare\s*\(/g,
  /new\s+Intl\./g,
];

describe("no raw locale formatting outside i18n/format.ts", () => {
  it("finds no direct locale API use in any other source file", () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(SRC)) {
      const rel = relative(SRC, file);
      if (rel === FORMAT_MODULE) continue;
      const text = readFileSync(file, "utf-8");
      for (const pattern of RAW_LOCALE_USES) {
        for (const match of text.matchAll(pattern)) {
          const line = text.slice(0, match.index).split("\n").length;
          offenders.push(`${rel}:${line} (${match[0].trim()})`);
        }
      }
    }
    expect(
      offenders,
      `Use the helpers from i18n/format.ts instead; these follow the browser's language, not the panel's.\n${offenders.join("\n")}`,
    ).toEqual([]);
  });
});

describe("setFormatLocale", () => {
  beforeEach(() => setFormatLocale(null, undefined));

  it("prefers the panel's resolved language over HA's profile language", () => {
    // An admin who overrode the panel to Chinese is asking for a Chinese panel;
    // a date in the HA profile's convention would read as a bug.
    setFormatLocale({ language: "en", locale: { language: "en" } }, "zh-Hans");
    expect(getFormatLocale()).toBe("zh-Hans");
  });

  it("falls back to hass.locale.language, then hass.language", () => {
    setFormatLocale({ language: "en", locale: { language: "de" } });
    expect(getFormatLocale()).toBe("de");
    setFormatLocale({ language: "fr" });
    expect(getFormatLocale()).toBe("fr");
  });

  it("leaves the locale undefined when hass carries nothing", () => {
    // Undefined means the browser default, which is today's behaviour: the
    // pre-load state must not invent a language.
    setFormatLocale(null);
    expect(getFormatLocale()).toBeUndefined();
  });

  it("reports whether anything actually changed", () => {
    expect(setFormatLocale({ locale: { language: "en" } })).toBe(true);
    expect(setFormatLocale({ locale: { language: "en" } })).toBe(false);
    expect(setFormatLocale({ locale: { language: "en", time_format: "24" } })).toBe(true);
  });
});

describe("date and time formatting", () => {
  const ISO = "2026-03-04T22:05:00Z";

  beforeEach(() => setFormatLocale(null, undefined));

  it("formats in the resolved language", () => {
    setFormatLocale({}, "en-US");
    const en = localeDate(ISO);
    setFormatLocale({}, "de-DE");
    expect(localeDate(ISO)).not.toBe(en);
  });

  it("honors HA's 24-hour preference over the language default", () => {
    setFormatLocale({ locale: { language: "en-US", time_format: "24" } });
    expect(localeDateTime(ISO)).not.toMatch(/[AP]M/);
    setFormatLocale({ locale: { language: "en-US", time_format: "12" } });
    expect(localeDateTime(ISO)).toMatch(/[AP]M/);
  });

  it("applies the preference to the short and clock forms too", () => {
    setFormatLocale({ locale: { language: "en-US", time_format: "24" } });
    expect(localeDateTimeShort(ISO)).not.toMatch(/[AP]M/);
    expect(localeClock(Date.parse(ISO))).not.toMatch(/[AP]M/);
    setFormatLocale({ locale: { language: "en-US", time_format: "12" } });
    expect(localeClock(Date.parse(ISO))).toMatch(/[AP]M/);
  });

  it("lets the language decide when no time_format preference is set", () => {
    // Absent means no preference, NOT a guess: en-US is 12-hour, de is 24-hour,
    // and each must come out its own way.
    setFormatLocale({ locale: { language: "en-US" } });
    expect(localeDateTime(ISO)).toMatch(/[AP]M/);
    setFormatLocale({ locale: { language: "de-DE" } });
    expect(localeDateTime(ISO)).not.toMatch(/[AP]M/);
  });
});

describe("number formatting", () => {
  beforeEach(() => setFormatLocale(null, undefined));

  it("maps HA's number_format choices the way HA does", () => {
    setFormatLocale({ locale: { language: "en", number_format: "comma_decimal" } });
    expect(localeNumber(1234567)).toBe("1,234,567");
    setFormatLocale({ locale: { language: "en", number_format: "decimal_comma" } });
    expect(localeNumber(1234567)).toBe("1.234.567");
    setFormatLocale({ locale: { language: "en", number_format: "quote_decimal" } });
    // CLDR has shipped both the straight and the typographic apostrophe as
    // de-CH's group separator, so pin the grouping rather than the glyph.
    expect(localeNumber(1234567)).toMatch(/^1['’]234['’]567$/);
  });

  it("drops grouping entirely for number_format none", () => {
    setFormatLocale({ locale: { language: "de", number_format: "none" } });
    expect(localeNumber(1234567)).toBe("1234567");
  });

  it("otherwise follows the display language", () => {
    setFormatLocale({ locale: { number_format: "language" } }, "de");
    expect(localeNumber(1234567)).toBe("1.234.567");
  });
});

describe("compact numbers", () => {
  beforeEach(() => setFormatLocale(null, undefined));

  it("uses the locale's own short form", () => {
    setFormatLocale({}, "en-US");
    expect(localeCompactNumber(812)).toBe("812");
    expect(localeCompactNumber(48_200)).toBe("48.2K");
    expect(localeCompactNumber(1_234_000)).toBe("1.2M");
  });

  it("groups by the locale's own magnitude, not a translated English one", () => {
    // The whole reason this cannot be a hand-rolled ladder with a translated
    // suffix: Chinese groups by 10,000, so 48,200 is 4.8万 and no wording
    // change to a "k" suffix would ever produce that number.
    setFormatLocale({}, "zh-Hans");
    expect(localeCompactNumber(48_200)).toBe("4.8万");
  });

  it("collapses zero and non-finite input", () => {
    setFormatLocale({}, "en-US");
    expect(localeCompactNumber(0)).toBe("0");
    expect(localeCompactNumber(-5)).toBe("0");
    expect(localeCompactNumber(NaN)).toBe("0");
  });
});

describe("string collation", () => {
  beforeEach(() => setFormatLocale(null, undefined));

  it("sorts by the panel's language, not the browser's", () => {
    // Swedish orders a-ring after z; German orders it with a. Same input, two
    // correct answers, and a bare localeCompare picks whichever the browser is.
    const words = ["zebra", "ärlig", "apple"];
    setFormatLocale({}, "sv");
    const swedish = [...words].sort(compareStrings);
    setFormatLocale({}, "de");
    const german = [...words].sort(compareStrings);
    expect(swedish).not.toEqual(german);
    expect(swedish[swedish.length - 1]).toBe("ärlig");
    expect(german[german.length - 1]).toBe("zebra");
  });

  it("rebuilds the collator when the locale changes", () => {
    // The collator is cached, so a stale one would keep the previous language's
    // order for the rest of the session.
    setFormatLocale({}, "sv");
    expect(["zebra", "ärlig"].sort(compareStrings)[0]).toBe("zebra");
    setFormatLocale({}, "de");
    expect(["zebra", "ärlig"].sort(compareStrings)[0]).toBe("ärlig");
  });
});

describe("adoption through the translation lifecycle", () => {
  beforeEach(() => setFormatLocale(null, undefined));

  it("adopts the locale even when the catalog fetch fails", () => {
    // The formatting locale is read off hass, not off the catalog, so a failed
    // request must not leave every timestamp in the browser's convention.
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("nope")));
    const hass = {
      locale: { language: "zh-Hans" },
      auth: { data: { access_token: "tok-test" } },
      fetchWithAuth: (path: string, init?: RequestInit) => fetch(path, init),
    };
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    return loadTranslations(hass, "zh-Hans").then(() => {
      expect(getFormatLocale()).toBe("zh-Hans");
      spy.mockRestore();
      vi.unstubAllGlobals();
    });
  });

  it("repaints when only a format preference moved", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ resources: { a: "A" } }),
    }));
    const hass = (timeFormat: string) => ({
      language: "en",
      locale: { language: "en", time_format: timeFormat },
      auth: { data: { access_token: "tok-test" } },
      fetchWithAuth: (path: string, init?: RequestInit) => fetch(path, init),
    });
    await loadTranslations(hass("24"), "en");
    // Same language, so no refetch happens; the switch to 12-hour still has to
    // be reported as a change or every visible timestamp stays stale.
    expect(await syncTranslations(hass("12"))).toBe(true);
    expect(await syncTranslations(hass("12"))).toBe(false);
    vi.unstubAllGlobals();
  });
});
