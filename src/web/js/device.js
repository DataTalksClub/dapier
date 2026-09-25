/* Device-pairing approval for `dapier auth login` (see /device). */
(function () {
  "use strict";

  var codeInput = document.getElementById("pair-code");
  var button = document.getElementById("pair-button");
  var status = document.getElementById("pair-status");

  function note(text, kind) {
    status.textContent = text;
    status.className = "device-status" + (kind ? " " + kind : "");
  }

  function signInLink(code) {
    return "/auth/login?next=" + encodeURIComponent("/device?code=" + code);
  }

  function approve(code) {
    button.disabled = true;
    note("Checking the code \u2026");
    fetch("/api/agent/device/confirm", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ user_code: code }),
    }).then(function (response) {
      if (response.status === 401) {
        status.innerHTML = "Sign in first \u2014 <a id=\"pair-signin\" href=\"" + signInLink(code) + "\">sign in and approve</a>.";
        status.className = "device-status err";
        button.disabled = false;
        return null;
      }
      return response.json().then(function (body) {
        return { ok: response.ok, body: body };
      });
    }).then(function (result) {
      if (!result) return;
      if (result.ok) {
        codeInput.value = "";
        button.disabled = true;
        note("Approved \u2014 return to your terminal.", "ok");
      } else {
        note((result.body && result.body.error) || "Could not approve this code.", "err");
        button.disabled = false;
      }
    }).catch(function () {
      note("Network error; try again.", "err");
      button.disabled = false;
    });
  }

  document.getElementById("pair-form").addEventListener("submit", function (event) {
    event.preventDefault();
    approve(codeInput.value);
  });

  var prefill = new URLSearchParams(window.location.search).get("code");
  if (prefill) {
    codeInput.value = prefill;
    approve(prefill);
  }
  codeInput.focus();
})();
