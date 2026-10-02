"""Exercise progressive loading with deferred network responses."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_sections_render_before_slow_response_and_ignore_superseded_refresh():
    if not shutil.which('node'):
        pytest.skip('node is required')
    source = (ROOT / 'src/web/js/overview-loader.js').read_text()
    script = source + '''
import assert from 'node:assert/strict';
const pending = [], rendered = [], errors = [];
const load = createOverviewLoader(path => new Promise((resolve, reject) => {
  pending.push({path, resolve, reject});
}), (section, data) => rendered.push([section, data]), (section) => errors.push(section));
const first = load();
assert.equal(pending.length, 7); // All requests start without waiting for another.
pending[0].resolve({workflows: [{id: 'first'}]});
await new Promise(resolve => setImmediate(resolve));
assert.deepEqual(rendered, [['workflows', {workflows: [{id: 'first'}]}]]);
// One failed section leaves the workflows and other results usable.
pending[3].reject(new Error('connection store unavailable'));
pending[1].resolve({runs: []});
await new Promise(resolve => setImmediate(resolve));
assert.deepEqual(errors, ['connections']);
assert.equal(rendered.length, 2);
const second = load();
for (const request of pending.slice(7)) request.resolve({fresh: true});
assert.equal(await second, true);
const count = rendered.length;
for (const request of pending.slice(0, 7)) request.resolve({stale: true});
assert.equal(await first, false);
assert.equal(rendered.length, count);
assert.equal(errors.length, 1);
'''
    subprocess.run(['node', '--input-type=module', '-e', script], check=True)
