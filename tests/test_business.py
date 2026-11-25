import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from demo_api.models import APIError, ActionRequest
from demo_api.providers.business import BusinessFlags
from demo_api.providers.composite import Composite
from demo_api.experiments import Experiments
from scripts.business_flags_setup import prepare


def resources():
    flags = {name: {'state': 'ENABLED', 'defaultVariant': 'off', 'variants': variants}
             for name, variants in {
                 'paymentFailure': {'off': 0, '25%': 0.25, '50%': 0.5, '100%': 1, '90%': 0.95},
                 'cartFailure': {'off': 0, '25%': 0.25},
                 'paymentUnreachable': {'off': False, 'on': True},
             }.items()}
    cm = {'metadata': {'name': 'flagd-config', 'uid': 'cm-1', 'resourceVersion': '1'},
          'data': {'demo.flagd.json': json.dumps({'flags': flags})}}
    deployment = {'spec': {'replicas': 1, 'template': {'spec': {
        'containers': [{'name': 'flagd', 'command': ['/flagd-build', 'start', '--uri', 'file:./etc/flagd/demo.flagd.json'],
                        'ports': [{'name': 'ofrep', 'containerPort': 8016}],
                        'volumeMounts': [{'name': 'config-rw', 'mountPath': '/etc/flagd'}]},
                       {'name': 'flagd-ui', 'volumeMounts': [{'name': 'config-rw', 'mountPath': '/app/data'}]}],
        'initContainers': [{'name': 'init-config'}],
        'volumes': [{'name': 'config-rw', 'emptyDir': {}}]}}}}
    service = {'spec': {'ports': [{'name': 'ofrep', 'port': 8016, 'targetPort': 'ofrep'}]}}
    return cm, deployment, service


class Kube:
    namespace = 'demo'
    def __init__(self):
        self.cm, deploy, svc = resources()
        self.deployment, self.service = prepare(deploy, svc, self.cm)
        self.writes = 0
        self.drop_response = False
        self.race = False

    def run(self, *args, document=None, **kwargs):
        if args[:2] == ('get', 'configmap'):
            return copy.deepcopy(self.cm)
        if args[:2] == ('get', 'deployment'):
            return copy.deepcopy(self.deployment)
        assert args[0] == 'replace'
        if self.race:
            self.cm['metadata']['resourceVersion'] = 'external'
        if document['metadata']['resourceVersion'] != self.cm['metadata']['resourceVersion']:
            raise APIError('backend_unavailable', 'resourceVersion conflict')
        self.cm = copy.deepcopy(document)
        self.cm['metadata']['resourceVersion'] = str(self.writes + 2)
        self.writes += 1
        if self.drop_response:
            self.drop_response = False
            raise APIError('backend_unavailable', 'Lost response AFTER successful replace')
        return copy.deepcopy(self.cm)

    def flags(self):
        return json.loads(self.cm['data']['demo.flagd.json'])['flags']

    def edit(self, flag, **fields):
        doc = json.loads(self.cm['data']['demo.flagd.json'])
        doc['flags'][flag].update(fields)
        self.cm['data']['demo.flagd.json'] = json.dumps(doc)


@pytest.fixture
def backend(monkeypatch):
    from demo_api.providers import business
    kube = Kube()
    discovery = SimpleNamespace(kube=kube, resolve=lambda service: [{'metadata': {'name': service, 'uid': 'uid'}}])
    provider = BusinessFlags({'evaluation': {'url': 'http://flagd'}}, {'discovery': discovery, 'target_id': 'test'})
    runtime = copy.deepcopy(kube.flags())
    def evaluate(method, url, **kwargs):
        flag = runtime[url.rsplit('/', 1)[-1]]
        variant = flag['defaultVariant']
        return {'variant': variant, 'value': flag['variants'][variant]}
    monkeypatch.setattr(business, 'request_json', evaluate)
    return provider, kube, runtime, discovery


def request(action='business.payment.failure', service='payment', **parameters):
    return ActionRequest(action=action, service=service, parameters=parameters)


def test_preview_is_read_only_and_checks_deployment_service_and_actual_variants(backend):
    p, kube, _, _ = backend
    plan = p.plan(request(percent=25))
    assert plan['original']['defaultVariant'] == 'off'
    assert plan['desired']['defaultVariant'] == '25%'
    assert kube.writes == 0
    with pytest.raises(APIError, match='only applies'):
        p.plan(request(service='cart'))
    kube.deployment['spec']['template']['spec']['volumes'][0] = {'name': 'config-rw', 'emptyDir': {}}
    with pytest.raises(APIError) as exc:
        p.plan(request())
    assert exc.value.code == 'business_backend_not_ready'


def test_injection_waits_for_evaluation_and_restores_original_not_off(backend):
    p, kube, runtime, _ = backend
    kube.edit('paymentFailure', defaultVariant='50%')
    plan = p.plan(request(percent=25))
    handle = p.apply('da-1', plan)
    assert p.status('da-1', handle)['state'] == 'pending'
    runtime.update(copy.deepcopy(kube.flags()))
    assert p.status('da-1', handle)['state'] == 'running'
    assert not p.status('da-1', handle)['backend_status']['business_effect_verified']
    assert p.cancel('da-1', handle)['state'] == 'cancelling'
    assert kube.flags()['paymentFailure']['defaultVariant'] == '50%'
    runtime.update(copy.deepcopy(kube.flags()))
    assert p.cancel('da-1', handle)['state'] == 'cancelled'
    assert kube.writes == 2


def test_external_edit_is_preserved_and_unrelated_flags_survive_restore(backend):
    p, kube, runtime, _ = backend
    h = p.apply('da-1', p.plan(request()))
    kube.edit('cartFailure', defaultVariant='25%')
    p.cancel('da-1', h)
    assert kube.flags()['cartFailure']['defaultVariant'] == '25%'
    # Another experiment; an external edit to the owned field blocks restore.
    h = p.apply('da-2', p.plan(request()))
    kube.edit('paymentFailure', description='human edit')
    with pytest.raises(APIError) as exc:
        p.cancel('da-2', h)
    assert exc.value.code == 'recovery_conflict'
    assert kube.flags()['paymentFailure']['description'] == 'human edit'


def test_uid_and_concurrent_update_are_not_overwritten(backend):
    p, kube, _, _ = backend
    plan = p.plan(request())
    kube.cm['metadata']['uid'] = 'replacement'
    with pytest.raises(APIError) as exc:
        p.apply('da-1', plan)
    assert exc.value.code == 'target_changed'
    kube.cm['metadata']['uid'] = 'cm-1'
    kube.race = True
    with pytest.raises(APIError, match='resourceVersion'):
        p.apply('da-1', plan)
    assert kube.writes == 0


def test_targeting_ownership_and_missing_variant_rejected(backend):
    p, kube, _, _ = backend
    kube.edit('paymentFailure', targeting={'if': [True, 'off', 'off']})
    with pytest.raises(APIError, match='targeting'):
        p.plan(request())
    kube.edit('paymentFailure', targeting={})
    with pytest.raises(APIError, match='actual variant'):
        p.plan(request(percent=75))
    plan = p.plan(request())
    kube.cm['metadata']['annotations'] = {p._owner_key('paymentFailure'): 'other:da-other'}
    with pytest.raises(APIError, match='owned'):
        p.apply('da-1', plan)
    with pytest.raises(APIError) as exc:
        p.cancel('da-1', {'plan': plan})
    assert exc.value.code == 'recovery_conflict'


def test_boolean_fault_and_category(backend):
    p, kube, _, _ = backend
    h = p.apply('da-1', p.plan(request('business.payment.unavailable')))
    assert kube.flags()['paymentUnreachable']['defaultVariant'] == 'on'
    assert p.describe()[0]['category'] == 'business'
    p.cancel('da-1', h)
    assert kube.flags()['paymentUnreachable']['defaultVariant'] == 'off'


def composite(backend, monkeypatch):
    p, _, _, discovery = backend
    import demo_api.plugins as plugins
    infra = SimpleNamespace(describe=lambda: [{"name": "pod.kill"}],
                            status=lambda *a: {"state": "running"},
                            cancel=lambda *a: {"state": "cancelled"})
    monkeypatch.setattr(plugins, "load_provider", lambda name, config, context: p if name == 'otel-demo-flags' else infra)
    config = {"legacy_provider": "infra", "providers": {
        "infra": {"plugin": "chaosblade"}, "business": {"plugin": "otel-demo-flags"}}}
    return Composite(config, {"target_id": "test", "discovery": discovery})


def test_lost_write_response_survives_restart_and_recovers_from_persisted_intent(backend, monkeypatch, tmp_path):
    actuator = composite(backend, monkeypatch)
    _, kube, runtime, _ = backend
    path = str(tmp_path / 'experiments.db')
    targets = {'test': SimpleNamespace(actuator=actuator)}
    store = Experiments(path, targets)
    kube.drop_response = True
    req = request().model_copy(update={'dry_run': False})
    with pytest.raises(APIError) as exc:
        store.create('test', req, 'key')
    assert exc.value.code == 'action_outcome_unknown'
    doc = store.list('test')[0]
    assert doc['state'] == 'unknown'
    assert doc['handle']['plan']['backend'] == 'business'
    assert store.create('test', req, 'key')['id'] == doc['id']
    assert kube.writes == 1
    store.db.close()
    runtime.update(copy.deepcopy(kube.flags()))
    store = Experiments(path, targets)
    assert store.refresh('test', doc['id'])['state'] == 'running'
    assert store.cancel('test', doc['id'])['state'] == 'cancelling'
    runtime.update(copy.deepcopy(kube.flags()))
    assert store.refresh('test', doc['id'])['state'] == 'cancelled'
    store.db.close()


def test_composite_preserves_legacy_and_rejects_changed_backend(backend, monkeypatch):
    c = composite(backend, monkeypatch)
    assert c.cancel('da-old', {'name': 'da-old'})['state'] == 'cancelled'
    h = {'name': 'da-new', 'plan': c.plan(request())}
    c.fingerprints['business'] = 'changed'
    with pytest.raises(APIError) as exc:
        c.cancel('da-new', h)
    assert exc.value.code == 'backend_changed'
    import demo_api.plugins as plugins
    monkeypatch.setattr(plugins, 'load_provider', lambda *a: SimpleNamespace(describe=lambda: [{'name': 'same'}]))
    with pytest.raises(ValueError, match='Duplicate'):
        Composite({'providers': {'a': {'plugin': 'a'}, 'b': {'plugin': 'b'}}}, {'target_id': 'test', 'discovery': backend[3]})


def test_http_business_lifecycle_idempotency_and_target_isolation(backend, monkeypatch, tmp_path):
    import yaml
    import demo_api.app as app_module
    c = composite(backend, monkeypatch)
    p, kube, runtime, discovery = backend
    monkeypatch.setattr(app_module, 'load_provider', lambda name, config, context: discovery if name == 'fake-discovery' else c)
    config = {'spec_version': 'demo-api/v1', 'state_path': str(tmp_path / 'state.db'), 'targets': [
        {'id': 'test', 'description': 'test', 'discovery': {'plugin': 'fake-discovery'},
         'telemetry': {}, 'actions': {'plugin': 'fake-actions'}}]}
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config))
    prefix = '/v1/targets/test/experiments'
    with TestClient(app_module.create_app(str(path))) as client:
        assert any(a['category'] == 'business' for a in client.get('/v1/targets/test/capabilities').json()['actions'] if 'category' in a)
        body = {'action': 'business.payment.failure', 'service': 'payment', 'parameters': {'percent': 25}, 'duration_seconds': 300}
        assert client.post(prefix, json=body).json()['dry_run']
        assert kube.writes == 0
        body['dry_run'] = False
        assert client.post(prefix, json=body).status_code == 422
        first = client.post(prefix, json=body, headers={'Idempotency-Key': 'business-1'}).json()
        assert client.post(prefix, json=body, headers={'Idempotency-Key': 'business-1'}).json()['id'] == first['id']
        assert kube.writes == 1
        runtime.update(copy.deepcopy(kube.flags()))
        url = prefix + '/' + first['id']
        assert client.get(url).json()['state'] == 'running'
        assert client.get(url.replace('/test/', '/other/')).status_code == 404
        assert client.delete(url).json()['state'] == 'cancelling'
        runtime.update(copy.deepcopy(kube.flags()))
        assert client.get(url).json()['state'] == 'cancelled'


def test_setup_preserves_inputs_and_does_not_duplicate_named_service_port():
    cm, deployment, svc = resources()
    before = copy.deepcopy(deployment)
    new, service = prepare(deployment, svc, cm)
    assert deployment == before
    assert new['spec']['template']['spec']['initContainers'] == []
    assert all(m['readOnly'] for c in new['spec']['template']['spec']['containers'] for m in c['volumeMounts'])
    assert len(service['spec']['ports']) == 1
    new2, service2 = prepare(new, service, cm)
    assert (new2, service2) == (new, service)


def test_expiry_reconciles_business_restore_and_evaluation_outage_stays_unconfirmed(backend, tmp_path, monkeypatch):
    p, kube, runtime, _ = backend
    store = Experiments(str(tmp_path / 'state.db'), {'test': SimpleNamespace(actuator=p)})
    doc = store.create('test', request().model_copy(update={'dry_run': False}), 'key')
    runtime.update(copy.deepcopy(kube.flags()))
    doc['expires_at'] = 0
    store.save(doc)
    from demo_api.providers import business
    original_evaluate = business.request_json
    def unavailable(*a, **kw):
        raise APIError('backend_unavailable', 'flagd is down')
    monkeypatch.setattr(business, 'request_json', unavailable)
    store.reconcile()
    result = store.get('test', doc['id'])
    assert result['state'] not in {'cancelled', 'completed'}
    assert 'flagd is down' in result['last_error']
    assert kube.flags()['paymentFailure']['defaultVariant'] == 'off'
    monkeypatch.setattr(business, 'request_json', original_evaluate)
    runtime.update(copy.deepcopy(kube.flags()))
    store.reconcile()
    assert store.get('test', doc['id'])['state'] == 'cancelled'
    store.db.close()


@pytest.mark.parametrize('parameters', [{'percent': '25'}, {'percent': 25.0}, {'percent': 90},
                                        {'percent': 25, 'namespace': 'other'}])
def test_business_rejects_untyped_or_arbitrary_parameters(backend, parameters):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        backend[0].plan(request().model_copy(update={'parameters': parameters}))
