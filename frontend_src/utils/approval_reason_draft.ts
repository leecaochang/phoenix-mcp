// Rejection-reason drafts, shared across the two surfaces that can reject the
// same approval: the panel's approval detail modal (where the reason box lives)
// and the Agent Chat window's inline approval bubble (which has only
// Approve/Reject/Review). Typing a reason in the modal and then clicking Reject
// on the chat bubble used to drop the reason silently, which matters because a
// rejection reason is the operator's steering channel for the next proposal.
//
// Storage is localStorage, not a module variable: when the global Agent Chat
// window is enabled it runs in its own bundle (phoenix-mcp-agentchat.js) with a
// separate module graph from the panel bundle, so nothing in-memory is shared.
// Drafts are short-lived by nature (an approval expires in an hour), so stale
// entries are pruned on every write.

const LS_KEY = "phx-approval-reason-drafts";
const MAX_AGE_MS = 3_600_000; // one approval TTL; a draft older than that is dead

interface Draft {
  text: string;
  ts: number;
}

function readAll(): Record<string, Draft> {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, Draft>;
    if (!parsed || typeof parsed !== "object") return {};
    return parsed;
  } catch {
    return {};
  }
}

function writeAll(drafts: Record<string, Draft>): void {
  try {
    const cutoff = Date.now() - MAX_AGE_MS;
    const fresh: Record<string, Draft> = {};
    for (const [id, d] of Object.entries(drafts)) {
      if (d && typeof d.text === "string" && d.text && typeof d.ts === "number" && d.ts > cutoff) {
        fresh[id] = d;
      }
    }
    if (Object.keys(fresh).length === 0) localStorage.removeItem(LS_KEY);
    else localStorage.setItem(LS_KEY, JSON.stringify(fresh));
  } catch {
    /* storage blocked: the draft simply won't be shared */
  }
}

/** Stash (or, for empty text, drop) the in-progress rejection reason. */
export function setReasonDraft(approvalId: string, text: string): void {
  if (!approvalId) return;
  const drafts = readAll();
  if (text) drafts[approvalId] = { text, ts: Date.now() };
  else delete drafts[approvalId];
  writeAll(drafts);
}

/** The stashed reason for this approval, or "" if none. Does not consume it:
 *  callers clear only after the rejection actually lands, so a failed request
 *  leaves the operator's text intact. */
export function getReasonDraft(approvalId: string): string {
  if (!approvalId) return "";
  const d = readAll()[approvalId];
  if (!d || typeof d.text !== "string") return "";
  if (typeof d.ts !== "number" || d.ts <= Date.now() - MAX_AGE_MS) return "";
  return d.text;
}

/** Drop the draft: the approval resolved (either way, from either surface). */
export function clearReasonDraft(approvalId: string): void {
  if (!approvalId) return;
  const drafts = readAll();
  if (!(approvalId in drafts)) return;
  delete drafts[approvalId];
  writeAll(drafts);
}
