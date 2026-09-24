import { readFileSync, writeFileSync } from "node:fs";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

/** Keeps a pointer to the readable source at the top of the committed bundle.
 *  Build tooling strips source comments, so this is applied after the build. */
function sourceBanner(): Plugin {
  const banner =
    "/* BUILD ARTIFACT - do not edit by hand.\n" +
    " *  Normal-code source of truth: designer/src/ (entry: src/console.tsx).\n" +
    " *  Rebuild into src/web/ with: make designer-console */\n";
  return {
    name: "source-banner",
    closeBundle() {
      for (const name of ["designer.js", "designer.css"]) {
        const target = new URL(`../src/web/${name}`, import.meta.url);
        writeFileSync(target, banner + readFileSync(target, "utf8"));
      }
    },
  };
}

// Vendored console bundle: an IIFE that mounts the designer into the dapier
// console at /designer (src/web/designer.js + designer.css, committed like
// lucide.min.js). Rebuild with `make designer-console` after changing sources;
// the console has no build step of its own.
export default defineConfig({
  plugins: [react(), sourceBanner()],
  // Dependencies ship bare process.env.NODE_ENV references; without this the
  // IIFE crashes in the browser (no process global on a page).
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    // Shipped readable: the console has no build step, so the committed bundle
    // is the code reviewers see. CSS follows the same switch.
    minify: false,
    outDir: "../src/web",
    emptyOutDir: false,
    lib: {
      entry: "src/console.tsx",
      formats: ["iife"],
      name: "DapierDesigner",
      fileName: () => "designer.js"
    },
    rollupOptions: {
      output: {
        assetFileNames: "designer.[ext]",
        banner:
          "/* BUILD ARTIFACT - do not edit. Source: designer/src/ (entry: console.tsx)," +
          " rebuilt into this file by: make designer-console */\n",
      },
    },
    cssCodeSplit: false
  }
});
