from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from demo_api.models import APIError, ActionRequest, Query
from demo_api.providers.chaosblade import ChaosBlade
from demo_api.providers.kubernetes import KubernetesDiscovery
from demo_api.providers.telemetry import Jaeger, Prometheus, OpenSearch


def query(signal="traces", **kw):
    now = datetime.now(timezone.utc)
    return Query(signal=signal, service="checkout", start=now - timedelta(minutes=5), end=now, **kw)


def test_jaeger_normalization(monkeypatch):
    from demo_api.providers import telemetry
    monkeypatch.setattr(telemetry, "request_json", lambda *a, **kw: {"data": [{"traceID": "a" * 32,
        "processes": {"p1": {"serviceName": "checkout"}}, "spans": [{
            "traceID": "a" * 32, "spanID": "b" * 16, "processID": "p1", "operationName": "GET /",
            "startTime": 1700000000000000, "duration": 1500, "tags": [{"key": "error", "value": True}],
            "references": [{"refType": "CHILD_OF", "spanID": "c" * 16}]}]}]})
    discovery = SimpleNamespace(telemetry_name=lambda s: s)
    result = Jaeger({"url": "http://jaeger"}, {"discovery": discovery}).query(query())
    span = result.records[0]["spans"][0]
    assert span["duration_ms"] == 1.5 and span["status"] == "error"
    assert span["parent_span_id"] == "c" * 16 and span["timestamp"].endswith("Z")


def test_prometheus_escaping_units_and_nonfinite(monkeypatch):
    from demo_api.providers import telemetry
    captured = {}
    def backend(*a, **kw):
        captured.update(kw["params"])
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"job": "checkout"}, "values": [[1700000000, "NaN"], [1700000015, "1.2"]]}]}}
    monkeypatch.setattr(telemetry, "request_json", backend)
    provider = Prometheus({"url": "http://prometheus", "metrics": {"latency": {
        "unit": "s", "description": "Latency", "promql": 'metric{job=$service}'}}},
        {"discovery": SimpleNamespace(telemetry_name=lambda s: 'checkout"} or up{')})
    result = provider.query(query("metrics", metric="latency"))
    assert '\\"' in captured["query"]
    assert result.records[0]["unit"] == "s"
    assert result.records[0]["points"][0]["value"] is None
    assert result.warnings


def test_discovery_ids_survive_pod_name_reuse():
    discovery = KubernetesDiscovery({"context": "test", "namespace": "test", "service_label": "service"}, {"target_id": "test"})
    discovery.snapshot = lambda: [{"metadata": {"name": "pod", "uid": "new-uid", "labels": {"service": "checkout"}},
                                    "spec": {"containers": [{"name": "app"}]}}]
    entities = discovery.entities()
    assert any(e.id == "test/pod/new-uid" for e in entities)
    assert discovery.relations()[0].target == "test/pod/new-uid"


def test_chaos_scope_uid_and_validation():
    class Kube:
        namespace = "target-ns"
        def run(self, *args, **kw):
            return {"metadata": {"uid": "replaced"}}
    discovery = SimpleNamespace(kube=Kube(), resolve=lambda s: [{
        "metadata": {"name": "pod", "uid": "original"}, "status": {"phase": "Running"}}])
    provider = ChaosBlade({}, {"target_id": "test", "discovery": discovery})
    request = ActionRequest(action="cpu.load", service="checkout")
    plan = provider.plan(request)
    assert {"name": "namespace", "value": ["target-ns"]} in plan["experiment"]["matchers"]
    with pytest.raises(APIError) as err:
        provider.apply("da-123", plan)
    assert err.value.code == "target_changed"
    with pytest.raises(ValidationError):
        provider.plan(request.model_copy(update={"parameters": {"percent": 99}}))
    with pytest.raises(ValidationError):
        provider.plan(request.model_copy(update={"parameters": {"percent": 10, "namespace": "other"}}))


def test_partial_opensearch_result_is_error(monkeypatch):
    from demo_api.providers import telemetry
    monkeypatch.setattr(telemetry, "request_json", lambda *a, **kw: {"_shards": {"failed": 1}})
    provider = OpenSearch({"url": "http://search"}, {"discovery": SimpleNamespace(telemetry_name=lambda s: s)})
    with pytest.raises(APIError) as err:
        provider.query(query("logs"))
    assert err.value.code == "backend_query_failed"


def test_pod_kill_has_no_runtime_matchers_and_is_irreversible():
    discovery = SimpleNamespace(kube=SimpleNamespace(namespace="target-ns"), resolve=lambda s: [{
        "metadata": {"name": "pod", "uid": "uid"}, "status": {"phase": "Running"}}])
    provider = ChaosBlade({}, {"target_id": "test", "discovery": discovery})
    plan = provider.plan(ActionRequest(action="pod.kill", service="checkout"))
    assert plan["experiment"]["target"] == "pod" and plan["experiment"]["action"] == "delete"
    assert len(plan["experiment"]["matchers"]) == 2
    assert not next(a for a in provider.describe() if a["name"] == "pod.kill")["reversible"]


def test_chaos_cancel_waits_for_backend_deletion():
    import json
    class Kube:
        def __init__(self):
            self.exists = True
        def run(self, *args, **kw):
            if args[0] == "get":
                return json.dumps({"metadata": {"labels": {"demo-api-target": "test"}}}) if self.exists else ""
            return "deletion requested"
    kube = Kube()
    provider = ChaosBlade({}, {"target_id": "test", "discovery": SimpleNamespace(kube=kube)})
    assert provider.cancel("da-test", {})["state"] == "cancelling"
    kube.exists = False
    assert provider.cancel("da-test", {})["state"] == "cancelled"


def test_opensearch_otlp_attribute_keys_with_dots(monkeypatch):
    from demo_api.providers import telemetry
    monkeypatch.setattr(telemetry, "request_json", lambda *a, **kw: {"hits": {"hits": [{"_source": {
        "@timestamp": "2026-10-27T12:00:00Z", "resource": {"attributes": {"service.name": "checkout"}},
        "body": "hello", "trace_id": "abc", "severity_text": "INFO"}}]}})
    provider = OpenSearch({"url": "http://search"}, {"discovery": SimpleNamespace(telemetry_name=lambda s: s)})
    record = provider.query(query("logs")).records[0]
    assert record["body"] == "hello" and record["severity"] == "INFO"
    assert record["timestamp"].endswith("Z")


def test_external_plugin_entry_point(monkeypatch):
    import demo_api.plugins as plugins
    factory = lambda config, context: (config, context)
    monkeypatch.setattr(plugins, "entry_points", lambda **kw: [SimpleNamespace(load=lambda: factory)])
    assert plugins.load_provider("external-demo", {"endpoint": "local"}, {"target_id": "other"}) == (
        {"endpoint": "local"}, {"target_id": "other"})


@pytest.mark.parametrize('tags,expected', [
    ({'otel.status_code': 'ERROR'}, 'error'),
    ({'http.response.status_code': 503}, 'error'),
    ({'http.status_code': '500'}, 'error'),
    ({'rpc.grpc.status_code': 14}, 'error'),
    ({'rpc.response.status_code': 'UNAVAILABLE'}, 'error'),
    ({'rpc.grpc.status_code': 0, 'http.status_code': 200}, 'unset'),
    ({'http.status_code': 404}, 'unset'),
])
def test_span_error_normalization_across_languages(tags, expected):
    from demo_api.providers.telemetry import span_status
    assert span_status(tags) == expected
