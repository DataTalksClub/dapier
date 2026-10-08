/* Operator words for triggers and actions, from the connector catalog the
   designer already reads (GET /api/admin/designer/catalog): "Telegram
   message received", "Code (Python)". Fetched once; until it arrives (or if
   it fails) the format.js fallbacks keep rows readable. */
import { api } from './api.js';
import { triggerEventLabel, sentenceCase } from './format.js';

let catalog = null;
let loading = null;

export function loadCatalog() {
  if (catalog || loading) return loading || Promise.resolve(catalog);
  loading = api('/api/admin/designer/catalog')
    .then((data) => {
      catalog = {
        connectors: new Map((data.connectors || []).map((item) => [item.name, item])),
        actions: new Map((data.actions || []).map((item) => [item.type, item])),
      };
      document.dispatchEvent(new CustomEvent('dapier:catalog-loaded'));
      return catalog;
    })
    .catch(() => null)
    .finally(() => { loading = null; });
  return loading;
}

/* "<Connector> <event>" unless the event label already names its source
   ("Email arrives"). */
export function eventLabel(connector, event) {
  const source = catalog && catalog.connectors.get(connector);
  const info = source && (source.event_info || []).find((item) => item.event === event);
  if (!source || !info) return triggerEventLabel(connector, event);
  const name = String(source.label || connector);
  const label = String(info.label || '');
  const source0 = name.toLowerCase().split(' ')[0];
  const label0 = label.toLowerCase().split(' ')[0];
  if (label.toLowerCase().includes(source0) || source0.startsWith(label0)) return label;
  return `${name} ${label.charAt(0).toLowerCase()}${label.slice(1)}`;
}

export function actionLabel(type) {
  const action = catalog && catalog.actions.get(type);
  return (action && action.label) || sentenceCase(String(type || 'Action').replace(/[._]+/g, ' '));
}
