/** Read-only YAML viewer for configuration snapshots. Prefers HA's native
 * <ha-code-editor> (CodeMirror: monospace, syntax highlighting, scrollbars) and
 * falls back to a styled <pre> when that element is not registered. */
import React, { useEffect, useRef, useState } from "react";
import yaml from "js-yaml";
import { t } from "../i18n";

export function toYaml(value: Record<string, unknown> | null): string {
  if (value == null) return "";
  try {
    return yaml.dump(value, { indent: 2, lineWidth: 100, sortKeys: false, noRefs: true });
  } catch {
    // Fall back to JSON if the structure is somehow not YAML-serialisable.
    return JSON.stringify(value, null, 2);
  }
}

export function YamlView({ value }: { value: string }) {
  const ref = useRef<HTMLElement | null>(null);
  const [useEditor, setUseEditor] = useState(() => !!customElements.get("ha-code-editor"));

  // ha-code-editor is lazy-loaded by HA, so it may not be registered at the
  // moment we mount. Rather than deciding once and being stuck on the <pre>
  // fallback, upgrade to the editor as soon as HA defines the element
  // (whenDefined resolves whenever that happens; if it never does, we simply
  // keep the styled <pre>).
  useEffect(() => {
    if (useEditor || typeof customElements === "undefined") return;
    let cancelled = false;
    customElements
      .whenDefined("ha-code-editor")
      .then(() => { if (!cancelled) setUseEditor(true); })
      .catch(() => { /* never defined: keep the <pre> fallback */ });
    return () => { cancelled = true; };
  }, [useEditor]);

  useEffect(() => {
    if (!useEditor || !ref.current) return;
    const el = ref.current as unknown as Record<string, unknown>;
    el.mode = "yaml";
    el.readOnly = true;
    el.linewrap = true;
    el.value = value;
  }, [useEditor, value]);

  if (!value) return <pre className="yaml-pre yaml-pre-empty">{t("common.none")}</pre>;

  if (useEditor) {
    return <ha-code-editor ref={ref as React.RefObject<HTMLElement>} className="yaml-editor" />;
  }
  return <pre className="yaml-pre">{value}</pre>;
}
