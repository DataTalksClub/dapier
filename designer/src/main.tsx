import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

// Standalone entry: served by server.mjs at / for local workflow editing.
// Dark mode follows the shared "dakit-theme" key — index.html applies it
// pre-paint, and a storage event lands here when another tab toggles, so an
// open designer re-skins without a reload. The pre-dakit "dapier-theme" key
// is still honored until the next toggle.
const THEME_KEY = "dakit-theme";
const LEGACY_THEME_KEY = "dapier-theme";

function storedTheme() {
  try {
    const theme = localStorage.getItem(THEME_KEY);
    if (theme === "dark" || theme === "light") return theme;
    const legacy = localStorage.getItem(LEGACY_THEME_KEY);
    return legacy === "dark" || legacy === "light" ? legacy : null;
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
  if (event.key === THEME_KEY || event.key === LEGACY_THEME_KEY || event.key === null) applyStoredTheme();
});

createRoot(document.getElementById("root")!).render(<App />);
