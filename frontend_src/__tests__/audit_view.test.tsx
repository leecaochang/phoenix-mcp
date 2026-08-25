import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, waitFor, within } from "@testing-library/react";
import { AuditView } from "../views/AuditView";
import { api } from "../api";
import type { AuditEntry } from "../types";

vi.mock("../api", () => ({
  api: { getAudit: vi.fn() },
}));

function entry(over: Partial<AuditEntry> = {}): AuditEntry {
  return {
    request_id: "r1",
    timestamp: "2026-01-01T00:00:00Z",
    token_id: "t1",
    token_name: "my_token",
    method: "tools/call",
    resource: "create_automation",
    outcome: "allowed",
    client_ip: "127.0.0.1",
    pass_through: false,
    ...over,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("AuditView", () => {
  beforeEach(() => vi.clearAllMocks());

  it("loads the first page and shows Load more only while more rows remain", async () => {
    vi.mocked(api.getAudit).mockResolvedValue({
      entries: [entry({ request_id: "r1" })],
      total: 3,
    });

    const { findByText, queryByText } = render(<AuditView tokens={[]} />);
    await findByText("create_automation");

    expect(api.getAudit).toHaveBeenCalledWith({
      outcome: undefined, token_id: undefined, since: undefined,
      method: undefined, resource: undefined, ip: undefined,
      limit: 100, offset: 0,
    });
    expect(queryByText("Load more")).toBeTruthy();
  });

  it("Load more appends the next page at the accumulated offset", async () => {
    vi.mocked(api.getAudit)
      .mockResolvedValueOnce({ entries: [entry({ request_id: "r1", resource: "first" })], total: 2 })
      .mockResolvedValueOnce({ entries: [entry({ request_id: "r2", resource: "second" })], total: 2 });

    const { findByText, getByText, queryByText } = render(<AuditView tokens={[]} />);
    await findByText("first");

    fireEvent.click(getByText("Load more"));
    await findByText("second");

    expect(api.getAudit).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 1, limit: 100 }));
    // Both pages of a two-total feed are now shown, so Load more disappears.
    expect(queryByText("Load more")).toBeNull();
  });

  it("changing a dropdown filter reloads from offset 0 with the filter applied", async () => {
    vi.mocked(api.getAudit).mockResolvedValue({ entries: [entry()], total: 1 });
    const { findByText, getByLabelText } = render(<AuditView tokens={[]} />);
    await findByText("create_automation");

    fireEvent.change(getByLabelText("Filter by outcome"), { target: { value: "denied" } });

    await waitFor(() =>
      expect(api.getAudit).toHaveBeenLastCalledWith(expect.objectContaining({ outcome: "denied", offset: 0 })),
    );
  });

  it("discards a slow response from the previous filter", async () => {
    const stale = deferred<{ entries: AuditEntry[]; total: number }>();
    const current = deferred<{ entries: AuditEntry[]; total: number }>();
    vi.mocked(api.getAudit)
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(current.promise);

    const { getByLabelText, findByText, queryByText } = render(<AuditView tokens={[]} />);
    await waitFor(() => expect(api.getAudit).toHaveBeenCalledTimes(1));
    fireEvent.change(getByLabelText("Filter by outcome"), { target: { value: "denied" } });
    await waitFor(() => expect(api.getAudit).toHaveBeenCalledTimes(2));

    current.resolve({ entries: [entry({ resource: "current-denied", outcome: "denied" })], total: 1 });
    await findByText("current-denied");
    stale.resolve({ entries: [entry({ resource: "stale-allowed" })], total: 1 });

    await waitFor(() => expect(queryByText("current-denied")).not.toBeNull());
    expect(queryByText("stale-allowed")).toBeNull();
  });

  it("debounces the method/resource/ip text filters instead of firing per keystroke", async () => {
    vi.useFakeTimers();
    vi.mocked(api.getAudit).mockResolvedValue({ entries: [], total: 0 });
    const { getByLabelText } = render(<AuditView tokens={[]} />);
    await vi.waitFor(() => expect(api.getAudit).toHaveBeenCalledTimes(1));

    fireEvent.change(getByLabelText("Filter by method"), { target: { value: "c" } });
    fireEvent.change(getByLabelText("Filter by method"), { target: { value: "cr" } });
    fireEvent.change(getByLabelText("Filter by method"), { target: { value: "cre" } });
    // Still just the initial load - no request fired per keystroke yet.
    expect(api.getAudit).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(300);
    await vi.waitFor(() =>
      expect(api.getAudit).toHaveBeenLastCalledWith(expect.objectContaining({ method: "cre" })),
    );
    vi.useRealTimers();
  });

  it("labels the column as Source and presents internal surfaces by friendly names", async () => {
    vi.mocked(api.getAudit).mockResolvedValue({
      entries: [
        entry({ request_id: "r1", client_ip: "agentcli" }),
        entry({ request_id: "r2", client_ip: "assist" }),
        entry({ request_id: "r3", client_ip: "voice" }),
        entry({ request_id: "r4", client_ip: "ai_task" }),
      ],
      total: 4,
    });

    const { findByText, getByRole, getByText } = render(<AuditView tokens={[]} />);
    await findByText("Agent Chat");

    expect(getByRole("columnheader", { name: /Source/ })).toBeTruthy();
    expect(getByText("Assist Tool Provider")).toBeTruthy();
    expect(getByText("Voice Agent")).toBeTruthy();
    expect(getByText("AI Task")).toBeTruthy();
    fireEvent.click(getByText("Agent Chat"));
    expect(within(getByRole("dialog")).getByText("Source")).toBeTruthy();
    expect(within(getByRole("dialog")).getByText("Agent Chat")).toBeTruthy();
    expect(within(getByRole("dialog")).queryByText("agentcli")).toBeNull();
  });

  it("filters a friendly Agent Chat source name using the stored marker", async () => {
    vi.useFakeTimers();
    vi.mocked(api.getAudit).mockResolvedValue({ entries: [], total: 0 });
    const { getByLabelText } = render(<AuditView tokens={[]} />);
    await vi.waitFor(() => expect(api.getAudit).toHaveBeenCalledTimes(1));

    fireEvent.change(getByLabelText("Filter by source"), { target: { value: "Agent Chat" } });
    vi.advanceTimersByTime(300);

    await vi.waitFor(() =>
      expect(api.getAudit).toHaveBeenLastCalledWith(expect.objectContaining({ ip: "agentcli" })),
    );
    vi.useRealTimers();
  });
});
