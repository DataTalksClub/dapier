import { FileText, StickyNote, Zap } from "lucide-react";
import { actionCatalog, actionMeta, actionNodeSubtitle, actionNodeTitle, connectorCatalog, connectorLabel, connectorMeta, defaultFields } from "../workflows";
import type { IconComponent } from "../catalog";
import type { ActionType, DiagramShape, NodeData } from "../types";

export type PaletteKind = `trigger:${string}` | ActionType | "note";
export function actionIcon(type: ActionType) {
  return actionMeta(type)?.icon ?? FileText;
}

/** Icon for a canvas node: the trigger connector's logo, or the action's. */
export function nodeIcon(data: NodeData | undefined): IconComponent {
  if (data?.nodeKind === "trigger") return connectorMeta(data.connector ?? "custom")?.logo ?? Zap;
  return actionIcon(data?.actionType ?? "webhook");
}

export interface PaletteEntry {
  kind: PaletteKind;
  label: string;
  icon: IconComponent;
}

/** One trigger chip per connector — the palette's Triggers group. */
export const triggerPalette: PaletteEntry[] = connectorCatalog.map((entry) => ({
  kind: `trigger:${entry.name}`,
  label: entry.label,
  icon: entry.logo
}));

export const actionPalette: PaletteEntry[] = actionCatalog.map((entry) => ({
  kind: entry.type,
  label: entry.label,
  icon: entry.icon ?? FileText
}));

export const notePalette: PaletteEntry[] = [{ kind: "note", label: "Note", icon: StickyNote }];

const allPaletteEntries = [...triggerPalette, ...actionPalette, ...notePalette];

export function paletteLabel(kind: PaletteKind) {
  return allPaletteEntries.find((entry) => entry.kind === kind)?.label ?? "Node";
}
export function nodeDataForKind(kind: PaletteKind): NodeData {
  if (kind === "note") return { nodeKind: "note" };
  if (kind.startsWith("trigger:")) {
    const connector = kind.slice("trigger:".length);
    return {
      nodeKind: "trigger",
      connector,
      event: connectorMeta(connector)?.events[0] ?? "received",
      filters: []
    };
  }
  // An action chip seeds the node with its own type and default fields.
  return { nodeKind: "action", actionType: kind, fields: defaultFields(kind) };
}

export function nodeTitle(shape: DiagramShape): string {
  if (shape.type === "note") return shape.label ?? "Note";
  if (shape.data?.nodeKind === "trigger") {
    return `${connectorLabel(shape.data.connector ?? "custom")} · ${shape.data.event}`;
  }
  return actionNodeTitle(shape.data ?? { nodeKind: "action" });
}

export function nodeSubtitle(shape: DiagramShape): string {
  if (shape.type === "note" || shape.data?.nodeKind === "trigger") return "";
  return actionNodeSubtitle(shape.data ?? { nodeKind: "action" });
}
