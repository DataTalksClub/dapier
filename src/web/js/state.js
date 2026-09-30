/* Shared console state: one fetch of /api/admin/overview (plus the small
   failed-runs summary that trails it), one active view.
   workflowSearchIds is the server-side ?q= match set for the workflow list
   (null while the search box is empty or the server call failed — then the
   loaded payload is filtered client-side). */
export const state = {
  data: null, errors: null, workflowSearchIds: null, view: 'overview',
};
