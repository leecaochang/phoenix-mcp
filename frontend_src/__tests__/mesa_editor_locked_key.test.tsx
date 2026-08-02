/** A caller that already knows the target must not have it re-validated.
 *
 * The permission tree and the in-context injector both open the profile editor
 * for a row the operator just clicked, so the target is a fact, not a choice.
 * The tree opened it UNLOCKED and supplied no picker source, so the editor
 * rendered its combobox, found the pre-filled key in an empty option list, and
 * refused it: "No matching device. Pick one from the list." on an id the panel
 * itself had just handed over. Reported live.
 *
 * `deviceOptions` is deliberately NOT passed here, because that is the state the
 * bug happened in: a locked editor must be correct without a picker source, or
 * the fix is only hiding the problem behind a second prop the next caller will
 * also forget.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";

vi.mock("../phoenix-mcp-panel.css?inline", () => ({ default: "" }));

vi.mock("../api", () => ({
  api: { getMesaProfile: vi.fn(), getMesaDevice: vi.fn(), putMesaDevice: vi.fn() },
  setHass: vi.fn(),
  ApiError: class extends Error {},
}));

import { ProfileEditor } from "../views/MesaView";

const DEVICE_ID = "0a79f96eaa7cb72d762280a0bafbfaed";
const DEVICE_NAME = "Pantry Light ZB";

function renderEditor(props: Record<string, unknown> = {}) {
  return render(
    <ProfileEditor
      scope="device"
      profileKey={DEVICE_ID}
      isNew
      entityTree={{}}
      canonicalTags={[]}
      onClose={() => {}}
      onSaved={() => {}}
      {...props}
    />
  );
}

describe("ProfileEditor with a caller-supplied target", () => {
  it("does not reject the key it was given when locked", () => {
    renderEditor({ lockedKey: true, keyLabel: DEVICE_NAME });

    expect(screen.queryByText(/No matching device/i)).toBeNull();
  });

  it("shows the device name rather than the registry id", () => {
    renderEditor({ lockedKey: true, keyLabel: DEVICE_NAME });

    expect(screen.getByDisplayValue(DEVICE_NAME)).toBeTruthy();
    expect(screen.queryByDisplayValue(DEVICE_ID)).toBeNull();
  });

  it("still refuses an unlocked key with no picker source, which is the bug", () => {
    // The counterpart assertion: unlocked is what produced the live error, so if
    // this ever stops failing, the two branches have converged and the locked
    // assertions above no longer prove anything about locking.
    renderEditor({ lockedKey: false });

    expect(screen.getByText(/No matching device/i)).toBeTruthy();
  });
});

describe("the callers that supply their own target", () => {
  /** Everything above tests ProfileEditor, but the DEFECT was in its caller:
   *  the permission tree opened the editor without `lockedKey`. Those tests
   *  would stay green if that regressed, which is the "tested next to the seam,
   *  not at it" trap. Reading the call site is crude but it guards the thing
   *  that actually broke, and rendering TokenDetailView here would mean mocking
   *  a dozen endpoints to assert one prop.
   */
  const callSiteOf = (file: string) => {
    const src = readFileSync(join(process.cwd(), "frontend_src", file), "utf8");
    const at = src.indexOf("<ProfileEditor");
    expect(at, `${file} no longer renders ProfileEditor`).toBeGreaterThan(-1);
    return src.slice(at, src.indexOf("/>", at));
  };

  it("the permission tree locks the target it was clicked on", () => {
    expect(callSiteOf("views/TokenDetail.tsx")).toMatch(/\blockedKey\b/);
  });

  it("the in-context injector does too", () => {
    expect(callSiteOf("inject/QuickAdd.tsx")).toMatch(/\blockedKey\b/);
  });
});
