import json

import httpx
import pytest

from scripts.business_validate import run_case


@pytest.mark.parametrize('fault_fails', [True, False])
def test_real_validator_restores_even_when_business_effect_assertion_fails(fault_fails):
    state = {'active': False, 'deleted': False}
    def backend(request):
        if request.method == 'POST':
            body = json.loads(request.content)
            if body['dry_run']:
                return httpx.Response(200, json={'dry_run': True, 'plan': {}})
            state['active'] = True
            return httpx.Response(200, json={'id': 'da-test'})
        if request.method == 'DELETE':
            state.update(active=False, deleted=True)
            return httpx.Response(200, json={'state': 'cancelled'})
        return httpx.Response(200, json={'state': 'running' if state['active'] else 'cancelled'})
    def probe():
        return {'exit_code': int(state['active'] and fault_fails)}
    saved = []
    with httpx.Client(base_url='http://api', transport=httpx.MockTransport(backend)) as client:
        if fault_fails:
            run_case(client, '/experiments', 'business.payment.failure', probe,
                     lambda: {'exit_code': 0}, 2, lambda record: saved.append(dict(record)))
            assert saved[-1]['passed']
            assert len(saved[-1]['recovery']) == 2
        else:
            with pytest.raises(RuntimeError, match='Expected all business'):
                run_case(client, '/experiments', 'business.payment.failure', probe,
                         lambda: {'exit_code': 0}, 2, lambda record: saved.append(dict(record)))
            assert saved[-1]['cleanup']['state'] == 'cancelled'
    assert state['deleted'] and not state['active']


def test_validator_rejects_probe_and_api_namespace_mismatch_before_injecting():
    executed = []
    def backend(request):
        body = json.loads(request.content)
        if not body['dry_run']:
            executed.append(body)
        return httpx.Response(200, json={'dry_run': True, 'plan': {'backend': 'business', 'plan': {'namespace': 'other'}}})
    with httpx.Client(base_url='http://api', transport=httpx.MockTransport(backend)) as client:
        with pytest.raises(RuntimeError, match='namespace differ'):
            run_case(client, '/experiments', 'business.payment.failure', lambda: {'exit_code': 0},
                     lambda: {'exit_code': 0}, 1, lambda record: None, expected_namespace='default')
    assert executed == []
