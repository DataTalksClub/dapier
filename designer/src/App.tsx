import { useCallback, useEffect, useMemo, useState } from "react";
import { GitBranch, Loader2, TriangleAlert } from "lucide-react";
import { dump, load } from "js-yaml";
import { WorkflowBoard } from "./board/WorkflowBoard";
import { actionCatalog, connectorCatalog, filterOperators } from "./catalog";
import { actionMeta, connectorLabel, connectorMeta, defaultFields, shapesFromWorkflow, summarize, workflowFromShapes } from "./workflows";
import type { CatalogField } from "./catalog";
import { localConfig, type DesignerConfig } from "./config";
import type { DiagramShape, FilterRule, GitStatus, NodeData, Workflow, WorkflowSummary } from "./types";

const EMPTY_SHAPES: DiagramShape[] = [];

/** Product logo for a workflow's trigger connector, used in list rows. */
function TriggerLogo({ connector }: { connector: string }) {
  const Logo = connectorMeta(connector)?.logo;
  return Logo ? <Logo size={12} /> : null;
}

async function api<T>(config: DesignerConfig, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${config.apiBase}${path}`, {
    headers: init?.body ? { "content-type": "application/json" } : undefined,
    ...init
  });
  if (response.status === 401 && config.onUnauthorized) {
    config.onUnauthorized();
    throw new Error("Sign-in required");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error((body as { error?: string }).error ?? `HTTP ${response.status}`);
  return body as T;
}

function workflowYaml(workflow: Workflow): string {
  return dump(workflow, { lineWidth: 100, noRefs: true }).trimEnd() + "\n";
}

/** One catalog field, rendered per its declared type. */
function FieldInput({ field, value, onChange }: {
  field: CatalogField;
  value: string;
  onChange: (value: string) => void;
}) {
  if (field.type === "boolean") {
    return (
      <label className="check-label">
        <input
          type="checkbox"
          checked={(value || field.default || "false") === "true"}
          onChange={(event) => onChange(event.target.checked ? "true" : "false")}
        />
        {field.label}
      </label>
    );
  }
  if (field.type === "select") {
    return (
      <label>{field.label}{field.required ? " *" : ""}
        <select value={value} onChange={(event) => onChange(event.target.value)}>
          {field.options?.map((option) => <option key={option} value={option}>{option}</option>)}
        </select>
      </label>
    );
  }
  if (field.type === "textarea") {
    return (
      <label>{field.label}{field.required ? " *" : ""}
        <textarea value={value} placeholder={field.placeholder} onChange={(event) => onChange(event.target.value)} />
      </label>
    );
  }
  return (
    <label>{field.label}{field.required ? " *" : ""}
      <input
        className="mono-input"
        type={field.type === "number" ? "number" : "text"}
        step={field.type === "number" ? "any" : undefined}
        value={value}
        placeholder={field.placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

/** Escape hatch for action types the catalog does not model: raw JSON editing. */
function RawJsonInput({ value, onChange }: {
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(value, null, 2));
  const [invalid, setInvalid] = useState(false);
  return (
    <>
      <textarea
        className="raw-json"
        value={text}
        rows={8}
        onChange={(event) => {
          setText(event.target.value);
          try {
            const parsed: unknown = JSON.parse(event.target.value);
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
              setInvalid(false);
              onChange(parsed as Record<string, unknown>);
              return;
            }
          } catch { /* not JSON yet */ }
          setInvalid(true);
        }}
      />
      {invalid && <span className="save-problems">Invalid JSON — fixes apply once it parses.</span>}
    </>
  );
}

export function App({ config = localConfig }: { config?: DesignerConfig }) {
  const [summaries, setSummaries] = useState<WorkflowSummary[]>([]);
  const [sourceName, setSourceName] = useState<string | null>(null);
  const [workflowId, setWorkflowId] = useState("new-workflow");
  const [enabled, setEnabled] = useState(true);
  const [shapes, setShapes] = useState<DiagramShape[]>(EMPTY_SHAPES);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [savedSnapshot, setSavedSnapshot] = useState("[]");
  const [status, setStatus] = useState<{ kind: "idle" | "busy" | "error" | "ok"; message: string }>({ kind: "idle", message: "" });
  const [git, setGit] = useState<GitStatus | null>(null);
  const [view, setView] = useState<"canvas" | "yaml">("canvas");
  const [yamlText, setYamlText] = useState("");
  const [savedYaml, setSavedYaml] = useState("");
  /** The workflow as last loaded or saved; carries `flows:`/`flow:` through canvas saves. */
  const [base, setBase] = useState<Workflow | null>(null);

  const dirty = useMemo(
    () => (view === "yaml" ? yamlText !== savedYaml : JSON.stringify(shapes) !== savedSnapshot),
    [view, yamlText, savedYaml, shapes, savedSnapshot]
  );

  /** Tells the framing console what the title bar should show and edit.
     The console owns the h1 rename affordance; it answers with set-id. */
  useEffect(() => {
    if (!config.embedded || window.parent === window) return;
    window.parent.postMessage(
      {
        type: "designer:meta",
        id: workflowId,
        enabled,
        source: sourceName,
        editable: view === "canvas"
      },
      window.location.origin
    );
  }, [config.embedded, workflowId, enabled, sourceName, view]);

  useEffect(() => {
    if (!config.embedded) return;
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data as { type?: string; id?: unknown };
      if (data?.type !== "designer:set-id" || typeof data.id !== "string") return;
      const id = data.id.trim();
      if (!id) return;
      if (view !== "canvas") {
        setStatus({ kind: "error", message: "Switch to Canvas to rename — or edit id: in the YAML." });
        return;
      }
      setWorkflowId(id);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [config.embedded, view]);

  const refreshGit = useCallback(() => {
    if (config.mode !== "local") return;
    api<GitStatus>(config, "/git/status").then(setGit).catch(() => setGit(null));
  }, [config]);

  const refreshList = useCallback(async () => {
    const data = await api<{ workflows: WorkflowSummary[] }>(config, "/workflows");
    setSummaries(data.workflows);
    return data.workflows;
  }, [config]);

  useEffect(() => {
    refreshList()
      .then((workflows) => {
        // Console deep link: /designer?workflow=<source> opens that workflow.
        const requested = new URLSearchParams(window.location.search).get("workflow");
        const match = requested ? workflows.find((summary) => summary.source === requested) : null;
        if (match) return openWorkflow(match);
        // The embedded view has no sidebar, so a blank console canvas starts
        // with the same seeded trigger the sidebar's New-workflow used to add.
        if (config.mode === "console") newWorkflow();
      })
      .catch((error) => setStatus({ kind: "error", message: String(error) }));
    refreshGit();
  }, [refreshGit, refreshList]);

  async function openWorkflow(summary: WorkflowSummary) {
    try {
      const data = await api<{ workflow: Workflow }>(config, `/workflows/${summary.source}`);
      const workflow = data.workflow;
      const shapes = shapesFromWorkflow(workflow);
      const yaml = workflowYaml(workflow);
      setSourceName(summary.source);
      setWorkflowId(workflow.id);
      setEnabled(workflow.enabled !== false);
      setShapes(shapes);
      setSavedSnapshot(JSON.stringify(shapes));
      setBase(workflow);
      setYamlText(yaml);
      setSavedYaml(yaml);
      setSelectedId(null);
      setStatus({ kind: "idle", message: "" });
      if (config.mode === "console" && !config.embedded) {
        history.replaceState(null, "", `${window.location.pathname}?workflow=${encodeURIComponent(summary.source)}`);
      }
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
    setBase(null);
    setYamlText("");
    setSavedYaml("");
    setSelectedId(null);
    setStatus({ kind: "idle", message: "" });
  }

  /** Parses editor YAML; on failure shows the problem and returns null. */
  function parseYamlText(text: string): Workflow | null {
    try {
      const parsed: unknown = load(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setStatus({ kind: "error", message: "The YAML must be one object mapping." });
        return null;
      }
      return parsed as Workflow;
    } catch (error) {
      const message = error instanceof Error ? error.message.split("\n")[0] : String(error);
      setStatus({ kind: "error", message: `Invalid YAML: ${message}` });
      return null;
    }
  }

  /** Switches editor view, materializing edits so nothing is silently dropped. */
  function switchView(next: "canvas" | "yaml") {
    if (next === view) return;
    if (next === "yaml") {
      const built = workflowFromShapes(shapes, workflowId, enabled, base);
      if (built.lost.length) {
        const noun = built.lost.length === 1 ? "node is" : "nodes are";
        setStatus({
          kind: "error",
          message: `${built.lost.length} ${noun} not connected to a trigger and would be lost in YAML — connect or delete them first.`
        });
        return;
      }
      setYamlText(workflowYaml(built.workflow));
    } else {
      const parsed = parseYamlText(yamlText);
      if (!parsed) return;
      const shapes = shapesFromWorkflow(parsed);
      setShapes(shapes);
      setSavedSnapshot(JSON.stringify(shapes));
      setBase(parsed);
      if (typeof parsed.id === "string" && parsed.id.trim()) setWorkflowId(parsed.id.trim());
      setEnabled(parsed.enabled !== false);
      setSelectedId(null);
    }
    setView(next);
  }

  async function save() {
    let yamlOut: string;
    let workflow: Workflow;
    let nextShapes: DiagramShape[];
    if (view === "yaml") {
      const parsed = parseYamlText(yamlText);
      if (!parsed) return;
      workflow = parsed;
      yamlOut = yamlText;
      nextShapes = shapesFromWorkflow(parsed);
    } else {
      const built = workflowFromShapes(shapes, workflowId, enabled, base);
      if (built.problems.length) {
        setStatus({ kind: "error", message: built.problems.join(" ") });
        return;
      }
      workflow = built.workflow;
      yamlOut = workflowYaml(workflow);
      nextShapes = shapes;
    }
    setStatus({ kind: "busy", message: "Saving…" });
    try {
      const result = await api<{ commit: string | null; html_url?: string }>(config, "/workflows", {
        method: "PUT",
        body: JSON.stringify({ yaml: yamlOut, renameFrom: sourceName })
      });
      setBase(workflow);
      setSavedYaml(yamlOut);
      if (view === "yaml") {
        setShapes(nextShapes);
        setSavedSnapshot(JSON.stringify(nextShapes));
        setWorkflowId(workflow.id);
        setEnabled(workflow.enabled !== false);
      } else {
        setSavedSnapshot(JSON.stringify(shapes));
      }
      setSourceName(`${workflow.id}.yaml`);
      if (config.mode === "console" && !config.embedded) {
        history.replaceState(null, "", `${window.location.pathname}?workflow=${encodeURIComponent(`${workflow.id}.yaml`)}`);
      }
      setSummaries((current) => {
        const others = current.filter((entry) => entry.source !== sourceName && entry.source !== `${workflow.id}.yaml`);
        return [...others, summarize(`${workflow.id}.yaml`, workflow)].sort((a, b) => a.source.localeCompare(b.source));
      });
      refreshGit();
      setStatus({
        kind: "ok",
        message: !result.commit
          ? "No changes to commit"
          : config.mode === "console"
            ? `Committed ${result.commit.slice(0, 7)} — the deploy pipeline publishes it in a few minutes`
            : `Saved and committed ${result.commit.slice(0, 7)}`
      });
    } catch (error) {
      setStatus({ kind: "error", message: String(error) });
    }
  }

  async function push() {
    setStatus({ kind: "busy", message: "Pushing…" });
    try {
      const result = await api<{ output: string }>(config, "/git/push", { method: "POST" });
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
        ? `${connectorLabel(data.connector ?? "custom")} · ${data.event}`
        : shape.label;
      return { ...shape, data, label };
    }));
  }

  const selected = shapes.find((shape) => shape.id === selectedId) ?? null;

  const triggerNodes = shapes.filter((shape) => shape.type === "node" && shape.data?.nodeKind === "trigger");
  const selectedInspector = () => {
    if (!selected || selected.type !== "node" || !selected.data) {
      return (
        <div className="inspector-empty">
          <p className="inspector-hint">Select a node on the canvas to edit its properties.</p>
          <p className="inspector-hint">Drag from a node's handle to connect it; double-click a node to rename it.</p>
        </div>
      );
    }
    const data = selected.data;
    if (data.nodeKind === "trigger") {
      const connectorEntry = connectorCatalog.find((entry) => entry.name === (data.connector ?? "custom"))
        ?? { name: "custom", events: [] as string[] };
      return (
        <>
          <section className="inspector-group">
            <h3>Source</h3>
            <label>Connector
              <select
                value={data.connector ?? "custom"}
                onChange={(event) => updateSelected((current) => ({ ...current, connector: event.target.value as NodeData["connector"] }))}
              >
                {connectorCatalog.map((entry) => <option key={entry.name} value={entry.name}>{entry.label}</option>)}
              </select>
            </label>
            <label>Event
              <input
                className="mono-input"
                value={data.event ?? ""}
                list="trigger-events"
                onChange={(event) => updateSelected((current) => ({ ...current, event: event.target.value }))}
              />
              <datalist id="trigger-events">
                {connectorEntry.events.map((event) => <option key={event} value={event} />)}
              </datalist>
            </label>
          </section>
          <section className="inspector-group">
            <h3>Filters</h3>
            {(data.filters ?? []).map((rule, index) => (
              <div key={index} className="filter-row">
                <input
                  className="mono-input"
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
                <input
                  className="mono-input filter-value"
                  placeholder="value"
                  value={rule.value}
                  onChange={(event) => updateSelected((current) => ({
                    ...current,
                    filters: (current.filters ?? []).map((entry, i): FilterRule => i === index ? { ...entry, value: event.target.value } : entry)
                  }))}
                />
              </div>
            ))}
            <button
              className="add-row"
              type="button"
              onClick={() => updateSelected((current) => ({
                ...current,
                filters: [...(current.filters ?? []), { field: "", operator: "equals", value: "" }]
              }))}
            >
              Add filter
            </button>
          </section>
        </>
      );
    }

    const type = data.actionType ?? "webhook";
    const meta = actionMeta(type);
    const setField = (key: string, value: string) => updateSelected((current) => ({
      ...current,
      fields: { ...current.fields, [key]: value }
    }));
    return (
      <>
        <section className="inspector-group">
          <h3>Identity</h3>
          <label>Action type
            {meta ? (
              <select
                value={data.actionType}
                onChange={(event) => updateSelected((current) => ({
                  ...current,
                  actionType: event.target.value,
                  fields: defaultFields(event.target.value),
                  raw: undefined
                }))}
              >
                {actionCatalog.map((entry) => <option key={entry.type} value={entry.type}>{entry.label}</option>)}
              </select>
            ) : (
              <input
                className="mono-input"
                value={data.actionType ?? ""}
                onChange={(event) => updateSelected((current) => ({ ...current, actionType: event.target.value }))}
              />
            )}
          </label>
          <label>Action ID
            <input
              className="mono-input"
              value={data.fields?.id ?? ""}
              placeholder="action-1"
              onChange={(event) => setField("id", event.target.value)}
            />
          </label>
        </section>
        {meta ? (
          <section className="inspector-group">
            <h3>Settings</h3>
            {meta.fields.map((field) => (
              <FieldInput
                key={field.key}
                field={field}
                value={data.fields?.[field.key] ?? ""}
                onChange={(value) => setField(field.key, value)}
              />
            ))}
          </section>
        ) : (
          <section className="inspector-group">
            <h3>Unknown action</h3>
            <p className="inspector-hint">
              Not in the catalog — the YAML is kept as-is on save. Edit it as JSON:
            </p>
            <RawJsonInput
              key={selected.id}
              value={data.raw ?? {}}
              onChange={(raw) => updateSelected((current) => ({ ...current, raw }))}
            />
          </section>
        )}
      </>
    );
  };

  return (
    <div className={config.embedded ? "designer-shell embedded" : "designer-shell"}>
      {!config.embedded && (
        <aside className="designer-sidebar">
        {config.mode === "console" ? (
          <a className="brand brand-link" href="/"><span className="brand-mark">D</span><span>← Console · Designer</span></a>
        ) : (
          <div className="brand"><span className="brand-mark">D</span><span>Workflow designer</span></div>
        )}
        <button className="button primary" type="button" onClick={newWorkflow}>
          <span>New workflow</span>
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
              <span className="workflow-meta">
                <TriggerLogo connector={summary.connector} />
                {connectorLabel(summary.connector)}/{summary.event} · {summary.actionCount} action{summary.actionCount === 1 ? "" : "s"}
              </span>
              {!summary.enabled && <span className="workflow-disabled">disabled</span>}
            </button>
          ))}
          {summaries.length === 0 && <p className="inspector-hint">No workflows found.</p>}
        </nav>
        {config.mode === "local" && git && (
          <div className="git-foot">
            <GitBranch size={14} />
            <span>{git.branch}</span>
            <span className={git.dirty ? "git-dirty" : "git-clean"}>{git.dirty ? "unsaved changes" : "clean"}</span>
            {(git.ahead > 0 || git.behind > 0) && <span>{git.ahead}↑ {git.behind}↓</span>}
          </div>
        )}
        </aside>
      )}

      <main className="designer-main">
        <header className="designer-topbar">
          <div className="topbar-title">
            <div className="view-switch" role="group" aria-label="Editor view">
              <button
                type="button"
                className={view === "canvas" ? "view-option active" : "view-option"}
                aria-pressed={view === "canvas"}
                onClick={() => switchView("canvas")}
              >
                Canvas
              </button>
              <button
                type="button"
                className={view === "yaml" ? "view-option active" : "view-option"}
                aria-pressed={view === "yaml"}
                onClick={() => switchView("yaml")}
              >
                YAML
              </button>
            </div>
            {/* Embedded in the console, the console's own h1 renames the
               workflow (see views/designer.js); standalone keeps an input. */}
            {!config.embedded && (
              <input
                className="id-input"
                value={workflowId}
                aria-label="Workflow ID"
                placeholder="workflow-id"
                disabled={view === "yaml"}
                title={view === "yaml" ? "Edit the id in the YAML view" : undefined}
                onChange={(event) => setWorkflowId(event.target.value)}
              />
            )}
            {view === "canvas" && triggerNodes.length === 0 && (
              <span className="save-problems">Add a trigger node to save.</span>
            )}
          </div>
          <div className="topbar-actions">
            <label
              className="check-label enabled-toggle"
              title={view === "yaml" ? "Edit enabled in the YAML view" : undefined}
            >
              <input
                type="checkbox"
                checked={enabled}
                disabled={view === "yaml"}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              Enabled
            </label>
            {status.message && (
              <span className={`status-message ${status.kind}`}>
                {status.kind === "busy" && <Loader2 size={14} className="spin" />}
                {status.kind === "error" && <TriangleAlert size={14} />}
                {status.message}
              </span>
            )}
            {config.mode === "local" && (
              <button className="button secondary" type="button" onClick={push} disabled={!git || git.ahead === 0}>
                <span>Push {git && git.ahead > 0 ? `(${git.ahead})` : ""}</span>
              </button>
            )}
            <button className="button primary" type="button" onClick={save} disabled={status.kind === "busy"}>
              <span>{dirty ? "Save to git" : "Saved"}</span>
            </button>
          </div>
        </header>

        {view === "yaml" ? (
          <div className="yaml-editor">
            <div className="yaml-editor-bar">
              <span className="mono-file">workflows/{sourceName ?? `${workflowId}.yaml`}</span>
              <span className="yaml-hint">comments are not preserved on save</span>
            </div>
            <textarea
              className="yaml-text"
              value={yamlText}
              spellCheck={false}
              aria-label="Workflow YAML"
              onChange={(event) => setYamlText(event.target.value)}
            />
          </div>
        ) : (
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
            {selected?.type === "node" && selected.data ? (
              <header className={`inspector-head ${selected.data.nodeKind === "trigger" ? "kind-trigger" : "kind-action"}`}>
                <h2>{selected.data.nodeKind === "trigger" ? "Trigger" : "Action"}</h2>
                <p className="inspector-summary">{selected.data.nodeKind === "trigger"
                  ? `${connectorLabel(selected.data.connector ?? "custom")} · ${selected.data.event ?? ""}`
                  : [selected.data.actionType, selected.data.fields?.id].filter(Boolean).join(" · ")}</p>
              </header>
            ) : (
              <header className="inspector-head">
                <h2>Inspector</h2>
              </header>
            )}
            {selectedInspector()}
            {selected?.type === "arrow" && (
              <p className="inspector-hint">Connector. Drag an endpoint handle to reattach it; Delete removes it.</p>
            )}
          </aside>
        </div>
        )}
      </main>
    </div>
  );
}
