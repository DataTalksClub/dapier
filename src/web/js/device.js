/* Device-pairing approval for `dapier auth login` (see /device).

   dakit's device-login page (docs/device-login.md): one .dk-auth-card,
   one state at a time — Enter the code, Review what is asking (machine
   label, requesting IP, age, time left, a different-network warning) and
   Deny or Authorize with equal weight, then a Result. A ?code= link only
   fills the field; it never looks up or approves. Focus moves to the
   state's heading after every change, so a stray Enter never grants
   access. Code normalising and the code field come from the vendored
   dakit device-code.js; validation accepts exactly the alphabet the
   server issues (dakit's canonical one). */
import {
  USER_CODE_ALPHABET,
  normalizeUserCode,
  formatUserCode,
  describeUserCodeError,
  bindCodeInput,
} from "/assets/vendor/device-code.js";

/* ---- canonical host ---- */

// The CLI can be pointed at a raw API host; this page is only useful on
// the canonical console origin, where the sign-in session cookie lives.
// Raw AWS API hosts bounce there (keeping the code); every other host --
// canonical, localhost, a custom domain -- is left alone.
var CANONICAL_ORIGIN = "https://dapier.dtcdev.click";

function isRawApiHost(hostname) {
  return typeof hostname === "string" &&
    (hostname.indexOf(".lambda-url.") !== -1 ||
     hostname.slice(-7) === ".on.aws" ||
     hostname.indexOf(".execute-api.") !== -1);
}

function canonicalDeviceRedirect(hostname, rawCode) {
  if (!isRawApiHost(hostname)) return null;
  return CANONICAL_ORIGIN + "/device" +
    (rawCode ? "?code=" + encodeURIComponent(rawCode) : "");
}

/* ---- end canonical host ---- */

const APP = "Dapier";
const CLI = "dapier";
const CODE_LIFETIME_MINUTES = 15;   // device_sessions.PAIRING_TTL_SECONDS
const SESSION_LIFETIME_DAYS = 30;   // device_sessions.SESSION_TTL_SECONDS

const card = document.getElementById("device-card");
const identity = document.getElementById("device-identity");

let actor = null;      // the signed-in email, or null
let code = "";         // the code as typed / reviewed (display form)
let prefilled = false;
let seq = 0;           // request sequencing: a stale response never paints
let timer = null;
let lastAction = null; // what "Try again" repeats

const ICONS = {
  check: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 20h16a2 2 0 0 0 1.73-2"/><path d="M12 9v4M12 17h.01"/>',
};

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function icon(name, cls) {
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
}

function api(path, options = {}) {
  return fetch(path, {
    method: options.method || "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers: options.body ? { "content-type": "application/json" } : {},
    body: options.body,
  }).then((response) => {
    const retryAfter = Number(response.headers.get("retry-after")) || 60;
    return response.json().catch(() => ({})).then((data) => {
      if (!response.ok) {
        const error = new Error((data && data.error) || "Request failed");
        error.status = response.status;
        error.retryAfter = retryAfter;
        throw error;
      }
      return data;
    });
  });
}

function signInHref() {
  return "/auth/login?next=" + encodeURIComponent("/device" + (code ? "?code=" + formatUserCode(code) : ""));
}

/* The identity line: which account the grant goes to, and a way out. */
function renderIdentity() {
  identity.innerHTML = actor
    ? `${esc(actor)} · <a href="${esc(signInHref())}">Switch account</a>`
    : `Not signed in · <a href="${esc(signInHref())}">Sign in</a>`;
}

function paint(html, { focusTitle = true } = {}) {
  clearInterval(timer);
  timer = null;
  card.innerHTML = html;
  if (focusTitle) card.querySelector(".dk-auth-card__title")?.focus();
}

function head(title, lede, { iconHtml = "", status = false } = {}) {
  return `<div class="dk-auth-card__head"${status ? ' role="status"' : ""}>
    <h1 class="dk-auth-card__title" id="card-title" tabindex="-1">${iconHtml}${title}</h1>
    ${lede ? `<p class="dk-auth-card__lede">${lede}</p>` : ""}</div>`;
}

/* ---- Enter ---- */

function renderEnter({ error = "" } = {}) {
  paint(`${head("Authorize a device", `${APP} CLI asked to sign in as you. Enter the code shown in your terminal.`)}
    <form class="dk-auth-card__body" id="enter" novalidate>
      <div>
        <label class="dk-label" for="device-code">Device code</label>
        <input class="dk-input dk-code-input" id="device-code" name="code" placeholder="XXXX-XXXX"
          autocomplete="off" autocapitalize="characters" spellcheck="false" inputmode="text" enterkeyhint="go"
          aria-describedby="code-hint code-error" value="${esc(formatUserCode(code))}">
        <p class="dk-hint" id="code-hint"${prefilled ? "" : " hidden"}>Check that this matches your terminal exactly.</p>
        <p class="dk-field-error" id="code-error" role="alert"${error ? "" : " hidden"}>${esc(error)}</p>
      </div>
      <div class="dk-auth-card__actions"><button class="dk-button dk-button--primary dk-button--lg" type="submit" id="continue">Continue</button></div>
    </form>`, { focusTitle: prefilled || Boolean(error) });
  const input = card.querySelector("#device-code");
  const button = card.querySelector("#continue");
  const errorEl = card.querySelector("#code-error");
  let result = normalizeUserCode(code);
  const showError = (message) => {
    errorEl.textContent = message;
    errorEl.hidden = !message;
    input.setAttribute("aria-invalid", String(Boolean(message)));
  };
  const sync = () => { button.disabled = !result.ok; };
  input.addEventListener("dk-code-change", (event) => {
    result = event.detail;
    code = input.value;
    seq++; // an edit cancels any in-flight lookup
    button.textContent = "Continue";
    button.removeAttribute("aria-busy");
    sync();
    // Too long and invalid characters show while typing; too short waits.
    showError(result.error === "too_long" || result.error === "invalid_char" ? describeUserCodeError(result) : "");
  });
  bindCodeInput(input, { alphabet: USER_CODE_ALPHABET });
  if (error) input.setAttribute("aria-invalid", "true");
  sync();
  if (!prefilled && !error) input.focus();
  card.querySelector("#enter").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!result.ok) { showError(describeUserCodeError(result)); input.focus(); return; }
    if (!actor) { renderError("signin"); return; }
    lookup(result.display, button);
  });
}

function lookup(display, button) {
  const mine = ++seq;
  code = display;
  lastAction = () => lookup(display);
  if (button) {
    button.disabled = true;
    button.textContent = "Checking…";
    button.setAttribute("aria-busy", "true");
  }
  api("/api/agent/device/pending?code=" + encodeURIComponent(display))
    .then((grant) => { if (mine === seq) renderReview(display, grant); })
    .catch((error) => { if (mine === seq) fail(error); });
}

/* ---- Review ---- */

function ago(ms) {
  const minutes = Math.max(0, Math.floor(ms / 60000));
  return minutes < 1 ? "just now" : minutes === 1 ? "1 min ago" : `${minutes} min ago`;
}

function renderReview(display, grant) {
  const offset = Date.now() - (grant.server_now || Date.now() / 1000) * 1000; // server clock → local
  const started = new Date(grant.created_at);
  const startedAgo = Number.isNaN(started.getTime()) ? "" : ago(Date.now() - offset - started.getTime());
  const warn = grant.same_network === false;
  paint(`${head("Authorize this device?", `It will be able to act as <strong>${esc(actor || "your account")}</strong> in ${APP} for ${SESSION_LIFETIME_DAYS} days, until you sign it out.`)}
    <div class="dk-auth-card__body">
      <dl class="dk-dl device-facts">
        <dt>Code</dt><dd class="dk-mono">${esc(display)}</dd>
        <dt>Machine</dt><dd><span class="dk-mono">${esc(grant.label || "unknown machine")}</span><span class="dk-dl__note">reported by the machine</span></dd>
        <dt>Requested from</dt><dd class="dk-mono">${esc(grant.request_ip || "unknown")}</dd>
        <dt>Started</dt><dd class="dk-mono">${Number.isNaN(started.getTime()) ? "—" : `${esc(started.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))} · ${esc(startedAgo)}`}</dd>
        <dt>Expires in</dt><dd class="dk-mono" id="expires"></dd>
      </dl>
      ${warn ? `<div class="dk-callout dk-callout--warning" id="net-warning">
        ${icon("alert", "dk-callout__icon")}
        <div class="dk-callout__body">
          <span class="dk-callout__title">This request came from a different network.</span>
          <span>Only continue if you started this login yourself on that machine.</span>
          <label class="dk-check"><input type="checkbox" id="ack"> I started this login on that machine</label>
        </div></div>` : ""}
      <div class="dk-auth-card__actions">
        <button class="dk-button dk-button--secondary dk-button--lg" type="button" id="deny">Deny</button>
        <button class="dk-button dk-button--primary dk-button--lg" type="button" id="approve"${warn ? ' disabled aria-describedby="net-warning"' : ""}>Authorize</button>
      </div>
    </div>
    <div class="dk-auth-card__foot"><span>Didn’t start this? Deny it. Nothing is shared.</span><span>Wrong account? <a href="${esc(signInHref())}">Switch account</a></span></div>`);
  const expires = card.querySelector("#expires");
  let announced = false;
  const tick = () => {
    const left = Math.max(0, Math.round((grant.expires_at * 1000 - (Date.now() - offset)) / 1000));
    expires.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    if (left <= 60) {
      expires.insertAdjacentHTML("beforeend", ' <span class="dk-badge dk-badge--warning">ending</span>');
      if (!announced) { announced = true; globalThis.dakitShell?.announce("One minute left to decide"); }
    }
    if (left === 0 && !card.querySelector("[aria-busy]")) renderError("expired");
  };
  tick();
  timer = setInterval(tick, 1000);
  const approve = card.querySelector("#approve");
  const deny = card.querySelector("#deny");
  card.querySelector("#ack")?.addEventListener("change", (event) => { approve.disabled = !event.target.checked; });
  approve.addEventListener("click", () => decide(display, true, approve, deny));
  deny.addEventListener("click", () => decide(display, false, approve, deny));
}

/* Both decisions go through the same endpoint and the same guards; the
   first click disables both, so neither can be sent twice. */
function decide(display, approve, approveButton, denyButton) {
  const mine = ++seq;
  lastAction = null;
  approveButton.disabled = denyButton.disabled = true;
  const busy = approve ? approveButton : denyButton;
  busy.textContent = approve ? "Authorizing…" : "Denying…";
  busy.setAttribute("aria-busy", "true");
  api("/api/agent/device/approve", {
    method: "POST",
    body: JSON.stringify({ user_code: display, approve }),
  }).then((result) => { if (mine === seq) renderResult(result.status); })
    .catch((error) => { if (mine === seq) fail(error); });
}

/* ---- Result ---- */

function renderResult(status) {
  if (status === "approved") {
    paint(`${head("Device authorized", "Return to your terminal. The CLI finishes signing in within a few seconds.", { iconHtml: icon("check", "dk-auth-card__icon--success"), status: true })}
      <div class="dk-auth-card__body">
        <div class="dk-code-block"><pre><code>${CLI} auth logout</code></pre><button class="dk-button dk-button--secondary dk-button--sm" type="button" data-dk-copy>Copy</button></div>
        <p class="dk-hint">Run this on that machine to sign it out.</p>
      </div>`);
  } else {
    paint(head("Request denied", `Nothing was authorized and the code no longer works. If you meant to sign in, run <code>${CLI} auth login</code> again.`, { status: true }));
  }
}

/* ---- Errors: one sentence each, and one way forward ---- */

const ERRORS = {
  notfound: ["Code not found", "danger", `This code is wrong, expired, or already used. Run <code>${CLI} auth login</code> again for a new one.`, "Enter another code"],
  expired: ["Code expired", "danger", `Codes last ${CODE_LIFETIME_MINUTES} minutes. Run <code>${CLI} auth login</code> again for a new one.`, "Enter another code"],
  ratelimited: ["Slow down", "warning", "Too many codes tried from this account. Wait a few minutes and try again.", "Try again"],
  signin: ["Sign in to continue", "warning", `Sign in to ${APP} to authorize this device. You’ll come back here with your code.`, "Sign in"],
  network: [`Couldn’t reach ${APP}`, "", "Check your connection and try again. Your code is still valid until it expires.", "Try again"],
};

function fail(error) {
  const status = error && error.status;
  if (status === 404 || status === 409) renderError("notfound");
  else if (status === 401) { actor = null; renderIdentity(); renderError("signin"); }
  else if (status === 429) renderError("ratelimited", { retryAfter: error.retryAfter });
  else renderError("network");
}

function renderError(kind, { retryAfter = 0 } = {}) {
  const [title, tone, sentence, action] = ERRORS[kind];
  paint(`${head(title, "")}
    <div class="dk-auth-card__body">
      <div class="dk-callout${tone ? ` dk-callout--${tone}` : ""}" role="alert"><div class="dk-callout__body"><span>${sentence}</span></div></div>
      <div class="dk-auth-card__actions"><button class="dk-button dk-button--secondary dk-button--lg" type="button" id="recover">${action}</button></div>
    </div>`);
  const recover = card.querySelector("#recover");
  recover.addEventListener("click", () => {
    if (kind === "signin") { window.location.assign(signInHref()); return; }
    if (kind === "notfound" || kind === "expired") {
      seq++;
      code = "";
      prefilled = false;
      renderEnter();
      return;
    }
    if (lastAction) lastAction(); else renderEnter();
  });
  if (kind === "ratelimited") {
    const until = Date.now() + retryAfter * 1000;
    const tick = () => {
      const seconds = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      recover.disabled = seconds > 0;
      recover.innerHTML = seconds ? `Try again <span class="dk-mono">(${seconds}s)</span>` : "Try again";
      if (!seconds) { clearInterval(timer); timer = null; }
    };
    tick();
    timer = setInterval(tick, 1000);
  }
}

/* ---- boot ---- */

function refreshIdentity() {
  return fetch("/api/admin/me", { credentials: "same-origin", cache: "no-store" })
    .then((response) => (response.ok ? response.json() : null))
    .then((me) => { actor = (me && (me.email || me.username)) || null; })
    .catch(() => { actor = null; })
    .then(() => {
      renderIdentity();
    });
}

var params = new URLSearchParams(window.location.search);
code = formatUserCode(params.get("code") || "");
prefilled = Boolean(code);
var bounce = canonicalDeviceRedirect(window.location.hostname, code);
if (bounce) {
  window.location.replace(bounce);
} else {
  // The code never lingers in history once it is in the field.
  if (code) history.replaceState(null, "", "/device");
  renderEnter();
  refreshIdentity();
}
