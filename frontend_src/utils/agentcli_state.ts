// Persistence for the agentCLI floating window so it survives navigating away
// from the panel and back, and closing/reopening. Two tiers:
//   - Durable (localStorage): open flag, geometry, and the token/provider/model/
//     options selections. Restored after a full page reload too.
//   - Session (module variable): the transcript. Survives a panel remount within
//     the same page load, but clears on a full reload (the conversation is
//     intentionally ephemeral).

export interface AgentCliOptions {
  thinking: boolean;
  effort: string;
  temperature: string;
  verbose: boolean;
}

export interface AgentCliDurable {
  open: boolean;
  minimized: boolean;
  // null = the user never chose: the window resolves a default at mount
  // (full screen on a narrow/mobile viewport, windowed on desktop).
  maximized: boolean | null;
  pos: { x: number; y: number };
  pillPos: { x: number; y: number };
  size: { w: number; h: number };
  tokenId: string;
  instanceId: string;
  model: string;
  options: AgentCliOptions;
  // One-shot text seeded into the message box on the next window mount (used by
  // the onboarding wizard's test prompt); the window clears it after reading.
  prefill: string;
  // Token-usage footer visibility (gear toggle). Default on.
  showUsage: boolean;
  // Per-message timestamps in the transcript (gear toggle). Default off.
  showTimestamps: boolean;
}

const LS_KEY = "phx-agentcli";

export function defaultDurable(): AgentCliDurable {
  return {
    open: false,
    minimized: false,
    maximized: null,
    pos: { x: -1, y: -1 }, // -1 => "unset", the window computes a default
    pillPos: { x: -1, y: -1 }, // -1 => "unset", seeded from the window on minimize
    size: { w: 440, h: 560 },
    tokenId: "",
    instanceId: "",
    model: "",
    options: { thinking: true, effort: "high", temperature: "", verbose: false },
    prefill: "",
    showUsage: true,
    showTimestamps: false,
  };
}

export function getDurable(): AgentCliDurable {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return defaultDurable();
    const p = JSON.parse(raw) as Partial<AgentCliDurable>;
    const d = defaultDurable();
    return {
      ...d, ...p,
      pos: { ...d.pos, ...(p.pos || {}) },
      pillPos: { ...d.pillPos, ...(p.pillPos || {}) },
      size: { ...d.size, ...(p.size || {}) },
      options: { ...d.options, ...(p.options || {}) },
    };
  } catch {
    return defaultDurable();
  }
}

/**
 * Build the durable-state change for opening Agent Chat.
 *
 * Button-driven opens pass a viewport so the window is summoned in the center.
 * Shortcut-driven reopens omit it, leaving the user's last dragged position
 * untouched while still restoring a minimized window to its full form.
 */
export function agentCliOpenPatch(
  durable: AgentCliDurable,
  tokenId?: string,
  viewport?: { w: number; h: number },
): Partial<AgentCliDurable> {
  const patch: Partial<AgentCliDurable> = {
    ...(tokenId ? { tokenId } : {}),
    open: true,
    minimized: false,
  };
  if (viewport) {
    patch.pos = {
      x: Math.max(8, Math.round((viewport.w - durable.size.w) / 2)),
      y: Math.max(8, Math.round((viewport.h - durable.size.h) / 2)),
    };
  }
  return patch;
}

export function patchDurable(patch: Partial<AgentCliDurable>): void {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({ ...getDurable(), ...patch }));
  } catch {
    /* storage blocked: window state simply won't persist */
  }
}

// ---------------------------------------------------------------------------
// Conversation tier: the transcript, the unsent message box, and the usage
// counters. Persisted so the conversation resumes after a full page reload,
// not just after the panel unmounts (which happens whenever the user navigates
// away from the Phoenix MCP panel in Home Assistant).
//
// Kept under its OWN storage key, separate from the window state above, for
// two reasons: it is the only part that can grow without bound, and if it ever
// has to be dropped (quota, corruption) the window geometry and the token /
// provider / model selections must survive independently.
//
// This does mean conversation content, including tool results, is written to
// browser localStorage on an admin-only page. It is bounded by the chat-memory
// setting and by MAX_CONV_BYTES below.
// ---------------------------------------------------------------------------

const CONV_KEY = "phx-agentcli-conversation";

// localStorage is ~5MB per origin and shared with the window state and every
// other Phoenix MCP key. Cap the conversation well under that: on overflow the
// oldest turns are dropped until it fits, which mirrors what the scrollback
// limit already does to the visible transcript.
const MAX_CONV_BYTES = 2_000_000;

interface Conversation {
  turns: unknown[];
  draft: string;
  usage: SessionUsage;
}

function sanitizeForPersistence(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sanitizeForPersistence);
  if (!value || typeof value !== "object") return value;
  const record = value as Record<string, unknown>;

  // Camera image bytes are intentionally session-only. Keep a placeholder
  // entry for the transcript, but never put the data URL or provider base64
  // into localStorage.
  if (record.kind === "tool_image") {
    return {
      kind: "tool_image",
      id: record.id,
      name: record.name,
      mimeType: record.mimeType,
      alt: record.alt,
      unavailable: true,
    };
  }
  if (record.type === "image") {
    return { type: "text", text: "[camera image unavailable after reload]" };
  }
  if (record.type === "image_url") {
    return { type: "text", text: "[camera image unavailable after reload]" };
  }

  return Object.fromEntries(
    Object.entries(record).map(([key, child]) => [key, sanitizeForPersistence(child)]),
  );
}

function defaultConversation(): Conversation {
  return { turns: [], draft: "", usage: { input: 0, output: 0, context: 0, noData: false } };
}

let conversation: Conversation = defaultConversation();
let loaded = false;

function load(): Conversation {
  if (loaded) return conversation;
  loaded = true;
  try {
    const raw = localStorage.getItem(CONV_KEY);
    if (raw) {
      const p = JSON.parse(raw) as Partial<Conversation>;
      const d = defaultConversation();
      conversation = {
        turns: Array.isArray(p.turns) ? p.turns : d.turns,
        draft: typeof p.draft === "string" ? p.draft : d.draft,
        usage: { ...d.usage, ...(p.usage || {}) },
      };
    }
  } catch {
    // Corrupt or unreadable: start clean rather than breaking the window.
    conversation = defaultConversation();
  }
  return conversation;
}

function save(): void {
  try {
    // Drop oldest turns until the payload fits. A single turn can be large on
    // its own (a tool result with many entities), so give up rather than loop
    // forever if even one turn will not fit.
    let turns = conversation.turns;
    for (;;) {
      const payload = JSON.stringify({
        ...conversation,
        turns: sanitizeForPersistence(turns),
      });
      if (payload.length <= MAX_CONV_BYTES) {
        localStorage.setItem(CONV_KEY, payload);
        return;
      }
      if (turns.length === 0) {
        // Keep the draft and usage even when no transcript fits.
        localStorage.setItem(CONV_KEY, JSON.stringify({ ...conversation, turns: [] }));
        return;
      }
      turns = turns.slice(1);
    }
  } catch {
    /* storage blocked or quota exceeded: the conversation stays in memory only */
  }
}

export function getSessionTurns(): unknown[] {
  return load().turns;
}
export function setSessionTurns(turns: unknown[]): void {
  load().turns = turns;
  save();
}

// The unsent message box. Cleared on send and on /clear, because the window
// sets the box to "" on both paths.
export function getSessionDraft(): string {
  return load().draft;
}
export function setSessionDraft(draft: string): void {
  load().draft = draft;
  save();
}

// Token usage, provider-reported. Same lifecycle as the transcript it belongs
// to, so the footer totals stay coherent with the restored conversation.
export interface SessionUsage {
  input: number;   // prompt tokens across all model calls this conversation
  output: number;  // completion tokens across all model calls
  context: number; // input tokens of the newest model call (current context size)
  // True once a turn completed without the provider reporting any usage, so
  // the footer can say so instead of showing a forever-zero counter. Cleared
  // the moment usage arrives (e.g. after switching models).
  noData: boolean;
}
export function getSessionUsage(): SessionUsage {
  return load().usage;
}
export function setSessionUsage(usage: SessionUsage): void {
  load().usage = usage;
  save();
}

// Test helper: simulate a full page reload. Drops the in-memory cache but
// leaves localStorage alone, so the next read comes back off disk exactly as
// it would after F5.
export function __reloadFromStorage(): void {
  conversation = defaultConversation();
  loaded = false;
}

// Test helper: reset both tiers between tests.
export function __resetAgentCliState(): void {
  conversation = defaultConversation();
  loaded = true;
  try {
    localStorage.removeItem(CONV_KEY);
    localStorage.removeItem(LS_KEY);
  } catch {
    /* ignore */
  }
}
