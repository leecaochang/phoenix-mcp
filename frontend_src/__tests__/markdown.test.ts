import { describe, it, expect } from "vitest";
import { renderMarkdown, stripControlChars, flagsUnsafeContent } from "../utils/markdown";

const NUL = String.fromCharCode(0);

describe("output sanitization", () => {
  it("strips non-text control characters but keeps tab/newline", () => {
    expect(stripControlChars("a" + NUL + "bc")).toBe("abc");
    expect(stripControlChars("line1\nline2\tend")).toBe("line1\nline2\tend");
  });

  it("flags active-content markup and control chars", () => {
    expect(flagsUnsafeContent("<script>alert(1)</script>")).toBe(true);
    expect(flagsUnsafeContent("<img src=x onerror=go()>")).toBe(true);
    expect(flagsUnsafeContent("click javascript:evil()")).toBe(true);
    expect(flagsUnsafeContent("has a " + NUL + " null")).toBe(true);
    expect(flagsUnsafeContent("just normal text about lights")).toBe(false);
  });

  it("renderMarkdown strips control chars and escapes markup", () => {
    const html = renderMarkdown("safe " + NUL + " <script>bad()</script>");
    expect(html).not.toContain(NUL);
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });
});

describe("renderMarkdown", () => {
  it("renders common inline and block markdown", () => {
    expect(renderMarkdown("**bold** and *italic*")).toContain("<strong>bold</strong>");
    expect(renderMarkdown("**bold** and *italic*")).toContain("<em>italic</em>");
    expect(renderMarkdown("use `get_state`")).toContain("<code>get_state</code>");
    expect(renderMarkdown("# Heading")).toContain("<h1>Heading</h1>");
    const list = renderMarkdown("- one\n- two");
    expect(list).toContain("<ul>");
    expect(list).toContain("<li>one</li>");
    expect(list).toContain("<li>two</li>");
    const ol = renderMarkdown("1. first\n2. second");
    expect(ol).toContain("<ol>");
    expect(ol).toContain("<li>first</li>");
  });

  it("renders fenced code blocks with escaped contents", () => {
    const html = renderMarkdown("```\nlight.turn_on <x>\n```");
    expect(html).toContain("<pre><code>");
    expect(html).toContain("light.turn_on &lt;x&gt;");
  });

  it("allows only http(s)/relative links and renders others as text", () => {
    expect(renderMarkdown("[docs](https://example.com)"))
      .toContain('<a href="https://example.com" target="_blank" rel="noopener noreferrer">docs</a>');
    expect(renderMarkdown("[x](javascript:alert(1))")).not.toContain("<a ");
    expect(renderMarkdown("[x](javascript:alert(1))")).toContain("x");
    // A root-relative path is fine, but a scheme-relative "//host" resolves to
    // an external https:// host in the browser and must NOT be linkified.
    expect(renderMarkdown("[ok](/config/dashboard)")).toContain("<a ");
    expect(renderMarkdown("[evil](//evil.example)")).not.toContain("<a ");
    expect(renderMarkdown("[evil](//evil.example)")).toContain("evil");
  });

  it("escapes raw HTML so model output cannot inject markup", () => {
    const html = renderMarkdown("<img src=x onerror=alert(1)> and <script>bad()</script>");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;img");
    expect(html).toContain("&lt;script&gt;");
  });

  it("strips quotes from link hrefs to prevent attribute breakout", () => {
    const html = renderMarkdown('[x](/a"onmouseover="alert(1))');
    expect(html).not.toContain('onmouseover="alert');
  });

  it("renders a GFM table into a real table (not one run-together paragraph)", () => {
    const src = [
      "| Entity ID | State |",
      "|-----------|-------|",
      "| light.a | on |",
      "| light.b | off |",
    ].join("\n");
    const html = renderMarkdown(src);
    expect(html).toContain("<table>");
    expect(html).toContain("<th>Entity ID</th>");
    expect(html).toContain("<th>State</th>");
    expect(html).toContain("<td>light.a</td>");
    expect(html).toContain("<td>off</td>");
    // The rows must not be collapsed into a single <p> with pipes.
    expect(html).not.toContain("<p>| light.a");
    // Cell content still gets inline formatting and escaping.
    expect(renderMarkdown("| A |\n|---|\n| `code` |")).toContain("<code>code</code>");
  });

  it("still renders intended emphasis", () => {
    expect(renderMarkdown("this is **bold** here")).toContain("<strong>bold</strong>");
    expect(renderMarkdown("this is _italic_ here")).toContain("<em>italic</em>");
    expect(renderMarkdown("this is *italic* here")).toContain("<em>italic</em>");
    expect(renderMarkdown("(__strong__)")).toContain("<strong>strong</strong>");
  });

  it("does NOT emphasize literal underscores/asterisks in ordinary text", () => {
    // Entity ids and identifiers with intra-word underscores must stay literal.
    const a = renderMarkdown("turn on sensor.living_room_temp and light.turn_on");
    expect(a).not.toContain("<em>");
    expect(a).toContain("living_room_temp");
    expect(a).toContain("light.turn_on");
    // Arithmetic-style asterisks/underscores must not become emphasis.
    expect(renderMarkdown("a * b * c")).not.toContain("<em>");
    expect(renderMarkdown("2*3*4 = 24")).not.toContain("<em>");
    // A word char right after the closing marker means it is not emphasis.
    expect(renderMarkdown("m**a**n")).not.toContain("<strong>");
  });

  it("keeps a lone dashed line as an <hr>, not a table separator", () => {
    const html = renderMarkdown("above\n\n---\n\nbelow");
    expect(html).toContain("<hr />");
    expect(html).not.toContain("<table>");
  });
});
