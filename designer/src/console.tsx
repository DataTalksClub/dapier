import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ConsoleShell } from "./ConsoleShell";
import "./styles.css";

// Console entry: mounted by src/web/designer.html at /designer/app. Saves go
// through the operator admin API, which commits workflows/*.yaml to GitHub.
// With ?embed=1 the console's designer view frames this page, so the app
// renders without its own sidebar and sign-in replaces the whole console.

// Dark mode follows the console: it owns the choice under "dakit-theme"
// (the shared design system's key — same origin, applied pre-paint by the
// head script in designer.html), and a storage event lands here when the
// operator toggles, so an open designer re-skins without a reload.
const THEME_KEY = "dakit-theme";

function storedTheme() {
  try {
    const theme = localStorage.getItem(THEME_KEY);
    return theme === "dark" || theme === "light" ? theme : null;
  } catch { /* private mode */ return null; }
}

function applyStoredTheme() {
  let theme = storedTheme();
  if (theme === null) {
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
  if (event.key === THEME_KEY || event.key === null) applyStoredTheme();
});

const root = document.getElementById("designer-root");
if (root) {
  const embedded = new URLSearchParams(window.location.search).get("embed") === "1";
  const app = (
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
  // Standalone, the designer sits in dakit's topbar shell; framed by the
  // console's /designer view, the console shell is the chrome.
  createRoot(root).render(embedded ? app : <ConsoleShell>{app}</ConsoleShell>);
}
