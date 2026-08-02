import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { agentCliChat, api } from "../api";
import { renderMarkdown, flagsUnsafeContent } from "../utils/markdown";
import {
  getDurable, patchDurable, getSessionTurns, setSessionTurns,
  getSessionDraft, setSessionDraft,
  getSessionUsage, setSessionUsage, type SessionUsage,
} from "../utils/agentcli_state";
import { approvalStatusLabel } from "../utils";
import { clearReasonDraft, getReasonDraft } from "../utils/approval_reason_draft";
import { subscribeApprovalEvents } from "../utils/approval_events";
import PHOENIX_ICON from "../../custom_components/phoenix_mcp/brand/icon.png";
import type {
  AgentCliProviderKind,
  AgentCliInstance,
  TokenRecord,
} from "../types";
import { hasMessage, localeClock, localeCompactNumber, localeNumber, t } from "../i18n";
import { tRich } from "../i18n/rich";

// One rendered item in the transcript. ts (epoch ms, stamped at creation) is
// optional: restored session entries from before the field existed lack it,
// and the timestamp display simply skips those.
type ChatEntry =
  | { kind: "user"; text: string; ts?: number }
  | { kind: "assistant"; text: string; thinking: string; ts?: number }
  | { kind: "tool_call"; id: string; name: string; args: unknown }
  | { kind: "tool_result"; id: string; name: string; isError: boolean; summary: string }
  | { kind: "progress"; id: string; message: string; activity?: boolean }
  | { kind: "approval"; approvalId: string; toolName: string; reviewUrl?: string; status: string; reason?: string }
  | { kind: "notice"; code?: string; message: string }
  | { kind: "error"; code: string; message: string };

// A completed turn: its entries plus the provider-format messages it produced.
export interface Turn {
  entries: ChatEntry[];
  messages: unknown[];
  lines: number;
}

interface GenOptions {
  thinking: boolean;
  effort: string;
  temperature: string;
  verbose: boolean;
}

// A single "Working: <tool>" line shown while a turn runs, so an operator with
// verbose off can tell working from hung. It is a live-only indicator: it moves
// to the bottom on each new tool call and is dropped when the turn is folded
// into the transcript, so the history keeps the reply rather than a trail of
// half-sentences about how it was produced.
const ACTIVITY_ID = "__activity__";

function isActivity(e: ChatEntry): boolean {
  return e.kind === "progress" && e.id === ACTIVITY_ID;
}

function stripActivity(entries: ChatEntry[]): ChatEntry[] {
  return entries.filter((e) => !isActivity(e));
}

function countLines(entries: ChatEntry[]): number {
  let n = 0;
  for (const e of entries) {
    if (e.kind === "user" || e.kind === "assistant") {
      const txt = e.kind === "assistant" ? e.text : e.text;
      n += 1 + (txt.match(/\n/g)?.length ?? 0);
    } else {
      n += 1;
    }
  }
  return n;
}

export function trimTurns(turns: Turn[], scrollback: number): Turn[] {
  if (scrollback <= 0) return turns.slice(-1);
  const kept: Turn[] = [];
  let total = 0;
  for (let i = turns.length - 1; i >= 0; i--) {
    total += turns[i].lines;
    if (total > scrollback && kept.length > 0) break;
    kept.unshift(turns[i]);
  }
  return kept;
}

export function memoryMessages(turns: Turn[], scrollback: number): unknown[] {
  if (scrollback <= 0) return [];
  return turns.flatMap((turn) => turn.messages);
}

// Wall-clock label for a transcript entry, in the viewer's locale convention.
function fmtClock(ts: number): string {
  return localeClock(ts);
}

// Compact token counts for the usage footer. Was a hand-rolled 48.2k/1.2M
// ladder, which is not translatable: Chinese groups by 10^4, so the right
// answer is 4.8万, not a translated "k". CLDR owns both the scale and the
// suffix, so this defers to it entirely.
export function fmtTokens(n: number): string {
  return localeCompactNumber(n);
}

// One entry in a provider's Thinking dropdown.
export interface ThinkOption { value: string; label: string }

// The thinking/effort options a given provider+model actually exposes at the
// API level, so the gear shows the real levels for each backend rather than a
// generic scale. `style` says how a selected value maps to request options:
//   "effort"  -> the value is an effort level (or "off"); Claude/DeepSeek/OpenAI
//   "boolean" -> the value is on/off only; Ollama's `think` flag
export interface ModelCaps {
  thinking: ThinkOption[];   // dropdown entries; [] => no thinking control
  style: "effort" | "boolean";
  defaultLevel: string;      // level used when the saved effort is not in the list
  temperature: boolean;      // temperature applies (given the current thinking state)
  note?: string;             // shown when thinking is intrinsic and not selectable
}

const LEVEL_LABEL: Record<string, string> = {
  off: "agentchat.levelOff", none: "agentchat.levelOff", on: "agentchat.levelOn",
  minimal: "agentchat.levelMinimal", low: "agentchat.levelLow",
  medium: "agentchat.levelMedium", high: "agentchat.levelHigh",
  xhigh: "agentchat.levelXHigh", max: "agentchat.levelMax",
};
function thinkOpts(values: string[]): ThinkOption[] {
  return values.map((v) => ({ value: v, label: LEVEL_LABEL[v] ? t(LEVEL_LABEL[v]) : v }));
}

export function modelCaps(kind: AgentCliProviderKind, model: string, thinkingOn: boolean): ModelCaps {
  const m = (model || "").toLowerCase();
  switch (kind) {
    case "claude":
      // Anthropic output_config.effort. Opus does not take temperature.
      return { thinking: thinkOpts(["off", "low", "medium", "high", "xhigh", "max"]),
               style: "effort", defaultLevel: "high", temperature: false };
    case "deepseek": {
      // deepseek-reasoner reasons intrinsically (being retired). Newer models take
      // a thinking toggle; reasoning_effort accepts only high/max (low/medium map
      // to high, xhigh to max), and thinking mode ignores temperature.
      if (m.includes("reasoner"))
        return { thinking: [], style: "effort", defaultLevel: "high", temperature: false,
                 note: t("agentchat.deepseekReasonerNote") };
      return { thinking: thinkOpts(["off", "high", "max"]), style: "effort",
               defaultLevel: "high", temperature: !thinkingOn };
    }
    case "chatgpt": {
      // OpenAI reasoning_effort (Chat Completions). gpt-5 supports none/minimal/
      // low/medium/high (none = reasoning off); o-series is low/medium/high with
      // no off. Reasoning models reject a custom temperature; plain gpt-* models
      // have no thinking control and take temperature.
      if (m.startsWith("gpt-5"))
        return { thinking: thinkOpts(["none", "minimal", "low", "medium", "high"]), style: "effort",
                 defaultLevel: "medium", temperature: false };
      if (/^o\d/.test(m))
        return { thinking: thinkOpts(["low", "medium", "high"]), style: "effort",
                 defaultLevel: "medium", temperature: false };
      return { thinking: [], style: "effort", defaultLevel: "high", temperature: true };
    }
    case "gemini": {
      // Gemini via Google's OpenAI-compatible endpoint: reasoning_effort maps to
      // Gemini's thinking_level (minimal/low/medium/high, default medium). Google
      // strongly recommends NOT setting temperature on its reasoning models, so no
      // temperature control is offered. Thinking-capable models are 2.5 and 3.x.
      if (m.includes("2.5") || m.includes("3.5") || m.includes("gemini-3"))
        return { thinking: thinkOpts(["minimal", "low", "medium", "high"]), style: "effort",
                 defaultLevel: "medium", temperature: false };
      return { thinking: [], style: "effort", defaultLevel: "medium", temperature: false };
    }
    case "grok": {
      // xAI reasoning_effort on chat completions. grok-3-mini and the grok-4
      // reasoning models take low/high (newer models also accept medium, but
      // off/low/high is the safe common set); grok-4 reasons even with none set.
      // Sending an effort marks a reasoning turn, which drops temperature.
      return { thinking: thinkOpts(["off", "low", "high"]), style: "effort",
               defaultLevel: "low", temperature: !thinkingOn };
    }
    case "kimi": {
      // Kimi splits the control by model family. K3 takes reasoning_effort
      // (low/high/max) and always reasons, so there is no off. K2.x has no effort
      // levels but takes a thinking on/off toggle. Neither accepts temperature;
      // only the legacy moonshot-v1 models do, and they have no thinking control.
      if (m.startsWith("kimi-k3"))
        return { thinking: thinkOpts(["low", "high", "max"]), style: "effort",
                 defaultLevel: "high", temperature: false };
      if (m.startsWith("kimi"))
        return { thinking: thinkOpts(["off", "on"]), style: "boolean",
                 defaultLevel: "on", temperature: false };
      return { thinking: [], style: "effort", defaultLevel: "high", temperature: true };
    }
    case "meta":
      // Meta's Model API reasoning_effort. Muse Spark always reasons (it rejects
      // "none" with a 400), so no off is offered, and "max" is not one of Meta's
      // levels. Meta tunes the model for its default temperature, so temperature
      // is not offered either.
      return { thinking: thinkOpts(["minimal", "low", "medium", "high", "xhigh"]),
               style: "effort", defaultLevel: "medium", temperature: false };
    case "minimax":
      // MiniMax via its Anthropic-compatible API: thinking is a plain on/off
      // adaptive toggle (no effort levels, no custom temperature). M2 models
      // always reason; turning thinking off there is a no-op on the model side.
      return { thinking: thinkOpts(["off", "on"]), style: "boolean", defaultLevel: "on", temperature: false };
    case "ollama":
    case "ollama_cloud":
      // Ollama's `think` is a boolean for the general case; cloud is the same
      // wire format, just hosted.
      return { thinking: thinkOpts(["off", "on"]), style: "boolean", defaultLevel: "on", temperature: true };
    case "openrouter":
    case "nvidia":
      // OpenRouter and NVIDIA both front many vendors' models behind one key; a
      // single thinking control can't fit them all, so no thinking level is
      // exposed (reasoning models still reason at their own default) and
      // temperature is offered.
      return { thinking: [], style: "effort", defaultLevel: "high", temperature: true };
    default:
      return { thinking: [], style: "effort", defaultLevel: "high", temperature: false };
  }
}

function hasOff(caps: ModelCaps): boolean {
  return caps.thinking.some((opt) => opt.value === "off");
}

// The dropdown value for the current options.
export function thinkValue(caps: ModelCaps, o: GenOptions): string {
  if (caps.style === "boolean") return o.thinking ? "on" : "off";
  if (hasOff(caps) && !o.thinking) return "off";
  const levels = caps.thinking.filter((opt) => opt.value !== "off").map((opt) => opt.value);
  return levels.includes(o.effort) ? o.effort : caps.defaultLevel;
}

// Fold a chosen dropdown value back into the thinking/effort options.
export function applyThink(
  caps: ModelCaps, v: string, set: (fn: (o: GenOptions) => GenOptions) => void,
): void {
  if (caps.style === "boolean") { set((o) => ({ ...o, thinking: v === "on" })); return; }
  if (v === "off") { set((o) => ({ ...o, thinking: false })); return; }
  set((o) => ({ ...o, thinking: true, effort: v }));
}

export function buildOptions(
  kind: AgentCliProviderKind, o: GenOptions, caps: ModelCaps,
): Record<string, unknown> {
  if (kind === "claude") return { thinking: o.thinking, effort: o.effort, show_thinking: o.verbose };
  const out: Record<string, unknown> = { show_thinking: o.verbose };
  if (caps.thinking.length) {
    if (caps.style === "boolean") {
      out.thinking = o.thinking;
    } else if (hasOff(caps)) {
      // DeepSeek: an on/off toggle plus a reasoning_effort level when on.
      out.thinking = o.thinking;
      if (o.thinking) out.effort = thinkValue(caps, o);
    } else {
      // OpenAI reasoning models: always on, effort only.
      out.effort = thinkValue(caps, o);
    }
  }
  if (caps.temperature && o.temperature.trim() !== "") {
    const temp = Number(o.temperature);
    if (!Number.isNaN(temp)) out.temperature = temp;
  }
  return out;
}

interface Props {
  tokens: TokenRecord[];
  instances: AgentCliInstance[];
  scrollbackLines: number;
  initialTokenId: string;
  onClose: () => void;
}

const RESIZE_MIN_W = 320, RESIZE_MIN_H = 280;
// The minimized pill's footprint (CSS: 220px wide, ~44px tall titlebar), used to
// keep a remembered pill position within the viewport.
const PILL_SIZE = { w: 220, h: 44 };

// The window is normal (windowed), minimized to a pill, or maximized full screen.
type WinMode = "normal" | "min" | "max";

// Same breakpoint as the panel's mobile tab-bar split. Guarded because jsdom
// (the test environment) has no matchMedia.
function isNarrowViewport(): boolean {
  return typeof window.matchMedia === "function"
    && window.matchMedia("(max-width: 700px)").matches;
}

/** Compute a resized window rect, snapping the edge(s) being dragged to the
 *  viewport margin exactly as the drag handler snaps the whole window. The
 *  `<= SNAP` test is true both when an edge nears the boundary (snap) and when
 *  it has crossed it (clamp), so a resize can never push an edge off the page.
 *  Pure, so it is unit-tested directly. */
export function snapResizeRect(
  dir: string,
  o: { x: number; y: number; w: number; h: number },
  dx: number, dy: number, vw: number, vh: number,
): { x: number; y: number; w: number; h: number } {
  const M = 8, SNAP = 56;
  let w = o.w, h = o.h, x = o.x, y = o.y;
  const rightAnchor = o.x + o.w;   // fixed edge when dragging west
  const bottomAnchor = o.y + o.h;  // fixed edge when dragging north
  if (dir.includes("e")) {
    w = Math.max(RESIZE_MIN_W, o.w + dx);
    if (vw - (o.x + w) <= SNAP) w = Math.max(RESIZE_MIN_W, vw - M - o.x);
  }
  if (dir.includes("s")) {
    h = Math.max(RESIZE_MIN_H, o.h + dy);
    if (vh - (o.y + h) <= SNAP) h = Math.max(RESIZE_MIN_H, vh - M - o.y);
  }
  if (dir.includes("w")) {
    w = Math.max(RESIZE_MIN_W, o.w - dx);
    x = rightAnchor - w;
    if (x <= SNAP) { w = Math.max(RESIZE_MIN_W, rightAnchor - M); x = rightAnchor - w; }
  }
  if (dir.includes("n")) {
    h = Math.max(RESIZE_MIN_H, o.h - dy);
    y = bottomAnchor - h;
    if (y <= SNAP) { h = Math.max(RESIZE_MIN_H, bottomAnchor - M); y = bottomAnchor - h; }
  }
  return { x, y, w, h };
}

/** Clamp a window's top-left so a window of the given size sits fully within the
 *  viewport (margin M), pinning to the margin when it is larger than the view.
 *  Used on restore: the minimized pill may have been dragged somewhere a full
 *  size window would overflow, and it must not unfold off-screen. Pure, so it is
 *  unit-tested directly. */
export function clampPosToViewport(
  pos: { x: number; y: number },
  size: { w: number; h: number },
  vw: number, vh: number,
): { x: number; y: number } {
  const M = 8;
  return {
    x: Math.min(Math.max(M, pos.x), Math.max(M, vw - size.w - M)),
    y: Math.min(Math.max(M, pos.y), Math.max(M, vh - size.h - M)),
  };
}

export function AgentCliWindow({
  tokens, instances, scrollbackLines, initialTokenId, onClose,
}: Props) {
  // Restore persisted window state (survives navigation away/back and reopen).
  const saved = useRef(getDurable()).current;
  const firstInstance = (instances.some((i) => i.id === saved.instanceId) && saved.instanceId)
    || instances[0]?.id || "";

  const [tokenId, setTokenId] = useState(saved.tokenId || initialTokenId || tokens[0]?.id || "");
  const [instanceId, setInstanceId] = useState<string>(firstInstance);
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState<string>(saved.model);
  const [turns, setTurns] = useState<Turn[]>(() => getSessionTurns() as Turn[]);
  const [live, setLive] = useState<ChatEntry[]>([]);
  // Seeded from the session draft so a half-written prompt survives the panel
  // unmounting (navigating away in HA), the same way the transcript does. The
  // wizard's one-shot prefill still wins when present.
  const [input, setInput] = useState(saved.prefill || getSessionDraft());
  const [sending, setSending] = useState(false);
  // Non-null when the last turn paused at the round-cap checkpoint (its value is
  // the round count reached); drives the Continue/Stop control.
  const [pendingContinue, setPendingContinue] = useState<number | null>(null);
  const [mode, setMode] = useState<WinMode>(() => {
    if (saved.minimized) return "min";
    // No stored maximize preference: mobile defaults to full screen.
    if (saved.maximized ?? isNarrowViewport()) return "max";
    return "normal";
  });
  const minimized = mode === "min";
  const maximized = mode === "max";
  const [gearOpen, setGearOpen] = useState(false);
  const [options, setOptions] = useState<GenOptions>(saved.options);
  const [showUsage, setShowUsage] = useState(saved.showUsage);
  const [showTimestamps, setShowTimestamps] = useState(saved.showTimestamps);
  // Provider-reported token usage for this conversation (session lifecycle,
  // like the transcript). The stream's usage events carry TURN-cumulative
  // totals, so each event is added to the session base captured at send time.
  const [usage, setUsage] = useState<SessionUsage>(() => getSessionUsage());
  // Screen-reader announcement of discrete chat events (completed replies,
  // approvals, errors). Deliberately NOT the streaming transcript itself:
  // a live region over token-by-token deltas would announce every chunk.
  const [announcement, setAnnouncement] = useState("");

  const [pos, setPos] = useState(() =>
    saved.pos.x >= 0 ? saved.pos : { x: Math.max(16, window.innerWidth - 480), y: 96 });
  const [pillPos, setPillPos] = useState(saved.pillPos);
  const [size, setSize] = useState(saved.size);

  const rootRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const cancelledRef = useRef(false);
  const lastQueryRef = useRef("");
  const liveRef = useRef<ChatEntry[]>([]);
  liveRef.current = live;
  const usageRef = useRef(usage);
  usageRef.current = usage;
  // Session totals at the moment the current turn started; the turn's
  // cumulative usage events are added on top of this base.
  const usageBaseRef = useRef({ input: 0, output: 0 });
  // Whether any usage event arrived during the current turn: a turn that
  // completes without one (and with nothing counted yet) marks the footer as
  // "this provider reports no usage" instead of a forever-zero counter.
  const turnSawUsageRef = useRef(false);
  // Skip the conversation-reset effect on the initial mount so a restored
  // transcript is not wiped when the window reopens.
  const firstRun = useRef(true);

  const instance = useMemo(() => instances.find((i) => i.id === instanceId), [instances, instanceId]);
  const kind: AgentCliProviderKind = instance?.kind ?? "claude";
  const instanceModel = instance?.model ?? "";
  const caps = useMemo(() => modelCaps(kind, model, options.thinking), [kind, model, options.thinking]);

  // Persist the selections, options, and geometry so they survive reopen/reload.
  useEffect(() => { patchDurable({ tokenId }); }, [tokenId]);
  useEffect(() => { patchDurable({ instanceId }); }, [instanceId]);
  useEffect(() => { patchDurable({ model }); }, [model]);
  useEffect(() => { patchDurable({ options }); }, [options]);
  useEffect(() => { patchDurable({ showUsage }); }, [showUsage]);
  useEffect(() => { patchDurable({ showTimestamps }); }, [showTimestamps]);
  useEffect(() => { patchDurable({ minimized, maximized }); }, [minimized, maximized]);
  // The prefill is one-shot: consumed into the input above, then cleared so it
  // does not reappear on a later open.
  useEffect(() => { if (saved.prefill) patchDurable({ prefill: "" }); }, [saved.prefill]);
  // The transcript is session-only (survives remount within a page load).
  useEffect(() => { setSessionTurns(turns); }, [turns]);
  // Mirror the unsent message box into the same session tier. Done as an effect
  // rather than at each setInput call site so every path stays in sync: typing,
  // send and /clear (which set it to ""), and the cancel path that restores the
  // last query.
  useEffect(() => { setSessionDraft(input); }, [input]);

  // Load the model list whenever the account changes (including on mount, to
  // restore/refresh the list). Keeps the restored model if still valid.
  useEffect(() => {
    if (!instanceId) { setModels([]); setModel(""); return; }
    let cancelled = false;
    api.getAgentCliModels(instanceId)
      .then((r) => {
        if (cancelled) return;
        setModels(r.models);
        setModel((m) => (r.models.includes(m) ? m : (instanceModel || r.models[0] || "")));
      })
      .catch(() => { if (!cancelled) setModels([]); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instanceId]);

  const resetUsage = useCallback(() => {
    const zero = { input: 0, output: 0, context: 0, noData: false };
    setSessionUsage(zero);
    setUsage(zero);
  }, []);

  // Reset the conversation when the token or account changes (tool scope /
  // message format differ), but NOT on the initial restore.
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return; }
    setTurns([]);
    setLive([]);
    resetUsage();
  }, [tokenId, instanceId, resetUsage]);

  // If an account is added or removed while the window is open, keep the
  // selection valid without a page reload. When the last account is removed,
  // fall back to "" so the model list and selection clear too.
  useEffect(() => {
    if (!instances.some((i) => i.id === instanceId)) {
      setInstanceId(instances[0]?.id ?? "");
    }
  }, [instances, instanceId]);

  // Same as above for the token: a revoke elsewhere (Token Detail fires
  // phx-tokens-changed, which both window hosts turn into a fresh tokens prop)
  // must not leave a dead token selected. Falls back to another available
  // token, or "" if none remain; the conversation-reset effect above already
  // clears history whenever tokenId changes, including this reselect.
  useEffect(() => {
    if (tokenId && !tokens.some((tok) => tok.id === tokenId)) {
      setTokenId(tokens[0]?.id ?? "");
    }
  }, [tokens, tokenId]);

  // Autoscroll on new content.
  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, live, minimized]);

  const displayed = useMemo(() => [...turns.flatMap((turn) => turn.entries), ...live], [turns, live]);

  // Start a fresh conversation: drop the transcript and memory, cancel any turn
  // in flight. Shared by the gear's "Clear chat history" button and the /clear
  // slash command.
  const clearHistory = useCallback(() => {
    cancelledRef.current = true;
    abortRef.current?.abort();
    abortRef.current = null;
    setSending(false);
    setTurns([]);
    setLive([]);
    resetUsage();
    setGearOpen(false);
    setAnnouncement("");
  }, [resetUsage]);

  // Closing the window must abort any in-flight turn, otherwise the server-side
  // agentic loop keeps running (and can still fire tool calls) after the operator
  // dismissed the chat. Same for unmount (e.g. navigating away).
  const handleClose = useCallback(() => {
    cancelledRef.current = true;
    abortRef.current?.abort();
    abortRef.current = null;
    onClose();
  }, [onClose]);

  useEffect(() => () => {
    cancelledRef.current = true;
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  // One turn: either a new user message (mode "user") or a resume of a turn
  // that paused at the round-cap checkpoint (mode "continue", no new user
  // message; the model continues from the existing conversation).
  const runTurn = useCallback(async (mode: "user" | "continue", text: string) => {
    if (sending) return;
    setGearOpen(false);
    setPendingContinue(null);
    cancelledRef.current = false;
    lastQueryRef.current = mode === "user" ? text : "";
    usageBaseRef.current = { input: usageRef.current.input, output: usageRef.current.output };
    turnSawUsageRef.current = false;
    setLive(mode === "user" ? [{ kind: "user", text, ts: Date.now() }] : []);
    setSending(true);

    const sentMessages = memoryMessages(turns, scrollbackLines);
    const sentLen = sentMessages.length;
    let newMessages: unknown[] = [];
    const ac = new AbortController();
    abortRef.current = ac;

    const push = (e: ChatEntry) => setLive((prev) => [...prev, e]);
    // Progress replaces the previous line for the same call rather than appending:
    // a five-minute build ticks dozens of times and would otherwise bury the chat.
    const showProgress = (id: string, message: string) => {
      setLive((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.kind === "progress" && last.id === id) {
          return [...prev.slice(0, -1), { ...last, message }];
        }
        return [...prev, { kind: "progress", id, message } as ChatEntry];
      });
    };
    // Always moves to the bottom, so the line tracks the newest call instead of
    // sitting wherever the first one happened.
    const showActivity = (name: string) => {
      setLive((prev) => [
        ...stripActivity(prev),
        { kind: "progress", id: ACTIVITY_ID, message: t("agentchat.workingOn", { name }), activity: true } as ChatEntry,
      ]);
    };
    const clearActivity = () => setLive((prev) => stripActivity(prev));
    const appendAssistant = (delta: string, thinking: boolean) => {
      setLive((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.kind === "assistant") {
          const upd = { ...last } as Extract<ChatEntry, { kind: "assistant" }>;
          if (thinking) upd.thinking += delta; else upd.text += delta;
          return [...prev.slice(0, -1), upd];
        }
        const fresh: ChatEntry = {
          kind: "assistant", text: thinking ? "" : delta, thinking: thinking ? delta : "",
          ts: Date.now(),
        };
        return [...prev, fresh];
      });
    };

    try {
      await agentCliChat(
        {
          token_id: tokenId,
          instance_id: instanceId,
          model: model || undefined,
          messages: sentMessages,
          ...(mode === "user" ? { user: text } : { continue: true }),
          options: buildOptions(kind, options, caps),
        },
        (name, payload) => {
          if (cancelledRef.current) return;  // cancelled: ignore any late events
          const p = (payload ?? {}) as Record<string, unknown>;
          switch (name) {
            case "assistant_delta":
              appendAssistant(String(p.text ?? ""), false);
              break;
            case "thinking_delta":
              appendAssistant(String(p.text ?? ""), true);
              break;
            case "tool_call":
              push({ kind: "tool_call", id: String(p.id ?? ""), name: String(p.name ?? ""), args: p.arguments });
              showActivity(String(p.name ?? ""));
              break;
            case "tool_progress":
              // A tool reporting its own progress says strictly more than the
              // generic line, so it takes over rather than stacking under it.
              clearActivity();
              showProgress(String(p.id ?? ""), String(p.message ?? ""));
              break;
            case "tool_result":
              push({ kind: "tool_result", id: String(p.id ?? ""), name: String(p.name ?? ""),
                     isError: Boolean(p.is_error), summary: String(p.summary ?? "") });
              break;
            case "approval_required":
              push({ kind: "approval", approvalId: String(p.approval_id ?? ""),
                     toolName: String(p.tool_name ?? ""), reviewUrl: p.review_url as string | undefined,
                     status: "pending" });
              setAnnouncement(t("agentchat.announceApproval", { name: String(p.tool_name ?? "") }));
              break;
            case "approval_resolved":
              setLive((prev) => prev.map((e) =>
                e.kind === "approval" && e.approvalId === p.approval_id
                  ? { ...e, status: String(p.status ?? "resolved"),
                      reason: typeof p.reason === "string" && p.reason ? p.reason : undefined } : e));
              setAnnouncement(t("agentchat.announceResolved", { status: approvalStatusLabel(String(p.status ?? "resolved")) }));
              break;
            case "usage": {
              // Turn-cumulative totals from the server, added onto the session
              // base captured at send time. Context is the newest call's input.
              turnSawUsageRef.current = true;
              const base = usageBaseRef.current;
              const next: SessionUsage = {
                input: base.input + Number(p.input_tokens ?? 0),
                output: base.output + Number(p.output_tokens ?? 0),
                context: Number(p.context_tokens ?? 0) || usageRef.current.context,
                noData: false,
              };
              setSessionUsage(next);
              setUsage(next);
              break;
            }
            case "notice":
              push({ kind: "notice", code: p.code ? String(p.code) : undefined,
                     message: String(p.message ?? "") });
              break;
            case "continue_required":
              // The turn paused at the round-cap checkpoint. Show the
              // Continue/Stop control; the conversation is left resumable.
              setPendingContinue(Number(p.iterations ?? 0) || 0);
              break;
            case "error":
              push({ kind: "error", code: String(p.code ?? "error"), message: String(p.message ?? "") });
              setAnnouncement(t("agentchat.announceError", { message: String(p.message ?? "") }));
              break;
            case "messages":
              newMessages = (Array.isArray(p.messages) ? p.messages : []).slice(sentLen);
              break;
            default:
              break;
          }
        },
        ac.signal,
      );
    } catch (err) {
      // An intentional cancel aborts the fetch; don't surface that as an error.
      if (!cancelledRef.current) {
        push({ kind: "error", code: "network", message: err instanceof Error ? err.message : t("agentchat.requestFailed") });
      }
    } finally {
      abortRef.current = null;
      if (!cancelledRef.current) {
        setSending(false);
        // A completed turn with no usage report (and nothing counted so far)
        // means this provider does not report usage; the footer says so
        // instead of showing a forever-zero counter. Cleared if usage ever
        // arrives (e.g. after switching models on the same account).
        if (!turnSawUsageRef.current
            && usageBaseRef.current.input + usageBaseRef.current.output === 0
            && !usageRef.current.noData) {
          const marked: SessionUsage = { ...usageRef.current, noData: true };
          setSessionUsage(marked);
          setUsage(marked);
        }
        // Finalize the turn: fold live entries into the turn history, then trim.
        const entries = stripActivity(liveRef.current);
        const turn: Turn = { entries, messages: newMessages, lines: countLines(entries) };
        setTurns((prev) => trimTurns([...prev, turn], scrollbackLines));
        setLive([]);
        // Announce the completed reply now that streaming is over.
        const finals = entries.filter((e) => e.kind === "assistant" && e.text.trim());
        const finalText = finals.length ? (finals[finals.length - 1] as Extract<ChatEntry, { kind: "assistant" }>).text : "";
        if (finalText) setAnnouncement(finalText);
      }
    }
  }, [sending, turns, scrollbackLines, tokenId, instanceId, kind, model, options, caps]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || sending) return;
    // Slash commands are handled locally and never sent to the model. /clear
    // starts a new conversation, identical to the gear's Clear chat history.
    if (text.toLowerCase() === "/clear") {
      setInput("");
      clearHistory();
      return;
    }
    // The onboarding wizard listens for this to auto-close once the user has
    // sent their first prompt through the window it opened.
    window.dispatchEvent(new CustomEvent("phx-agentcli-sent"));
    setInput("");
    void runTurn("user", text);
  }, [input, sending, runTurn, clearHistory]);

  // Grant the agent another N rounds from where it paused (idea b): resends the
  // held conversation with continue:true, no new user message.
  const continueTurn = useCallback(() => {
    void runTurn("continue", "");
  }, [runTurn]);

  // Cancel the running turn: abort the request, ignore any late response, and
  // put the query back in the box so it can be edited and resent. The partial
  // turn is folded into the transcript, not discarded (a long agentic exchange
  // is one turn, so dropping live entries erased the whole visible chat). Any
  // still-pending approval card flips to cancelled, matching the server's
  // orphaned-approval cleanup. The folded turn carries no provider messages:
  // the aborted exchange is kept for display but deliberately not replayed as
  // model memory, since the restored query is usually edited and resent.
  const cancel = useCallback(() => {
    cancelledRef.current = true;
    abortRef.current?.abort();
    abortRef.current = null;
    setSending(false);
    const entries = stripActivity(liveRef.current);
    if (entries.length) {
      const kept: ChatEntry[] = entries.map((e) =>
        e.kind === "approval" && e.status === "pending" ? { ...e, status: "cancelled" } : e);
      kept.push({ kind: "notice", message: t("agentchat.cancelledNotice") });
      setTurns((prev) => trimTurns([...prev, { entries: kept, messages: [], lines: countLines(kept) }], scrollbackLines));
    }
    setLive([]);
    setInput((cur) => cur || lastQueryRef.current);
  }, [scrollbackLines]);

  const resolvingApprovalsRef = useRef<Set<string>>(new Set());

  const resolveApproval = useCallback(async (approvalId: string, approve: boolean) => {
    // Guard against a second click landing before the optimistic re-render
    // below has taken the buttons away.
    if (resolvingApprovalsRef.current.has(approvalId)) return;
    resolvingApprovalsRef.current.add(approvalId);
    // Take the buttons away immediately: the card otherwise stays clickable
    // for the whole round trip plus tool execution, and repeated Approve
    // clicks each fire another request whose conflict error lands in the
    // chat. Only a still-pending card is swapped, so a fast SSE resolution
    // is never overwritten.
    const marker = approve ? t("agentchat.approving") : t("agentchat.rejecting");
    setLive((prev) => prev.map((e) =>
      e.kind === "approval" && e.approvalId === approvalId && e.status === "pending"
        ? { ...e, status: marker } : e));
    try {
      if (approve) {
        await api.approveApproval(approvalId);
      } else {
        // A reason typed into the panel's approval detail modal but never
        // submitted there (the operator reviewed in the panel, then rejected
        // from the chat) still steers the next proposal. Empty when nothing
        // was typed, which is the plain do-not-retry rejection.
        const reason = getReasonDraft(approvalId);
        await api.rejectApproval(approvalId, reason ? { reason } : {});
      }
      // The draft has served its purpose either way; the resolved bubble shows
      // the reason the server recorded.
      clearReasonDraft(approvalId);
      // The stream's approval_resolved event updates the card's own status on
      // success; nothing else to do here.
    } catch (err: unknown) {
      // A failure (the capability having been denied since, a transient
      // network error, ...) was previously swallowed silently: the card kept
      // showing Approve/Reject with no indication the click did nothing,
      // which read as "the tab never updates" when actually the request
      // never succeeded in the first place. Surface it and restore the
      // buttons so the admin can see what happened and retry. Restore only
      // from our own marker: if the SSE event resolved the card meanwhile
      // (say, an admin approved it in the panel while our reject failed),
      // that final status wins.
      setLive((prev) => prev.map((e) =>
        e.kind === "approval" && e.approvalId === approvalId && e.status === marker
          ? { ...e, status: "pending" } : e));
      setLive((prev) => [...prev, {
        kind: "error", code: "approval_action_failed",
        message: err instanceof Error ? err.message : t("agentchat.approvalActionFailed"),
      }]);
    } finally {
      resolvingApprovalsRef.current.delete(approvalId);
    }
  }, []);

  // Update one approval card wherever it currently lives.
  //
  // While the agent is blocked waiting for a human the stream is still open and
  // the card is in `live`, which is the usual case. But a turn that ended or was
  // cancelled with an approval still pending folds its entries into `turns` and
  // clears `live`, and that card stays on screen and stays actionable. A handler
  // that only mapped `live` would silently do nothing for exactly those cards.
  // `lines` is left alone deliberately: it bounds scrollback trimming, and a
  // status word is not a line.
  const updateApprovalEntry = useCallback((
    approvalId: string, update: (e: Extract<ChatEntry, { kind: "approval" }>) => ChatEntry,
  ) => {
    const mapEntry = (e: ChatEntry) =>
      e.kind === "approval" && e.approvalId === approvalId ? update(e) : e;
    setLive((prev) => prev.map(mapEntry));
    setTurns((prev) => prev.map((turn) =>
      turn.entries.some((e) => e.kind === "approval" && e.approvalId === approvalId)
        ? { ...turn, entries: turn.entries.map(mapEntry) } : turn));
  }, []);

  // Learn about approvals resolved or claimed ELSEWHERE, straight off the HA bus.
  //
  // The SSE stream also carries approval_resolved, but that frame rides the agent
  // loop resuming and can be several seconds behind a click made in the panel or
  // a notification, during which this bubble kept offering Approve and Reject on
  // an approval already acted on. A claim (an admin's Approve executing its saved
  // action, which is inline and slow) has no SSE frame at all. Both now land here
  // immediately, and the SSE path is left exactly as it was: whichever arrives
  // first wins, and both write the same status, so the duplicate is harmless.
  useEffect(() => subscribeApprovalEvents(null, {
    onResolved: (approvalId, status) => {
      // Unconditional for a matching id, exactly like the SSE handler: an
      // approval resolves once, so the server's status is authoritative.
      updateApprovalEntry(approvalId, (e) => ({ ...e, status: status || "resolved" }));
    },
    onClaimChanged: (approvalId, claimed) => {
      // Claimed: show it as being approved and take the buttons away. Released:
      // the execution failed and it is pending again, so the buttons come back.
      // A card this window itself is mid-request on is left alone, since
      // resolveApproval owns its own optimistic marker and its own rollback.
      if (resolvingApprovalsRef.current.has(approvalId)) return;
      updateApprovalEntry(approvalId, (e) => {
        if (claimed) {
          return e.status === "pending" ? { ...e, status: t("agentchat.approving") } : e;
        }
        return e.status === t("agentchat.approving") ? { ...e, status: "pending" } : e;
      });
    },
  }), [updateApprovalEntry]);

  // Open the full approval in the Phoenix MCP panel's Approvals tab. Uses HA's soft SPA
  // navigation (pushState + location-changed) so it works whether the chat is the
  // panel window or the global overlay on another page, and the overlay survives
  // (no full reload). The panel's own hash listener opens the specific approval.
  const openReview = useCallback((reviewUrl: string | undefined, approvalId: string) => {
    const url = reviewUrl || `/phoenix-mcp#approvals/${approvalId}`;
    try {
      window.history.pushState(null, "", url);
      window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
    } catch {
      window.location.href = url;
    }
  }, []);

  // --- drag ---
  const startDrag = useCallback((e: React.PointerEvent) => {
    if (maximized) return;  // full screen: nothing to drag
    if ((e.target as HTMLElement).closest("button, select")) return;
    e.preventDefault();
    const startX = e.clientX, startY = e.clientY;
    // The pill remembers its own position independently of the restored window,
    // so a drag moves (and persists) whichever one is showing.
    const setActive = minimized ? setPillPos : setPos;
    const persistKey = minimized ? "pillPos" : "pos";
    const origin = minimized
      ? (pillPos.x >= 0 ? { ...pillPos } : { ...pos })
      : { ...pos };
    let latest = { ...origin };
    const move = (ev: PointerEvent) => {
      // Keep the whole window on screen on every side. Uses the live element size
      // (so it is correct whether minimized or full height); if the window is
      // larger than the viewport the max clamps to 0 and it pins to the corner.
      const w = rootRef.current?.offsetWidth ?? size.w;
      const h = rootRef.current?.offsetHeight ?? size.h;
      const maxX = Math.max(0, window.innerWidth - w);
      const maxY = Math.max(0, window.innerHeight - h);
      latest = {
        x: Math.max(0, Math.min(maxX, origin.x + ev.clientX - startX)),
        y: Math.max(0, Math.min(maxY, origin.y + ev.clientY - startY)),
      };
      setActive(latest);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      // Snap to a nearby edge or corner on release: each axis independently snaps
      // to its margin when the window's edge is within SNAP of the viewport edge,
      // so dragging into a corner clicks neatly into it. Skipped while minimized
      // (the bar has no fixed height to snap the bottom edge against).
      let final = latest;
      if (!minimized) {
        const M = 8, SNAP = 56;
        const { w, h } = size;
        let { x, y } = latest;
        if (x <= SNAP) x = M;
        else if (window.innerWidth - (x + w) <= SNAP) x = Math.max(M, window.innerWidth - w - M);
        if (y <= SNAP) y = M;
        else if (window.innerHeight - (y + h) <= SNAP) y = Math.max(M, window.innerHeight - h - M);
        final = { x, y };
        setActive(final);
      }
      patchDurable({ [persistKey]: final });  // persist only when the drag settles
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }, [pos, pillPos, size, minimized, maximized]);

  // --- resize ---
  const startResize = useCallback((dir: string) => (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX, startY = e.clientY;
    const o = { ...size, ...pos };
    let latestSize = { w: o.w, h: o.h };
    let latestPos = { x: o.x, y: o.y };
    const move = (ev: PointerEvent) => {
      const r = snapResizeRect(
        dir, o, ev.clientX - startX, ev.clientY - startY,
        window.innerWidth, window.innerHeight,
      );
      latestSize = { w: r.w, h: r.h };
      latestPos = { x: r.x, y: r.y };
      setSize(latestSize);
      setPos(latestPos);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      patchDurable({ size: latestSize, pos: latestPos });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }, [size, pos]);

  // Mode transitions. The pill and the restored window each remember their own
  // position, so each transition seeds or clamps only the geometry it enters.
  const minimize = useCallback(() => {
    // The pill's own remembered spot wins; the first time, a narrow (mobile)
    // viewport defaults to the very top-right corner (above the panel's Agent
    // Chat button), desktop to the window's corner. Clamped into view either way.
    const base = pillPos.x >= 0
      ? pillPos
      : isNarrowViewport()
        ? { x: window.innerWidth - PILL_SIZE.w - 8, y: 8 }
        : pos;
    const np = clampPosToViewport(base, PILL_SIZE, window.innerWidth, window.innerHeight);
    setPillPos(np);
    patchDurable({ pillPos: np });
    setMode("min");
  }, [pillPos, pos]);

  const maximize = useCallback(() => setMode("max"), []);

  // Rotating a phone (any viewport resize) can strand the remembered window or
  // pill geometry entirely off the new viewport, with nothing left to tap;
  // refit whichever is showing as soon as the viewport changes. Also run once
  // on mount for a geometry that went stale while the window was unmounted.
  // The change-guards make in-bounds calls no-ops, so drags and resizes (which
  // already clamp to the viewport) are never fought.
  useEffect(() => {
    const refit = () => {
      const vw = window.innerWidth, vh = window.innerHeight, M = 8;
      if (mode === "min") {
        const base = pillPos.x >= 0 ? pillPos : pos;  // same fallback the pill renders at
        const np = clampPosToViewport(base, PILL_SIZE, vw, vh);
        if (np.x !== pillPos.x || np.y !== pillPos.y) {
          setPillPos(np);
          patchDurable({ pillPos: np });
        }
      } else if (mode === "normal") {
        const ns = {
          w: Math.min(size.w, Math.max(RESIZE_MIN_W, vw - 2 * M)),
          h: Math.min(size.h, Math.max(RESIZE_MIN_H, vh - 2 * M)),
        };
        const np = clampPosToViewport(pos, ns, vw, vh);
        if (ns.w !== size.w || ns.h !== size.h || np.x !== pos.x || np.y !== pos.y) {
          setSize(ns);
          setPos(np);
          patchDurable({ size: ns, pos: np });
        }
      }
    };
    refit();
    window.addEventListener("resize", refit);
    return () => window.removeEventListener("resize", refit);
  }, [mode, pos, size, pillPos]);

  // Back to the windowed geometry, refitted to the CURRENT viewport: both were
  // remembered under a possibly different one (a phone rotated while minimized
  // or maximized), so clamping the position alone is not enough; a size wider
  // or taller than the viewport must shrink too or it still hangs off-screen.
  const restore = useCallback(() => {
    const vw = window.innerWidth, vh = window.innerHeight, M = 8;
    const ns = {
      w: Math.min(size.w, Math.max(RESIZE_MIN_W, vw - 2 * M)),
      h: Math.min(size.h, Math.max(RESIZE_MIN_H, vh - 2 * M)),
    };
    const np = clampPosToViewport(pos, ns, vw, vh);
    setSize(ns);
    setPos(np);
    patchDurable({ size: ns, pos: np });
    setMode("normal");
  }, [pos, size]);

  const noInstances = instances.length === 0;

  const pillXY = pillPos.x >= 0 ? pillPos : pos;  // fall back before first seed
  // Maximized geometry comes entirely from CSS (inset: 0); no inline style.
  const style: React.CSSProperties = maximized
    ? {}
    : minimized
      ? { left: pillXY.x, top: pillXY.y }
      : { left: pos.x, top: pos.y, width: size.w, height: size.h };

  return (
    <div ref={rootRef} className={`agentcli-window${minimized ? " agentcli-minimized" : ""}${maximized ? " agentcli-maximized" : ""}`} style={style} role="dialog" aria-label={t("agentchat.title")}>
      {/* Persistent polite announcer; mounted unconditionally so assistive
          tech reliably picks up updates (live regions must pre-exist). */}
      <div className="sr-only" role="status">{announcement}</div>
      <div className="agentcli-titlebar" onPointerDown={startDrag}>
        <span className="agentcli-title"><img src={PHOENIX_ICON} className="agentcli-title-icon" alt="" />{t("agentchat.title")}</span>
        <div className="agentcli-titlebar-actions">
          {!minimized && (
            <button className="agentcli-icon-btn agentcli-gear-btn" title={t("agentchat.options")} aria-label={t("agentchat.options")}
                    aria-expanded={gearOpen}
                    onClick={() => setGearOpen((v) => !v)}>&#9881;</button>
          )}
          {mode !== "min" && (
            <button className="agentcli-icon-btn" title={t("agentchat.minimize")} aria-label={t("agentchat.minimize")}
                    onClick={minimize}>–</button>
          )}
          {mode !== "max" && (
            <button className="agentcli-icon-btn" title={t("agentchat.maximize")} aria-label={t("agentchat.maximize")}
                    onClick={maximize}>□</button>
          )}
          {mode !== "normal" && (
            <button className="agentcli-icon-btn" title={t("agentchat.restore")} aria-label={t("agentchat.restore")}
                    onClick={restore}>&#10064;</button>
          )}
          <button className="agentcli-icon-btn" title={t("common.close")} aria-label={t("common.close")} onClick={handleClose}>&times;</button>
        </div>
      </div>

      {gearOpen && !minimized && (
        <div className="agentcli-gear">
          {caps.thinking.length > 0 && (
            // One dropdown carries this provider's real API thinking levels.
            <label className="agentcli-gear-row">
              {kind === "ollama" ? t("agentchat.thinkingOllama") : t("agentchat.thinking")}
              <select value={thinkValue(caps, options)}
                      onChange={(e) => applyThink(caps, e.target.value, setOptions)}>
                {caps.thinking.map((opt) => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
              </select>
            </label>
          )}
          {caps.note && (
            <div className="agentcli-gear-note">{caps.note}</div>
          )}
          {caps.temperature && (
            <label className="agentcli-gear-row">
              {t("agentchat.temperature")}
              <input type="number" step="0.1" min="0" max="2" value={options.temperature} placeholder={t("agentchat.temperatureDefault")}
                     onChange={(e) => setOptions((o) => ({ ...o, temperature: e.target.value }))} />
            </label>
          )}
          <div className="agentcli-gear-row" title={t("agentchat.verboseHint")}>
            <span>{t("agentchat.verbose")}</span>
            <label className="toggle-switch">
              <input type="checkbox" checked={options.verbose} aria-label={t("agentchat.verbose")}
                     onChange={(e) => setOptions((o) => ({ ...o, verbose: e.target.checked }))} />
              <span className="toggle-switch-track" />
            </label>
          </div>
          <div className="agentcli-gear-row" title={t("agentchat.showUsageHint")}>
            <span>{t("agentchat.showUsage")}</span>
            <label className="toggle-switch">
              <input type="checkbox" checked={showUsage} aria-label={t("agentchat.showUsage")}
                     onChange={(e) => setShowUsage(e.target.checked)} />
              <span className="toggle-switch-track" />
            </label>
          </div>
          <div className="agentcli-gear-row" title={t("agentchat.showTimestampsHint")}>
            <span>{t("agentchat.showTimestamps")}</span>
            <label className="toggle-switch">
              <input type="checkbox" checked={showTimestamps} aria-label={t("agentchat.showTimestamps")}
                     onChange={(e) => setShowTimestamps(e.target.checked)} />
              <span className="toggle-switch-track" />
            </label>
          </div>
          <button className="btn btn-outline btn-sm agentcli-gear-clear" onClick={clearHistory}>{t("agentchat.clearHistory")}</button>
          <button className="btn btn-primary btn-sm" onClick={() => setGearOpen(false)}>{t("common.close")}</button>
        </div>
      )}

      {!minimized && (
        <>
          <div className="agentcli-controls">
            <label className="agentcli-control" title={t("agentchat.tokenHint")}>
              <span>{t("agentchat.token")}</span>
              <select value={tokenId} onChange={(e) => { setGearOpen(false); setTokenId(e.target.value); }} aria-label={t("agentchat.token")} disabled={sending}>
                {tokens.map((tok) => <option key={tok.id} value={tok.id}>{tok.name}</option>)}
              </select>
            </label>
            <label className="agentcli-control" title={t("agentchat.providerHint")}>
              <span>{t("agentchat.provider")}</span>
              <select value={instanceId} onChange={(e) => { setGearOpen(false); setInstanceId(e.target.value); }} aria-label={t("agentchat.provider")} disabled={sending || noInstances}>
                {noInstances
                  ? <option value="">{t("common.none")}</option>
                  : instances.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
              </select>
            </label>
            <label className="agentcli-control" title={t("agentchat.modelHint")}>
              <span>{t("agentchat.model")}</span>
              <select value={model} onChange={(e) => { setGearOpen(false); setModel(e.target.value); }} aria-label={t("agentchat.model")} disabled={sending || !models.length}>
                {noInstances
                  ? <option value="">{t("common.none")}</option>
                  : models.length
                    ? models.map((m) => <option key={m} value={m}>{m}</option>)
                    : <option value="">{t("agentchat.noModels")}</option>}
              </select>
            </label>
          </div>

          <div className="agentcli-body" ref={bodyRef}>
            {noInstances && (
              <div className="agentcli-empty">{t("agentchat.noProvider")}</div>
            )}
            {displayed.map((e, i) => (
              <ChatItem key={i} entry={e} verbose={options.verbose} showTs={showTimestamps}
                        onResolve={resolveApproval} onReview={openReview} />
            ))}
            {sending && (
              <div className="agentcli-working" role="status" aria-live="polite">
                <span className="agentcli-spin" aria-hidden="true" />
                {t("agentchat.workingSpinner")}
              </div>
            )}
          </div>

          {pendingContinue !== null && !sending && (
            <div className="agentcli-continue" role="status">
              <span className="agentcli-continue-text">
                {t("agentchat.pausedAfter", { count: pendingContinue })}
              </span>
              <button className="btn btn-primary btn-sm" onClick={continueTurn}>{t("agentchat.continue")}</button>
              <button className="btn btn-sm" onClick={() => setPendingContinue(null)}>{t("agentchat.stop")}</button>
            </div>
          )}

          <div className="agentcli-input">
            <textarea
              value={input}
              aria-label={t("agentchat.messageAria")}
              placeholder={noInstances ? t("agentchat.configureFirst") : t("agentchat.messagePlaceholder")}
              disabled={sending || noInstances}
              rows={2}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); } }}
            />
            {sending ? (
              <button className="btn btn-sm agentcli-cancel-btn" onClick={cancel} title={t("agentchat.stopRequest")}>
                {t("common.cancel")}
              </button>
            ) : (
              <button className="btn btn-primary btn-sm" disabled={noInstances || !input.trim()} onClick={() => void send()}>
                {t("agentchat.send")}
              </button>
            )}
          </div>

          {showUsage && (
            <div className="agentcli-usage"
                 title={usage.input + usage.output > 0
                   ? t("agentchat.usageTitle", { input: localeNumber(usage.input), output: localeNumber(usage.output) })
                   : t("agentchat.usageTitleEmpty")}>
              {usage.input + usage.output > 0 ? (
                <>
                  {t("agentchat.usageSession", { tokens: fmtTokens(usage.input + usage.output) })}
                  {usage.context > 0 ? t("agentchat.usageContext", { tokens: fmtTokens(usage.context) }) : ""}
                </>
              ) : usage.noData ? (
                t("agentchat.usageNoData")
              ) : (
                t("agentchat.usageZero")
              )}
            </div>
          )}

          {/* resize handles: windowed mode only (full screen has no edges to drag) */}
          {mode === "normal" && (["n", "s", "e", "w", "ne", "nw", "se", "sw"] as const).map((d) => (
            <div key={d} className={`agentcli-resize agentcli-resize-${d}`} onPointerDown={startResize(d)} />
          ))}
        </>
      )}
    </div>
  );
}

/** A backend notice or error in the operator's language.
 *
 *  The event carries a stable `code` and the English `message`. Codes Phoenix
 *  itself raises have a catalog entry; a provider-relayed message (a model API's
 *  own error text) has none and shows verbatim, which is what you want for text
 *  Phoenix did not write. */
function serverText(code: string | undefined, message: string): string {
  const key = code ? `agentchat.notice.${code}` : "";
  return key && hasMessage(key) ? t(key) : message;
}

function ChatItem({ entry, verbose, showTs, onResolve, onReview }: {
  entry: ChatEntry;
  verbose: boolean;
  showTs: boolean;
  onResolve: (id: string, approve: boolean) => void;
  onReview: (reviewUrl: string | undefined, approvalId: string) => void;
}) {
  // Wall-clock label under a message bubble; entries predating the ts field
  // (a restored session transcript) simply show none.
  const ts = showTs && "ts" in entry && entry.ts
    ? <div className="agentcli-ts">{fmtClock(entry.ts)}</div>
    : null;
  switch (entry.kind) {
    case "user":
      return <div className="agentcli-msg agentcli-msg-user">{entry.text}{ts}</div>;
    case "assistant":
      return (
        <div className="agentcli-msg agentcli-msg-assistant">
          {verbose && entry.thinking && (
            <details className="agentcli-thinking">
              <summary>{t("agentchat.reasoning")}</summary>
              {/* Same sanitizing renderer as the reply body: models emit
                  markdown in their reasoning too, and a raw <pre> showed the
                  literal ** and # markup. */}
              <div
                className="agentcli-md agentcli-thinking-body"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(entry.thinking) }}
              />
            </details>
          )}
          {flagsUnsafeContent(entry.text) && (
            <div className="agentcli-unsafe">{t("agentchat.unsafeContent")}</div>
          )}
          <div className="agentcli-md" dangerouslySetInnerHTML={{ __html: renderMarkdown(entry.text) }} />
          {ts}
        </div>
      );
    case "tool_call":
      // Tool activity is "verbose" detail; hidden unless the operator opts in.
      return verbose ? <div className="agentcli-tool">{tRich("agentchat.callingTool", { code: (c) => <code>{c}</code> }, { name: entry.name })}</div> : null;
    case "progress":
      // Shown even with verbose off: this is not debug detail, it is the only
      // sign of life during a multi-minute build, which is exactly when a
      // non-verbose operator would otherwise be staring at an empty window.
      // The generic activity line is the mirror image: with verbose ON the
      // "calling <tool>" entries already say it, so rendering both is noise.
      if (entry.activity && verbose) return null;
      return (
        <div className="agentcli-progress" role="status" aria-live="polite">
          <span className="agentcli-progress-dot" aria-hidden="true" />
          {entry.message}
        </div>
      );
    case "tool_result":
      return verbose ? (
        <div className={`agentcli-tool-result${entry.isError ? " is-error" : ""}`}>
          {tRich("agentchat.toolResult", { code: (c) => <code>{c}</code> },
                 { name: entry.name, summary: entry.summary || (entry.isError ? t("agentchat.resultError") : t("agentchat.resultOk")) })}
        </div>
      ) : null;
    case "approval":
      return (
        <div className="agentcli-approval">
          <div>{tRich("agentchat.approvalNeeded", { code: (c) => <code>{c}</code> }, { name: entry.toolName })}</div>
          {entry.status === "pending" ? (
            <div className="agentcli-approval-actions">
              <button className="btn btn-outline btn-sm" onClick={() => onReview(entry.reviewUrl, entry.approvalId)}>{t("agentchat.review")}</button>
              <button className="btn btn-primary btn-sm" onClick={() => onResolve(entry.approvalId, true)}>{t("agentchat.approve")}</button>
              <button className="btn btn-sm" onClick={() => onResolve(entry.approvalId, false)}>{t("agentchat.reject")}</button>
            </div>
          ) : (
            <div className="agentcli-approval-status">
              {approvalStatusLabel(entry.status)}
              {entry.reason ? <span className="agentcli-approval-reason">{t("agentchat.approvalReason", { reason: entry.reason })}</span> : null}
            </div>
          )}
        </div>
      );
    case "notice":
      return <div className="agentcli-notice">{serverText(entry.code, entry.message)}</div>;
    case "error":
      return <div className="agentcli-error">{serverText(entry.code, entry.message)}</div>;
    default:
      return null;
  }
}
