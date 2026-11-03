from datetime import datetime, timedelta, timezone

import httpx

from examples.system_overview import collect, render_html, render_text, summarize


def test_overview_discovers_queries_deduplicates_and_preserves_failures():
    end = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    start = end - timedelta(minutes=5)
    entities = [
        {"id": "test/service/web", "name": "web", "kind": "service", "attributes": {"telemetry_service_name": "web-otel"}},
        {"id": "test/service/db", "name": "db", "kind": "service", "attributes": {}},
        {"id": "test/pod/uid", "name": "web-1", "kind": "pod", "attributes": {"phase": "Running"}},
    ]
    spans = [
        {"trace_id": "trace", "span_id": "root", "parent_span_id": None, "service": "web-otel",
         "timestamp": "2026-10-27T11:59:00Z", "duration_ms": 10, "status": "unset"},
        {"trace_id": "trace", "span_id": "child", "parent_span_id": "root", "service": "db",
         "timestamp": "2026-10-27T11:59:01Z", "duration_ms": 5, "status": "error"},
        {"trace_id": "trace", "span_id": "old", "parent_span_id": None, "service": "web-otel",
         "timestamp": "2026-10-27T10:00:00Z", "duration_ms": 9999, "status": "error"},
    ]
    bodies = []
    def handler(req):
        import json
        if req.method == "GET":
            key = req.url.path.rsplit("/", 1)[1]
            resources = {
                "capabilities": {"telemetry": {"metrics": {"configured": False, "reason": "missing"},
                                               "logs": {"configured": True}, "traces": {"configured": True}}},
                "entities": {"entities": entities},
                "relations": {"relations": [{"source": "test/service/web", "target": "test/pod/uid", "kind": "contains"}]},
                "experiments": {"experiments": [{"id": "exp", "state": "unknown", "created_at": 0,
                                                  "action": "cpu.load", "plan": {"service": "web"}}]},
            }
            return httpx.Response(200, json=resources[key])
        body = json.loads(req.content)
        bodies.append(body)
        assert body["start"] == start.isoformat() and body["end"] == end.isoformat()
        if body["service"] == "db" and body["signal"] == "logs":
            return httpx.Response(502, json={"error": {"code": "backend_unavailable"}})
        records = [{"trace_id": "trace", "spans": spans}] if body["signal"] == "traces" else [
            {"timestamp": "2026-10-27T11:59:00Z", "body": '<script>alert("oops")</script>', "severity": "ERROR"}]
        return httpx.Response(200, json={"data": {"records": records, "source": "mock", "truncated": True,
                                                 "warnings": ["sampled"]}})
    with httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler)) as client:
        snapshot = collect(client, {"id": "test"}, start, end)
    summary = summarize(snapshot, start, end)
    snapshot["summary"] = summary
    assert len(bodies) == 4  # No queries for unsupported metrics; all discovered services queried.
    assert summary["unique_spans_returned"] == 3  # Two copies of same trace do not double count.
    assert summary["services"]["web"]["span_count"] == 1  # Excludes out-of-window span.
    assert summary["services"]["web"]["span_p95_ms"] == 10
    assert summary["services"]["db"]["error_spans"] == 1
    assert summary["services"]["web"]["pod_phases"] == {"Running": 1}
    assert summary["call_edges"] == [{"source": "web", "target": "db", "observed_spans": 1}]
    report = {"start": start.isoformat(), "end": end.isoformat(), "targets": [snapshot]}
    text = render_text(report)
    assert "backend_unavailable" in text and "truncated=True" in text and "exp" in text
    html = render_html(report, text)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "marker-end" in html


def test_entity_discovery_failure_does_not_claim_empty_healthy_system():
    now = datetime.now(timezone.utc)
    def handler(req):
        return httpx.Response(502, text="offline")
    with httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler)) as client:
        snapshot = collect(client, {"id": "test"}, now - timedelta(minutes=1), now)
    snapshot["summary"] = summarize(snapshot, now - timedelta(minutes=1), now)
    report = {"start": (now - timedelta(minutes=1)).isoformat(), "end": now.isoformat(), "targets": [snapshot]}
    text = render_text(report)
    assert "请求失败 entities" in text and "API" in text and "不证明健康" in text
    assert "未发现服务" in render_html(report, text)


def test_metric_samples_keep_units_timestamps_and_unknown_values():
    from examples.system_overview import metric_svg
    now = datetime.now(timezone.utc)
    points = [{"timestamp": now.isoformat(), "value": None},
              {"timestamp": (now - timedelta(seconds=15)).isoformat(), "value": 4}]
    plot = metric_svg(points, '<unsafe>')
    assert "&lt;unsafe&gt;" in plot and "<unsafe>" not in plot
    assert "min=4, max=4" in plot
    assert "无有效数值样本" in metric_svg(points[:1], "metric")


def test_metric_applicability_skips_uninstrumented_services():
    import json
    now = datetime.now(timezone.utc)
    queried = []
    def handler(req):
        if req.method == 'POST':
            queried.append(json.loads(req.content))
            return httpx.Response(200, json={'data': {'records': [], 'source': 'mock'}})
        key = req.url.path.rsplit('/', 1)[1]
        values = {
            'entities': {'entities': [{'id': 't/service/' + n, 'name': n, 'kind': 'service'} for n in ['app', 'db']]},
            'capabilities': {'telemetry': {'metrics': {'configured': True, 'metrics': [
                {'name': 'rpc.rate', 'services': ['app']}]}}},
            'relations': {'relations': []}, 'experiments': {'experiments': []}}
        return httpx.Response(200, json=values[key])
    with httpx.Client(base_url='http://api', transport=httpx.MockTransport(handler)) as client:
        snapshot = collect(client, {'id': 't'}, now - timedelta(minutes=1), now)
    assert len(queried) == 1 and queried[0]['service'] == 'app'
    assert summarize(snapshot, now - timedelta(minutes=1), now)['findings']


def test_database_dependency_requires_exact_entity_evidence_and_operations_separate():
    now = datetime.now(timezone.utc)
    span = {'trace_id': 'a', 'span_id': '1', 'service': 'web', 'name': 'sql.query',
            'timestamp': now.isoformat(), 'duration_ms': 2, 'status': 'unset',
            'attributes': {'db.system.name': 'postgresql', 'server.address': 'db'}}
    snapshot = {'resources': {
        'entities': {'value': {'entities': [{'id': n, 'kind': 'service', 'name': n} for n in ['web', 'db']]}},
        'relations': {'value': {'relations': []}}}, 'observations': [
            {'service': 'web', 'signal': 'traces', 'metric': None, 'status': 'ok',
             'value': {'data': {'records': [{'spans': [span]}]}}}]}
    summary = summarize(snapshot, now - timedelta(minutes=1), now)
    assert summary['dependencies'][0]['target'] == 'db'
    assert summary['operations'][0]['kind'] == 'database'
    assert summary['operations'][0]['p95_ms'] == 2
    span['attributes']['server.address'] = 'some-external-db'
    assert not summarize(snapshot, now - timedelta(minutes=1), now)['dependencies']
