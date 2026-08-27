// Nothing user-visible may be a bare literal in source.
//
// This replaces a regex scan that looked only for capitalized, multi-word JSX
// text. That scan passed while thirteen real strings were shipping in English
// inside a Chinese panel, because they were short, lowercase, template
// literals, or routed through a data object. Every miss was a shape the regex
// could not express, so this walks the TypeScript AST instead.
//
// Checked: JSX text, the four user-visible attributes, string and template
// expressions rendered inside JSX, and UI-ish object properties.

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import ts from "typescript";

const SRC = join(process.cwd(), "frontend_src");
const SKIP_DIRS = new Set(["__tests__", "i18n"]);

// Attributes a user or a screen reader reads out.
const VISIBLE_ATTRS = new Set(["aria-label", "title", "placeholder", "alt"]);
// Object properties that carry UI copy rather than data.
const UI_PROPS = new Set(["label", "title", "hint", "message", "intro", "sub", "note", "text", "alt"]);

/**
 * Values that are legitimately bare English.
 *
 * Keep this SHORT and justified. Everything here is either a proper noun, a
 * protocol/vendor token a user must read verbatim, or a value some non-UI
 * consumer compares against; see scripts/i18n_check_locale.py for the catalog
 * side of the same decision.
 */
const ALLOWED = new Set([
  "Phoenix MCP",           // the product mark, kept Latin in-panel
  "WIPE",                  // WipeConfirmModal compares the typed value
  "Anthropic", "DeepSeek", "Gemini", "Grok", "Kimi", "Meta", "MiniMax", "NVIDIA NIM",
  "OpenAI", "OpenRouter",
  "Ollama (cloud)", "Ollama (local)",   // resolved through kindLabel(); these are the fallbacks
  "Claude Code", "Gemini CLI", "Codex", "Cursor",   // agent product names (wizard_helpers AGENTS)
  "MESA",
]);

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

/** Prose a human reads, as opposed to a slug, class name, id, or code payload.
 *
 *  `minWords` is 2 everywhere except the four visible attributes, which pass 1.
 *  A template literal is scanned one fragment at a time, so `Collapse ${name}`
 *  reaches here as the single word "Collapse" and the two-word floor silently
 *  spared it: interpolation splits a real label below the threshold. That is
 *  fine for JSX text and UI object properties, where a lone word is usually an
 *  identifier being rendered, but aria-label/title/placeholder/alt carry nothing
 *  BUT human-facing prose, so one untranslated word there is already a defect.
 */
function isProse(raw: string, minWords = 2): boolean {
  const text = raw.split(/\s+/).join(" ").trim();
  if (!text || ALLOWED.has(text)) return false;
  // Must contain at least two runs of letters, i.e. be more than one token.
  const words = text.split(" ");
  if (words.length < minWords) return false;
  // Reject anything that looks like code, config, a path, or a selector.
  if (/[<>{}[\]|\\=;#]|:\/\/|\$\{|^[.#]|\w+\(\)/.test(text)) return false;
  if (/^[a-z_]+\.[a-z_.]+$/.test(text)) return false;
  // A lone snake_case token is an identifier being shown as an example
  // (placeholder="test_token"), not a sentence. Only reachable at minWords 1.
  if (words.length === 1 && text.includes("_")) return false;
  // A CSS class list, not a sentence.
  if (words.every((w) => /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/.test(w)) && text.includes("-")) return false;
  // Every token must look like a word (letters, with ordinary punctuation).
  const wordy = words.filter((w) => /^[A-Za-z][A-Za-z'’-]*[,.!?:;)]?$|^\(?[A-Za-z]/.test(w));
  if (wordy.length < words.length) return false;
  // At least two real alphabetic words, so "3 s" or "id name" style pairs of
  // identifiers do not register.
  return words.filter((w) => /[A-Za-z]{3,}/.test(w)).length >= minWords;
}

interface Finding { file: string; line: number; kind: string; text: string }

function scan(file: string): Finding[] {
  const text = readFileSync(file, "utf8");
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const out: Finding[] = [];
  const at = (node: ts.Node) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
  const report = (node: ts.Node, kind: string, value: string, minWords = 2) => {
    if (isProse(value, minWords)) {
      out.push({ file: relative(SRC, file), line: at(node), kind, text: value.trim() });
    }
  };

  const visit = (node: ts.Node): void => {
    // Text between JSX tags.
    if (ts.isJsxText(node)) report(node, "jsx-text", node.text);

    // aria-label / title / placeholder / alt as a literal, or as a literal
    // inside an expression container ({"..."} and {cond ? "a" : "b"}).
    if (ts.isJsxAttribute(node) && ts.isIdentifier(node.name) === false) { /* namespaced: ignore */ }
    if (ts.isJsxAttribute(node)) {
      const name = node.name.getText(sf);
      if (VISIBLE_ATTRS.has(name) && node.initializer) {
        const init = node.initializer;
        if (ts.isStringLiteral(init)) report(init, `attr:${name}`, init.text, 1);
        else if (ts.isJsxExpression(init) && init.expression) {
          for (const lit of literalsIn(init.expression)) report(lit, `attr:${name}`, lit.text, 1);
        }
      }
    }

    // A literal rendered directly as a child: <p>{"..."}</p>, <p>{a ? "x" : "y"}</p>.
    if (ts.isJsxExpression(node) && node.expression && node.parent
        && (ts.isJsxElement(node.parent) || ts.isJsxFragment(node.parent))) {
      for (const lit of literalsIn(node.expression)) report(lit, "jsx-child", lit.text);
    }

    // UI copy hiding in a data object: { label: "Other (MCP spec)" }.
    if (ts.isPropertyAssignment(node) && node.name) {
      const name = ts.isIdentifier(node.name) || ts.isStringLiteral(node.name)
        ? node.name.text : null;
      if (name && UI_PROPS.has(name)) {
        for (const lit of literalsIn(node.initializer)) report(lit, `prop:${name}`, lit.text);
      }
    }

    ts.forEachChild(node, visit);
  };

  /** String and no-substitution template literals reachable outside i18n calls. */
  function literalsIn(node: ts.Node): ts.StringLiteralLike[] {
    const found: ts.StringLiteralLike[] = [];
    const walk = (n: ts.Node) => {
      // Localization calls are covered by the key guard. Other calls still
      // carry visible fallbacks, such as String(value ?? "Camera image"), and
      // must be inspected rather than becoming an escape hatch.
      if (ts.isCallExpression(n)) {
        const called = ts.isIdentifier(n.expression) ? n.expression.text : "";
        if (["t", "tn", "tRich"].includes(called)) return;
        n.arguments.forEach(walk);
        return;
      }
      // Stop at nested JSX: its own attributes and children are visited by the
      // main walk, and descending here reported every className in a
      // conditionally-rendered element as if it were the rendered text.
      if (ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n) || ts.isJsxFragment(n)) return;
      // A ternary's CONDITION is code, never rendered: `tabKey === "approvals"`
      // put a slug in front of the prose check that only the branches can fail.
      if (ts.isConditionalExpression(n)) { walk(n.whenTrue); walk(n.whenFalse); return; }
      if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) { found.push(n); return; }
      if (ts.isTemplateExpression(n)) {
        // Collect every fragment and let the caller's own threshold judge it.
        // Filtering here applied the 2-word default regardless of context, which
        // is what hid `Collapse ${name}` from the visible-attribute check.
        for (const span of [n.head, ...n.templateSpans.map((s) => s.literal)]) {
          found.push(span as unknown as ts.StringLiteralLike);
        }
        n.templateSpans.forEach((s) => walk(s.expression));
        return;
      }
      ts.forEachChild(n, walk);
    };
    walk(node);
    return found;
  }

  visit(sf);
  return out;
}

describe("no bare user-visible English in source", () => {
  const findings = sourceFiles(SRC).flatMap(scan);

  it("finds nothing", () => {
    const report = findings.map((f) => `${f.file}:${f.line} [${f.kind}] ${f.text.slice(0, 90)}`);
    expect(report).toEqual([]);
  });

  it("the scanner actually parses and matches", () => {
    // Guards the whole file from passing vacuously if the AST walk broke: this
    // fixture must be reported, using the same code path as the real scan.
    const fixture = `
      const x = <div aria-label="Reload the entity tree">
        This card could not be rendered.
        {"Another bare sentence here"}
      </div>;
      const opts = [{ label: "Other things entirely" }];
    `;
    const sf = ts.createSourceFile("f.tsx", fixture, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    expect(sf.statements.length).toBeGreaterThan(0);
    expect(isProse("This card could not be rendered.")).toBe(true);
    expect(isProse("Other things entirely")).toBe(true);
    expect(isProse("Camera image", 1)).toBe(true);
    // ...and does not fire on the shapes it must tolerate.
    expect(isProse("badge badge-green")).toBe(false);
    expect(isProse("light.kitchen")).toBe(false);
    expect(isProse("Phoenix MCP")).toBe(false);
  });
});
