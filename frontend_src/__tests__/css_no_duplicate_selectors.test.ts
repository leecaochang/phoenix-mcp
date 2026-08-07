import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";

// A class defined twice in one stylesheet does not fail: the later block simply
// wins, on whatever properties it happens to set, for every element wearing that
// name. It cost a day of "the titlebar icons look shifted" when a new provider
// card reused .agentcli-icon-btn, a name the chat window's titlebar had held all
// along. Nothing errored; the buttons just grew a border and a fixed box.
//
// A shared prefix is not a namespace. This fails on any class whose top-level
// rule block appears more than once, so the next collision is a red test rather
// than something someone notices in a screenshot.

const CSS = fs.readFileSync(
  path.join(__dirname, "..", "phoenix-mcp-panel.css"), "utf8");

/** Strip @media / @supports bodies: a class restated inside one is a responsive
 *  override, which is the normal way to write them and not a collision. */
function stripAtRuleBlocks(css: string): string {
  let out = "";
  for (let i = 0; i < css.length; i++) {
    if (css[i] !== "@") { out += css[i]; continue; }
    const open = css.indexOf("{", i);
    if (open === -1) break;
    let depth = 0, j = open;
    for (; j < css.length; j++) {
      if (css[j] === "{") depth++;
      else if (css[j] === "}" && --depth === 0) break;
    }
    i = j;
  }
  return out;
}

/** Class names that OPEN a top-level rule block, one entry per block. */
function definedClasses(css: string): string[] {
  const withoutComments = stripAtRuleBlocks(css.replace(/\/\*[\s\S]*?\*\//g, ""));
  const out: string[] = [];
  // Only selectors that are a bare class (optionally with pseudo/state/compound
  // parts) count as "defining" it; a descendant selector is scoping, not a
  // redefinition, and legitimately appears many times.
  for (const m of withoutComments.matchAll(/(^|\})\s*([^{}@]+)\{/g)) {
    // Only a rule whose ENTIRE selector is one bare class counts as defining it.
    // A comma-separated group (".a, .b { font-size }") is shared styling that
    // routinely sits beside a class's own rule, and counting it would report
    // every such pair as a collision.
    const bare = /^\.([a-zA-Z0-9_-]+)$/.exec(m[2].trim());
    if (bare) out.push(bare[1]);
  }
  return out;
}

describe("panel stylesheet", () => {
  it("anchors modal backdrops to the viewport", () => {
    expect(CSS).toMatch(/\.modal-backdrop\s*\{[^}]*position:\s*fixed;/s);
  });

  it("keeps short tab content painted through the viewport", () => {
    expect(CSS).toMatch(/:host\s*\{[^}]*min-height:\s*100vh;[^}]*min-height:\s*100dvh;/s);
    expect(CSS).toMatch(/\.phx-shell\s*\{[^}]*min-height:\s*100vh;[^}]*min-height:\s*100dvh;/s);
  });

  it("never defines the same class in two separate blocks", () => {
    const seen = new Map<string, number>();
    for (const cls of definedClasses(CSS)) seen.set(cls, (seen.get(cls) ?? 0) + 1);
    const duplicated = [...seen.entries()].filter(([, n]) => n > 1).map(([c]) => c).sort();
    expect(duplicated).toEqual([]);
  });

  it("the detector actually finds a duplicate", () => {
    // Asserting an absence above, so prove the walk is not blind.
    const dupes = definedClasses(".a { color: red } .b { color: blue } .a { color: green }");
    expect(dupes.filter((c) => c === "a")).toHaveLength(2);
  });

  it("a responsive override inside @media is not a redefinition", () => {
    const found = definedClasses(".a { x: 1 } @media (max-width: 700px) { .a { x: 2 } }");
    expect(found).toEqual(["a"]);
  });

  it("a descendant or compound selector is not a redefinition", () => {
    const found = definedClasses(".a .b { x: 1 } .a.c { y: 2 } .a { z: 3 }");
    expect(found).toEqual(["a"]);
  });

  it("a shared comma group beside a class's own rule is not a redefinition", () => {
    const found = definedClasses(".a, .b { font-size: 1px } .b { margin: 0 }");
    expect(found).toEqual(["b"]);
  });
});
