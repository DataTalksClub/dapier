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


def test_home_reads_the_api_verdict_on_a_resolved_failure():
    """The API decides what still needs action (``resolved`` — an operator
    marked it, or a completed rerun settled it); Home just believes it. A
    workflow whose latest run is a resolved failure has nothing to act on."""
    result = model({'workflows': [
        {'id': 'marked', 'enabled': True},
        {'id': 'recovered', 'enabled': True},
        {'id': 'broken', 'enabled': True},
    ], 'runs': [
        # Latest run is a failure something settled — either verdict, same
        # answer here: not a problem.
        {'workflow_id': 'marked', 'status': 'failed', 'resolved': True,
         'resolved_reason': 'acknowledged', 'started_at': '2026-09-04'},
        {'workflow_id': 'recovered', 'status': 'failed', 'resolved': True,
         'resolved_reason': 'recovered', 'started_at': '2026-09-04'},
        {'workflow_id': 'broken', 'status': 'failed', 'started_at': '2026-09-04'},
    ]})
    assert [p['workflow']['id'] for p in result['problems']] == ['broken']


def test_home_still_lists_an_auto_paused_workflow_with_a_resolved_failure():
    """An auto-pause is the engine's own verdict and outranks resolution: the
    workflow is switched off, which needs a human whatever its last run said."""
    result = model({'workflows': [
        {'id': 'paused', 'enabled': True, 'auto_paused': True},
    ], 'runs': [
        {'workflow_id': 'paused', 'status': 'failed', 'resolved': True,
         'resolved_reason': 'recovered', 'started_at': '2026-09-04'},
    ]})
    assert [p['workflow']['id'] for p in result['problems']] == ['paused']


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


def test_home_has_one_workflow_list_and_one_results_feed():
    from collections import Counter
    from html.parser import HTMLParser

    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []

        def handle_starttag(self, tag, attrs):
            self.ids.extend(value for key, value in attrs if key == 'id')

    html = (ROOT / 'src/web/index.html').read_text()
    parser = Elements()
    parser.feed(html)
    assert not [key for key, count in Counter(parser.ids).items() if count > 1]
    home = html.split('data-page="overview">', 1)[1].split('<section class="view" data-page="workflows">', 1)[0]
    assert 'id="overview-runs-table"' not in home
    assert 'id="overview-errors"' in home
    assert 'id="nav-runs-count"' in html
    assert home.count('<section ') == 3
    assert home.count('</section>') == 4  # failed runs, workflows, results, and the home view
    assert 'data-page="usage"' not in html
    assert 'data-view="usage"' not in html
    runs = html.split('data-page="runs">', 1)[1]
    assert 'id="overview-usage"' in runs
    assert 'id="quota-form"' in runs
