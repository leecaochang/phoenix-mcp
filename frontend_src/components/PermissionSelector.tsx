import React from "react";
import type { NodeState } from "../types";
import { t } from "../i18n";

interface Props {
  value: NodeState;
  onChange: (state: NodeState) => void;
  disabled?: boolean;
  label?: string;
}

const BUTTONS: { state: NodeState; label: string; titleKey: string }[] = [
  { state: "GREY", label: "N", titleKey: "perms.selGrey" },
  { state: "YELLOW", label: "R", titleKey: "perms.selYellow" },
  { state: "GREEN", label: "W", titleKey: "perms.selGreen" },
  { state: "RED", label: "D", titleKey: "perms.selRed" },
];

let _dragState: NodeState | null = null;

if (typeof document !== "undefined") {
  document.addEventListener("pointerup", () => { _dragState = null; });
  document.addEventListener("pointercancel", () => { _dragState = null; });
}

// Holds the nearest scrolling ancestor's scroll position steady for a few
// frames. Belt-and-suspenders against whatever is nudging the page on tap
// (mobile-only, not reproduced on desktop): blurring the button (below)
// covers a focus-driven "scroll the focused element into view", this covers
// anything else (a reflow, a browser scroll-anchoring heuristic) by simply
// undoing any scroll delta that shows up in the next few animation frames.
function lockScroll(container: HTMLElement | null) {
  if (!container) return;
  const el = container;
  const y = el.scrollTop;
  let frames = 0;
  function hold() {
    if (el.scrollTop !== y) el.scrollTop = y;
    frames += 1;
    if (frames < 6) requestAnimationFrame(hold);
  }
  requestAnimationFrame(hold);
}

export const PermissionSelector = React.memo(function PermissionSelector({ value, onChange, disabled, label = "Permission" }: Props) {
  return (
    <div
      className="perm-selector"
      role="group"
      aria-label={label}
      style={{ touchAction: "none" }}
      onPointerEnter={(e) => {
        if (_dragState !== null && !disabled) {
          lockScroll(e.currentTarget.closest<HTMLElement>(".phx-content"));
          onChange(_dragState);
        }
      }}
    >
      {BUTTONS.map(({ state, label, titleKey }) => {
        const title = t(titleKey);
        return (
        <button
          key={state}
          type="button"
          title={title}
          className={`perm-btn${value === state ? ` active-${state}` : ""}`}
          onPointerDown={(e) => {
            if (disabled) return;
            e.preventDefault();
            e.currentTarget.releasePointerCapture(e.pointerId);
            lockScroll(e.currentTarget.closest<HTMLElement>(".phx-content"));
            const newState: NodeState = state === value ? "GREY" : state;
            _dragState = newState;
            onChange(newState);
            // Unconditional, and safe for keyboard users: this only runs on
            // the pointer path (a keyboard Enter/Space activation never
            // fires onPointerDown), so it can't blur a real Tab-focused
            // button. Belt-and-suspenders alongside lockScroll above for
            // whatever is causing the mobile-only scroll on tap.
            e.currentTarget.blur();
          }}
          disabled={disabled}
          aria-pressed={value === state}
          aria-label={value === state ? t("perms.selSelected", { title }) : title}
        >
          {label}
        </button>
        );
      })}
    </div>
  );
});
