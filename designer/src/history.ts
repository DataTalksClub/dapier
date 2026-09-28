import type { DiagramShape } from "./types";

/**
 * Per-editing-session undo/redo for the editor draft, kept as a pure data
 * structure so the reducer logic stays testable without a renderer: App owns
 * one HistoryState and applies snapshots to its draft state on undo/redo.
 *
 * A snapshot is the whole editable draft — canvas shapes, workflow id and the
 * on/off flag — so undo restores renames and trigger changes as well as step
 * edits. Mutations push the PREVIOUS snapshot ("how it was before"), so a
 * burst of keystrokes coalesces by keeping the oldest entry of the burst.
 */

/** Everything the canvas editor can change, captured at one moment. */
export interface DraftSnapshot {
  shapes: DiagramShape[];
  workflowId: string;
  enabled: boolean;
}

interface HistoryEntry {
  snapshot: DraftSnapshot;
  /** Wall-clock ms when the entry was last touched (coalescing window). */
  at: number;
  /** Edits sharing a key within COALESCE_MS collapse into one entry. */
  key?: string;
}

export interface HistoryState {
  past: HistoryEntry[];
  future: HistoryEntry[];
}

/** Typing bursts within this window become a single undo entry. */
export const COALESCE_MS = 500;

/** Oldest entries fall off; undo cannot walk past this many changes. */
export const MAX_HISTORY = 50;

/** History starts at the loaded draft: nothing to undo, nothing to redo. */
export function initHistory(): HistoryState {
  return { past: [], future: [] };
}

/** Records that `snapshot` (the state before the change being applied) is
    restorable. Pass `key` to coalesce rapid related edits — e.g. one text
    field's typing burst — into the previous entry instead of a new one. */
export function pushHistory(
  state: HistoryState,
  snapshot: DraftSnapshot,
  options: { key?: string; now?: number } = {}
): HistoryState {
  const now = options.now ?? Date.now();
  const top = state.past[state.past.length - 1];
  // Same editing burst: slide the window, keep the older snapshot so one
  // undo returns to just before the burst started.
  if (options.key && top && top.key === options.key && now - top.at <= COALESCE_MS) {
    return { past: [...state.past.slice(0, -1), { ...top, at: now }], future: [] };
  }
  return {
    past: [...state.past.slice(-(MAX_HISTORY - 1)), { snapshot, at: now, key: options.key }],
    future: []
  };
}

/** Steps back one entry, parking the current draft on the redo stack. */
export function undoHistory(
  state: HistoryState,
  present: DraftSnapshot
): { state: HistoryState; snapshot: DraftSnapshot } | null {
  const top = state.past[state.past.length - 1];
  if (!top) return null;
  const past = state.past.slice(0, -1);
  // The new top's key clears so an edit right after the undo opens its own
  // entry instead of folding into the burst that preceded the undo.
  if (past.length) past[past.length - 1] = { ...past[past.length - 1], key: undefined };
  return {
    state: {
      past,
      future: [...state.future.slice(-(MAX_HISTORY - 1)), { snapshot: present, at: Date.now() }]
    },
    snapshot: top.snapshot
  };
}

/** Steps forward one entry, parking the current draft back on the undo stack. */
export function redoHistory(
  state: HistoryState,
  present: DraftSnapshot
): { state: HistoryState; snapshot: DraftSnapshot } | null {
  const top = state.future[state.future.length - 1];
  if (!top) return null;
  return {
    state: {
      past: [...state.past.slice(-(MAX_HISTORY - 1)), { snapshot: present, at: Date.now() }],
      future: state.future.slice(0, -1)
    },
    snapshot: top.snapshot
  };
}
