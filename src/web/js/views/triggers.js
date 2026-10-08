/* The Workflows family's trigger tabs: Hooks (hooks.js) and Polls
   (polls.js) each own their tab, their dialogs and their API calls. This
   module keeps the one entry point the overview refresh calls. Email
   triggers live in the Emails tab, schedules in Schedules. */
import { renderHooks } from './hooks.js';
import { renderPolls } from './polls.js';

/* Called from the overview render after each refresh: each tab fetches
   while it is on screen, otherwise it repaints its cache. */
export function renderTriggers() {
  renderHooks();
  renderPolls();
}
