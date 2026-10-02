/* Overview sections merge into shared state as each independent read arrives. */
export const state = {
  data: null, loadedSections: new Set(), errors: null, workflowSearchIds: null, view: 'overview',
};
