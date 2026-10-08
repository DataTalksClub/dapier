/* Canvas navigation and inspector helpers: wheel zoom/pan, the opening
   viewport, effective step ids, and node summaries. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { test, after } = require('node:test');
const ts = require('typescript');

const root = path.resolve(__dirname, '..');
const output = fs.mkdtempSync(path.join(os.tmpdir(), 'dapier-board-'));
fs.symlinkSync(path.join(root, 'node_modules'), path.join(output, 'node_modules'), 'junction');
after(() => fs.rmSync(output, { recursive: true, force: true }));
const sources = { catalog: 'catalog.ts', workflows: 'workflows.ts', logos: 'logos.tsx', icons: 'icons.tsx', geometry: 'board/geometry.ts' };
for (const [name, file] of Object.entries(sources)) {
  const source = fs.readFileSync(path.join(root, 'src', file), 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022, jsx: ts.JsxEmit.ReactJSX }
  }).outputText;
  fs.writeFileSync(path.join(output, name + '.mjs'), compiled
    .replaceAll('"./catalog"', '"./catalog.mjs"')
    .replaceAll('"./logos"', '"./logos.mjs"')
    .replaceAll('"./icons"', '"./icons.mjs"'));
}
const load = (name) => import(pathToFileURL(path.join(output, name + '.mjs')).href);

const world = { width: 800, height: 600 };
const size = { width: 800, height: 600 };
const wheel = (over) => ({ deltaX: 0, deltaY: 0, deltaMode: 0, ctrlKey: false, metaKey: false, shiftKey: false, ...over });
/** World point under a screen offset from the canvas center. */
const worldAt = (view, offset) => ({
  x: world.width / 2 + view.pan.x + offset.x * (world.width / view.zoom / size.width),
  y: world.height / 2 + view.pan.y + offset.y * (world.height / view.zoom / size.height)
});

test('a mouse wheel notch zooms around the pointer and clamps to the zoom range', async () => {
  const { wheelViewport, wheelGesture, minZoom, maxZoom } = await load('geometry');
  const start = { zoom: 1, pan: { x: 0, y: 0 } };
  const offset = { x: 200, y: -100 };
  assert.equal(wheelGesture(wheel({ deltaY: -100 })), 'zoom');
  const zoomedIn = wheelViewport(start, wheel({ deltaY: -100 }), offset, size, world);
  assert.ok(zoomedIn.zoom > 1);
  const before = worldAt(start, offset);
  const afterPoint = worldAt(zoomedIn, offset);
  assert.ok(Math.abs(before.x - afterPoint.x) < 1e-9 && Math.abs(before.y - afterPoint.y) < 1e-9,
    'the point under the cursor stays put');
  let view = start;
  for (let i = 0; i < 50; i += 1) view = wheelViewport(view, wheel({ deltaY: -100 }), offset, size, world);
  assert.equal(view.zoom, maxZoom);
  for (let i = 0; i < 80; i += 1) view = wheelViewport(view, wheel({ deltaY: 100 }), offset, size, world);
  assert.equal(view.zoom, minZoom);
  // Line-mode wheels (Firefox) zoom too.
  assert.equal(wheelGesture(wheel({ deltaY: 3, deltaMode: 1 })), 'zoom');
});

test('pinch (ctrl+wheel) zooms; trackpad scroll and shift+wheel pan', async () => {
  const { wheelViewport, wheelGesture } = await load('geometry');
  const start = { zoom: 1, pan: { x: 10, y: 20 } };
  assert.equal(wheelGesture(wheel({ deltaY: 4.5, ctrlKey: true })), 'zoom');
  const pinched = wheelViewport(start, wheel({ deltaY: -4.5, ctrlKey: true }), { x: 0, y: 0 }, size, world);
  assert.ok(pinched.zoom > 1);
  assert.deepEqual(pinched.pan, start.pan, 'zooming at the center keeps the center');

  assert.equal(wheelGesture(wheel({ deltaX: 12, deltaY: 3 })), 'pan');
  assert.equal(wheelGesture(wheel({ deltaY: 7.25 })), 'pan');
  const panned = wheelViewport(start, wheel({ deltaX: 12, deltaY: 30 }), { x: 0, y: 0 }, size, world);
  assert.equal(panned.zoom, 1);
  assert.deepEqual(panned.pan, { x: 22, y: 50 });

  const sideways = wheelViewport({ zoom: 2, pan: { x: 0, y: 0 } }, wheel({ deltaY: 100, shiftKey: true }), { x: 0, y: 0 }, size, world);
  assert.equal(sideways.zoom, 2);
  assert.deepEqual(sideways.pan, { x: 50, y: 0 }, 'shift+wheel scrolls sideways, scaled to the zoom');
});

test('a workflow opens at 100% with the trigger at the top center', async () => {
  const { openingViewport } = await load('geometry');
  const shapes = [
    { id: 'a', type: 'node', x: 100, y: 40, width: 220, height: 92, data: { nodeKind: 'action' } },
    { id: 't', type: 'node', x: 900, y: 200, width: 220, height: 92, data: { nodeKind: 'trigger' } }
  ];
  const view = openingViewport(shapes, world, 48);
  assert.equal(view.zoom, 1);
  // Visible box at zoom 1 starts at pan; the trigger's center sits mid-width.
  assert.equal(view.pan.x + world.width / 2, 1010);
  assert.equal(view.pan.y, 200 - 48);
  assert.deepEqual(openingViewport([], world), { zoom: 1, pan: { x: 0, y: 0 } });
});

test('step ids fall back to action-<n> in run order; conditions summarize their test', async () => {
  const { effectiveActionId, shapesFromWorkflow, actionNodeSubtitle } = await load('workflows');
  const shapes = shapesFromWorkflow({
    id: 'w', enabled: true,
    trigger: { connector: 'telegram', event: 'message.received', filters: {} },
    actions: [
      { id: 'named', type: 'condition', when: { channel: { equals: 'course-ml' } }, then: [] },
      { id: '', type: 'condition', field: 'route', operator: 'not_equals', value: 'x', then: [] }
    ]
  });
  const actions = shapes.filter((shape) => shape.type === 'node' && shape.data?.nodeKind === 'action');
  assert.equal(effectiveActionId(shapes, actions[0].id), 'named');
  assert.equal(effectiveActionId(shapes, actions[1].id), 'action-2');
  assert.equal(effectiveActionId(shapes, 'nope'), null);
  assert.equal(actionNodeSubtitle(actions[0].data), 'channel = course-ml');
  assert.equal(actionNodeSubtitle(actions[1].data), 'route ≠ x');
});
