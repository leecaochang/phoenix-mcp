// The catalog is fetched at runtime, so a typo'd key is not a compile error.
// This test is what replaces that: it reads every t()/tn()/tRich() call in the
// source and checks the key against en.json.

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import en from "../../custom_components/phoenix_mcp/catalogs/en.json";
import { flattenCatalog, LANGUAGES } from "../i18n";

// jsdom does not give import.meta.url a file: scheme, and vitest runs from the
// repo root, so resolve the source tree from there.
const SRC = join(process.cwd(), "frontend_src");
const SKIP_DIRS = new Set(["__tests__", "i18n"]);

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      if (!SKIP_DIRS.has(name)) out.push(...sourceFiles(full));
    } else if (/\.tsx?$/.test(name)) {
      out.push(full);
    }
  }
  return out;
}

interface Use {
  key: string;
  file: string;
}

/**
 * Every literal key referenced in the source, expanded to the entries the
 * catalog must actually hold (a tn() call names a base; the catalog stores
 * .one and .other). A computed key cannot be checked here, which is why label
 * maps hold key strings rather than building them inline.
 */
function collectUses(): Use[] {
  const call = /\b(t|tn|tRich)\(\s*(["'])([\w.]+)\2/g;
  const uses: Use[] = [];
  for (const file of sourceFiles(SRC)) {
    const rel = relative(SRC, file);
    for (const [, fn, , key] of readFileSync(file, "utf8").matchAll(call)) {
      const keys = fn === "tn" ? [`${key}.one`, `${key}.other`] : [key];
      for (const k of keys) uses.push({ key: k, file: rel });
    }
  }
  return uses;
}

/**
 * Every dotted string literal in the source, whatever it is passed to. Label
 * maps hold keys as plain values (`{ approved: "approvals.filterApproved" }`)
 * and reach t() through a lookup, so the orphan check has to see those too.
 */
function collectLiterals(): Set<string> {
  const literal = /["']([\w]+(?:\.[\w]+)+)["']/g;
  const found = new Set<string>();
  for (const file of sourceFiles(SRC)) {
    for (const [, value] of readFileSync(file, "utf8").matchAll(literal)) {
      found.add(value);
    }
  }
  return found;
}

// Sections whose keys are assembled at runtime from a backend key (an
// approval's summary_key, its friendly title/body, or a chat event's code).
// No literal for each catalog entry exists here.
const BACKEND_NAMED = [
  "diff.", "approvalSummary.", "version.", "mesaSuggestion.", "agentchat.notice.",
  "agentchat.progress.", "adminError.",
  "settings.provider", "settings.agentcliApiKey", "settings.agentcliBaseUrl",
  "settings.agentcliServerUrl", "settings.agentcliZai",
];

const catalog = flattenCatalog(en.panel);
const uses = collectUses();
const literals = collectLiterals();

describe("i18n keys", () => {
  it("every key used in the source exists in en.json", () => {
    const missing = uses
      .filter((u) => !(u.key in catalog))
      .map((u) => `${u.file}: ${u.key}`);
    expect(missing).toEqual([]);
  });

  it("every catalog-shaped literal resolves, including indirect ones", () => {
    // collectUses only sees a direct t("key") call. Keys reached through a map
    // (`{ labelKey: "settings.someTypo" }` then `t(k.labelKey)`) were invisible:
    // a typo there renders the raw key at runtime and nothing reported it.
    //
    // A literal counts as catalog-shaped when its first segment is a real
    // top-level section, which distinguishes "settings.someTypo" from
    // "light.kitchen" or "homeassistant.restart".
    const sections = new Set(Object.keys(en.panel));
    const suspects = [...collectLiterals()].filter((v) => sections.has(v.split(".")[0]));
    // A tn() base is legitimate: the catalog stores <base>.one / <base>.other.
    const unresolved = suspects.filter((v) => !(v in catalog) && !(`${v}.one` in catalog));
    expect(unresolved).toEqual([]);
    // Not vacuous: indirect keys really are present in this codebase.
    expect(suspects.length).toBeGreaterThan(20);
  });

  it("every key in en.json is used somewhere", () => {
    // An orphan is usually a rename that left the old entry behind, or the
    // other half of a typo the previous check already flagged.
    //
    // diff.* and version.* are exempt: those keys are named by the BACKEND, which puts one on
    // each approval record as summary_key, and the panel looks it up at runtime.
    // No literal for them exists in this source tree. They have their own guard
    // in tests/test_diff_summary_contract.py, which pins them against
    // const.DIFF_SUMMARY_TEMPLATES in both directions.
    const used = new Set([...uses.map((u) => u.key), ...literals]);
    const orphans = Object.keys(catalog)
      .filter((k) => !BACKEND_NAMED.some((p) => k.startsWith(p)))
      .filter((k) => !used.has(k));
    expect(orphans).toEqual([]);
  });

  it("the backend-supplied sections are present and non-trivial", () => {
    // The exemption above would otherwise hide a section disappearing (a bad
    // gen_diff_catalog.py run, a bad merge) behind a passing orphan check.
    const count = (prefix: string) =>
      Object.keys(catalog).filter((k) => k.startsWith(prefix)).length;
    expect(count("diff.")).toBeGreaterThan(50);
    expect(count("version.")).toBeGreaterThan(10);
    expect(count("mesaSuggestion.")).toBeGreaterThan(10);
    expect(count("agentchat.progress.")).toBeGreaterThan(4);
    expect(count("agentchat.notice.")).toBeGreaterThan(3);
    expect(count("adminError.")).toBeGreaterThan(15);
  });

  it("the language picker offers exactly the locales that ship", () => {
    // LANGUAGES drives the Settings dropdown, so a locale added to translations/
    // but not here is invisible, and one listed here but not shipped silently
    // renders English. Neither shows up as an error at runtime.
    const shipped = readdirSync(
      join(process.cwd(), "custom_components", "phoenix_mcp", "translations"),
    )
      .filter((f) => f.endsWith(".json") && f !== "strings.json")
      .map((f) => f.replace(/\.json$/, ""))
      .sort();
    expect(LANGUAGES.map((l) => l.code).sort()).toEqual(shipped);
  });

  it("finds call sites once the catalog has entries", () => {
    // Guards the regex itself: if it silently stopped matching, both checks
    // above would pass vacuously.
    if (Object.keys(catalog).length > 0) expect(uses.length).toBeGreaterThan(0);
  });
});
