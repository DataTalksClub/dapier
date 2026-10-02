import { useCallback, useEffect, useMemo, useRef, useState, useId, type Dispatch, type SetStateAction } from "react";
import { ClipboardCopy, CloudDownload, Copy, FlaskConical, GitBranch, Keyboard, Loader2, Menu, Play, RotateCcw, RotateCw, Trash2, TriangleAlert, Workflow as WorkflowIcon, X } from "./icons";
import { dump, load } from "js-yaml";
import { WorkflowBoard } from "./board/WorkflowBoard";
import { actionCatalog, connectorCatalog, errorActionsField, filterOperators, onErrorField, onFailField } from "./catalog";
import { NODE_HEIGHT, NODE_WIDTH, actionMeta, connectorLabel, connectorMeta, defaultFields, orderedActionNodes, orderedWorkflow, shapesFromWorkflow, summarize, workflowFromShapes } from "./workflows";
import type { CatalogField } from "./catalog";
import { localConfig, type DesignerConfig } from "./config";
import type { ConnectionOption, DiagramShape, DraftInfo, FilterRule, GitStatus, NodeData, Point, TestRunResult, Workflow, WorkflowSummary } from "./types";
import { initHistory, pushHistory, undoHistory, redoHistory, type DraftSnapshot, type HistoryState } from "./history";

const EMPTY_SHAPES: DiagramShape[] = [];

/** Read-only canvas: the board's drag path writes through this instead of
    setShapes when the open workflow runs from its trigger. */
const noopSetShapes: Dispatch<SetStateAction<DiagramShape[]>> = () => {};

/** localStorage key copying a step across workflows: Copy step writes it,
    Paste step (any workflow's editor) reads it. */
const STEP_CLIPBOARD_KEY = "dapier-designer.step-clipboard";

/** What Copy step stores: everything needed to rebuild the action node.
    Forgiving on purpose — an entry pasted where it does not fit surfaces the
    mismatch through the ordinary save validation, like a manual step. */
interface StepClipboardEntry {
  actionType?: string;
  fields?: Record<string, string>;
  raw?: Record<string, unknown>;
  label?: string;
}

function readStepClipboard(): StepClipboardEntry | null {
  try {
    const text = window.localStorage.getItem(STEP_CLIPBOARD_KEY);
    if (!text) return null;
    const parsed: unknown = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    return parsed as StepClipboardEntry;
  } catch {
    return null;
  }
}

function writeStepClipboard(step: StepClipboardEntry) {
  try {
    window.localStorage.setItem(STEP_CLIPBOARD_KEY, JSON.stringify(step));
  } catch { /* best-effort: private mode or blocked storage just skips it */ }
}

/** Rows for the "?" cheat sheet; ⌘ where the platform uses it, Ctrl elsewhere. */
const MOD_KEY = /Mac|iPhone|iPad/i.test(navigator.userAgent) ? "⌘" : "Ctrl";
const SHORTCUTS: Array<{ keys: string[]; description: string }> = [
  { keys: [`${MOD_KEY} Z`], description: "Undo" },
  { keys: [`${MOD_KEY} ⇧ Z`, "Ctrl Y"], description: "Redo" },
  { keys: [`${MOD_KEY} D`], description: "Duplicate the selected step" },
  { keys: [`${MOD_KEY} C`], description: "Copy the selected step for pasting into any workflow" },
  { keys: [`${MOD_KEY} V`], description: "Paste a copied step as a new node" },
  { keys: ["Delete", "Backspace"], description: "Delete the selected shape (undoable)" },
  { keys: ["?"], description: "Show this cheat sheet" },
  { keys: ["Esc"], description: "Close menus and dialogs" }
];

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

/** `base` overrides apiBase for endpoints outside the designer prefix:
    discovery answers on /api/admin, not /api/admin/designer. */
async function api<T>(config: DesignerConfig, path: string, init?: RequestInit, base: string = config.apiBase): Promise<T> {
  const response = await fetch(`${base}${path}`, {
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
  return dump(orderedWorkflow(workflow), { lineWidth: 100, noRefs: true }).trimEnd() + "\n";
}

/** A prose editor with room to read long prompts without changing their text. */
function PromptField({ field, value, onChange }: {
  field: CatalogField;
  value: string;
  onChange: (value: string) => void;
}) {
  const id = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const inline = useRef<HTMLTextAreaElement>(null);
  const expanded = useRef<HTMLTextAreaElement>(null);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(value);
  useEffect(() => {
    if (open && dialog.current && !dialog.current.open) {
      dialog.current.showModal();
      expanded.current?.focus();
      expanded.current?.setSelectionRange(inline.current?.selectionStart || 0, inline.current?.selectionEnd || 0);
    } else if (!open && dialog.current?.open) dialog.current.close();
  }, [open]);
  const close = () => setOpen(false);
  return (
    <div className="prompt-field">
      <div className="prompt-field-label">
        <label htmlFor={id}>{field.label}{field.required ? " *" : ""}</label>
        <button type="button" className="prompt-expand" onClick={() => { setDraft(value); setOpen(true); }}>Expand editor</button>
      </div>
      <textarea id={id} ref={inline} className="prompt-inline" rows={10} value={value}
        placeholder={field.placeholder} onChange={event => onChange(event.target.value)} />
      <dialog ref={dialog} className="prompt-editor-dialog" aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-hint`} onCancel={close} onClose={close}
        onKeyDown={event => event.stopPropagation()}>
        <header className="prompt-editor-head">
          <h2 id={`${id}-title`}>Edit {field.label.toLowerCase()}</h2>
          <button type="button" className="dk-button dk-button--secondary" aria-label="Close prompt editor" onClick={close}><X size={16} strokeWidth={1.5} /></button>
        </header>
        <p id={`${id}-hint`} className="prompt-editor-hint">Changes apply to this step. Save the workflow when you’re ready.</p>
        <label className="prompt-editor-label" htmlFor={`${id}-expanded`}>{field.label}</label>
        <textarea id={`${id}-expanded`} ref={expanded} className="prompt-expanded" value={draft}
          placeholder={field.placeholder} spellCheck onChange={event => setDraft(event.target.value)} />
        <footer className="prompt-editor-actions">
          <button className="dk-button dk-button--secondary" type="button" onClick={close}>Cancel</button>
          <button className="dk-button dk-button--primary" type="button" onClick={() => { onChange(draft); close(); }}>Apply to step</button>
        </footer>
      </dialog>
    </div>
  );
}

/** One catalog field, rendered per its declared type. */
function FieldInput({ field, value, onChange, connections, fields, siblingFields, config }: {
  field: CatalogField;
  value: string;
  onChange: (value: string) => void;
  connections?: ConnectionOption[] | null;
  /** Sibling field values of the selected step — discovery's query params. */
  fields?: Record<string, string>;
  /** Sibling field definitions, for the discovery hint's label. */
  siblingFields?: CatalogField[];
  config?: DesignerConfig;
}) {
  const [discovering, setDiscovering] = useState(false);
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
  if (field.type === "textarea" || field.type === "json") {
    if (field.key === "prompt" || field.key === "system") {
      return <PromptField field={field} value={value} onChange={onChange} />;
    }
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
  // Discoverable resource fields (Spreadsheet ID, Channel, …) keep their plain
  // text input and gain a Browse… picker over the connector's live resources;
  // console mode only — local dev has no admin API to discover against.
  if (field.discover && config?.mode === "console") {
    const discover = field.discover;
    const fromKey = discover.from ?? "connection_id";
    const account = discover.account ?? (fields?.[fromKey] ?? "").trim();
    const fromLabel = discover.from
      ? siblingFields?.find((sibling) => sibling.key === fromKey)?.label.toLowerCase()
      : undefined;
    return (
      <>
        <label>{field.label}{field.required ? " *" : ""}
          <span className="discover-field">
            <input
              className="mono-input"
              type={field.type === "number" ? "number" : "text"}
              value={value}
              placeholder={field.placeholder}
              onChange={(event) => onChange(event.target.value)}
            />
            <button className="dk-button quiet" type="button" disabled={!account} onClick={() => setDiscovering(true)}>
              Browse…
            </button>
          </span>
          {!account && (
            <span className="connection-hint">
              Set {fromLabel ?? "Connection ID"} first
            </span>
          )}
        </label>
        {account && discovering && (
          <DiscoveryPicker
            config={config}
            discover={discover}
            accountId={account}
            fields={fields ?? {}}
            onPick={(picked) => {
              setDiscovering(false);
              onChange(picked);
            }}
            onClose={() => setDiscovering(false)}
          />
        )}
      </>
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

/** One live resource from the discovery API: id + display name plus extras. */
interface DiscoveryItem {
  id: string;
  name: string;
  [key: string]: unknown;
}

/** "spreadsheets" → "spreadsheet" for the picker's title. */
function resourceSingular(resource: string): string {
  return resource.replace(/_/g, " ").replace(/s$/, "");
}

/** "update" wants "an", "spreadsheet" wants "a". */
function article(word: string): string {
  return /^[aeiou]/i.test(word) ? "an" : "a";
}

/** Fills a "{key}" template from the picked item, e.g. a Drive download URL. */
function applyTemplate(template: string, item: DiscoveryItem): string {
  return template.replace(/\{(\w+)\}/g, (_, key: string) => String(item[key] ?? ""));
}

/** Modal browser over the discovery API for one field's resources. Mounts
    open, fetches once, and applies the picked template through onPick. */
function DiscoveryPicker({ config, discover, accountId, fields, onPick, onClose }: {
  config: DesignerConfig;
  discover: NonNullable<CatalogField["discover"]>;
  accountId: string;
  fields: Record<string, string>;
  onPick: (value: string) => void;
  onClose: () => void;
}) {
  const [items, setItems] = useState<DiscoveryItem[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const adminBase = config.apiBase.replace(/\/designer$/, "");
  const accountPath = `/connections/${encodeURIComponent(accountId)}/discover`;
  // Only the tag's declared discovery params ride along, resolved from their
  // sibling fields; the backend rejects any other query param with a 400.
  const url = useMemo(() => {
    const params = new URLSearchParams();
    for (const [param, key] of Object.entries(discover.params ?? {})) {
      const val = (fields[key] ?? "").trim();
      if (val) params.set(param, val);
    }
    const qs = params.toString();
    return qs ? `${accountPath}/${discover.resource}?${qs}` : `${accountPath}/${discover.resource}`;
  }, [accountPath, discover.resource, discover.params, fields]);
  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setError("");
    api<{ items: DiscoveryItem[] }>(config, url, undefined, adminBase)
      .then((data) => { if (!cancelled) setItems(data.items ?? []); })
      .catch((err) => { if (!cancelled) setError(String(err)); });
    return () => { cancelled = true; };
  }, [config, url, adminBase]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const needle = query.trim().toLowerCase();
  const matches = (items ?? []).filter((item) =>
    !needle || `${item.name} ${item.id}`.toLowerCase().includes(needle));
  const resource = discover.resource.replace(/_/g, " ");
  return (
    <div className="picker-backdrop" role="presentation" onClick={onClose}>
      <section
        className="picker-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="picker-title"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="picker-title">Pick {article(resourceSingular(discover.resource))} {resourceSingular(discover.resource)}</h2>
        <input
          value={query}
          placeholder={`Filter ${resource}…`}
          aria-label={`Filter ${resource}`}
          autoFocus
          onChange={(event) => setQuery(event.target.value)}
        />
        {error && <p className="picker-error" role="alert">{error}</p>}
        {!error && items === null && <p className="picker-status">Loading…</p>}
        {items !== null && matches.length === 0 && <p className="picker-status">No {resource} found</p>}
        <div className="picker-list">
          {matches.map((item) => (
            <button
              key={item.id}
              type="button"
              className="picker-item"
              onClick={() => onPick(applyTemplate(discover.value ?? "{id}", item))}
            >
              <span className="picker-item-name">{item.name}</span>
              <span className="picker-item-id">{item.id}</span>
            </button>
          ))}
        </div>
      </section>
    </div>
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

/** One earlier step as the insert-from-previous picker shows it: the save-time
    id plus its canvas label, and its test output when one exists. */
interface PriorStep {
  id: string;
  label: string;
  output?: Record<string, unknown>;
}

/** Leaf paths into a step's test output: top-level keys, recursing one level
    into nested objects (arrays count as leaves — a path can stop on them). */
function outputPaths(output: Record<string, unknown>, depth = 0, prefix = ""): string[] {
  const paths: string[] = [];
  for (const [key, value] of Object.entries(output)) {
    const path = prefix ? `${prefix}.${key}` : key;
    paths.push(path);
    if (depth < 1 && value && typeof value === "object" && !Array.isArray(value)) {
      paths.push(...outputPaths(value as Record<string, unknown>, depth + 1, path));
    }
  }
  return paths;
}

/** Modal list of the steps that run before the one being edited: id, label,
    and its saved output keys as template chips. Clicking a chip hands the
    full `{steps.<id>.output…}` template to onPick (copyTemplate), like the
    trigger's sample-field chips. */
function StepsTemplatePicker({ steps, onPick, onClose }: {
  steps: PriorStep[];
  onPick: (template: string) => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="picker-backdrop" role="presentation" onClick={onClose}>
      <section
        className="picker-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="steps-templates-title"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="steps-templates-title">Insert from previous steps</h2>
        <p className="picker-status">
          Outputs from earlier steps&rsquo; test runs — click one to copy its template,
          then paste it into any action field.
        </p>
        <div className="picker-list">
          {steps.map((step) => (
            <div key={step.id} className="step-templates">
              <p className="step-templates-head">
                <span className="picker-item-id">{step.id}</span>
                <span className="picker-item-name">{step.label}</span>
              </p>
              <div className="template-chips">
                <button
                  type="button"
                  className="template-chip"
                  title={`Copy {steps.${step.id}.output}`}
                  onClick={() => onPick(`{steps.${step.id}.output}`)}
                >
                  {`{steps.${step.id}.output}`}
                </button>
                {outputPaths(step.output ?? {}).map((path) => (
                  <button
                    key={path}
                    type="button"
                    className="template-chip"
                    title={`Copy {steps.${step.id}.output.${path}}`}
                    onClick={() => onPick(`{steps.${step.id}.output.${path}}`)}
                  >
                    {`{steps.${step.id}.output.${path}}`}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

export function App({ config = localConfig }: { config?: DesignerConfig }) {
  const [summaries, setSummaries] = useState<WorkflowSummary[]>([]);
  const [sourceName, setSourceName] = useState<string | null>(null);
  const [workflowId, setWorkflowId] = useState("new-workflow");
  /** The open workflow runs from its trigger (webhook/telegram/...): the API
      serves it by id, there is no file to save to, and saves refuse the id —
      everything below keys off this to keep the canvas read-only. */
  const [hookBacked, setHookBacked] = useState(false);
  const [initialWorkflowLoaded, setInitialWorkflowLoaded] = useState(false);
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
  /** The saved server-side draft for the open workflow (G15): a save writes a
      draft, Publish/Discard promote or throw it. Null = no draft known. */
  const [draftInfo, setDraftInfo] = useState<DraftInfo | null>(null);
  const [testOpen, setTestOpen] = useState(false);
  const [testEvent, setTestEvent] = useState("{\n  \"title\": \"Sample event\"\n}");
  const [testBusy, setTestBusy] = useState(false);
  const [testStrict, setTestStrict] = useState(false);
  const [sampleBusy, setSampleBusy] = useState(false);
  const [testResult, setTestResult] = useState<TestRunResult | null>(null);
  /** Per-step test (Zapier's "Test step"): the selected action runs alone
     against the test panel's sample event. Keyed by node id so a stale
     result never shows for another node. */
  const [stepTest, setStepTest] = useState<{ nodeId: string | null; busy: boolean; result: TestRunResult | null }>({
    nodeId: null, busy: false, result: null
  });
  /** Outputs of the steps already tested for real this session, shaped like
     run history: testing step 2 sees step 1's output, like Zapier's editor. */
  const [stepOutputs, setStepOutputs] = useState<Record<string, { status: string; output?: Record<string, unknown>; error?: string }>>({});
  /** The workflow's own last trigger input (GET /api/admin/triggers/sample):
      the {trigger.*} names action templates can rely on. Null = not fetched
      (local mode has no admin API, or the workflow has no sample yet). */
  const [triggerSample, setTriggerSample] = useState<{ source?: string; fields: string[] } | null>(null);
  /** The insert-from-previous picker over the earlier steps' outputs. */
  const [stepsPickerOpen, setStepsPickerOpen] = useState(false);
  /** "?" overlay listing the editor's keyboard shortcuts. */
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  /** Mobile: the workflow sidebar opens as a modal drawer (scrim, one pane). */
  const [navOpen, setNavOpen] = useState(false);
  /** A step sits on the cross-workflow clipboard, so Paste step can appear. */
  const [clipboardHasStep, setClipboardHasStep] = useState(() => readStepClipboard() !== null);

  const dirty = useMemo(
    () => view === "yaml"
      ? yamlText !== savedYaml
      : Object.keys(invalidRawDrafts).length > 0 || canvasExtraDirty || workflowId !== savedId || enabled !== savedEnabled || JSON.stringify(shapes) !== savedSnapshot,
    [view, yamlText, savedYaml, invalidRawDrafts, canvasExtraDirty, workflowId, savedId, enabled, savedEnabled, shapes, savedSnapshot]
  );

  /** Whole-editor undo history (designer/src/history.ts): canvas ops,
      inspector typing, renames, the on/off toggle, and YAML materializations
      all push here, so one timeline covers everything instead of the board
      alone. Entries hold the snapshot from BEFORE each change. */
  const [editHistory, setEditHistory] = useState<HistoryState>(initHistory);
  const canUndo = editHistory.past.length > 0;
  const canRedo = editHistory.future.length > 0;
  /** Always-fresh draft snapshot: handlers registered in dep-array effects
      (the console's set-id message) would otherwise commit stale shapes. */
  const draftRef = useRef<DraftSnapshot>({ shapes: EMPTY_SHAPES, workflowId: "new-workflow", enabled: true });
  draftRef.current = { shapes, workflowId, enabled };

  /** Records the editor state as restorable right before a change lands.
      Pass coalesceKey for rapid like-key edits (typing bursts) so they
      collapse into one undo step instead of one per keystroke. */
  function commitEdit(previous: DraftSnapshot, coalesceKey?: string) {
    setEditHistory((current) => pushHistory(current, previous, coalesceKey ? { key: coalesceKey } : {}));
  }

  /** History-tracked shape edit — the board's discrete ops and the inspector. */
  function editShapes(updater: (current: DiagramShape[]) => DiagramShape[], coalesceKey?: string) {
    if (hookBacked) return;
    const current = draftRef.current;
    const nextShapes = updater(current.shapes);
    if (nextShapes === current.shapes) return;
    commitEdit(current, coalesceKey);
    setShapes(nextShapes);
  }

  /** Records a drag that already mutated shapes live through setShapes while
      the pointer was down: the board hands back its pre-drag snapshot, which
      is what undo must restore. */
  function commitDrag(preDragShapes: DiagramShape[]) {
    if (preDragShapes === draftRef.current.shapes) return;
    commitEdit({ ...draftRef.current, shapes: preDragShapes });
  }

  function applySnapshot(snapshot: DraftSnapshot) {
    setShapes(snapshot.shapes);
    setWorkflowId(snapshot.workflowId);
    setEnabled(snapshot.enabled);
    setSelectedId((current) => snapshot.shapes.some((shape) => shape.id === current) ? current : null);
  }

  function undo() {
    if (hookBacked) return;
    const step = undoHistory(editHistory, draftRef.current);
    if (!step) return;
    applySnapshot(step.snapshot);
    setEditHistory(step.state);
  }

  function redo() {
    if (hookBacked) return;
    const step = redoHistory(editHistory, draftRef.current);
    if (!step) return;
    applySnapshot(step.snapshot);
    setEditHistory(step.state);
  }

  /** Opening or creating a workflow starts a fresh timeline. */
  function resetHistory() {
    setEditHistory(initHistory());
  }

  /** Rename from the standalone input or the console's set-id message. */
  function renameWorkflow(id: string) {
    if (hookBacked) return;
    commitEdit(draftRef.current, "workflow-id");
    setWorkflowId(id);
  }

  function toggleEnabled(next: boolean) {
    if (hookBacked) return;
    commitEdit(draftRef.current);
    setEnabled(next);
  }

  /** Deletes the selected shape (node — with its arrows — note, or connector).
      One undo entry, so no confirm dialog. */
  function deleteSelectedShape() {
    if (hookBacked) return;
    const id = selectedId;
    if (!id) return;
    commitEdit(draftRef.current);
    setShapes((current) => current.filter((shape) => (
      shape.id !== id && shape.sourceId !== id && shape.targetId !== id
    )));
    setSelectedId(null);
  }

  /** Duplicate: a copy of the action directly after it in the chain, with a
      fresh node id, its Action ID suffixed "-copy", and its field values,
      filters and raw extras carried over. */
  function duplicateStep(id: string) {
    if (hookBacked) return;
    const shape = draftRef.current.shapes.find((entry) => entry.id === id);
    if (!shape || shape.type !== "node" || shape.data?.nodeKind !== "action") return;
    const data = shape.data;
    const label = `${shape.label ?? actionMeta(data.actionType ?? "webhook")?.label ?? data.actionType ?? "Step"} (copy)`;
    const copy: DiagramShape = {
      ...shape,
      id: crypto.randomUUID(),
      x: shape.x + 36,
      y: shape.y + 36,
      label,
      data: {
        ...data,
        fields: { ...data.fields, id: data.fields?.id ? `${data.fields.id.trim()}-copy` : "" },
        ...(data.filters ? { filters: data.filters.map((rule) => ({ ...rule })) } : {}),
        ...(data.raw ? { raw: { ...data.raw } } : {})
      }
    };
    commitEdit(draftRef.current);
    setShapes((current) => {
      const at = current.findIndex((entry) => entry.id === id);
      if (at < 0) return [...current, copy];
      const next = [...current];
      next.splice(at + 1, 0, copy);
      return next;
    });
    setSelectedId(copy.id);
    setStatus({ kind: "ok", message: `Step duplicated as "${label}".` });
  }

  /** Copy step onto the cross-workflow clipboard: type, fields, raw extras
      and label. Paste lives in any workflow's editor, this browser only. */
  function copyStep(id: string) {
    const shape = draftRef.current.shapes.find((entry) => entry.id === id);
    if (!shape || shape.type !== "node" || shape.data?.nodeKind !== "action") return;
    const data = shape.data;
    writeStepClipboard({
      actionType: data.actionType,
      fields: { ...(data.fields ?? {}) },
      ...(data.raw ? { raw: { ...data.raw } } : {}),
      label: shape.label
    });
    setClipboardHasStep(true);
    setStatus({ kind: "ok", message: "Step copied — open another workflow and press Ctrl/Cmd+V or right-click → Paste step." });
  }

  /** Paste the clipboard step as a NEW action node: fresh node id, the copied
      field values ride along (Action ID included — a collision surfaces
      through save validation). Unconnected, like a manually-added step. */
  function pasteStep(at?: Point) {
    const step = readStepClipboard();
    if (!step || view !== "canvas" || hookBacked) return;
    const anchor = at ?? (selected
      ? { x: selected.x + 36, y: selected.y + 36 }
      : { x: 420, y: 260 });
    const type = step.actionType ?? "webhook";
    const label = step.label ?? actionMeta(type)?.label ?? type;
    const next: DiagramShape = {
      id: crypto.randomUUID(),
      type: "node",
      x: anchor.x,
      y: anchor.y,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      label,
      data: {
        nodeKind: "action",
        actionType: type,
        fields: { ...(step.fields ?? {}) },
        ...(step.raw ? { raw: step.raw } : {})
      }
    };
    commitEdit(draftRef.current);
    setShapes((current) => [...current, next]);
    setSelectedId(next.id);
    setStatus({ kind: "ok", message: `Step pasted as "${label}" — connect it, review its settings, then save.` });
  }

  // Editor shortcuts: Ctrl/Cmd+Z / +Shift+Z / Ctrl+Y mirror the toolbar
  // buttons, Ctrl/Cmd+D duplicates the selected step, Ctrl/Cmd+C copies it
  // for pasting into any workflow, Ctrl/Cmd+V pastes a copied step,
  // Delete/Backspace deletes the selection, ? opens the cheat sheet. Text
  // fields keep the browser's native editing keys, the YAML view edits text
  // not shapes, and open dialogs swallow everything but their own Escape.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        if (shortcutsOpen) {
          event.preventDefault();
          setShortcutsOpen(false);
        }
        if (navOpen) {
          event.preventDefault();
          setNavOpen(false);
        }
        return;
      }
      if (leaveOpen || stepsPickerOpen || shortcutsOpen) return;
      const target = event.target;
      if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return;
      // Rich-text hosts (contenteditable) keep the browser's editing keys too.
      if (target instanceof HTMLElement && target.isContentEditable) return;
      if (event.metaKey || event.ctrlKey) {
        if (view !== "canvas") return;
        const key = event.key.toLowerCase();
        if (key === "z") {
          event.preventDefault();
          if (event.shiftKey) redo();
          else undo();
        } else if (key === "y") {
          event.preventDefault();
          redo();
        } else if (key === "v" && readStepClipboard()) {
          event.preventDefault();
          pasteStep();
        } else if (key === "d") {
          // The editor owns Cmd/Ctrl+D on the canvas: duplicate the selected
          // step, and always swallow the key so it never becomes a bookmark.
          event.preventDefault();
          if (selectedId) duplicateStep(selectedId);
        } else if (key === "c" && selectedId) {
          event.preventDefault();
          copyStep(selectedId);
        }
        return;
      }
      if ((event.key === "Delete" || event.key === "Backspace") && selectedId) {
        if (view !== "canvas") return;
        event.preventDefault();
        deleteSelectedShape();
        return;
      }
      if (event.key === "?") {
        event.preventDefault();
        setShortcutsOpen(true);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

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
    // Keep the requested console URL until its workflow has loaded. Reporting
    // the initial blank canvas can otherwise replace the deep link mid-load.
    if (!initialWorkflowLoaded || !config.embedded || window.parent === window) return;
    window.parent.postMessage(
      {
        type: "designer:meta",
        id: workflowId,
        enabled,
        source: sourceName,
        editable: view === "canvas" && !hookBacked,
        dirty
      },
      window.location.origin
    );
  }, [initialWorkflowLoaded, config.embedded, workflowId, enabled, sourceName, view, dirty, hookBacked]);

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
      renameWorkflow(id);
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

  // Autofill: fetch the workflow's own last trigger input so the trigger
  // inspector can offer {trigger.*} chips. Console mode only — the local
  // companion has no admin API; failures just hide the chips.
  useEffect(() => {
    if (config.mode !== "console" || !workflowId || workflowId === "new-workflow") {
      setTriggerSample(null);
      return;
    }
    let cancelled = false;
    const adminBase = config.apiBase.replace(/\/designer$/, "");
    api<{ source?: string; data?: Record<string, unknown> }>(
      config, `/triggers/sample?workflow=${encodeURIComponent(workflowId)}`, undefined, adminBase)
      .then((payload) => {
        if (cancelled) return;
        const data = payload.data && typeof payload.data === "object" ? payload.data : {};
        setTriggerSample({ source: payload.source, fields: Object.keys(data) });
      })
      .catch(() => { if (!cancelled) setTriggerSample(null); });
    return () => { cancelled = true; };
  }, [config, workflowId]);

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
      .catch((error) => setStatus({ kind: "error", message: String(error) }))
      .finally(() => setInitialWorkflowLoaded(true));
    refreshGit();
  }, [refreshGit, refreshList]);

  async function openWorkflow(summary: WorkflowSummary) {
    try {
      // A draft-only workflow (published: false) has no live item to GET —
      // the draft read serves it. A live workflow with a draft also pulls the
      // draft block so the Publish/Discard buttons know what they act on.
      // Hook-backed workflows (summary.source null) have no file at all —
      // the API resolves their id; the response's hook_backed flag turns
      // the canvas read-only.
      const draftOnly = summary.published === false;
      const ref = summary.source || summary.id;
      const data = await api<{ workflow: Workflow; draft?: DraftInfo; hook_backed?: boolean }>(
        config, `/workflows/${encodeURIComponent(ref)}${draftOnly ? "/draft" : ""}`);
      const workflow = data.workflow;
      let draft = data.draft ?? null;
      if (!draftOnly && summary.has_draft && !draft) {
        draft = await api<{ draft: DraftInfo }>(config, `/workflows/${summary.source}/draft`)
          .then((payload) => payload.draft)
          .catch(() => null);
      }
      allowUnload.current = false;
      const shapes = shapesFromWorkflow(workflow);
      const yaml = workflowYaml(workflow);
      setSourceName(summary.source);
      setHookBacked(data.hook_backed === true);
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
      setDraftInfo(draft);
      setSelectedId(null);
      setStepTest({ nodeId: null, busy: false, result: null });
      setStepOutputs({});
      resetHistory();
      setStatus({ kind: "idle", message: "" });
      if (config.mode === "console" && !config.embedded) {
        history.replaceState(null, "", `${window.location.pathname}?workflow=${encodeURIComponent(ref)}`);
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
      data: { nodeKind: "trigger", connector: "email", event: "message.received", filters: [{ field: "route", operator: "equals", value: "" }] }
    };
    setSourceName(null);
    setHookBacked(false);
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
    setDraftInfo(null);
    setSelectedId("trigger");
    setStepTest({ nodeId: null, busy: false, result: null });
    setStepOutputs({});
    resetHistory();
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
      const nextShapes = shapesFromWorkflow(parsed);
      const nextId = typeof parsed.id === "string" && parsed.id.trim() ? parsed.id.trim() : workflowId;
      const nextEnabled = parsed.enabled !== false;
      commitEdit({ shapes, workflowId, enabled });
      setShapes(nextShapes);
      setCanvasExtraDirty(yamlText !== savedYaml);
      setBase(parsed);
      setWorkflowId(nextId);
      setEnabled(nextEnabled);
      setSelectedId(null);
    }
    setTestOpen(false);
    setStepTest({ nodeId: null, busy: false, result: null });
    setView(next);
  }

  async function save(): Promise<boolean> {
    if (hookBacked) {
      setStatus({ kind: "error", message: "This workflow runs from its trigger and is read-only here — duplicate it to edit a copy." });
      return false;
    }
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
      const result = await api<{ commit?: string | null; published?: boolean; revision?: number; draft?: DraftInfo; git_sync_error?: string }>(config, "/workflows", {
        method: "PUT",
        body: JSON.stringify({ yaml: yamlOut, renameFrom: sourceName })
      });
      setBase(workflow);
      setSavedYaml(yamlOut);
      setSavedId(workflow.id);
      setSavedEnabled(workflow.enabled !== false);
      setCanvasExtraDirty(false);
      // A save drafts (G15): the response carries the draft block; publish
      // responses (published: true, from the live verbs) carry no draft.
      setDraftInfo(result.published === false ? (result.draft ?? { base_revision: 0, stale: false }) : null);
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
        const entry = summarize(`${workflow.id}.yaml`, workflow);
        // Reflect the draft state locally so the list badges stay honest
        // without waiting for the next server refresh.
        const previous = current.find((existing) => existing.source === `${workflow.id}.yaml`);
        const published = result.published !== false ? true : previous?.published === true;
        return [...others, {
          ...entry,
          published,
          has_draft: result.published === false || previous?.has_draft === true || undefined,
        }].sort((a, b) => a.source.localeCompare(b.source));
      });
      refreshGit();
      // A save is the new baseline: undo cannot reach past it.
      resetHistory();
      setStatus({
        kind: "ok",
        message: result.published === false
          ? "Draft saved — nothing is live yet. Publish when it is ready."
          : `Saved live. Workflow is ${workflow.enabled === false ? "Off" : "On"}.`
            + (result.git_sync_error ? ` Git sync failed: ${result.git_sync_error}` : "")
      });
      return true;
    } catch (error) {
      setStatus({ kind: "error", message: String(error) });
      return false;
    }
  }

  /** Promotes the saved draft live (POST .../publish): the API runs the
      ordinary publish path — git, version record, YouTube reconcile; a
      draft-only workflow becomes v1. A stale draft (live moved past its
      base) is refused; the API's message says so. */
  async function publishDraft() {
    if (!sourceName || !draftInfo) return;
    if (dirty) {
      setStatus({ kind: "error", message: "Save the draft before publishing it." });
      return;
    }
    setStatus({ kind: "busy", message: "Publishing…" });
    try {
      const result = await api<{ file: string; revision?: number; commit?: string | null; git_sync_error?: string }>(
        config, `/workflows/${encodeURIComponent(sourceName)}/publish`, { method: "POST", body: "{}" });
      setDraftInfo(null);
      await refreshList();
      refreshGit();
      setStatus({
        kind: "ok",
        message: `Published live${result.revision ? ` as v${result.revision}` : ""}.`
          + (result.git_sync_error ? ` Git sync failed: ${result.git_sync_error}` : "")
      });
    } catch (error) {
      setStatus({ kind: "error", message: String(error) });
    }
  }

  /** Throws the saved draft away (DELETE .../draft); live is untouched. */
  async function discardDraft() {
    if (!sourceName || !draftInfo) return;
    if (!window.confirm("Discard the saved draft? The drafted edits are lost; the live workflow is untouched.")) return;
    setStatus({ kind: "busy", message: "Discarding…" });
    try {
      await api(config, `/workflows/${encodeURIComponent(sourceName)}/draft`, { method: "DELETE" });
      setDraftInfo(null);
      await refreshList();
      setStatus({ kind: "ok", message: "Draft discarded — live is untouched." });
    } catch (error) {
      setStatus({ kind: "error", message: String(error) });
    }
  }

  /** Revert: throw unsaved edits away and reload the workflow as the server
      has it — the saved draft when one exists, else the live version. A
      never-saved workflow goes back to the blank new canvas. A client-side
      reload only: no server state changes, unlike Discard draft. */
  async function revertChanges() {
    if (!dirty || status.kind === "busy") return;
    if (!sourceName) {
      newWorkflow();
      return;
    }
    if (!window.confirm("Revert unsaved changes? The canvas goes back to the last saved state.")) return;
    const summary = summaries.find((item) => item.source === sourceName) ?? {
      id: workflowId,
      enabled: true,
      source: sourceName,
      connector: "",
      event: "",
      actionCount: 0,
      published: !draftInfo,
      has_draft: !!draftInfo,
    };
    await openWorkflow(summary);
  }

  async function leaveAfterSave() {
    if (await save()) resolveLeave(true);
  }

  async function openWorkflowSafely(summary: WorkflowSummary) {
    // Same-workflow check: saved flows match on file, hook-backed ones on id
    // (they have no source file).
    const same = summary.source
      ? summary.source === sourceName
      : !summary.source && summary.id === workflowId;
    if (same || !(await askToLeave())) return;
    await openWorkflow(summary);
  }

  async function newWorkflowSafely() {
    if (!(await askToLeave())) return;
    newWorkflow();
  }

  /** Copies the saved workflow under a new id (server slugifies the name,
     default `<id>-copy`) without touching this draft; the list then shows
     both. The console's duplicate route commits and publishes like a save.
     A hook-backed workflow duplicates by id — the copy is an ordinary
     managed workflow the trigger does not own. */
  async function duplicateWorkflow() {
    const ref = sourceName || (hookBacked ? workflowId : null);
    if (!ref) return;
    const name = window.prompt("Duplicate workflow as (blank for the suggested name):", `${workflowId}-copy`);
    if (name === null) return;
    const trimmed = name.trim();
    setStatus({ kind: "busy", message: "Duplicating…" });
    try {
      const result = await api<{ file: string; published?: boolean; commit?: string | null }>(
        config, `/workflows/${encodeURIComponent(ref)}/duplicate`, {
          method: "POST",
          body: JSON.stringify(trimmed ? { name: trimmed } : {})
        });
      await refreshList();
      refreshGit();
      setStatus({
        kind: "ok",
        message: result.published === false
          ? `Duplicated as ${result.file}; goes live after deployment.`
          : `Duplicated as ${result.file}.`
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

  /** Test the unsaved canvas against a sample event (dry-run unless execute;
     strict fails dry-run steps whose rendered inputs trip the field rules). */
  async function runTest(execute: boolean, strict: boolean) {
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
        body: JSON.stringify({ event: sample, workflow, execute, ...(strict ? { strict: true } : {}) })
      }));
    } catch (error) {
      setTestResult({ mode, matched: false, steps: [], error: String(error) });
    } finally {
      setTestBusy(false);
    }
  }

  /** Pull a real sample event for the canvas's trigger connector into the
     test panel (Zapier's "pull in sample data"): live from the connected
     account, else the newest recorded run, else a documented example. */
  async function pullSample() {
    const trigger = shapes.find((shape) => shape.data?.nodeKind === "trigger")?.data;
    if (!trigger?.connector) {
      setStatus({ kind: "error", message: "Add a trigger node first — the sample is pulled for its connector." });
      return;
    }
    // Sample discovery answers on /api/admin, not /api/admin/designer — the
    // same base override the connection-field browser uses.
    const adminBase = config.apiBase.replace(/\/designer$/, "");
    setSampleBusy(true);
    try {
      const pulled = await api<{ sample: { data?: unknown } & Record<string, unknown>; source?: string }>(
        config, "/discover", {
          method: "POST",
          body: JSON.stringify({
            connector: trigger.connector,
            event: trigger.event || undefined,
            connection_id: trigger.fields?.connection_id || undefined,
          })
        }, adminBase);
      const sample = pulled.sample ?? {};
      const data = sample.data;
      setTestEvent(JSON.stringify(
        data && typeof data === "object" && Object.keys(data as object).length > 0 ? data : sample,
        null, 2));
      setStatus({ kind: "ok", message: `Sample pulled for ${trigger.connector} (${pulled.source ?? "discovered"}).` });
    } catch (error) {
      setStatus({ kind: "error", message: `Sample pull failed: ${String(error)}` });
    } finally {
      setSampleBusy(false);
    }
  }

  /** Test just the selected action step against the test panel's sample
     event (Zapier's per-step "Test step"). "Run step" executes it for real —
     side effects limited to this one step — and its output joins the steps
     context, so the next step's test sees it. The current draft goes inline:
     the server tests exactly what would be saved. */
  async function testSelectedStep(execute: boolean) {
    if (!selected || selected.type !== "node" || !selected.data || selected.data.nodeKind !== "action") return;
    const nodeId = selected.id;
    const actionId = (selected.data.fields?.id ?? "").trim();
    if (!actionId) {
      setStatus({ kind: "error", message: "Give this action an Action ID first — the step test targets it." });
      return;
    }
    const { workflow, problems } = workflowFromShapes(shapes, workflowId, enabled);
    if (problems.length) {
      setStatus({ kind: "error", message: problems.join(" ") });
      return;
    }
    let sample: unknown;
    try {
      sample = JSON.parse(testEvent);
    } catch {
      setTestOpen(true);
      setStepTest({ nodeId, busy: false, result: { mode: "test-step", matched: false, steps: [], error: "The sample event is not valid JSON — fix it in the Test run panel." } });
      return;
    }
    if (!sample || typeof sample !== "object" || Array.isArray(sample)) {
      setTestOpen(true);
      setStepTest({ nodeId, busy: false, result: { mode: "test-step", matched: false, steps: [], error: "The sample event must be a JSON object — fix it in the Test run panel." } });
      return;
    }
    if (execute && !window.confirm(`Run the ${selected.data.actionType} step for real? It acts with live side effects.`)) return;
    // Prior steps' outputs: everything tested for real so far, overlaid with
    // the last full test run's outputs (the fresher whole-chain picture).
    const steps: Record<string, { status: string; output?: Record<string, unknown>; error?: string }> = { ...stepOutputs };
    for (const step of testResult?.steps ?? []) {
      if (step.action_id && (step.output || step.error)) {
        steps[step.action_id] = {
          status: step.ok ? "completed" : "failed",
          ...(step.output ? { output: step.output } : {}),
          ...(step.error ? { error: step.error } : {})
        };
      }
    }
    setStepTest({ nodeId, busy: true, result: null });
    try {
      const result = await api<TestRunResult>(config, "/workflows/test-step", {
        method: "POST",
        body: JSON.stringify({
          action_id: actionId, event: sample, workflow, execute,
          ...(Object.keys(steps).length ? { steps } : {})
        })
      });
      setStepTest({ nodeId, busy: false, result });
      if (execute && result.ok && !result.error) {
        const output = result.steps[0]?.output;
        setStepOutputs((current) => ({
          ...current,
          [actionId]: { status: "completed", ...(output ? { output } : {}) }
        }));
      }
    } catch (error) {
      setStepTest({ nodeId, busy: false, result: { mode: execute ? "execute-step" : "test-step", matched: false, steps: [], error: String(error) } });
    }
  }

  /** Click-to-insert a {trigger.field} chip: copies the template so it can
     be pasted into any action field; without clipboard access the template
     itself lands in the status line, still readable and copyable. */
  async function copyTemplate(template: string) {
    try {
      await navigator.clipboard.writeText(template);
      setStatus({ kind: "ok", message: `Copied ${template} — paste it into any action template.` });
    } catch {
      setStatus({ kind: "ok", message: template });
    }
  }

  function updateSelected(mutate: (data: NodeData) => NodeData) {
    if (!selectedId) return;
    // Coalesced under the node's key: field edits arrive per keystroke, and
    // one undo step per burst beats one per character.
    editShapes((current) => current.map((shape) => {
      if (shape.id !== selectedId || !shape.data) return shape;
      const data = mutate(shape.data);
      const label = data.nodeKind === "trigger"
        ? `${connectorLabel(data.connector ?? "custom")} · ${data.event}`
        : shape.label;
      return { ...shape, data, label };
    }), `node:${selectedId}`);
  }

  const selected = shapes.find((shape) => shape.id === selectedId) ?? null;

  const triggerNodes = shapes.filter((shape) => shape.type === "node" && shape.data?.nodeKind === "trigger");
  /** The steps that run before the selected one, in save order, with the ids
     a save would write and each step's last real test output — the
     insert-from-previous picker's rows. */
  const priorSteps: PriorStep[] = (() => {
    const chain = orderedActionNodes(shapes);
    const at = chain.findIndex((node) => node.id === selectedId);
    if (at <= 0) return [];
    return chain
      .map((node, index) => {
        // The id here must match what a save writes, including the
        // action-<n> fallback for steps whose Action ID is blank.
        const id = (node.data?.fields?.id ?? "").trim() || `action-${index + 1}`;
        const output = stepOutputs[id]?.output;
        return {
          id,
          label: node.label || node.data?.actionType || id,
          ...(output ? { output } : {})
        };
      })
      .slice(0, at);
  })();
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
          {triggerSample && triggerSample.fields.length > 0 && (
            <section className="inspector-group">
              <h3>Sample fields</h3>
              <p className="inspector-hint">
                From this workflow's last trigger{triggerSample.source ? ` (${triggerSample.source})` : ""} —
                click to copy a field, paste it into any action template.
              </p>
              <div className="template-chips">
                {triggerSample.fields.map((field) => (
                  <button
                    key={field}
                    type="button"
                    className="template-chip"
                    title={`Copy {trigger.${field}}`}
                    onClick={() => copyTemplate(`{trigger.${field}}`)}
                  >
                    {`{trigger.${field}}`}
                  </button>
                ))}
              </div>
            </section>
          )}
          <section className="inspector-group">
            <h3>Filters</h3>
            {data.connector === "email" && data.event === "message.received" && (
              <p className="inspector-hint">Use route equals with the address name, such as invoice, or route in with a JSON list of names. Actions belong to this workflow. To deliberately share addresses with another workflow, set allow_email_overlap: true in YAML.</p>
            )}
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
                  placeholder={rule.operator === "in" ? '["invoice", "receipts"]' : "value"}
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
                fields={data.fields}
                siblingFields={meta.fields}
                config={config}
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
        {/* Template fields read {steps.<id>.output…} from earlier steps; the
           picker lists them with their tested outputs, click-to-copy like the
           trigger's sample chips. Console only, like those chips: local mode
           has no test runs, so outputs would never be filled in. */}
        {config.mode === "console" && priorSteps.length > 0 && (
          <section className="inspector-group">
            <h3>Insert from previous steps</h3>
            <p className="inspector-hint">
              Earlier steps&rsquo; outputs as {"{steps.*}"} templates — click one,
              paste it into any template field.
            </p>
            <button className="dk-button dk-button--secondary" type="button" onClick={() => setStepsPickerOpen(true)}>
              <span>Browse step outputs…</span>
            </button>
          </section>
        )}
        {meta && (
          <section className="inspector-group">
            <h3>Error handling</h3>
            {/* on_fail and on_error are mutually exclusive on one step
               (engine/logic.py); picking a real policy for one clears the
               other, so a save can never carry both. */}
            <FieldInput
              field={onErrorField}
              value={data.fields?.on_error ?? ""}
              onChange={(value) => updateSelected((current) => ({
                ...current,
                fields: { ...current.fields, on_error: value, ...(value && value !== "halt" ? { on_fail: "" } : {}) }
              }))}
            />
            <FieldInput
              field={onFailField}
              value={data.fields?.on_fail ?? ""}
              onChange={(value) => updateSelected((current) => ({
                ...current,
                fields: { ...current.fields, on_fail: value, ...(value === "continue" ? { on_error: "" } : {}) }
              }))}
            />
            {((data.fields?.on_error ?? "") === "run" ||
              (data.fields?.error_actions ?? "").trim() !== "") && (
              <FieldInput
                field={errorActionsField}
                value={data.fields?.error_actions ?? ""}
                onChange={(value) => setField("error_actions", value)}
              />
            )}
            <p className="inspector-hint">
              What a failed step does. On error: halt (the default) fails the run,
              continue records the failure and moves on, run also executes the
              error steps. On fail: continue absorbs one failure — the step reads
              skipped in run history. A handled failure is visible to later steps
              as {"{steps.<id>.error}"}.
            </p>
          </section>
        )}
        {/* Console only, like the Test run panel: local mode has no admin
           API to run the step against. */}
        {config.mode === "console" && (
          <section className="inspector-group">
            <h3>Test step</h3>
            <p className="inspector-hint">
              Runs just this step against the Test run panel&rsquo;s sample event.
              &ldquo;Run step&rdquo; executes it for real — side effects limited to
              this step — and its output feeds the next step&rsquo;s test.
            </p>
            <div className="test-actions">
              <button className="dk-button dk-button--secondary" type="button" disabled={stepTest.busy}
                      onClick={() => testSelectedStep(false)}>
                {stepTest.busy ? <Loader2 size={16} strokeWidth={1.5} className="spin" /> : <FlaskConical size={16} strokeWidth={1.5} />}
                <span>Dry</span>
              </button>
              <button className="dk-button dk-button--danger" type="button" disabled={stepTest.busy}
                      onClick={() => testSelectedStep(true)}>
                <Play size={16} strokeWidth={1.5} /><span>Run step</span>
              </button>
            </div>
            {stepTest.nodeId === selected.id && stepTest.result && (
              <div className={`test-result ${stepTest.result.error && stepTest.result.steps.length === 0 ? "failed" : stepTest.result.ok ? "passed" : ""}`}>
                <p className="test-summary">
                  {stepTest.result.error && stepTest.result.steps.length === 0
                    ? stepTest.result.error
                    : <strong>{stepTest.result.ok ? "Step ok" : "Step failed"}
                        {stepTest.result.mode === "test-step" ? " (dry)" : ""}</strong>}
                </p>
                {stepTest.result.steps.map((step, index) => (
                  <div key={`${step.action_id}-${index}`} className={step.ok ? "test-step ok" : "test-step failed"}>
                    <span className="test-step-title">
                      {step.action_id} <em>({step.action_type || "?"})</em>
                      {!step.ok && <span className="test-step-error"> — {step.error}</span>}
                    </span>
                    {(step.warnings ?? []).map((warning) => (
                      <span key={warning} className="test-step-error">warning: {warning}</span>
                    ))}
                    {step.rendered_input != null && (
                      <pre className="test-io">{JSON.stringify(step.rendered_input, null, 2)}</pre>
                    )}
                    {step.output != null && (
                      <pre className="test-io">output: {JSON.stringify(step.output, null, 2)}</pre>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>
        )}
      </>
    );
  };

  return (
    <div className={config.embedded ? "designer-shell embedded" : "designer-shell"}>
      {navOpen && !config.embedded && (
        <div className="sidebar-scrim" role="presentation" onClick={() => setNavOpen(false)} />
      )}
      {!config.embedded && (
        <aside className={navOpen ? "designer-sidebar open" : "designer-sidebar"}>
        {config.mode === "console" ? (
          <a className="brand brand-link" href="/"><span className="workspace-mark" aria-hidden="true">D</span><span>← Console · Designer</span></a>
        ) : (
          <div className="brand"><span className="workspace-mark" aria-hidden="true">D</span><span>Workflow designer</span></div>
        )}
        <button className="dk-button dk-button--secondary sidebar-action" type="button" onClick={newWorkflowSafely}>
          <span>New workflow</span>
        </button>
        <nav className="workflow-nav" aria-label="Workflows">
          {summaries.map((summary) => (
            <button
              key={summary.source}
              className={summary.source === sourceName ? "workflow-item active" : "workflow-item"}
              aria-current={summary.source === sourceName ? "page" : undefined}
              onClick={() => { setNavOpen(false); openWorkflowSafely(summary); }}
              type="button"
            >
              <WorkflowIcon size={16} strokeWidth={1.5} />
              <span className="workflow-text">
                <span className="workflow-name">{summary.id}</span>
                <span className="workflow-meta">
                  <TriggerLogo connector={summary.connector} />
                  {connectorLabel(summary.connector)}/{summary.event} · {summary.actionCount} action{summary.actionCount === 1 ? "" : "s"}
                </span>
              </span>
              {!summary.enabled && <span className="workflow-disabled state-off">Off</span>}
              {summary.published === false && <span className="workflow-disabled state-draft">Draft</span>}
              {summary.published !== false && summary.has_draft && <span className="workflow-disabled state-draft">Edited</span>}
            </button>
          ))}
          {summaries.length === 0 && <p className="inspector-hint">No workflows found.</p>}
        </nav>
        {config.mode === "local" && git && (
          <div className="git-foot">
            <GitBranch size={16} strokeWidth={1.5} />
            <span>{git.branch}</span>
            <span className={git.dirty ? "git-dirty" : "git-clean"}>{git.dirty ? "unsaved changes" : "clean"}</span>
            {(git.ahead > 0 || git.behind > 0) && <span>{git.ahead}↑ {git.behind}↓</span>}
          </div>
        )}
        </aside>
      )}

      <main className="designer-main">
        <header className="designer-topbar">
          {!config.embedded && (
            <button
              className="icon-button nav-toggle"
              type="button"
              aria-label="Toggle workflow list"
              aria-expanded={navOpen}
              onClick={() => setNavOpen((open) => !open)}
            >
              <Menu size={16} strokeWidth={1.5} />
            </button>
          )}
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
                disabled={view === "yaml" || hookBacked}
                title={hookBacked ? "Runs from its trigger — read-only" : view === "yaml" ? "Edit the id in the YAML view" : undefined}
                onChange={(event) => renameWorkflow(event.target.value)}
              />
            )}
            {view === "canvas" && triggerNodes.length === 0 && (
              <span className="save-problems">Add a trigger node to save.</span>
            )}
          </div>
          <div className="topbar-actions">
            <label
              className="check-label enabled-toggle"
              title={view === "yaml" ? "Edit the on/off state in YAML" : "Applies when you save"}
            >
              <input
                type="checkbox"
                checked={enabled}
                disabled={view === "yaml" || hookBacked}
                title={hookBacked ? "Runs from its trigger — the on/off switch lives in Triggers" : undefined}
                onChange={(event) => toggleEnabled(event.target.checked)}
                aria-label="Workflow state after saving"
              />
              {enabled ? "On" : "Off"}
            </label>
            {enabled !== savedEnabled && <span className="state-save-hint">Save to apply</span>}
            {status.message && (
              <span className={`status-message ${status.kind}`}>
                {status.kind === "busy" && <Loader2 size={16} strokeWidth={1.5} className="spin" />}
                {status.kind === "error" && <TriangleAlert size={16} strokeWidth={1.5} />}
                {status.message}
              </span>
            )}
            {config.mode === "local" && (
              <button className="dk-button dk-button--secondary" type="button" onClick={push} disabled={!git || git.ahead === 0}>
                <span>Push {git && git.ahead > 0 ? `(${git.ahead})` : ""}</span>
              </button>
            )}
            {config.mode === "console" && (
              <button
                className="dk-button dk-button--secondary"
                type="button"
                onClick={duplicateWorkflow}
                disabled={status.kind === "busy" || (!sourceName && !hookBacked)}
                title={hookBacked
                  ? "Copy this trigger-run workflow under a new id — the copy is an ordinary editable workflow"
                  : !sourceName ? "Save the workflow first — duplicates copy the saved file" : undefined}
              >
                <span>Duplicate</span>
              </button>
            )}
            {config.mode === "console" && (
              <button
                className={testOpen ? "button secondary active" : "dk-button dk-button--secondary"}
                type="button"
                onClick={() => setTestOpen(!testOpen)}
                disabled={status.kind === "busy" || view === "yaml"}
                title={view === "yaml" ? "Switch to Canvas to test this workflow" : undefined}
              >
                <span>Test run</span>
              </button>
            )}
            {config.mode === "console" && (
              <button
                className="dk-button dk-button--secondary"
                type="button"
                onClick={publishDraft}
                disabled={status.kind === "busy" || !sourceName || !draftInfo || dirty}
                title={!draftInfo
                  ? "Save the workflow first — a save writes a draft"
                  : dirty
                    ? "Save the draft before publishing it"
                    : draftInfo.stale
                      ? `The live workflow moved past this draft (based on v${draftInfo.base_revision}) — publishing will refuse it until you save again`
                      : `Publish the draft live (based on v${draftInfo.base_revision})`}
              >
                <span>Publish draft</span>
              </button>
            )}
            {config.mode === "console" && draftInfo && (
              <button
                className="dk-button dk-button--secondary"
                type="button"
                onClick={discardDraft}
                disabled={status.kind === "busy"}
                title="Throw the saved draft away — the live workflow is untouched"
              >
                <span>Discard draft</span>
              </button>
            )}
            <button
              className="dk-button dk-button--secondary"
              type="button"
              onClick={revertChanges}
              disabled={hookBacked || status.kind === "busy" || !dirty}
              title={hookBacked ? "Runs from its trigger — read-only" : "Throw unsaved changes away — back to the last saved state"}
            >
              <span>Revert</span>
            </button>
            <button
              className={dirty ? "dk-button dk-button--primary" : "dk-button dk-button--secondary"}
              type="button"
              onClick={save}
              disabled={hookBacked || status.kind === "busy" || Object.keys(invalidRawDrafts).length > 0}
              title={hookBacked ? "Runs from its trigger — read-only" : undefined}
            >
              <span>{hookBacked ? "Read-only" : Object.keys(invalidRawDrafts).length ? "Fix JSON to save" : dirty ? (draftInfo ? "Save draft" : "Save changes") : "Saved"}</span>
            </button>
          </div>
        </header>

        {hookBacked && (
          <div className="read-only-note" role="note">
            This workflow runs from its trigger (webhook/telegram/…) — the designer shows it read-only.
            Edit the trigger in Triggers, or Duplicate it to edit an editable copy.
          </div>
        )}

        {view === "yaml" ? (
          <div className="yaml-editor">
            <div className="yaml-editor-bar">
              <span className="mono-file">{hookBacked ? workflowId : `workflows/${sourceName ?? `${workflowId}.yaml`}`}</span>
              <span className="yaml-hint">comments are not preserved on save</span>
            </div>
            <textarea
              className="yaml-text"
              value={yamlText}
              spellCheck={false}
              aria-label="Workflow YAML"
              readOnly={hookBacked}
              onChange={(event) => setYamlText(event.target.value)}
            />
          </div>
        ) : (
        <div className="designer-body">
          <WorkflowBoard
            shapes={shapes}
            setShapes={hookBacked ? noopSetShapes : setShapes}
            editShapes={editShapes}
            commitDrag={commitDrag}
            selectedId={selectedId}
            setSelectedId={setSelectedId}
            canPasteStep={clipboardHasStep}
            onDuplicateStep={duplicateStep}
            onCopyStep={copyStep}
            onPasteStep={pasteStep}
            sessionControls={(actions) => (
              <>
                <button className="icon-button" disabled={!canUndo} onClick={undo} title="Undo (Ctrl+Z)" type="button"><RotateCcw size={16} strokeWidth={1.5} /></button>
                <button className="icon-button" disabled={!canRedo} onClick={redo} title="Redo (Ctrl+Shift+Z)" type="button"><RotateCw size={16} strokeWidth={1.5} /></button>
                <button className="icon-button" onClick={() => setShortcutsOpen(true)} title="Keyboard shortcuts (?)" type="button">
                  <Keyboard size={16} strokeWidth={1.5} />
                </button>
                <button className="icon-button" onClick={actions.clearCanvas} title="Clear canvas" type="button"><X size={16} strokeWidth={1.5} /></button>
              </>
            )}
          />
          {testOpen && (
            <section className="test-panel" aria-label="Test run">
              <header className="test-panel-head">
                <h2>Test run</h2>
                <button className="icon-button" type="button" title="Close" onClick={() => setTestOpen(false)}>
                  <X size={16} strokeWidth={1.5} />
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
                <button className="dk-button dk-button--primary" type="button" disabled={testBusy} onClick={() => runTest(false, testStrict)}>
                  {testBusy ? <Loader2 size={16} strokeWidth={1.5} className="spin" /> : <FlaskConical size={16} strokeWidth={1.5} />}
                  <span>Dry run</span>
                </button>
                <button className="dk-button dk-button--danger" type="button" disabled={testBusy} onClick={() => runTest(true, testStrict)}>
                  <Play size={16} strokeWidth={1.5} /><span>Run for real</span>
                </button>
                <button
                  className="dk-button dk-button--secondary"
                  type="button"
                  disabled={sampleBusy || testBusy}
                  onClick={pullSample}
                  title="Pull a real sample event for this workflow's trigger connector"
                >
                  {sampleBusy ? <Loader2 size={16} strokeWidth={1.5} className="spin" /> : <CloudDownload size={16} strokeWidth={1.5} />}
                  <span>Pull sample</span>
                </button>
                <label
                  className="check-label"
                  title="Dry run only: rendered-input warnings (an empty required field, a value implausible for its type) fail their step instead of riding along"
                >
                  <input
                    type="checkbox"
                    checked={testStrict}
                    onChange={(event) => setTestStrict(event.target.checked)}
                    aria-label="Strict test run"
                  />
                  Strict
                </label>
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
                          {testResult.enabled === false && <span> This workflow is Off.</span>}
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
            {selected?.type === "node" && (
              <button className="icon-button inspector-close" type="button" aria-label="Close panel" onClick={() => setSelectedId(null)}>
                <X size={16} strokeWidth={1.5} />
              </button>
            )}
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
            {selected?.type === "node" && selected.data?.nodeKind === "action" && (
              <div className="inspector-toolbar">
                <button className="dk-button dk-button--secondary" type="button" onClick={() => duplicateStep(selected.id)} title="Duplicate this step (Ctrl/Cmd+D)">
                  <Copy size={16} strokeWidth={1.5} /><span>Duplicate</span>
                </button>
                <button className="dk-button dk-button--secondary" type="button" onClick={() => copyStep(selected.id)} title="Copy for pasting into any workflow (Ctrl/Cmd+C, then Ctrl/Cmd+V)">
                  <ClipboardCopy size={16} strokeWidth={1.5} /><span>Copy step</span>
                </button>
                <button className="dk-button dk-button--danger" type="button" onClick={deleteSelectedShape} title="Delete this step (Delete)">
                  <Trash2 size={16} strokeWidth={1.5} /><span>Delete</span>
                </button>
              </div>
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
              <button className="dk-button dk-button--secondary" type="button" onClick={() => resolveLeave(false)} autoFocus>Stay</button>
              <button className="dk-button dk-button--secondary" type="button" onClick={() => resolveLeave(true)}>Discard changes</button>
              <button className="dk-button dk-button--primary" type="button" disabled={status.kind === "busy" || Object.keys(invalidRawDrafts).length > 0} onClick={leaveAfterSave}>Save and continue</button>
            </div>
          </section>
        </div>
      )}
      {stepsPickerOpen && (
        <StepsTemplatePicker
          steps={priorSteps}
          onPick={(template) => {
            setStepsPickerOpen(false);
            void copyTemplate(template);
          }}
          onClose={() => setStepsPickerOpen(false)}
        />
      )}
      {shortcutsOpen && (
        <div className="picker-backdrop" role="presentation" onClick={() => setShortcutsOpen(false)}>
          <section
            className="picker-panel shortcuts-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="shortcuts-title"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 id="shortcuts-title">Keyboard shortcuts</h2>
            <div className="shortcut-list">
              {SHORTCUTS.map((shortcut) => (
                <div className="shortcut-row" key={shortcut.description}>
                  <span className="shortcut-keys">
                    {shortcut.keys.map((key) => <kbd key={key}>{key}</kbd>)}
                  </span>
                  <span className="shortcut-desc">{shortcut.description}</span>
                </div>
              ))}
            </div>
            <p className="picker-status">Double-click a node to rename it; drag from a handle to connect steps.</p>
          </section>
        </div>
      )}
    </div>
  );
}
