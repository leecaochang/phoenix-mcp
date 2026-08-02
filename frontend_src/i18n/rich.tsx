// Sentences that carry inline markup.
//
// A sentence like "Select <strong>W</strong> to grant access" has to stay one
// catalog entry: splitting it into before/inside/after fragments would stop a
// translator reordering it, which Chinese and German both need. The catalog
// holds the markup as tags and the call site supplies the renderer per tag.
//
// Tags are parsed from the catalog string BEFORE parameters are substituted,
// so a parameter value can never introduce markup. Whitespace inside a tag is
// preserved exactly, which is what keeps "Select<strong> W</strong>" rendering
// with its leading space intact.

import { Fragment, type ReactNode } from "react";
import { interpolate, rawMessage, type Params } from "./index";

export function tRich(
  key: string,
  tags: Record<string, (chunk: string) => ReactNode>,
  params?: Params,
): ReactNode {
  const template = rawMessage(key);
  // Single level only; no catalog string nests tags.
  const pattern = /<(\w+)>([\s\S]*?)<\/\1>/g;
  const parts: ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(template)) !== null) {
    if (match.index > cursor) {
      parts.push(interpolate(template.slice(cursor, match.index), params));
    }
    const [whole, name, inner] = match;
    const content = interpolate(inner, params);
    const render = tags[name];
    // An unrendered tag stays visible rather than vanishing, so a missing
    // renderer shows up instead of silently dropping the words it wrapped.
    parts.push(render ? render(content) : `<${name}>${content}</${name}>`);
    cursor = match.index + whole.length;
  }

  if (cursor < template.length) {
    parts.push(interpolate(template.slice(cursor), params));
  }

  return parts.map((node, i) => <Fragment key={i}>{node}</Fragment>);
}
