/** The permission tree's MESA link must open the SCOPE it is standing on.
 *
 * The tree renders three levels an operator can profile (domain group, device
 * group, entity row) and this one component serves all three. Nothing about the
 * markup differs between them, so a wrong or missing `scope` produces a button
 * that looks perfect and writes the profile at the wrong inheritance level. That
 * is the same silent-fallthrough class that once routed every unknown scope to
 * the area endpoint, and tsc cannot see it because every call has valid types.
 *
 * So these assert the ARGUMENTS the click delivers, not that a click happened.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { MesaProfileLink } from "../components/MesaProfileLink";

describe("MesaProfileLink", () => {
  it("defaults to entity scope, so existing call sites are unchanged", () => {
    const onOpen = vi.fn();
    render(<MesaProfileLink targetKey="light.kitchen" exists={false} onOpen={onOpen} />);

    fireEvent.click(screen.getByRole("button"));

    expect(onOpen).toHaveBeenCalledWith("light.kitchen", "entity", undefined);
  });

  it("opens a device profile with the device id, not the entity scope", () => {
    const onOpen = vi.fn();
    render(
      <MesaProfileLink
        targetKey="0a79f96eaa7cb72d762280a0bafbfaed"
        scope="device"
        targetName="Pantry Light ZB"
        exists={false}
        onOpen={onOpen}
      />
    );

    fireEvent.click(screen.getByRole("button"));

    // The name travels with the click so the editor never has to resolve an
    // opaque device id for itself.
    expect(onOpen).toHaveBeenCalledWith(
      "0a79f96eaa7cb72d762280a0bafbfaed", "device", "Pantry Light ZB");
  });

  it("opens a domain profile with the domain as its key", () => {
    const onOpen = vi.fn();
    render(<MesaProfileLink targetKey="light" scope="domain" exists onOpen={onOpen} />);

    fireEvent.click(screen.getByRole("button"));

    expect(onOpen).toHaveBeenCalledWith("light", "domain", undefined);
  });

  it("names a device by its name, because its key is an opaque registry id", () => {
    render(
      <MesaProfileLink
        targetKey="0a79f96eaa7cb72d762280a0bafbfaed"
        scope="device"
        targetName="Pantry Light ZB"
        exists={false}
        onOpen={() => {}}
      />
    );

    const label = screen.getByRole("button").getAttribute("aria-label") ?? "";
    expect(label).toContain("Pantry Light ZB");
    expect(label).not.toContain("0a79f96e");
  });

  it("gives each scope its own wording rather than one interpolated sentence", () => {
    // A shared sentence with a {scope} placeholder reads as "the MESA Device
    // profile" in English and needs a different word order elsewhere, so the
    // scopes deliberately do not share a key. If they ever collapse to one,
    // these labels become identical and this fails.
    const labelFor = (props: Parameters<typeof MesaProfileLink>[0]) => {
      const { unmount } = render(<MesaProfileLink {...props} />);
      const text = screen.getByRole("button").getAttribute("aria-label") ?? "";
      unmount();
      return text;
    };
    const common = { exists: false, onOpen: () => {} } as const;
    const entity = labelFor({ ...common, targetKey: "x" });
    const device = labelFor({ ...common, targetKey: "x", scope: "device" });
    const domain = labelFor({ ...common, targetKey: "x", scope: "domain" });

    expect(new Set([entity, device, domain]).size).toBe(3);
  });

  it("distinguishes an existing profile from one to create", () => {
    const { unmount } = render(
      <MesaProfileLink targetKey="light" scope="domain" exists onOpen={() => {}} />);
    const viewLabel = screen.getByRole("button").getAttribute("aria-label");
    expect(screen.getByRole("button").textContent).toBe("MESA");
    unmount();

    render(<MesaProfileLink targetKey="light" scope="domain" exists={false} onOpen={() => {}} />);
    expect(screen.getByRole("button").textContent).toBe("+");
    expect(screen.getByRole("button").getAttribute("aria-label")).not.toBe(viewLabel);
  });
});
