import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useId } from "react";
import { flushSync } from "react-dom";
import { ClipboardCopy, CloudDownload, Copy, FlaskConical, GitBranch, GitMerge, Keyboard, Loader2, Menu, Play, RotateCcw, RotateCw, Trash2, TriangleAlert, Workflow as WorkflowIcon, X } from "./icons";
import { dump, load } from "js-yaml";
import { WorkflowBoard } from "./board/WorkflowBoard";
import { nodeTitle, nodeSubtitle, paletteLabel, type PaletteKind } from "./board/nodes";
import { StepPicker } from "./board/StepPicker";
import { actionCatalog, connectorCatalog, errorActionsField, eventInfo, filterOperators, onErrorField, onFailField } from "./catalog";
import { NODE_HEIGHT, NODE_WIDTH, actionMeta, connectorLabel, connectorMeta, decisionMode, defaultFields, effectiveActionId, extractNodeBranches, extractPredicateRules, filterRulesToYaml, isBranchingAction, orderedActionNodes, orderedWorkflow, parseBranchSteps, rebuildShapesFromBranchEdit, shapesFromWorkflow, summarize, syncBranchFieldsFromCanvas, triggerLabel, workflowFromShapes } from "./workflows";
import type { CatalogField } from "./catalog";
import { localConfig, type DesignerConfig } from "./config";
import type { CodeTestReport, ConnectionOption, DiagramShape, DraftInfo, FilterRule, GitStatus, NodeData, Point, TestRunResult, Workflow, WorkflowSummary } from "./types";
import { StoredDataPanel, listStoredItems, workflowUsesStorage, type StoredItem } from "./StoredData";
import { initHistory, pushHistory, undoHistory, redoHistory, type DraftSnapshot, type HistoryState } from "./history";
import "../../src/web/vendor/dialogs.js";
import "../../src/web/vendor/tabs.js";

/** dakit's load-before-open helper (vendor/dialogs.js, docs/dialog-loading.md). */
declare global {
  var dakitDialogs: {
    openWhenReady<T>(
      trigger: Element,
      load: (signal: AbortSignal) => T | Promise<T>,
      render: (data: T, signal: AbortSignal) => HTMLDialogElement | void | Promise<HTMLDialogElement | void>,
      options?: { label?: string; spinner?: "inline" | "overlay"; errorMessage?: string | ((error: Error) => string) },
    ): Promise<unknown>;
  };
}

const EMPTY_SHAPES: DiagramShape[] = [];
/** The id a never-saved canvas carries until the API assigns one from its name. */
const NEW_WORKFLOW_ID = "new-workflow";

/** Open a native <dialog> as a modal on mount. Backdrop and Escape dismissal
    come from the vendored dakit helper; the caller unmounts on close. */
function useMountedModal() {
  const ref = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && !el.open) el.showModal();
  }, []);
  return ref;
}
/** engine.naming.MAX_NAME_LENGTH: the longest `name:` override the API accepts. */
const MAX_NAME_LENGTH = 80;

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

/** The "<service> <account>" a flow writes for this connection: the
    reference whose service matches the step (drive_find_file → drive), else
    its first. Never the internal connection_id unless nothing else exists
    (a connection that never finished sign-in has no account to name). */
function connectionRef(connection: ConnectionOption, nodeType?: string, others: ConnectionOption[] = []): string {
  const refs = connection.refs ?? [];
  const service = (nodeType ?? "").split("_")[0];
  const ref = refs.find((candidate) => candidate.split(" ")[0] === service) ?? refs[0];
  // A reference another connection also answers to would be ambiguous at
  // run time; the internal id is the only safe value then.
  const shared = ref && others.some((other) => other.connection_id !== connection.connection_id
    && (other.refs ?? []).includes(ref));
  return ref && !shared ? ref : connection.connection_id;
}

/** Whether a field value names this connection: its id or any reference. */
function connectionMatches(connection: ConnectionOption, value: string): boolean {
  const wanted = value.trim().toLowerCase();
  return connection.connection_id === wanted
    || (connection.refs ?? []).some((ref) => ref.toLowerCase() === wanted);
}

/** "<title> · <summary>" for a node, matching its canvas card. */
function nodeListLabel(shape: DiagramShape): string {
  const summary = nodeSubtitle(shape).replace(/\s+/g, " ").trim();
  const clipped = summary.length > 48 ? `${summary.slice(0, 47)}…` : summary;
  return [nodeTitle(shape), clipped].filter(Boolean).join(" · ");
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
      <dialog ref={dialog} className="dk-dialog prompt-editor-dialog" aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-hint`} onCancel={close} onClose={close}
        onKeyDown={event => event.stopPropagation()}>
        <div className="dk-dialog__head">
          <h2 id={`${id}-title`}>Edit {field.label.toLowerCase()}</h2>
          <button type="button" className="dk-button dk-button--secondary" aria-label="Close prompt editor" onClick={close}><X size={20} strokeWidth={1.8} /></button>
        </div>
        <div className="dk-dialog__body">
          <p id={`${id}-hint`} className="prompt-editor-hint">Changes apply to this step. Save the workflow when you’re ready.</p>
          <label className="prompt-editor-label" htmlFor={`${id}-expanded`}>{field.label}</label>
          <textarea id={`${id}-expanded`} ref={expanded} className="prompt-expanded" value={draft}
            placeholder={field.placeholder} spellCheck onChange={event => setDraft(event.target.value)} />
        </div>
        <div className="dk-dialog__foot">
          <button className="dk-button dk-button--secondary" type="button" onClick={close}>Cancel</button>
          <button className="dk-button dk-button--primary" type="button" onClick={() => { onChange(draft); close(); }}>Apply to step</button>
        </div>
      </dialog>
    </div>
  );
}

/** One catalog field, rendered per its declared type. */
function FieldInput({ field, value, onChange, connections, fields, siblingFields, config, nodeType }: {
  field: CatalogField;
  value: string;
  onChange: (value: string) => void;
  connections?: ConnectionOption[] | null;
  /** Sibling field values of the selected step — discovery's query params. */
  fields?: Record<string, string>;
  /** Sibling field definitions, for the discovery hint's label. */
  siblingFields?: CatalogField[];
  config?: DesignerConfig;
  /** The step's action type, which picks the service in a connection reference. */
  nodeType?: string;
}) {
  // The Browse… picker's items: null while closed. The picker mounts only
  // once its items are in (load before open).
  const [pickerItems, setPickerItems] = useState<DiscoveryItem[] | null>(null);
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
  // written to the YAML is the "<service> <account>" reference (drive
  // alexey@…), which the engine resolves at run time. Existing internal ids
  // still match, and free text still works for anything the snapshot does
  // not know (offline mode, brand-new records).
  if (field.provider && connections) {
    const matches = connections.filter((connection) => connection.provider === field.provider);
    const current = matches.find((connection) => connectionMatches(connection, value));
    return (
      <label>{field.label}{field.required ? " *" : ""}
        <input
          className="mono-input"
          list={`connections-${field.key}`}
          value={value}
          placeholder={matches.length === 1 && !value ? connectionRef(matches[0], nodeType, matches) : field.placeholder}
          onChange={(event) => onChange(event.target.value)}
        />
        <datalist id={`connections-${field.key}`}>
          {matches.map((connection) => (
            <option key={connection.connection_id} value={connectionRef(connection, nodeType, matches)}>
              {connectionHint(connection)}
            </option>
          ))}
        </datalist>
        {value !== "" && current && <span className="connection-hint">{connectionHint(current)}</span>}
        {value !== "" && !current && (
          <span className="connection-hint warn">
            Not one of your {field.provider} connections — pick one from the list.
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
    // The field may hold a "<service> <account>" reference; discovery's
    // route wants the connection's internal id.
    const named = (fields?.[fromKey] ?? "").trim();
    const account = discover.account
      ?? connections?.find((connection) => connectionMatches(connection, named))?.connection_id
      ?? named;
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
            <button
              className="dk-button quiet"
              type="button"
              disabled={!account}
              onClick={(event) => {
                const resource = discover.resource.replace(/_/g, " ");
                const path = discoveryPath(discover, account, fields ?? {});
                void dakitDialogs.openWhenReady(
                  event.currentTarget,
                  (signal) => api<{ items: DiscoveryItem[] }>(config, path, { signal }, adminBase(config))
                    .then((data) => data.items ?? []),
                  // The picker mounts and opens itself (useMountedModal) inside flushSync.
                  (items) => { flushSync(() => setPickerItems(items)); },
                  { label: `Loading ${resource}…`, errorMessage: (error) => `Couldn't load ${resource}: ${error.message}` },
                );
              }}
            >
              Browse…
            </button>
          </span>
          {!account && (
            <span className="connection-hint">
              Set {fromLabel ?? "Connection ID"} first
            </span>
          )}
        </label>
        {pickerItems && (
          <DiscoveryPicker
            discover={discover}
            items={pickerItems}
            onPick={(picked) => {
              setPickerItems(null);
              onChange(picked);
            }}
            onClose={() => setPickerItems(null)}
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

/** Structured multi-rule AND predicate editor for Condition, Filter, and Paths. */
function RuleBuilder({
  rules,
  onChange,
  addLabel = "Add rule (AND)"
}: {
  rules: FilterRule[];
  onChange: (nextRules: FilterRule[]) => void;
  addLabel?: string;
}) {
  const effectiveRules: FilterRule[] = rules.length > 0
    ? rules
    : [{ field: "", operator: "equals", value: "" }];
  return (
    <div className="rule-builder">
      {effectiveRules.map((rule, index) => (
        <div key={index} className="filter-row">
          <input
            className="mono-input"
            placeholder="field (e.g. subject)"
            aria-label="Field"
            value={rule.field}
            onChange={(event) => onChange(
              effectiveRules.map((entry, i): FilterRule => i === index ? { ...entry, field: event.target.value } : entry)
            )}
          />
          <select
            aria-label="Operator"
            value={rule.operator}
            onChange={(event) => onChange(
              effectiveRules.map((entry, i): FilterRule => i === index
                ? { ...entry, operator: event.target.value as FilterRule["operator"] }
                : entry)
            )}
          >
            {filterOperators.map((op) => <option key={op} value={op}>{op}</option>)}
          </select>
          <button
            className="icon-button"
            title="Remove rule"
            type="button"
            disabled={effectiveRules.length <= 1}
            onClick={() => onChange(effectiveRules.filter((_, i) => i !== index))}
          >
            ×
          </button>
          <input
            className="mono-input filter-value"
            aria-label="Value"
            placeholder={rule.operator === "in" ? '["a", "b"]' : "value"}
            value={rule.value}
            onChange={(event) => onChange(
              effectiveRules.map((entry, i): FilterRule => i === index ? { ...entry, value: event.target.value } : entry)
            )}
          />
        </div>
      ))}
      <button
        className="add-row"
        type="button"
        onClick={() => onChange([...effectiveRules, { field: "", operator: "equals", value: "" }])}
      >
        + {addLabel}
      </button>
    </div>
  );
}

/** Interactive branch step list shared by Condition, Decision, and Paths. */
function BranchStepList({
  branchKey,
  branchTitle,
  parsed,
  onSelectChild,
  onAddStep,
  onMoveStep,
  onDeleteStep
}: {
  branchKey: string;
  branchTitle: string;
  parsed: ReturnType<typeof parseBranchSteps>;
  onSelectChild?: (branchKey: string, stepIndex: number) => void;
  onAddStep?: (branchKey: string, branchTitle: string) => void;
  onMoveStep?: (branchKey: string, stepIndex: number, delta: -1 | 1) => void;
  onDeleteStep?: (branchKey: string, stepIndex: number) => void;
}) {
  return (
    <>
      {parsed.error ? (
        <p className="branch-error" role="alert">
          {parsed.error} Fix it below — nothing was lost.
        </p>
      ) : parsed.steps.length === 0 ? (
        <p className="inspector-hint">Nothing runs on this side yet.</p>
      ) : (
        <ol className="branch-list">
          {parsed.steps.map((step, index) => (
            <li key={`${step.id}-${index}`} className="branch-row branch-row-interactive">
              <button
                type="button"
                className="branch-row-select"
                onClick={() => onSelectChild?.(branchKey, index)}
                title="Select this step on the canvas"
              >
                <span className="branch-index" aria-hidden="true">{index + 1}</span>
                <span className="branch-step">
                  <span className="branch-step-label">{step.label}</span>
                  <span className="branch-step-id">{step.id}</span>
                  {step.detail && <span className="branch-step-detail">{step.detail}</span>}
                </span>
              </button>
              <span className="branch-row-actions">
                {onMoveStep && (
                  <>
                    <button
                      type="button"
                      className="icon-button branch-row-btn"
                      disabled={index === 0}
                      title="Move step up"
                      onClick={() => onMoveStep(branchKey, index, -1)}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      className="icon-button branch-row-btn"
                      disabled={index === parsed.steps.length - 1}
                      title="Move step down"
                      onClick={() => onMoveStep(branchKey, index, 1)}
                    >
                      ↓
                    </button>
                  </>
                )}
                {onDeleteStep && (
                  <button
                    type="button"
                    className="icon-button branch-row-btn"
                    title="Remove step from branch"
                    onClick={() => onDeleteStep(branchKey, index)}
                  >
                    ×
                  </button>
                )}
              </span>
            </li>
          ))}
        </ol>
      )}
      {onAddStep && (
        <button
          type="button"
          className="add-row branch-add-step"
          onClick={() => onAddStep(branchKey, branchTitle)}
        >
          + Add step to {branchTitle}
        </button>
      )}
    </>
  );
}

/** A condition's two sides as interactive step rows with click-to-select on
    canvas, + Add step, reorder/remove controls, and YAML disclosure. */
function ConditionBranches({
  thenField,
  elseField,
  thenValue,
  elseValue,
  onChange,
  onSelectChild,
  onAddStep,
  onMoveStep,
  onDeleteStep
}: {
  thenField: CatalogField;
  elseField: CatalogField;
  thenValue: string;
  elseValue: string;
  onChange: (key: string, value: string) => void;
  onSelectChild?: (branchKey: string, stepIndex: number) => void;
  onAddStep?: (branchKey: string, branchTitle: string) => void;
  onMoveStep?: (branchKey: string, stepIndex: number, delta: -1 | 1) => void;
  onDeleteStep?: (branchKey: string, stepIndex: number) => void;
}) {
  const branches = useMemo(() => ([
    { key: "then", title: "Then", hint: "Runs when the test passes.", field: thenField, value: thenValue, parsed: parseBranchSteps(thenValue) },
    { key: "else", title: "Else", hint: "Runs when the test fails.", field: elseField, value: elseValue, parsed: parseBranchSteps(elseValue) },
  ]), [thenField, elseField, thenValue, elseValue]);
  return (
    <div className="branch-groups">
      <p className="inspector-hint">
        Two sides of one test: Then runs on a pass, Else on a fail. Click a step
        to edit it on the canvas, or add and reorder steps directly.
      </p>
      {branches.map((branch) => (
        <div key={branch.key} className={`branch-group branch-${branch.key}`}>
          <div className="branch-head">
            <span className={`branch-badge branch-badge-${branch.key}`}>{branch.title}</span>
            <span className="branch-count">
              {branch.parsed.error
                ? "needs attention"
                : branch.parsed.steps.length === 0
                  ? "no steps"
                  : `${branch.parsed.steps.length} step${branch.parsed.steps.length === 1 ? "" : "s"}`}
            </span>
          </div>
          <p className="inspector-hint">{branch.hint}</p>
          <BranchStepList
            branchKey={branch.key}
            branchTitle={branch.title}
            parsed={branch.parsed}
            onSelectChild={onSelectChild}
            onAddStep={onAddStep}
            onMoveStep={onMoveStep}
            onDeleteStep={onDeleteStep}
          />
          <details className="inspector-advanced" open={Boolean(branch.parsed.error)}>
            <summary>Edit {branch.title} steps as YAML</summary>
            <FieldInput
              field={branch.field}
              value={branch.value}
              onChange={(value) => onChange(branch.key, value)}
            />
          </details>
        </div>
      ))}
    </div>
  );
}

/** Interactive branch editor for Decision (jev) steps in both switch and if modes. */
function DecisionBranchesEditor({
  mode,
  choicesField,
  casesField,
  defaultActionsField,
  thenField,
  elseField,
  choicesValue,
  casesValue,
  defaultActionsValue,
  thenValue,
  elseValue,
  rawBranches,
  onChangeFields,
  onSelectChild,
  onAddStep,
  onMoveStep,
  onDeleteStep
}: {
  mode: "switch" | "if";
  choicesField: CatalogField;
  casesField: CatalogField;
  defaultActionsField: CatalogField;
  thenField: CatalogField;
  elseField: CatalogField;
  choicesValue: string;
  casesValue: string;
  defaultActionsValue: string;
  thenValue: string;
  elseValue: string;
  rawBranches?: unknown;
  onChangeFields: (updates: Record<string, string>, options?: { rebuildCanvas?: boolean; renameBranch?: { from: string; to: string } }) => void;
  onSelectChild?: (branchKey: string, stepIndex: number) => void;
  onAddStep?: (branchKey: string, branchTitle: string) => void;
  onMoveStep?: (branchKey: string, stepIndex: number, delta: -1 | 1) => void;
  onDeleteStep?: (branchKey: string, stepIndex: number) => void;
}) {
  if (mode === "if") {
    return (
      <ConditionBranches
        thenField={thenField}
        elseField={elseField}
        thenValue={thenValue}
        elseValue={elseValue}
        onChange={(key, val) => onChangeFields({ [key]: val })}
        onSelectChild={onSelectChild}
        onAddStep={onAddStep}
        onMoveStep={onMoveStep}
        onDeleteStep={onDeleteStep}
      />
    );
  }

  let parseError: string | undefined;
  let parsedCases: Array<Record<string, unknown>> = [];
  let parsedChoiceList: string[] = [];

  if (choicesValue.trim()) {
    try {
      const loadedChoices = load(choicesValue);
      if (Array.isArray(loadedChoices)) {
        parsedChoiceList = loadedChoices.map((c) => String(c ?? "").trim()).filter(Boolean);
      }
    } catch { /* handled via cases or disclosure */ }
  }

  if (casesValue.trim()) {
    try {
      const loaded = load(casesValue);
      if (Array.isArray(loaded)) {
        parsedCases = loaded.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)));
      } else {
        parseError = "Cases must be a YAML list of { choice, actions } objects.";
      }
    } catch (err) {
      parseError = err instanceof Error ? err.message.split("\n")[0] : String(err);
    }
  } else if (rawBranches && typeof rawBranches === "object" && !Array.isArray(rawBranches)) {
    for (const [k, v] of Object.entries(rawBranches as Record<string, unknown>)) {
      if (Array.isArray(v)) {
        parsedCases.push({ choice: k, actions: v });
      } else if (v && typeof v === "object") {
        const entryObj = v as Record<string, unknown>;
        parsedCases.push({
          choice: k,
          ...(typeof entryObj.description === "string" ? { description: entryObj.description } : {}),
          actions: Array.isArray(entryObj.actions) ? entryObj.actions : []
        });
      }
    }
  }

  const seenChoices = new Set<string>();
  const choiceEntries: Array<{ key: string; description: string; actions: unknown[]; parsed: ReturnType<typeof parseBranchSteps> }> = [];
  for (const c of parsedCases) {
    const key = String(c.choice ?? c.label ?? "").trim();
    if (!key) continue;
    seenChoices.add(key);
    const description = typeof c.description === "string" ? c.description : "";
    const actions = Array.isArray(c.actions) ? c.actions : [];
    const yamlSteps = actions.length > 0 ? dump(actions, { lineWidth: 100 }).trimEnd() : "";
    choiceEntries.push({ key, description, actions, parsed: parseBranchSteps(yamlSteps) });
  }
  for (const choiceName of parsedChoiceList) {
    if (!seenChoices.has(choiceName)) {
      seenChoices.add(choiceName);
      choiceEntries.push({ key: choiceName, description: "", actions: [], parsed: { steps: [], error: null } });
    }
  }

  const commitChoices = (
    nextEntries: Array<{ key: string; description: string; actions: unknown[] }>,
    options?: { rebuildCanvas?: boolean; renameBranch?: { from: string; to: string } }
  ) => {
    const nextChoices = nextEntries.map((item) => item.key.trim() || "choice");
    const nextCases = nextEntries.map((item) => {
      const k = item.key.trim() || "choice";
      return {
        choice: k,
        ...(item.description.trim() ? { description: item.description } : {}),
        actions: item.actions
      };
    });
    const serializedChoices = nextChoices.length > 0 ? dump(nextChoices, { lineWidth: 100 }).trimEnd() : "";
    const serializedCases = nextCases.length > 0 ? dump(nextCases, { lineWidth: 100 }).trimEnd() : "";
    onChangeFields({ choices: serializedChoices, cases: serializedCases }, options);
  };

  const defaultParsed = parseBranchSteps(defaultActionsValue);

  return (
    <div className="branch-groups">
      <p className="inspector-hint">
        Jev classifies the event into one of the named choices below, or falls
        back to <code>else</code> when none fit.
      </p>
      {parseError && (
        <p className="branch-error" role="alert">
          {parseError} Fix it in the YAML disclosure below.
        </p>
      )}
      {choiceEntries.map((choice, idx) => (
        <div key={idx} className="branch-group branch-choice">
          <div className="branch-head">
            <input
              className="mono-input branch-key-input"
              aria-label={`Choice ${idx + 1} name`}
              value={choice.key}
              placeholder="choice_name"
              onChange={(event) => {
                const newKey = event.target.value;
                const next = choiceEntries.map((c, i) => i === idx ? { ...c, key: newKey } : c);
                commitChoices(next, { renameBranch: { from: choice.key, to: newKey.trim() || "choice" } });
              }}
            />
            <span className="branch-count">
              {choice.parsed.steps.length === 0
                ? "no steps"
                : `${choice.parsed.steps.length} step${choice.parsed.steps.length === 1 ? "" : "s"}`}
            </span>
            <button
              type="button"
              className="icon-button branch-row-btn"
              title="Remove choice branch"
              disabled={choiceEntries.length <= 1}
              onClick={() => commitChoices(choiceEntries.filter((_, i) => i !== idx), { rebuildCanvas: true })}
            >
              ×
            </button>
          </div>
          <label className="branch-criterion-label">
            Criterion for Jev (optional)
            <input
              type="text"
              placeholder="When should Jev pick this choice?"
              value={choice.description}
              onChange={(event) => {
                const next = choiceEntries.map((c, i) => i === idx ? { ...c, description: event.target.value } : c);
                commitChoices(next);
              }}
            />
          </label>
          <BranchStepList
            branchKey={choice.key}
            branchTitle={choice.key}
            parsed={choice.parsed}
            onSelectChild={onSelectChild}
            onAddStep={onAddStep}
            onMoveStep={onMoveStep}
            onDeleteStep={onDeleteStep}
          />
        </div>
      ))}
      <button
        type="button"
        className="add-row"
        onClick={() => {
          let idx = choiceEntries.length + 1;
          let candidate = `choice_${idx}`;
          while (choiceEntries.some((c) => c.key === candidate)) {
            idx += 1;
            candidate = `choice_${idx}`;
          }
          commitChoices([...choiceEntries, { key: candidate, description: "", actions: [] }], { rebuildCanvas: true });
        }}
      >
        + Add choice branch
      </button>

      <div className="branch-group branch-else">
        <div className="branch-head">
          <span className="branch-badge branch-badge-else">else</span>
          <span className="branch-count">
            {defaultParsed.steps.length === 0
              ? "no steps"
              : `${defaultParsed.steps.length} step${defaultParsed.steps.length === 1 ? "" : "s"}`}
          </span>
        </div>
        <p className="inspector-hint">Fallback steps when Jev returns none or an unknown choice.</p>
        <BranchStepList
          branchKey="default"
          branchTitle="else"
          parsed={defaultParsed}
          onSelectChild={onSelectChild}
          onAddStep={onAddStep}
          onMoveStep={onMoveStep}
          onDeleteStep={onDeleteStep}
        />
      </div>

      <details className="inspector-advanced" open={Boolean(parseError)}>
        <summary>Edit decision branches as YAML</summary>
        <FieldInput
          field={choicesField}
          value={choicesValue}
          onChange={(val) => onChangeFields({ choices: val })}
        />
        <FieldInput
          field={casesField}
          value={casesValue}
          onChange={(val) => onChangeFields({ cases: val })}
        />
        <FieldInput
          field={defaultActionsField}
          value={defaultActionsValue}
          onChange={(val) => onChangeFields({ default_actions: val })}
        />
      </details>
    </div>
  );
}

/** Interactive branch editor for Paths (multi-branch switch) steps. */
function PathsBranchesEditor({
  pathsField,
  defaultField,
  pathsValue,
  defaultValue,
  onChangeField,
  onSelectChild,
  onAddStep,
  onMoveStep,
  onDeleteStep
}: {
  pathsField: CatalogField;
  defaultField: CatalogField;
  pathsValue: string;
  defaultValue: string;
  onChangeField: (key: string, value: string, options?: { rebuildCanvas?: boolean; renameBranch?: { from: string; to: string } }) => void;
  onSelectChild?: (branchKey: string, stepIndex: number) => void;
  onAddStep?: (branchKey: string, branchTitle: string) => void;
  onMoveStep?: (branchKey: string, stepIndex: number, delta: -1 | 1) => void;
  onDeleteStep?: (branchKey: string, stepIndex: number) => void;
}) {
  let rawPaths: Array<Record<string, unknown>> = [];
  let parseError: string | undefined;
  if (pathsValue.trim()) {
    try {
      const loaded = load(pathsValue);
      if (Array.isArray(loaded)) {
        rawPaths = loaded.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)));
      } else {
        parseError = "Paths must be a YAML list of path objects.";
      }
    } catch (err) {
      parseError = err instanceof Error ? err.message.split("\n")[0] : String(err);
    }
  }

  const commitPaths = (
    nextPaths: Array<Record<string, unknown>>,
    options?: { rebuildCanvas?: boolean; renameBranch?: { from: string; to: string } }
  ) => {
    const serialized = nextPaths.length > 0 ? dump(nextPaths, { lineWidth: 100 }).trimEnd() : "";
    onChangeField("paths", serialized, options);
  };

  const defaultParsed = parseBranchSteps(defaultValue);

  return (
    <div className="branch-groups">
      <p className="inspector-hint">
        Evaluates paths top to bottom and runs the first matching path, or{" "}
        <code>else</code> when none match.
      </p>
      {parseError && (
        <p className="branch-error" role="alert">
          {parseError} Fix it in the YAML disclosure below.
        </p>
      )}
      {rawPaths.map((pathObj, idx) => {
        const key = (typeof pathObj.id === "string" && pathObj.id.trim())
          || (typeof pathObj.label === "string" && pathObj.label.trim())
          || `path_${idx + 1}`;
        const label = (typeof pathObj.label === "string" && pathObj.label)
          || (typeof pathObj.id === "string" && pathObj.id)
          || `Path ${idx + 1}`;
        const rules = extractPredicateRules(undefined, { when: pathObj.when });
        const actions = Array.isArray(pathObj.actions) ? pathObj.actions : [];
        const yamlSteps = actions.length > 0 ? dump(actions, { lineWidth: 100 }).trimEnd() : "";
        const parsed = parseBranchSteps(yamlSteps);
        return (
          <div key={idx} className="branch-group branch-path">
            <div className="branch-head">
              <input
                className="mono-input branch-key-input"
                aria-label={`Path ${idx + 1} label`}
                value={label}
                placeholder={`Path ${idx + 1}`}
                onChange={(event) => {
                  const newLabel = event.target.value;
                  const newKey = (typeof pathObj.id === "string" && pathObj.id.trim()) || newLabel.trim() || `path_${idx + 1}`;
                  const next = rawPaths.map((p, i) => i === idx ? { ...p, label: newLabel } : p);
                  commitPaths(next, { renameBranch: { from: key, to: newKey } });
                }}
              />
              <span className="branch-count">
                {parsed.steps.length === 0
                  ? "no steps"
                  : `${parsed.steps.length} step${parsed.steps.length === 1 ? "" : "s"}`}
              </span>
              <button
                type="button"
                className="icon-button branch-row-btn"
                title="Remove path"
                disabled={rawPaths.length <= 1}
                onClick={() => commitPaths(rawPaths.filter((_, i) => i !== idx), { rebuildCanvas: true })}
              >
                ×
              </button>
            </div>
            <div className="branch-rules-box">
              <span className="branch-rules-title">When all rules match:</span>
              <RuleBuilder
                rules={rules}
                onChange={(nextRules) => {
                  const whenYaml = filterRulesToYaml(nextRules);
                  const next = rawPaths.map((p, i) => i === idx ? { ...p, when: whenYaml } : p);
                  commitPaths(next);
                }}
              />
            </div>
            <BranchStepList
              branchKey={key}
              branchTitle={label}
              parsed={parsed}
              onSelectChild={onSelectChild}
              onAddStep={onAddStep}
              onMoveStep={onMoveStep}
              onDeleteStep={onDeleteStep}
            />
          </div>
        );
      })}
      <button
        type="button"
        className="add-row"
        onClick={() => {
          const nextIdx = rawPaths.length + 1;
          commitPaths([
            ...rawPaths,
            {
              label: `Path ${nextIdx}`,
              when: { field: "", operator: "equals", value: "" },
              actions: []
            }
          ], { rebuildCanvas: true });
        }}
      >
        + Add path branch
      </button>

      <div className="branch-group branch-else">
        <div className="branch-head">
          <span className="branch-badge branch-badge-else">else</span>
          <span className="branch-count">
            {defaultParsed.steps.length === 0
              ? "no steps"
              : `${defaultParsed.steps.length} step${defaultParsed.steps.length === 1 ? "" : "s"}`}
          </span>
        </div>
        <p className="inspector-hint">Fallback steps when no path matches.</p>
        <BranchStepList
          branchKey="default"
          branchTitle="else"
          parsed={defaultParsed}
          onSelectChild={onSelectChild}
          onAddStep={onAddStep}
          onMoveStep={onMoveStep}
          onDeleteStep={onDeleteStep}
        />
      </div>

      <details className="inspector-advanced" open={Boolean(parseError)}>
        <summary>Edit paths as YAML</summary>
        <FieldInput
          field={pathsField}
          value={pathsValue}
          onChange={(val) => onChangeField("paths", val)}
        />
        <FieldInput
          field={defaultField}
          value={defaultValue}
          onChange={(val) => onChangeField("default", val)}
        />
      </details>
    </div>
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

/** Discovery answers on /api/admin, not the designer prefix. */
function adminBase(config: DesignerConfig): string {
  return config.apiBase.replace(/\/designer$/, "");
}

/** The discovery route for one field's resources. Only the tag's declared
    discovery params ride along, resolved from their sibling fields; the
    backend rejects any other query param with a 400. */
function discoveryPath(discover: NonNullable<CatalogField["discover"]>, accountId: string, fields: Record<string, string>): string {
  const accountPath = `/connections/${encodeURIComponent(accountId)}/discover`;
  const params = new URLSearchParams();
  for (const [param, key] of Object.entries(discover.params ?? {})) {
    const val = (fields[key] ?? "").trim();
    if (val) params.set(param, val);
  }
  const qs = params.toString();
  return qs ? `${accountPath}/${discover.resource}?${qs}` : `${accountPath}/${discover.resource}`;
}

/** Modal browser over one field's discovered resources. Mounts open with the
    items already loaded (the Browse… button loads them first) and applies
    the picked template through onPick. */
function DiscoveryPicker({ discover, items, onPick, onClose }: {
  discover: NonNullable<CatalogField["discover"]>;
  items: DiscoveryItem[];
  onPick: (value: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const dialogRef = useMountedModal();
  const needle = query.trim().toLowerCase();
  const matches = items.filter((item) =>
    !needle || `${item.name} ${item.id}`.toLowerCase().includes(needle));
  const resource = discover.resource.replace(/_/g, " ");
  return (
    <dialog
      ref={dialogRef}
      className="dk-dialog picker-dialog"
      aria-labelledby="picker-title"
      onClose={onClose}
      onCancel={onClose}
    >
      <div className="dk-dialog__head">
        <h2 id="picker-title">Pick {article(resourceSingular(discover.resource))} {resourceSingular(discover.resource)}</h2>
      </div>
      <div className="dk-dialog__body">
        <input
          value={query}
          placeholder={`Filter ${resource}…`}
          aria-label={`Filter ${resource}`}
          autoFocus
          onChange={(event) => setQuery(event.target.value)}
        />
        {matches.length === 0 && <p className="picker-status">No {resource} found</p>}
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
      </div>
    </dialog>
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
  const dialogRef = useMountedModal();
  return (
    <dialog
      ref={dialogRef}
      className="dk-dialog picker-dialog"
      aria-labelledby="steps-templates-title"
      onClose={onClose}
      onCancel={onClose}
    >
      <div className="dk-dialog__head">
        <h2 id="steps-templates-title">Insert from previous steps</h2>
      </div>
      <div className="dk-dialog__body">
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
      </div>
    </dialog>
  );
}

function LeavePromptDialog({ error, saveDisabled, onStay, onDiscard, onSave }: {
  error: string | null;
  saveDisabled: boolean;
  onStay: () => void;
  onDiscard: () => void;
  onSave: () => void;
}) {
  const ref = useMountedModal();
  return (
    <dialog
      ref={ref}
      className="dk-dialog"
      role="alertdialog"
      aria-labelledby="leave-title"
      aria-describedby="leave-description"
      onClose={onStay}
      onCancel={onStay}
    >
      <div className="dk-dialog__head">
        <h2 id="leave-title">Unsaved workflow changes</h2>
      </div>
      <div className="dk-dialog__body">
        <p id="leave-description">Save this draft before opening another workflow?</p>
        {error && <p className="leave-error" role="alert">{error}</p>}
      </div>
      <div className="dk-dialog__foot">
        <button className="dk-button dk-button--secondary" type="button" onClick={onStay} autoFocus>Stay</button>
        <button className="dk-button dk-button--secondary" type="button" onClick={onDiscard}>Discard changes</button>
        <button className="dk-button dk-button--primary" type="button" disabled={saveDisabled} onClick={onSave}>Save and continue</button>
      </div>
    </dialog>
  );
}

function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  const ref = useMountedModal();
  return (
    <dialog
      ref={ref}
      className="dk-dialog picker-dialog"
      aria-labelledby="shortcuts-title"
      onClose={onClose}
      onCancel={onClose}
    >
      <div className="dk-dialog__head">
        <h2 id="shortcuts-title">Keyboard shortcuts</h2>
      </div>
      <div className="dk-dialog__body">
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
      </div>
    </dialog>
  );
}

export function App({ config = localConfig }: { config?: DesignerConfig }) {
  const [summaries, setSummaries] = useState<WorkflowSummary[]>([]);
  const [sourceName, setSourceName] = useState<string | null>(null);
  const [workflowId, setWorkflowId] = useState("new-workflow");
  const [initialWorkflowLoaded, setInitialWorkflowLoaded] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [shapes, setShapes] = useState<DiagramShape[]>(EMPTY_SHAPES);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [savedSnapshot, setSavedSnapshot] = useState("[]");
  /* Phones fold the secondary workflow actions behind More. */
  const [moreOpen, setMoreOpen] = useState(false);
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
  /** The workflow as last loaded or saved; its other keys survive canvas saves. */
  const [base, setBase] = useState<Workflow | null>(null);
  /** The API's generated name for the open workflow ("<trigger> → <actions>",
      engine.naming) as of the last load or save; "" for a never-saved one.
      A `name:` override in the draft wins over it. */
  const [autoName, setAutoName] = useState("");
  const customName = typeof base?.name === "string" ? base.name.trim() : "";
  /** The saved server-side draft for the open workflow (G15): a save writes a
      draft, Publish/Discard promote or throw it. Null = no draft known. */
  const [draftInfo, setDraftInfo] = useState<DraftInfo | null>(null);
  const [testOpen, setTestOpen] = useState(false);
  /** The Stored data panel (StoredData.tsx): the saved workflow's
      key-value state, offered only when a step reads or writes it. */
  const [storedOpen, setStoredOpen] = useState(false);
  /** The keys the Stored data button listed before opening the panel. */
  const [storedItems, setStoredItems] = useState<StoredItem[]>([]);
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
  /** Code-block tests (the code/js step's tests list): the cases run
     through the real runner against each case's own input. Keyed by node
     id like the step test. */
  const [codeTest, setCodeTest] = useState<{ nodeId: string | null; busy: boolean; result: CodeTestReport | null }>({
    nodeId: null, busy: false, result: null
  });
  /** Work-in-progress tests YAML, only while the text does not parse — the
     textarea would otherwise snap back to the last valid list mid-keystroke. */
  const [testsDrafts, setTestsDrafts] = useState<Record<string, string>>({});
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
  const navDrawerRef = useRef<HTMLElement>(null);
  const navToggleRef = useRef<HTMLButtonElement>(null);
  const navWasOpen = useRef(false);
  /** A step sits on the cross-workflow clipboard, so Paste step can appear. */
  const [clipboardHasStep, setClipboardHasStep] = useState(() => readStepClipboard() !== null);

  const dirty = useMemo(
    () => view === "yaml"
      ? yamlText !== savedYaml
      : Object.keys(invalidRawDrafts).length > 0 || canvasExtraDirty || workflowId !== savedId || enabled !== savedEnabled || JSON.stringify(shapes) !== savedSnapshot,
    [view, yamlText, savedYaml, invalidRawDrafts, canvasExtraDirty, workflowId, savedId, enabled, savedEnabled, shapes, savedSnapshot]
  );

  /** Stored data is per saved workflow id: offered in console mode once the
      workflow is saved and one of its steps (canvas, flows, nested chains)
      is a storage_* action. */
  const storedDataAvailable = useMemo(
    () => config.mode === "console" && !!sourceName && workflowUsesStorage(
      shapes.map((shape) => (shape.type === "node" ? shape.data?.actionType : undefined)), base),
    [config.mode, sourceName, shapes, base]
  );
  useEffect(() => {
    if (!storedDataAvailable) setStoredOpen(false);
  }, [storedDataAvailable]);
  const storageRequest = useCallback(
    <T,>(path: string, init?: RequestInit) => api<T>(config, path, init, "/api/admin"),
    [config]
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
    const step = undoHistory(editHistory, draftRef.current);
    if (!step) return;
    applySnapshot(step.snapshot);
    setEditHistory(step.state);
  }

  function redo() {
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
    commitEdit(draftRef.current, "workflow-id");
    setWorkflowId(id);
  }

  function toggleEnabled(next: boolean) {
    commitEdit(draftRef.current);
    setEnabled(next);
  }

  const [branchPicker, setBranchPicker] = useState<{ parentNodeId: string; branchKey: string; title: string } | null>(null);

  /** Mutates the step list of `branchKey` on `parentNodeId` and re-lays out the 2D branching canvas. */
  function mutateParentBranchSteps(
    parentNodeId: string,
    branchKey: string,
    mutateSteps: (steps: Record<string, unknown>[]) => Record<string, unknown>[],
    selectAfter?: (rebuilt: DiagramShape[], canonParentId: string) => string | null
  ) {
    const current = draftRef.current.shapes;
    const synced = syncBranchFieldsFromCanvas(current, parentNodeId);
    const parent = synced.find((s) => s.id === parentNodeId);
    if (!parent || !parent.data) return;
    const topOrder = orderedActionNodes(synced);
    const parentOrderIdx = topOrder.findIndex((s) => s.id === parentNodeId);
    const canonParentId = parentOrderIdx >= 0 ? `action-${parentOrderIdx}` : parentNodeId;

    const isDefaultBranch = branchKey === "else" || branchKey === "default";
    const branches = extractNodeBranches(parent.data);
    const targetBranch = branches.find(
      (b) => b.key === branchKey || (isDefaultBranch && (b.key === "else" || b.key === "default"))
    );
    const nextSteps = mutateSteps(targetBranch ? targetBranch.steps : []);
    const serializedSteps = nextSteps.length > 0 ? dump(nextSteps, { lineWidth: 100 }).trimEnd() : "";

    const actionType = parent.data.actionType;
    const nextFields = { ...(parent.data.fields ?? {}) };
    const nextRaw = { ...(parent.data.raw ?? {}) };

    if (actionType === "condition") {
      if (branchKey === "then") {
        nextFields.then = serializedSteps;
        delete nextRaw.then;
      } else {
        nextFields.else = serializedSteps;
        delete nextRaw.else;
      }
    } else if (actionType === "decision") {
      const mode = decisionMode(nextFields, nextRaw);
      if (mode === "if") {
        if (branchKey === "then") {
          nextFields.then = serializedSteps;
          delete nextRaw.then;
          delete nextRaw.then_actions;
        } else {
          nextFields.else = serializedSteps;
          delete nextRaw.else;
        }
      } else if (isDefaultBranch) {
        nextFields.default_actions = serializedSteps;
        delete nextRaw.default_actions;
      } else if (
        nextRaw.branches &&
        typeof nextRaw.branches === "object" &&
        !Array.isArray(nextRaw.branches) &&
        !(nextFields.cases ?? "").trim()
      ) {
        const parsedMap = { ...(nextRaw.branches as Record<string, unknown>) };
        const existingEntry = parsedMap[branchKey];
        if (existingEntry && typeof existingEntry === "object" && !Array.isArray(existingEntry)) {
          parsedMap[branchKey] = { ...(existingEntry as Record<string, unknown>), actions: nextSteps };
        } else {
          parsedMap[branchKey] = nextSteps;
        }
        nextRaw.branches = parsedMap;
      } else {
        let parsedCases: Array<Record<string, unknown>> = [];
        if ((nextFields.cases ?? "").trim()) {
          try {
            const loaded = load(nextFields.cases);
            if (Array.isArray(loaded)) {
              parsedCases = loaded.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)));
            }
          } catch { /* fallback */ }
        }
        let found = false;
        const updatedCases = parsedCases.map((c) => {
          const k = String(c.choice ?? c.label ?? "").trim();
          if (k === branchKey) {
            found = true;
            return { ...c, choice: branchKey, actions: nextSteps };
          }
          return c;
        });
        if (!found) {
          updatedCases.push({ choice: branchKey, actions: nextSteps });
        }
        const choiceNames = branches
          .filter((b) => !b.isDefault)
          .map((b) => b.key);
        if (!choiceNames.includes(branchKey)) choiceNames.push(branchKey);
        nextFields.choices = choiceNames.length > 0 ? dump(choiceNames, { lineWidth: 100 }).trimEnd() : "";
        nextFields.cases = updatedCases.length > 0 ? dump(updatedCases, { lineWidth: 100 }).trimEnd() : "";
        delete nextRaw.choices;
        delete nextRaw.cases;
      }
    } else if (actionType === "paths") {
      if (isDefaultBranch) {
        nextFields.default = serializedSteps;
        delete nextRaw.default;
      } else {
        let rawPaths: Array<Record<string, unknown>> = [];
        if ((nextFields.paths ?? "").trim()) {
          try {
            const loaded = load(nextFields.paths);
            if (Array.isArray(loaded)) {
              rawPaths = loaded.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)));
            }
          } catch { /* fallback */ }
        }
        const updatedPaths = rawPaths.map((p, idx) => {
          const k = (typeof p.id === "string" && p.id.trim())
            || (typeof p.label === "string" && p.label.trim())
            || `path_${idx + 1}`;
          return k === branchKey ? { ...p, actions: nextSteps } : p;
        });
        nextFields.paths = updatedPaths.length > 0 ? dump(updatedPaths, { lineWidth: 100 }).trimEnd() : "";
        delete nextRaw.paths;
      }
    }

    const stripped = synced.map((s) =>
      s.id === parentNodeId ? { ...s, data: { ...s.data!, fields: nextFields, raw: nextRaw } } : s
    );

    const rebuilt = rebuildShapesFromBranchEdit(stripped, workflowId, enabled, base, parentNodeId);
    commitEdit(draftRef.current);
    setShapes(rebuilt);
    if (selectAfter) {
      const nextSelected = selectAfter(rebuilt, canonParentId);
      if (nextSelected) setSelectedId(nextSelected);
    }
  }

  function addStepToBranch(parentNodeId: string, branchKey: string, kind: PaletteKind) {
    if (kind === "note" || kind.startsWith("trigger:")) return;
    const defaults = defaultFields(kind);
    const isDefaultBranch = branchKey === "else" || branchKey === "default";
    mutateParentBranchSteps(
      parentNodeId,
      branchKey,
      (steps) => {
        const nextIdx = steps.length + 1;
        const stepObj: Record<string, unknown> = {
          id: `${kind}_${nextIdx}`,
          type: kind
        };
        for (const [k, v] of Object.entries(defaults)) {
          if (k === "id" || v === "") continue;
          stepObj[k] = v;
        }
        return [...steps, stepObj];
      },
      (rebuilt, canonParentId) => {
        const children = rebuilt.filter(
          (s) =>
            s.data?.parentBranchNodeId === canonParentId &&
            (s.data?.branchKey === branchKey || (isDefaultBranch && (s.data?.branchKey === "else" || s.data?.branchKey === "default"))) &&
            !s.data?.isBranchPlaceholder
        );
        return children[children.length - 1]?.id ?? canonParentId;
      }
    );
    setStatus({ kind: "ok", message: `Added ${paletteLabel(kind)} to branch "${branchKey}".` });
  }

  function moveBranchStep(parentNodeId: string, branchKey: string, stepIndex: number, delta: -1 | 1) {
    mutateParentBranchSteps(
      parentNodeId,
      branchKey,
      (steps) => {
        const targetIdx = stepIndex + delta;
        if (targetIdx < 0 || targetIdx >= steps.length) return steps;
        const next = [...steps];
        const [moved] = next.splice(stepIndex, 1);
        next.splice(targetIdx, 0, moved);
        return next;
      },
      (_rebuilt, canonParentId) => canonParentId
    );
  }

  function deleteBranchStep(parentNodeId: string, branchKey: string, stepIndex: number) {
    mutateParentBranchSteps(
      parentNodeId,
      branchKey,
      (steps) => steps.filter((_, idx) => idx !== stepIndex),
      (_rebuilt, canonParentId) => canonParentId
    );
  }

  /** Converts a synthetic Union join bar into an explicit `type: "union"` step right after its branching parent. */
  function convertUnionJoinToExplicit(branchNodeId: string) {
    const current = draftRef.current.shapes;
    const synced = syncBranchFieldsFromCanvas(current, branchNodeId);
    const built = workflowFromShapes(synced, workflowId, enabled, base);
    const parentActionId = effectiveActionId(synced, branchNodeId) ?? branchNodeId;
    const topOrder = orderedActionNodes(synced);
    const parentIdx = topOrder.findIndex((s) => s.id === branchNodeId);
    const actions = [...(built.workflow.actions ?? [])];
    const unionStep: Record<string, unknown> = {
      id: `union_${parentActionId}`,
      type: "union",
      from: parentActionId
    };
    if (parentIdx >= 0 && parentIdx < actions.length) {
      actions.splice(parentIdx + 1, 0, unionStep);
    } else {
      actions.push(unionStep);
    }
    const rebuilt = shapesFromWorkflow({ ...built.workflow, actions });
    commitEdit(draftRef.current);
    setShapes(rebuilt);
    const unionNode = rebuilt.find((s) => s.type === "node" && s.data?.actionType === "union" && s.data?.fields?.from === parentActionId);
    if (unionNode) setSelectedId(unionNode.id);
    setStatus({ kind: "ok", message: `Inserted explicit Union step for "${parentActionId}".` });
  }

  /** Deletes the selected shape (node — with its arrows — note, or connector).
      One undo entry, so no confirm dialog. */
  function deleteSelectedShape() {
    const id = selectedId;
    if (!id) return;
    const target = draftRef.current.shapes.find((shape) => shape.id === id);
    if (target?.data?.isUnionJoin || target?.data?.isBranchPlaceholder) {
      setSelectedId(null);
      return;
    }
    if (target?.data?.parentBranchNodeId && target.data.branchKey !== undefined && target.data.branchIndex !== undefined) {
      deleteBranchStep(target.data.parentBranchNodeId, target.data.branchKey, target.data.branchIndex);
      return;
    }
    commitEdit(draftRef.current);
    setShapes((current) => {
      const removedNodeIds = new Set<string>([id]);
      for (const shape of current) {
        if (shape.data?.parentBranchNodeId === id || shape.data?.unionForNodeId === id) {
          removedNodeIds.add(shape.id);
        }
      }
      return current.filter((shape) => (
        !removedNodeIds.has(shape.id)
        && (!shape.sourceId || !removedNodeIds.has(shape.sourceId))
        && (!shape.targetId || !removedNodeIds.has(shape.targetId))
      ));
    });
    setSelectedId(null);
  }

  /** Duplicate: a copy of the action directly after it in the chain, with a
      fresh node id, its Action ID suffixed "-copy", and its field values,
      filters and raw extras carried over. */
  function duplicateStep(id: string) {
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
    if (!step || view !== "canvas") return;
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

  /** The mobile drawer is a modal dialog, like the console's: while it is
      open the main pane goes inert, focus starts inside, Tab cycles inside,
      and closing restores focus to the toggle. */
  useEffect(() => {
    const drawer = navDrawerRef.current;
    if (!drawer) return;
    if (navOpen) {
      navWasOpen.current = true;
      const main = document.querySelector<HTMLElement>(".designer-main");
      if (main) main.inert = true;
      const first = drawer.querySelector<HTMLElement>("a[href], button:not([disabled])");
      first?.focus();
      return () => {
        if (main) main.inert = false;
      };
    }
    if (navWasOpen.current) {
      navWasOpen.current = false;
      navToggleRef.current?.focus();
    }
  }, [navOpen]);

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const drawer = navDrawerRef.current;
      if (!drawer) return;
      const items = [...drawer.querySelectorAll<HTMLElement>("a[href], button:not([disabled])")]
        .filter((el) => el.offsetParent !== null);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
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
  }, [navOpen]);

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
        saved: Boolean(sourceName),
        name: customName || autoName,
        nameSource: customName ? "custom" : "auto",
        description: typeof base?.description === "string" ? base.description : "",
        enabled,
        source: sourceName,
        editable: view === "canvas",
        dirty
      },
      window.location.origin
    );
  }, [initialWorkflowLoaded, config.embedded, workflowId, enabled, sourceName, view, dirty, customName, autoName, base]);

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

  /** The console's h1 edits the human name (the `name:` override): the
      value lands in the draft and saves with it; an empty one drops the
      override so the generated name shows again. The id never changes here. */
  useEffect(() => {
    if (!config.embedded) return;
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data as { type?: string; name?: unknown };
      if (data?.type !== "designer:set-name" || typeof data.name !== "string") return;
      if (view !== "canvas") {
        setStatus({ kind: "error", message: "Switch to Canvas to rename — or edit name: in the YAML." });
        return;
      }
      const name = data.name.replace(/\s+/g, " ").trim();
      if (name.length > MAX_NAME_LENGTH) {
        setStatus({ kind: "error", message: `A workflow name may be at most ${MAX_NAME_LENGTH} characters.` });
        return;
      }
      setBase((current) => {
        const next = { ...(current ?? {}) } as Workflow;
        if (name) next.name = name;
        else delete next.name;
        return next;
      });
      setCanvasExtraDirty(true);
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
        // Console deep link: /workflows/<id> opens that workflow; the
        // console passes the id through as ?workflow=<id>.
        const requested = new URLSearchParams(window.location.search).get("workflow");
        const match = requested
          ? workflows.find((summary) => summary.id === requested)
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
      const draftOnly = summary.published === false;
      const ref = summary.source || summary.id;
      const data = await api<{ workflow: Workflow; draft?: DraftInfo; name?: string; name_source?: string }>(
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
      setStoredOpen(false);
      setWorkflowId(workflow.id);
      setEnabled(workflow.enabled !== false);
      setShapes(shapes);
      setSavedSnapshot(JSON.stringify(shapes));
      setSavedId(workflow.id);
      setSavedEnabled(workflow.enabled !== false);
      setCanvasExtraDirty(false);
      setInvalidRawDrafts({});
      setBase(workflow);
      setAutoName(data.name_source === "custom" ? "" : (data.name ?? ""));
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
    setStoredOpen(false);
    setInvalidRawDrafts({});
    setWorkflowId("new-workflow");
    setEnabled(true);
    setShapes([trigger]);
    setSavedSnapshot("[]");
    setSavedId("new-workflow");
    setSavedEnabled(true);
    setCanvasExtraDirty(false);
    setBase(null);
    setAutoName("");
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
    setStoredOpen(false);
    setStepTest({ nodeId: null, busy: false, result: null });
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
    // A never-saved workflow still on the placeholder id lets the API pick
    // the id: the slug of its name (unique), so nobody has to type one.
    if (!sourceName && workflow.id === NEW_WORKFLOW_ID) {
      const rest: Record<string, unknown> = { ...workflow };
      delete rest.id;
      yamlOut = workflowYaml(rest as unknown as Workflow);
    }
    setStatus({ kind: "busy", message: "Saving…" });
    try {
      const result = await api<{ id?: string; name?: string; name_source?: string; commit?: string | null; published?: boolean; revision?: number; draft?: DraftInfo; git_sync_error?: string }>(config, "/workflows", {
        method: "PUT",
        body: JSON.stringify({ yaml: yamlOut, renameFrom: sourceName })
      });
      if (result.id && result.id !== workflow.id) {
        workflow = { ...workflow, id: result.id };
        yamlOut = workflowYaml(workflow);
        setWorkflowId(result.id);
        if (view === "yaml") setYamlText(yamlOut);
      }
      setAutoName(result.name_source === "custom" ? "" : (result.name ?? ""));
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
     both. The console's duplicate route commits and publishes like a save. */
  async function duplicateWorkflow() {
    const ref = sourceName;
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
    const actionId = effectiveActionId(shapes, nodeId);
    if (!actionId) {
      setStatus({ kind: "error", message: "Connect this step to the trigger first — the step test runs it in the workflow." });
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
      setStoredOpen(false);
      setTestOpen(true);
      setStepTest({ nodeId, busy: false, result: { mode: "test-step", matched: false, steps: [], error: "The sample event is not valid JSON — fix it in the Test run panel." } });
      return;
    }
    if (!sample || typeof sample !== "object" || Array.isArray(sample)) {
      setStoredOpen(false);
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

  /** Edit the selected code/js step's tests list (its `raw.tests`) through a
     YAML textarea. Text that parses updates the step at once (an empty one
     clears it); mid-edit garbage parks in the draft so the textarea does
     not snap back to the last valid list. */
  function setTestsText(nodeId: string, text: string) {
    let parsed: unknown;
    try {
      parsed = load(text);
    } catch {
      setTestsDrafts((current) => ({ ...current, [nodeId]: text }));
      return;
    }
    setTestsDrafts((current) => {
      if (!(nodeId in current)) return current;
      const next = { ...current };
      delete next[nodeId];
      return next;
    });
    if (parsed === null || parsed === undefined) {
      updateSelected((current) => {
        const raw = { ...(current.raw ?? {}) };
        delete raw.tests;
        return { ...current, raw };
      });
      return;
    }
    if (!Array.isArray(parsed)) {
      setTestsDrafts((current) => ({ ...current, [nodeId]: text }));
      return;
    }
    updateSelected((current) => ({ ...current, raw: { ...(current.raw ?? {}), tests: parsed } }));
  }

  /** Run the selected code/js step's tests (Zapier-style code-block tests):
     each case runs the snippet against its own input through the real
     runner — pure, no connections. The current draft goes inline, like the
     step test. */
  async function runCodeTests() {
    if (!selected || selected.type !== "node" || !selected.data || selected.data.nodeKind !== "action") return;
    const nodeId = selected.id;
    const actionId = effectiveActionId(shapes, nodeId);
    if (!actionId) {
      setStatus({ kind: "error", message: "Connect this step to the trigger first — its tests run it in the workflow." });
      return;
    }
    const { workflow, problems } = workflowFromShapes(shapes, workflowId, enabled);
    if (problems.length) {
      setStatus({ kind: "error", message: problems.join(" ") });
      return;
    }
    setCodeTest({ nodeId, busy: true, result: null });
    try {
      const result = await api<CodeTestReport>(config, "/workflows/test-code", {
        method: "POST",
        body: JSON.stringify({ action_id: actionId, workflow })
      });
      setCodeTest({ nodeId, busy: false, result });
    } catch (error) {
      setCodeTest({ nodeId, busy: false, result: { total: 0, passed: 0, failed: 0, cases: [], error: String(error) } });
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
    editShapes((current) => {
      const target = current.find((shape) => shape.id === selectedId);
      if (!target || !target.data) return current;
      const nextData = mutate(target.data);
      const label = nextData.nodeKind === "trigger"
        ? triggerLabel(nextData.connector ?? "custom", dataOrEmpty(nextData.event))
        : target.label;
      let nextShapes = current.map((shape) => (
        shape.id === selectedId ? { ...shape, data: nextData, label } : shape
      ));
      if (nextData.parentBranchNodeId) {
        nextShapes = syncBranchFieldsFromCanvas(nextShapes, nextData.parentBranchNodeId);
      } else if (isBranchingAction(nextData.actionType)) {
        const prevFields = target.data.fields ?? {};
        const curFields = nextData.fields ?? {};
        const branchStructureChanged = ["then", "else", "branches", "then_actions", "default_actions", "paths", "default", "mode"].some(
          (k) => (prevFields[k] ?? "") !== (curFields[k] ?? "")
        );
        if (branchStructureChanged) {
          const stripped = nextShapes.filter(
            (s) => s.data?.parentBranchNodeId !== selectedId && s.data?.unionForNodeId !== selectedId
          );
          nextShapes = rebuildShapesFromBranchEdit(stripped, workflowId, enabled, base);
        }
      }
      return nextShapes;
    }, `node:${selectedId}`);
  }

  function dataOrEmpty(val: string | undefined): string {
    return val ?? "";
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
    if (data.isUnionJoin) {
      const parentNode = shapes.find((s) => s.id === data.unionForNodeId);
      const parentActionId = (data.unionForNodeId && effectiveActionId(shapes, data.unionForNodeId)) || "branch_step";
      return (
        <section className="inspector-group">
          <h3>Branches rejoin</h3>
          <p className="inspector-hint">
            All branches from <strong>{parentNode ? nodeTitle(parentNode) : parentActionId}</strong> converge here before the next step runs.
          </p>
          <div className="union-dataflow-card">
            <p className="inspector-hint">
              Downstream steps can read the active branch&rsquo;s output without knowing which branch ran:
            </p>
            <div className="template-chips">
              <button
                type="button"
                className="template-chip"
                onClick={() => copyTemplate(`{steps.${parentActionId}.output.result}`)}
              >
                {`{steps.${parentActionId}.output.result}`}
              </button>
              <button
                type="button"
                className="template-chip"
                onClick={() => copyTemplate(`{steps.${parentActionId}.output.branch}`)}
              >
                {`{steps.${parentActionId}.output.branch}`}
              </button>
            </div>
          </div>
          {data.unionForNodeId && (
            <div className="test-actions">
              <button
                type="button"
                className="dk-button dk-button--secondary"
                onClick={() => convertUnionJoinToExplicit(data.unionForNodeId!)}
              >
                <GitMerge size={18} strokeWidth={1.8} />
                <span>Convert to explicit Union step</span>
              </button>
            </div>
          )}
        </section>
      );
    }
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
                onChange={(event) => {
                  const connector = event.target.value;
                  // A new source starts on its own default event; a source
                  // with no catalog events (custom) keeps the typed one.
                  const firstEvent = connectorCatalog.find((entry) => entry.name === connector)?.events[0];
                  updateSelected((current) => ({ ...current, connector, event: firstEvent ?? current.event }));
                }}
              >
                {connectorCatalog.map((entry) => <option key={entry.name} value={entry.name}>{entry.label}</option>)}
              </select>
            </label>
            {connectorEntry.events.length > 0 ? (
              <label>Event
                <select
                  value={data.event ?? ""}
                  onChange={(event) => updateSelected((current) => ({ ...current, event: event.target.value }))}
                >
                  {connectorEntry.events.map((event) => (
                    <option key={event} value={event}>
                      {eventInfo(connectorEntry.name, event)?.label ?? event} ({event})
                    </option>
                  ))}
                  {/* A YAML-authored event outside the catalog stays selectable
                     so opening the workflow never rewrites it. */}
                  {data.event && !connectorEntry.events.includes(data.event) && (
                    <option value={data.event}>{data.event} (not in catalog)</option>
                  )}
                </select>
                {data.event && (
                  <span className="connection-hint">
                    {eventInfo(connectorEntry.name, data.event)?.description
                      ?? "Not a catalog event for this source — the trigger only fires if something emits it."}
                  </span>
                )}
              </label>
            ) : (
              <label>Event
                <input
                  className="mono-input"
                  value={data.event ?? ""}
                  placeholder="order.created"
                  onChange={(event) => updateSelected((current) => ({ ...current, event: event.target.value }))}
                />
                <span className="connection-hint">Any event name — this source has no fixed list.</span>
              </label>
            )}
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
    const setField = (key: string, value: string) => updateSelected((current) => {
      const nextFields = { ...current.fields, [key]: value };
      const nextData: NodeData = { ...current, fields: nextFields };
      if (isBranchingAction(current.actionType)) {
        nextData.branchPills = extractNodeBranches(nextData).map((b) => b.label);
      }
      return nextData;
    });
    const updateBranchFields = (
      updates: Record<string, string>,
      options?: { rebuildCanvas?: boolean; renameBranch?: { from: string; to: string } }
    ) => {
      commitEdit(draftRef.current, `inspector:${selected.id}:${Object.keys(updates).join(",")}`);
      setShapes((currentShapes) => {
        const nextShapes = currentShapes.map((shape) => {
          if (shape.id === selected.id && shape.data) {
            const nextFields = { ...shape.data.fields, ...updates };
            const nextRaw = { ...(shape.data.raw ?? {}) };
            if ("cases" in updates || "choices" in updates) {
              delete nextRaw.cases;
              delete nextRaw.choices;
              delete nextRaw.branches;
            }
            if ("paths" in updates) delete nextRaw.paths;
            if ("then" in updates) delete nextRaw.then;
            if ("else" in updates) delete nextRaw.else;
            if ("default" in updates) delete nextRaw.default;
            if ("default_actions" in updates) delete nextRaw.default_actions;
            const nextData: NodeData = { ...shape.data, fields: nextFields, raw: nextRaw };
            nextData.branchPills = extractNodeBranches(nextData).map((b) => b.label);
            return { ...shape, data: nextData };
          }
          if (
            options?.renameBranch &&
            shape.data?.parentBranchNodeId === selected.id &&
            shape.data?.branchKey === options.renameBranch.from
          ) {
            return {
              ...shape,
              label: shape.data.isBranchPlaceholder ? `+ Add step to ${options.renameBranch.to}` : shape.label,
              data: {
                ...shape.data,
                branchKey: options.renameBranch.to,
                branchLabel: options.renameBranch.to
              }
            };
          }
          if (
            options?.renameBranch &&
            shape.type === "arrow" &&
            shape.sourceId === selected.id &&
            shape.label === options.renameBranch.from
          ) {
            return { ...shape, label: options.renameBranch.to };
          }
          return shape;
        });
        if (options?.rebuildCanvas) {
          return rebuildShapesFromBranchEdit(nextShapes, workflowId, enabled, base, selected.id);
        }
        return syncBranchFieldsFromCanvas(nextShapes, selected.id);
      });
    };
    const selectBranchChild = (branchKey: string, stepIdx: number) => {
      const isDefault = branchKey === "else" || branchKey === "default";
      const child = shapes.find(
        (s) =>
          s.data?.parentBranchNodeId === selected.id &&
          (s.data?.branchKey === branchKey || (isDefault && (s.data?.branchKey === "else" || s.data?.branchKey === "default"))) &&
          s.data?.branchIndex === stepIdx &&
          !s.data?.isBranchPlaceholder
      );
      if (child) setSelectedId(child.id);
    };
    const updatePredicateRules = (nextRules: FilterRule[]) => {
      updateSelected((current) => {
        const first = nextRules[0] ?? { field: "", operator: "equals", value: "" };
        const nextFields = { ...(current.fields ?? {}) };
        const nextRaw = { ...(current.raw ?? {}) };
        if (nextRules.length <= 1) {
          nextFields.field = first.field;
          nextFields.operator = first.operator;
          nextFields.value = first.value;
          delete nextRaw.when;
        } else {
          nextFields.field = "";
          nextFields.operator = "equals";
          nextFields.value = "";
          nextRaw.when = filterRulesToYaml(nextRules);
        }
        return { ...current, fields: nextFields, raw: nextRaw };
      });
    };
    const parentBranchNode = data.parentBranchNodeId
      ? shapes.find((s) => s.id === data.parentBranchNodeId)
      : null;
    const branchSiblingCount = data.parentBranchNodeId && data.branchKey
      ? shapes.filter(
          (s) => s.data?.parentBranchNodeId === data.parentBranchNodeId
            && s.data?.branchKey === data.branchKey
            && !s.data?.isBranchPlaceholder
        ).length
      : 0;
    return (
      <>
        {parentBranchNode && data.branchKey !== undefined && (
          <section className="inspector-group branch-context-bar" aria-label="Branch context">
            <div className="branch-context-crumb">
              <button
                type="button"
                className="branch-back-link"
                onClick={() => setSelectedId(parentBranchNode.id)}
              >
                ← {nodeTitle(parentBranchNode)}
              </button>
              <span className="branch-badge">{data.branchLabel ?? data.branchKey}</span>
              <span className="branch-count">Step {(data.branchIndex ?? 0) + 1} of {branchSiblingCount}</span>
            </div>
            <div className="branch-context-actions">
              <button
                type="button"
                className="dk-button dk-button--secondary"
                disabled={(data.branchIndex ?? 0) <= 0}
                onClick={() => moveBranchStep(parentBranchNode.id, data.branchKey!, data.branchIndex ?? 0, -1)}
              >
                ↑ Move up
              </button>
              <button
                type="button"
                className="dk-button dk-button--secondary"
                disabled={(data.branchIndex ?? 0) >= branchSiblingCount - 1}
                onClick={() => moveBranchStep(parentBranchNode.id, data.branchKey!, data.branchIndex ?? 0, 1)}
              >
                ↓ Move down
              </button>
            </div>
          </section>
        )}
        <section className="inspector-group">
          <h3>Action</h3>
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
        </section>
        {meta ? (
          <>
          <section className="inspector-group">
            <h3>Settings</h3>
            {data.actionType === "condition" ? (
              <>
                <div className="branch-rules-box">
                  <span className="branch-rules-title">When all rules match:</span>
                  <RuleBuilder
                    rules={extractPredicateRules(data.fields, data.raw)}
                    onChange={updatePredicateRules}
                  />
                </div>
                <ConditionBranches
                  thenField={meta.fields.find((field) => field.key === "then")!}
                  elseField={meta.fields.find((field) => field.key === "else")!}
                  thenValue={data.fields?.then ?? ""}
                  elseValue={data.fields?.else ?? ""}
                  onChange={(key, value) => updateBranchFields({ [key]: value }, { rebuildCanvas: true })}
                  onSelectChild={selectBranchChild}
                  onAddStep={(branchKey, branchTitle) => setBranchPicker({
                    parentNodeId: selected.id,
                    branchKey,
                    title: `Add step to ${branchTitle}`
                  })}
                  onMoveStep={(branchKey, stepIdx, delta) => moveBranchStep(selected.id, branchKey, stepIdx, delta)}
                  onDeleteStep={(branchKey, stepIdx) => deleteBranchStep(selected.id, branchKey, stepIdx)}
                />
              </>
            ) : data.actionType === "filter" ? (
              <>
                <div className="branch-rules-box">
                  <span className="branch-rules-title">Continue only when all rules match:</span>
                  <RuleBuilder
                    rules={extractPredicateRules(data.fields, data.raw)}
                    onChange={updatePredicateRules}
                  />
                </div>
                {meta.fields
                  .filter((field) => field.key !== "field" && field.key !== "operator" && field.key !== "value")
                  .map((field) => (
                    <FieldInput
                      key={field.key}
                      field={field}
                      value={data.fields?.[field.key] ?? ""}
                      connections={connections}
                      fields={data.fields}
                      siblingFields={meta.fields}
                      config={config}
                      nodeType={data.actionType}
                      onChange={(value) => setField(field.key, value)}
                    />
                  ))}
              </>
            ) : data.actionType === "decision" ? (
              <>
                {meta.fields
                  .filter((field) => field.key === "mode" || field.key === "prompt" || field.key === "system")
                  .map((field) => (
                    <FieldInput
                      key={field.key}
                      field={field}
                      value={data.fields?.[field.key] ?? ""}
                      connections={connections}
                      fields={data.fields}
                      siblingFields={meta.fields}
                      config={config}
                      nodeType={data.actionType}
                      onChange={(value) => {
                        if (field.key === "mode") {
                          updateBranchFields({ mode: value }, { rebuildCanvas: true });
                        } else {
                          setField(field.key, value);
                        }
                      }}
                    />
                  ))}
                <DecisionBranchesEditor
                  mode={decisionMode(data.fields, data.raw)}
                  choicesField={meta.fields.find((field) => field.key === "choices")!}
                  casesField={meta.fields.find((field) => field.key === "cases")!}
                  defaultActionsField={meta.fields.find((field) => field.key === "default_actions")!}
                  thenField={meta.fields.find((field) => field.key === "then")!}
                  elseField={meta.fields.find((field) => field.key === "else")!}
                  choicesValue={data.fields?.choices ?? ""}
                  casesValue={data.fields?.cases ?? ""}
                  defaultActionsValue={data.fields?.default_actions ?? ""}
                  thenValue={data.fields?.then ?? ""}
                  elseValue={data.fields?.else ?? ""}
                  rawBranches={data.raw?.branches}
                  onChangeFields={(updates, options) => updateBranchFields(updates, options)}
                  onSelectChild={selectBranchChild}
                  onAddStep={(branchKey, branchTitle) => setBranchPicker({
                    parentNodeId: selected.id,
                    branchKey,
                    title: `Add step to ${branchTitle}`
                  })}
                  onMoveStep={(branchKey, stepIdx, delta) => moveBranchStep(selected.id, branchKey, stepIdx, delta)}
                  onDeleteStep={(branchKey, stepIdx) => deleteBranchStep(selected.id, branchKey, stepIdx)}
                />
                <details className="inspector-advanced">
                  <summary>Model &amp; timeout</summary>
                  {meta.fields
                    .filter((field) => field.key === "model" || field.key === "timeout_seconds")
                    .map((field) => (
                      <FieldInput
                        key={field.key}
                        field={field}
                        value={data.fields?.[field.key] ?? ""}
                        connections={connections}
                        fields={data.fields}
                        siblingFields={meta.fields}
                        config={config}
                        nodeType={data.actionType}
                        onChange={(value) => setField(field.key, value)}
                      />
                    ))}
                </details>
              </>
            ) : data.actionType === "paths" ? (
              <PathsBranchesEditor
                pathsField={meta.fields.find((field) => field.key === "paths")!}
                defaultField={meta.fields.find((field) => field.key === "default")!}
                pathsValue={data.fields?.paths ?? ""}
                defaultValue={data.fields?.default ?? ""}
                onChangeField={(key, value, options) => updateBranchFields({ [key]: value }, options)}
                onSelectChild={selectBranchChild}
                onAddStep={(branchKey, branchTitle) => setBranchPicker({
                  parentNodeId: selected.id,
                  branchKey,
                  title: `Add step to ${branchTitle}`
                })}
                onMoveStep={(branchKey, stepIdx, delta) => moveBranchStep(selected.id, branchKey, stepIdx, delta)}
                onDeleteStep={(branchKey, stepIdx) => deleteBranchStep(selected.id, branchKey, stepIdx)}
              />
            ) : data.actionType === "union" ? (
              <>
                {(() => {
                  const branchCandidates = orderedActionNodes(shapes)
                    .filter((s) => s.id !== selected.id && isBranchingAction(s.data?.actionType))
                    .map((s) => ({
                      id: effectiveActionId(shapes, s.id) ?? s.id,
                      title: nodeTitle(s)
                    }));
                  return (
                    <label>Branching step ID (from)
                      <input
                        className="mono-input"
                        list={`union-from-${selected.id}`}
                        value={data.fields?.from ?? ""}
                        placeholder={branchCandidates[0]?.id ?? "route_by_kind"}
                        onChange={(event) => setField("from", event.target.value)}
                      />
                      <datalist id={`union-from-${selected.id}`}>
                        {branchCandidates.map((c) => (
                          <option key={c.id} value={c.id}>{c.title}</option>
                        ))}
                      </datalist>
                    </label>
                  );
                })()}
                {meta.fields
                  .filter((field) => field.key !== "from")
                  .map((field) => (
                    <FieldInput
                      key={field.key}
                      field={field}
                      value={data.fields?.[field.key] ?? ""}
                      connections={connections}
                      fields={data.fields}
                      siblingFields={meta.fields}
                      config={config}
                      nodeType={data.actionType}
                      onChange={(value) => setField(field.key, value)}
                    />
                  ))}
                <div className="union-dataflow-card">
                  <p className="inspector-hint">
                    Coalesces outputs from whichever branch ran into{" "}
                    <code>{`{steps.${effectiveActionId(shapes, selected.id) ?? "union"}.output.result.*}`}</code>{" "}
                    and exposes the winning branch name as{" "}
                    <code>{`{steps.${effectiveActionId(shapes, selected.id) ?? "union"}.output.branch}`}</code>.
                  </p>
                </div>
              </>
            ) : (
              meta.fields.map((field) => (
                <FieldInput
                  key={field.key}
                  field={field}
                  value={data.fields?.[field.key] ?? ""}
                  connections={connections}
                  fields={data.fields}
                  siblingFields={meta.fields}
                  config={config}
                  nodeType={data.actionType}
                  onChange={(value) => setField(field.key, value)}
                />
              ))
            )}
          </section>
          {(data.actionType === "code" || data.actionType === "js") && (
            <section className="inspector-group">
              <h3>Code tests</h3>
              <p className="inspector-hint">
                Cases riding on this step as its <code>tests</code> list (YAML).
                Each runs the snippet against its own <code>input</code> with
                prior outputs as <code>steps</code>:
                <code>expected</code> asserts the result deep-equal,
                <code>expected_error</code> asserts a failing run, neither only
                asserts it runs. Pure — no connections, nothing recorded.
              </p>
              <textarea
                aria-label="Code tests (YAML)"
                className="tests-editor"
                rows={9}
                spellCheck={false}
                value={testsDrafts[selected.id] ?? dump(data.raw?.tests ?? [])}
                placeholder={"- name: no attachments\n  input:\n    attachments: []\n  expected:\n    has_attachment: false"}
                onChange={(event) => setTestsText(selected.id, event.target.value)}
              />
              {config.mode === "console" && (
                <div className="test-actions">
                  <button className="dk-button dk-button--secondary" type="button"
                          disabled={codeTest.busy} onClick={runCodeTests}>
                    {codeTest.busy
                      ? <Loader2 size={20} strokeWidth={1.8} className="spin" />
                      : <FlaskConical size={20} strokeWidth={1.8} />}
                    <span>Run tests</span>
                  </button>
                </div>
              )}
              {codeTest.nodeId === selected.id && codeTest.result && (
                <div className={`test-result ${codeTest.result.error && codeTest.result.cases.length === 0 ? "failed" : codeTest.result.failed === 0 && codeTest.result.total > 0 ? "passed" : "failed"}`}>
                  <p className="test-summary">
                    {codeTest.result.error && codeTest.result.cases.length === 0
                      ? codeTest.result.error
                      : <strong>{codeTest.result.passed}/{codeTest.result.total} passed
                          {codeTest.result.failed ? ` — ${codeTest.result.failed} FAILED` : ""}</strong>}
                  </p>
                  {codeTest.result.cases.map((item) => (
                    <div key={item.name} className={item.ok ? "test-step ok" : "test-step failed"}>
                      <span className="test-step-title">
                        {item.name}
                        {!item.ok && item.error && <span className="test-step-error"> — {item.error}</span>}
                      </span>
                      {!item.ok && item.error && item.stdout && (
                        <pre className="test-io">stdout: {item.stdout}</pre>
                      )}
                      {!item.ok && (item.expected !== undefined || item.actual !== undefined) && (
                        <pre className="test-io">expected: {JSON.stringify(item.expected)}{"\n"}actual:   {JSON.stringify(item.actual)}</pre>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </section>
          )}
          </>
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
        {/* The step id is internal plumbing: saves fill in action-<n> when
           it is blank. It only matters for {steps.<id>.output} references,
           so it sits behind a disclosure, never as a primary field. */}
        <details className="inspector-group inspector-advanced">
          <summary>Advanced</summary>
          <label>Step ID
            <input
              className="mono-input"
              value={data.fields?.id ?? ""}
              placeholder={effectiveActionId(shapes, selected.id) ?? "action-1"}
              onChange={(event) => setField("id", event.target.value)}
            />
            <span className="connection-hint">
              Names this step in {"{steps.<id>.output…}"} templates. Leave blank
              to use the generated {effectiveActionId(shapes, selected.id) ?? "action-<n>"}.
            </span>
          </label>
        </details>
        {/* Console only, like the Test run panel: local mode has no admin
           API to run the step against. */}
        {config.mode === "console" && (
          <section className="inspector-group">
            <h3>Test step</h3>
            <p className="inspector-hint">
              Test this step on the Test run panel&rsquo;s sample event.
              Dry run renders its inputs and evaluates conditions — nothing is
              sent. Run step executes it for real (side effects limited to this
              step) and feeds its output to the next step&rsquo;s test.
            </p>
            <div className="test-actions">
              <button className="dk-button dk-button--secondary" type="button" disabled={stepTest.busy}
                      onClick={() => testSelectedStep(false)}>
                {stepTest.busy ? <Loader2 size={20} strokeWidth={1.8} className="spin" /> : <FlaskConical size={20} strokeWidth={1.8} />}
                <span>Dry run</span>
              </button>
              <button className="dk-button dk-button--danger" type="button" disabled={stepTest.busy}
                      onClick={() => testSelectedStep(true)}>
                <Play size={20} strokeWidth={1.8} /><span>Run step</span>
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
        <aside
          ref={navDrawerRef}
          className={navOpen ? "designer-sidebar open" : "designer-sidebar"}
          role={navOpen ? "dialog" : undefined}
          aria-modal={navOpen || undefined}
          aria-label="Workflows"
        >
        {/* Console mode: the dakit topbar (ConsoleShell) carries the brand. */}
        {config.mode !== "console" && (
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
              <WorkflowIcon size={20} strokeWidth={1.8} />
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
            <GitBranch size={20} strokeWidth={1.8} />
            <span>{git.branch}</span>
            <span className={git.dirty ? "git-dirty" : "git-clean"}>{git.dirty ? "unsaved changes" : "clean"}</span>
            {(git.ahead > 0 || git.behind > 0) && <span>{git.ahead}↑ {git.behind}↓</span>}
          </div>
        )}
        </aside>
      )}

      <main className="designer-main">
        <header className={config.embedded ? "designer-topbar embedded" : "designer-topbar"}>
          {!config.embedded && (
            <button
              ref={navToggleRef}
              className="icon-button nav-toggle"
              type="button"
              aria-label="Toggle workflow list"
              aria-expanded={navOpen}
              onClick={() => setNavOpen((open) => !open)}
            >
              <Menu size={20} strokeWidth={1.8} />
            </button>
          )}
          <div className="topbar-title">
            <div className="dk-tabs view-switch" role="tablist" aria-label="Editor view">
              {(["canvas", "yaml"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  className="dk-tab"
                  role="tab"
                  id={`designer-tab-${option}`}
                  aria-controls={`designer-panel-${option}`}
                  aria-selected={view === option}
                  tabIndex={view === option ? 0 : -1}
                  onClick={() => switchView(option)}
                >
                  {option === "canvas" ? "Canvas" : "YAML"}
                </button>
              ))}
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
                disabled={view === "yaml"}
                onChange={(event) => toggleEnabled(event.target.checked)}
                aria-label="Workflow state after saving"
                className="switch-input"
              />
              <span className="switch-track" aria-hidden="true" />
              <span className="switch-text">{enabled ? "On" : "Off"}</span>
            </label>
            {enabled !== savedEnabled && <span className="state-save-hint">Save to apply</span>}
            {status.message && (
              <span className={`status-message ${status.kind}`}>
                {status.kind === "busy" && <Loader2 size={20} strokeWidth={1.8} className="spin" />}
                {status.kind === "error" && <TriangleAlert size={20} strokeWidth={1.8} />}
                {status.message}
              </span>
            )}
            <button
              className="dk-button dk-button--secondary topbar-more"
              type="button"
              aria-expanded={moreOpen}
              aria-controls="topbar-secondary"
              onClick={() => setMoreOpen((open) => !open)}
            >
              <span>More</span>
            </button>
            <div id="topbar-secondary" className={moreOpen ? "topbar-secondary open" : "topbar-secondary"} onClick={() => setMoreOpen(false)}>
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
                disabled={status.kind === "busy" || !sourceName}
                title={!sourceName ? "Save the workflow first — duplicates copy the saved file" : undefined}
              >
                <span>Duplicate</span>
              </button>
            )}
            {config.mode === "console" && (
              <button
                className={testOpen ? "button secondary active" : "dk-button dk-button--secondary"}
                type="button"
                onClick={() => { setStoredOpen(false); setTestOpen(!testOpen); }}
                disabled={status.kind === "busy" || view === "yaml"}
                title={view === "yaml" ? "Switch to Canvas to test this workflow" : undefined}
              >
                <span>Test run</span>
              </button>
            )}
            {storedDataAvailable && (
              <button
                className={storedOpen ? "dk-button dk-button--secondary active" : "dk-button dk-button--secondary"}
                type="button"
                aria-pressed={storedOpen}
                onClick={(event) => {
                  if (storedOpen) { setStoredOpen(false); return; }
                  void dakitDialogs.openWhenReady(
                    event.currentTarget,
                    (signal) => listStoredItems(storageRequest, savedId, "", signal),
                    (items) => {
                      flushSync(() => { setTestOpen(false); setStoredItems(items); setStoredOpen(true); });
                    },
                    { label: "Loading stored data…", errorMessage: (error) => `Couldn't list stored keys: ${error.message}` },
                  );
                }}
                disabled={view === "yaml"}
                title={view === "yaml" ? "Switch to Canvas to see stored data" : "The keys this workflow's storage steps read and write"}
              >
                <span>Stored data</span>
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
              disabled={status.kind === "busy" || !dirty}
              title="Throw unsaved changes away — back to the last saved state"
            >
              <span>Revert</span>
            </button>
            </div>
            {/* A clean workflow says so as a state, not a button; the save
               action appears once there is something to save. */}
            {dirty || Object.keys(invalidRawDrafts).length ? (
              <button
                className="dk-button dk-button--primary"
                type="button"
                onClick={save}
                disabled={status.kind === "busy" || Object.keys(invalidRawDrafts).length > 0}
              >
                <span>{Object.keys(invalidRawDrafts).length ? "Fix JSON to save" : draftInfo ? "Save draft" : "Save changes"}</span>
              </button>
            ) : (
              <span className="save-state" role="status">Saved</span>
            )}
          </div>
        </header>

        {view === "yaml" ? (
          <div className="yaml-editor" id="designer-panel-yaml" role="tabpanel" aria-labelledby="designer-tab-yaml">
            <div className="yaml-editor-bar">
              <span className="mono-file">{`workflows/${sourceName ?? `${workflowId}.yaml`}`}</span>
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
        <div className="designer-body" id="designer-panel-canvas" role="tabpanel" aria-labelledby="designer-tab-canvas">
          <WorkflowBoard
            shapes={shapes}
            setShapes={setShapes}
            editShapes={editShapes}
            commitDrag={commitDrag}
            selectedId={selectedId}
            setSelectedId={setSelectedId}
            canPasteStep={clipboardHasStep}
            onDuplicateStep={duplicateStep}
            onCopyStep={copyStep}
            onPasteStep={pasteStep}
            onAddBranchStep={addStepToBranch}
            sessionControls={(actions) => (
              <>
                <button className="icon-button" disabled={!canUndo} onClick={undo} title="Undo (Ctrl+Z)" type="button"><RotateCcw size={20} strokeWidth={1.8} /></button>
                <button className="icon-button" disabled={!canRedo} onClick={redo} title="Redo (Ctrl+Shift+Z)" type="button"><RotateCw size={20} strokeWidth={1.8} /></button>
                <button className="icon-button" onClick={() => setShortcutsOpen(true)} title="Keyboard shortcuts (?)" type="button">
                  <Keyboard size={20} strokeWidth={1.8} />
                </button>
                <button className="icon-button" onClick={actions.clearCanvas} title="Clear canvas" type="button"><X size={20} strokeWidth={1.8} /></button>
              </>
            )}
          />
          {testOpen && (
            <section className="test-panel" aria-label="Test run">
              <header className="test-panel-head">
                <h2>Test run</h2>
                <button className="icon-button" type="button" title="Close" onClick={() => setTestOpen(false)}>
                  <X size={20} strokeWidth={1.8} />
                </button>
              </header>
              <p className="test-hint">
                Dry-run this canvas against a sample event: every step's inputs are rendered, nothing is sent.
                &ldquo;Run&rdquo; executes the actions with live side effects.
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
                  {testBusy ? <Loader2 size={20} strokeWidth={1.8} className="spin" /> : <FlaskConical size={20} strokeWidth={1.8} />}
                  <span>Dry run</span>
                </button>
                <button className="dk-button dk-button--danger" type="button" disabled={testBusy} onClick={() => runTest(true, testStrict)}>
                  <Play size={20} strokeWidth={1.8} /><span>Run</span>
                </button>
                <button
                  className="dk-button dk-button--secondary"
                  type="button"
                  disabled={sampleBusy || testBusy}
                  onClick={pullSample}
                  title="Pull a real sample event for this workflow's trigger connector"
                >
                  {sampleBusy ? <Loader2 size={20} strokeWidth={1.8} className="spin" /> : <CloudDownload size={20} strokeWidth={1.8} />}
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
          {storedOpen && storedDataAvailable && (
            <StoredDataPanel workflowId={savedId} request={storageRequest} initialItems={storedItems} onClose={() => setStoredOpen(false)} />
          )}
          {/* "empty" lets the narrow-frame layout drop the panel until a node
             is selected — on a phone the hints would cost half the canvas. */}
          <aside className={selected?.type === "node" ? "inspector" : "inspector empty"}>
            {selected?.type === "node" && (
              <button className="icon-button inspector-close" type="button" aria-label="Close panel" onClick={() => setSelectedId(null)}>
                <X size={20} strokeWidth={1.8} />
              </button>
            )}
            {selected?.type === "node" && selected.data ? (
              <header className={`inspector-head ${selected.data.nodeKind === "trigger" ? "kind-trigger" : "kind-action"}`}>
                <h2>{selected.data.nodeKind === "trigger" ? "Trigger" : selected.data.isUnionJoin ? "Union" : "Action"}</h2>
                <p className="inspector-summary">{selected.data.nodeKind === "trigger"
                  ? triggerLabel(selected.data.connector ?? "custom", selected.data.event ?? "")
                  : nodeListLabel(selected)}</p>
              </header>
            ) : (
              <header className="inspector-head">
                <h2>Inspector</h2>
              </header>
            )}
            {/* The node list is a way in when nothing is selected; with a node
               open its fields start right under the header (the canvas is
               the map). Entries use the canvas card's title and summary so
               repeated steps (eight Conditions) stay distinguishable. */}
            {selected?.type !== "node" && shapes.some((shape) => shape.type === "node" && !shape.data?.isBranchPlaceholder) && (
              <nav className="node-jump" aria-label="Select a workflow node">
                <p>Steps on the canvas</p>
                {shapes.filter((shape) => shape.type === "node" && !shape.data?.isBranchPlaceholder).map((shape) => (
                  <button
                    key={shape.id}
                    type="button"
                    className={shape.id === selectedId ? "node-jump-item selected" : "node-jump-item"}
                    aria-current={shape.id === selectedId ? "true" : undefined}
                    onClick={() => setSelectedId(shape.id)}
                  >
                    <span>{nodeListLabel(shape)}</span>
                  </button>
                ))}
              </nav>
            )}
            {selected?.type === "node" && selected.data?.nodeKind === "action" && !selected.data?.isUnionJoin && !selected.data?.isBranchPlaceholder && (
              <div className="inspector-toolbar">
                <button className="dk-button dk-button--secondary" type="button" onClick={() => duplicateStep(selected.id)} title="Duplicate this step (Ctrl/Cmd+D)">
                  <Copy size={20} strokeWidth={1.8} /><span>Duplicate</span>
                </button>
                <button className="dk-button dk-button--secondary" type="button" onClick={() => copyStep(selected.id)} title="Copy for pasting into any workflow (Ctrl/Cmd+C, then Ctrl/Cmd+V)">
                  <ClipboardCopy size={20} strokeWidth={1.8} /><span>Copy step</span>
                </button>
                <button className="dk-button dk-button--danger" type="button" onClick={deleteSelectedShape} title="Delete this step (Delete)">
                  <Trash2 size={20} strokeWidth={1.8} /><span>Delete</span>
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
        <LeavePromptDialog
          error={status.kind === "error" ? status.message : null}
          saveDisabled={status.kind === "busy" || Object.keys(invalidRawDrafts).length > 0}
          onStay={() => resolveLeave(false)}
          onDiscard={() => resolveLeave(true)}
          onSave={leaveAfterSave}
        />
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
        <ShortcutsDialog onClose={() => setShortcutsOpen(false)} />
      )}
      {branchPicker && (
        <StepPicker
          mode="action"
          title={branchPicker.title}
          onPick={(kind) => {
            const target = branchPicker;
            setBranchPicker(null);
            addStepToBranch(target.parentNodeId, target.branchKey, kind);
          }}
          onClose={() => setBranchPicker(null)}
        />
      )}
    </div>
  );
}
