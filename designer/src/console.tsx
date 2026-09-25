import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

// Console entry: mounted by src/web/designer.html at /designer/app. Saves go
// through the operator admin API, which commits workflows/*.yaml to GitHub.
// With ?embed=1 the console's /designer view frames this page, so the app
// renders without its own sidebar and sign-in replaces the whole console.

// Dark mode follows the console: it owns the choice under "dapier-theme"
// (same origin, applied pre-paint by the head script in designer.html), and a
// storage event lands here when the operator toggles, so an open designer
// re-skins without a reload.
function applyStoredTheme() {
  let theme = null;
  try { theme = localStorage.getItem("dapier-theme"); } catch { /* private mode */ }
  if (theme !== "dark" && theme !== "light") {
    // No stored choice: the head script already decided (OS preference, or a
    // ?theme= override) — keep its decision rather than re-deriving it.
    const applied = document.documentElement.dataset.theme;
    theme = applied === "dark" || applied === "light"
      ? applied
      : window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.dataset.theme = theme;
}
applyStoredTheme();
window.addEventListener("storage", (event) => {
  if (event.key === "dapier-theme" || event.key === null) applyStoredTheme();
});

const root = document.getElementById("designer-root");
if (root) {
  const embedded = new URLSearchParams(window.location.search).get("embed") === "1";
  createRoot(root).render(
    <App
      config={{
        apiBase: "/api/admin/designer",
        mode: "console",
        embedded,
        onUnauthorized: () => {
          (embedded && window.top ? window.top : window).location.assign("/auth/login");
        }
      }}
    />
  );
}
