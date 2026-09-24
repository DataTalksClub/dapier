import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Vendored console bundle: an IIFE that mounts the designer into the dapier
// console at /designer (src/web/designer.js + designer.css, committed like
// lucide.min.js). Rebuild with `make designer-console` after changing sources;
// the console has no build step of its own.
export default defineConfig({
  plugins: [react()],
  // Dependencies ship bare process.env.NODE_ENV references; without this the
  // IIFE crashes in the browser (no process global on a page).
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    outDir: "../src/web",
    emptyOutDir: false,
    lib: {
      entry: "src/console.tsx",
      formats: ["iife"],
      name: "DapierDesigner",
      fileName: () => "designer.js"
    },
    rollupOptions: { output: { assetFileNames: "designer.[ext]" } },
    cssCodeSplit: false
  }
});
