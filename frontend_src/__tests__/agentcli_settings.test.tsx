import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";

const getAgentCliProviders = vi.fn();
const probeAgentCliProvider = vi.fn();
const createAgentCliProvider = vi.fn();
const deleteAgentCliProvider = vi.fn();
const getAgentCliModels = vi.fn();
const setAgentCliProviderModel = vi.fn();
const probeAgentCliCapabilities = vi.fn();
const refreshAgentCliProvider = vi.fn();

vi.mock("../api", () => ({
  api: {
    getAgentCliProviders: (...a: unknown[]) => getAgentCliProviders(...a),
    probeAgentCliProvider: (...a: unknown[]) => probeAgentCliProvider(...a),
    createAgentCliProvider: (...a: unknown[]) => createAgentCliProvider(...a),
    deleteAgentCliProvider: (...a: unknown[]) => deleteAgentCliProvider(...a),
    getAgentCliModels: (...a: unknown[]) => getAgentCliModels(...a),
    setAgentCliProviderModel: (...a: unknown[]) => setAgentCliProviderModel(...a),
    probeAgentCliCapabilities: (...a: unknown[]) => probeAgentCliCapabilities(...a),
    refreshAgentCliProvider: (...a: unknown[]) => refreshAgentCliProvider(...a),
  },
}));

import { AgentCliSettings } from "../components/AgentCliSettings";

function instances() {
  return { instances: [{ id: "i1", kind: "deepseek", name: "DeepSeek", model: "deepseek-v4-flash" }] };
}

function renderCard() {
  return render(
    <AgentCliSettings
      scrollback={100}
      onScrollbackChange={() => {}}
      maxIterations={20}
      onMaxIterationsChange={() => {}}
      globalVisible={false}
      onGlobalChange={() => {}}
      saving={false}
    />,
  );
}

describe("AgentCliSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getAgentCliProviders.mockResolvedValue(instances());
    probeAgentCliProvider.mockResolvedValue({ ok: true, models: ["claude-opus-4-8", "claude-haiku"] });
    createAgentCliProvider.mockResolvedValue({ instance: { id: "i2", kind: "claude", name: "Claude", model: "claude-opus-4-8" } });
    deleteAgentCliProvider.mockResolvedValue({ deleted: "i1" });
    getAgentCliModels.mockResolvedValue({ models: ["deepseek-v4-flash", "deepseek-v4-pro"] });
    setAgentCliProviderModel.mockResolvedValue({ instance: { id: "i1", model: "deepseek-v4-pro" } });
  });

  it("Validate with no API key shows an error and does not create", async () => {
    renderCard();
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalled());
    fireEvent.click(screen.getByText("Add new provider…"));  // claude selected by default
    fireEvent.click(screen.getByText("Validate"));
    await waitFor(() => expect(screen.getByText(/Enter your API key/i)).toBeInTheDocument());
    expect(createAgentCliProvider).not.toHaveBeenCalled();
  });

  it("Validate lists models and turns into Done, which creates the account", async () => {
    renderCard();
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalled());
    fireEvent.click(screen.getByText("Add new provider…"));
    fireEvent.change(screen.getByPlaceholderText("API key"), { target: { value: "sk-abc" } });
    fireEvent.click(screen.getByText("Validate"));
    await waitFor(() => expect(probeAgentCliProvider).toHaveBeenCalledWith("claude", { api_key: "sk-abc" }));
    // Model dropdown appears; the button is now "Done".
    await waitFor(() => expect(screen.getByText(/Select default model/i)).toBeInTheDocument());
    fireEvent.click(screen.getByText("Done"));
    await waitFor(() => expect(createAgentCliProvider).toHaveBeenCalled());
    expect(createAgentCliProvider.mock.calls[0][0]).toBe("claude");
  });

  it("shows a duplicate error before default-model selection", async () => {
    probeAgentCliProvider.mockRejectedValueOnce(new Error("This provider account is already configured."));
    renderCard();
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalled());
    fireEvent.click(screen.getByText("Add new provider…"));
    fireEvent.change(screen.getByPlaceholderText("API key"), { target: { value: "sk-abc" } });
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() => expect(screen.getByText("This provider account is already configured.")).toBeInTheDocument());
    expect(screen.queryByText(/Select default model/i)).toBeNull();
    expect(createAgentCliProvider).not.toHaveBeenCalled();
  });

  it("Cancel abandons the add without creating", async () => {
    renderCard();
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalled());
    fireEvent.click(screen.getByText("Add new provider…"));
    fireEvent.change(screen.getByPlaceholderText("API key"), { target: { value: "sk-abc" } });
    fireEvent.click(screen.getByText("Cancel"));
    await waitFor(() => expect(screen.queryByPlaceholderText("API key")).toBeNull());
    expect(createAgentCliProvider).not.toHaveBeenCalled();
  });

  it("Remove asks for confirmation before deleting the instance", async () => {
    renderCard();
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalled());
    fireEvent.click(screen.getByLabelText(/^Remove /));
    await waitFor(() => expect(screen.getByText(/Remove provider/i)).toBeInTheDocument());
    expect(deleteAgentCliProvider).not.toHaveBeenCalled();
    const removeButtons = screen.getAllByText("Remove");
    fireEvent.click(removeButtons[removeButtons.length - 1]);
    await waitFor(() => expect(deleteAgentCliProvider).toHaveBeenCalledWith("i1"));
  });

  it("surfaces a failed removal and keeps the confirm dialog open", async () => {
    deleteAgentCliProvider.mockRejectedValueOnce(new Error("server unreachable"));
    renderCard();
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalled());
    fireEvent.click(screen.getByLabelText(/^Remove /));
    await waitFor(() => expect(screen.getByText(/Remove provider/i)).toBeInTheDocument());
    const removeButtons = screen.getAllByText("Remove");
    fireEvent.click(removeButtons[removeButtons.length - 1]);
    // The error is shown and the dialog stays up (not silently closed as success).
    await waitFor(() => expect(screen.getByText("server unreachable")).toBeInTheDocument());
    expect(screen.getByText(/Remove provider/i)).toBeInTheDocument();
  });

  it("supports adding a second account of an existing kind (Ollama URL field)", async () => {
    renderCard();
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Provider type"), { target: { value: "ollama" } });
    fireEvent.click(screen.getByText("Add new provider…"));
    // Ollama shows a base-URL field, not an API key field.
    expect(screen.getByPlaceholderText("http://host:11434")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("API key")).toBeNull();
  });

  it("the global-visibility toggle reports changes", async () => {
    const onGlobal = vi.fn();
    render(
      <AgentCliSettings
        scrollback={100}
        onScrollbackChange={() => {}}
        maxIterations={20}
        onMaxIterationsChange={() => {}}
        globalVisible={false}
        onGlobalChange={onGlobal}
        saving={false}
      />,
    );
    await waitFor(() => expect(getAgentCliProviders).toHaveBeenCalled());
    fireEvent.click(screen.getByLabelText("Show Agent Chat throughout Home Assistant"));
    expect(onGlobal).toHaveBeenCalledWith(true);
  });

  it("chat memory auto-saves after a change without an explicit blur (spinner fix)", () => {
    vi.useFakeTimers();
    try {
      const onScroll = vi.fn();
      render(
        <AgentCliSettings
          scrollback={500}
          onScrollbackChange={onScroll}
          maxIterations={20}
          onMaxIterationsChange={() => {}}
          globalVisible={false}
          onGlobalChange={() => {}}
          saving={false}
        />,
      );
      const input = screen.getByLabelText("Chat history line limit");
      fireEvent.change(input, { target: { value: "300" } });
      expect(onScroll).not.toHaveBeenCalled();  // debounced, nothing yet
      vi.advanceTimersByTime(700);
      expect(onScroll).toHaveBeenCalledWith(300);
    } finally {
      vi.useRealTimers();
    }
  });
});


// The account's default model used to be frozen at creation, and the model list is
// the free half of staleness detection: a cheap authenticated GET, run exactly when
// the operator is looking at providers and can act on what it says.
describe("AgentCliSettings default model", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getAgentCliProviders.mockResolvedValue(instances());
    getAgentCliModels.mockResolvedValue({ models: ["deepseek-v4-flash", "deepseek-v4-pro"] });
    setAgentCliProviderModel.mockResolvedValue({ instance: { id: "i1", model: "deepseek-v4-pro" } });
  });

  it("changes the default model without touching the credential", async () => {
    renderCard();
    fireEvent.click(await screen.findByLabelText("Change default model"));
    const select = await screen.findByLabelText("Select default model:");
    fireEvent.change(select, { target: { value: "deepseek-v4-pro" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(setAgentCliProviderModel).toHaveBeenCalledWith("i1", "deepseek-v4-pro"));
    // Changing a model must never go near the account's credential, which the
    // delete-and-recreate workaround did every time.
    expect(deleteAgentCliProvider).not.toHaveBeenCalled();
    expect(createAgentCliProvider).not.toHaveBeenCalled();
  });

  it("warns when the configured model is no longer offered", async () => {
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "deepseek", name: "DeepSeek", model: "deepseek-chat" }],
    });
    renderCard();
    expect(await screen.findByText(/no longer offers deepseek-chat/)).toBeTruthy();
  });

  it("says nothing when the configured model is still offered", async () => {
    renderCard();
    await screen.findByLabelText("Change default model");
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    expect(screen.queryByText(/no longer offers/)).toBeNull();
  });

  it("says nothing when the provider could not be reached", async () => {
    // "Nobody checked" and "the model is gone" must not look the same. An
    // unreachable provider says nothing about the model, and reporting it as
    // retired sends the operator to fix an account that is fine.
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "deepseek", name: "DeepSeek", model: "deepseek-chat" }],
    });
    getAgentCliModels.mockRejectedValue(new Error("connection refused"));
    renderCard();
    await screen.findByLabelText("Change default model");
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    expect(screen.queryByText(/no longer offers/)).toBeNull();
  });

  it("says nothing when the provider reports an empty list", async () => {
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "deepseek", name: "DeepSeek", model: "deepseek-chat" }],
    });
    getAgentCliModels.mockResolvedValue({ models: [] });
    renderCard();
    await screen.findByLabelText("Change default model");
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    expect(screen.queryByText(/no longer offers/)).toBeNull();
  });

  it("shows the retired model but does not let it be picked again", async () => {
    // Both halves matter and the first attempt only had one. It has to be
    // PRESENT, or a select whose value matches no option silently displays some
    // other model as though it were the configured one. It has to be DISABLED,
    // or the card offers the very model it has just finished warning about,
    // which is what happened live: a model deleted from the server was still
    // selectable and saveable.
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "ollama", name: "Ollama", model: "gone:8b" }],
    });
    getAgentCliModels.mockResolvedValue({ models: ["still-here:8b"] });
    renderCard();
    fireEvent.click(await screen.findByLabelText("Change default model"));
    const select = await screen.findByLabelText("Select default model:") as HTMLSelectElement;
    expect(select.value).toBe("gone:8b");
    const stale = [...select.options].find((o) => o.value === "gone:8b");
    expect(stale).toBeTruthy();
    expect(stale!.disabled).toBe(true);
    expect(stale!.text).toContain("no longer available");
    // Nothing changed, so there is nothing to save: with the option disabled,
    // pressing Save is the only way left to re-commit the dead model.
    expect((screen.getByText("Save") as HTMLButtonElement).disabled).toBe(true);
  });

  it("a model still on offer is selectable and saveable", async () => {
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "ollama", name: "Ollama", model: "a:8b" }],
    });
    getAgentCliModels.mockResolvedValue({ models: ["a:8b", "b:8b"] });
    renderCard();
    fireEvent.click(await screen.findByLabelText("Change default model"));
    const select = await screen.findByLabelText("Select default model:") as HTMLSelectElement;
    expect([...select.options].every((o) => !o.disabled)).toBe(true);
    fireEvent.change(select, { target: { value: "b:8b" } });
    expect((screen.getByText("Save") as HTMLButtonElement).disabled).toBe(false);
  });
});

// Both reports from the same live session. A BANNER now means only "this account
// is broken, fix it"; anything else belongs on the option it describes.
describe("AgentCliSettings tool-calling capability", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setAgentCliProviderModel.mockResolvedValue({ instance: { id: "i1", model: "good" } });
  });

  function withCaps(model: string, models: string[], caps: Record<string, { tools: boolean }>) {
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "ollama", name: "Ollama", model, capabilities: caps }],
    });
    getAgentCliModels.mockResolvedValue({ models });
  }

  it("marks an unusable model on its own option instead of in a banner", async () => {
    withCaps("good", ["good", "notools"], { good: { tools: true }, notools: { tools: false } });
    renderCard();
    fireEvent.click(await screen.findByLabelText("Change default model"));
    const select = await screen.findByLabelText("Select default model:") as HTMLSelectElement;
    const bad = [...select.options].find((o) => o.value === "notools")!;
    expect(bad.disabled).toBe(true);
    expect(bad.text).toContain("no tool calling");
    expect([...select.options].find((o) => o.value === "good")!.disabled).toBe(false);
  });

  it("shows no banner when the account's own model is fine", async () => {
    // The live report: a permanent banner naming every unusable model, none of
    // which the operator could select, and with nothing to fix it could not be
    // dismissed either.
    withCaps("good", ["good", "notools"], { good: { tools: true }, notools: { tools: false } });
    renderCard();
    await screen.findByLabelText("Change default model");
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    expect(screen.queryByText(/cannot call tools/)).toBeNull();
  });

  it("banners only when the account's OWN default cannot call tools", async () => {
    withCaps("notools", ["good", "notools"], { good: { tools: true }, notools: { tools: false } });
    renderCard();
    expect(await screen.findByText(/notools cannot call tools/)).toBeTruthy();
  });

  it("says nothing about a model whose capability was never declared", async () => {
    // Most providers publish nothing. An undeclared model is not an unusable one.
    withCaps("mystery", ["mystery"], {});
    renderCard();
    fireEvent.click(await screen.findByLabelText("Change default model"));
    const select = await screen.findByLabelText("Select default model:") as HTMLSelectElement;
    expect([...select.options].every((o) => !o.disabled)).toBe(true);
    expect(screen.queryByText(/cannot call tools/)).toBeNull();
  });
});

// A warning about a broken account is worth showing, but not worth showing
// forever with no way to acknowledge it.
describe("AgentCliSettings dismissible warnings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "ollama", name: "Ollama", model: "gone:8b" }],
    });
    getAgentCliModels.mockResolvedValue({ models: ["here:8b"] });
  });

  it("closes on the X and leaves a marker that brings it back", async () => {
    const { unmount } = renderCard();
    fireEvent.click(await screen.findByLabelText("Dismiss this warning"));
    await waitFor(() => expect(screen.queryByText(/no longer offers/)).toBeNull());

    const badge = screen.getByLabelText(/no longer offers gone:8b/);
    fireEvent.click(badge);
    expect(await screen.findByText(/no longer offers gone:8b/)).toBeTruthy();

    // Close it again and prove the dismissal SURVIVES a remount: an in-memory
    // one would come straight back on the next card open, which is the whole
    // complaint the X exists to answer.
    fireEvent.click(screen.getByLabelText("Dismiss this warning"));
    await waitFor(() => expect(screen.queryByText(/no longer offers/)).toBeNull());
    unmount();
    renderCard();
    await screen.findByLabelText("Change default model");
    expect(screen.queryByText(/no longer offers/)).toBeNull();
    expect(await screen.findByLabelText(/no longer offers gone:8b/)).toBeTruthy();
  });

  it("a dismissal does not cover a warning about a different model", async () => {
    // Dismissing is acknowledging one statement about one model, not silencing
    // the category: a NEW broken model has to speak up on its own.
    const { unmount } = renderCard();
    fireEvent.click(await screen.findByLabelText("Dismiss this warning"));
    await waitFor(() => expect(screen.queryByText(/no longer offers/)).toBeNull());
    unmount();

    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "ollama", name: "Ollama", model: "alsogone:8b" }],
    });
    renderCard();
    expect(await screen.findByText(/no longer offers alsogone:8b/)).toBeTruthy();
  });

  it("no marker when there is nothing wrong", async () => {
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "ollama", name: "Ollama", model: "here:8b" }],
    });
    renderCard();
    await screen.findByLabelText("Change default model");
    await waitFor(() => expect(getAgentCliModels).toHaveBeenCalled());
    expect(screen.queryByLabelText("Dismiss this warning")).toBeNull();
    expect(screen.queryByText(/no longer offers/)).toBeNull();
  });
});

// The probe is the only control on this card that spends money, which is why it
// has its own button and its own confirmation.
describe("AgentCliSettings capability probe", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "deepseek", name: "DeepSeek", model: "deepseek-v4-flash" }],
    });
    getAgentCliModels.mockResolvedValue({ models: ["deepseek-v4-flash"] });
    probeAgentCliCapabilities.mockResolvedValue({
      model: "deepseek-v4-flash", probed: { effort_levels: ["low", "high", "max"] },
      calls: 7, checked_at: "2026-08-04T00:00:00Z", effort_checkable: true, answered: true,
    });
  });

  it("asks before spending anything, and says so", async () => {
    renderCard();
    fireEvent.click(await screen.findByLabelText("Check options"));
    // The confirmation has to name the cost: it is the one thing that separates
    // this button from the free refresh beside it.
    expect(await screen.findByText(/uses your own API credit/)).toBeTruthy();
    expect(probeAgentCliCapabilities).not.toHaveBeenCalled();
  });

  it("cancelling spends nothing", async () => {
    renderCard();
    fireEvent.click(await screen.findByLabelText("Check options"));
    fireEvent.click(await screen.findByText("Cancel"));
    await waitFor(() => expect(screen.queryByText(/uses your own API credit/)).toBeNull());
    expect(probeAgentCliCapabilities).not.toHaveBeenCalled();
  });

  it("reports the levels it established and how many calls it took", async () => {
    renderCard();
    fireEvent.click(await screen.findByLabelText("Check options"));
    fireEvent.click(await screen.findByText("Check now"));
    await waitFor(() => expect(probeAgentCliCapabilities).toHaveBeenCalledWith("i1"));
    expect(await screen.findByText(/Thinking levels accepted: Low, High, Max/)).toBeTruthy();
    expect(screen.getByText(/7 requests/)).toBeTruthy();
  });

  it("says plainly when nothing could be established", async () => {
    // Not a failure: a provider that ignores unknown parameters cannot be asked
    // this way, and reporting that as an error would send someone debugging.
    probeAgentCliCapabilities.mockResolvedValue({
      model: "m", probed: {}, calls: 2, checked_at: "2026-08-04T00:00:00Z", effort_checkable: true, answered: true,
    });
    renderCard();
    fireEvent.click(await screen.findByLabelText("Check options"));
    fireEvent.click(await screen.findByText("Check now"));
    expect(await screen.findByText(/does not validate the options/)).toBeTruthy();
  });

  it("cannot be run on an account with no model chosen", async () => {
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "ollama", name: "Ollama", model: "" }],
    });
    renderCard();
    expect((await screen.findByLabelText("Check options") as HTMLButtonElement).disabled).toBe(true);
  });

  it("says there was nothing to check, not that the provider ignored us", async () => {
    // Live-found on a backend whose thinking control is a boolean flag: the probe
    // correctly skipped the effort stage, and the panel reported that as "this
    // provider does not validate the options", blaming it for ignoring a question
    // nobody had asked.
    probeAgentCliCapabilities.mockResolvedValue({
      model: "gemma", probed: {}, calls: 1, checked_at: "2026-08-04T00:00:00Z",
      effort_checkable: false, answered: true,
    });
    renderCard();
    fireEvent.click(await screen.findByLabelText("Check options"));
    fireEvent.click(await screen.findByText("Check now"));
    expect(await screen.findByText(/no levels to check/)).toBeTruthy();
    expect(screen.queryByText(/does not validate the options/)).toBeNull();
  });

  it.each([
    ["effort_levels as a bare string", { model: "m", probed: { effort_levels: "high" }, calls: 2, checked_at: "x", effort_checkable: true, answered: true }],
    ["effort_levels holding non-strings", { model: "m", probed: { effort_levels: [1, 2] }, calls: 2, checked_at: "x", effort_checkable: true, answered: true }],
    ["effort_levels as an object", { model: "m", probed: { effort_levels: { a: 1 } }, calls: 2, checked_at: "x", effort_checkable: true, answered: true }],
  ])("survives %s inside an otherwise well-formed response", async (_label, response) => {
    // A level deeper than the container check. `probed` IS an object here, so the
    // outer guard passes; a string has a truthy `.length` but no `.map`, so
    // cardResultText threw during render, which is the same unhandled render
    // error one nesting level down. A malformed levels list reads as "no levels
    // reported", which this card already renders, rather than as a failure.
    probeAgentCliCapabilities.mockResolvedValue(response);
    renderCard();
    fireEvent.click(await screen.findByLabelText("Check options"));
    fireEvent.click(await screen.findByText("Check now"));
    await waitFor(() => expect(probeAgentCliCapabilities).toHaveBeenCalledWith("i1"));
    // Rendered an outcome rather than crashing, and the card is still usable.
    expect(await screen.findByText(/Checked with 2 requests/)).toBeTruthy();
    expect(screen.getByLabelText("Check options")).toBeTruthy();
  });

  it.each([
    ["a 204, which the client returns as undefined", undefined],
    ["a success that did not parse as JSON", { error: "parse_error", message: "OK" }],
    ["a response with no probed block", { calls: 3, answered: true }],
    ["a null probed block", { probed: null, calls: 1, answered: true }],
  ])("survives %s", async (_label, response) => {
    // A response the return type does not admit must read as a failed check
    // rather than take the card down. The card result used to be assembled
    // INSIDE the setRefreshResult updater, which React runs during the next
    // render: the throw landed outside the try, so it was an unhandled render
    // error, not a caught failure. Assert what renders, because the suite
    // reported every assertion passing while vitest exited non-zero on it.
    probeAgentCliCapabilities.mockResolvedValue(response);
    renderCard();
    fireEvent.click(await screen.findByLabelText("Check options"));
    fireEvent.click(await screen.findByText("Check now"));
    await waitFor(() => expect(probeAgentCliCapabilities).toHaveBeenCalledWith("i1"));
    expect(await screen.findByText("Connection failed.")).toBeTruthy();
    // Still mounted and still offering the action.
    expect(screen.getByLabelText("Check options")).toBeTruthy();
  });
});

// Four labelled buttons plus a status line outgrew the card, so the actions
// became icons. An icon carries no name, which makes the accessible name the
// only name there is.
describe("AgentCliSettings account actions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    getAgentCliProviders.mockResolvedValue({
      instances: [{
        id: "i1", kind: "deepseek", name: "DeepSeek", model: "deepseek-v4-flash",
        capabilities_checked_at: "2026-08-04T00:00:00Z",
      }],
    });
    getAgentCliModels.mockResolvedValue({ models: ["deepseek-v4-flash"] });
  });

  it("every action has an accessible name, and the icon is never the label", async () => {
    renderCard();
    expect(await screen.findByLabelText("Change default model")).toBeTruthy();
    expect(screen.getByLabelText("Refresh models")).toBeTruthy();
    expect(screen.getByLabelText(/^Check options/)).toBeTruthy();
    // Naming the account matters here: with four identical squares, "Remove"
    // alone does not say what is about to be removed.
    expect(screen.getByLabelText("Remove DeepSeek")).toBeTruthy();
  });

  it("the last-checked time rides on the button it belongs to", async () => {
    // It used to be its own line, which is what made the card too busy.
    renderCard();
    const probe = await screen.findByLabelText(/^Check options \(last checked/);
    expect(probe.getAttribute("title")).toBe(probe.getAttribute("aria-label"));
    expect(screen.queryByText(/Capabilities last checked/)).toBeNull();
  });

  it("a running action renames itself rather than only spinning", async () => {
    let release: (v: unknown) => void = () => {};
    refreshAgentCliProvider.mockReturnValue(new Promise((r) => { release = r; }));
    renderCard();
    fireEvent.click(await screen.findByLabelText("Refresh models"));
    // An icon that merely spins announces nothing to a screen reader.
    expect(await screen.findByLabelText("Refreshing...")).toBeTruthy();
    release({ models: [], capabilities: {}, declared: false, checked_at: "x" });
  });

  it("checks the new model's options as part of adding an account", async () => {
    // The point of doing it here is that it matters BEFORE the first
    // conversation, not after one behaves oddly.
    createAgentCliProvider.mockResolvedValue({ instance: { id: "new1", kind: "claude", name: "Anthropic", model: "claude-opus-4-8" } });
    probeAgentCliCapabilities.mockResolvedValue({
      model: "claude-opus-4-8", probed: {}, calls: 2, checked_at: "x", effort_checkable: true, answered: true,
    });
    renderCard();
    fireEvent.click(await screen.findByText("Add new provider…"));
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "sk-test" } });
    fireEvent.click(screen.getByText("Validate"));
    fireEvent.click(await screen.findByText("Done"));
    await waitFor(() => expect(probeAgentCliCapabilities).toHaveBeenCalledWith("new1"));
  });

  it("adding without the check spends nothing", async () => {
    createAgentCliProvider.mockResolvedValue({ instance: { id: "new1", kind: "claude", name: "Anthropic", model: "claude-opus-4-8" } });
    renderCard();
    fireEvent.click(await screen.findByText("Add new provider…"));
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "sk-test" } });
    fireEvent.click(screen.getByText("Validate"));
    fireEvent.click(await screen.findByLabelText(/Check which options this model accepts/));
    fireEvent.click(screen.getByText("Done"));
    await waitFor(() => expect(createAgentCliProvider).toHaveBeenCalled());
    expect(probeAgentCliCapabilities).not.toHaveBeenCalled();
  });

  it("a failed check does not fail the account", async () => {
    // The account is already stored and working; the card can retry.
    createAgentCliProvider.mockResolvedValue({ instance: { id: "new1", kind: "claude", name: "Anthropic", model: "claude-opus-4-8" } });
    probeAgentCliCapabilities.mockRejectedValue(new Error("rate limited"));
    renderCard();
    fireEvent.click(await screen.findByText("Add new provider…"));
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "sk-test" } });
    fireEvent.click(screen.getByText("Validate"));
    fireEvent.click(await screen.findByText("Done"));
    await waitFor(() => expect(createAgentCliProvider).toHaveBeenCalled());
    expect(screen.queryByText("rate limited")).toBeNull();
  });
});

describe("AgentCliSettings probe could not run", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "openrouter", name: "OpenRouter", model: "openai/gpt-5" }],
    });
    getAgentCliModels.mockResolvedValue({ models: ["openai/gpt-5"] });
  });

  it("says the provider declined, not that it does not validate options", async () => {
    // Live-hit with a key that had no credit: every probe came back refused for
    // an ACCOUNT reason, and reporting that as a finding about the model blames
    // the provider for something it never got to answer.
    probeAgentCliCapabilities.mockResolvedValue({
      model: "openai/gpt-5", probed: {}, calls: 2, checked_at: "x",
      effort_checkable: false, answered: false,
    });
    renderCard();
    fireEvent.click(await screen.findByLabelText(/^Check options/));
    fireEvent.click(await screen.findByText("Check now"));
    expect(await screen.findByText(/declined every one/)).toBeTruthy();
    expect(screen.queryByText(/no levels to check/)).toBeNull();
  });

  it("offers the check when the default model is changed", async () => {
    // A different model has different options, so the offer belongs here too.
    setAgentCliProviderModel.mockResolvedValue({ instance: { id: "i1", model: "openai/gpt-5-mini" } });
    probeAgentCliCapabilities.mockResolvedValue({
      model: "openai/gpt-5-mini", probed: {}, calls: 2, checked_at: "x",
      effort_checkable: false, answered: true,
    });
    getAgentCliModels.mockResolvedValue({ models: ["openai/gpt-5", "openai/gpt-5-mini"] });
    renderCard();
    fireEvent.click(await screen.findByLabelText("Change default model"));
    fireEvent.change(await screen.findByLabelText("Select default model:"),
                     { target: { value: "openai/gpt-5-mini" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(probeAgentCliCapabilities).toHaveBeenCalledWith("i1"));
  });

  it("changing the model without the check spends nothing", async () => {
    setAgentCliProviderModel.mockResolvedValue({ instance: { id: "i1", model: "openai/gpt-5-mini" } });
    getAgentCliModels.mockResolvedValue({ models: ["openai/gpt-5", "openai/gpt-5-mini"] });
    renderCard();
    fireEvent.click(await screen.findByLabelText("Change default model"));
    fireEvent.click(await screen.findByLabelText(/Check which options this model accepts/));
    fireEvent.change(await screen.findByLabelText("Select default model:"),
                     { target: { value: "openai/gpt-5-mini" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(setAgentCliProviderModel).toHaveBeenCalled());
    expect(probeAgentCliCapabilities).not.toHaveBeenCalled();
  });
});

// Whoever asked for the check gets the answer. Running it silently on the add
// and save paths meant paying for an answer and not being shown it.
describe("AgentCliSettings reports every check", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "openrouter", name: "OpenRouter", model: "a/one" }],
    });
    getAgentCliModels.mockResolvedValue({ models: ["a/one", "a/two"] });
    setAgentCliProviderModel.mockResolvedValue({ instance: { id: "i1", model: "a/two" } });
    probeAgentCliCapabilities.mockResolvedValue({
      model: "a/two", probed: {}, calls: 1, checked_at: "x",
      effort_checkable: false, answered: true,
    });
  });

  it("shows the result after changing the default model", async () => {
    renderCard();
    fireEvent.click(await screen.findByLabelText("Change default model"));
    fireEvent.change(await screen.findByLabelText("Select default model:"), { target: { value: "a/two" } });
    fireEvent.click(screen.getByText("Save"));
    expect(await screen.findByText(/no levels to check/)).toBeTruthy();
  });

  it("shows the result after adding an account", async () => {
    const added = { id: "new1", kind: "claude", name: "Anthropic", model: "claude-opus-4-8" };
    createAgentCliProvider.mockResolvedValue({ instance: added });
    // The reload after Done returns the new account, as the server would; the
    // result has to land on ITS card, which only exists once it is listed.
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "openrouter", name: "OpenRouter", model: "a/one" }, added],
    });
    renderCard();
    fireEvent.click(await screen.findByText("Add new provider…"));
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "sk-test" } });
    fireEvent.click(screen.getByText("Validate"));
    fireEvent.click(await screen.findByText("Done"));
    expect(await screen.findByText(/no levels to check/)).toBeTruthy();
  });

  it("a failed check is reported rather than swallowed", async () => {
    probeAgentCliCapabilities.mockRejectedValue(new Error("rate limited"));
    renderCard();
    fireEvent.click(await screen.findByLabelText("Change default model"));
    fireEvent.change(await screen.findByLabelText("Select default model:"), { target: { value: "a/two" } });
    fireEvent.click(screen.getByText("Save"));
    // Reported, but the model change still stands: the two are separate.
    expect(await screen.findByText("rate limited")).toBeTruthy();
    await waitFor(() => expect(setAgentCliProviderModel).toHaveBeenCalled());
  });
});

// A check that writes new capabilities has to announce them: both chat-window
// hosts hold accounts as state and reload on this event.
describe("AgentCliSettings announces capability changes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    getAgentCliProviders.mockResolvedValue({
      instances: [{ id: "i1", kind: "openrouter", name: "OpenRouter", model: "a/one" }],
    });
    getAgentCliModels.mockResolvedValue({ models: ["a/one"] });
    probeAgentCliCapabilities.mockResolvedValue({
      model: "a/one", probed: { effort_levels: ["low", "high"] }, calls: 7,
      checked_at: "x", effort_checkable: true, answered: true,
    });
  });

  it("fires providers-changed after a manual check", async () => {
    const seen = vi.fn();
    window.addEventListener("phx-agentcli-providers-changed", seen);
    renderCard();
    fireEvent.click(await screen.findByLabelText(/^Check options/));
    fireEvent.click(await screen.findByText("Check now"));
    // Without this the chat window keeps its old accounts, so a Thinking control
    // the check just established never appears and the check looks broken.
    await waitFor(() => expect(seen).toHaveBeenCalled());
    window.removeEventListener("phx-agentcli-providers-changed", seen);
  });
});
