"""Current failures require action; recovered runs and unused setup do not."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.dapier.api.router import _static

ROOT = Path(__file__).resolve().parents[1]


def model(data):
    if not shutil.which('node'):
        pytest.skip('node is needed to evaluate the console model')
    source = (ROOT / 'src/web/js/home-model.js').read_text()
    script = source.replace('export function', 'function') + '\nconst m = homeModel(' + json.dumps(data) + '); console.log(JSON.stringify({...m, latest: [...m.latest]}));'
    return json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script], text=True))


def test_home_prioritizes_pauses_and_ignores_recovered_failures():
    result = model({'workflows': [
        {'id': 'recovered', 'enabled': True},
        {'id': 'broken', 'enabled': True},
        {'id': 'paused', 'enabled': True, 'auto_paused': True},
    ], 'runs': [
        {'workflow_id': 'recovered', 'status': 'failed', 'started_at': '2026-09-01'},
        {'workflow_id': 'recovered', 'status': 'completed', 'started_at': '2026-09-03'},
        {'workflow_id': 'broken', 'status': 'failed', 'started_at': '2026-09-02'},
    ]})
    assert [p['workflow']['id'] for p in result['problems']] == ['paused', 'broken']
    assert result['running'] == 2
    assert result['workflows'][0]['id'] == 'recovered'


def test_home_only_surfaces_connections_used_by_enabled_workflows():
    result = model({'workflows': [
        {'id': 'live', 'enabled': True, 'actions': [{'paths': [{'actions': [{'connection_id': 'used'}]}]}]},
        {'id': 'off', 'enabled': False, 'trigger': {'connection_id': 'unused'}},
    ], 'connections': [
        {'connection_id': 'used', 'status': 'connected', 'health': 'expired'},
        {'connection_id': 'unused', 'status': 'ready'},
        {'connection_id': 'spare', 'status': 'revoked'},
    ], 'quota': {'enabled': True, 'remaining': 0}})
    assert [c['connection_id'] for c in result['connections']] == ['used']
    assert result['quotaBlocked'] is True
    assert model({'quota': {'enabled': False, 'remaining': None}})['quotaBlocked'] is False


def test_usage_and_home_model_deep_links_are_served():
    assert _static('/usage')['statusCode'] == 200
    assert _static('/assets/js/home-model.js')['statusCode'] == 200
