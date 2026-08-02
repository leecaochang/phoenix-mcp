/** Creating a token must announce itself on the window.
 *
 * Every host caches the token list: the panel shell gates its header "Agent
 * Chat" button on a non-empty list and hands the panel-hosted chat window its
 * `tokens` prop, and the floating window (a separate bundle) refetches only on
 * this event. The wizard originally created a token without dispatching, so a
 * first token stayed invisible to both surfaces until a page reload, and the
 * chat window's prune-dead-selection effect then discarded the just-created
 * token id. Revoke already dispatched; create did not.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";

const createToken = vi.fn();
const patchToken = vi.fn();

vi.mock("../api", () => ({
  api: {
    createToken: (...a: unknown[]) => createToken(...a),
    patchToken: (...a: unknown[]) => patchToken(...a),
  },
}));

import { TokenCreateModal } from "../components/TokenCreateModal";

const RECORD = {
  id: "t1",
  name: "wizard-token",
  permissions: { domains: {}, devices: {}, entities: {} },
};

describe("token creation announces phx-tokens-changed", () => {
  let fired: number;
  const bump = () => { fired += 1; };

  beforeEach(() => {
    vi.clearAllMocks();
    fired = 0;
    window.addEventListener("phx-tokens-changed", bump);
    createToken.mockResolvedValue({ token: "phx_" + "a".repeat(64), ...RECORD });
    patchToken.mockResolvedValue(RECORD);
  });

  afterEach(() => {
    window.removeEventListener("phx-tokens-changed", bump);
  });

  it("dispatches once the token exists, so every host refreshes its list", async () => {
    render(
      <TokenCreateModal
        existingNames={[]}
        onCreated={() => {}}
        onClose={() => {}}
        onOpenSettings={() => {}}
      />,
    );

    fireEvent.change(screen.getByLabelText(/name \(required\)/i), {
      target: { value: "wizard-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createToken).toHaveBeenCalled());
    await waitFor(() => expect(fired).toBe(1));
  });
});
