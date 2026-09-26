import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FlaskConical, GitBranch, Loader2, Play, TriangleAlert, X } from "lucide-react";
import { dump, load } from "js-yaml";
import { WorkflowBoard } from "./board/WorkflowBoard";
import { actionCatalog, connectorCatalog, filterOperators } from "./catalog";
import { actionMeta, connectorLabel, connectorMeta, defaultFields, shapesFromWorkflow, summarize, workflowFromShapes } from "./workflows";
import type { CatalogField } from "./catalog";
import { localConfig, type DesignerConfig } from "./config";
import type { ConnectionOption, DiagramShape, FilterRule, GitStatus, NodeData, TestRunResult, Workflow, WorkflowSummary } from "./types";

const EMPTY_SHAPES: DiagramShape[] = [];

/** Plain words for a connection status, mirroring the console's labels. */
const CONNECTION_STATUS_LABELS: Record<string, string> = {
  connected: "connected",
  ready: "setup incomplete",
  expired: "needs reconnection",
  revoked: "revoked",
};

/** What to call a connection: its display name, or the verified identity. */
function connectionTitle(connection: ConnectionOption): string {
  if (connection.display_name && connection.display_name !== connection.connection_id) {
    return connection.display_name;
  }
  return connection.account_title || connection.connection_id;
}

/** "display_name · status" for the datalist entries and the selected hint. */
function connectionHint(connection: ConnectionOption): string {
  const title = connectionTitle(connection);
  const status = CONNECTION_STATUS_LABELS[connection.status ?? ""] ?? connection.status;
  return status && status !== "connected" ? `${title} · ${status}` : title;
}

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
function FieldInput({ field, value, onChange, connections }: {
  field: CatalogField;
  value: string;
  onChange: (value: string) => void;
  connections?: ConnectionOption[] | null;
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
        <select value={value || field.default || ""} onChange={(event) => onChange(event.target.value)}>
          {field.options?.map((option) => <option key={option} value={option}>{option}</option>)}
        </select>
      </label>
    );
  }
  if (field.type === "yaml") {
    return (
      <label>{field.label}{field.required ? " *" : ""}
        <textarea
          className="raw-yaml"
          rows={6}
          value={value}
          placeholder={field.placeholder}
          onChange={(event) => onChange(event.target.value)}
        />
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
  // Connection fields suggest the operator's real connections (display name,
  // verified identity, status) over a bare ID typed from memory; the value
  // stays the plain connection_id in the YAML, and free text still works for
  // anything the snapshot does not know (offline mode, brand-new records).
  if (field.provider && connections) {
    const matches = connections.filter((connection) => connection.provider === field.provider);
    const current = matches.find((connection) => connection.connection_id === value);
    return (
      <label>{field.label}{field.required ? " *" : ""}
        <input
          className="mono-input"
          list={`connections-${field.key}`}
          value={value}
          placeholder={matches.length === 1 && !value ? matches[0].connection_id : field.placeholder}
          onChange={(event) => onChange(event.target.value)}
        />
        <datalist id={`connections-${field.key}`}>
          {matches.map((connection) => (
            <option key={connection.connection_id} value={connection.connection_id}>
              {connectionHint(connection)}
            </option>
          ))}
        </datalist>
        {value !== "" && current && <span className="connection-hint">{connectionHint(current)}</span>}
        {value !== "" && !current && (
          <span className="connection-hint warn">
            Not one of your {field.provider} connections — pick one from the list or check the ID.
          </span>
        )}
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
function RawJsonInput({ value, draft, onChange, onInvalidChange }: {
  value: Record<string, unknown>;
  draft?: string;
  onChange: (next: Record<string, unknown>) => void;
  onInvalidChange: (text: string | null) => void;
}) {
  const [text, setText] = useState(() => draft ?? JSON.stringify(value, null, 2));
  const [invalid, setInvalid] = useState(draft !== undefined);
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
              onInvalidChange(null);
              onChange(parsed as Record<string, unknown>);
              return;
            }
          } catch { /* not JSON yet */ }
          setInvalid(true);
          onInvalidChange(event.target.value);
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
  const [savedId, setSavedId] = useState("new-workflow");
  const [savedEnabled, setSavedEnabled] = useState(true);
  const [canvasExtraDirty, setCanvasExtraDirty] = useState(false);
  const [invalidRawDrafts, setInvalidRawDrafts] = useState<Record<string, string>>({});
  const [leaveOpen, setLeaveOpen] = useState(false);
  const leaveResolver = useRef<((allowed: boolean) => void) | null>(null);
  const allowUnload = useRef(false);
  const [status, setStatus] = useState<{ kind: "idle" | "busy" | "error" | "ok"; message: string }>({ kind: "idle", message: "" });
  const [git, setGit] = useState<GitStatus | null>(null);
  /** The operator's connections from the console host; null = unknown (the
     text inputs render without suggestions or match warnings). */
  const [connections, setConnections] = useState<ConnectionOption[] | null>(null);
  const [view, setView] = useState<"canvas" | "yaml">("canvas");
  const [yamlText, setYamlText] = useState("");
  const [savedYaml, setSavedYaml] = useState("");
  /** The workflow as last loaded or saved; carries `flows:`/`flow:` through canvas saves. */
  const [base, setBase] = useState<Workflow | null>(null);
  const [testOpen, setTestOpen] = useState(false);
  const [testEvent, setTestEvent] = useState("{\n  \"title\": \"Sample event\"\n}");
  const [testBusy, setTestBusy] = useState(false);
  const [testResult, setTestResult] = useState<TestRunResult | null>(null);

  const dirty = useMemo(
    () => view === "yaml"
      ? yamlText !== savedYaml
      : Object.keys(invalidRawDrafts).length > 0 || canvasExtraDirty || workflowId !== savedId || enabled !== savedEnabled || JSON.stringify(shapes) !== savedSnapshot,
    [view, yamlText, savedYaml, invalidRawDrafts, canvasExtraDirty, workflowId, savedId, enabled, savedEnabled, shapes, savedSnapshot]
  );

  // Removing an unknown node or changing its action type also removes its
  // unparseable editor text; other unknown nodes keep their draft on selection.
  useEffect(() => {
    setInvalidRawDrafts((current) => {
      const retained = Object.fromEntries(Object.entries(current).filter(([id]) =>
        shapes.some((shape) => shape.id === id && shape.type === "node" &&
          shape.data?.nodeKind === "action" && !actionCatalog.some((action) => action.type === shape.data?.actionType))));
      return Object.keys(retained).length === Object.keys(current).length ? current : retained;
    });
  }, [shapes]);

  function askToLeave(): Promise<boolean> {
    if (!dirty) return Promise.resolve(true);
    return new Promise((resolve) => {
      if (leaveResolver.current) return resolve(false);
      leaveResolver.current = resolve;
      setLeaveOpen(true);
    });
  }

  function resolveLeave(allowed: boolean) {
    allowUnload.current = allowed;
    setLeaveOpen(false);
    leaveResolver.current?.(allowed);
    leaveResolver.current = null;
  }

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      if (allowUnload.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (!leaveOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        resolveLeave(false);
      }
      if (event.key !== "Tab") return;
      const buttons = [...document.querySelectorAll<HTMLButtonElement>(".leave-prompt button:not(:disabled)")];
      if (!buttons.length) return;
      const first = buttons[0];
      const last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [leaveOpen]);

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
        editable: view === "canvas",
        dirty
      },
      window.location.origin
    );
  }, [config.embedded, workflowId, enabled, sourceName, view, dirty]);

  useEffect(() => {
    if (!config.embedded) return;
    const onLeaveRequest = async (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.source !== window.parent) return;
      const data = event.data as { type?: string; requestId?: unknown };
      if (data?.type !== "designer:request-leave" || typeof data.requestId !== "string") return;
      const allowed = await askToLeave();
      window.parent.postMessage({ type: "designer:leave-result", requestId: data.requestId, allowed }, window.location.origin);
    };
    window.addEventListener("message", onLeaveRequest);
    return () => window.removeEventListener("message", onLeaveRequest);
  });

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

  useEffect(() => {
    if (!config.embedded) return;
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data as { type?: string; connections?: unknown };
      if (data?.type !== "designer:set-connections" || !Array.isArray(data.connections)) return;
      const options = data.connections.filter((entry): entry is ConnectionOption => {
        if (!entry || typeof entry !== "object") return false;
        const record = entry as Partial<ConnectionOption>;
        return typeof record.connection_id === "string" && typeof record.provider === "string";
      });
      setConnections(options);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [config.embedded]);

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
        // Console deep link: /workflows/<id> (or legacy ?workflow=<source>)
        // opens that workflow; the console passes the ref through as-is.
        const requested = new URLSearchParams(window.location.search).get("workflow");
        const match = requested
          ? workflows.find((summary) => summary.source === requested || summary.id === requested)
          : null;
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
      allowUnload.current = false;
      const shapes = shapesFromWorkflow(workflow);
      const yaml = workflowYaml(workflow);
      setSourceName(summary.source);
      setWorkflowId(workflow.id);
      setEnabled(workflow.enabled !== false);
      setShapes(shapes);
      setSavedSnapshot(JSON.stringify(shapes));
      setSavedId(workflow.id);
      setSavedEnabled(workflow.enabled !== false);
      setCanvasExtraDirty(false);
      setInvalidRawDrafts({});
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
    allowUnload.current = false;
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
    setInvalidRawDrafts({});
    setWorkflowId("new-workflow");
    setEnabled(true);
    setShapes([trigger]);
    setSavedSnapshot("[]");
    setSavedId("new-workflow");
    setSavedEnabled(true);
    setCanvasExtraDirty(false);
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
      if (Object.keys(invalidRawDrafts).length) {
        setStatus({ kind: "error", message: "Fix the invalid action JSON before switching to YAML." });
        return;
      }
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
      setCanvasExtraDirty(yamlText !== savedYaml);
      setBase(parsed);
      if (typeof parsed.id === "string" && parsed.id.trim()) setWorkflowId(parsed.id.trim());
      setEnabled(parsed.enabled !== false);
      setSelectedId(null);
    }
    setTestOpen(false);
    setView(next);
  }

  async function save(): Promise<boolean> {
    if (Object.keys(invalidRawDrafts).length) {
      setStatus({ kind: "error", message: "Fix the invalid action JSON before saving." });
      return false;
    }
    let yamlOut: string;
    let workflow: Workflow;
    let nextShapes: DiagramShape[];
    if (view === "yaml") {
      const parsed = parseYamlText(yamlText);
      if (!parsed) return false;
      workflow = parsed;
      yamlOut = yamlText;
      nextShapes = shapesFromWorkflow(parsed);
    } else {
      const built = workflowFromShapes(shapes, workflowId, enabled, base);
      if (built.problems.length) {
        setStatus({ kind: "error", message: built.problems.join(" ") });
        return false;
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
      setSavedId(workflow.id);
      setSavedEnabled(workflow.enabled !== false);
      setCanvasExtraDirty(false);
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
      return true;
    } catch (error) {
      setStatus({ kind: "error", message: String(error) });
      return false;
    }
  }

  async function leaveAfterSave() {
    if (await save()) resolveLeave(true);
  }

  async function openWorkflowSafely(summary: WorkflowSummary) {
    if (summary.source === sourceName || !(await askToLeave())) return;
    await openWorkflow(summary);
  }

  async function newWorkflowSafely() {
    if (!(await askToLeave())) return;
    newWorkflow();
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

  /** Test the unsaved canvas against a sample event (dry-run unless execute). */
  async function runTest(execute: boolean) {
    const { workflow, problems } = workflowFromShapes(shapes, workflowId, enabled);
    if (problems.length) {
      setStatus({ kind: "error", message: problems.join(" ") });
      return;
    }
    let sample: unknown;
    const mode = execute ? "execute" : "dry-run";
    try {
      sample = JSON.parse(testEvent);
    } catch {
      setTestResult({ mode, matched: false, steps: [], error: "The sample event is not valid JSON." });
      return;
    }
    if (!sample || typeof sample !== "object" || Array.isArray(sample)) {
      setTestResult({ mode, matched: false, steps: [], error: "The sample event must be a JSON object." });
      return;
    }
    if (execute && !window.confirm("Run the actions for real? Live messages will be sent.")) return;
    setTestBusy(true);
    setTestResult(null);
    try {
      // The draft goes inline: the server tests exactly what would be saved.
      setTestResult(await api<TestRunResult>(config, "/workflows/test", {
        method: "POST",
        body: JSON.stringify({ event: sample, workflow, execute })
      }));
    } catch (error) {
      setTestResult({ mode, matched: false, steps: [], error: String(error) });
    } finally {
      setTestBusy(false);
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
                connections={connections}
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
              draft={invalidRawDrafts[selected.id]}
              onInvalidChange={(text) => setInvalidRawDrafts((current) => {
                const next = { ...current };
                if (text === null) delete next[selected.id];
                else next[selected.id] = text;
                return next;
              })}
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
        <button className="button primary" type="button" onClick={newWorkflowSafely}>
          <span>New workflow</span>
        </button>
        <nav className="workflow-nav" aria-label="Workflows">
          {summaries.map((summary) => (
            <button
              key={summary.source}
              className={summary.source === sourceName ? "workflow-item active" : "workflow-item"}
              onClick={() => openWorkflowSafely(summary)}
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
            {config.mode === "console" && (
              <button
                className={testOpen ? "button secondary active" : "button secondary"}
                type="button"
                onClick={() => setTestOpen(!testOpen)}
                disabled={status.kind === "busy" || view === "yaml"}
                title={view === "yaml" ? "Switch to Canvas to test this workflow" : undefined}
              >
                <span>Test run</span>
              </button>
            )}
            <button className="button primary" type="button" onClick={save} disabled={status.kind === "busy" || Object.keys(invalidRawDrafts).length > 0}>
              <span>{Object.keys(invalidRawDrafts).length ? "Fix JSON to save" : dirty ? "Save to git" : "Saved"}</span>
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
          {testOpen && (
            <section className="test-panel" aria-label="Test run">
              <header className="test-panel-head">
                <h2>Test run</h2>
                <button className="icon-button" type="button" title="Close" onClick={() => setTestOpen(false)}>
                  <X size={15} />
                </button>
              </header>
              <p className="test-hint">
                Dry-run this canvas against a sample event: every step's inputs are rendered, nothing is sent.
                &ldquo;Run for real&rdquo; executes the actions with live side effects.
              </p>
              <label>Sample event (JSON)
                <textarea
                  className="test-event"
                  rows={6}
                  spellCheck={false}
                  value={testEvent}
                  onChange={(event) => setTestEvent(event.target.value)}
                />
              </label>
              <div className="test-actions">
                <button className="button primary" type="button" disabled={testBusy} onClick={() => runTest(false)}>
                  {testBusy ? <Loader2 size={15} className="spin" /> : <FlaskConical size={15} />}
                  <span>Dry run</span>
                </button>
                <button className="button danger" type="button" disabled={testBusy} onClick={() => runTest(true)}>
                  <Play size={15} /><span>Run for real</span>
                </button>
              </div>
              {testResult && (
                <div className={`test-result ${testResult.error && testResult.steps.length === 0 ? "failed" : testResult.ok ? "passed" : ""}`}>
                  <p className="test-summary">
                    {testResult.error && testResult.steps.length === 0
                      ? testResult.error
                      : <>
                          <strong>{testResult.mode === "execute" ? "Executed" : "Dry run"}</strong>
                          {" — "}
                          {testResult.matched
                            ? "a trigger matches the sample event."
                            : "NO trigger matches the sample event."}
                          {testResult.enabled === false && <span> The workflow is disabled.</span>}
                          {testResult.error && <span> Run stopped: {testResult.error}</span>}
                        </>}
                  </p>
                  {testResult.steps.map((step, index) => (
                    <div key={`${step.action_id}-${index}`} className={step.ok ? "test-step ok" : "test-step failed"}>
                      <span className="test-step-title">
                        {index + 1}. {step.action_id} <em>({step.action_type || "?"})</em>
                        {!step.ok && <span className="test-step-error"> — {step.error}</span>}
                      </span>
                      {(step.rendered_input ?? step.output) !== undefined && (step.rendered_input ?? step.output) !== null && (
                        <pre className="test-io">{JSON.stringify(step.rendered_input ?? step.output, null, 2)}</pre>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </section>
          )}
          {/* "empty" lets the narrow-frame layout drop the panel until a node
             is selected — on a phone the hints would cost half the canvas. */}
          <aside className={selected?.type === "node" ? "inspector" : "inspector empty"}>
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
            {shapes.some((shape) => shape.type === "node") && (
              <nav className="node-jump" aria-label="Select a workflow node">
                <p>Nodes</p>
                {shapes.filter((shape) => shape.type === "node").map((shape) => (
                  <button
                    key={shape.id}
                    type="button"
                    className={shape.id === selectedId ? "node-jump-item selected" : "node-jump-item"}
                    aria-current={shape.id === selectedId ? "true" : undefined}
                    onClick={() => setSelectedId(shape.id)}
                  >
                    {shape.label || shape.data?.actionType || shape.id}
                  </button>
                ))}
              </nav>
            )}
            {selectedInspector()}
            {selected?.type === "arrow" && (
              <p className="inspector-hint">Connector. Drag an endpoint handle to reattach it; Delete removes it.</p>
            )}
          </aside>
        </div>
        )}
      </main>
      {leaveOpen && (
        <div className="leave-backdrop" role="presentation">
          <section className="leave-prompt" role="alertdialog" aria-modal="true" aria-labelledby="leave-title" aria-describedby="leave-description">
            <h2 id="leave-title">Unsaved workflow changes</h2>
            <p id="leave-description">Save this draft before opening another workflow?</p>
            {status.kind === "error" && <p className="leave-error" role="alert">{status.message}</p>}
            <div className="leave-actions">
              <button className="button secondary" type="button" onClick={() => resolveLeave(false)} autoFocus>Stay</button>
              <button className="button secondary" type="button" onClick={() => resolveLeave(true)}>Discard changes</button>
              <button className="button primary" type="button" disabled={status.kind === "busy" || Object.keys(invalidRawDrafts).length > 0} onClick={leaveAfterSave}>Save and continue</button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
