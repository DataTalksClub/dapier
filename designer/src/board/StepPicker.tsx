import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, FileText, Search, X } from "../icons";
import { actionCatalog, actionConnector, actionVerbRank, connectorCatalog, stepSection } from "../catalog";
import type { IconComponent } from "../catalog";
import { triggerPalette } from "./nodes";
import type { PaletteKind } from "./nodes";

/**
 * The step picker — how every node enters the canvas. Search-first with a
 * grouped browse below it, organized by the job the step is hired for:
 * flow control, AI, developer plumbing, and per-app verb lists (the app's
 * logo carries the recognition the old icon dump tried to provide).
 *
 * Modes: "trigger" offers connectors, "action" offers steps, "change"
 * retypes an existing action node. The board decides what a pick does
 * (place at a point, auto-connect to a dropped handle, retype in place).
 */

export type PickerMode = "trigger" | "action" | "change";

interface StepPickerProps {
  mode: PickerMode;
  title: string;
  onPick: (kind: PaletteKind) => void;
  onClose: () => void;
}

interface PickerRow {
  kind: PaletteKind;
  label: string;
  icon: IconComponent;
  /** One line under the label: the action's description, or the trigger's events. */
  detail: string;
  /** Where the row came from, shown while searching ("Slack", "Flow control"). */
  section: string;
}

const RECENTS_KEY = "dapier-designer-recent-steps";
const RECENTS_MAX = 5;

function loadRecents(): string[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(RECENTS_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((kind): kind is string => typeof kind === "string").slice(0, RECENTS_MAX) : [];
  } catch {
    return [];
  }
}

function saveRecent(kind: string) {
  const next = [kind, ...loadRecents().filter((entry) => entry !== kind)].slice(0, RECENTS_MAX);
  try {
    localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  } catch {
    // Private mode or full quota: recents are a nicety, never a failure.
  }
}

const sectionOrder: Record<string, number> = { Recent: 0, "Flow control": 1, AI: 2, "Developer & data": 3, Triggers: 4 };
function sectionRank(name: string) {
  return sectionOrder[name] ?? 99;
}

const sectionNames: Record<Exclude<ReturnType<typeof stepSection>, "app">, string> = {
  flow: "Flow control",
  ai: "AI",
  data: "Developer & data"
};

export function StepPicker({ mode, title, onPick, onClose }: StepPickerProps) {
  const [query, setQuery] = useState("");
  /** App groups start folded (the job groups stay open); a name here opens it. */
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const recents = useRef(loadRecents());
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const rows: PickerRow[] = useMemo(() => {
    if (mode === "trigger") {
      return triggerPalette.map((entry) => {
        const connector = connectorCatalog.find((candidate) => `trigger:${candidate.name}` === entry.kind);
        return {
          kind: entry.kind,
          label: entry.label,
          icon: entry.icon,
          detail: connector && connector.events.length ? `Fires on: ${connector.events.join(", ")}` : "Custom event payload",
          section: "Triggers"
        };
      });
    }
    return actionCatalog.map((entry) => {
      const connector = actionConnector(entry.type);
      const section = connector?.label ?? sectionNames[stepSection(entry.type) as "flow" | "ai" | "data"];
      return {
        kind: entry.type,
        label: entry.label,
        icon: entry.icon ?? FileText,
        detail: entry.description ?? "",
        section
      };
    });
  }, [mode]);

  const filtered: PickerRow[] = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) => (
      row.label.toLowerCase().includes(needle)
      || String(row.kind).toLowerCase().includes(needle)
      || row.section.toLowerCase().includes(needle)
      || row.detail.toLowerCase().includes(needle)
    ));
  }, [query, rows]);

  /** Browse groups when not searching: recents, then job sections, then one
      collapsible group per app. */
  const groups: Array<{ name: string; icon?: IconComponent; rows: PickerRow[]; collapsible: boolean }> = useMemo(() => {
    if (query.trim()) return [];
    const byName = new Map<string, PickerRow[]>();
    for (const row of filtered) {
      const bucket = byName.get(row.section) ?? [];
      bucket.push(row);
      byName.set(row.section, bucket);
    }
    const recentKinds = new Set(recents.current);
    const recentRows = rows.filter((row) => recentKinds.has(String(row.kind)))
      .sort((a, b) => recents.current.indexOf(String(a.kind)) - recents.current.indexOf(String(b.kind)));
    const appGroups = [...byName.entries()]
      .filter(([name]) => sectionOrder[name] === undefined)
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([name, groupRows]) => ({ name, rows: groupRows, collapsible: true }));
    const fixedGroups = [...byName.entries()]
      .filter(([name]) => sectionOrder[name] !== undefined)
      .sort((a, b) => sectionRank(a[0]) - sectionRank(b[0]))
      .map(([name, groupRows]) => ({ name, rows: groupRows, collapsible: false }));
    const result: Array<{ name: string; icon?: IconComponent; rows: PickerRow[]; collapsible: boolean }> = [];
    if (recentRows.length) result.push({ name: "Recent", rows: recentRows, collapsible: false });
    result.push(...fixedGroups);
    result.push(...appGroups.map((group) => ({
      ...group,
      rows: [...group.rows].sort((a, b) => actionVerbRank(String(a.kind)) - actionVerbRank(String(b.kind)))
    })));
    return result;
  }, [filtered, query, rows]);

  const firstKind = filtered[0]?.kind;

  function pick(kind: PaletteKind) {
    if (mode !== "trigger") saveRecent(String(kind));
    onPick(kind);
  }

  function toggle(name: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  function onListKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Enter" && firstKind !== undefined && event.target === inputRef.current) {
      event.preventDefault();
      pick(firstKind);
    }
  }

  const groupIcon = (name: string): IconComponent | undefined => {
    const connector = connectorCatalog.find((entry) => entry.label === name);
    return connector?.logo;
  };

  return (
    <div className="step-picker-overlay" onPointerDown={onClose}>
      <div
        className="step-picker"
        role="dialog"
        aria-label={title}
        onPointerDown={(event) => event.stopPropagation()}
        onKeyDown={onListKeyDown}
      >
        <header className="step-picker-head">
          <Search size={20} strokeWidth={1.8} />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={`${title} — search ${mode === "trigger" ? "triggers" : "steps"}`}
            aria-label="Search steps"
            spellCheck={false}
          />
          <button className="icon-button" onClick={onClose} title="Close" type="button">
            <X size={20} strokeWidth={1.8} />
          </button>
        </header>
        <div className="step-picker-body">
          {filtered.length === 0 && (
            <p className="step-picker-empty">Nothing matches “{query.trim()}”.</p>
          )}
          {groups.map((group) => {
            const isOpen = !group.collapsible || expanded.has(group.name);
            const GroupIcon = group.icon ?? groupIcon(group.name);
            const countNoun = mode === "trigger" ? "trigger" : "step";
            return (
              <section key={group.name} className="step-group">
                <button
                  className={group.collapsible ? "step-group-head toggle" : "step-group-head"}
                  onClick={group.collapsible ? () => toggle(group.name) : undefined}
                  aria-expanded={group.collapsible ? isOpen : undefined}
                  type="button"
                >
                  {group.collapsible && (isOpen ? <ChevronDown size={20} strokeWidth={1.8} /> : <ChevronRight size={20} strokeWidth={1.8} />)}
                  {GroupIcon && <GroupIcon size={20} strokeWidth={1.8} />}
                  <span>{group.name}</span>
                  <em>{group.rows.length === 1 ? `1 ${countNoun}` : `${group.rows.length} ${countNoun}s`}</em>
                </button>
                {isOpen && group.rows.map((row) => (
                  <button
                    key={String(row.kind)}
                    className="step-row"
                    onClick={() => pick(row.kind)}
                    type="button"
                  >
                    <row.icon size={20} strokeWidth={1.8} />
                    <span className="step-row-text">
                      <span className="step-row-label">{row.label}</span>
                      {row.detail && <span className="step-row-detail">{row.detail}</span>}
                    </span>
                  </button>
                ))}
              </section>
            );
          })}
          {/* Searching flattens the groups: one list, the origin named per
              row — unless the label already says it. */}
          {query.trim() && filtered.map((row) => (
            <button key={`search-${row.kind}`} className="step-row" onClick={() => pick(row.kind)} type="button">
              <row.icon size={20} strokeWidth={1.8} />
              <span className="step-row-text">
                <span className="step-row-label">{row.label}</span>
                {!row.label.toLowerCase().includes(row.section.toLowerCase()) && (
                  <span className="step-row-detail">{row.section}</span>
                )}
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
