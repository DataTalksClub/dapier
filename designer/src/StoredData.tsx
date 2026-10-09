import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Loader2, X } from "./icons";
import type { Workflow } from "./types";

/* Stored data: the open workflow's key-value state — the store its
   storage_get/set/find/delete steps run on. The panel is the console's way
   to inspect and fix that state; it drives the same /api/admin/storage
   endpoints the CLI's `dapier storage find|set|delete` reach through
   /api/agent/storage, so the two surfaces stay at parity. */

export interface StoredItem {
  key: string;
  value: string;
  updated_at?: string | null;
  expires?: number | null;
}

type Request = <T>(path: string, init?: RequestInit) => Promise<T>;

const STORAGE_TYPE = /^storage_(get|set|find|delete)$/;

/** Whether a workflow reads or writes its storage anywhere — inline
    actions, a named flow, or a nested branch/loop chain. */
export function workflowUsesStorage(actionTypes: Array<string | undefined>, base: Workflow | null): boolean {
  if (actionTypes.some((type) => type !== undefined && STORAGE_TYPE.test(type))) return true;
  return !!base && /"type":"storage_(get|set|find|delete)"/.test(JSON.stringify(base));
}

/** "2026-10-08 20:00" in the viewer's zone; the ISO value rides in title. */
function shortTime(value: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())} ${pad(value.getHours())}:${pad(value.getMinutes())}`;
}

function timeFromIso(iso?: string | null): { text: string; title?: string } {
  if (!iso) return { text: "—" };
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? { text: iso } : { text: shortTime(date), title: date.toISOString() };
}

function expiryLabel(expires?: number | null): { text: string; title?: string } {
  if (!expires) return { text: "Never expires" };
  const date = new Date(expires * 1000);
  return Number.isNaN(date.getTime()) ? { text: `Expires ${expires}` } : { text: `Expires ${shortTime(date)}`, title: date.toISOString() };
}

export function StoredDataPanel({ workflowId, request, onClose }: {
  workflowId: string;
  request: Request;
  onClose: () => void;
}) {
  const ids = useId();
  const base = `/storage/${encodeURIComponent(workflowId)}`;
  const [prefix, setPrefix] = useState("");
  const [items, setItems] = useState<StoredItem[] | null>(null);
  const [listing, setListing] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [deleting, setDeleting] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [ttl, setTtl] = useState("");
  const [storing, setStoring] = useState(false);
  const listedPrefix = useRef("");

  const list = useCallback(async (nextPrefix: string) => {
    setListing(true);
    setError("");
    try {
      const query = nextPrefix ? `?prefix=${encodeURIComponent(nextPrefix)}` : "";
      const data = await request<{ items?: StoredItem[] }>(`${base}${query}`);
      listedPrefix.current = nextPrefix;
      setItems(data.items ?? []);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setListing(false);
    }
  }, [base, request]);

  // Opening the panel (or switching workflows under it) lists every key.
  useEffect(() => {
    setItems(null);
    setExpanded({});
    setNotice("");
    void list("");
  }, [list]);

  async function store(event: FormEvent) {
    event.preventDefault();
    if (storing) return;
    const body: Record<string, unknown> = { key: key.trim(), value };
    if (ttl.trim()) body.ttl_seconds = Number(ttl);
    setStoring(true);
    setError("");
    try {
      await request(base, { method: "POST", body: JSON.stringify(body) });
      setNotice(`Stored ${body.key as string}.`);
      setKey("");
      setValue("");
      setTtl("");
      await list(listedPrefix.current);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setStoring(false);
    }
  }

  async function remove(itemKey: string) {
    setDeleting(itemKey);
    setError("");
    try {
      await request(`${base}?key=${encodeURIComponent(itemKey)}`, { method: "DELETE" });
      setNotice(`Deleted ${itemKey}.`);
      await list(listedPrefix.current);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setDeleting(null);
    }
  }

  return (
    <section className="stored-panel" aria-labelledby={`${ids}-title`}>
      <header className="stored-panel-head">
        <h2 id={`${ids}-title`}>Stored data</h2>
        <button className="icon-button" type="button" aria-label="Close stored data" title="Close" onClick={onClose}>
          <X size={20} strokeWidth={1.8} />
        </button>
      </header>
      <p className="stored-hint">
        Keys this workflow&rsquo;s storage steps read and write. Changes here apply right away, outside of saving.
      </p>

      <form
        className="stored-filter"
        role="search"
        onSubmit={(event) => { event.preventDefault(); void list(prefix.trim()); }}
      >
        <label className="dk-visually-hidden" htmlFor={`${ids}-prefix`}>Key prefix</label>
        <input
          id={`${ids}-prefix`}
          className="dk-input mono-field"
          value={prefix}
          placeholder="Key prefix"
          spellCheck={false}
          onChange={(event) => setPrefix(event.target.value)}
        />
        <button className="dk-button dk-button--secondary" type="submit" disabled={listing}>
          {listing && <Loader2 size={16} strokeWidth={1.8} className="spin" />}
          <span>List keys</span>
        </button>
      </form>

      {(error || notice) && (
        <p className={error ? "stored-status error" : "stored-status"} role={error ? "alert" : "status"}>{error || notice}</p>
      )}

      <div className="stored-register">
        {items === null ? (
          <p className="stored-empty-line">{listing ? "Loading keys…" : "Keys could not be listed."}</p>
        ) : items.length === 0 ? (
          <div className="stored-empty">
            <h3>{listedPrefix.current ? `No keys under ${listedPrefix.current}` : "No stored keys"}</h3>
            <p>Keys appear as this workflow&rsquo;s storage steps run. Store one below to try it.</p>
          </div>
        ) : (
          <ul className="stored-list" aria-label="Stored keys">
            {items.map((item) => {
              const open = !!expanded[item.key];
              const updated = timeFromIso(item.updated_at);
              const expiry = expiryLabel(item.expires);
              return (
                <li key={item.key} className="stored-item">
                  <div className="stored-item-main">
                    <span className="stored-key" title={item.key}>{item.key}</span>
                    <button
                      type="button"
                      className={open ? "stored-value open" : "stored-value"}
                      aria-expanded={open}
                      title={open ? "Collapse the value" : "Show the whole value"}
                      onClick={() => setExpanded((current) => ({ ...current, [item.key]: !open }))}
                    >
                      {item.value === "" ? <em>empty</em> : item.value}
                    </button>
                    <span className="stored-meta">
                      <span title={updated.title}>Updated {updated.text}</span>
                      <span aria-hidden="true"> · </span>
                      <span title={expiry.title}>{expiry.text}</span>
                    </span>
                  </div>
                  <button
                    className="dk-button dk-button--danger dk-button--sm stored-delete"
                    type="button"
                    disabled={deleting === item.key}
                    aria-label={`Delete ${item.key}`}
                    onClick={() => void remove(item.key)}
                  >
                    Delete
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <form className="stored-form" onSubmit={store} aria-labelledby={`${ids}-store`}>
        <h3 id={`${ids}-store`}>Store a value</h3>
        <div className="stored-fields">
          <label className="stored-field">
            <span className="dk-label">Key</span>
            <input className="dk-input mono-field" required value={key} spellCheck={false} placeholder="dedupe:message-123" onChange={(event) => setKey(event.target.value)} />
          </label>
          <label className="stored-field stored-ttl">
            <span className="dk-label">Expires after (seconds)</span>
            <input className="dk-input" type="number" min={1} value={ttl} placeholder="Optional" onChange={(event) => setTtl(event.target.value)} />
          </label>
          <label className="stored-field stored-value-field">
            <span className="dk-label">Value</span>
            <textarea className="dk-textarea mono-field" required rows={2} value={value} spellCheck={false} placeholder="true" onChange={(event) => setValue(event.target.value)} />
          </label>
        </div>
        <div className="dk-form-actions stored-actions">
          <button className="dk-button dk-button--primary" type="submit" disabled={storing}>
            {storing ? "Storing…" : "Store"}
          </button>
        </div>
      </form>
    </section>
  );
}
