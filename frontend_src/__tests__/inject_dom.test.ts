import { describe, it, expect } from "vitest";
import {
  areaIdFromPath,
  BTN_CLASS,
  deepQueryAll,
  deviceIdFromPath,
  extractEntityId,
  integrationDomainFromPath,
  isSelfMutation,
  nameInsertionPoint,
  onEntityPage,
  SUBPAGE_SURFACES,
  WIDEN_STYLE_ID,
} from "../inject/dom";

describe("onEntityPage", () => {
  it("matches the config list pages we inject into", () => {
    expect(onEntityPage("/config/entities")).toBe(true);
    expect(onEntityPage("/config/automation/dashboard")).toBe(true);
    expect(onEntityPage("/config/script/edit/123")).toBe(true);
    expect(onEntityPage("/config/scene/dashboard")).toBe(true);
    expect(onEntityPage("/config/helpers")).toBe(true);
    expect(onEntityPage("/config/person")).toBe(true);
  });

  it("ignores pages that are not entity data tables", () => {
    expect(onEntityPage("/lovelace/0")).toBe(false);
    expect(onEntityPage("/config/dashboard")).toBe(false);
    expect(onEntityPage("/config/areas/dashboard")).toBe(false);
    expect(onEntityPage("/config/devices/dashboard")).toBe(false);
    // The three subpage surfaces are decorated in their HEADER, not per row, so
    // they must not be treated as entity tables even though they are injected.
    expect(onEntityPage("/config/integrations/integration/hue")).toBe(false);
    expect(onEntityPage("/config/devices/device/abc123")).toBe(false);
    expect(onEntityPage("/config/areas/area/kitchen")).toBe(false);
  });
});

describe("areaIdFromPath", () => {
  it("extracts the area_id from an area detail URL", () => {
    expect(areaIdFromPath("/config/areas/area/fitness")).toBe("fitness");
    expect(areaIdFromPath("/config/areas/area/living_room/")).toBe("living_room");
  });

  it("returns null when not on an area detail page", () => {
    expect(areaIdFromPath("/config/areas/dashboard")).toBeNull();
    expect(areaIdFromPath("/config/entities")).toBeNull();
  });
});

describe("deviceIdFromPath", () => {
  it("extracts the device_id from a device detail URL", () => {
    expect(deviceIdFromPath("/config/devices/device/a1b2c3")).toBe("a1b2c3");
    expect(deviceIdFromPath("/config/devices/device/a1b2c3/")).toBe("a1b2c3");
  });

  it("returns null when not on a device detail page", () => {
    expect(deviceIdFromPath("/config/devices/dashboard")).toBeNull();
    expect(deviceIdFromPath("/config/areas/area/kitchen")).toBeNull();
    expect(deviceIdFromPath("/config/entities")).toBeNull();
  });
});

describe("integrationDomainFromPath", () => {
  it("extracts the domain from an integration detail URL", () => {
    expect(integrationDomainFromPath("/config/integrations/integration/hue")).toBe("hue");
    expect(integrationDomainFromPath("/config/integrations/integration/yale_access_bluetooth/")).toBe(
      "yale_access_bluetooth",
    );
  });

  it("returns null when not on an integration detail page", () => {
    expect(integrationDomainFromPath("/config/integrations")).toBeNull();
    expect(integrationDomainFromPath("/config/devices/device/abc")).toBeNull();
  });
});

describe("the three subpage extractors do not overlap", () => {
  // Each page must be claimed by exactly one scope. The scan loop tries them in
  // order and takes the first that answers, so an extractor matching another
  // scope's page would decorate it with the wrong level.
  const extractors = { device: deviceIdFromPath, area: areaIdFromPath, integration: integrationDomainFromPath };
  const pages = {
    device: "/config/devices/device/abc123",
    area: "/config/areas/area/kitchen",
    integration: "/config/integrations/integration/hue",
  };

  for (const [owner, path] of Object.entries(pages)) {
    it(`only the ${owner} extractor claims its own page`, () => {
      for (const [scope, fn] of Object.entries(extractors)) {
        if (scope === owner) expect(fn(path)).not.toBeNull();
        else expect(fn(path)).toBeNull();
      }
    });
  }
});

describe("SUBPAGE_SURFACES", () => {
  // The scope-to-extractor pairing type-checks perfectly when wrong: every
  // extractor has the same signature, so a device entry pointed at the area
  // extractor compiles. The result is a button that never appears on device
  // pages and appears with the WRONG level on area pages.
  it("each surface's extractor claims its own sample page", () => {
    for (const { scope, keyFromPath, samplePath } of SUBPAGE_SURFACES) {
      expect(keyFromPath(samplePath), `${scope} extractor did not claim its own page`).not.toBeNull();
    }
  });

  it("no surface's extractor claims another surface's page", () => {
    for (const mine of SUBPAGE_SURFACES) {
      for (const other of SUBPAGE_SURFACES) {
        if (mine.scope === other.scope) continue;
        expect(
          mine.keyFromPath(other.samplePath),
          `${mine.scope} extractor also claimed the ${other.scope} page`,
        ).toBeNull();
      }
    }
  });

  it("no surface claims an entity data-table page", () => {
    // Those are decorated per row instead, so a subpage scope matching one would
    // put a single header button on a list of many entities.
    for (const { scope, keyFromPath } of SUBPAGE_SURFACES) {
      for (const path of ["/config/entities", "/config/automation/dashboard", "/config/helpers"]) {
        expect(keyFromPath(path), `${scope} extractor claimed ${path}`).toBeNull();
      }
    }
  });
});

describe("nameInsertionPoint", () => {
  function iconCellWith(...extra: Element[]): HTMLElement {
    const c = document.createElement("div");
    c.setAttribute("role", "cell");
    c.className = "mdc-data-table__cell mdc-data-table__cell--icon";
    c.appendChild(document.createElement("ha-state-icon"));
    extra.forEach((e) => c.appendChild(e));
    return c;
  }
  function textCell(text: string): HTMLElement {
    const c = document.createElement("div");
    c.setAttribute("role", "cell");
    c.className = "mdc-data-table__cell";
    c.textContent = text;
    return c;
  }

  it("inserts into the icon's cell, right after the icon", () => {
    const iconC = iconCellWith();
    const device = textCell("Front Door"); // the next cell (Device) must NOT win
    const row = document.createElement("div");
    row.append(iconC, device);
    const point = nameInsertionPoint(row)!;
    expect(point.parent).toBe(iconC);
    const iconEl = iconC.querySelector("ha-state-icon");
    expect(point.before).toBe(iconEl?.nextSibling ?? null);
  });

  it("places after the icon even when the name shares the icon's cell", () => {
    const nameEl = document.createElement("span"); // component-rendered name in the same cell
    const iconC = iconCellWith(nameEl);
    const row = document.createElement("div");
    row.append(iconC, textCell("Outside"));
    const point = nameInsertionPoint(row)!;
    expect(point.parent).toBe(iconC);
    expect(point.before).toBe(nameEl); // icon.nextSibling === the name element
  });

  it("returns null when there is no icon yet (so a later scan retries)", () => {
    const a = textCell("X");
    const b = textCell("Y");
    const row = document.createElement("div");
    row.append(a, b);
    expect(nameInsertionPoint(row)).toBeNull();
  });
});

describe("extractEntityId", () => {
  function row(props: Record<string, unknown>, attrs: Record<string, string> = {}): HTMLElement {
    const el = document.createElement("div");
    Object.assign(el, props);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }

  it("reads a valid entity_id from the .rowId property", () => {
    expect(extractEntityId(row({ rowId: "automation.morning" }))).toBe("automation.morning");
    expect(extractEntityId(row({ rowId: "input_boolean.guest_mode" }))).toBe("input_boolean.guest_mode");
  });

  it("falls back to data attributes", () => {
    expect(extractEntityId(row({}, { "data-row-id": "script.run" }))).toBe("script.run");
  });

  it("returns null for non-entity ids, so the feature self-disables (no oracle)", () => {
    expect(extractEntityId(row({ rowId: "12345" }))).toBeNull();        // numeric row id
    expect(extractEntityId(row({ rowId: "8d1fconfigentry" }))).toBeNull(); // config-entry id (no dot)
    expect(extractEntityId(row({}))).toBeNull();                         // nothing present
  });
});

describe("isSelfMutation", () => {
  function ourButton(): HTMLElement {
    const b = document.createElement("button");
    b.className = BTN_CLASS;
    return b;
  }
  function widenStyle(): HTMLElement {
    const s = document.createElement("style");
    s.id = WIDEN_STYLE_ID;
    return s;
  }
  function rec(p: { target?: Node; added?: Node[]; removed?: Node[] }): MutationRecord {
    return {
      target: p.target ?? document.createElement("div"),
      addedNodes: (p.added ?? []) as unknown as NodeList,
      removedNodes: (p.removed ?? []) as unknown as NodeList,
    } as MutationRecord;
  }

  it("treats our own glyph swap (textContent on the button) as self", () => {
    const btn = ourButton();
    // applyButtonState swapping +/check: childList churn whose target is our button.
    const r = rec({ target: btn, added: [document.createTextNode("+")], removed: [document.createTextNode("✓")] });
    expect(isSelfMutation([r])).toBe(true);
  });

  it("treats our button being inserted, and the width style appended, as self", () => {
    const cell = document.createElement("div");
    const sr = document.createElement("div");
    expect(
      isSelfMutation([
        rec({ target: cell, added: [ourButton()] }),
        rec({ target: sr, added: [widenStyle()] }),
      ])
    ).toBe(true);
  });

  it("treats HA removing our button as NOT self, so a later scan re-adds it", () => {
    const cell = document.createElement("div");
    expect(isSelfMutation([rec({ target: cell, removed: [ourButton()] })])).toBe(false);
  });

  it("treats a genuine HA row re-render (foreign nodes) as NOT self", () => {
    const tbody = document.createElement("div");
    const foreignRow = document.createElement("div"); // not our button/style
    expect(isSelfMutation([rec({ target: tbody, added: [foreignRow] })])).toBe(false);
  });

  it("is NOT self when a batch mixes our writes with a foreign change", () => {
    const cell = document.createElement("div");
    expect(
      isSelfMutation([
        rec({ target: cell, added: [ourButton()] }), // self
        rec({ target: cell, added: [document.createElement("span")] }), // foreign
      ])
    ).toBe(false);
  });
});

describe("deepQueryAll", () => {
  it("pierces open shadow roots to find the data table", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const sr = host.attachShadow({ mode: "open" });
    const table = document.createElement("ha-data-table");
    sr.appendChild(table);

    expect(deepQueryAll("ha-data-table")).toContain(table);
    host.remove();
  });

  it("returns empty when the anchor is absent (graceful no-op)", () => {
    expect(deepQueryAll("ha-nonexistent-table-xyz")).toEqual([]);
  });
});
