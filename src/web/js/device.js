/* Device-pairing approval for `dapier auth login` (see /device).

   Two steps, matching the family's device-login flow: the person
   reviews what is asking (machine label, requesting IP, age, time
   left) and Deny is exactly as easy as Authorize. A ?code= link only
   fills the field — it never approves. One card, one state at a time;
   focus moves to the step heading after every state change, and a
   stray Enter never grants access. */
(function () {
  "use strict";

  // The alphabet the server issues codes from. Validation must accept
  // every code the server can issue; lowercase is folded, never offending.
  var ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789";
  var CODE_LENGTH = 8;
  var SEPARATORS = /[\s\-–—._]/g;
  var CODE_LIFETIME_MINUTES = 15;

  var title = document.getElementById("device-title");
  var lede = document.getElementById("device-lede");
  var body = document.getElementById("device-body");
  var foot = document.getElementById("device-foot");
  var identity = document.getElementById("device-identity");

  var code = "";
  var actor = null;
  var grant = null;
  var phase = "enter";
  var pending = "";
  var failure = null;
  var timer = null;
  var serverOffset = 0;
  var sequence = 0;
  var retryAt = 0;
  var lastAction = null;
  var prefilled = false;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function stopTimer() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  /* ---- the code: fold case and separators, never truncate ---- */

  function compact(raw) {
    return String(raw || "").toUpperCase().replace(SEPARATORS, "");
  }

  function formatCode(raw) {
    var value = compact(raw);
    var out = value.slice(0, 4);
    if (value.length > 4) out += "-" + value.slice(4, CODE_LENGTH);
    if (value.length > CODE_LENGTH) out += "-" + value.slice(CODE_LENGTH);
    return out;
  }

  function normalizeCode(raw) {
    var value = compact(raw);
    var offending = "";
    for (var i = 0; i < value.length; i++) {
      var char = value[i];
      if ((char < "A" || char > "Z") && (char < "0" || char > "9")) continue;
      if (ALPHABET.indexOf(char) === -1 && !offending) offending = char;
    }
    if (!value) return { ok: false, error: "empty", display: "", length: 0 };
    if (value.length < CODE_LENGTH)
      return { ok: false, error: "too_short", display: formatCode(value), length: value.length };
    if (value.length > CODE_LENGTH)
      return { ok: false, error: "too_long", display: formatCode(value), length: value.length };
    if (offending)
      return { ok: false, error: "invalid_char", display: formatCode(value),
               length: value.length, offending: offending };
    return { ok: true, code: value, display: formatCode(value), length: value.length };
  }

  function describeError(result) {
    if (result.error === "too_short")
      return "Codes have 8 characters. This one has " + result.length + ".";
    if (result.error === "too_long")
      return "Codes have 8 characters. This one has " + result.length +
        ". Check for an extra character.";
    if (result.error === "invalid_char")
      return "Codes don\u2019t use \u201C" + result.offending +
        "\u201D. Check the code in your terminal.";
    return "";
  }

  /* ---- plumbing ---- */

  function api(path, options) {
    options = options || {};
    return fetch(path, {
      method: options.method || "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: options.body ? { "content-type": "application/json" } : {},
      body: options.body,
    }).then(function (response) {
      var retryAfter = Number(response.headers.get("retry-after")) || 60;
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var error = new Error((data && data.error) || "Request failed");
          error.status = response.status;
          error.retryAfter = retryAfter;
          throw error;
        }
        return data;
      });
    });
  }

  function setLede(text) {
    lede.textContent = text;
  }

  function render(focus) {
    stopTimer();
    body.replaceChildren();
    foot.replaceChildren();
    title.classList.remove("device-ok");
    if (failure) renderFailure();
    else if (phase === "review") renderReview();
    else if (phase === "approved" || phase === "denied") renderResult();
    else renderEnter();
    if (focus !== false) title.focus({ preventScroll: true });
  }

  /* ---- states ---- */

  function renderEnter() {
    title.textContent = "Authorize a device";
    setLede("A dapier CLI asked to sign in as you. Enter the code shown in your terminal.");
    var form = el("form", "device-form");
    var field = el("div", "device-field");
    var label = el("label", "", "Device code");
    label.htmlFor = "pair-code";
    var input = el("input");
    input.id = "pair-code";
    input.type = "text";
    input.value = code;
    input.placeholder = "XXXX-XXXX";
    input.autocomplete = "off";
    input.autocapitalize = "characters";
    input.spellcheck = false;
    input.enterKeyHint = "go";
    input.setAttribute("aria-describedby", "pair-hint pair-error");
    var hint = el("p", "device-hint",
      prefilled ? "Check that this matches your terminal exactly." : "");
    hint.id = "pair-hint";
    var error = el("p", "device-error");
    error.id = "pair-error";
    error.setAttribute("role", "alert");
    field.append(label, input, hint, error);
    var actions = el("div", "device-actions");
    var submit = el("button", "device-primary", "Continue");
    submit.type = "submit";
    actions.append(submit);
    form.append(field, actions);

    function update(submitted) {
      var result = normalizeCode(input.value);
      submit.disabled = !result.ok || Boolean(pending) || !actor;
      submit.setAttribute("aria-busy", String(Boolean(pending)));
      error.textContent =
        !result.ok && (submitted || (result.error !== "too_short" && result.error !== "empty"))
          ? describeError(result) : "";
      input.setAttribute("aria-invalid", error.textContent ? "true" : "false");
    }
    input.addEventListener("input", function () {
      var caretAtEnd = input.selectionStart === input.value.length;
      code = input.value;
      input.value = formatCode(input.value);
      if (caretAtEnd) input.setSelectionRange(input.value.length, input.value.length);
      sequence++; // an edit cancels any in-flight lookup
      pending = "";
      submit.textContent = "Continue";
      update(false);
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      update(true);
      if (!submit.disabled) lookup();
    });
    body.append(form);
    update(false);
  }

  function renderReview() {
    title.textContent = "Authorize this device?";
    setLede("It will be able to act as " + (actor || "your signed-in account") +
      " in dapier for 30 days, until you sign it out.");

    var started = new Date(grant.created_at);
    var minutes = Math.max(0, Math.floor((Date.now() + serverOffset - started.getTime()) / 60000));
    var facts = el("dl", "device-facts");
    [["Code", code, false],
     ["Machine", grant.label || "unknown machine", true],
     ["Requested from", grant.request_ip || "unknown", false],
     ["Started", started.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) +
       " \u00B7 " + minutes + " min ago", false],
     ["Expires in", "", false]].forEach(function (row) {
      var dd = el("dd", "device-mono");
      dd.append(el("span", "", row[1]));
      if (row[2]) dd.append(el("small", "device-reported", "reported by the machine"));
      if (row[0] === "Expires in") {
        dd.id = "device-countdown";
        dd.setAttribute("aria-live", "off");
      }
      facts.append(el("dt", "", row[0]), dd);
    });
    body.append(facts);

    var actions = el("div", "device-actions device-actions--split");
    var deny = el("button", "device-secondary", pending === "deny" ? "Denying\u2026" : "Deny");
    var approve = el("button", "device-primary",
      pending === "approve" ? "Authorizing\u2026" : "Authorize");
    deny.type = "button";
    approve.type = "button";
    deny.disabled = Boolean(pending);
    approve.disabled = Boolean(pending);
    if (pending) {
      deny.setAttribute("aria-busy", "true");
      approve.setAttribute("aria-busy", "true");
    }
    deny.addEventListener("click", function () { decide(false); });
    approve.addEventListener("click", function () { decide(true); });
    actions.append(deny, approve);
    body.append(actions);

    foot.append(el("p", "", "Didn\u2019t start this? Deny it. Nothing is shared."));

    var tick = function () {
      var countdown = document.getElementById("device-countdown");
      if (!countdown) return;
      var seconds = Math.max(0, Math.ceil((grant.expires_at * 1000 - Date.now() - serverOffset) / 1000));
      countdown.textContent = Math.floor(seconds / 60) + ":" +
        String(seconds % 60).padStart(2, "0");
      countdown.classList.toggle("device-expiring", seconds < 60);
      if (!seconds && !pending) {
        failure = {
          kind: "expired", title: "Code expired", tone: "device-err",
          message: "Codes last " + CODE_LIFETIME_MINUTES +
            " minutes. Run `dapier auth login` again for a new one.",
          action: "Enter another code",
        };
        render();
      }
    };
    timer = setInterval(tick, 1000);
    tick();
  }

  function renderResult() {
    var approved = phase === "approved";
    title.textContent = approved ? "Device authorized" : "Request denied";
    setLede(approved
      ? "Return to your terminal. The CLI finishes signing in within a few seconds."
      : "Nothing was authorized and the code no longer works. " +
        "If you meant to sign in, run `dapier auth login` again.");
    if (!approved) return;
    title.classList.add("device-ok");
    var command = el("div", "device-command");
    var pre = el("pre");
    var line = el("code", "", "dapier auth logout");
    pre.append(line);
    var copy = el("button", "device-secondary device-copy", "Copy");
    copy.type = "button";
    copy.addEventListener("click", function () {
      if (navigator.clipboard) navigator.clipboard.writeText("dapier auth logout");
      copy.textContent = "Copied";
    });
    command.append(pre, copy, el("p", "device-hint", "Run this on that machine to sign it out."));
    body.append(command);
  }

  function renderFailure() {
    title.textContent = failure.title;
    setLede("");
    var alert = el("div", "device-callout" + (failure.tone ? " " + failure.tone : ""));
    alert.setAttribute("role", "alert");
    alert.append(el("span", "", failure.message));
    body.append(alert);
    var actions = el("div", "device-actions");
    var recovery = el("button", "device-primary", failure.action);
    recovery.type = "button";
    recovery.addEventListener("click", function () {
      if (failure.kind === "signin") {
        window.location.assign(signInHref());
        return;
      }
      if (failure.kind === "notfound" || failure.kind === "expired") {
        sequence++;
        code = "";
        grant = null;
        phase = "enter";
        prefilled = false;
        failure = null;
        render();
        return;
      }
      failure = null;
      lastAction();
    });
    actions.append(recovery);
    body.append(actions);
    if (failure.kind === "ratelimit" && retryAt > Date.now()) {
      var tick = function () {
        var seconds = Math.max(0, Math.ceil((retryAt - Date.now()) / 1000));
        recovery.disabled = seconds > 0;
        recovery.textContent = seconds ? "Try again (" + seconds + "s)" : "Try again";
        if (!seconds) stopTimer();
      };
      tick();
      timer = setInterval(tick, 1000);
    }
  }

  /* ---- actions ---- */

  function lookup() {
    if (pending || !actor) return;
    var result = normalizeCode(code);
    if (!result.ok) return;
    var token = ++sequence;
    pending = "lookup";
    lastAction = lookup;
    var submit = body.querySelector('button[type="submit"]');
    if (submit) {
      submit.disabled = true;
      submit.textContent = "Checking\u2026";
      submit.setAttribute("aria-busy", "true");
    }
    api("/api/agent/device/pending?code=" + encodeURIComponent(result.display))
      .then(function (data) {
        if (token !== sequence) return;
        pending = "";
        grant = data;
        code = result.display;
        serverOffset = (data.server_now || Date.now() / 1000) * 1000 - Date.now();
        phase = "review";
        render();
      })
      .catch(function (error) {
        if (token !== sequence) return;
        pending = "";
        fail(error);
      });
  }

  function decide(approve) {
    if (pending || phase !== "review") return;
    var token = ++sequence;
    pending = approve ? "approve" : "deny";
    render(false);
    api("/api/agent/device/approve", {
      method: "POST",
      body: JSON.stringify({ user_code: code, approve: approve }),
    }).then(function (result) {
      if (token !== sequence) return;
      pending = "";
      phase = result.status;
      grant = null;
      render();
    }).catch(function (error) {
      if (token !== sequence) return;
      pending = "";
      fail(error);
    });
  }

  function fail(error) {
    if (error && (error.status === 404 || error.status === 409))
      failure = {
        kind: "notfound", title: "Code not found", tone: "device-err",
        message: "This code is wrong, expired, or already used. " +
          "Run `dapier auth login` again for a new one.",
        action: "Enter another code",
      };
    else if (error && error.status === 401) {
      actor = null;
      updateIdentity();
      failure = {
        kind: "signin", title: "Sign in to continue", tone: "device-warn",
        message: "Sign in to dapier to authorize this device. You\u2019ll come back here with your code.",
        action: "Sign in",
      };
    } else if (error && error.status === 429) {
      retryAt = Date.now() + (error.retryAfter || 60) * 1000;
      failure = {
        kind: "ratelimit", title: "Slow down", tone: "device-warn",
        message: "Too many codes tried from this account. Wait a few minutes and try again.",
        action: "Try again",
      };
    } else
      failure = {
        kind: "network", title: "Couldn\u2019t reach dapier", tone: "",
        message: "Check your connection and try again. Your code is still valid until it expires.",
        action: "Try again",
      };
    render();
  }

  /* ---- identity ---- */

  function signInHref() {
    return "/auth/login?next=" + encodeURIComponent("/device" + (code ? "?code=" + code : ""));
  }

  function updateIdentity() {
    identity.replaceChildren();
    identity.append(el("span", "",
      actor ? "Signed in as " + actor + ". Approve only if the code matches your terminal."
            : "Sign in to approve this dapier CLI request."));
    var link = el("a", "", actor ? "Switch account" : "Sign in");
    link.href = signInHref();
    identity.append(document.createTextNode(" "));
    identity.append(link);
  }

  function refreshIdentity() {
    fetch("/api/admin/me", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("anonymous");
        return response.json();
      })
      .then(function (me) {
        if (!me || !me.username) throw new Error("anonymous");
        actor = me.username;
        updateIdentity();
        var submit = body.querySelector('button[type="submit"]');
        if (submit && phase === "enter" && !pending)
          submit.disabled = !normalizeCode(code).ok;
      })
      .catch(function () {
        actor = null;
        updateIdentity();
        var submit = body.querySelector('button[type="submit"]');
        if (submit && phase === "enter") submit.disabled = true;
      });
  }

  var params = new URLSearchParams(window.location.search);
  code = formatCode(params.get("code") || "");
  prefilled = Boolean(code);
  if (code) history.replaceState(null, "", "/device");
  updateIdentity();
  render();
  refreshIdentity();
})();
