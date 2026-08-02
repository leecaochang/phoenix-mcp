import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

// The injected modal mounts the panel's CSS as an inline string; stub it so the
// test does not depend on Vite's ?inline CSS handling.
vi.mock("../phoenix-mcp-panel.css?inline", () => ({ default: "" }));

// Real t()/interpolate, but hasMessage and loadTranslations are observable: the
// modal's whole job here is to notice an EMPTY catalog and fill it, and both
// halves of that decision have to be steerable to test either branch.
vi.mock("../i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../i18n")>();
  return { ...actual, hasMessage: vi.fn(() => true), loadTranslations: vi.fn(async () => {}) };
});

const { getEntityTree, getMesaVocabulary, getMesaProfile, putMesaProfile, getMesaDevice } = vi.hoisted(() => ({
  getEntityTree: vi.fn(),
  getMesaVocabulary: vi.fn(),
  getMesaProfile: vi.fn(),
  putMesaProfile: vi.fn(),
  getMesaDevice: vi.fn(),
}));

vi.mock("../api", () => {
  class ApiError extends Error {
    status: number;
    code: string;
    constructor(s: number, c: string, m: string) {
      super(m);
      this.status = s;
      this.code = c;
    }
  }
  return {
    api: { getEntityTree, getMesaVocabulary, getMesaProfile, putMesaProfile, getMesaDevice },
    setHass: vi.fn(),
    ApiError,
  };
});

import { QuickAddApp } from "../inject/QuickAdd";
import { hasMessage, loadTranslations } from "../i18n";

const TREE = {
  automation: {
    devices: {},
    deviceless_entities: ["automation.morning"],
    entity_details: {
      "automation.morning": {
        entity_id: "automation.morning",
        friendly_name: "Morning",
        device_id: null,
        area_id: null,
        area_name: null,
        labels: [],
      },
    },
  },
};

const DETAIL = {
  entity_id: "automation.morning",
  stored: null,
  effective: {},
  explanation: { entity_id: "automation.morning", explanation: [], conflicts_detected: false, warnings: [] },
};

describe("QuickAddApp", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getEntityTree.mockResolvedValue(TREE);
    getMesaVocabulary.mockResolvedValue({ canonical_tags: ["lighting.ambient"], canonical_roots: ["lighting"] });
    getMesaProfile.mockResolvedValue(DETAIL);
  });

  it("loads the registry then mounts the editor for the target entity", async () => {
    render(<QuickAddApp scope="entity" profileKey="automation.morning" isNew onClose={() => {}} onSaved={() => {}} />);
    expect(await screen.findByText(/Add entity profile/i)).toBeTruthy();
    expect(getEntityTree).toHaveBeenCalled();
    expect(getMesaVocabulary).toHaveBeenCalled();
  });

  it("shows an error with a Close action when the registry fails", async () => {
    getEntityTree.mockRejectedValueOnce(new Error("boom"));
    const onClose = vi.fn();
    render(<QuickAddApp scope="entity" profileKey="automation.morning" isNew onClose={onClose} onSaved={() => {}} />);
    expect(await screen.findByText(/Close/i)).toBeTruthy();
  });
});

/** A device id is 32 hex characters and names nothing an operator recognises.
 *
 * The injected modal has no picker source to look a name up in (it always
 * supplies a locked key), so the editor rendered whatever key it was handed:
 * from an HA device page that meant a hex id in both the title and the target
 * field. The injector already resolves a display name for the button tooltip,
 * so the fix is to pass it; these assert it reaches the rendered output.
 *
 * The hex is asserted ABSENT rather than only asserting the name is present,
 * because showing both would be the same defect with extra steps: the operator
 * still cannot tell which device they are editing at a glance.
 */
describe("QuickAddApp device targets", () => {
  const DEVICE_ID = "0a79f96eaa7cb72d762280a0bafbfaed";
  const DEVICE_NAME = "Pantry Light ZB";

  beforeEach(() => {
    vi.clearAllMocks();
    getEntityTree.mockResolvedValue(TREE);
    getMesaVocabulary.mockResolvedValue({ canonical_tags: [], canonical_roots: [] });
    getMesaDevice.mockResolvedValue({ stored: null });
  });

  it("names the device in the locked target field, not its opaque id", async () => {
    render(
      <QuickAddApp
        scope="device"
        profileKey={DEVICE_ID}
        keyLabel={DEVICE_NAME}
        isNew
        onClose={() => {}}
        onSaved={() => {}}
      />
    );
    expect(await screen.findByDisplayValue(DEVICE_NAME)).toBeTruthy();
    expect(screen.queryByDisplayValue(DEVICE_ID)).toBeNull();
  });

  it("names the device in the edit title, not its opaque id", async () => {
    render(
      <QuickAddApp
        scope="device"
        profileKey={DEVICE_ID}
        keyLabel={DEVICE_NAME}
        isNew={false}
        onClose={() => {}}
        onSaved={() => {}}
      />
    );
    expect(await screen.findByText(new RegExp(DEVICE_NAME))).toBeTruthy();
    expect(document.body.textContent).not.toContain(DEVICE_ID);
  });

  it("loads its own strings when this module instance has an empty catalog", async () => {
    // The lazy chunk imports its parent BARE while the page loads it with a
    // cache-busting ?v=, so opening the modal instantiates a second injector
    // copy that stands down and never loads translations. Every label then
    // rendered as its own key. The modal therefore owns its strings.
    vi.mocked(hasMessage).mockReturnValue(false);
    document.body.innerHTML = "<home-assistant></home-assistant>";
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (document.querySelector("home-assistant") as any).hass = { language: "en" };

    render(
      <QuickAddApp scope="device" profileKey={DEVICE_ID} keyLabel={DEVICE_NAME} isNew onClose={() => {}} onSaved={() => {}} />
    );
    await screen.findByDisplayValue(DEVICE_NAME);
    expect(loadTranslations).toHaveBeenCalled();
  });

  it("does not refetch strings when the catalog is already loaded", async () => {
    vi.mocked(hasMessage).mockReturnValue(true);
    render(
      <QuickAddApp scope="device" profileKey={DEVICE_ID} keyLabel={DEVICE_NAME} isNew onClose={() => {}} onSaved={() => {}} />
    );
    await screen.findByDisplayValue(DEVICE_NAME);
    expect(loadTranslations).not.toHaveBeenCalled();
  });

  it("falls back to the key when no name was supplied", async () => {
    render(
      <QuickAddApp
        scope="device"
        profileKey={DEVICE_ID}
        isNew
        onClose={() => {}}
        onSaved={() => {}}
      />
    );
    expect(await screen.findByDisplayValue(DEVICE_ID)).toBeTruthy();
  });
});
