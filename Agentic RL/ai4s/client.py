"""Agent credential only. No operator capabilities, keys or arbitrary URLs."""
import json
import os
import urllib.request
import urllib.error


def call(path, data=None):
    token = os.environ.get('AI4S_AGENT_TOKEN')
    if not token:
        raise RuntimeError('Set AI4S_AGENT_TOKEN in the MCP host environment')
    allowed = {'status','propose','execute','stop','estop'}
    if path not in allowed:
        raise ValueError('Tool not exposed to agent')
    request = urllib.request.Request('http://127.0.0.1:8080/api/'+path,
        data=None if data is None else json.dumps(data,allow_nan=False).encode(),
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        message = json.load(exc).get('error','Gateway denied request')
        raise RuntimeError(message) from None


def get_status():
    return call('status')


def propose_scan(low=20.0, high=70.0, step=.1, dwell=.02):
    return call('propose',{'params':dict(low=low,high=high,step=step,dwell=dwell)})


def execute_approved_plan(plan_id):
    return call('execute',{'plan_id':plan_id})


def stop_experiment():
    return call('stop',{})


def request_emergency_stop():
    return call('estop',{})
