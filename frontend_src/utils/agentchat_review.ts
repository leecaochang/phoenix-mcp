// The Phoenix panel and injected Agent Chat are separate frontend bundles, but
// they share the same browser Window. A DOM event is therefore the narrow,
// synchronous bridge for completing a review opened from Agent Chat without
// coupling either bundle to the other's React tree.
export const AGENTCHAT_REVIEW_DECIDED_EVENT = "phx-agentchat-review-decided";

export function notifyAgentChatReviewDecided(approvalId: string): void {
  if (!approvalId) return;
  window.dispatchEvent(new CustomEvent(AGENTCHAT_REVIEW_DECIDED_EVENT, {
    detail: { approvalId },
  }));
}

export function reviewDecisionApprovalId(event: Event): string | null {
  const id = (event as CustomEvent<{ approvalId?: unknown }>).detail?.approvalId;
  return typeof id === "string" && id ? id : null;
}
