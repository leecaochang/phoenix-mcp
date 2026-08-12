import { describe, expect, it, vi } from "vitest";
import { claimInjectController, compareBuilds } from "../inject/ownership";

describe("injected module ownership", () => {
  it("orders dotted numeric build identifiers", () => {
    expect(compareBuilds("1.0.102", "1.0.99")).toBeGreaterThan(0);
    expect(compareBuilds("1.2", "1.2.0")).toBe(0);
  });

  it("disposes an older build before the newer controller takes ownership", () => {
    const host: Record<string, unknown> = {};
    const oldDispose = vi.fn();
    const nextDispose = vi.fn();
    expect(claimInjectController(host, "inject", { build: "1.0.100", dispose: oldDispose })).toBe(true);
    expect(claimInjectController(host, "inject", { build: "1.0.101", dispose: nextDispose })).toBe(true);
    expect(oldDispose).toHaveBeenCalledOnce();
    expect(host.inject).toMatchObject({ build: "1.0.101" });
  });

  it("keeps an equal or newer active controller", () => {
    const current = { build: "1.0.101", dispose: vi.fn() };
    const host: Record<string, unknown> = { inject: current };
    expect(claimInjectController(host, "inject", { build: "1.0.101", dispose: vi.fn() })).toBe(false);
    expect(claimInjectController(host, "inject", { build: "1.0.100", dispose: vi.fn() })).toBe(false);
    expect(current.dispose).not.toHaveBeenCalled();
    expect(host.inject).toBe(current);
  });
});
