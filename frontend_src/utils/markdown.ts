// Minimal, dependency-free Markdown -> HTML renderer for agentCLI assistant
// output. Safety model: every piece of source text is HTML-escaped BEFORE any
// transform runs, and the transforms only ever insert a fixed whitelist of tags
// (never raw model text into an attribute or tag position), so model output
// cannot inject markup. Non-text control characters are stripped, link hrefs are
// restricted to http(s)/relative and have quotes stripped. This is intentionally
// small: it covers the common Markdown an assistant emits (headings, emphasis,
// code, lists, links, blockquotes) and renders anything it does not recognise as
// plain text.

// Non-text control characters (keep tab \x09, newline \x0A, carriage return
// \x0D). Built via RegExp() with escaped strings so no literal control bytes
// live in this source file.
const CONTROL_CLASS = "\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F\\x7F";
// eslint-disable-next-line no-control-regex
const CONTROL_RE = new RegExp("[" + CONTROL_CLASS + "]", "g");

export function stripControlChars(s: string): string {
  // Model output should be text; NUL and other C0 controls have no legitimate
  // place and can corrupt rendering, so they are removed before display.
  return s.replace(CONTROL_RE, "");
}

// True if the RAW model output contains active-content markup (script/iframe/
// event handlers/javascript: URLs) or control characters. renderMarkdown always
// neutralizes such content by escaping it to plain text; this flag lets the UI
// tell the user that neutralization happened, so a reply full of literal tags is
// not mistaken for a bug.
// eslint-disable-next-line no-control-regex
const UNSAFE_RE = new RegExp(
  "<\\s*(?:script|iframe|object|embed|link|style|meta|svg|math|base)\\b" +
    "|\\son[a-z]+\\s*=|javascript:|data:text/html|[" + CONTROL_CLASS + "]",
  "i",
);

export function flagsUnsafeContent(raw: string): boolean {
  return UNSAFE_RE.test(raw);
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Inline transforms. Input MUST already be HTML-escaped.
//
// Emphasis is boundary-aware so literal underscores/asterisks in ordinary output
// (entity ids like sensor.living_room_temp, or "a * b") are NOT mistaken for
// markdown. A marker only opens/closes emphasis at a word boundary: the run must
// be preceded by start-of-string or a non-word, non-marker character, its content
// must not begin or end with whitespace, and it must be followed by a non-word
// character or end-of-string. This mirrors the GFM rule that emphasis markers
// flanked by alphanumerics on the inside edge do not delimit, so intra-word
// underscores (the common case that used to garble output) stay literal.
function inline(s: string): string {
  s = s.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`);
  s = s.replace(/(^|[^\w*])\*\*(\S(?:[^*]*?\S)?)\*\*(?!\w)/g, "$1<strong>$2</strong>");
  s = s.replace(/(^|[^\w_])__(\S(?:[^_]*?\S)?)__(?!\w)/g, "$1<strong>$2</strong>");
  s = s.replace(/(^|[^\w*])\*(\S(?:[^*]*?\S)?)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^\w_])_(\S(?:[^_]*?\S)?)_(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, text: string, url: string) => {
    // Absolute http(s), or a root-relative path that is NOT scheme-relative:
    // "//evil.example" starts with "/" but browsers resolve it as an external
    // https:// host, so a bare "/" allow-rule would let it through. Require a
    // single leading slash not followed by another.
    if (/^(https?:\/\/|\/(?!\/))/i.test(url)) {
      const safe = url.replace(/"/g, "%22");
      return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${text}</a>`;
    }
    return text;
  });
  return s;
}

// GFM table detection. A separator row is dashes (optionally colon-aligned)
// between pipes; it must contain a pipe so a lone "---" stays an <hr>.
const TABLE_SEP_RE = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;
function isTableSep(line: string): boolean {
  return line.includes("|") && TABLE_SEP_RE.test(line);
}
function splitTableRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}
function renderTable(header: string[], rows: string[][]): string {
  const th = header.map((h) => `<th>${inline(esc(h))}</th>`).join("");
  const body = rows.map((r) => {
    const cells: string[] = [];
    for (let i = 0; i < header.length; i++) cells.push(`<td>${inline(esc(r[i] ?? ""))}</td>`);
    return `<tr>${cells.join("")}</tr>`;
  }).join("");
  return `<table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>`;
}

export function renderMarkdown(src: string): string {
  if (!src) return "";
  const lines = stripControlChars(src).replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let inCode = false;
  let codeBuf: string[] = [];
  let listType: "ul" | "ol" | null = null;
  let para: string[] = [];

  const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };
  const flushPara = () => {
    if (para.length) { out.push(`<p>${inline(esc(para.join(" ")))}</p>`); para = []; }
  };

  for (let li = 0; li < lines.length; li++) {
    const line = lines[li];
    const fence = line.match(/^```/);
    if (fence) {
      if (!inCode) { flushPara(); closeList(); inCode = true; codeBuf = []; }
      else { out.push(`<pre><code>${esc(codeBuf.join("\n"))}</code></pre>`); inCode = false; }
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }

    // Table: a header row with pipes immediately followed by a separator row.
    if (line.includes("|") && li + 1 < lines.length && isTableSep(lines[li + 1])) {
      flushPara(); closeList();
      const header = splitTableRow(line);
      const rows: string[][] = [];
      let j = li + 2;
      for (; j < lines.length; j++) {
        const r = lines[j];
        if (/^\s*$/.test(r) || !r.includes("|")) break;
        rows.push(splitTableRow(r));
      }
      out.push(renderTable(header, rows));
      li = j - 1;
      continue;
    }

    if (/^\s*$/.test(line)) { flushPara(); closeList(); continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushPara(); closeList(); const lvl = h[1].length; out.push(`<h${lvl}>${inline(esc(h[2]))}</h${lvl}>`); continue; }

    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushPara(); closeList(); out.push("<hr />"); continue; }

    const bq = line.match(/^>\s?(.*)$/);
    if (bq) { flushPara(); closeList(); out.push(`<blockquote>${inline(esc(bq[1]))}</blockquote>`); continue; }

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) { flushPara(); if (listType !== "ul") { closeList(); out.push("<ul>"); listType = "ul"; } out.push(`<li>${inline(esc(ul[1]))}</li>`); continue; }

    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ol) { flushPara(); if (listType !== "ol") { closeList(); out.push("<ol>"); listType = "ol"; } out.push(`<li>${inline(esc(ol[1]))}</li>`); continue; }

    closeList();
    para.push(line);
  }

  if (inCode) out.push(`<pre><code>${esc(codeBuf.join("\n"))}</code></pre>`);
  flushPara();
  closeList();
  return out.join("\n");
}
