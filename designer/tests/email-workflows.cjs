/* Semantic regression checks for editing workflow-owned email routes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { test, after } = require('node:test');
const ts = require('typescript');

const root = path.resolve(__dirname, '..');
const output = fs.mkdtempSync(path.join(os.tmpdir(), 'dapier-email-workflows-'));
fs.symlinkSync(path.join(root, 'node_modules'), path.join(output, 'node_modules'), 'junction');
after(() => fs.rmSync(output, { recursive: true, force: true }));
for (const name of ['catalog', 'workflows', 'logos']) {
  const source = fs.readFileSync(path.join(root, 'src', name + (name === 'logos' ? '.tsx' : '.ts')), 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022, jsx: ts.JsxEmit.ReactJSX }
  }).outputText;
  fs.writeFileSync(path.join(output, name + '.mjs'), compiled
    .replaceAll('"./catalog"', '"./catalog.mjs"')
    .replaceAll('"./logos"', '"./logos.mjs"'));
}
const converters = import(pathToFileURL(path.join(output, 'workflows.mjs')).href);

function emailWorkflow() {
  return {
    id: 'email-intake', enabled: true, allow_email_overlap: true,
    description: 'Forward incoming documents', tags: ['ops'], notify: [],
    trigger: { connector: 'email', event: 'message.received', filters: {
      route: { in: ['invoice', 'receipts'] },
      subject: { prefix: 'Invoice', contains: '2026' },
      html: { exists: false }
    } },
    actions: [{ id: '0', type: 'webhook', url: 'https://example.test' }]
  };
}

test('Canvas preserves typed address filters, compound rules, settings and execution identity', async () => {
  const { workflowFromShapes, shapesFromWorkflow } = await converters;
  const original = emailWorkflow();
  const result = workflowFromShapes(shapesFromWorkflow(original), original.id, original.enabled, original);
  assert.deepEqual(result.problems, []);
  assert.deepEqual(result.workflow, original);
});

test('Removing an entry point does not restore it from the original definition', async () => {
  const { workflowFromShapes, shapesFromWorkflow } = await converters;
  const original = emailWorkflow();
  original.triggers = [original.trigger, { connector: 'telegram', event: 'message.received', filters: {} }];
  delete original.trigger;
  const shapes = shapesFromWorkflow(original).filter(shape => shape.data?.connector !== 'telegram');
  const result = workflowFromShapes(shapes, original.id, true, original);
  assert.equal(result.workflow.triggers, undefined);
  assert.equal(result.workflow.trigger.connector, 'email');
});

test('Invalid list filters cannot silently become nonmatching strings', async () => {
  const { workflowFromShapes, shapesFromWorkflow } = await converters;
  const original = emailWorkflow();
  const shapes = shapesFromWorkflow(original);
  const trigger = shapes.find(shape => shape.data?.nodeKind === 'trigger');
  trigger.data.filters.find(rule => rule.operator === 'in').value = 'invoice, receipts';
  const result = workflowFromShapes(shapes, original.id, true, original);
  assert.ok(result.problems.some(problem => problem.includes('JSON list')));
});
