/**
 * The shared approval-event subscriber, used by BOTH bundles.
 *
 * An approval is shown in three places and each one used to learn it had been
 * acted on by a different route. Agent Chat's route was the slowest: it had no
 * `hass` at all and updated only from its own SSE stream, which rides the agent's
 * turn resuming, so a reject in the panel left the chat bubble offering Approve
 * and Reject for seconds. The panel and the injected chat window are separate
 * bundles sharing no module state, so the only way they cannot drift apart again
 * is for both to go through this one module; these pin its contract.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { subscribeApprovalEvents } from "../utils/approval_events";

type Sub = (cb: (ev: unknown) => void, event: string) => Promise<() => void>;

function fakeHass() {
  const handlers: Record<string, (ev: unknown) => void> = {};
  const unsub = vi.fn();
  const subscribeEvents: Sub = (cb, event) => {
    handlers[event] = cb;
    return Promise.resolve(unsub);
  };
  return { hass: { connection: { subscribeEvents } }, handlers, unsub };
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("subscribeApprovalEvents", () => {
  it("reports a resolution with its status", async () => {
    const { hass, handlers } = fakeHass();
    const onResolved = vi.fn();
    subscribeApprovalEvents(hass, { onResolved });
    await Promise.resolve();

    handlers["phoenix_mcp_approval_resolved"]({ data: { approval_id: "ap1", status: "rejected" } });
    expect(onResolved).toHaveBeenCalledWith("ap1", "rejected");
  });

  it("reports a claim and a release as one handler with a flag", async () => {
    const { hass, handlers } = fakeHass();
    const onClaimChanged = vi.fn();
    subscribeApprovalEvents(hass, { onClaimChanged });
    await Promise.resolve();

    handlers["phoenix_mcp_approval_claimed"]({ data: { approval_id: "ap1", claimed: true } });
    handlers["phoenix_mcp_approval_claimed"]({ data: { approval_id: "ap1", claimed: false } });
    expect(onClaimChanged.mock.calls).toEqual([["ap1", true], ["ap1", false]]);
  });

  it("treats a missing claimed flag as claimed", async () => {
    // The safe direction: stop offering an action rather than offer one that the
    // server would only answer with a 409.
    const { hass, handlers } = fakeHass();
    const onClaimChanged = vi.fn();
    subscribeApprovalEvents(hass, { onClaimChanged });
    await Promise.resolve();

    handlers["phoenix_mcp_approval_claimed"]({ data: { approval_id: "ap1" } });
    expect(onClaimChanged).toHaveBeenCalledWith("ap1", true);
  });

  it("ignores an event with no approval id", async () => {
    const { hass, handlers } = fakeHass();
    const onResolved = vi.fn();
    const onClaimChanged = vi.fn();
    subscribeApprovalEvents(hass, { onResolved, onClaimChanged });
    await Promise.resolve();

    handlers["phoenix_mcp_approval_resolved"]({ data: {} });
    handlers["phoenix_mcp_approval_claimed"]({ data: { approval_id: 7 } });
    expect(onResolved).not.toHaveBeenCalled();
    expect(onClaimChanged).not.toHaveBeenCalled();
  });

  it("subscribes only to the events the caller asked for", async () => {
    const events: string[] = [];
    const hass = {
      connection: {
        subscribeEvents: ((_cb, event) => {
          events.push(event);
          return Promise.resolve(() => {});
        }) as Sub,
      },
    };
    subscribeApprovalEvents(hass, { onClaimChanged: vi.fn() });
    await Promise.resolve();
    expect(events).toEqual(["phoenix_mcp_approval_claimed"]);
  });

  it("finds hass on the document when the caller has none", async () => {
    // This is what lets Agent Chat subscribe at all: the component is mounted
    // from two bundles and is handed no hass by either.
    const { hass, handlers } = fakeHass();
    const el = document.createElement("home-assistant");
    (el as unknown as { hass: unknown }).hass = hass;
    document.body.appendChild(el);

    const onResolved = vi.fn();
    subscribeApprovalEvents(null, { onResolved });
    await Promise.resolve();

    handlers["phoenix_mcp_approval_resolved"]({ data: { approval_id: "ap1", status: "approved" } });
    expect(onResolved).toHaveBeenCalledWith("ap1", "approved");
  });

  it("is a safe no-op with no connection, so no caller has to feature-detect", () => {
    const unsubscribe = subscribeApprovalEvents(null, { onResolved: vi.fn() });
    expect(() => unsubscribe()).not.toThrow();
  });

  it("unsubscribes every subscription it made", async () => {
    const { hass, unsub } = fakeHass();
    const unsubscribe = subscribeApprovalEvents(hass, {
      onResolved: vi.fn(), onClaimChanged: vi.fn(),
    });
    await Promise.resolve();
    unsubscribe();
    expect(unsub).toHaveBeenCalledTimes(2);
  });

  it("unsubscribes a subscription that resolved after teardown", async () => {
    // The subscribe promise can settle after the component unmounted; without
    // the cancelled flag that listener would outlive the window forever.
    let resolveSub: (u: () => void) => void = () => {};
    const unsub = vi.fn();
    const hass = {
      connection: {
        subscribeEvents: (() => new Promise<() => void>((res) => { resolveSub = res; })) as Sub,
      },
    };
    const unsubscribe = subscribeApprovalEvents(hass, { onResolved: vi.fn() });
    unsubscribe();
    resolveSub(unsub);
    await Promise.resolve();
    expect(unsub).toHaveBeenCalledTimes(1);
  });
});
