import json
import pytest
from src.dapier.connectors import registry
from src.dapier.engine.logic_pkg import run_chain
from plugins.cmp import read_course

TOKEN = "private-legacy-token"
COURSE = {
    "slug": "ml-zoomcamp-2026", "title": "ML Zoomcamp", "start_date": "2026-09-14", "end_date": "2027-01-25",
    "homeworks": [{"slug": "hw03", "due_date": "2026-10-12T23:00:00+00:00", "state": "OP"}],
    "projects": [{"slug": "midterm", "submission_due_date": "2026-11-16T23:00:00+00:00", "peer_review_due_date": "2026-11-23T23:00:00+00:00", "state": "CS"}],
    "private_users": ["must not return"],
}
ACTION = {"id": "course", "type": "cmp_read_course", "connection_id": "cmp-production", "course_slug": "ml-zoomcamp-2026"}

@pytest.fixture(autouse=True)
def connection(monkeypatch):
    monkeypatch.setattr(read_course.base, '_connected_connection', lambda _: {
        'provider': 'cmp', 'credential_id': 'oauth#cmp-production', 'verified_account_id': 'courses.datatalks.club'})
    monkeypatch.setattr(read_course.credentials, 'get_credential', lambda _: {'token': TOKEN})

def transport(payload=COURSE, status=200):
    def send(method, url, **kwargs):
        assert method == 'GET' and url == 'https://courses.datatalks.club/api/courses/ml-zoomcamp-2026/'
        assert kwargs['headers'] == {'authorization': 'Token ' + TOKEN}
        assert kwargs['body'] is None
        return status, json.dumps(payload).encode()
    return send

def test_fixed_authenticated_read_and_allowlisted_results():
    result = read_course.run_read_course(ACTION, {}, transport=transport())
    assert result['homeworks']['hw03']['state'] == 'OP'
    assert result['projects']['midterm']['state'] == 'CS'
    assert 'private_users' not in json.dumps(result) and TOKEN not in json.dumps(result)
    assert registry.ACTIONS['cmp_read_course'].required == frozenset({'connection_id', 'course_slug'})

@pytest.mark.parametrize('payload,status', [(COURSE,401),(COURSE,302),({'slug':'wrong'},200),([],200),({**COURSE,'projects':None},200),({**COURSE,'projects':COURSE['projects']*2},200)])
def test_read_failures_are_redacted(payload,status):
    with pytest.raises(ValueError) as error:
        read_course.run_read_course(ACTION, {}, transport=transport(payload,status))
    assert TOKEN not in str(error.value)

def test_wrong_provider_binding_and_slug_refused(monkeypatch):
    for connection in [{'provider':'telegram'}, {'provider':'cmp','verified_account_id':'other'}]:
        monkeypatch.setattr(read_course.base,'_connected_connection',lambda _: {**connection, 'connection_id':'cmp-production'})
        with pytest.raises(ValueError):read_course.run_read_course(ACTION, {}, transport=transport())
    with pytest.raises(ValueError):read_course.run_read_course({**ACTION,'course_slug':'../../elsewhere'}, {}, transport=transport())

def test_network_error_never_exposes_token():
    def bad(*a,**kw):raise RuntimeError('secret='+TOKEN)
    with pytest.raises(ValueError) as error:read_course.run_read_course(ACTION,{},transport=bad)
    assert TOKEN not in str(error.value)

@pytest.mark.parametrize('state,deadline,send', [('CS','2026-11-16T23:00:00+00:00',True),('CL','2026-11-16T23:00:00+00:00',False),('CS','changed',False),('PR','2026-11-16T23:00:00+00:00',False)])
def test_workflow_stops_closed_or_changed_submission_before_send(state,deadline,send):
    payload={**COURSE,'projects':[{**COURSE['projects'][0],'state':state,'submission_due_date':deadline}]}
    steps=[ACTION,{'id':'guard','type':'filter','when':{
        'steps.course.output.course.start_date':{'equals':'2026-09-14'},
        'steps.course.output.projects.midterm.submission_due_date':{'equals':'2026-11-16T23:00:00+00:00'},
        'steps.course.output.projects.midterm.state':{'equals':'CS'}}},
        {'id':'send','type':'telegram_send'}]
    sent=[]
    def runner(action,event,workflow_id,steps=None):
        if action['type']=='cmp_read_course':return read_course.run_read_course(action,event,transport=transport(payload))
        sent.append(action);return {'message_id':1}
    stop=run_chain('guarded',steps,{'data':{}},runner)
    assert bool(sent)==send and stop==(None if send else 'filtered')

def test_workflow_read_error_halts_without_sending():
    sent=[]
    def runner(action,event,workflow_id,steps=None):
        if action['type']=='cmp_read_course':return read_course.run_read_course(action,event,transport=transport(COURSE,401))
        sent.append(action)
    with pytest.raises(ValueError):run_chain('guarded',[ACTION,{'id':'send','type':'telegram_send'}],{'data':{}},runner)
    assert not sent
