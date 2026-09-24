import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

// Console entry: mounted by src/web/designer.html at /designer. Saves go
// through the operator admin API, which commits workflows/*.yaml to GitHub.
const root = document.getElementById("designer-root");
if (root) {
  createRoot(root).render(
    <App config={{ apiBase: "/api/admin/designer", mode: "console", onUnauthorized: () => window.location.assign("/auth/login") }} />
  );
}
