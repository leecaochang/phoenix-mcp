/** In-context MESA profile quick-add modal, injected into HA's native config
 * pages. Defines <phx-mesa-quick-add>, which mounts the panel's ProfileEditor in
 * its own shadow root so an admin can create or edit an entity profile without
 * leaving the page. Lazy-loaded by inject/index.ts on first use; reuses the real
 * editor, validation, and admin API with no duplication.
 *
 * The host element is fixed and full-viewport so the injected dialog stays
 * independent of the native configuration page's layout and stacking context.
 */
import { useEffect, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { EntityTree, MesaProfileScope } from "../types";
import { api } from "../api";
import { ProfileEditor } from "../views/MesaView";
import { Modal } from "../components/Modal";
import { Loading, ErrorMsg } from "../components/common";
import PANEL_CSS from "../phoenix-mcp-panel.css?inline";
import { hasMessage, loadTranslations, resolveLanguage, t } from "../i18n";

const TAG = "phx-mesa-quick-add";

/** The page's hass object, or null before Home Assistant has booted. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function pageHass(): any {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (document.querySelector("home-assistant") as any)?.hass ?? null;
  } catch {
    return null;
  }
}

/** Load the string catalog when THIS module instance does not already have one.
 *
 * It usually will not, and the reason is structural rather than a race. The
 * injector is registered with a cache-busting query (`...inject.js?v=<mtime>`),
 * but this lazy chunk's static import of its parent is bare
 * (`./phoenix-mcp-inject.js`). A module is keyed by URL, so those are two
 * DIFFERENT instances: opening this modal instantiates a second copy of the
 * injector, which finds the active-flag already set, stands down, and therefore
 * never runs the startup that loads translations. Every string in this modal
 * then resolved to its own key, which is what shipped.
 *
 * Loading here rather than "fixing" the URL split is deliberate: the split is a
 * property of how the bundle is chunked and cache-busted, so a modal that owns
 * its own strings keeps working whichever instance ends up rendering it. The
 * probe key is one this modal itself renders, so a catalog that loaded but
 * lacks the MESA section is still treated as missing.
 */
async function ensureStrings(): Promise<void> {
  if (hasMessage("mesa.save")) return;
  const hass = pageHass();
  if (hass) await loadTranslations(hass, resolveLanguage(hass));
}

// The scopes the injector can open this modal for, derived from the one shared
// union rather than restated, so a level added there cannot be silently
// unreachable here. Domain is excluded because Home Assistant has no per-domain
// page for a button to live on.
export type QuickAddScope = Exclude<MesaProfileScope, "domain">;

export const QUICK_ADD_SCOPES: readonly QuickAddScope[] = ["entity", "device", "area", "integration"];

/** The scope named by the host element, or entity when it named nothing usable.
 *
 * Read back from an attribute, so it is a string until proven otherwise. The
 * previous form was `=== "area" ? "area" : "entity"`, which silently opened an
 * ENTITY editor for any scope it did not know: a device or integration target
 * would have been written to the wrong level with nothing reporting it.
 */
export function parseQuickAddScope(raw: string | null): QuickAddScope {
  return QUICK_ADD_SCOPES.find((s) => s === raw) ?? "entity";
}

export function QuickAddApp({
  scope,
  profileKey,
  keyLabel,
  isNew,
  onClose,
  onSaved,
}: {
  scope: QuickAddScope;
  profileKey: string;
  // What the injector calls this target. The editor shows the locked key
  // verbatim otherwise, which for a device is an opaque registry id.
  keyLabel?: string;
  isNew: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [entityTree, setEntityTree] = useState<EntityTree | null>(null);
  const [tags, setTags] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Nothing renders until the catalog is in hand, so no frame can paint a raw
  // key. The initial value is the answer when the strings are already loaded,
  // which keeps the common case a single render with no flash.
  const [stringsReady, setStringsReady] = useState(() => hasMessage("mesa.save"));

  // ProfileEditor needs the registry (for its key list + friendly names) and the
  // canonical tag vocabulary. Fetch both before mounting it, alongside the
  // strings this modal renders itself with (see ensureStrings).
  useEffect(() => {
    let cancelled = false;
    Promise.all([api.getEntityTree(), api.getMesaVocabulary(), ensureStrings()])
      .then(([tree, vocab]) => {
        if (cancelled) return;
        setStringsReady(true);
        setEntityTree(tree);
        setTags(vocab.canonical_tags);
      })
      .catch((e) => {
        if (cancelled) return;
        // Released even on failure, so the error below is readable rather than
        // being suppressed by a gate that is waiting on strings that never came.
        setStringsReady(true);
        setError(e instanceof Error ? e.message : t("inject.registryLoadFailed"));
      });
    return () => { cancelled = true; };
  }, []);

  if (!stringsReady) return null;

  if (error) {
    return (
      <Modal titleId="phx-qa-title" onClose={onClose}>
        <h3 className="modal-title" id="phx-qa-title">{t("inject.mesaProfile")}</h3>
        <ErrorMsg msg={error} />
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onClose}>{t("common.close")}</button>
        </div>
      </Modal>
    );
  }

  if (!entityTree || tags === null) {
    return (
      <Modal titleId="phx-qa-title" onClose={onClose}>
        <h3 className="modal-title" id="phx-qa-title">{t("inject.mesaProfile")}</h3>
        <Loading />
      </Modal>
    );
  }

  return (
    <ProfileEditor
      scope={scope}
      profileKey={profileKey}
      keyLabel={keyLabel}
      isNew={isNew}
      entityTree={entityTree}
      canonicalTags={tags}
      onClose={onClose}
      onSaved={onSaved}
      lockedKey
    />
  );
}

class QuickAddElement extends HTMLElement {
  private _root: Root | null = null;

  connectedCallback() {
    const scope: QuickAddScope = parseQuickAddScope(this.getAttribute("scope"));
    const key = this.getAttribute("key") || "";
    const label = this.getAttribute("label") || "";
    // The injector sets has-profile="1" when a stored profile exists, so the
    // editor opens in edit mode (loads + shows Delete) rather than create mode.
    const isNew = this.getAttribute("has-profile") !== "1";
    if (!key) {
      this.remove();
      return;
    }

    // Fixed full-viewport, but transparent: PANEL_CSS paints :host with the panel
    // background, which would otherwise cover the page in solid white. Transparent
    // lets the shared .modal-backdrop (dim + blur) show the HA page behind it.
    this.style.cssText = "position:fixed; inset:0; z-index:2147483600; background:transparent;";
    this._applyTheme();
    const shadow = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = PANEL_CSS;
    shadow.appendChild(style);
    const mount = document.createElement("div");
    mount.style.height = "100%";
    shadow.appendChild(mount);

    this._root = createRoot(mount);
    this._root.render(
      <QuickAddApp
        scope={scope}
        profileKey={key}
        keyLabel={label || undefined}
        isNew={isNew}
        onClose={() => this._close()}
        onSaved={() =>
          this.dispatchEvent(
            new CustomEvent("phx-mesa-saved", {
              detail: { scope, key },
              bubbles: true,
              composed: true,
            })
          )
        }
      />
    );
  }

  // Match HA's current theme: PANEL_CSS only switches to dark via the
  // .phx-theme-dark class (or an OS prefers-color-scheme match), but HA's dark
  // mode is its own setting, so read it from the page hass like the panel does.
  private _applyTheme() {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const dark = (document.querySelector("home-assistant") as any)?.hass?.themes?.darkMode;
      if (dark === true) this.classList.add("phx-theme-dark");
      else if (dark === false) this.classList.add("phx-theme-light");
    } catch {
      // Leave to prefers-color-scheme.
    }
  }

  private _close() {
    this._root?.unmount();
    this._root = null;
    this.remove();
  }

  disconnectedCallback() {
    this._root?.unmount();
    this._root = null;
  }
}

export function defineQuickAdd() {
  if (!customElements.get(TAG)) customElements.define(TAG, QuickAddElement);
}

defineQuickAdd();
