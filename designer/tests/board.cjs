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
  const { effectiveActionId, shapesFromWorkflow, actionNodeSubtitle, shortConditionField } = await load('workflows');
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
  // Long template paths shorten to their last segment so the canvas card
  // reads "has_attachment = true", not "steps.triage.output.res...".
  assert.equal(shortConditionField('steps.triage.output.result.has_attachment'), 'has_attachment');
  assert.equal(shortConditionField('subject'), 'subject');
  assert.equal(shortConditionField('  '), '');
});

test('condition branches parse to preview rows; garbage reports an error', async () => {
  const { parseBranchSteps, actionNodeSubtitle } = await load('workflows');
  const thenText = '- id: email-date\n  type: date_time\n  value: "{date}"\n- id: archive-attachment\n  type: dropbox_upload\n  folder: /_dtc_paperwork/invoices\n';
  const parsed = parseBranchSteps(thenText);
  assert.equal(parsed.error, null);
  assert.equal(parsed.steps.length, 2);
  assert.equal(parsed.steps[0].id, 'email-date');
  assert.equal(parsed.steps[0].detail, '{date}');
  assert.equal(parsed.steps[1].id, 'archive-attachment');
  assert.ok(parsed.steps[1].detail.includes('invoices'));
  assert.deepEqual(parseBranchSteps('').steps, []);
  assert.ok(parseBranchSteps('::: not yaml :::{{').error);
  assert.ok(parseBranchSteps('just: a-mapping').error);
  // The invoice-intake `when:` mapping form shortens the same way.
  assert.equal(
    actionNodeSubtitle({ nodeKind: 'action', actionType: 'condition', raw: { when: { 'steps.triage.output.result.has_attachment': { equals: true } } } }),
    'has_attachment = true'
  );
});

test('2D branching layout materializes condition, decision, and paths columns with labeled edges and union join', async () => {
  const { shapesFromWorkflow, workflowFromShapes, effectiveActionId } = await load('workflows');
  const { openingViewport } = await load('geometry');
  const wf = {
    id: 'branch-flow',
    enabled: true,
    trigger: { connector: 'email', event: 'message.received', filters: { route: 'support' } },
    actions: [
      {
        id: 'triage',
        type: 'decision',
        mode: 'switch',
        prompt: 'Classify support request',
        branches: {
          billing: {
            description: 'Invoice or payment question',
            actions: [
              { id: 'post_billing', type: 'slack', channel: 'C1', text: 'Billing' }
            ]
          },
          technical: [
            { id: 'post_tech', type: 'slack', channel: 'C2', text: 'Tech' }
          ]
        },
        default_actions: []
      },
      {
        id: 'after_join',
        type: 'webhook',
        url: 'https://example.com/done'
      }
    ]
  };
  const shapes = shapesFromWorkflow(wf);
  const childNodes = shapes.filter((s) => s.type === 'node' && s.data?.parentBranchNodeId === 'action-0' && !s.data?.isBranchPlaceholder);
  assert.equal(childNodes.length, 2);
  assert.ok(childNodes[0].x < childNodes[1].x, 'parallel branch columns have distinct horizontal X coordinates');
  assert.equal(effectiveActionId(shapes, childNodes[0].id), 'triage.billing.post_billing');
  assert.equal(effectiveActionId(shapes, childNodes[1].id), 'triage.technical.post_tech');

  // Empty default_actions branch produces a placeholder node
  const placeholders = shapes.filter((s) => s.type === 'node' && s.data?.isBranchPlaceholder);
  assert.equal(placeholders.length, 1);
  assert.equal(placeholders[0].data.branchLabel, 'else');

  // Labeled branch edges and synthetic union join node
  const branchEdges = shapes.filter((s) => s.type === 'arrow' && s.isBranchEdge);
  assert.equal(branchEdges.length, 3);
  assert.deepEqual(branchEdges.map((e) => e.branchLabel), ['billing', 'technical', 'else']);
  const unionJoin = shapes.find((s) => s.type === 'node' && s.data?.isUnionJoin);
  assert.ok(unionJoin, 'synthetic union join node is created before downstream step');

  // Opening viewport scales down on a narrow 400px viewport so parallel branches fit
  const narrowView = openingViewport(shapes, { width: 400, height: 600 }, 48);
  assert.ok(narrowView.zoom < 1 && narrowView.zoom >= 0.55, 'narrow viewport scales zoom to fit branches');

  // Round-trip editing a child node updates nested YAML in workflowFromShapes
  const editedShapes = shapes.map((s) => (
    s.id === childNodes[0].id
      ? { ...s, data: { ...s.data, fields: { ...s.data.fields, text: 'Updated Billing Text' } } }
      : s
  ));
  const roundTripped = workflowFromShapes(editedShapes, 'branch-flow', true, wf);
  assert.equal(roundTripped.problems.length, 0);
  const savedDecision = roundTripped.workflow.actions[0];
  assert.equal(savedDecision.branches.billing.description, 'Invoice or payment question');
  assert.equal(savedDecision.branches.billing.actions[0].text, 'Updated Billing Text');
});

test('multi-rule when predicates and explicit union step round-trip cleanly', async () => {
  const { shapesFromWorkflow, workflowFromShapes, extractPredicateRules, actionNodeSubtitle } = await load('workflows');
  const wf = {
    id: 'paths-union-flow',
    enabled: true,
    trigger: { connector: 'email', event: 'message.received', filters: {} },
    actions: [
      {
        id: 'multi_filter',
        type: 'filter',
        when: [
          { field: 'subject', operator: 'contains', value: 'Invoice' },
          { field: 'from', operator: 'contains', value: 'billing@' }
        ],
        reason: 'Only billing invoices'
      },
      {
        id: 'route_paths',
        type: 'paths',
        paths: [
          {
            id: 'eu',
            label: 'EU',
            when: { currency: { equals: 'EUR' } },
            actions: [{ id: 'eu_step', type: 'webhook', url: 'https://example.com/eu' }]
          }
        ],
        default: []
      },
      {
        id: 'join_results',
        type: 'union',
        from: 'route_paths',
        fields: { target_url: '{steps.route_paths.output.result.url}' }
      }
    ]
  };
  const shapes = shapesFromWorkflow(wf);
  const topActions = shapes.filter((s) => s.type === 'node' && s.data?.nodeKind === 'action' && !s.data?.parentBranchNodeId && !s.data?.isUnionJoin);
  assert.equal(topActions.length, 3);
  const rules = extractPredicateRules(topActions[0].data.fields, topActions[0].data.raw);
  assert.equal(rules.length, 2);
  assert.ok(actionNodeSubtitle(topActions[0].data).includes('(+1)'), 'multi-rule filter subtitle shows (+1) badge');
  assert.ok(actionNodeSubtitle(topActions[2].data).includes('route_paths'), 'union subtitle shows source branch step');

  // Explicit union step immediately following paths suppresses synthetic union join bar
  const syntheticUnions = shapes.filter((s) => s.type === 'node' && s.data?.isUnionJoin);
  assert.equal(syntheticUnions.length, 0);

  const built = workflowFromShapes(shapes, 'paths-union-flow', true, wf);
  assert.equal(built.problems.length, 0);
  assert.equal(built.workflow.actions[2].type, 'union');
  assert.equal(built.workflow.actions[2].from, 'route_paths');
  assert.deepEqual(built.workflow.actions[2].fields, { target_url: '{steps.route_paths.output.result.url}' });
});

