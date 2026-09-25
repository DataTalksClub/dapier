import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

// Console entry: mounted by src/web/designer.html at /designer/app. Saves go
// through the operator admin API, which commits workflows/*.yaml to GitHub.
// With ?embed=1 the console's /designer view frames this page, so the app
// renders without its own sidebar and sign-in replaces the whole console.
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
