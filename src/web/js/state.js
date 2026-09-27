/* Shared console state: one fetch of /api/admin/overview (plus the small
   failed-runs summary that trails it), one active view. */
export const state = { data: null, errors: null, view: 'overview' };
