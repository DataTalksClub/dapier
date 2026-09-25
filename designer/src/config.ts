/** Where the workflow API lives and which chrome the designer renders. */
export interface DesignerConfig {
  /** Base path of the workflow API: local companion server or console admin API. */
  apiBase: string;
  /** "local" renders local-git chrome (push button, branch status); "console" commits via the deployed API. */
  mode: "local" | "console";
  /** Sidebar-less rendering for the console's embedded designer view (src/web/js/views/designer.js). */
  embedded?: boolean;
  /** Called when the API answers 401, e.g. to redirect to the console sign-in. */
  onUnauthorized?: () => void;
}

export const localConfig: DesignerConfig = { apiBase: "/api", mode: "local" };
