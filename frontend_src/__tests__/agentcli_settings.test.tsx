import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";

const getAgentCliProviders = vi.fn();
const probeAgentCliProvider = vi.fn();
const createAgentCliProvider = vi.fn();
const deleteAgentCliProvider = vi.fn();

vi.mock("../api", () => ({
  api: {
    getAgentCliProviders: (...a: unknown[]) => getAgentCliProviders(...a),
    probeAgentCliProvider: (...a: unknown[]) => probeAgentCliProvider(...a),
    createAgentCliProvider: (...a: unknown[]) => createAgentCliProvider(...a),
    deleteAgentCliProvider: (...a: unknown[]) => deleteAgentCliProvider(...a),
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
    fireEvent.click(screen.getByText("Remove"));
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
    fireEvent.click(screen.getByText("Remove"));
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
