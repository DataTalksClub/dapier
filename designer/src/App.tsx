import { useCallback, useEffect, useMemo, useState } from "react";
import { CloudUpload, FilePlus2, GitBranch, Loader2, Save, TriangleAlert } from "lucide-react";
import { dump } from "js-yaml";
import { WorkflowBoard } from "./WorkflowBoard";
import {
  actionCatalog,
  connectorCatalog,
  filterOperators,
  shapesFromWorkflow,
  summarize,
  workflowFromShapes
} from "./workflows";
import type { DiagramShape, FilterRule, GitStatus, NodeData, Workflow, WorkflowSummary } from "./types";

const EMPTY_SHAPES: DiagramShape[] = [];

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: init?.body ? { "content-type": "application/json" } : undefined,
    ...init
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error((body as { error?: string }).error ?? `HTTP ${response.status}`);
  return body as T;
}

function workflowYaml(workflow: Workflow): string {
  return dump(workflow, { lineWidth: 100, noRefs: true }).trimEnd() + "\n";
}

export function App() {
  const [summaries, setSummaries] = useState<WorkflowSummary[]>([]);
  const [sourceName, setSourceName] = useState<string | null>(null);
  const [workflowId, setWorkflowId] = useState("new-workflow");
  const [enabled, setEnabled] = useState(true);
  const [shapes, setShapes] = useState<DiagramShape[]>(EMPTY_SHAPES);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [savedSnapshot, setSavedSnapshot] = useState("[]");
  const [status, setStatus] = useState<{ kind: "idle" | "busy" | "error" | "ok"; message: string }>({ kind: "idle", message: "" });
  const [git, setGit] = useState<GitStatus | null>(null);

  const dirty = useMemo(() => JSON.stringify(shapes) !== savedSnapshot, [shapes, savedSnapshot]);

  const refreshGit = useCallback(() => {
    api<GitStatus>("/git/status").then(setGit).catch(() => setGit(null));
  }, []);

  const refreshList = useCallback(async () => {
    const data = await api<{ workflows: WorkflowSummary[] }>("/workflows");
    setSummaries(data.workflows);
  }, []);

  useEffect(() => {
    refreshList().catch((error) => setStatus({ kind: "error", message: String(error) }));
    refreshGit();
  }, [refreshGit, refreshList]);

  async function openWorkflow(summary: WorkflowSummary) {
    try {
      const data = await api<{ workflow: Workflow }>(`/workflows/${summary.source}`);
      const workflow = data.workflow;
      setSourceName(summary.source);
      setWorkflowId(workflow.id);
      setEnabled(workflow.enabled !== false);
      setShapes(shapesFromWorkflow(workflow));
      setSavedSnapshot(JSON.stringify(shapesFromWorkflow(workflow)));
      setSelectedId(null);
      setStatus({ kind: "idle", message: "" });
    } catch (error) {
      setStatus({ kind: "error", message: String(error) });
    }
  }

  function newWorkflow() {
    const trigger: DiagramShape = {
      id: "trigger",
      type: "node",
      x: 420,
      y: 140,
      width: 232,
      height: 96,
      label: "email · message.received",
      data: { nodeKind: "trigger", connector: "email", event: "message.received", filters: [] }
    };
    setSourceName(null);
    setWorkflowId("new-workflow");
    setEnabled(true);
    setShapes([trigger]);
    setSavedSnapshot("[]");
    setSelectedId(null);
    setStatus({ kind: "idle", message: "" });
  }

  async function save() {
    const { workflow, problems } = workflowFromShapes(shapes, workflowId, enabled);
    if (problems.length) {
      setStatus({ kind: "error", message: problems.join(" ") });
      return;
    }
    setStatus({ kind: "busy", message: "Saving…" });
    try {
      const result = await api<{ commit: string | null }>("/workflows", {
        method: "PUT",
        body: JSON.stringify({ yaml: workflowYaml(workflow), renameFrom: sourceName })
      });
      setSavedSnapshot(JSON.stringify(shapes));
      setSourceName(`${workflow.id}.yaml`);
      setSummaries((current) => {
        const others = current.filter((entry) => entry.source !== sourceName && entry.source !== `${workflow.id}.yaml`);
        return [...others, summarize(`${workflow.id}.yaml`, workflow)].sort((a, b) => a.source.localeCompare(b.source));
      });
      refreshGit();
      setStatus({
        kind: "ok",
        message: result.commit ? `Saved and committed ${result.commit.slice(0, 7)}` : "Saved (no changes)"
      });
    } catch (error) {
      setStatus({ kind: "error", message: String(error) });
    }
  }

  async function push() {
    setStatus({ kind: "busy", message: "Pushing…" });
    try {
      const result = await api<{ output: string }>("/git/push", { method: "POST" });
      refreshGit();
      setStatus({ kind: "ok", message: result.output.trim() || "Pushed" });
    } catch (error) {
      setStatus({ kind: "error", message: String(error) });
    }
  }

  function updateSelected(mutate: (data: NodeData) => NodeData) {
    if (!selectedId) return;
    setShapes((current) => current.map((shape) => {
      if (shape.id !== selectedId || !shape.data) return shape;
      const data = mutate(shape.data);
      const label = data.nodeKind === "trigger"
        ? `${data.connector} · ${data.event}`
        : shape.label;
      return { ...shape, data, label };
    }));
  }

  const selected = shapes.find((shape) => shape.id === selectedId) ?? null;

  const triggerNode = shapes.find((shape) => shape.data?.nodeKind === "trigger") ?? null;
  const selectedInspector = () => {
    if (!selected || selected.type !== "node" || !selected.data) {
      return <p className="inspector-hint">Select a node on the canvas to edit it. Drag from a node's handles to connect actions.</p>;
    }
    const data = selected.data;
    if (data.nodeKind === "trigger") {
      return (
        <>
          <label>Connector
            <select
              value={data.connector ?? "custom"}
              onChange={(event) => updateSelected((current) => ({ ...current, connector: event.target.value as NodeData["connector"] }))}
            >
              {connectorCatalog.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          <label>Event
            <input value={data.event ?? ""} onChange={(event) => updateSelected((current) => ({ ...current, event: event.target.value }))} />
          </label>
          <div className="field-block">
            <span className="field-label">Filters</span>
            {(data.filters ?? []).map((rule, index) => (
              <div key={index} className="filter-row">
                <input
                  placeholder="field"
                  value={rule.field}
                  onChange={(event) => updateSelected((current) => ({
                    ...current,
                    filters: (current.filters ?? []).map((entry, i): FilterRule => i === index ? { ...entry, field: event.target.value } : entry)
                  }))}
                />
                <select
                  value={rule.operator}
                  onChange={(event) => updateSelected((current) => ({
                    ...current,
                    filters: (current.filters ?? []).map((entry, i): FilterRule => i === index
                      ? { ...entry, operator: event.target.value as FilterRule["operator"] }
                      : entry)
                  }))}
                >
                  {filterOperators.map((op) => <option key={op} value={op}>{op}</option>)}
                </select>
                <input
                  placeholder="value"
                  value={rule.value}
                  onChange={(event) => updateSelected((current) => ({
                    ...current,
                    filters: (current.filters ?? []).map((entry, i): FilterRule => i === index ? { ...entry, value: event.target.value } : entry)
                  }))}
                />
                <button
                  className="icon-button"
                  title="Remove filter"
                  type="button"
                  onClick={() => updateSelected((current) => ({
                    ...current,
                    filters: (current.filters ?? []).filter((_, i) => i !== index)
                  }))}
                >
                  ×
                </button>
              </div>
            ))}
            <button
              className="button quiet"
              type="button"
              onClick={() => updateSelected((current) => ({
                ...current,
                filters: [...(current.filters ?? []), { field: "", operator: "equals", value: "" }]
              }))}
            >
              Add filter
            </button>
          </div>
        </>
      );
    }

    const meta = actionCatalog.find((entry) => entry.type === data.actionType) ?? actionCatalog[0];
    return (
      <>
        <label>Action type
          <select
            value={data.actionType ?? "webhook"}
            onChange={(event) => updateSelected((current) => ({
              ...current,
              actionType: event.target.value as NodeData["actionType"],
              fields: {}
            }))}
          >
            {actionCatalog.map((entry) => <option key={entry.type} value={entry.type}>{entry.label}</option>)}
          </select>
        </label>
        <label>Action ID
          <input
            value={data.fields?.id ?? ""}
            placeholder={meta.fields[0].label}
            onChange={(event) => updateSelected((current) => ({ ...current, fields: { ...current.fields, id: event.target.value } }))}
          />
        </label>
        {meta.fields.filter((field) => field.key !== "id").map((field) => (
          <label key={field.key}>{field.label}{field.required ? " *" : ""}
            <input
              value={data.fields?.[field.key] ?? ""}
              placeholder={field.placeholder}
              onChange={(event) => updateSelected((current) => ({ ...current, fields: { ...current.fields, [field.key]: event.target.value } }))}
            />
          </label>
        ))}
        {(data.actionType === "render_html_to_pdf") && (
          <>
            <label>PDF page format
              <input
                value={data.pdfPageFormat ?? "A4"}
                onChange={(event) => updateSelected((current) => ({ ...current, pdfPageFormat: event.target.value }))}
              />
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={data.pdfPrintBackground ?? true}
                onChange={(event) => updateSelected((current) => ({ ...current, pdfPrintBackground: event.target.checked }))}
              />
              Print background
            </label>
          </>
        )}
      </>
    );
  };

  return (
    <div className="designer-shell">
      <aside className="designer-sidebar">
        <div className="brand"><span className="brand-mark">D</span><span>Workflow designer</span></div>
        <button className="button primary" type="button" onClick={newWorkflow}>
          <FilePlus2 size={15} /><span>New workflow</span>
        </button>
        <nav className="workflow-nav" aria-label="Workflows">
          {summaries.map((summary) => (
            <button
              key={summary.source}
              className={summary.source === sourceName ? "workflow-item active" : "workflow-item"}
              onClick={() => openWorkflow(summary)}
              type="button"
            >
              <span className="workflow-name">{summary.id}</span>
              <span className="workflow-meta">{summary.connector}/{summary.event} · {summary.actionCount} action{summary.actionCount === 1 ? "" : "s"}</span>
              {!summary.enabled && <span className="workflow-disabled">disabled</span>}
            </button>
          ))}
          {summaries.length === 0 && <p className="inspector-hint">No workflows found.</p>}
        </nav>
        {git && (
          <div className="git-foot">
            <GitBranch size={14} />
            <span>{git.branch}</span>
            <span className={git.dirty ? "git-dirty" : "git-clean"}>{git.dirty ? "unsaved changes" : "clean"}</span>
            {(git.ahead > 0 || git.behind > 0) && <span>{git.ahead}↑ {git.behind}↓</span>}
          </div>
        )}
      </aside>

      <main className="designer-main">
        <header className="designer-topbar">
          <div className="topbar-title">
            <label>Workflow ID
              <input className="id-input" value={workflowId} onChange={(event) => setWorkflowId(event.target.value)} />
            </label>
            <label className="check-label">
              <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
              Enabled
            </label>
            {triggerNode === null && <span className="save-problems">Add a trigger node to save.</span>}
          </div>
          <div className="topbar-actions">
            {status.message && (
              <span className={`status-message ${status.kind}`}>
                {status.kind === "busy" && <Loader2 size={14} className="spin" />}
                {status.kind === "error" && <TriangleAlert size={14} />}
                {status.message}
              </span>
            )}
            <button className="button secondary" type="button" onClick={push} disabled={!git || git.ahead === 0}>
              <CloudUpload size={15} /><span>Push {git && git.ahead > 0 ? `(${git.ahead})` : ""}</span>
            </button>
            <button className="button primary" type="button" onClick={save} disabled={status.kind === "busy"}>
              <Save size={15} /><span>{dirty ? "Save to git" : "Saved"}</span>
            </button>
          </div>
        </header>

        <div className="designer-body">
          <WorkflowBoard
            shapes={shapes}
            setShapes={setShapes}
            selectedId={selectedId}
            setSelectedId={setSelectedId}
            sessionControls={(actions) => (
              <>
                <button className="icon-button" disabled={!actions.canUndo} onClick={actions.undo} title="Undo" type="button">↺</button>
                <button className="icon-button" disabled={!actions.canRedo} onClick={actions.redo} title="Redo" type="button">↻</button>
                <button className="icon-button" onClick={actions.clearCanvas} title="Clear canvas" type="button">✕</button>
              </>
            )}
          />
          <aside className="inspector">
            <h2>{selected?.type === "node"
              ? (selected.data?.nodeKind === "trigger" ? "Trigger" : "Action")
              : "Inspector"}</h2>
            {selectedInspector()}
            {selected?.type === "arrow" && (
              <p className="inspector-hint">Connector. Drag an endpoint handle to reattach it; Delete removes it.</p>
            )}
          </aside>
        </div>
      </main>
    </div>
  );
}
