/** Discard a response that a newer request has already superseded. */

import { useCallback, useRef } from "react";

/**
 * Hand back a "is this still the newest request?" check, one per view.
 *
 * Every list here is loaded from several places at once: the initial mount, a
 * manual refresh, a poll tick, an HA event, a filter change and a "Load more"
 * all call the same loader and write the same state, with no ordering between
 * them. Responses arrive in whatever order the network returns them, so a slow
 * request started under the OLD filter could land after a fast one started
 * under the new one and put the old rows back, where they sit until something
 * happens to reload.
 *
 * Last STARTED wins, not last finished: a newer request is newer intent (the
 * operator switched tab, or asked for a fresh top page), so its answer is the
 * one that should be on screen. A superseded "Load more" is dropped rather than
 * appended, which is right for the same reason: an offset-zero refresh has
 * already replaced the window its offset was computed against.
 *
 * Usage: call `begin()` before awaiting, and check its result before touching
 * state afterwards.
 *
 *   const isLatest = useLatestRequest();
 *   const load = useCallback(async () => {
 *     const current = isLatest();
 *     const resp = await api.something();
 *     if (!current()) return;
 *     setRows(resp.rows);
 *   }, [isLatest]);
 */
export function useLatestRequest(): () => () => boolean {
  const generation = useRef(0);
  return useCallback(() => {
    const mine = ++generation.current;
    return () => mine === generation.current;
  }, []);
}
